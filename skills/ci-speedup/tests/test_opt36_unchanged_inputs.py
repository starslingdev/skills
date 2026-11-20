from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))
import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_WF = ".github/workflows/codeql.yml"
_YAML = """
name: CodeQL
on:
  schedule:
    - cron: '0 * * * *'
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: github/codeql-action/init@v4
        with:
          config: |
            paths-ignore:
              - tests/**
              - docs/**
      - uses: github/codeql-action/analyze@v4
"""


def _verify_module():
    name = "ci_speedup_verify_opt36_unchanged"
    spec = importlib.util.spec_from_file_location(name, _SKILL / "tests/verify_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Client:
    gave_up = False
    errors = 0
    queries = 0

    def available(self):
        return True

    @staticmethod
    def _run(index: int) -> dict:
        sha = ("base", "docs", "tests")[index]
        return {
            "id": 100 + index, "event": "schedule", "head_sha": sha,
            "head_branch": "main", "status": "completed", "conclusion": "success",
            "run_attempt": 1, "created_at": f"2026-11-0{index + 1}T00:00:00Z",
            "run_started_at": f"2026-11-0{index + 1}T00:00:00Z",
            "updated_at": f"2026-11-0{index + 1}T00:02:00Z",
        }

    @classmethod
    def _pr_run(cls, index: int) -> dict:
        row = cls._run(index)
        row.update(event="pull_request", head_sha=f"pr-{index}",
                   head_branch=f"feature-{index}",
                   pull_requests=[{"number": 10 + index}])
        return row

    @staticmethod
    def _job(index: int) -> dict:
        day = index + 1
        return {
            "id": 500 + index, "run_id": 100 + index, "name": "scan",
            "status": "completed", "conclusion": "success", "run_attempt": 1,
            "started_at": f"2026-11-0{day}T00:00:00Z",
            "completed_at": f"2026-11-0{day}T00:02:00Z",
            "runner_name": "ubuntu-latest", "labels": ["ubuntu-latest"],
            "steps": [{"name": "Analyze", "number": 1, "status": "completed",
                       "conclusion": "success",
                       "started_at": f"2026-11-0{day}T00:00:00Z",
                       "completed_at": f"2026-11-0{day}T00:02:00Z"}],
        }

    def json(self, endpoint, allow_missing=False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WF, "name": "CodeQL",
                                    "state": "active"}]}
        if endpoint.startswith("repos/o/r/actions/workflows/1/runs?"):
            if re.search(r"per_page=1(?:&|$)", endpoint):
                return {"total_count": 30, "workflow_runs": [self._run(2)]}
            schedule = "event=schedule" in endpoint
            rows = [self._run(index) if schedule else self._pr_run(index)
                    for index in range(3)]
            return {"total_count": 3, "workflow_runs": rows}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"total_count": 1, "jobs": [self._job(int(match.group(1)) - 100)]}
        if endpoint.startswith(f"repos/o/r/contents/{_WF}"):
            return {"content": base64.b64encode(_YAML.encode()).decode()}
        if endpoint == "repos/o/r/compare/base...docs":
            return {"files": [{"filename": "docs/api.md"}]}
        if endpoint == "repos/o/r/compare/docs...tests":
            return {"files": [{"filename": "tests/api.test.ts"}]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        match = re.match(r"repos/o/r/commits/pr-(\d+)/check-runs", endpoint)
        if match:
            job = self._job(int(match.group(1)))
            return {"check_runs": [{"name": "CodeQL / scan",
                                     "started_at": job["started_at"],
                                     "completed_at": job["completed_at"],
                                     "conclusion": "success"}]}
        return None

    def text(self, endpoint, allow_missing=False):
        self.queries += 1
        return ""


def test_opt36_unchanged_scanner_inputs_collect_render_and_fully_verify(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _Client)
    doc = cr.collect({"repo": "o/r", "findings": [
                          {"id": "f1", "pattern": "OPT36",
                           "workflow_file": _WF}],
                      "skill_commit_sha": "installed:unversioned"},
                     "o/r", max_runs=3, shallow_runs=3)
    findings = [finding for finding in doc["findings"]
                if finding.get("pattern") == "OPT36"
                and finding.get("opt36_unchanged_inputs")]
    assert len(findings) == 1
    assert findings[0]["opt36_unchanged_inputs"]["excluded_only_pairs"] == 2

    report = bp.render(doc, captured_at="2026-11-20T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    verifier = _verify_module()
    assert verifier._vr_opt36_unchanged_inputs_valid(findings[0])
    tampered = json.loads(json.dumps(findings[0]))
    tampered["opt36_unchanged_inputs"]["pairs"][0]["changed_paths"] = ["src/app.ts"]
    assert not verifier._vr_opt36_unchanged_inputs_valid(tampered)
    failed = [check for check in verifier.run_checks(
        report, report_path, findings_path, None) if not check.skipped and not check.ok]
    assert failed == [], [(check.name, check.detail) for check in failed]
