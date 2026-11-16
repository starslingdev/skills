from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan as scan_mod  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "rust_coverage_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)

_WORKFLOW = ".github/workflows/coverage.yml"


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


_WORKFLOW_TEXT = """\
on: pull_request
jobs:
  coverage:
    name: Rust coverage
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run coverage
        run: cargo llvm-cov --workspace --lcov
"""


class _Client:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{
                "id": 1, "path": _WORKFLOW, "name": "coverage", "state": "active",
            }]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 2}
            return {"workflow_runs": [
                {
                    "id": run_id,
                    "event": "pull_request",
                    "head_branch": f"feature-{run_id}",
                    "head_sha": f"sha-{run_id}",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": _iso(run_id - 101),
                    "run_started_at": _iso(run_id - 101),
                    "updated_at": _iso(620 + run_id - 101),
                }
                for run_id in (101, 102)
            ]}
        match = re.match(r"repos/o/r/actions/runs/(101|102)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            return {"jobs": [{
                "id": run_id * 10 + 1,
                "run_id": run_id,
                "name": "Rust coverage",
                "conclusion": "success",
                "labels": ["ubuntu-latest"],
                "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/1",
                "started_at": _iso(0),
                "completed_at": _iso(620),
                "steps": [
                    {"name": "Checkout", "conclusion": "success",
                     "started_at": _iso(0), "completed_at": _iso(20)},
                    {"name": "Run coverage", "conclusion": "success",
                     "started_at": _iso(20), "completed_at": _iso(620)},
                ],
            }]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [{
                "name": "Rust coverage", "started_at": _iso(0),
                "completed_at": _iso(620),
            }]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["Rust coverage"]}
        if endpoint == f"repos/o/r/contents/{_WORKFLOW}":
            return {"content": base64.b64encode(_WORKFLOW_TEXT.encode()).decode()}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


def _scan_doc(tmp_path: Path, cargo: str = "[package]\nname = 'demo'\nversion = '0.1.0'\n") -> dict:
    workflow = tmp_path / _WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text(_WORKFLOW_TEXT, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(cargo, encoding="utf-8")
    return scan_mod.scan(tmp_path, SKILL / "references" / "patterns")


def _collect(monkeypatch, tmp_path: Path, cargo: str | None = None) -> tuple[dict, str]:
    monkeypatch.setattr(cr, "GhClient", _Client)
    doc = _scan_doc(tmp_path, cargo or "[package]\nname = 'demo'\nversion = '0.1.0'\n")
    doc["repo"] = "o/r"
    doc["skill_commit_sha"] = "installed:unversioned"
    out = cr.collect(doc, "o/r", max_runs=2, shallow_runs=2)
    report = bp.render(out, captured_at="2026-11-16T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(out), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    return out, report


def test_measured_rust_coverage_pole_reports_unoptimized_debug_profile(
        monkeypatch, tmp_path: Path) -> None:
    doc, report = _collect(monkeypatch, tmp_path)
    findings = [f for f in doc["findings"] if f.get("pattern") == "OPT104"]
    assert len(findings) == 1, "the measured coverage pole needs its Rust profile finding"
    finding = findings[0]
    assert finding["sizing_basis"] == "uncredited"
    assert finding["wall_clock_p50_s"] == 0.0
    assert finding["runner_min_saving"] is None
    assert "covered-line count" in report


def test_explicit_rust_test_profile_stays_quiet(monkeypatch, tmp_path: Path) -> None:
    cargo = """\
[package]
name = "demo"
version = "0.1.0"

[profile.test]
debug = 0
opt-level = 1
"""
    doc, _report = _collect(monkeypatch, tmp_path, cargo)
    assert not any(f.get("pattern") == "OPT104" for f in doc["findings"])
