"""Small whole-pipeline fixtures for report-level interaction coverage."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402
import summary  # noqa: E402


HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE / "fixtures" / "e2e"
CATALOG = HERE.parent / "references" / "patterns"

_VR_SPEC = importlib.util.spec_from_file_location(
    "e2e_fixture_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


class FixtureClient:
    """Small recorded-response client; unknown optional reads stay unavailable."""

    gave_up = False

    def __init__(self, recorded: dict) -> None:
        self.recorded = recorded
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return not self.gave_up

    def _bump(self, *, error: bool = False) -> None:
        if error:
            self.errors += 1

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        expire_after = self.recorded.get("expire_after")
        if isinstance(expire_after, int) and self.queries > expire_after:
            self.errors += 1
            self.gave_up = True
            return None
        repo = self.recorded["repo"]
        if endpoint == f"repos/{repo}":
            return {"default_branch": self.recorded.get("default_branch", "main")}
        if endpoint.startswith(f"repos/{repo}/actions/workflows?"):
            return {"workflows": [
                {"id": row["id"], "path": row["path"],
                 "name": row.get("name", Path(row["path"]).stem)}
                for row in self.recorded["workflows"]
            ]}
        match = re.match(
            rf"repos/{re.escape(repo)}/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            workflow = next(
                row for row in self.recorded["workflows"]
                if row["id"] == int(match.group(1)))
            if re.search(r"(?:^|&)per_page=1(?:&|$)", match.group(2)):
                return {"total_count": workflow.get(
                    "runs_30d", len(workflow.get("runs", [])))}
            return {"workflow_runs": workflow.get("runs", [])}
        match = re.match(
            rf"repos/{re.escape(repo)}/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"jobs": self.recorded.get("jobs", {}).get(match.group(1), [])}
        match = re.match(
            rf"repos/{re.escape(repo)}/commits/([^/]+)/check-runs", endpoint)
        if match:
            return {"check_runs": self.recorded.get("checks", {}).get(
                match.group(1), [])}
        if endpoint == f"repos/{repo}/rulesets?includes_parents=true":
            return []
        if endpoint == (
                f"repos/{repo}/branches/{self.recorded.get('default_branch', 'main')}"
                "/protection/required_status_checks"):
            return {"contexts": self.recorded.get("required_checks", ["gate"])}
        if "/commits?path=" in endpoint:
            return []
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        match = re.search(r"/actions/jobs/(\d+)/logs$", endpoint)
        if match:
            return self.recorded.get("logs", {}).get(match.group(1), "")
        return ""


def run_fixture(path: Path, monkeypatch, tmp_path: Path) -> tuple[dict, str, list]:
    recorded = json.loads((path / "responses.json").read_text(encoding="utf-8"))
    repo_root = tmp_path / f"{path.name}-repo"
    shutil.copytree(path, repo_root)
    workflow_root = repo_root / ".github/workflows"
    workflow_root.mkdir(parents=True)
    for workflow in (repo_root / "workflows").glob("*.yml"):
        shutil.copy2(workflow, workflow_root / workflow.name)
    client = FixtureClient(recorded)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(cr, "_root_is_clone_of", lambda *args, **kwargs: True)
    monkeypatch.setattr(cr, "_workflows_are_dirty", lambda *args, **kwargs: False)
    monkeypatch.setattr(cr, "_root_branch_skew", lambda *args, **kwargs: None)
    scanned = scan.scan(
        repo_root, CATALOG, default_branch=recorded.get("default_branch", "main"))
    scanned["skill_commit_sha"] = "installed:unversioned"
    doc = cr.collect(
        scanned, recorded["repo"], max_runs=20,
        shallow_runs=recorded.get("shallow_runs", 20),
        with_logs=True, data_dir=tmp_path / f"{path.name}-data",
        created_before=recorded["captured_at"], root=repo_root)
    logs: dict[str, str] = {}
    steps: dict[str, dict] = {}
    mags: dict[str, dict] = {}
    log_runs: dict[str, str] = {}
    bundle = doc.get("data_bundle") or {}
    entries = bundle.get("logs") or []
    logs_dir = Path(bundle.get("logs_dir") or tmp_path)
    for key, entry in zip(summary._render_keys(entries), entries):
        if "=" in key:
            continue
        if entry.get("file"):
            logs[key] = (logs_dir / entry["file"]).read_text()
        if entry.get("steps_file"):
            steps[key] = json.loads((logs_dir / entry["steps_file"]).read_text())
        if entry.get("mag_file"):
            mags[key] = json.loads((logs_dir / entry["mag_file"]).read_text())
        if entry.get("html_url"):
            log_runs[key] = entry["html_url"]
    report = bp.render(
        doc, logs, steps=steps, mags=mags, log_runs=log_runs,
        captured_at=recorded["captured_at"])
    out = tmp_path / path.name
    out.mkdir()
    findings_path = out / "findings.json"
    report_path = out / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    return doc, report, checks


def fixture_facts(doc: dict, checks: list) -> dict:
    cp = doc.get("pr_critical_path") or {}
    poles = cp.get("poles") or []
    crown = poles[0] if poles else {}
    clock = cp.get("commit_wall_clock") or {}
    lane = doc.get("default_branch_push_lane") or {}
    bundle = doc.get("data_bundle") or {}
    logs_dir = Path(bundle.get("logs_dir") or ".")
    leaves = []
    for entry in bundle.get("logs") or []:
        if not entry.get("file"):
            continue
        leaf = bp._parse_log((logs_dir / entry["file"]).read_text())
        if leaf and leaf.get("fix_key"):
            leaves.append(str(leaf["fix_key"]))
    leaves.sort()
    credited = [
        {"pattern": row.get("pattern"),
         "wall_clock_s": row.get("wall_clock_p50_s"),
         "runner_min": row.get("runner_min_saving")}
        for row in doc.get("findings", [])
        if ((isinstance(row.get("wall_clock_p50_s"), (int, float))
             and row.get("wall_clock_p50_s") > 0)
            or (isinstance(row.get("runner_min_saving"), (int, float))
                and row.get("runner_min_saving") > 0))
    ]
    return {
        "headline": {
            "check": cp.get("critical_path_check"),
            "seconds": cp.get("critical_path_s"),
        },
        "gate_kind": crown.get("gate_kind"),
        "provenance": crown.get("provenance"),
        "commit_wall_clock": {"p50_s": clock.get("p50_s"), "p90_s": clock.get("p90_s")},
        "representative_jobs": {
            "slowest": (clock.get("slowest_job") or {}).get("name"),
            "next": (clock.get("next_longest_job") or {}).get("name"),
        },
        "credited_findings": credited,
        "lane": {
            "pushes_30d": lane.get("pushes_30d"),
            "sampled_pushes": lane.get("sampled_push_count"),
            "job_fetches": lane.get("job_fetch_count"),
            "p50_s": lane.get("p50_s"),
        },
        "leaves": leaves,
        "verifier": {
            "fail": sorted(check.name for check in checks if not check.skipped and not check.ok),
            "skip": sorted(check.name for check in checks if check.skipped),
        },
    }


def _first_fact_difference(expected, actual, path: str = "") -> tuple[str, object, object]:
    """Return the first compact-fact path that moved."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else str(key)
            if key not in expected or key not in actual:
                return child, expected.get(key), actual.get(key)
            if expected[key] != actual[key]:
                return _first_fact_difference(expected[key], actual[key], child)
    if isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                return _first_fact_difference(left, right, f"{path}[{index}]")
        if len(expected) != len(actual):
            return f"{path}.length", len(expected), len(actual)
    return path, expected, actual


def assert_fixture_facts(fixture: Path, actual: dict) -> None:
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    if actual != expected:
        path, before, after = _first_fact_difference(expected, actual)
        raise AssertionError(
            f"{fixture.name}: {path} changed from {before!r} to {after!r}")


def test_fixture_corpus_has_broad_interaction_coverage():
    fixtures = sorted(
        path for path in (FIXTURE_ROOT.iterdir() if FIXTURE_ROOT.exists() else ())
        if path.is_dir())

    assert len(fixtures) >= 10, (
        f"expected at least 10 whole-pipeline fixtures, found {len(fixtures)}")


@pytest.mark.parametrize(
    "fixture", sorted(path for path in FIXTURE_ROOT.glob("*") if path.is_dir()),
    ids=lambda path: path.name)
def test_fixture_runs_through_collect_render_and_full_verifier(
        fixture: Path, monkeypatch, tmp_path: Path):
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [
        (check.name, check.detail) for check in failures]

    assert_fixture_facts(fixture, fixture_facts(doc, checks))


def test_fixture_set_catches_required_crown_regression(monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "unpinnable-slow-check"
    real_collect = cr.collect

    def reverted_collect(*args, **kwargs):
        doc = real_collect(*args, **kwargs)
        path = doc.get("pr_critical_path") or {}
        checks = path.get("checks") or []
        if checks:
            old_crown = max(checks, key=lambda row: float(row.get("p50_s") or 0))
            path["critical_path_check"] = old_crown["name"]
            path["critical_path_s"] = old_crown["p50_s"]
        return doc

    monkeypatch.setattr(cr, "collect", reverted_collect)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    with pytest.raises(
            AssertionError,
            match=r"unpinnable-slow-check: headline\.check changed"):
        assert_fixture_facts(fixture, fixture_facts(doc, checks))


def test_fixture_set_catches_leaf_precedence_regression(monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    real_parse = bp._parse_log

    def reverted_parse(text: str, *args, **kwargs):
        if "Cache not found for input keys:" in text and "./run-test.sh" in text:
            text = "\n".join(
                line for line in text.splitlines()
                if "Cache not found for input keys:" not in line)
        return real_parse(text, *args, **kwargs)

    monkeypatch.setattr(bp, "_parse_log", reverted_parse)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    with pytest.raises(
            AssertionError, match=r"serial-chain: leaves\[0\] changed"):
        assert_fixture_facts(fixture, fixture_facts(doc, checks))


def test_fixture_set_catches_unconfirmed_gate_credit_regression(
        monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "unpinnable-slow-check"
    monkeypatch.setattr(cr, "_pole_is_required_reachable", lambda *args, **kwargs: True)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    with pytest.raises(
            AssertionError,
            match=(r"unpinnable-slow-check: credited_findings\[1\]\."
                   r"wall_clock_s changed")):
        assert_fixture_facts(fixture, fixture_facts(doc, checks))
