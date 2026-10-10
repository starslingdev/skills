"""OPT79 block durations under the shared step-timing rule (`_step_span`).

A cache block step GitHub did NOT report as skipped, whose timestamps parse
but carry no in-window span (a `started_at` in 1970 or earlier, the year-1
placeholder included, a start more than 1s before its own job, or a start
after its job ended or in its last second with no time left once its end is
cut back), did not
measure. It must withhold the occurrence as `step_has_no_in_window_time`,
never read as its raw duration (about 63.9 billion seconds from year 1) nor as
0s: a 0s post save on a miss run removes the miss-side save cost and inflates
the excess OPT79 reports. Reversed timestamps (end before start, both parse)
are a broken duration and withhold as
`step_timestamps_unparseable_in_this_occurrence`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import collect_runs as cr  # noqa: E402

_BLOCK = {"restore": "Run actions/cache@v4", "install": "Run npm ci",
          "post": "Post Run actions/cache@v4", "cache_ref": "actions/cache@v4"}


def _job(post: dict) -> dict:
    return {
        "started_at": "2026-06-01T00:00:00Z",
        "completed_at": "2026-06-01T00:02:00Z",
        "steps": [
            {"name": "Run actions/cache@v4", "conclusion": "success",
             "started_at": "2026-06-01T00:00:01Z",
             "completed_at": "2026-06-01T00:00:11Z"},
            {"name": "Run npm ci", "conclusion": "success",
             "started_at": "2026-06-01T00:00:11Z",
             "completed_at": "2026-06-01T00:01:11Z"},
            dict({"name": "Post Run actions/cache@v4"}, **post),
        ],
    }


def test_sentinel_started_post_step_withholds_instead_of_measuring_0s():
    job = _job({"conclusion": "success", "started_at": "0001-01-01T00:00:00Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    durs, present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_has_no_in_window_time", (durs, present, gate)
    assert durs == {} and present == {}


def test_post_step_started_before_its_job_withholds():
    job = _job({"conclusion": "success", "started_at": "2026-05-31T23:59:00Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    _durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_has_no_in_window_time", gate


def test_sentinel_started_install_step_is_named_as_out_of_window():
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:01:11Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    job["steps"][1]["started_at"] = "0001-01-01T00:00:00Z"
    _durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_has_no_in_window_time", gate


def test_genuinely_skipped_post_step_still_measures_0s():
    job = _job({"conclusion": "skipped", "started_at": "0001-01-01T00:00:00Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    durs, present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "", gate
    assert durs == {"restore": 10.0, "install": 60.0, "post": 0.0}, durs
    assert present["post"] is True


def test_measured_post_step_is_unchanged():
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:01:11Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == ""
    assert durs == {"restore": 10.0, "install": 60.0, "post": 39.0}, durs


def test_sentinel_started_restore_step_withholds():
    """The restore slot is held to the same rule as install and post."""
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:01:11Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    job["steps"][0]["started_at"] = "0001-01-01T00:00:00Z"
    durs, present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_has_no_in_window_time", (durs, present, gate)
    assert durs == {} and present == {}


def test_reversed_cache_step_timestamps_withhold_as_unparseable():
    """End before start (both parse) is a broken duration, not a step with no
    in-window time."""
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:01:50Z",
                "completed_at": "2026-06-01T00:01:11Z"})
    _durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_timestamps_unparseable_in_this_occurrence", gate


def test_post_step_starting_at_the_job_end_and_running_past_it_withholds():
    """A step that starts in the job's last second and ends well past it has no time
    left inside the job once its end is cut back: no span, so OPT79 withholds rather
    than reading a 0s post save on the miss side."""
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:02:00Z",
                "completed_at": "2026-06-01T00:02:30Z"})
    durs, present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_has_no_in_window_time", (durs, present, gate)
