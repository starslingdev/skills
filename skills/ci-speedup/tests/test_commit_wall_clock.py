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


class _MissingSecondWorkflowRunClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        match = re.match(r"repos/o/r/actions/workflows/2/runs\?(.*)", endpoint)
        if match and not re.search(r"per_page=1(?![0-9])", match.group(1)):
            self.queries += 1
            return {"workflow_runs": []}
        return super().json(endpoint, allow_missing)


class _PullRequestAndQueueClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match and not re.search(r"per_page=1(?![0-9])", match.group(2)):
            result = super().json(endpoint, allow_missing)
            pull_request_run = dict(
                result["workflow_runs"][0], pull_requests=[{"number": 7}])
            queue_run = dict(
                pull_request_run,
                id=int(pull_request_run["id"]) + 1000,
                event="merge_group",
                head_sha="queue-sha",
                head_branch="gh-readonly-queue/main/pr-7-abc",
                pull_requests=[],
                created_at=_iso(120),
                run_started_at=_iso(120),
            )
            return {"workflow_runs": [queue_run, pull_request_run]}
        match = re.match(r"repos/o/r/actions/runs/(1\d{3})/jobs", endpoint)
        if match:
            return super().json(
                endpoint.replace(match.group(1), str(int(match.group(1)) - 1000), 1),
                allow_missing)
        return super().json(endpoint, allow_missing)


class _MatrixSiblingClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint.startswith("repos/o/r/actions/runs/202/jobs"):
            self.queries += 1
            return {"jobs": [
                _job("second-long (one)", 202, 60, 1500),
                _job("second-long (two)", 202, 60, 1060),
            ]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            self.queries += 1
            return {"check_runs": [
                {"name": "first-short", "started_at": _iso(0),
                 "completed_at": _iso(300)},
                {"name": "first-long", "started_at": _iso(0),
                 "completed_at": _iso(900)},
                {"name": "second-long (one)", "started_at": _iso(60),
                 "completed_at": _iso(1500)},
                {"name": "second-long (two)", "started_at": _iso(60),
                 "completed_at": _iso(1060)},
            ]}
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            self.queries += 1
            return {"contexts": ["second-long (one)"]}
        return super().json(endpoint, allow_missing)


class _FailedWorkflowRunClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        match = re.match(r"repos/o/r/actions/workflows/2/runs\?(.*)", endpoint)
        if match and not re.search(r"per_page=1(?![0-9])", match.group(1)):
            self.queries += 1
            return {"workflow_runs": [{
                "id": 202,
                "event": "pull_request",
                "head_sha": _SHA,
                "status": "completed",
                "conclusion": "failure",
                "created_at": _iso(60),
                "run_started_at": _iso(60),
                "updated_at": _iso(120),
            }]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            self.queries += 1
            return {"check_runs": [
                {"name": "first-short", "started_at": _iso(0),
                 "completed_at": _iso(300)},
                {"name": "first-long", "started_at": _iso(0),
                 "completed_at": _iso(900)},
            ]}
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            self.queries += 1
            return {"contexts": ["first-long"]}
        return super().json(endpoint, allow_missing)


class _ZeroWallClient(_TwoWorkflowClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match and not re.search(r"per_page=1(?![0-9])", match.group(2)):
            result = super().json(endpoint, allow_missing)
            for run in result["workflow_runs"]:
                run.update({
                    "created_at": _iso(0), "run_started_at": _iso(0),
                    "updated_at": _iso(0),
                })
            return result
        match = re.match(r"repos/o/r/actions/runs/(101|202)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            name = "first-long" if run_id == 101 else "second-long"
            self.queries += 1
            return {"jobs": [_job(name, run_id, 0, 0)]}
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
    zero_projection = wc.project_commit_wall(100, 100, 0, float("inf"))
    assert zero_projection.projected_s == 0.0
    assert zero_projection.multiple is None
    json.dumps({"multiple": zero_projection.multiple}, allow_nan=False)

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
    assert all(sum(
        1 for step in (finding.get("wall_clock_derivation") or [])
        if step.get("bound") == "commit-projection") <= 1
        for finding in doc["findings"])


def test_missing_workflow_run_makes_the_commit_partial_instead_of_shorter(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch, _MissingSecondWorkflowRunClient)
    stamp = doc["pr_critical_path"]["commit_wall_clock"]
    fact = stamp["facts"][0]

    assert fact["partial"] is True
    assert fact["wall_s"] is None
    assert fact["observed_wall_s"] == 900.0
    assert fact["missing_workflows"] == [_SECOND]
    assert stamp["p50_s"] is None
    assert stamp["p90_s"] is None

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    assert ("> 1 sampled commit(s) were partial (missing "
            "`.github/workflows/second.yml`); their observed shorter spans were "
            "excluded from these percentiles.") in report
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    check = vr.check_commit_wall_clock_projection(report, findings_path)
    assert check.ok, check


def test_pull_request_and_merge_queue_members_each_get_a_commit_stamp(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _PullRequestAndQueueClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=2, shallow_runs=2)
    facts = doc["pr_critical_path"]["commit_wall_clock"]["facts"]

    assert {fact["sha"] for fact in facts} == {_SHA, "queue-sha"}
    assert len(facts) == 2


def test_independent_matrix_leg_can_be_the_next_longest_floor(monkeypatch):
    doc = json.loads(json.dumps(_DOC))
    doc["workflow_job_graph"][_SECOND] = {
        "second-long": {"name": "second-long", "needs": [], "matrix": True},
    }
    monkeypatch.setattr(cr, "GhClient", _MatrixSiblingClient)
    collected = cr.collect(doc, "o/r", max_runs=1, shallow_runs=1)
    fact = collected["pr_critical_path"]["commit_wall_clock"]["facts"][0]

    assert fact["slowest_job"]["name"] == "second-long (one)"
    assert fact["next_longest_job"]["name"] == "second-long (two)"
    assert fact["next_longest_job"]["duration_s"] == 1000.0


def test_failed_all_status_run_without_jobs_makes_commit_partial(monkeypatch):
    doc = _collect(monkeypatch, _FailedWorkflowRunClient)
    fact = doc["pr_critical_path"]["commit_wall_clock"]["facts"][0]

    assert fact["partial"] is True
    assert fact["wall_s"] is None
    assert fact["missing_workflows"] == [_SECOND]


def test_zero_projected_wall_omits_multiple_and_still_renders_and_verifies(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch, _ZeroWallClient)
    fact = doc["pr_critical_path"]["commit_wall_clock"]["facts"][0]
    assert fact["wall_s"] == 0.0
    assert fact["projection_4x"]["projected_s"] == 0.0
    assert fact["projection_4x"]["multiple"] is None

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    assert "**Per-commit PR wait:** p50 **0m 00s**, p90 **0m 00s**" in report
    assert "(**infx**)" not in report
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, allow_nan=False), encoding="utf-8")
    check = vr.check_commit_wall_clock_projection(report, findings_path)
    assert check.ok, check


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


def test_commit_fact_carries_the_run_start_and_job_intervals_for_the_queue_split(
        monkeypatch):
    """The queue/work split needs the commit's first run creation and every job's
    queued/started/completed times; the producer stamps them on each fact."""
    doc = _collect(monkeypatch)
    fact = doc["pr_critical_path"]["commit_wall_clock"]["facts"][0]

    assert fact.get("created_at") == _iso(0)
    jobs = fact.get("jobs")
    assert isinstance(jobs, list)
    assert sorted((job["name"], job["started_at"], job["completed_at"])
                  for job in jobs) == [
        ("first-long", _iso(0), _iso(900)),
        ("first-short", _iso(0), _iso(300)),
        ("second-long", _iso(60), _iso(1500)),
    ]
    assert all(job["workflow_file"] in (_FIRST, _SECOND) for job in jobs)
