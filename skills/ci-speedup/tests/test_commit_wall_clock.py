from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import wall_clock as wc  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "commit_wall_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_FIRST = ".github/workflows/first.yml"
_SECOND = ".github/workflows/second.yml"
_SHA = "sample-sha"


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def _job(name: str, run_id: int, start_s: int, end_s: int) -> dict:
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(start_s),
        "completed_at": _iso(end_s),
        "steps": [{
            "name": "work", "started_at": _iso(start_s),
            "completed_at": _iso(end_s),
        }],
    }


class _TwoWorkflowClient:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _FIRST, "name": "first"},
                {"id": 2, "path": _SECOND, "name": "second"},
            ]}
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            workflow_id = int(match.group(1))
            if re.search(r"per_page=1(?![0-9])", match.group(2)):
                return {"total_count": 12}
            created = 0 if workflow_id == 1 else 60
            return {"workflow_runs": [{
                "id": 101 if workflow_id == 1 else 202,
                "event": "pull_request",
                "head_sha": _SHA,
                "status": "completed",
                "conclusion": "success",
                "created_at": _iso(created),
                "run_started_at": _iso(created),
                "updated_at": _iso(900 if workflow_id == 1 else 1500),
            }]}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            if run_id == 101:
                return {"jobs": [
                    _job("first-short", run_id, 0, 300),
                    _job("first-long", run_id, 0, 900),
                ]}
            if run_id == 202:
                return {"jobs": [_job("second-long", run_id, 60, 1500)]}
            return {"jobs": []}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [
                {"name": "first-short", "started_at": _iso(0),
                 "completed_at": _iso(300)},
                {"name": "first-long", "started_at": _iso(0),
                 "completed_at": _iso(900)},
                {"name": "second-long", "started_at": _iso(60),
                 "completed_at": _iso(1500)},
            ]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["second-long"]}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _MissingSecondWorkflowJobsClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint.startswith("repos/o/r/actions/runs/202/jobs"):
            self.queries += 1
            return {"jobs": []}
        return super().json(endpoint, allow_missing)


_DOC = {
    "repo": "o/r",
    "skill_commit_sha": "installed:unversioned",
    "workflow_job_graph": {
        _FIRST: {
            "first-short": {"name": "first-short", "needs": []},
            "first-long": {"name": "first-long", "needs": []},
        },
        _SECOND: {
            "second-long": {"name": "second-long", "needs": []},
        },
    },
    "findings": [
        {"id": "first", "workflow_file": _FIRST},
        {"id": "second", "workflow_file": _SECOND},
    ],
}


def _collect(monkeypatch, client_type=_TwoWorkflowClient) -> dict:
    monkeypatch.setattr(cr, "GhClient", client_type)
    return cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=1, shallow_runs=1)


def test_collect_stamps_one_commit_wall_across_both_workflows_and_verifies(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    cp = doc["pr_critical_path"]

    assert "commit_wall_clock" in cp
    stamp = cp["commit_wall_clock"]
    assert stamp["p50_s"] == 1500.0
    assert stamp["p90_s"] == 1500.0
    assert len(stamp["facts"]) == 1
    fact = stamp["facts"][0]
    assert fact["wall_s"] == 1500.0
    assert fact["slowest_job"]["name"] == "second-long"
    assert fact["slowest_job"]["duration_s"] == 1440.0
    assert fact["next_longest_job"]["name"] == "first-long"
    assert fact["next_longest_job"]["duration_s"] == 900.0

    projection = wc.project_commit_wall(1500, 1440, 900, 4)
    assert projection.projected_s == 960.0
    assert projection.multiple == 1.56
    assert fact["projection_4x"] == {
        "job_speedup": 4.0, "projected_s": 960.0, "multiple": 1.56,
    }

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    assert "Per-commit PR wait" in report
    assert "p50 **25m 00s**" in report
    assert "p90 **25m 00s**" in report
    assert "projects to **16m 00s** (**1.56x**)" in report

    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_missing_workflow_jobs_make_the_commit_partial_instead_of_shorter(monkeypatch):
    doc = _collect(monkeypatch, _MissingSecondWorkflowJobsClient)
    stamp = doc["pr_critical_path"]["commit_wall_clock"]
    fact = stamp["facts"][0]

    assert fact["partial"] is True
    assert fact["wall_s"] is None
    assert fact["observed_wall_s"] == 900.0
    assert fact["missing_workflows"] == [_SECOND]
    assert stamp["p50_s"] is None
    assert stamp["p90_s"] is None


def test_verifier_rejects_a_projection_multiple_above_the_shared_cap(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    doc["pr_critical_path"]["commit_wall_clock"]["facts"][0][
        "projection_4x"]["multiple"] = 2.0
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")

    check = vr.check_commit_wall_clock_projection(report, findings_path)
    assert not check.ok
    assert "exceeds 1.56" in check.detail
