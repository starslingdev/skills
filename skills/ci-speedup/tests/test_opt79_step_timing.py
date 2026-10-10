"""OPT79 block durations under the shared step-timing rule (`_step_span`).

A cache block step whose timestamps parse but carry no in-window span (the
year-1 sentinel start, or a start before its own job) did not measure. It must
withhold the occurrence, never read as a 0s step: a 0s post save on a miss run
removes the miss-side save cost and inflates the excess OPT79 reports.
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
    assert gate == "step_skipped_or_out_of_window", (durs, present, gate)
    assert durs == {} and present == {}


def test_post_step_started_before_its_job_withholds():
    job = _job({"conclusion": "success", "started_at": "2026-05-31T23:59:00Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    _durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_skipped_or_out_of_window", gate


def test_sentinel_started_install_step_is_named_as_out_of_window():
    job = _job({"conclusion": "success", "started_at": "2026-06-01T00:01:11Z",
                "completed_at": "2026-06-01T00:01:50Z"})
    job["steps"][1]["started_at"] = "0001-01-01T00:00:00Z"
    _durs, _present, gate = cr._opt79_block_durations(job, _BLOCK)
    assert gate == "step_skipped_or_out_of_window", gate


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
