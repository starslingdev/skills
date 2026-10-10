"""Mutant pins for the step-timing rule (skipped steps, year-1 placeholder starts).

Each test here exists because a specific one-line change to `collect_runs.py`
survived the rest of the suite. The docstring of each test names the change it
kills, so deleting or weakening the guarded line turns this file red:

  - the long-pole stamp in `collect()` receiving the raw decomposition, so a pole
    whose every step was dropped still says why and how many steps it leaves out;
  - `_step_timeline` ignoring a placeholder job `started_at`;
  - OPT79's choice between the two "population truncated" withholds when log gates
    and step-time gates both set runs aside (the majority case and the exact tie);
  - OPT79 withholding a SKIPPED block step whose timestamps do not parse, instead of
    reading it as a 0s step.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR.parent / "scripts"))
sys.path.insert(0, str(_TESTS_DIR))
import collect_runs as cr  # noqa: E402
import test_offline_pipeline_e2e as e2e  # noqa: E402
import test_tier2_wave1_detectors as t2  # noqa: E402

_PLACEHOLDER = "0001-01-01T00:00:00Z"


# ---- 1. collect() stamps the pole from the RAW decomposition ----------------------

def _drop_pole_step_times(fixtures: Path) -> None:
    """In every recorded jobs listing: every `verify` step gets the year-1 placeholder
    start (one of them also reported skipped), and `prep`'s `Upload artifact` step gets
    the placeholder start beside its measured steps."""
    for path in fixtures.glob("repos_synthetic_repo_actions_runs_*_jobs_per_page_100.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for job in doc.get("jobs") or []:
            for step in job.get("steps") or []:
                if job.get("name") == "verify":
                    step["started_at"] = _PLACEHOLDER
                    if step.get("name") == "Download artifact":
                        step["conclusion"] = "skipped"
                elif job.get("name") == "prep" and step.get("name") == "Upload artifact":
                    step["started_at"] = _PLACEHOLDER
        path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture(scope="module")
def dropped_step_poles(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("dropped_step_poles")
    with pytest.MonkeyPatch.context() as mp:
        doc = e2e._collect_in_process(tmp_path, mp, edit_fixtures=_drop_pole_step_times)
    poles = (doc.get("pr_critical_path") or {}).get("poles") or []
    return {p.get("job"): p for p in poles}


def test_collect_stamps_a_pole_whose_every_step_was_dropped(dropped_step_poles):
    """Kills `_stamp_pole_decomposition(entry, _measured_decomposition(decomp))` in
    `collect()`: that hands the stamp None for an all-dropped job, so the pole loses
    its `no_step_measured_in_sample` reason and the counts of the steps it omits."""
    pole = dropped_step_poles.get("verify")
    assert pole is not None, sorted(dropped_step_poles)
    assert pole.get("step_decomposition_reason") == "no_step_measured_in_sample", pole
    assert "steps" not in pole and "dominant_step" not in pole, pole
    assert pole.get("skipped_steps") == 1, pole
    assert pole.get("unmeasured_steps") == 2, pole


def test_collect_counts_one_unmeasured_step_beside_measured_ones(dropped_step_poles):
    """A pole with measured steps keeps its step list and also says one declared step
    had no usable time."""
    pole = dropped_step_poles.get("prep")
    assert pole is not None, sorted(dropped_step_poles)
    assert pole.get("steps"), pole
    assert "step_decomposition_reason" not in pole, pole
    assert pole.get("unmeasured_steps") == 1, pole
    assert "Upload artifact" not in [s["step"] for s in pole["steps"]], pole


# ---- 2. _step_timeline: a placeholder job start gives 0s offsets -------------------

def test_step_timeline_reads_a_placeholder_job_start_as_no_start():
    """Kills deleting `if j0 is not None and j0.year <= _STEP_START_SENTINEL_MAX_YEAR:
    j0 = None` in `_step_timeline`: without it every offset is measured from year 1,
    ~63.9 billion seconds."""
    job = {"id": 1, "name": "build", "html_url": "https://github.com/o/r/actions/runs/1/job/1",
           "started_at": _PLACEHOLDER, "completed_at": "2026-06-01T00:02:00Z",
           "steps": [
               {"name": "Set up job", "number": 1, "conclusion": "success",
                "started_at": "2026-06-01T00:00:00Z", "completed_at": "2026-06-01T00:00:05Z"},
               {"name": "Build", "number": 2, "conclusion": "success",
                "started_at": "2026-06-01T00:00:05Z", "completed_at": "2026-06-01T00:01:35Z"},
           ]}
    tl = cr._step_timeline(job, "build", 120.0)
    assert [s["name"] for s in tl["steps"]] == ["Set up job", "Build"], tl
    assert [s["start_s"] for s in tl["steps"]] == [0.0, 0.0], tl
    assert [s["dur_s"] for s in tl["steps"]] == [5.0, 90.0], tl


# ---- 4. OPT79: which "population truncated" withhold wins -------------------------

def _opt79_mixed(step_time_runs: int, log_runs: int) -> dict:
    """Four hit and four miss runs. The first `step_time_runs` hit runs carry a
    placeholder restore start (a step-time gate); the next `log_runs` hit runs have no
    restore group in their log (a log gate). Returns the withheld tally."""
    jpr, logs = t2._opt79_sample(hits=4, misses=4)
    for run_jobs in jpr[:step_time_runs]:
        t2._opt79_sentinel_restore(run_jobs)
    for run_jobs in jpr[step_time_runs:step_time_runs + log_runs]:
        logs[run_jobs[0]["id"]] = t2._opt79_log(t2._OPT79_HIT_LINE,
                                                group="Run something/else@v1")
    w: dict = {}
    out = cr._detect_opt79_net_negative_cache(
        "ci.yml", jpr, t2._opt79_crit(), t2._opt79_wf(), 100, 0,
        logs_by_job_id=logs, withheld=w)
    assert out == [], w
    return w


def test_opt79_log_gates_in_the_majority_name_the_excluded_runs():
    """One step-time exclusion beside three log exclusions: the withhold names the
    excluded runs. Kills flipping `_step_time > excluded - _step_time` to `<`."""
    w = _opt79_mixed(step_time_runs=1, log_runs=3)
    assert w.get("step_has_no_in_window_time") == 1, w
    assert w.get("restore_step_log_group_not_found_in_the_run_log") == 3, w
    assert w.get("population_truncated_by_excluded_runs") == 1, w
    assert w.get("population_truncated_by_unmeasurable_step_times") is None, w


def test_opt79_a_tie_between_step_time_and_log_gates_names_the_excluded_runs():
    """Two step-time exclusions and two log exclusions: on an exact tie the step-time
    gates are NOT the larger share, so the current code names the excluded runs. Kills
    `>` -> `>=`."""
    w = _opt79_mixed(step_time_runs=2, log_runs=2)
    assert w.get("step_has_no_in_window_time") == 2, w
    assert w.get("restore_step_log_group_not_found_in_the_run_log") == 2, w
    assert w.get("population_truncated_by_excluded_runs") == 1, w
    assert w.get("population_truncated_by_unmeasurable_step_times") is None, w


# ---- 5. OPT79: a skipped block step whose timestamps do not parse ------------------

_BLOCK = {"restore": "Run actions/cache@v4", "install": "Run npm ci",
          "post": "Post Run actions/cache@v4"}


def _skipped_restore_job(started_at, completed_at) -> dict:
    job = t2._opt79_job(901, restore=28.0, install=3.0, post=2.0)
    for st in job["steps"]:
        if st["name"] == "Run actions/cache@v4":
            st["conclusion"] = "skipped"
            st["started_at"] = started_at
            st["completed_at"] = completed_at
    return job


@pytest.mark.parametrize("started_at, completed_at", [
    ("not-a-time", "not-a-time"),
    ("not-a-time", "2026-06-01T00:00:29Z"),
])
def test_opt79_skipped_block_step_with_unparseable_timestamps_is_withheld(
        started_at, completed_at):
    """A step GitHub reports skipped whose timestamps do not parse did not measure 0s.
    `_step_span_verdict` checks `skipped` before parsing, so only the explicit clause in
    `_opt79_block_durations` withholds it. Kills dropping
    `or (why == _SPAN_SKIPPED and _duration_s(...) is None)`, which reads it as 0s."""
    durs, present, gate = cr._opt79_block_durations(
        _skipped_restore_job(started_at, completed_at), _BLOCK)
    assert (durs, present) == ({}, {}), durs
    assert gate == "step_timestamps_unparseable_in_this_occurrence"


def test_opt79_skipped_block_step_with_parseable_timestamps_measures_zero():
    """The contrast: skipped with timestamps that parse genuinely ran nothing, 0s."""
    durs, present, gate = cr._opt79_block_durations(
        _skipped_restore_job("2026-06-01T00:00:01Z", "2026-06-01T00:00:29Z"), _BLOCK)
    assert gate == "", gate
    assert durs["restore"] == 0.0 and durs["install"] == 3.0, durs
