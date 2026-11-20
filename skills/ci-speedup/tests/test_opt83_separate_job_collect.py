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

_WF = ".github/workflows/quality.yml"
_YAML = """
name: Quality
on: pull_request
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Lint
        run: go test ./a/...
      - name: Typecheck
        run: go test ./b/...
"""


def _verify_module():
    name = "ci_speedup_verify_opt83_separate"
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
    def _run(index):
        return {"id": 100 + index, "event": "pull_request", "head_sha": f"sha-{index}",
                "head_branch": f"feature-{index}", "status": "completed",
                "conclusion": "success", "run_attempt": 1,
                "pull_requests": [{"number": index + 1}],
                "created_at": f"2026-11-0{index + 1}T00:00:00Z",
                "run_started_at": f"2026-11-0{index + 1}T00:00:00Z",
                "updated_at": f"2026-11-0{index + 1}T00:01:55Z"}

    @staticmethod
    def _job(index):
        day = index + 1
        spans = [("Set up job", 0, 5), ("Checkout", 5, 15),
                 ("Lint", 15, 75), ("Typecheck", 75, 115),
                 ("Complete job", 115, 115)]
        return {"id": 500 + index, "run_id": 100 + index, "name": "quality",
                "status": "completed", "conclusion": "success", "run_attempt": 1,
                "runner_name": "ubuntu-latest", "labels": ["ubuntu-latest"],
                "started_at": f"2026-11-0{day}T00:00:00Z",
                "completed_at": f"2026-11-0{day}T00:01:55Z",
                "steps": [{"name": name, "number": number, "status": "completed",
                           "conclusion": "success",
                           "started_at": f"2026-11-0{day}T00:{start // 60:02d}:{start % 60:02d}Z",
                           "completed_at": f"2026-11-0{day}T00:{end // 60:02d}:{end % 60:02d}Z"}
                          for number, (name, start, end) in enumerate(spans, 1)]}

    def json(self, endpoint, allow_missing=False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WF, "name": "Quality",
                                    "state": "active"}]}
        if endpoint.startswith("repos/o/r/actions/workflows/1/runs?"):
            if re.search(r"per_page=1(?:&|$)", endpoint):
                return {"total_count": 30, "workflow_runs": [self._run(2)]}
            return {"total_count": 3,
                    "workflow_runs": [self._run(index) for index in range(3)]}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"total_count": 1, "jobs": [self._job(int(match.group(1)) - 100)]}
        if endpoint.startswith(f"repos/o/r/contents/{_WF}"):
            return {"content": base64.b64encode(_YAML.encode()).decode()}
        match = re.match(r"repos/o/r/commits/sha-(\d+)/check-runs", endpoint)
        if match:
            job = self._job(int(match.group(1)))
            return {"check_runs": [{"name": "Quality / quality", "conclusion": "success",
                                     "started_at": job["started_at"],
                                     "completed_at": job["completed_at"]}]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None

    def text(self, endpoint, allow_missing=False):
        self.queries += 1
        return ""


def test_opt83_separate_job_collects_renders_and_fully_verifies(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _Client)
    doc = cr.collect({"repo": "o/r", "findings": [
                          {"id": "f1", "pattern": "OPT1", "workflow_file": _WF}],
                      "skill_commit_sha": "installed:unversioned"},
                     "o/r", max_runs=3, shallow_runs=3)
    findings = [finding for finding in doc["findings"]
                if finding.get("pattern") == "OPT83"
                and (finding.get("independent_steps") or {}).get("kind")
                == "opt83_separate_jobs"]
    assert len(findings) == 1
    assert findings[0]["wall_clock_p50_s"] == 40.0

    report = bp.render(doc, captured_at="2026-11-20T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    verifier = _verify_module()
    failed = [check for check in verifier.run_checks(
        report, report_path, findings_path, None) if not check.skipped and not check.ok]
    assert failed == [], [(check.name, check.detail) for check in failed]
