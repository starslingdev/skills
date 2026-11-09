from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from before_after import advance_verification


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

    def rerun_cohort(self, repo, cohort):
        self.events.append("rerun")
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
        baseline.write_text(json.dumps({
            "verification_baseline": [
                _cohort("base-1", "base-1"),
                _cohort("base-2", "base-2"),
            ]}))
    return advance_verification(
        client, repo="o/r", branch="fix", baseline_artifact=str(baseline),
        context_path=tmp_path / "verify.json",
        changed_files=[".github/workflows/ci.yml"],
        emit=lambda line: events.append(("emit", line)),
        environ={"CI_SPEEDUP_VERIFY_RUNS": "2"}, **kwargs)


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
    assert client.events.index("rerun") > next(
        i for i, event in enumerate(client.events)
        if isinstance(event, tuple) and "runner-minutes" in event[1])


def test_repeat_invocation_reuses_verdict_without_duplicate_rerun(tmp_path):
    client = FakeClient()
    first = _invoke(tmp_path, client)
    second = _invoke(tmp_path, client)
    assert first["status"] == second["status"] == "complete"
    assert client.reruns == 1
    assert second["reused"] is True


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
    assert client.reruns == 0
