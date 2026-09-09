"""Workstream C — DESCRIPTIVE timing spread for a measured pole.

WHY THIS EXISTS. The report ranks poles by p50 and carries a p95 for the long pole, and
neither quantile can express how much a check's duration actually MOVED across the sampled
runs. The counterexample is exact: twenty observations of 100s, and nine of 1s plus eleven
of 100s, both give p50 = p95 = 100s under `_percentile` — one sample never varies, the
other varies by 99s. A reader shown only the quantiles cannot tell them apart.

WHAT THIS PINS. For each measured pole the engine stamps a versioned DESCRIPTIVE summary —
`n`, `min_s`, `median_s`, `max_s`, its basis/selection identifiers, its sample ids, and a
coverage status — re-derived from the observations the sampler ALREADY fetched. It is a
description of the observed sample. It is deliberately NOT:

  - a symmetric +/- band,
  - a "minimum detectable effect" or an "outside noise" verdict,
  - any statistical-significance claim,
  - an input to the Bottom-line savings number.

Duration spread is not uncertainty in an estimated change, and this module's tests fail if
that vocabulary ever appears in the rendered summary.

All fixtures are SYNTHETIC job payloads shaped like the GitHub jobs API. No captured run,
log, or session artifact is committed here.

Run: pytest -v skills/ci-speedup/tests/test_timing_spread.py
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp   # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr    # noqa: E402


# --------------------------------------------------------------------------------------
# Synthetic observation builders — job payloads shaped like the GitHub jobs API.
# --------------------------------------------------------------------------------------

def _job(job_id: int, dur_s: float, *, name: str = "tests-web",
         runner: str = "ubuntu-latest", run_id: int = 0, attempt: int = 1,
         conclusion: str = "success") -> dict:
    """One sampled job execution lasting `dur_s`. `started_at`/`completed_at` are the
    SAME timing definition `_job_duration_s` reads for the pole's own p50."""
    start = 3600
    return {
        "id": job_id,
        "run_id": run_id or (10_000 + job_id),
        "run_attempt": attempt,
        "name": name,
        "conclusion": conclusion,
        "labels": [runner],
        "runner_name": runner,
        "started_at": _iso(start),
        "completed_at": _iso(start + dur_s),
    }


def _iso(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"2026-09-08T{h:02d}:{m:02d}:{s:02d}Z"


def _runs(durations: list[float], **kw) -> list[list[dict]]:
    """`jobs_per_run` — one sampled run per duration, exactly the shape
    `_critical_path` consumes and `jobs_per_run_by_wf` retains."""
    return [[_job(100 + i, d, run_id=20_000 + i, **kw)] for i, d in enumerate(durations)]


_WF = ".github/workflows/pipeline.yml"
_JOB = "tests-web"


def _spread_for(durations: list[float], **kw) -> dict:
    """Run the PRODUCTION selection: build the critical path from the sampled runs (which
    is what fixes the pole's timing basis, runner scope and quantiles), then ask for that
    pole's descriptive summary off the SAME retained observations."""
    jobs_per_run = _runs(durations, **kw)
    crit = cr._critical_path(jobs_per_run)
    return cr._pole_timing_spread(
        _JOB, _WF, _JOB, {_WF: crit}, {_WF: jobs_per_run}, [])


# --------------------------------------------------------------------------------------
# 1. The counterexample: identical p50 AND p95, different observed range.
# --------------------------------------------------------------------------------------

_CONSTANT = [100.0] * 20
_SPREAD = [1.0] * 9 + [100.0] * 11


def test_quantiles_agree_on_both_samples():
    """The premise. If this ever fails the counterexample below is no longer a
    counterexample and the whole workstream needs re-motivating."""
    assert cr._percentile(_CONSTANT, 50) == cr._percentile(_SPREAD, 50) == 100.0
    assert cr._percentile(_CONSTANT, 95) == cr._percentile(_SPREAD, 95) == 100.0


def test_counterexample_fixtures_render_different_observed_ranges():
    a = _spread_for(_CONSTANT)
    b = _spread_for(_SPREAD)
    assert a["median_s"] == b["median_s"] == 100.0, (a, b)
    assert (a["min_s"], a["max_s"]) == (100.0, 100.0)
    assert (b["min_s"], b["max_s"]) == (1.0, 100.0)
    assert a["n"] == 20 and b["n"] == 20
    # And the RENDERED sentence differs — the reader, not just the artifact, can tell them
    # apart. This is the finding that scoped the workstream.
    sa = bp._timing_spread_sentence({"timing_spread": a})
    sb = bp._timing_spread_sentence({"timing_spread": b})
    assert sa and sb and sa != sb, (sa, sb)


# --------------------------------------------------------------------------------------
# 2. Coverage states: empty, single, constant, high-spread.
# --------------------------------------------------------------------------------------

def test_empty_sample_is_unavailable_never_zero():
    s = _spread_for([])
    assert s["coverage"] == "unavailable"
    assert s["n"] == 0
    # NEVER a fabricated zero: no numeric range keys at all.
    for k in ("min_s", "median_s", "max_s"):
        assert k not in s, f"{k} was fabricated on an empty sample: {s}"
    assert s.get("unavailable_reason")
    line = bp._timing_spread_sentence({"timing_spread": s})
    assert "unavailable" in line.lower()
    assert "0s" not in line


def test_single_observation_says_one_observed_run_and_never_a_spread():
    s = _spread_for([240.0])
    assert s["coverage"] == "single_observation"
    assert s["n"] == 1
    assert s["min_s"] == s["median_s"] == s["max_s"] == 240.0
    line = bp._timing_spread_sentence({"timing_spread": s})
    assert "one observed run" in line.lower()
    assert "spread" not in line.lower().replace("not a spread", "")


def test_constant_sample_is_constant_in_the_sample_not_proof_of_no_noise():
    s = _spread_for([120.0] * 12)
    assert s["coverage"] == "constant_in_sample"
    line = bp._timing_spread_sentence({"timing_spread": s}).lower()
    assert "constant in this sample" in line
    # The disclaimer that keeps a constant sample from reading as a promise about CI.
    assert "not proof" in line or "is not proof" in line


def test_high_spread_sample_reports_min_median_max_from_the_observations():
    durs = [30.0, 45.0, 60.0, 300.0, 900.0]
    s = _spread_for(durs)
    assert s["n"] == 5
    assert s["min_s"] == 30.0 and s["max_s"] == 900.0
    assert s["median_s"] == cr._percentile(durs, 50)


# --------------------------------------------------------------------------------------
# 3. Selection identity: runner scope, config era, mode split, excluded/duplicate attempts.
# --------------------------------------------------------------------------------------

def test_runner_mixed_sample_scopes_to_the_dominant_runner_and_says_so():
    """`_critical_path` computes the pole's p50 on its OWN dominant runner. The spread
    must use the SAME population — a blended range would describe a job that never ran."""
    jobs_per_run = (_runs([100.0] * 6, runner="ubuntu-latest")
                    + _runs([900.0, 950.0], runner="macos-14"))
    # distinct ids across the two groups
    for i, run in enumerate(jobs_per_run):
        run[0]["id"] = 500 + i
        run[0]["run_id"] = 60_000 + i
    crit = cr._critical_path(jobs_per_run)
    s = cr._pole_timing_spread(_JOB, _WF, _JOB, {_WF: crit},
                               {_WF: jobs_per_run}, [])
    assert s["selection"]["runner_scope"] == "ubuntu-latest"
    assert s["n"] == 6
    assert s["max_s"] == 100.0, "a macos observation leaked into the ubuntu range"
    assert "macos-14" in (s.get("other_runner_labels") or [])
    line = bp._timing_spread_sentence({"timing_spread": s})
    assert "ubuntu-latest" in line and "macos-14" in line


def test_era_mixed_sample_uses_the_retained_era_and_identifies_it():
    """The spine door drops the other configuration era's runs BEFORE the pole is
    measured. The summary must both exclude them and NAME the era it describes, so its
    range is never read as covering the current configuration when it does not."""
    retained = _runs([200.0, 210.0, 220.0])
    era_facts = [{"workflow_file": _WF, "kept_era": "post",
                  "boundary": "abc1234", "rule": "keep_post",
                  "pre_count": 5, "post_count": 3}]
    crit = cr._critical_path(retained)
    s = cr._pole_timing_spread(_JOB, _WF, _JOB, {_WF: crit},
                               {_WF: retained}, era_facts)
    assert s["n"] == 3
    assert s["selection"]["config_era"] == "post"
    assert "post" in bp._timing_spread_sentence({"timing_spread": s})
    # A workflow with no straddle identifies its era as the whole sampled window,
    # never as a silent "post".
    s2 = _spread_for([200.0, 210.0, 220.0])
    assert s2["selection"]["config_era"] == "all_sampled"


def test_mode_split_is_identified_not_collapsed_into_one_variability_statement():
    """A bimodal pole keeps its existing fast/slow split. The summary reports the whole
    observed range AND identifies the two modes; it must not present one blended number
    as 'the' variability of the check."""
    durs = [30.0] * 6 + [600.0] * 6
    s = _spread_for(durs)
    modes = s.get("modes")
    assert modes and len(modes) == 2, s
    assert [m["n"] for m in modes] == [6, 6]
    assert modes[0]["max_s"] == 30.0 and modes[1]["min_s"] == 600.0
    # The mode split re-uses the engine's OWN bimodality definition, so the summary and
    # the pole's `bimodal` stamp can never disagree about whether this check has modes.
    assert cr._bimodal_split(durs) is not None
    line = bp._timing_spread_sentence({"timing_spread": s}).lower()
    assert "mode" in line


def test_excluded_and_duplicate_attempts_do_not_inflate_n():
    """One eligible observation per sampler-selected run/attempt. A retried run that
    appears twice in the retained list, a skipped job, and a job with unparseable
    timestamps must not each add to `n` — and the rendered values must still be
    re-derivable from the observations that WERE retained."""
    kept = _runs([100.0, 200.0, 300.0])
    dup = copy.deepcopy(kept[1])              # same job id: the same execution, twice
    skipped = [[_job(700, 999.0, conclusion="skipped")]]
    undated = [[{"id": 800, "run_id": 800, "run_attempt": 1, "name": _JOB,
                 "labels": ["ubuntu-latest"], "conclusion": "success",
                 "started_at": None, "completed_at": None}]]
    jobs_per_run = kept + [dup] + skipped + undated
    crit = cr._critical_path(jobs_per_run)
    s = cr._pole_timing_spread(_JOB, _WF, _JOB, {_WF: crit},
                               {_WF: jobs_per_run}, [])
    assert s["n"] == 3, f"n was inflated by an excluded/duplicate attempt: {s}"
    assert (s["min_s"], s["median_s"], s["max_s"]) == (100.0, 200.0, 300.0)
    assert len(s["sample_ids"]) == 3 and len(set(s["sample_ids"])) == 3
    assert s["excluded"]["duplicate"] == 1
    assert s["excluded"]["no_duration"] == 2   # skipped + undated
    # A rerun ATTEMPT is a distinct execution and IS counted — but the summary counts
    # runs/attempts, and never claims they are independent PRs.
    assert "pr" not in bp._timing_spread_sentence({"timing_spread": s}).lower().split()


def test_summary_carries_its_basis_version_and_sample_ids():
    s = _spread_for([100.0, 140.0])
    assert s["version"] == cr._TIMING_SPREAD_VERSION
    assert s["basis"] == cr._TIMING_SPREAD_BASIS
    sel = s["selection"]
    for k in ("check", "workflow_file", "job", "runner_scope", "config_era",
              "attempt_policy", "population"):
        assert sel.get(k), f"selection identifier {k} missing: {sel}"
    assert s["sample_ids"], "no sample ids stamped — the summary is not re-derivable"


def test_a_pole_whose_aggregate_uses_another_population_is_marked_unavailable():
    """When the pole's own p50 did not come from sampled workflow jobs (a PR check-run
    timing), no job durations share its basis. Say unavailable — never attach the
    durations of some other population."""
    s = cr._pole_timing_spread(
        _JOB, _WF, _JOB, {}, {}, [],
        unavailable="pole timing came from PR check-runs")
    assert s["coverage"] == "unavailable"
    assert "min_s" not in s
    assert "check-runs" in s["unavailable_reason"]


# --------------------------------------------------------------------------------------
# 4. Rendering: same summary in the report and the prompt; nothing fabricated; nothing
#    that reads as an inference claim.
# --------------------------------------------------------------------------------------

_LOG = "\n".join([
    " RUN  v4.1.4 /repo/web",
    " Test Files  149 passed (149)",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, "
    "tests 214.54s, environment 8ms)",
])


def _doc(spread: dict | None) -> dict:
    pole: dict = {
        "check": "tests-web", "p50_s": 255.0,
        "workflow_file": _WF, "job": "tests-web",
        "dominant_step": "run tests", "dominant_p50_s": 91.0,
        "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0},
                  {"step": "Build", "category": "build", "p50_s": 60.0}],
    }
    if spread is not None:
        pole["timing_spread"] = spread
    return {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [pole]},
    }


def _render(doc: dict) -> str:
    return bp.render(doc, {"pipeline": _LOG}, {},
                     {"pipeline": "https://github.com/o/r/actions/runs/123"},
                     "2026-06-08")


def test_report_and_prompt_carry_the_same_summary_sentence():
    s = _spread_for([190.0, 205.0, 240.0, 268.0, 331.0])
    md = _render(_doc(s))
    sentence = bp._timing_spread_sentence({"timing_spread": s})
    assert sentence
    # Once in the pole section, once inside the ```text agent prompt fence.
    assert md.count(sentence) >= 2, (
        "the pole section and the agent prompt must carry the SAME summary; found "
        f"{md.count(sentence)} occurrence(s)")
    prompt = md.split("Prompt for your coding agent", 1)[1]
    assert sentence in prompt


def test_legacy_pole_without_a_stamped_summary_renders_no_fabricated_values():
    md = _render(_doc(None))
    assert bp._timing_spread_sentence({}) == ""
    for probe in ("observed run", "comparable sampled runs", "constant in this sample",
                  "duration spread"):
        assert probe not in md.lower(), (
            f"a legacy artifact (no stamped summary) rendered {probe!r}")


def test_a_future_summary_version_is_not_rendered_as_if_it_were_this_one():
    s = _spread_for([190.0, 331.0])
    s["version"] = cr._TIMING_SPREAD_VERSION + 1
    assert bp._timing_spread_sentence({"timing_spread": s}) == ""


def test_the_summary_makes_no_band_detectability_or_significance_claim():
    for durs in ([], [240.0], [100.0] * 12, [1.0] * 9 + [100.0] * 11,
                 [30.0] * 6 + [600.0] * 6):
        line = bp._timing_spread_sentence({"timing_spread": _spread_for(durs)}).lower()
        for banned in ("+/-", "±", "minimum detectable", "detectable effect",
                       "outside noise", "within noise", "statistically",
                       "significant", "confidence", "margin of error", "p-value"):
            assert banned not in line, f"{banned!r} appeared in: {line!r}"


def test_the_summary_never_reaches_the_bottom_line_or_the_savings_numbers():
    """The headline and savings outputs are computed from the sizing stamps, not from
    observed spread. Adding the summary must change ONLY the summary lines."""
    s = _spread_for([190.0, 205.0, 240.0, 268.0, 331.0])
    # The untrusted-log fence carries a fresh random nonce on every render, so two renders
    # of the SAME doc differ on those lines. Normalize it away; it is not our delta.
    def _norm(md: str) -> list[str]:
        return re.sub(r"\[[0-9a-f]{8}\]", "[nonce]", md).splitlines()

    with_s = _norm(_render(_doc(s)))
    without = _norm(_render(_doc(None)))
    sentence = bp._timing_spread_sentence({"timing_spread": s})
    added = [ln for ln in with_s if ln not in without]
    assert added, "the summary rendered nothing at all"
    assert all(sentence in ln for ln in added), (
        f"adding the timing summary changed non-summary lines: "
        f"{[ln for ln in added if sentence not in ln]}")
    removed = [ln for ln in without if ln not in with_s]
    assert not removed, f"adding the timing summary REMOVED report lines: {removed}"


def test_the_gap_fill_prompt_carries_the_same_summary_sentence():
    """A pole that matched no catalog detector hands off an LLM-AUTHORED prompt. That
    body must carry the report's own sentence about what was observed, not the model's
    account of how much the check varies - and not twice if it already quoted it."""
    s = _spread_for([190.0, 205.0, 240.0, 268.0, 331.0])
    pole = {"check": "tests-web", "timing_spread": s}
    sentence = bp._timing_spread_sentence(pole)
    assert sentence
    out = bp._llm_agent_prompt("Root cause: the build re-downloads its toolchain.", pole)
    assert sentence in out
    twice = bp._llm_agent_prompt(
        sentence + "\n\nRoot cause: the build re-downloads its toolchain.", pole)
    assert twice.count(sentence) == 1, "the sentence was doubled in the gap-fill prompt"
