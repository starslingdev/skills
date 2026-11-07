"""OPT9 distinguishes measured tool caches from speculative build state."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402
from test_opt81_faster_runner import _CI, _Opt81Client  # noqa: E402


def _collect(monkeypatch) -> dict:
    client = _Opt81Client([("ubuntu-latest", 197)] * 8)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = {
        "repo": "o/r",
        "findings": [
            {
                "id": "f1",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["typecheck"],
                "title": "TypeScript incremental build state",
                "evidence": (
                    "job `typecheck` runs `tsc` without `--incremental`; the proposed "
                    "cache persists `.tsbuildinfo`"
                ),
            },
            {
                "id": "f2",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["generate-client"],
                "title": "Generated client cache",
                "evidence": (
                    "job `vitest-codegen` regenerates an API client and could cache the "
                    "generated-client output"
                ),
            },
            {
                "id": "f3",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["test"],
                "title": "TypeScript ESLint cache",
                "evidence": (
                    "job `test` runs `eslint` over generated code without its "
                    "`--cache` flag"
                ),
            },
            {
                "id": "f4",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["test"],
                "title": "TypeScript Vitest cache",
                "evidence": (
                    "job `test` runs `vitest` over generated TypeScript fixtures "
                    "without its result cache"
                ),
            },
        ],
    }
    return cr.collect(doc, "o/r", max_runs=8, shallow_runs=8)


def _verify_full(doc: dict, tmp_path: Path) -> str:
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    doc["skill_commit_sha"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_SKILL, text=True).strip()
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    assert bp.main(["--in", str(findings_path), "--out", str(report_path)]) == 0
    proc = subprocess.run(
        [
            sys.executable,
            str(_SKILL / "tests" / "verify_report.py"),
            "--report",
            str(report_path),
            "--findings",
            str(findings_path),
        ],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return report_path.read_text(encoding="utf-8")


def test_opt9_cache_shapes_stay_uncredited_without_the_cache_guard_inputs(
        monkeypatch, tmp_path):
    doc = _collect(monkeypatch)
    by_id = {finding["id"]: finding for finding in doc["findings"]}

    for finding_id in ("f1", "f2"):
        finding = by_id[finding_id]
        assert finding["wall_clock_p50_s"] is None
        assert finding["runner_min_saving"] is None
        assert finding["tier"] == 2
        assert finding["realization"] == "none"
        assert finding["opt9_measure_before_after"] is True
        assert "measure before and after" in finding["size_note"].lower()

    for finding_id in ("f3", "f4"):
        finding = by_id[finding_id]
        assert finding["wall_clock_p50_s"] is None
        assert finding["runner_min_saving"] is None
        assert finding["tier"] == 2
        assert finding["realization"] == "none"
        assert finding["runner_min_unrealizable_s"] == 75.0
        assert finding["runner_min_unrealizable"] is True
        assert finding["sizing_basis"] == "modeled"
        assert "ephemeral-runner caveat" in finding["size_note"]
        assert "cache saving uncredited" in finding["size_note"]
        assert finding["cache_sizing_guard"]["status"] == "not_measured"

    report = _verify_full(doc, tmp_path)
    assert "measure before and after" in report.lower()


def test_scanned_tsc_stays_uncredited_in_result_cache_named_job(
        monkeypatch, tmp_path):
    workflow = """name: CI
on: pull_request
jobs:
  eslint-typecheck:
    runs-on: ubuntu-latest
    steps:
      - run: tsc -p packages/eslint-config/tsconfig.json --noEmit
"""
    workflow_path = tmp_path / _CI
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(workflow, encoding="utf-8")
    scanned = scan.scan(
        tmp_path, _SKILL / "references" / "patterns", default_branch="main")

    class _NamedJobClient(_Opt81Client):
        def _job(self, i):
            job = super()._job(i)
            job["name"] = "eslint-typecheck"
            return job

        def json(self, endpoint, allow_missing=False):
            payload = super().json(endpoint, allow_missing=allow_missing)
            if (endpoint.startswith("repos/o/r/actions/workflows/1/runs?")
                    and "workflow_runs" in payload):
                for number, run in enumerate(payload["workflow_runs"], 1):
                    run["pull_requests"] = [{"number": number}]
            if endpoint.startswith("repos/o/r/commits/s"):
                payload["check_runs"][0]["name"] = "eslint-typecheck"
            return payload

    client = _NamedJobClient([("ubuntu-latest", 197)] * 8, yaml=workflow)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect(scanned, "o/r", max_runs=8, shallow_runs=8)
    finding = next(
        f for f in doc["findings"]
        if f["pattern"] == "OPT9" and f.get("opt9_tool") == "tsc"
    )

    assert finding.get("opt9_measure_before_after") is True
    assert finding["wall_clock_p50_s"] is None
    assert finding["runner_min_saving"] is None
    _verify_full(doc, tmp_path)
