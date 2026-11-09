from __future__ import annotations

import copy
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from before_after import compare_push_cohorts


def _job(name: str, start: int, end: int, **extra):
    started = f"2026-11-08T00:00:{start + 2:02d}Z"
    completed = f"2026-11-08T00:00:{end:02d}Z"
    return {"name": name, "workflow": "ci",
            "created_at": f"2026-11-08T00:00:{start:02d}Z",
            "started_at": started, "completed_at": completed,
            "steps": [{"name": name, "started_at": started,
                       "completed_at": completed}],
            "runner_label": "ubuntu-latest", "runner_class": "github-hosted", **extra}


def _cohort(sha: str, identity: str, wall: int, *, position: int = 1,
            log: str = "", conclusion: str = "success", attempt: int = 1,
            tests=("a", "b"), workflows=("ci",), jobs=None, pair=None):
    if jobs is None:
        job_rows = [_job("test" if workflow == "ci" else f"test-{workflow}",
                         0, wall, tests=list(tests), workflow=workflow)
                    for workflow in workflows]
    else:
        job_rows = copy.deepcopy(jobs)
    for job in job_rows:
        job["head_sha"] = sha
        job["run_attempt"] = attempt
    return {"head_sha": sha, "cohort_id": identity, "attempt": attempt,
            "position": position, "conclusion": conclusion,
            "workflows": list(workflows), "expected_workflows": list(workflows),
            "jobs": job_rows,
            "logs": [log], "pair_id": pair, "lane": "pull requests",
            "lane_runs_30d": 42}


def test_complete_exact_head_attempts_produce_warm_median_and_range():
    before = [_cohort("base-a", "b1", 50), _cohort("base-b", "b2", 40, position=2)]
    after = [_cohort("fix", "a1", 26, log="cache key miss"),
             _cohort("fix", "a2", 20, position=2, log="cache hit"),
             _cohort("fix", "a3", 24, position=3, log="cache hit")]
    result = compare_push_cohorts(before, after, changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is True
    assert result["after"]["warm"] == {"n": 2, "median_s": 22.0, "range_s": [20.0, 24.0]}
    assert result["after"]["cold"]["values_s"] == [26.0]
    assert result["after"]["all_complete"]["values_s"] == [26.0, 20.0, 24.0]
    assert result["workload_state"] == "same"
    assert result["clean_attribution"] is True


def test_incomplete_failed_and_mixed_head_cohorts_never_become_samples():
    bad = _cohort("fix", "missing", 20)
    bad["jobs"][0]["completed_at"] = None
    result = compare_push_cohorts([_cohort("base", "b", 40)],
        [bad, _cohort("fix", "failed", 21, conclusion="failure"),
         _cohort("other", "drift", 18)], changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is False
    assert result["failed_count"] == 1
    assert set(result["excluded"]) == {"missing", "failed", "drift"}


def test_adaptive_sampling_uses_thirty_percent_spread_and_caps_at_four():
    result = compare_push_cohorts([_cohort("base", "b", 40)],
        [_cohort("fix", "a1", 20), _cohort("fix", "a2", 30, position=2)],
        changed_files=[".github/workflows/ci.yml"])
    assert result["sampling"] == {"need": 3, "cap": 4,
                                  "reason": "samples differ by 40.0%"}


def test_workload_direction_is_evidence_based_and_resharding_is_changed():
    base = [_cohort("base", "b", 40)]
    equal_unknown = [_cohort("fix", "a", 20, tests=())]
    assert compare_push_cohorts(base, equal_unknown,
        changed_files=[".github/workflows/ci.yml"])["workload_state"] == "unknown"
    reshard = [_cohort("fix", "a", 20, jobs=[
        _job("test 1/2", 0, 20, suite="unit", shard="1/2"),
        _job("test 2/2", 0, 18, suite="unit", shard="2/2")])]
    reshaped = compare_push_cohorts(base, reshard, changed_files=[".github/workflows/ci.yml"])
    assert reshaped["workload_state"] == "changed"
    assert reshaped["clean_attribution"] is False
    assert reshaped["lower_bound"] is False


def test_removed_test_and_product_change_withhold_verified_saving():
    before = [_cohort("base", "b", 40, tests=("a", "b", "removed"))]
    after = [_cohort("fix", "a", 20, tests=("a", "b"))]
    reduced = compare_push_cohorts(before, after, changed_files=[".github/workflows/ci.yml"])
    assert reduced["workload_state"] == "reduced"
    assert reduced["verified_saving"] is None
    product = compare_push_cohorts(before, after, changed_files=["src/app.py"])
    assert product["ci_only"] is False
    assert product["clean_attribution"] is False


def test_runner_or_triggered_set_change_makes_merge_wait_unavailable():
    before = [_cohort("base", "b", 40)]
    runner_jobs = [_job("test", 0, 20, tests=["a", "b"], runner_label="large")]
    runner = compare_push_cohorts(before, [_cohort("fix", "a", 20, jobs=runner_jobs)],
                                 changed_files=[".github/workflows/ci.yml"])
    assert runner["available"] is False
    assert "runner" in runner["reason"]
    gate = compare_push_cohorts(before,
        [_cohort("fix", "a", 20, workflows=("ci", "new-gate"))],
        changed_files=[".github/workflows/ci.yml"])
    assert gate["available"] is False
    assert "triggered" in gate["reason"]


def test_cache_regression_and_same_sha_rerun_are_disclosed():
    before = [_cohort("same", "b", 40, log="144 packages (cached)")]
    after = [_cohort("same", "a", 20, log="0 packages (cached)", attempt=2)]
    result = compare_push_cohorts(before, after, changed_files=[".github/workflows/ci.yml"])
    assert result["workload_state"] == "changed"
    assert result["no_change_rerun"] is True
    assert result["cache"]["regressed"] is True


def test_report_carries_queue_work_compute_step_and_six_disclosures():
    before = [_cohort("base", "b", 40)]
    after = [_cohort("fix", "a", 20), _cohort("fix", "a2", 20, position=2)]
    result = compare_push_cohorts(before, after, changed_files=[".github/workflows/ci.yml"])
    report = result["report"]
    assert result["queue_delta_s"] is not None
    assert result["work_delta_s"] is not None
    assert result["total_job_duration"] == {"before_s": 38.0, "after_s": 18.0}
    assert "Changed step" in report
    for label in ("Sample size", "Asymmetry", "CI-only scope", "Variance",
                  "Cache state", "Environment"):
        assert label in report


def test_missing_head_or_completeness_evidence_never_credits_a_saving():
    no_head = _cohort("", "a", 20)
    result = compare_push_cohorts(
        [_cohort("base", "b", 40), _cohort("base2", "b2", 40)],
        [no_head, _cohort("fix", "a2", 20, position=2)],
        changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is False
    partial = _cohort("fix", "partial", 20, workflows=("ci", "lint"))
    partial["expected_workflows"] = ["ci", "lint", "deploy"]
    result = compare_push_cohorts(
        [_cohort("base", "b", 40), _cohort("base2", "b2", 40)],
        [partial], changed_files=[".github/workflows/ci.yml"])
    assert result["available"] is False


def test_empty_diff_evidence_and_runner_job_swaps_withhold_attribution():
    before = [_cohort("base", "b", 40), _cohort("base2", "b2", 40)]
    after = [_cohort("fix", "a", 20), _cohort("fix", "a2", 20, position=2)]
    missing_diff = compare_push_cohorts(before, after, changed_files=[])
    assert missing_diff["ci_only"] is False
    assert missing_diff["clean_attribution"] is False
    before_jobs = [_job("test", 0, 40, tests=["a", "b"], runner_label="ubuntu"),
                   _job("lint", 0, 10, tests=["lint"], runner_label="windows")]
    after_jobs = [_job("test", 0, 20, tests=["a", "b"], runner_label="windows"),
                  _job("lint", 0, 10, tests=["lint"], runner_label="ubuntu")]
    swapped = compare_push_cohorts(
        [_cohort("base", "b", 40, jobs=before_jobs),
         _cohort("base2", "b2", 40, jobs=before_jobs)],
        [_cohort("fix", "a", 20, jobs=after_jobs),
         _cohort("fix", "a2", 20, position=2, jobs=after_jobs)],
        changed_files=[".github/workflows/ci.yml"])
    assert swapped["available"] is False
    assert swapped["clean_attribution"] is False


def test_each_sample_must_carry_the_same_named_workload():
    before = [_cohort("base", "b", 40), _cohort("base2", "b2", 40)]
    after = [_cohort("fix", "a", 20, tests=("a",)),
             _cohort("fix", "a2", 20, position=2, tests=("b",))]
    result = compare_push_cohorts(before, after,
                                  changed_files=[".github/workflows/ci.yml"])
    assert result["workload_state"] == "changed"
    assert result["clean_attribution"] is False


def test_cache_evidence_is_compared_per_sample_not_as_arm_totals():
    before = [_cohort("base", "b", 40, log="cache hit"),
              _cohort("base2", "b2", 40, position=2, log="cache hit")]
    after = [_cohort("fix", "a", 20, log="cache key miss"),
             _cohort("fix", "a2", 20, position=2, log="cache key miss")]
    result = compare_push_cohorts(before, after,
                                  changed_files=[".github/workflows/ci.yml"])
    assert result["cache"]["regressed"] is True
    assert result["workload_state"] == "changed"
    assert result["verified_saving"] is None


def test_compute_disclosure_compares_median_cohorts_and_step_names():
    before_jobs = [_job("lint", 0, 10, tests=["lint"]),
                   _job("test", 0, 40, tests=["a", "b"], changed_step=True)]
    after_jobs = [_job("test", 0, 20, tests=["a", "b"], changed_step=True),
                  _job("lint", 0, 10, tests=["lint"])]
    result = compare_push_cohorts(
        [_cohort("base", "b", 40, jobs=before_jobs),
         _cohort("base2", "b2", 40, jobs=before_jobs)],
        [_cohort("fix", "a", 20, jobs=after_jobs),
         _cohort("fix", "a2", 20, position=2, jobs=after_jobs)],
        changed_files=[".github/workflows/ci.yml"])
    assert result["total_job_duration"] == {"before_s": 46.0, "after_s": 26.0}
    assert "Changed step: test" in result["report"]
    assert "38.0s before" in result["report"]
    assert "18.0s after" in result["report"]
