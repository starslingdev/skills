"""Unit tests for the load-bearing MEASUREMENT functions in collect_runs.

These pin the functions every sizing number is built on, which the sizing
guardrail tests (test_collect_runs_sizing.py) hand-feed rather than exercise:

- ``_critical_path`` — derives ``long_pole_p50`` / ``floor_p50`` (the input to
  every wall-clock cap and the modeled headline) from raw job timestamps. A
  bug here silently corrupts every savings figure while the sizing tests stay
  green, because they bypass it.
- ``_detect_opt43_queue_time`` — a data-driven detector that emits savings
  numbers with no other coverage. (``_detect_opt51_install_ratio`` was CUT; its
  only contract now is the not-dispatched guard in test_measured_evidence.py.)

They call the functions directly (ci-speedup/scripts is on pythonpath via
pyproject), so the math is pinned independent of the gh pass.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_collect_runs_measurement.py
"""

from __future__ import annotations

import datetime as _dt

from collect_runs import (
    _critical_path,
    _detect_opt43_queue_time,
    _detect_shared_substep,
    _dominant_step_sample,
)

_BASE = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)


def _ts(offset_s: float) -> str:
    return (_BASE + _dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name: str, dur_s: float, *, start: float = 0.0,
         steps: list[dict] | None = None, created_offset: float | None = None,
         run_created_offset: float | None = None) -> dict:
    j: dict = {"name": name,
               "started_at": _ts(start), "completed_at": _ts(start + dur_s),
               "html_url": f"https://example/{name}"}
    if steps is not None:
        j["steps"] = steps
    if created_offset is not None:
        # the JOB's own created_at (gate-resolution for a gated job) precedes started_at.
        j["created_at"] = _ts(start - created_offset)
    if run_created_offset is not None:
        # the RUN's created_at (trigger time) — what OPT43 measures queue from.
        j["_run_created_at"] = _ts(start - run_created_offset)
    return j


def _step(name: str, dur_s: float, start: float = 0.0) -> dict:
    return {"name": name,
            "started_at": _ts(start), "completed_at": _ts(start + dur_s)}


# --- _critical_path ----------------------------------------------------------
def test_critical_path_long_pole_and_floor():
    """Long pole = tallest job p50; floor = second-tallest p50."""
    runs = [[_job("build", 100), _job("test", 50), _job("lint", 20)]]
    crit = _critical_path(runs)
    assert crit["long_pole_job"] == "build"
    assert crit["long_pole_p50"] == 100.0
    assert crit["floor_p50"] == 50.0   # second-tallest, NOT the shortest
    assert set(crit["job_p50"]) == {"build", "test", "lint"}


def test_critical_path_single_job_floor_is_zero():
    """A single-job workflow has no second job, so the cluster floor is 0.0 —
    the whole long pole is then realizable headroom. This edge is load-bearing:
    with floor 0 the wall-clock cap never clamps, so it must be deliberate."""
    crit = _critical_path([[_job("only", 90)]])
    assert crit["long_pole_p50"] == 90.0
    assert crit["floor_p50"] == 0.0


def test_critical_path_p50_interpolates_across_runs():
    """p50 of an even number of samples interpolates (matches _percentile)."""
    runs = [[_job("build", 100)], [_job("build", 200)]]
    crit = _critical_path(runs)
    assert crit["long_pole_p50"] == 150.0  # midpoint of 100 and 200


def test_critical_path_filters_zero_and_missing_durations():
    """Jobs with no parseable / non-positive duration are dropped, not counted
    as 0 (which would corrupt the floor selection)."""
    runs = [[
        _job("build", 100),
        {"name": "skipped", "started_at": None, "completed_at": None},
        _job("zero", 0),  # zero duration → filtered
    ]]
    crit = _critical_path(runs)
    assert set(crit["job_p50"]) == {"build"}
    assert crit["floor_p50"] == 0.0  # only one real job remains


def test_critical_path_empty_is_honest_zero():
    crit = _critical_path([])
    assert crit == {"long_pole_job": "", "long_pole_p50": 0.0, "long_pole_p95": 0.0,
                    "floor_p50": 0.0, "job_p50": {}, "job_bimodal": {},
                    "runner_scope": "all-runners"}


# OPT51 ("Install-to-Test Ratio") is CUT — see test_opt49_is_cut_not_dispatched
# in test_measured_evidence.py for the not-dispatched guard. Its detector unit
# tests were removed alongside the cut (a high setup/total ratio is an
# observation, not a verified lever; it sized unrealizable runner-min onto
# structurally install-bound jobs). The function is retained for reference.


# --- OPT43 queue time --------------------------------------------------------
def test_opt43_fires_on_pr_queue_over_threshold():
    # 3 runs each queued 90s from the RUN trigger; P90 = 90 > 60s PR bar.
    runs = [[_job("test", 30, run_created_offset=90)] for _ in range(3)]
    out = _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=True)
    assert len(out) == 1
    assert out[0]["id"] == "f1" and out[0]["pattern"] == "OPT43"
    assert out[0]["wall_clock_p50_s"] == 90.0


def test_opt43_measures_from_run_trigger_not_job_created():
    # The load-bearing fix: a GATED job's own created_at (gate-resolution) is only 10s
    # before it started, but the RUN was triggered 200s earlier. Measuring from the job's
    # created_at undercounts (10s < 60s → would miss it); from the run trigger it's 200s.
    runs = [[_job("e2e", 30, created_offset=10, run_created_offset=200)]
            for _ in range(3)]
    out = _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=True)
    assert len(out) == 1 and out[0]["wall_clock_p50_s"] == 200.0   # run-trigger, not 10s


def test_opt43_falls_back_to_job_created_when_run_trigger_absent():
    # Older data / direct calls without `_run_created_at` still measure queue from the
    # job's created_at rather than silently emitting nothing.
    runs = [[_job("test", 30, created_offset=90)] for _ in range(3)]
    out = _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=True)
    assert len(out) == 1 and out[0]["wall_clock_p50_s"] == 90.0


def test_opt43_run_trigger_present_but_none_falls_back_to_job_created():
    # In accumulated data `_run_created_at` is ALWAYS set (to `run.created_at`), which
    # can itself be None for a run with no trigger time. The `or` must fall through to
    # the job's own created_at rather than measure from None and emit nothing.
    runs = []
    for _ in range(3):
        j = _job("test", 30, created_offset=90)
        j["_run_created_at"] = None
        runs.append([j])
    out = _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=True)
    assert len(out) == 1 and out[0]["wall_clock_p50_s"] == 90.0


def test_opt43_release_threshold_is_higher():
    # 90s queue is under the 120s release/scheduled threshold → no finding.
    runs = [[_job("test", 30, run_created_offset=90)] for _ in range(3)]
    assert _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=False) == []


def test_opt43_needs_at_least_three_samples():
    runs = [[_job("test", 30, run_created_offset=90)] for _ in range(2)]
    assert _detect_opt43_queue_time("ci.yml", runs, 0, is_pr_workflow=True) == []


# --- _detect_shared_substep (OPT73) shared-step guard ------------------------
def _shared_substep_findings(steps_a, steps_b,
                             jobA="Integration Test prisma",
                             jobB="Integration Test drizzle"):
    wf = "ci.yml"
    crit_by_wf = {wf: {"long_pole_p50": 200.0, "job_p50": {jobA: 200.0, jobB: 180.0}}}
    jobs_per_run_by_wf = {wf: [[_job(jobA, 200, steps=steps_a),
                                _job(jobB, 180, steps=steps_b)]]}
    return _detect_shared_substep(
        crit_by_wf, jobs_per_run_by_wf, {wf: {"pull_request"}},
        (), [], 0, vol_by_wf={wf: 1000})


def test_opt73_clusters_a_genuinely_shared_named_test_step():
    # Matrix legs of one job run the SAME named test step ("Adapter Integration")
    # against different backends — a real cluster-floor lever (the better-auth case).
    out = _shared_substep_findings(
        [_step("Checkout", 5), _step("Adapter Integration", 150)],
        [_step("Checkout", 5), _step("Adapter Integration", 140)])
    assert [f["pattern"] for f in out] == ["OPT73"]


def test_opt73_does_not_cluster_heterogeneous_test_suites():
    # Distinct jobs whose `test` steps have DIFFERENT names (mastra prebuild: e2e
    # kitchen-sink vs unit shards vs store tests) are NOT one shared step you can
    # "fix once" — OPT73 must not fire on heterogeneous same-category test work.
    out = _shared_substep_findings(
        [_step("Checkout", 5), _step("Run e2e tests", 150)],
        [_step("Checkout", 5), _step("Run unit tests", 140)])
    assert out == []


# --- _dominant_step_sample (duplicate-named-step cross-run collapse) ----------

def _step_dur(name: str, dur_s: float, start: float) -> dict:
    # A timeline step carries dur_s (what `dom` ranks on) AND start/end (what the
    # cross-run extractor measures); keep both consistent.
    return {"name": name, "dur_s": dur_s,
            "started_at": _ts(start), "completed_at": _ts(start + dur_s)}


def _job_with_steps(jid: str, steps: list[dict]) -> dict:
    return {"id": jid, "html_url": f"https://example/runs/{jid}/job/{jid}",
            "steps": steps}


def test_dominant_step_sample_resolves_duplicate_named_step_to_real_occurrence():
    # Real-world trigger (embrace-android-sdk emulator job): a job emits TWO steps
    # named identically — a guarded zero-duration variant AND the real 358s run. The
    # dominant-step picker takes the 358s one (max dur_s); the cross-run extractor must
    # resolve the SAME (longest) occurrence in every sampled job, not the first textual
    # match — otherwise the per-run values collapse to 0 and contradict this_run=358.
    dup_steps = lambda: [_step_dur("Run tests on android emulator", 0.0, 0),
                         _step_dur("Setup", 10.0, 0),
                         _step_dur("Run tests on android emulator", 358.0, 20)]
    timeline = {"steps": dup_steps()}
    repr_job = _job_with_steps("r1", dup_steps())
    qual = [(0.0, _job_with_steps("r0", dup_steps())),
            (1.0, _job_with_steps("r2", dup_steps()))]

    mag = _dominant_step_sample(timeline, qual, repr_job)
    assert mag is not None
    assert mag["this_run"] == 358.0
    vals = [v["value"] for v in mag["values"]]
    # every sampled run (incl. the drilled one) must reflect the 358s occurrence,
    # never the 0s guarded variant — the bug produced [0.0, 0.0, 0.0]
    assert vals == [358.0, 358.0, 358.0], vals
    assert all(v["value"] == 358.0 for v in mag["values"] if v["drilled"])


def test_dominant_step_sample_single_named_step_still_samples_correctly():
    # Guard against over-correction: a normally single-named step must keep sampling
    # each run's own value (here a real spread), not always the max.
    timeline = {"steps": [_step_dur("Gradle :test", 1027.0, 0)]}
    repr_job = _job_with_steps("r1", [_step_dur("Gradle :test", 1027.0, 0)])
    qual = [(0.0, _job_with_steps("r0", [_step_dur("Gradle :test", 46.0, 0)])),
            (1.0, _job_with_steps("r2", [_step_dur("Gradle :test", 2812.0, 0)]))]
    mag = _dominant_step_sample(timeline, qual, repr_job)
    assert mag["this_run"] == 1027.0
    assert sorted(v["value"] for v in mag["values"]) == [46.0, 1027.0, 2812.0]
