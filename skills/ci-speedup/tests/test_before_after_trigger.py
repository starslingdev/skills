from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from before_after import (GhVerificationClient, _baseline_from_findings, _cache_class,
                          advance_verification, compare_push_cohorts)


def test_verification_log_fetch_allows_and_strips_terminal_escapes(monkeypatch):
    def fake_run(command, **_kwargs):
        assert "--allow-escape-sequences" in command
        return SimpleNamespace(
            returncode=0, stdout="\x1b[31mCache restored from key: node-v1\x1b[0m",
            stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GhVerificationClient._text("repos/o/r/actions/jobs/1/logs") == (
        "Cache restored from key: node-v1")


def test_verification_log_fetch_records_timeout_as_unreadable(monkeypatch):
    def fake_run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 90)

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = GhVerificationClient(poll_s=0, timeout_s=0.01, settle_s=0)
    sample = client._job_logs("o/r", [{"id": 1}])

    assert sample["log_api_calls"] == 1
    assert sample["log_jobs_unreadable"] == 1


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

    def completed_cohort(self, repo, head, branch=None):
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
        self.text_gets = []

    def _api(self, endpoint, *, method="GET"):
        if method != "GET":
            self.posts.append(endpoint)
            return {}
        for key in sorted(self.responses, key=len, reverse=True):
            if key in endpoint:
                return copy.deepcopy(self.responses[key])
        return None

    def _text(self, endpoint):
        self.text_gets.append(endpoint)
        for key in sorted(self.responses, key=len, reverse=True):
            if key in endpoint and isinstance(self.responses[key], str):
                return self.responses[key]
        return None


def test_default_branch_lookup_preserves_an_unknown_api_result():
    client = AdapterFixture({})
    assert client.is_default_branch("o/r", "main") is None


def _api_run(run_id, workflow_id, event="pull_request", head="fix", attempt=1):
    return {
        "id": run_id, "workflow_id": workflow_id, "name": f"wf-{workflow_id}",
        "event": event, "head_sha": head, "run_attempt": attempt,
        "head_branch": "fix" if event != "push" else "main",
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
    cohort = client.completed_cohort("o/r", "fix", "fix")
    assert cohort is not None
    assert set(cohort["workflows"]) == {"1:pull_request", "2:workflow_run"}
    assert cohort["lane_runs_30d"] == 45
    assert all(job["workload_identity"] for job in cohort["jobs"])


def test_same_sha_on_topic_and_default_branch_reruns_only_the_topic_run():
    topic = _api_run(10, 1, "pull_request", "shared")
    topic["head_branch"] = "fix"
    default = _api_run(30, 3, "push", "shared")
    client = AdapterFixture({
        "actions/runs?head_sha=shared": {"workflow_runs": [topic, default]},
        "check-runs": {"check_runs": [
            {"details_url": "https://github.com/o/r/actions/runs/10/job/1"},
            {"details_url": "https://github.com/o/r/actions/runs/30/job/3"},
        ]},
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(1)]},
        "actions/runs/10": topic,
        "event=pull_request": {"total_count": 40},
        "repos/o/r": {"default_branch": "main"},
    })

    cohort = client.completed_cohort("o/r", "shared", "fix")
    assert cohort is not None
    assert cohort["run_attempt_ids"] == [{"run_id": 10, "attempt": 1}]

    assert client.rerun_cohort("o/r", cohort) is None
    assert client.posts == ["repos/o/r/actions/runs/10/rerun"]


def test_rerun_preflights_every_member_before_posting_any_rerun():
    first = _api_run(10, 1)
    first["head_branch"] = "fix"
    mismatched = _api_run(20, 2)
    mismatched["head_branch"] = "other"
    client = AdapterFixture({
        "actions/runs/10": first,
        "actions/runs/20": mismatched,
        "repos/o/r": {"default_branch": "main"},
    })
    cohort = {
        "head_sha": "fix",
        "head_branch": "fix",
        "run_attempt_ids": [
            {"run_id": 10, "attempt": 1},
            {"run_id": 20, "attempt": 1},
        ],
    }

    assert client.rerun_cohort("o/r", cohort) == {
        "_rerun_status": "preflight-refused"}
    assert client.posts == []


def test_topic_cohort_waits_for_a_topic_branch_check_run():
    topic = _api_run(10, 1, "pull_request", "shared")
    topic["head_branch"] = "fix"
    default = _api_run(30, 3, "push", "shared")
    client = AdapterFixture({
        "actions/runs?head_sha=shared": {"workflow_runs": [topic, default]},
        "check-runs": {"check_runs": [
            {"details_url": "https://github.com/o/r/actions/runs/30/job/3"},
        ]},
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(1)]},
        "event=pull_request": {"total_count": 40},
    })

    assert client.completed_cohort("o/r", "shared", "fix") is None


def test_topic_cohort_rejects_a_workflow_already_on_a_later_attempt():
    first = _api_run(10, 1, "pull_request", "shared")
    first["head_branch"] = "fix"
    rerun = _api_run(20, 2, "workflow_run", "shared", attempt=2)
    rerun["head_branch"] = "fix"
    client = AdapterFixture({
        "actions/runs?head_sha=shared": {"workflow_runs": [first, rerun]},
        "check-runs": {"check_runs": [
            {"details_url": "https://github.com/o/r/actions/runs/10/job/1"},
            {"details_url": "https://github.com/o/r/actions/runs/20/job/2"},
        ]},
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(1)]},
        "event=pull_request": {"total_count": 40},
    })

    assert client.completed_cohort("o/r", "shared", "fix") is None


def test_preflight_refusal_clears_pending_intent_for_a_later_retry(tmp_path):
    class RefusedClient(FakeClient):
        def rerun_cohort(self, repo, cohort, *, trigger=True):
            self.events.append("rerun-trigger" if trigger else "rerun-poll")
            return {"_rerun_status": "preflight-refused"}

    client = RefusedClient()
    first = _invoke(tmp_path, client)
    second = _invoke(tmp_path, client)
    saved = json.loads((tmp_path / "verify.json").read_text())

    assert first["status"] == second["status"] == "rerun-preflight-refused"
    assert client.events.count("rerun-trigger") == 2
    assert saved["pending_rerun"] is None


def test_missing_push_baseline_names_the_lane_and_status(tmp_path):
    client = FakeClient(pushes=[])
    baseline = tmp_path / "base.json"
    baseline.write_text("{}")
    emitted = []

    result = advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact=str(baseline),
        context_path=tmp_path / "verify.json",
        changed_files=[".github/workflows/ci.yml"], cache_sensitive=True,
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, emit=emitted.append)

    assert result["status"] == "baseline-unavailable"
    assert result["baseline_lane"] == "push"
    assert emitted == ["Verification baseline for the push lane is unavailable."]


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


def test_real_adapter_reads_the_sampled_jobs_log():
    run = _api_run(10, 1)
    client = AdapterFixture({
        "runs/10/attempts/1/jobs": {"jobs": [_api_job(7)]},
        "event=pull_request": {"total_count": 40},
        "actions/jobs/7/logs": "Cache not found for input keys: node-v1",
    })

    cohort = client._cohort(
        "o/r", "fix", [run], identity="fix:initial",
        expected_workflows=["1:pull_request"])

    assert cohort is not None
    assert cohort["logs"] == ["Cache not found for input keys: node-v1"]
    assert client.text_gets == ["repos/o/r/actions/jobs/7/logs"]


def test_real_adapter_caps_and_counts_job_log_calls():
    run = _api_run(10, 1)
    client = AdapterFixture({
        "runs/10/attempts/1/jobs": {
            "jobs": [_api_job(job_id) for job_id in range(1, 23)]},
        "event=pull_request": {"total_count": 40},
        "actions/jobs/": "log",
    })

    cohort = client._cohort(
        "o/r", "fix", [run], identity="fix:initial",
        expected_workflows=["1:pull_request"])

    assert cohort is not None
    assert len(client.text_gets) == 20
    assert cohort["log_api_calls"] == 20
    assert cohort["log_jobs_omitted"] == 2
    assert _cache_class({**cohort, "position": 2}) == ("warm", "later run")


def test_real_adapter_bounds_total_retained_log_text():
    client = AdapterFixture({"actions/jobs/": "cache hit " + "x" * 60_000})
    sample = client._job_logs("o/r", [_api_job(job_id) for job_id in range(1, 4)])

    assert sum(len(log) for log in sample["logs"]) == 100_000
    assert sample["log_api_calls"] == 2
    assert sample["log_jobs_omitted"] == 1
    assert sample["log_jobs_truncated"] == 1


def test_real_adapter_finds_cache_markers_after_a_long_log_prefix():
    log = "ordinary output\n" * 11_000 + "Cache restored from key: node-v1\n"
    client = AdapterFixture({"actions/jobs/": log})

    sample = client._job_logs("o/r", [_api_job()])

    assert sample["logs"] == ["Cache restored from key: node-v1"]
    assert sample["log_jobs_truncated"] == 0
    assert _cache_class({"position": 1, **sample})[0] == "warm"


def test_renamed_cache_actions_without_a_marker_stay_unknown():
    for action in ("actions/cache@v4", "actions/cache/restore@v4",
                   "actions/cache/save@v4"):
        client = AdapterFixture({
            "actions/jobs/": (
                f"##[group]Run {action}\n"
                "with:\n"
                "  path: node_modules\n"),
        })
        job = _api_job()
        job["steps"][0]["name"] = "Restore dependencies"

        sample = client._job_logs("o/r", [job])

        assert sample["log_has_cache_step"] is True, action
        assert _cache_class({"position": 2, **sample}) == (
            "unknown", "no cache marker in sampled job logs"), action

    client = AdapterFixture({
        "actions/jobs/": (
            "2026-11-11T08:14:03.1234567Z ##[group]Run actions/setup-node@v4\n"
            "2026-11-11T08:14:03.1235678Z with:\n"
            "2026-11-11T08:14:03.1236789Z   cache: npm\n"
            "2026-11-11T08:14:04.2345678Z npm cache is not found\n"),
    })
    job = _api_job()
    job["steps"][0]["name"] = "Set up Node.js"

    sample = client._job_logs("o/r", [job])

    assert sample["log_has_cache_step"] is True
    assert _cache_class({"position": 2, **sample}) == (
        "unknown", "no cache marker in sampled job logs")


def test_unreadable_renamed_step_cannot_prove_cache_absence():
    client = AdapterFixture({})
    job = _api_job()
    job["steps"][0]["name"] = "Restore dependencies"

    sample = client._job_logs("o/r", [job])

    assert _cache_class({"position": 2, **sample}) == (
        "unknown", "1 job log unreadable")


def test_real_adapter_discloses_jobs_without_readable_ids():
    sample = AdapterFixture({})._job_logs("o/r", [{"id": None}])

    assert sample["log_api_calls"] == 0
    assert sample["log_jobs_unreadable"] == 1


def test_actions_cache_hit_and_miss_lines_drive_cache_classification():
    assert _cache_class({
        "position": 2,
        "logs": ["Cache not found for input keys: node-v1"],
    })[0] == "cold"
    assert _cache_class({
        "position": 1,
        "logs": ["Cache restored from key: node-v1"],
    })[0] == "warm"
    assert _cache_class({
        "position": 2,
        "logs": [],
        "log_api_calls": 1,
        "log_jobs_unreadable": 1,
    }) == ("unknown", "1 job log unreadable")
    assert _cache_class({
        "position": 2,
        "logs": ["Cache restored from key: node-v1"],
        "log_api_calls": 1,
        "log_jobs_truncated": 1,
    }) == ("unknown", "1 job log truncated by cap")


def test_collected_baseline_and_real_adapter_use_the_same_work_identity():
    job = {
        "id": 77, "name": "test", "workflow_file": ".github/workflows/ci.yml",
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
    client = AdapterFixture({
        "actions/jobs/77/logs": "Cache restored from key: node-v1",
    })
    before = _baseline_from_findings(doc, client=client, repo="o/r")
    assert before[0]["logs"] == ["Cache restored from key: node-v1"]
    assert before[0]["log_api_calls"] == 1
    assert client.text_gets == ["repos/o/r/actions/jobs/77/logs"] * 2
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
        "jobs": [after_job],
        "logs": ["Cache restored from key: node-v1"], "lane": "pull_request",
        "lane_runs_30d": 40,
    } for n in (1, 2)]
    result = compare_push_cohorts(
        before, after, changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is True
    assert result["workload_state"] == "same"


def test_verification_has_one_persisted_baseline_log_budget(tmp_path):
    class BudgetClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.log_gets = []

        def _text(self, endpoint):
            self.log_gets.append(endpoint)
            return "Cache restored from key: node-v1"

        _job_logs = GhVerificationClient._job_logs

        def rerun_cohort(self, repo, cohort, *, trigger=True):
            self.events.append("rerun-trigger" if trigger else "rerun-poll")
            return None

    client = BudgetClient()
    client.initial = None
    facts = []
    job_id = 1
    for fact_number in range(6):
        jobs = []
        fact_size = 19 if fact_number == 5 else 20
        for _ in range(fact_size):
            jobs.append({
                **_job(f"base-{fact_number}", 1),
                "id": job_id,
                "workflow_file": "ci.yml",
            })
            job_id += 1
        facts.append({
            "sha": f"base-{fact_number}",
            "event": "pull_request",
            "jobs": jobs,
        })
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({
        "per_workflow_monthly_volume": {"ci.yml": 40},
        "pr_critical_path": {"commit_wall_clock": {"facts": facts}},
    }))
    context_path = tmp_path / "verify.json"

    first = advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact=str(baseline),
        context_path=context_path, changed_files=[".github/workflows/ci.yml"],
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, emit=lambda _line: None)

    assert first["status"] == "waiting-for-first-cohort"
    assert len(client.log_gets) <= 20
    assert first["verification_log_api_calls"] == len(client.log_gets)
    assert first["verification_log_api_budget"] == 20
    saved = json.loads(context_path.read_text())
    assert saved["baseline"]
    after_baseline = client._job_logs(
        "o/r", [{
            "id": 1000 + index,
            "steps": [{"name": "Restore dependencies"}],
        } for index in range(5)])
    assert after_baseline["log_api_calls"] == 0
    assert after_baseline["log_jobs_omitted"] == 5
    client.log_gets.clear()
    client.initial = _cohort("fix", "fix-initial")
    second = advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact=str(baseline),
        context_path=context_path, changed_files=[".github/workflows/ci.yml"],
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, emit=lambda _line: None)
    assert second["status"] == "waiting-for-rerun"
    assert second["sample_count"] == 1
    assert client.log_gets == []
    assert second["verification_log_api_calls"] == first["verification_log_api_calls"]
    assert _cache_class({
        "position": 2,
        **after_baseline,
    }) == ("unknown", "5 job logs omitted by cap")


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
