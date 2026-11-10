from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "shift_left_cases.json").read_text())
SPEC = importlib.util.spec_from_file_location("shift_left", ROOT / "scripts" / "shift_left.py")
assert SPEC and SPEC.loader
SHIFT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHIFT)


def record(duration, *, job="test", workflow="ci.yml", conclusion="success", pr=None, sha=None):
    return {
        "workflow": workflow,
        "job": job,
        "duration_s": duration,
        "status": "completed",
        "conclusion": conclusion,
        "pr_number": pr,
        "sha": sha or f"sha-{pr or duration}-{job}",
    }


def kinds(report):
    return [item["kind"] for item in report["observations"]]


def test_noisy_job_does_not_flag_a_slowdown():
    case = FIXTURES["noisy_l2"]
    history = [record(x) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    assert "slowdown" not in kinds(report)
    assert report["jobs"][0]["evidence"] == "threshold_not_exceeded"


def test_real_step_change_is_flagged_and_attributed_to_first_merge_where_it_holds():
    case = FIXTURES["step_l3"]
    history = [record(d, pr=p) for d, p in zip(case["durations"], case["prs"])]
    report = SHIFT.analyze_l3(history)
    step = next(item for item in report["observations"] if item["kind"] == "step_change")
    assert step["attribution"]["pr_number"] == 109
    assert step["conclusion"] == "warning"


def test_one_percent_per_merge_creep_is_flagged():
    case = FIXTURES["creep_l3"]
    history = [record(d, pr=p) for d, p in zip(case["durations"], case["prs"])]
    report = SHIFT.analyze_l3(history)
    creep = next(item for item in report["observations"] if item["kind"] == "creep")
    assert creep["change_percent"] > 10
    assert creep["attribution"]["pr_number"] >= 210


def test_renamed_job_is_reported_as_rename_not_slowdown():
    case = FIXTURES["rename_l2"]
    history = [record(x, job=case["previous_job"]) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"], job=case["current_job"])])
    assert kinds(report) == ["renamed_job"]
    assert report["observations"][0]["previous_job"] == case["previous_job"]


def test_new_job_is_not_called_a_slowdown():
    case = FIXTURES["new_job_l2"]
    history = [record(x, job=case["existing_job"]) for x in case["history"]]
    current = [record(case["existing_current"], job=case["existing_job"]),
               record(case["new_current"], job=case["new_job"])]
    report = SHIFT.analyze_l2(history, current)
    new = next(item for item in report["observations"] if item["job"] == case["new_job"])
    assert new["kind"] == "new_job"
    assert "slowdown" not in [x["kind"] for x in report["observations"]
                              if x["job"] == case["new_job"]]


def test_cancelled_and_failed_runs_are_excluded_from_baseline():
    case = FIXTURES["status_filter"]
    history = [record(x) for x in case["successful"]]
    history += [record(x, conclusion="failure") for x in case["failed"]]
    history += [record(x, conclusion="cancelled") for x in case["cancelled"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    job = report["jobs"][0]
    assert job["baseline_runs"] == 5
    assert "slowdown" in kinds(report)


def test_too_little_history_says_no_baseline_never_no_slowdown():
    case = FIXTURES["no_baseline_l2"]
    report = SHIFT.analyze_l2([record(x) for x in case["history"]],
                              [record(case["current"])])
    assert kinds(report) == ["no_baseline"]
    assert report["jobs"][0]["evidence"] == "no_baseline"
    assert "no slowdown" not in json.dumps(report).lower()


def test_performance_observations_never_make_the_check_red():
    case = FIXTURES["report_only_l2"]
    history = [record(x) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    assert "slowdown" in kinds(report)
    assert report["job_conclusion"] == "success"
