"""A pole whose only catalog match is a GENERIC structural pattern is a gap-fill target.

Regression (dogfood 2026-11-03, starslingdev/skills): the top pole `test (self-hosted)`
(2m 06s, 79% in `Run tests`) matched only OPT75, the catch-all "the long pole's time is
one addressable step". OPT75 names no cause INSIDE the step, yet because a catalog
pattern matched, the "no drill-down available" marker never rendered, phase 4a never
read the log, and the report shipped "no known root-cause pattern matched" for its
biggest lever while the log named the cause (`pytest -v -n 4 --dist worksteal`,
`9504 passed ... in 98.43s`, 13s from step start to `test session starts`).

Two halves, both pinned here:
- engine: an OPT75-only (or OPT71-only) pole with a captured log is a coverage gap. It
  renders the phase-4a trigger, is listed by `_gap_poles`, and takes `--analysis`;
  a specific structural pattern (OPT72) still covers its pole.
- verify: a pole that says "no known root-cause pattern matched" without a
  `🤖 LLM root-cause analysis` block fails the report.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)


def _load_verify_report():
    # `verify_report` is not a unique module name (ci-secure ships one too): load by path.
    path = _SKILL / "tests" / "verify_report.py"
    name = "ci_speedup_verify_report_generic_gap"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register first: its @dataclass resolves __module__ here
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_WF = ".github/workflows/ci.yml"
_CHECK = "test (self-hosted)"
_JOB_URL = "https://github.com/o/r/actions/runs/41727873262/job/126237287954"
_RUN_URL = "https://github.com/o/r/actions/runs/41727873262"
# `ci` uniquely owns the one pole (its workflow basename is `ci.yml`), as the render
# command's owner key would.
_KEY = "ci"

# Trimmed, verbatim excerpt of the drilled job's log (the dogfood evidence).
_LOG = "\n".join([
    "2026-11-03T22:07:22.2925862Z ##[group]Run pip install -e \".[dev]\"",
    "2026-11-03T22:07:25.6785365Z ##[group]Run pytest -v -n 4 --dist worksteal",
    "2026-11-03T22:07:25.6785887Z pytest -v -n 4 --dist worksteal",
    "2026-11-03T22:07:25.6816104Z shell: /usr/bin/bash -e {0}",
    "2026-11-03T22:07:25.6818076Z ##[endgroup]",
    "2026-11-03T22:07:38.8129321Z ============================= test session starts "
    "==============================",
    "2026-11-03T22:07:38.8138889Z created: 4/4 workers",
    "2026-11-03T22:07:38.8139256Z 4 workers [9538 items]",
    "2026-11-03T22:09:04.6633249Z = 9504 passed, 33 skipped, 1 xfailed, 6 warnings, "
    "155 subtests passed in 98.43s (0:01:38) =",
])

_TIMELINE = {
    "job": _CHECK, "run_url": _RUN_URL, "job_url": _JOB_URL, "job_dur_s": 126.0,
    "steps": [
        {"name": "Set up job", "number": 1, "start_s": 1.0, "dur_s": 0.0},
        {"name": "Checkout", "number": 3, "start_s": 2.0, "dur_s": 16.0},
        {"name": "Install test dependencies", "number": 6, "start_s": 19.0, "dur_s": 3.0},
        {"name": "Run tests", "number": 7, "start_s": 22.0, "dur_s": 99.0},
        {"name": "Complete job", "number": 15, "start_s": 122.0, "dur_s": 0.0},
    ],
}

_ANALYSIS = {
    "cause": ("`Run tests` is one pytest-xdist run over the whole suite on 4 workers: "
              "about 13s of collection before the session starts, then 98.43s of tests."),
    "breakdown": [["collection and worker start-up", "~13s"],
                  ["9504 tests on 4 xdist workers", "98.43s"]],
    "evidence": [
        "pytest -v -n 4 --dist worksteal",
        "4 workers [9538 items]",
        "= 9504 passed, 33 skipped, 1 xfailed, 6 warnings, 155 subtests passed in "
        "98.43s (0:01:38) =",
    ],
    "prompt": "SENTINEL-GAP-FILL-PROMPT: check whether more xdist workers fit the runner.",
}


def _structural(pattern: str) -> dict:
    return {"id": f"f-{pattern}", "pattern": pattern, "pattern_class": "structural",
            "structural": True, "severity": "HIGH", "risk": "MEDIUM",
            "title": f"{pattern} title", "workflow_file": _WF, "line": 0,
            "affected_jobs": [_CHECK], "fix_recipe_anchor": f"{pattern.lower()}-anchor",
            "guardrail": "GUARDRAIL-SENTINEL", "rollout": "ROLLOUT-SENTINEL",
            "failure_mode": "FAILURE-MODE-SENTINEL",
            "evidence": "dominant step Run tests (test, 78%)"}


def _doc(pattern: str = "OPT75") -> dict:
    return {
        "repo": "o/r", "scanned_at": "2026-11-03T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 3},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": _CHECK, "p50_s": 126.0, "workflow_file": _WF, "job": _CHECK,
                "dominant_step": "Run tests", "dominant_p50_s": 99.0,
                "dominant_category": "test",
                "steps": [{"step": "Run tests", "category": "test", "p50_s": 99.0},
                          {"step": "Checkout", "category": "checkout", "p50_s": 16.0}],
            }]},
        "findings": [_structural(pattern)],
    }


def _render(doc: dict, analysis: dict | None = None) -> str:
    return bp.render(doc, {_KEY: _LOG}, {}, {_KEY: _RUN_URL}, "2026-11-03",
                     {_KEY: _TIMELINE},
                     analyses=({_KEY: analysis} if analysis else None))


def test_log_excerpt_matches_no_log_level_detector():
    # Precondition: the pole's only catalog match is the structural one.
    assert bp._catalog_leaf(bp._parse_log(_LOG)) is None


@pytest.mark.parametrize("pattern", ["OPT75", "OPT71"])
def test_generic_only_pole_is_listed_as_a_gap(pattern):
    gaps = bp._gap_poles(_doc(pattern), {_KEY: _LOG})
    assert [(p.get("check"), key) for p, _wf, _log, key in gaps] == [(_CHECK, _KEY)]


@pytest.mark.parametrize("pattern", ["OPT75", "OPT71"])
def test_generic_only_pole_renders_the_gap_fill_trigger(pattern):
    md = _render(_doc(pattern))
    # The phase-4a trigger renders, so the agent reads the log...
    assert "no drill-down available; this is a coverage gap" in md
    # ...while the generic lever itself still renders with its risk axis.
    assert f"📐 Structural root-cause - {pattern}" in md
    assert "GUARDRAIL-SENTINEL" in md


@pytest.mark.parametrize("pattern", ["OPT75", "OPT71"])
def test_generic_only_pole_takes_the_gap_fill_analysis(pattern):
    md = _render(_doc(pattern), _ANALYSIS)
    assert "🤖 LLM root-cause analysis" in md
    assert "98.43s (0:01:38)" in md                 # the quoted evidence renders
    assert "SENTINEL-GAP-FILL-PROMPT" in md          # the tailored prompt replaces the generic one
    assert "no drill-down available" not in md      # filled, so no dead-end
    assert f"📐 Structural root-cause - {pattern}" in md  # the lever is not dropped
    # The filled pole's audit line no longer claims nothing is known about the step.
    assert "no known root-cause pattern matched" not in md


def test_specific_structural_pattern_still_covers_its_pole():
    # Control: OPT72 names a cause inside the job (setup dwarfs the payload), so it is a
    # catalog match, not a gap, and pulls no gap-fill.
    doc = _doc("OPT72")
    assert bp._gap_poles(doc, {_KEY: _LOG}) == []
    md = _render(doc, _ANALYSIS)
    assert "no drill-down available" not in md
    assert "🤖 LLM root-cause analysis" not in md
    assert "structural catalog pattern" in md
    # And its audit line does not say no pattern matched (it did).
    assert "no known root-cause pattern matched" not in md


# --------------------------------------------------------------------------- #
# verify_report: "no known root-cause pattern matched" needs an analysis
# --------------------------------------------------------------------------- #

_POLE_HEAD = "## 🟠 Long pole 1: `ci.yml` ▸ `test (self-hosted)` - 2m 06s"
_AUDIT_NO_ROOT_CAUSE = (
    "**🔗 Audit:** run [41727873262](" + _RUN_URL + ") → [the `test (self-hosted)` job]("
    + _JOB_URL + ") → [the `Run tests` step](" + _JOB_URL + "#step:7:1) - open the step "
    "to inspect its log directly (no known root-cause pattern matched, so there is no "
    "specific callout to search for).")
_ANALYSIS_LEAD = ("**🤖 LLM root-cause analysis** - only a generic structural pattern "
                  "matched this job, so the analysis below is the skill's LLM reading the "
                  "captured job log.")


def _report(*pole_lines: str) -> str:
    return "\n".join(["# o/r - why is the merge slow?", "", '<a id="pole-1"></a>', "",
                      _POLE_HEAD, "", *pole_lines, "", "## Data sources", "", "x", ""])


def _no_root_cause_failures(report: str) -> list:
    vr = _load_verify_report()
    return [c for c in vr.run_checks(report, None, None, None)
            if not c.ok and not c.skipped
            and "no known root-cause pattern matched" in c.detail]


def test_verify_fails_pole_with_no_root_cause_and_no_analysis():
    failures = _no_root_cause_failures(_report(_AUDIT_NO_ROOT_CAUSE))
    assert failures, ("verify_report passed a pole that says 'no known root-cause "
                      "pattern matched' with no gap-fill analysis attached")
    assert "test (self-hosted)" in failures[0].detail


def test_verify_fails_the_waterfall_wording_too():
    failures = _no_root_cause_failures(_report(
        "```text", "   Run tests  ████  1m 39s  79%", "",
        "   (captured this job's log but matched no known root-cause pattern.)", "```"))
    assert failures


def test_verify_passes_pole_with_no_root_cause_once_analysis_attached():
    assert _no_root_cause_failures(
        _report(_AUDIT_NO_ROOT_CAUSE, "", _ANALYSIS_LEAD)) == []
