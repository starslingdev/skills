"""Step timings read from the GitHub jobs API must stay inside their own job.

The jobs API reports a SKIPPED step (for example a `background: true` step whose
`if:` was false) as

    {"conclusion": "skipped", "started_at": "0001-01-01T00:00:00Z",
     "completed_at": "<a real 2026 timestamp>"}

Read naively that is a ~63.9-billion-second step (year 1 to 2026). On curl/curl
(run 38018162993, job `CM clang-tidy`, steps `test-linter`, `randcurl`,
`single-use function check`) it produced a HIGH "shared step recurs across the
cluster" finding worth 145,186 runner-min/mo for a step that never ran in that
job. These tests pin the rule every step-timing reader shares:

  - a step GitHub reports as `skipped` contributes no duration;
  - a step whose `started_at` falls in 1970 or earlier (the year-1 placeholder
    included) contributes no duration, with or without the job's own window;
  - a step starting more than 1s before its job's `started_at` contributes no
    duration; within that 1s (whole-second rounding) it is clamped to the job's
    start;
  - a step's end is clamped to its job's end, so no step outlasts its job, and a
    cut of more than 1s is counted (`trimmed_steps`) and logged;
  - the per-run timeline the report draws applies the same rule, and keeps a
    skipped step whose timestamps sit inside the job in its place at 0s;
  - every declared step left out is counted (`skipped_steps`,
    `unmeasured_steps`), logged at DEBUG, and named in one line on the pole;
  - a step that only carries the sentinel can never become a cluster finding.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_step_timing_sanity.py
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import collect_runs as cr  # noqa: E402

_SENTINEL = "0001-01-01T00:00:00Z"
_BASE = _dt.datetime(2026, 10, 10, 2, 46, 0)


def _ts(sec: int) -> str:
    """An ISO timestamp `sec` seconds after 2026-10-10T02:46:00Z (the API shape)."""
    t = _BASE + _dt.timedelta(seconds=sec)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _step(n: int, name: str, start: int | None, end: int,
          conclusion: str = "success") -> dict:
    return {"number": n, "name": name, "status": "completed",
            "conclusion": conclusion,
            "started_at": _SENTINEL if start is None else _ts(start),
            "completed_at": _ts(end)}


def _clang_tidy_job(job_id: int = 1, name: str = "CM clang-tidy",
                    build_name: str = "build", tag: str = "") -> dict:
    """The shape of curl run 38018162993's `CM clang-tidy` job: real steps,
    zero-span skipped steps, and three skipped background steps carrying the
    year-1 `started_at` sentinel. `tag` renames the job's other material steps
    so two jobs can be built that share no real step by name."""
    steps = [
        _step(1, "Set up job", 1, 3),
        _step(2, "install prereqs" + tag, 3, 31),
        _step(3, "install prereqs (i686)", 31, 31, "skipped"),
        _step(31, "Run actions/checkout@v5", 34, 36),
        _step(33, "configure" + tag, 36, 57),
        _step(38, build_name, 57, 178),
        _step(39, "single-use function check", None, 178, "skipped"),
        _step(42, "randcurl", None, 178, "skipped"),
        _step(43, "build tests" + tag, 178, 207, "failure"),
        _step(44, "test-linter", None, 207, "skipped"),
        _step(45, "run tests", 207, 207, "skipped"),
        _step(99, "Complete job", 207, 207),
    ]
    return {"id": job_id, "name": name, "status": "completed",
            "conclusion": "failure",
            "html_url": f"https://github.com/curl/curl/actions/runs/1/job/{job_id}",
            "started_at": _ts(0), "completed_at": _ts(209), "steps": steps}


_JOB_WINDOW_S = 209.0


# --------------------------------------------------------------------------- #
# The shared per-step rule
# --------------------------------------------------------------------------- #

def test_skipped_step_with_year_one_start_contributes_no_duration():
    durs = dict(cr._step_durations(_clang_tidy_job()))
    for name in ("test-linter", "randcurl", "single-use function check"):
        assert name not in durs, (name, durs.get(name))
    assert durs["build"] == 121.0


def test_skipped_step_with_a_real_span_contributes_no_duration():
    job = _clang_tidy_job()
    job["steps"].append(_step(50, "skipped but spanned", 100, 150, "skipped"))
    assert "skipped but spanned" not in dict(cr._step_durations(job))


def test_step_starting_before_its_job_contributes_no_duration():
    job = _clang_tidy_job()
    # Not skipped, yet its start predates the job's own start by a minute.
    job["steps"].append({"number": 60, "name": "early", "conclusion": "success",
                         "started_at": "2026-10-10T02:45:00Z",
                         "completed_at": _ts(30)})
    # The year-1 sentinel on a step that claims success is still not a start.
    job["steps"].append({"number": 61, "name": "sentinel success",
                         "conclusion": "success", "started_at": _SENTINEL,
                         "completed_at": _ts(40)})
    durs = dict(cr._step_durations(job))
    assert "early" not in durs
    assert "sentinel success" not in durs


def test_year_one_start_is_dropped_even_without_job_timestamps():
    job = _clang_tidy_job()
    job.pop("started_at")
    job.pop("completed_at")
    job["steps"].append({"number": 62, "name": "no-job-window sentinel",
                         "conclusion": "success", "started_at": _SENTINEL,
                         "completed_at": _ts(40)})
    durs = dict(cr._step_durations(job))
    assert "no-job-window sentinel" not in durs
    # Non-vacuous: the real steps are still measured without a job window.
    assert durs["build"] == 121.0, durs
    assert all(d < 10 * 365 * 86400 for d in durs.values()), durs


def test_start_cutoff_is_year_1970_inclusive():
    """A `started_at` in 1970 is a placeholder (an epoch-zero stamp), not a start;
    1971 is the first year read as a real start. No job window, so only the
    sentinel rule is in play."""
    def step(start: str, end: str) -> dict:
        return {"name": "s", "conclusion": "success", "started_at": start,
                "completed_at": end}
    assert cr._step_duration_s(step("1970-01-01T00:00:00Z", "1970-01-01T00:00:10Z"),
                               None) is None
    assert cr._step_duration_s(step("1970-12-31T23:59:50Z", "1971-01-01T00:00:00Z"),
                               None) is None
    assert cr._step_duration_s(step("1971-01-01T00:00:00Z", "1971-01-01T00:00:10Z"),
                               None) == 10.0


def _window_job(*steps: dict) -> dict:
    """A job running from _ts(10) to _ts(110), carrying `steps`."""
    return {"name": "w", "started_at": _ts(10), "completed_at": _ts(110),
            "steps": list(steps)}


def test_step_starting_within_one_second_before_its_job_is_clamped_to_the_job_start():
    """Step and job stamps round independently, so a step starting 0.5s before its
    job is rounding: it keeps its duration, clamped to the job's start. Two seconds
    before is a step that did not start inside this job: no duration."""
    j0 = _BASE + _dt.timedelta(seconds=10)
    half = (j0 - _dt.timedelta(seconds=0.5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rounding = {"name": "rounding", "conclusion": "success", "started_at": half,
                "completed_at": _ts(30)}
    early = {"name": "early", "conclusion": "success", "started_at": _ts(8),
             "completed_at": _ts(30)}
    job = _window_job(rounding, early)
    assert cr._step_duration_s(rounding, job) == 20.0
    assert cr._step_duration_s(early, job) is None
    assert dict(cr._step_durations(job)) == {"rounding": 20.0}


def test_reversed_step_timestamps_have_no_duration():
    """A step whose `completed_at` precedes its `started_at` has no span, with or
    without the job's window."""
    step = {"name": "reversed", "conclusion": "success", "started_at": _ts(50),
            "completed_at": _ts(40)}
    assert cr._step_duration_s(step, _window_job(step)) is None
    assert cr._step_duration_s(step, None) is None


def test_step_durations_never_exceed_the_job_window():
    job = _clang_tidy_job()
    # A step whose end overshoots the job's completion is clamped to it.
    job["steps"].append(_step(70, "overshoot", 200, 400))
    durs = dict(cr._step_durations(job))
    assert durs["overshoot"] == 9.0, durs["overshoot"]
    assert max(durs.values()) <= _JOB_WINDOW_S, durs


# --------------------------------------------------------------------------- #
# The per-run timeline the report draws
# --------------------------------------------------------------------------- #

def test_step_timeline_agrees_with_step_durations():
    job = _clang_tidy_job()
    job["steps"].append(_step(70, "overshoot", 200, 400))
    tl = cr._step_timeline(job, "CM clang-tidy", _JOB_WINDOW_S)
    names = [s["name"] for s in tl["steps"]]
    for name in ("test-linter", "randcurl", "single-use function check"):
        assert name not in names, (name, tl["steps"])
    for s in tl["steps"]:
        assert s["dur_s"] <= _JOB_WINDOW_S, s
        assert s["start_s"] + s["dur_s"] <= _JOB_WINDOW_S, s
    durs = dict(cr._step_durations(job))
    for s in tl["steps"]:
        if s["dur_s"] > 0:
            assert durs[s["name"]] == s["dur_s"], (s, durs.get(s["name"]))


def test_in_window_skipped_step_stays_on_the_timeline_at_zero():
    """A skipped step whose timestamps sit inside its job keeps its place in the
    succession, drawn at 0s: it did not run, and that is shown, not hidden."""
    job = _clang_tidy_job()
    job["steps"].append(_step(50, "skipped but spanned", 100, 150, "skipped"))
    tl = cr._step_timeline(job, "CM clang-tidy", _JOB_WINDOW_S)
    by_name = {s["name"]: s for s in tl["steps"]}
    for name, start in (("install prereqs (i686)", 31.0), ("run tests", 207.0),
                        ("skipped but spanned", 100.0)):
        assert name in by_name, (name, tl["steps"])
        assert by_name[name]["dur_s"] == 0.0, by_name[name]
        assert by_name[name]["start_s"] == start, by_name[name]


# --------------------------------------------------------------------------- #
# The dominant step's cross-run sample
# --------------------------------------------------------------------------- #

def _test_job(job_id: int, test_s: int | None, conclusion: str = "success",
              start: str | None = None) -> dict:
    """A job whose `run tests` step ran `test_s` seconds (`conclusion` as given)."""
    end = 5 + (test_s or 0)
    run_tests = _step(2, "run tests", 5, end, conclusion)
    if start is not None:
        run_tests["started_at"] = start
    return {"id": job_id, "name": "test", "status": "completed", "conclusion": "success",
            "html_url": f"https://github.com/acme/app/actions/runs/{job_id}/job/{job_id}",
            "started_at": _ts(0), "completed_at": _ts(end + 1),
            "steps": [_step(1, "Set up job", 0, 5), run_tests,
                      _step(3, "Complete job", end, end + 1)]}


def _dominant_sample(fastest: dict) -> list[dict]:
    drilled = _test_job(2, 280)
    slowest = _test_job(3, 300)
    timeline = cr._step_timeline(drilled, "test", 286.0)
    sample = cr._dominant_step_sample(
        timeline, [(16.0, fastest), (286.0, drilled), (306.0, slowest)], drilled)
    assert sample is not None and sample["label"] == "the `run tests` step (wall)"
    return sample["values"]


def test_dominant_step_sample_counts_a_skipped_run_as_zero():
    """A run where GitHub skipped the dominant step is a truthful "did not run":
    it contributes 0s to the cross-run sample, not nothing."""
    values = _dominant_sample(_test_job(1, 0, "skipped", start=_SENTINEL))
    assert [v["value"] for v in values] == [280.0, 0.0, 300.0], values


def test_dominant_step_sample_drops_an_out_of_window_run():
    """A step that claims to have run but carries the year-1 placeholder start has
    no measurement: that run is left out of the sample, never read as a duration."""
    values = _dominant_sample(_test_job(1, 10, start=_SENTINEL))
    assert [v["value"] for v in values] == [280.0, 300.0], values


# --------------------------------------------------------------------------- #
# Downstream: decomposition and the cluster-floor (OPT73) finding
# --------------------------------------------------------------------------- #

def test_decomposition_job_total_stays_inside_the_job_window():
    d = cr._decompose_job_steps([_clang_tidy_job(i) for i in range(1, 6)])
    assert d is not None
    assert d["job_p50"] <= _JOB_WINDOW_S, d["job_p50"]
    assert {n for n, _c, _p in d["steps"]}.isdisjoint(
        {"test-linter", "randcurl", "single-use function check"})
    # The omitted steps are counted, not silently lost: `install prereqs (i686)`,
    # `single-use function check`, `randcurl`, `test-linter`, `run tests`.
    assert d["skipped_steps"] == 5, d.get("skipped_steps")


def test_skipped_steps_counts_only_current_steps_that_never_ran():
    """`skipped_steps` counts a step the CURRENT workflow version declares and that
    GitHub skipped on every sampled run. A step skipped in some runs but run in
    others is in `steps` already; a skipped step only an older version declared
    (absent from the newest run) is not the job's any more."""
    def job(i: int, steps: list[tuple[str, int, int, str]]) -> dict:
        day = _dt.timedelta(days=i)
        def t(sec: int) -> str:
            return (_BASE + day + _dt.timedelta(seconds=sec)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        return {"id": i, "name": "j", "conclusion": "success",
                "started_at": t(0), "completed_at": t(100),
                "steps": [{"name": n, "number": k + 1, "conclusion": c,
                           "started_at": t(a), "completed_at": t(b)}
                          for k, (n, a, b, c) in enumerate(steps)]}
    old = [("Set up job", 0, 2, "success"), ("build", 2, 60, "success"),
           ("retired", 60, 60, "skipped"), ("gate", 60, 60, "skipped"),
           ("flaky", 60, 60, "skipped")]
    # The newest run declares as many steps (so it anchors the current version):
    # `retired` is gone, `new step` is added, and `flaky` ran this time.
    new = [("Set up job", 0, 2, "success"), ("build", 2, 60, "success"),
           ("new step", 60, 70, "success"), ("gate", 70, 70, "skipped"),
           ("flaky", 70, 90, "success")]
    d = cr._decompose_job_steps([job(1, old), job(2, old), job(3, new)])
    assert d is not None
    assert "flaky" in {n for n, _c, _p in d["steps"]}, d["steps"]
    # Only `gate`: declared now, skipped on every sampled run.
    assert d["skipped_steps"] == 1, d.get("skipped_steps")


def _cluster(build_names: tuple[str, str], tags: tuple[str, str] = ("", "")):
    runs = [[_clang_tidy_job(10 * r + 1, "CM clang-tidy", build_names[0], tags[0]),
             _clang_tidy_job(10 * r + 2, "CM openssl torture 2", build_names[1], tags[1])]
            for r in range(5)]
    wf = ".github/workflows/linux.yml"
    crit_by_wf = {wf: cr._critical_path(runs)}
    return cr._detect_shared_substep(
        crit_by_wf, {wf: runs}, {wf: {"pull_request"}},
        (("CM clang-tidy", 209.0), ("CM openssl torture 2", 209.0)), [], 0,
        vol_by_wf={wf: 1519})


def test_skipped_sentinel_step_never_becomes_a_shared_step_finding():
    # The two jobs share NO real step by name (`build` vs `make`); the only steps
    # they share by name are the skipped sentinel ones. No cluster lever exists.
    out = _cluster(("build", "make"), (" (clang)", " (openssl)"))
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert not opt73, json.dumps(opt73, default=str)[:1500]


def test_shared_real_step_finding_cites_only_in_window_numbers():
    # Both jobs share a real `build` step: the lever is real and must cite it,
    # with every figure inside the 209s job window.
    out = _cluster(("build", "build"))
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert opt73, "expected the real shared `build` step to surface"
    f = opt73[0]
    assert "`build`" in f["evidence"], f["evidence"]
    wf = (f.get("measured_evidence") or {}).get("waterfall") or {}
    assert wf.get("job_p50_s", 0) <= _JOB_WINDOW_S, wf.get("job_p50_s")
    # Non-vacuous: the waterfall carries steps for the loop below to bound.
    assert wf.get("steps"), wf
    for s in wf.get("steps") or []:
        assert s["p50_s"] <= _JOB_WINDOW_S, s
    assert f["runner_min_saving"] < 1519 * 2 * _JOB_WINDOW_S / 60.0, f["runner_min_saving"]


# --------------------------------------------------------------------------- #
# OPT82: the lint step's own p50 reads step durations under the same rule
# --------------------------------------------------------------------------- #

def _opt82_fixtures():
    """The OPT82 suite's repo-tree and run builders, loaded under a private name so
    its tests are not collected twice."""
    import importlib.util
    name = "_opt82_fixtures_for_step_timing"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "test_opt82_type_aware_lint.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def test_opt82_lint_step_p50_ignores_a_skipped_year_one_lint_step(tmp_path):
    """In two of three sampled runs GitHub skipped the lint step and stamped its
    `started_at` at the year-1 placeholder. Those runs carry no lint-step duration;
    the ceiling is the one run that linted (78s), never a ~63.9-billion-second p50."""
    import scan
    fx = _opt82_fixtures()
    runs = fx._runs(95, 20)
    for run in runs[:2]:
        lint = next(s for s in run[0]["steps"] if s["name"] == "Lint")
        lint["conclusion"] = "skipped"
        lint["started_at"] = _SENTINEL
    crit = cr._critical_path(runs)
    block = scan._read_type_aware_lint(fx._tree(tmp_path))
    withheld: dict = {}
    out = cr._detect_opt82_type_aware_lint(
        ".github/workflows/lint.yml", runs, crit, fx._WF, block, 0, withheld=withheld)
    assert len(out) == 1, withheld
    tal = out[0]["type_aware_lint"]
    assert tal["lint_step_p50_s"] == 78.0, tal["lint_step_p50_s"]
    assert tal["ceiling_basis"] == "lint_step" and tal["ceiling_s"] == 78.0, tal


# --------------------------------------------------------------------------- #
# Every drop and every trim is counted and logged, never silent
# --------------------------------------------------------------------------- #

def _unmeasured_job(i: int) -> dict:
    """`_clang_tidy_job` plus a step claiming success with the year-1 start, and a
    step that starts a minute before its job: both declared, neither measurable."""
    job = _clang_tidy_job(i)
    job["steps"].append({"number": 60, "name": "early", "conclusion": "success",
                         "started_at": "2026-10-10T02:45:00Z", "completed_at": _ts(30)})
    job["steps"].append({"number": 61, "name": "sentinel success",
                         "conclusion": "success", "started_at": _SENTINEL,
                         "completed_at": _ts(40)})
    return job


def test_decomposition_counts_declared_steps_with_no_usable_time():
    d = cr._decompose_job_steps([_unmeasured_job(i) for i in range(1, 4)])
    assert d is not None
    assert d.get("unmeasured_steps") == 2, d.get("unmeasured_steps")
    # Skipped steps keep their own count; the two figures never overlap.
    assert d["skipped_steps"] == 5, d["skipped_steps"]


def test_decomposition_stamps_no_unmeasured_count_when_none_dropped():
    d = cr._decompose_job_steps([_clang_tidy_job(i) for i in range(1, 4)])
    assert d is not None
    assert "unmeasured_steps" not in d and "trimmed_steps" not in d, d


def test_decomposition_counts_a_step_trimmed_to_its_job_end():
    jobs = [_clang_tidy_job(i) for i in range(1, 4)]
    for j in jobs:
        j["steps"].append(_step(70, "overshoot", 200, 400))
    d = cr._decompose_job_steps(jobs)
    assert d is not None and d.get("trimmed_steps") == 1, d


def test_each_dropped_and_trimmed_step_is_logged_at_debug(caplog):
    import logging
    job = _unmeasured_job(1)
    job["steps"].append(_step(70, "overshoot", 200, 400))
    with caplog.at_level(logging.DEBUG, logger=cr.logger.name):
        cr._step_durations(job)
    msgs = [r.getMessage() for r in caplog.records]
    for name, why in (("early", "started_before_job"),
                      ("sentinel success", "placeholder_start"),
                      ("test-linter", "skipped")):
        assert any(f"'{name}'" in m and why in m and "CM clang-tidy" in m
                   for m in msgs), (name, why, msgs)
    assert any("'overshoot'" in m and "trimmed" in m for m in msgs), msgs


def test_a_job_whose_every_step_dropped_still_stamps_a_decomposition():
    """No step measured in the sample is a fact to state, not a missing breakdown."""
    def job(i: int) -> dict:
        return {"id": i, "name": "bg", "conclusion": "success",
                "started_at": _ts(0), "completed_at": _ts(60),
                "steps": [_step(1, "lint", None, 30, "skipped"),
                          _step(2, "bench", None, 40, "skipped"),
                          {"number": 3, "name": "probe", "conclusion": "success",
                           "started_at": _SENTINEL, "completed_at": _ts(50)}]}
    d = cr._decompose_job_steps([job(1), job(2)])
    assert d is not None, "an all-dropped job must not vanish"
    assert d["steps"] == [] and d["reason"] == "no_step_measured_in_sample", d
    assert d["skipped_steps"] == 2 and d["unmeasured_steps"] == 1, d


def test_pole_stamp_carries_the_counts_and_the_no_step_reason():
    entry: dict = {}
    cr._stamp_pole_decomposition(entry, {
        "steps": [], "reason": "no_step_measured_in_sample",
        "skipped_steps": 2, "unmeasured_steps": 1})
    assert entry == {"step_decomposition_reason": "no_step_measured_in_sample",
                     "skipped_steps": 2, "unmeasured_steps": 1}, entry
    entry = {}
    cr._stamp_pole_decomposition(entry, cr._decompose_job_steps(
        [_unmeasured_job(i) for i in range(1, 4)]))
    assert entry["skipped_steps"] == 5 and entry["unmeasured_steps"] == 2, entry
    assert entry["steps"] and "step_decomposition_reason" not in entry, entry


def test_callers_treat_an_all_dropped_decomposition_as_no_steps():
    """A decomposition with no measured step crowns no dominant step: the structural
    and runner-size callers see it the way they saw None."""
    d = {"steps": [], "reason": "no_step_measured_in_sample",
         "skipped_steps": 1, "unmeasured_steps": 0}
    assert cr._measured_decomposition(d) is None
    assert cr._measured_decomposition(None) is None
    full = cr._decompose_job_steps([_clang_tidy_job(i) for i in range(1, 4)])
    assert cr._measured_decomposition(full) is full


# --------------------------------------------------------------------------- #
# Timezones, the setup prefix, and the drilled run's own sample entry
# --------------------------------------------------------------------------- #

def test_naive_step_timestamp_beside_an_aware_job_still_measures():
    """A step stamp without a timezone is read as UTC, not a TypeError."""
    step = {"name": "naive", "conclusion": "success",
            "started_at": "2026-10-10T02:46:20", "completed_at": "2026-10-10T02:46:50"}
    assert cr._step_duration_s(step, _window_job(step)) == 30.0
    job = _window_job(step)
    job["started_at"] = "2026-10-10T02:46:10"
    aware = {**step, "started_at": "2026-10-10T02:46:20Z",
             "completed_at": "2026-10-10T02:46:50Z"}
    assert cr._step_duration_s(aware, job) == 30.0


def test_setup_prefix_does_not_show_a_setup_step_that_has_no_time():
    """A setup step with no in-window span adds nothing to the prefix's seconds, so it
    is not named in the prefix's display either: shown steps and total describe the
    same steps. The signature keeps it, so the job's shape does not change between a
    run that skipped it and one that ran it."""
    job = {"name": "j", "started_at": _ts(0), "completed_at": _ts(100), "steps": [
        _step(1, "Set up job", 0, 2),
        _step(2, "Run actions/checkout@v4", 2, 5),
        {"number": 3, "name": "Run actions/setup-node@v4", "conclusion": "success",
         "started_at": _SENTINEL, "completed_at": _ts(9)},
        _step(4, "npm test", 9, 90)]}
    sig, shown, total = cr._leading_setup_prefix(job)
    assert "Run actions/setup-node@v4" not in shown, shown
    assert shown == ("Set up job", "Run actions/checkout@v4"), shown
    assert len(sig) == 3 and total == 5.0, (sig, shown, total)


def test_dominant_step_sample_keeps_the_drilled_run_when_its_step_has_no_time():
    """The drilled run is always in the cross-run sample; with no usable time for its
    own dominant step it is listed with no value, never silently omitted."""
    drilled = _test_job(2, 280)
    timeline = cr._step_timeline(drilled, "test", 286.0)
    no_time = _test_job(2, 280, start=_SENTINEL)
    sample = cr._dominant_step_sample(
        timeline, [(16.0, _test_job(1, 10)), (286.0, no_time),
                   (306.0, _test_job(3, 300))], no_time)
    assert sample is not None
    drilled_rows = [v for v in sample["values"] if v["drilled"]]
    assert [(v["value"], v["drilled"]) for v in drilled_rows] == [(None, True)], (
        sample["values"])


# --------------------------------------------------------------------------- #
# The report says how many declared steps a pole's step list leaves out
# --------------------------------------------------------------------------- #

def _bp():
    import blocking_path
    return blocking_path


_POLE = {"check": "test", "job": "test", "workflow_file": ".github/workflows/ci.yml",
         "p50_s": 120.0, "dominant_step": "run tests", "dominant_category": "test",
         "dominant_p50_s": 100.0,
         "steps": [{"step": "run tests", "category": "test", "p50_s": 100.0},
                   {"step": "Set up job", "category": "setup", "p50_s": 20.0}]}
_NOTE = ("(2 declared step(s) skipped on every sampled run and 1 with no usable time "
         "are not timed here)")


def test_pole_step_list_says_how_many_declared_steps_it_leaves_out():
    pole = dict(_POLE, skipped_steps=2, unmeasured_steps=1)
    lines = _bp()._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    assert [ln for ln in lines if "declared step(s)" in ln] == [_NOTE], lines


def test_pole_timeline_says_how_many_declared_steps_it_leaves_out():
    pole = dict(_POLE, skipped_steps=2, unmeasured_steps=1)
    timeline = cr._step_timeline(_test_job(2, 100), "test", 106.0)
    lines = _bp()._pole_waterfall(pole, leaf=None, timeline=timeline, log_present=False)
    assert [ln for ln in lines if "declared step(s)" in ln] == [_NOTE], lines


def test_pole_with_no_omitted_steps_renders_no_note():
    lines = _bp()._pole_waterfall(dict(_POLE, skipped_steps=0), leaf=None,
                                  timeline=None, log_present=False)
    assert not [ln for ln in lines if "declared step" in ln or "no usable" in ln], lines


def test_pole_whose_every_step_dropped_says_no_step_could_be_measured():
    pole = {k: v for k, v in _POLE.items()
            if k not in ("steps", "dominant_step", "dominant_category", "dominant_p50_s")}
    pole.update(step_decomposition_reason="no_step_measured_in_sample",
                skipped_steps=2, unmeasured_steps=1)
    lines = _bp()._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    assert lines[0] == "No step could be measured: 2 skipped, 1 with no usable time.", lines
    # No step header and no step rows: there is no step to list.
    assert not [ln for ln in lines if "every step" in ln or "Level 2" in ln], lines
    assert lines[1:] == ["", "(no captured log for this job — run with `--log "
                         "ci=<job log>` to drill into this job.)"], lines


def _dropped_pole() -> dict:
    pole = {k: v for k, v in _POLE.items()
            if k not in ("steps", "dominant_step", "dominant_category", "dominant_p50_s")}
    pole.update(step_decomposition_reason="no_step_measured_in_sample",
                skipped_steps=2, unmeasured_steps=1)
    return pole


def test_all_dropped_pole_with_a_captured_log_still_says_it_is_a_coverage_gap():
    """The note does not replace the pointer lines: a captured log no detector matched
    is still a coverage gap, and the LLM analysis and catalog pointers still render."""
    bp = _bp()
    note = "No step could be measured: 2 skipped, 1 with no usable time."
    lines = bp._pole_waterfall(_dropped_pole(), leaf=None, timeline=None, log_present=True)
    assert lines[0] == note, lines
    assert any("this is a coverage gap, not a clean job" in ln for ln in lines), lines
    lines = bp._pole_waterfall(_dropped_pole(), leaf=None, timeline=None,
                               log_present=True, analysis_present=True)
    assert any("LLM root-cause analysis" in ln for ln in lines), lines
    lines = bp._pole_waterfall(_dropped_pole(), leaf=None, timeline=None,
                               log_present=True, structural_present=True)
    assert any("structural catalog pattern" in ln for ln in lines), lines
    lines = bp._pole_waterfall(_dropped_pole(), leaf=None, timeline=None,
                               log_present=True, opt79_present=True)
    assert any("OPT79" in ln for ln in lines), lines


def test_all_dropped_pole_with_a_log_drill_still_renders_level_three():
    leaf = {"unit_label": "slowest test files", "search": [],
            "deeper": [{"header": "files", "blocker_note": "the slowest file",
                        "rows": [("a.test.ts", 50.0, None), ("b.test.ts", 20.0, None)]}]}
    lines = _bp()._pole_waterfall(_dropped_pole(), leaf=leaf, timeline=None,
                                  log_present=True)
    assert lines[0].startswith("No step could be measured:"), lines
    assert any("Level 3" in ln and "slowest test files" in ln for ln in lines), lines
    assert any("a.test.ts" in ln for ln in lines), lines


# --------------------------------------------------------------------------- #
# A step whose start is the job's last second and whose end runs past it
# --------------------------------------------------------------------------- #

def _late_job(i: int) -> dict:
    """A job running _ts(0).._ts(60) whose `late` step starts at the job's end second
    and ends 30s past it: cut back to the job's end it has no time left."""
    return {"id": i, "name": "late", "conclusion": "success",
            "started_at": _ts(0), "completed_at": _ts(60),
            "steps": [_step(1, "work", 0, 60), _step(2, "late", 60, 90)]}


def _mixed_job(i: int, flaky_start: int | None, gate: str = "skip") -> dict:
    """A job with a measured `work` step, a `flaky` step whose start is the year-1
    placeholder when `flaky_start` is None, and a `gate` step that GitHub skipped
    (`gate="skip"`) or that claims success with the placeholder start (`"none"`)."""
    flaky = _step(2, "flaky", flaky_start, 40)
    gate_step = (_step(3, "gate", None, 50, "skipped") if gate == "skip"
                 else _step(3, "gate", None, 50))
    return {"id": i, "name": "mixed", "conclusion": "success",
            "started_at": _ts(0), "completed_at": _ts(60),
            "steps": [_step(1, "work", 0, 30), flaky, gate_step]}


def test_a_step_measured_in_only_some_runs_is_counted_as_partially_measured():
    """10 runs: `flaky` has the placeholder start in 7 and measures in 3. Its p50
    comes from 3 runs, so the decomposition says so instead of counting it nowhere."""
    jobs = [_mixed_job(i, None if i < 7 else 30) for i in range(10)]
    d = cr._decompose_job_steps(jobs)
    assert d is not None
    assert "flaky" in {n for n, _c, _p in d["steps"]}, d["steps"]
    assert d.get("partially_measured_steps") == 1, d
    assert "unmeasured_steps" not in d and d["skipped_steps"] == 1, d
    entry: dict = {}
    cr._stamp_pole_decomposition(entry, d)
    assert entry.get("partially_measured_steps") == 1, entry


def test_no_partially_measured_count_when_every_run_measures_every_step():
    d = cr._decompose_job_steps([_mixed_job(i, 30) for i in range(3)])
    assert d is not None and "partially_measured_steps" not in d, d


def test_a_step_skipped_in_one_run_and_untimed_in_another_is_not_called_skipped():
    """`skipped_steps` means skipped on EVERY sampled run; a step skipped in one run
    and claiming success with no usable time in the other has no usable time."""
    d = cr._decompose_job_steps([_mixed_job(1, 30, "skip"), _mixed_job(2, 30, "none")])
    assert d is not None
    assert d["skipped_steps"] == 0 and d.get("unmeasured_steps") == 1, d


def test_pole_note_names_steps_timed_in_only_some_runs():
    pole = dict(_POLE, skipped_steps=0, partially_measured_steps=1)
    lines = _bp()._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    assert "(1 step(s) were timed in only some sampled runs)" in lines, lines
    pole = dict(_POLE, skipped_steps=2, unmeasured_steps=1, partially_measured_steps=1)
    lines = _bp()._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    assert (_NOTE[:-1] + "; 1 step(s) were timed in only some sampled runs)") in lines, lines


def test_step_trimmed_to_nothing_has_no_span_and_counts_as_unmeasured():
    job = _late_job(1)
    late = job["steps"][1]
    assert cr._step_span_verdict(late, job) == (None, "started_after_job")
    d = cr._decompose_job_steps([_late_job(i) for i in range(1, 4)])
    assert d is not None
    assert d.get("unmeasured_steps") == 1 and "trimmed_steps" not in d, d
