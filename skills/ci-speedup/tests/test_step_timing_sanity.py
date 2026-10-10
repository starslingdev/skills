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
  - a step whose `started_at` falls before its job's `started_at` (the year-1
    sentinel included) contributes no duration;
  - a step's span is clamped to its job's window, so no step outlasts its job;
  - the per-run timeline the report draws applies the same rule;
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
    assert all(d < 10 * 365 * 86400 for d in durs.values()), durs


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
    for s in wf.get("steps") or []:
        assert s["p50_s"] <= _JOB_WINDOW_S, s
    assert f["runner_min_saving"] < 1519 * 2 * _JOB_WINDOW_S / 60.0, f["runner_min_saving"]
