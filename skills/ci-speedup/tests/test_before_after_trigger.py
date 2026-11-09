from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from before_after import (GhVerificationClient, _baseline_from_findings,
                          advance_verification, compare_push_cohorts)


def _job(sha: str, attempt: int, end: int = 20):
    return {
        "name": "test", "workflow": "ci", "head_sha": sha,
        "run_attempt": attempt, "created_at": "2026-11-08T00:00:00Z",
        "started_at": "2026-11-08T00:00:02Z",
        "completed_at": f"2026-11-08T00:00:{end:02d}Z",
        "runner_label": "ubuntu-latest", "runner_class": "github-hosted",
        "steps": [{"name": "test", "started_at": "2026-11-08T00:00:02Z",
                   "completed_at": f"2026-11-08T00:00:{end:02d}Z"}],
        "tests": ["a", "b"],
    }


def _cohort(sha: str, identity: str, attempt: int = 1):
    return {
        "head_sha": sha, "cohort_id": identity, "attempt": attempt,
        "position": attempt, "conclusion": "success",
        "workflows": ["ci"], "expected_workflows": ["ci"],
        "jobs": [_job(sha, attempt)], "logs": [""],
        "lane": "pull requests", "lane_runs_30d": 42,
        "run_attempt_ids": [{"run_id": 10, "attempt": attempt}],
    }


class FakeClient:
    def __init__(self, head="fix", pushes=None):
        self.head = head
        self.initial = _cohort(head, f"{head}-initial") if head else None
        self.reruns = 0
        self.events = []
        self.pushes = pushes or []

    def remote_head(self, repo, branch):
        self.events.append("head")
        return self.head

    def completed_cohort(self, repo, head):
        self.events.append("initial")
        return copy.deepcopy(self.initial) if self.initial and head == self.head else None

    def rerun_cohort(self, repo, cohort, *, trigger=True):
        self.events.append("rerun-trigger" if trigger else "rerun-poll")
        if trigger:
            self.reruns += 1
        return _cohort(self.head, f"{self.head}-rerun-{self.reruns}",
                       attempt=self.reruns + 1)

    def default_branch_cohorts(self, repo, after_head, limit):
        self.events.append("pushes")
        return copy.deepcopy(self.pushes[:limit])


def _invoke(tmp_path, client, **kwargs):
    events = client.events
    baseline = tmp_path / "base.json"
    if not baseline.exists():
        pull_rows = [_cohort(f"base-{n}", f"base-{n}") for n in (1, 2)]
        push_rows = copy.deepcopy(pull_rows)
        for row in push_rows:
            row["lane"] = "push"
            row["cohort_id"] += "-push"
        baseline.write_text(json.dumps({
            "verification_baseline": pull_rows + push_rows}))
    branch = kwargs.pop("branch", "fix")
    return advance_verification(
        client, repo="o/r", branch=branch, baseline_artifact=str(baseline),
        context_path=tmp_path / "verify.json",
        changed_files=[".github/workflows/ci.yml"],
        emit=lambda line: events.append(("emit", line)),
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, **kwargs)


def test_default_branch_verification_never_requests_a_rerun(tmp_path):
    class DefaultBranchClient(FakeClient):
        def is_default_branch(self, repo, branch):
            return branch == "main"

    client = DefaultBranchClient(head="main")
    baseline = tmp_path / "base.json"
    rows = [_cohort(f"base-{n}", f"base-{n}") for n in (1, 2)]
    for row in rows:
        row["lane"] = "push"
    baseline.write_text(json.dumps({"verification_baseline": rows}))
    result = _invoke(tmp_path, client, branch="main")
    assert result["status"] == "waiting-for-default-branch-pushes"
    assert client.reruns == 0


def test_default_branch_verification_keeps_its_anchor_as_head_advances(tmp_path):
    class MovingDefaultClient(FakeClient):
        def __init__(self):
            super().__init__(head="main-1", pushes=[_cohort("main-2", "p1")])
            self.anchors = []

        def is_default_branch(self, repo, branch):
            return True

        def default_branch_cohorts(self, repo, after_head, limit):
            self.anchors.append(after_head)
            return super().default_branch_cohorts(repo, after_head, limit)

    client = MovingDefaultClient()
    baseline = tmp_path / "base.json"
    rows = [_cohort(f"base-{n}", f"base-{n}") for n in (1, 2)]
    for row in rows:
        row["lane"] = "push"
    baseline.write_text(json.dumps({"verification_baseline": rows}))
    first = _invoke(tmp_path, client, branch="main")
    client.head = "main-2"
    client.pushes.append(_cohort("main-3", "p2"))
    second = _invoke(tmp_path, client, branch="main")

    assert first["status"] == "waiting-for-default-branch-pushes"
    assert second["status"] == "complete"
    assert second["head_sha"] == "main-1"
    assert client.anchors == ["main-1", "main-1"]
    assert client.reruns == 0


def test_cache_sensitive_baseline_uses_push_lane_facts(tmp_path):
    pushes = [_cohort(f"push-{n}", f"p{n}") for n in range(1, 4)]
    for cohort in pushes:
        cohort["workflows"] = ["ci.yml:push"]
        cohort["expected_workflows"] = ["ci.yml:push"]
        cohort["jobs"][0]["workflow"] = "ci.yml:push"
        cohort["jobs"][0]["workload_identity"] = ["ci.yml:push/test/test"]
        cohort["lane"] = "push"
    client = FakeClient(pushes=pushes)
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({
        "per_workflow_monthly_volume": {"ci.yml": 40},
        "default_branch_push_lane": {"facts": [
            {
                "sha": f"base-{n}-{event}", "event": event, "jobs": [{
                    **_job(f"base-{n}-{event}", 1),
                    "workflow_file": "ci.yml",
                }],
            } for event in ("pull_request", "push") for n in (1, 2)
        ]},
    }))
    result = advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact=str(baseline),
        context_path=tmp_path / "verify.json",
        changed_files=[".github/workflows/ci.yml"], cache_sensitive=True,
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, emit=lambda _line: None)
    assert result["status"] == "complete"
    assert result["verdict"]["reason"] != "triggered workflow set changed"
    assert "distinct default-branch push cohorts" in result["verdict"]["report"]


def test_same_head_after_arm_is_labelled_no_change_reruns():
    before = [_cohort(f"base-{n}", f"base-{n}") for n in (1, 2)]
    after = [_cohort("fix", f"fix-{n}", attempt=n) for n in (1, 2)]
    result = compare_push_cohorts(
        before, after, changed_files=[".github/workflows/ci.yml"])
    assert "no-change reruns" in result["report"]
    assert result["verified_saving"] is not None


def test_unknown_default_branch_identity_never_requests_a_rerun(tmp_path):
    class UnknownDefaultClient(FakeClient):
        def is_default_branch(self, repo, branch):
            return None

    client = UnknownDefaultClient()
    result = _invoke(tmp_path, client)
    assert result["status"] == "default-branch-unknown"
    assert result["verdict"] is None
    assert client.reruns == 0


def test_no_rerun_before_remote_head_and_completed_first_cohort(tmp_path):
    no_head = FakeClient(head=None)
    result = _invoke(tmp_path, no_head)
    assert result["status"] == "ineligible"
    assert no_head.reruns == 0
    no_cohort = FakeClient()
    no_cohort.initial = None
    result = _invoke(tmp_path, no_cohort)
    assert result["status"] == "waiting-for-first-cohort"
    assert no_cohort.reruns == 0


def test_automatic_cohort_is_sample_one_and_cost_prints_before_rerun(tmp_path):
    client = FakeClient()
    result = _invoke(tmp_path, client)
    assert result["status"] == "complete"
    assert result["sample_count"] == 2
    assert client.reruns == 1
    assert client.events.index("rerun-trigger") > next(
        i for i, event in enumerate(client.events)
        if isinstance(event, tuple) and "runner-minutes" in event[1])


def test_repeat_invocation_reuses_verdict_without_duplicate_rerun(tmp_path):
    client = FakeClient()
    first = _invoke(tmp_path, client)
    second = _invoke(tmp_path, client)
    assert first["status"] == second["status"] == "complete"
    assert client.reruns == 1
    assert second["reused"] is True


def test_saved_verdict_reuses_without_refetching_the_initial_cohort(tmp_path):
    client = FakeClient()
    _invoke(tmp_path, client)
    client.initial = None
    result = _invoke(tmp_path, client)
    assert result["status"] == "complete"
    assert result["reused"] is True
    assert client.reruns == 1


def test_changed_sampling_binding_never_reuses_an_ordinary_verdict(tmp_path):
    client = FakeClient()
    _invoke(tmp_path, client)
    client.pushes = [_cohort(f"push-{n}", f"p{n}") for n in range(1, 4)]
    result = _invoke(tmp_path, client, cache_sensitive=True)
    assert result["reused"] is False
    assert result["sample_count"] == 3
    assert client.reruns == 1


class InterruptedClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def rerun_cohort(self, repo, cohort, *, trigger=True):
        self.events.append("rerun-trigger" if trigger else "rerun-poll")
        self.calls += 1
        if self.calls == 1:
            return None
        assert trigger is False
        return _cohort(self.head, "resumed-attempt", attempt=2)


def test_interrupted_rerun_resumes_polling_without_a_second_trigger(tmp_path):
    client = InterruptedClient()
    first = _invoke(tmp_path, client)
    second = _invoke(tmp_path, client)
    assert first["status"] == "waiting-for-rerun"
    assert second["status"] == "complete"
    assert client.events.count("rerun-trigger") == 1
    assert client.events.count("rerun-poll") == 1


def test_moved_head_discards_old_in_flight_samples(tmp_path):
    client = FakeClient()
    _invoke(tmp_path, client)
    client.head = "new-head"
    client.initial = _cohort("new-head", "new-initial")
    result = _invoke(tmp_path, client)
    saved = json.loads((tmp_path / "verify.json").read_text())
    assert result["head_sha"] == "new-head"
    assert {sample["head_sha"] for sample in saved["samples"]} == {"new-head"}
    assert result["discarded_moved_head"] is True


def test_zero_run_opt_out_makes_no_calls_and_renders_no_verdict(tmp_path):
    client = FakeClient()
    result = advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact="/tmp/base.json",
        context_path=tmp_path / "verify.json", changed_files=[],
        environ={"CI_SPEEDUP_VERIFY_RUNS": "0"},
        emit=lambda line: client.events.append(("emit", line)))
    assert result == {"status": "disabled", "verdict": None}
    assert client.events == []
    assert not (tmp_path / "verify.json").exists()


def test_cache_sensitive_change_waits_for_three_distinct_pushes_without_rerun(tmp_path):
    pushes = [_cohort(f"push-{n}", f"p{n}") for n in range(1, 4)]
    client = FakeClient(pushes=pushes[:2])
    result = _invoke(tmp_path, client, cache_sensitive=True)
    assert result["status"] == "waiting-for-default-branch-pushes"
    assert result["needed"] == 3
    assert client.reruns == 0
    client.pushes = pushes
    result = _invoke(tmp_path, client, cache_sensitive=True)
    assert result["status"] == "complete"
    assert result["sample_count"] == 3
    assert result["verdict"]["after"]["all_complete"]["n"] == 3
    assert client.reruns == 0


def test_cache_sampling_keeps_its_anchor_as_the_default_branch_advances(tmp_path):
    class MovingDefaultClient(FakeClient):
        def __init__(self):
            super().__init__(head="main-1", pushes=[
                _cohort("main-1", "p1"), _cohort("main-2", "p2")])
            self.anchors = []

        def is_default_branch(self, repo, branch):
            return True

        def default_branch_cohorts(self, repo, after_head, limit):
            self.anchors.append(after_head)
            return super().default_branch_cohorts(repo, after_head, limit)

    client = MovingDefaultClient()
    first = _invoke(tmp_path, client, cache_sensitive=True)
    client.head = "main-2"
    client.pushes.append(_cohort("main-3", "p3"))
    second = _invoke(tmp_path, client, cache_sensitive=True)
    assert first["status"] == "waiting-for-default-branch-pushes"
    assert second["status"] == "complete"
    assert second["head_sha"] == "main-1"
    assert client.anchors == ["main-1", "main-1"]


def test_cli_verification_preserves_the_original_baseline(tmp_path, monkeypatch):
    run_path = SCRIPTS / "run.py"
    spec = importlib.util.spec_from_file_location("ci_speedup_run_verify", run_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = tmp_path / "findings.json"
    original = json.dumps({"verification_baseline": [
        _cohort("base-1", "base-1"), _cohort("base-2", "base-2")]})
    baseline.write_text(original)
    observed = {}

    def fake_advance(client, **kwargs):
        observed.update(kwargs)
        return {"status": "complete", "verdict": {}}

    monkeypatch.setattr(module, "advance_verification", fake_advance)
    monkeypatch.setattr(module, "GhVerificationClient", object)
    assert module.main([
        "--root", str(tmp_path), "--out", str(baseline),
        "--repo", "o/r", "--verify-branch", "fix",
    ]) == 0
    assert baseline.read_text() == original
    assert observed["baseline_artifact"] == str(baseline)


class AdapterFixture(GhVerificationClient):
    def __init__(self, responses):
        super().__init__(poll_s=0, timeout_s=0.01, settle_s=0)
        self.responses = responses
        self.posts = []

    def _api(self, endpoint, *, method="GET"):
        if method != "GET":
            self.posts.append(endpoint)
            return {}
        for key in sorted(self.responses, key=len, reverse=True):
            if key in endpoint:
                return copy.deepcopy(self.responses[key])
        return None


def test_default_branch_lookup_preserves_an_unknown_api_result():
    client = AdapterFixture({})
    assert client.is_default_branch("o/r", "main") is None


def _api_run(run_id, workflow_id, event="pull_request", head="fix", attempt=1):
    return {
        "id": run_id, "workflow_id": workflow_id, "name": f"wf-{workflow_id}",
        "event": event, "head_sha": head, "run_attempt": attempt,
        "status": "completed", "conclusion": "success",
        "created_at": "2026-11-08T00:00:00Z",
    }


def _api_job(job_id=1):
    return {
        "id": job_id, "name": "test", "created_at": "2026-11-08T00:00:00Z",
        "started_at": "2026-11-08T00:00:02Z",
        "completed_at": "2026-11-08T00:00:20Z", "conclusion": "success",
        "labels": ["ubuntu-latest"], "steps": [{
            "name": "tests", "started_at": "2026-11-08T00:00:02Z",
            "completed_at": "2026-11-08T00:00:20Z"}],
    }


def test_real_adapter_keeps_all_events_and_uses_independent_check_run_set():
    first = _api_run(10, 1, "pull_request")
    chained = _api_run(20, 2, "workflow_run")
    client = AdapterFixture({
        "actions/runs?head_sha=fix": {"workflow_runs": [first, chained]},
        "check-runs": {"check_runs": [
            {"details_url": "https://github.com/o/r/actions/runs/10/job/1"},
            {"details_url": "https://github.com/o/r/actions/runs/20/job/2"},
        ]},
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(1)]},
        "runs/20/attempts/1/jobs": {"jobs": [_api_job(2)]},
        "event=pull_request": {"total_count": 40},
        "event=workflow_run": {"total_count": 5},
    })
    cohort = client.completed_cohort("o/r", "fix")
    assert cohort is not None
    assert set(cohort["workflows"]) == {"1:pull_request", "2:workflow_run"}
    assert cohort["lane_runs_30d"] == 45
    assert all(job["workload_identity"] for job in cohort["jobs"])


def test_real_adapter_paginates_large_job_matrices():
    first = [_api_job(job_id) for job_id in range(1, 101)]
    last = [_api_job(101)]
    client = AdapterFixture({
        "runs/10/attempts/1/jobs?per_page=100&page=1": {
            "total_count": 101, "jobs": first},
        "runs/10/attempts/1/jobs?per_page=100&page=2": {
            "total_count": 101, "jobs": last},
    })
    assert len(client._jobs("o/r", 10, 1) or []) == 101


def test_real_adapter_retains_healthy_skipped_jobs_as_workload_evidence():
    run = _api_run(10, 1)
    skipped = {"id": 2, "name": "optional", "conclusion": "skipped",
               "labels": ["ubuntu-latest"], "steps": []}
    client = AdapterFixture({
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(1), skipped]},
        "event=pull_request": {"total_count": 40},
    })
    cohort = client._cohort(
        "o/r", "fix", [run], identity="fix:initial",
        expected_workflows=["1:pull_request"])
    assert cohort is not None
    assert [job["name"] for job in cohort["jobs"]] == ["test"]
    assert [job["name"] for job in cohort["skipped_jobs"]] == ["optional"]


def test_collected_baseline_and_real_adapter_use_the_same_work_identity():
    job = {
        "name": "test", "workflow_file": ".github/workflows/ci.yml",
        "created_at": "2026-11-08T00:00:00Z",
        "started_at": "2026-11-08T00:00:02Z",
        "completed_at": "2026-11-08T00:00:30Z",
        "runner_label": "ubuntu-latest", "runner_class": "github-hosted",
        "steps": [{"name": "tests", "started_at": "2026-11-08T00:00:02Z",
                   "completed_at": "2026-11-08T00:00:30Z"}],
    }
    doc = {
        "per_workflow_monthly_volume": {".github/workflows/ci.yml": 40},
        "pr_critical_path": {"commit_wall_clock": {"facts": [
            {"sha": "base-1", "event": "pull_request", "jobs": [job]},
            {"sha": "base-2", "event": "pull_request", "jobs": [job]},
        ]}},
    }
    before = _baseline_from_findings(doc)
    after_job = copy.deepcopy(job)
    after_job.update({
        "workflow": ".github/workflows/ci.yml:pull_request",
        "head_sha": "fix", "run_attempt": 1,
        "workload_identity": [
            ".github/workflows/ci.yml:pull_request/test/tests"],
    })
    after = [{
        "head_sha": "fix", "cohort_id": f"after-{n}", "attempt": 1,
        "position": n, "conclusion": "success",
        "workflows": [".github/workflows/ci.yml:pull_request"],
        "expected_workflows": [".github/workflows/ci.yml:pull_request"],
        "jobs": [after_job], "logs": [], "lane": "pull_request",
        "lane_runs_30d": 40,
    } for n in (1, 2)]
    result = compare_push_cohorts(
        before, after, changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is True
    assert result["workload_state"] == "same"


def test_default_branch_sampling_maps_squash_merge_and_stops_at_a_gap():
    commits = [{"sha": "newer"}, {"sha": "merge"}, {"sha": "older"}]
    merge_run = _api_run(30, 3, "push", "merge")
    newer_run = _api_run(40, 3, "push", "newer")
    client = AdapterFixture({
        "repos/o/r/commits?": commits,
        "commits/fix/pulls": [{
            "merged_at": "2026-11-08T01:00:00Z", "merge_commit_sha": "merge",
            "base": {"ref": "main"},
        }],
        "head_sha=merge": {"workflow_runs": [merge_run]},
        "head_sha=newer": {"workflow_runs": [newer_run]},
        "runs/30/attempts/1/jobs": {"jobs": [_api_job(3)]},
        "runs/40/attempts/1/jobs": {"jobs": []},
        "event=push": {"total_count": 30},
        "repos/o/r": {"default_branch": "main"},
    })
    cohorts = client.default_branch_cohorts("o/r", "fix", 3)
    assert [cohort["head_sha"] for cohort in cohorts] == ["merge"]
