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
import json
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


def _secondary_remote_cache_finding() -> dict:
    return {
        "id": "f-OPT3", "pattern": "OPT3", "pattern_class": "data-driven",
        "severity": "MEDIUM", "title": "Remote cache is read-only",
        "workflow_file": _WF, "line": 12, "affected_jobs": [_CHECK],
        "wall_clock_p50_s": 180.0, "runner_min_saving": 0.0,
        "pr_critical_path_check": _CHECK,
        "evidence": "TURBO_CACHE is remote:ro on pull requests",
    }


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


def test_generic_primary_takes_analysis_when_a_secondary_leaf_matches_the_job():
    doc = _doc()
    doc["findings"].append(_secondary_remote_cache_finding())
    assert bp._data_driven_for_pole(doc["pr_critical_path"]["poles"][0], doc["findings"])

    md = _render(doc, _ANALYSIS)

    assert "🤖 LLM root-cause analysis" in md
    assert "SENTINEL-GAP-FILL-PROMPT" in md
    assert "no drill-down available" not in md
    vr = _load_verify_report()
    failures = [c for c in vr.run_checks(md, None, None, None)
                if not c.ok and not c.skipped and c.name !=
                "report's skill commit is HEAD or an ancestor of it (provenance)"]
    assert failures == []


def test_generic_primary_takes_analysis_beside_an_offcategory_log_leaf():
    doc = _doc()
    log = _LOG + "\n$ eslint . --ext .ts,.tsx,.js\n"
    pole = doc["pr_critical_path"]["poles"][0]
    parsed = bp._parse_log(log)
    assert parsed and parsed["fix_key"] == "eslint-no-cache"
    kept, secondary = bp._demote_offcategory_leaf(parsed, pole)
    assert kept is None and secondary is not None

    md = bp.render(doc, {_KEY: log}, {}, {_KEY: _RUN_URL}, "2026-11-03",
                   {_KEY: _TIMELINE}, analyses={_KEY: _ANALYSIS})

    assert "ci-speedup:offcategory-leaf" in md
    assert "🤖 LLM root-cause analysis" in md
    assert "SENTINEL-GAP-FILL-PROMPT" in md
    assert "no drill-down available" not in md


def test_declined_analysis_is_loud_and_recorded_in_findings(capsys):
    doc = _doc("OPT72")

    _render(doc, _ANALYSIS)

    assert "--analysis ci declined" in capsys.readouterr().err
    assert doc["analysis_dispositions"] == [{
        "key": _KEY,
        "check": _CHECK,
        "status": "declined",
        "reason": "a specific structural catalog pattern already names the root cause",
    }]


def _run_cli(tmp_path, doc: dict, analyses: dict[str, object], *,
             logs: dict[str, str] | None = None) -> tuple[bytes, str]:
    findings = tmp_path / "findings.json"
    report = tmp_path / "report.md"
    findings.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    before = findings.read_bytes()
    argv = ["--in", str(findings), "--out", str(report)]
    for key, payload in analyses.items():
        path = tmp_path / f"{key}-analysis.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        argv += ["--analysis", f"{key}={path}"]
    for key, text in (logs or {}).items():
        path = tmp_path / f"{key}-job.log"
        path.write_text(text, encoding="utf-8")
        argv += ["--log", f"{key}={path}"]

    assert bp.main(argv) == 0
    return before, findings.read_bytes()


def test_cli_never_rewrites_findings_when_reporting_a_declined_analysis(tmp_path, capsys):
    before, after = _run_cli(tmp_path, _doc("OPT72"), {_KEY: _ANALYSIS},
                             logs={_KEY: _LOG})

    assert after == before
    err = capsys.readouterr().err
    assert "--analysis ci declined" in err
    assert "specific structural catalog pattern" in err


def test_cli_reports_null_invalid_and_unbound_analysis_inputs(tmp_path, capsys):
    null_dir = tmp_path / "null"
    invalid_dir = tmp_path / "invalid"
    unbound_dir = tmp_path / "unbound"
    null_dir.mkdir()
    invalid_dir.mkdir()
    unbound_dir.mkdir()
    null_before, null_after = _run_cli(
        null_dir, _doc(), {_KEY: None}, logs={_KEY: _LOG})
    invalid_before, invalid_after = _run_cli(
        invalid_dir, _doc(), {_KEY: ["not", "an", "object"]}, logs={_KEY: _LOG})
    unbound_before, unbound_after = _run_cli(
        unbound_dir, _doc(), {"unbound": _ANALYSIS}, logs={_KEY: _LOG})

    assert null_after == null_before
    assert invalid_after == invalid_before
    assert unbound_after == unbound_before
    err = capsys.readouterr().err
    assert "--analysis ci declined" in err and "not an object" in err
    assert "--analysis unbound declined" in err and "no rendered pole matched" in err


def test_cli_reports_analysis_for_a_pole_below_the_render_cap(tmp_path, capsys):
    doc = _doc()
    doc["pr_critical_path"]["poles"] = []
    doc["findings"] = []
    for index in range(6):
        check = f"job-{index}"
        workflow = f".github/workflows/w{index}.yml"
        doc["pr_critical_path"]["poles"].append({
            "check": check, "p50_s": float(600 - index * 10),
            "workflow_file": workflow, "job": check,
            "dominant_step": "work", "dominant_p50_s": float(500 - index * 10),
            "steps": [{"step": "work", "category": "test",
                       "p50_s": float(500 - index * 10)}],
        })

    before, after = _run_cli(tmp_path, doc, {"w5": _ANALYSIS}, logs={"w5": _LOG})

    assert after == before
    assert "--analysis w5 declined: no rendered pole matched" in capsys.readouterr().err


def test_cli_reports_syntactically_invalid_analysis_without_touching_input(tmp_path, capsys):
    findings = tmp_path / "findings.json"
    analysis = tmp_path / "analysis.json"
    report = tmp_path / "report.md"
    findings.write_text(json.dumps(_doc(), indent=2) + "\n", encoding="utf-8")
    before = findings.read_bytes()
    analysis.write_text("{not-json", encoding="utf-8")

    assert bp.main([
        "--in", str(findings), "--out", str(report),
        "--analysis", f"{_KEY}={analysis}",
    ]) == 0

    assert findings.read_bytes() == before
    err = capsys.readouterr().err
    assert "--analysis ci declined" in err
    assert "could not be read or parsed" in err


def _aggregation_gate_doc() -> dict:
    wf = ".github/workflows/aggregate.yml"
    return {
        "repo": "o/r", "scanned_at": "2026-11-03T00:00:00Z", "findings": [],
        "data_bundle": {"logs": [{"check": "gate", "workflow_file": wf}]},
        "data_sources": {"runs_sampled": 20, "jobs_sampled": 40,
                         "workflows_analyzed": 1},
        "workflow_job_graph": {wf: {
            "build": {"name": "build", "needs": []},
            "lint": {"name": "lint", "needs": []},
            "gate": {"name": "gate", "needs": ["build", "lint"]},
        }},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "check_present_n_pr": 20, "critical_path_check": "build",
            "poles": [
                {"check": "build", "p50_s": 120.0, "workflow_file": wf,
                 "job": "build", "dominant_step": "compile", "dominant_p50_s": 100.0,
                 "steps": [{"step": "compile", "category": "build", "p50_s": 100.0}]},
                {"check": "gate", "p50_s": 3.0, "workflow_file": wf,
                 "job": "gate", "steps": []},
            ],
            "checks": [
                {"name": "build", "p50_s": 120.0, "present_on": 20,
                 "workflow_file": wf},
                {"name": "lint", "p50_s": 60.0, "present_on": 20,
                 "workflow_file": wf},
                {"name": "gate", "p50_s": 3.0, "present_on": 20,
                 "workflow_file": wf},
            ],
            "populations": [],
        },
    }


def test_aggregation_gate_analysis_is_declined_not_recorded_as_rendered(capsys):
    doc = _aggregation_gate_doc()

    md = bp.render(doc, {"aggregate": _LOG}, analyses={"aggregate": _ANALYSIS})

    gate = md[md.index("▸ `gate`"):]
    assert "**Aggregation gate" in gate
    assert "🤖 LLM root-cause analysis" not in gate
    assert doc["analysis_dispositions"][-1] == {
        "key": "aggregate", "check": "gate", "status": "declined",
        "reason": "the pole is an aggregation gate with no analysis block",
    }
    assert "--analysis aggregate declined" in capsys.readouterr().err


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


def test_prompt_only_analysis_does_not_hide_unfilled_root_cause():
    report = _render(_doc(), {"prompt": "Inspect the captured build step."})
    assert "🤖 LLM root-cause analysis" not in report
    assert "no drill-down available; this is a coverage gap" in report
    assert _no_root_cause_failures(report)


@pytest.mark.parametrize("missing", ["cause", "evidence", "prompt"])
def test_ungrounded_analysis_does_not_count_as_gap_filled(missing):
    analysis = {
        "cause": "The build step performs redundant work.",
        "prompt": "Inspect the captured build step.",
        "evidence": ["Build step output"],
    }
    analysis.pop(missing)
    report = _render(_doc(), analysis)
    assert "🤖 LLM root-cause analysis" not in report
    assert "no drill-down available; this is a coverage gap" in report
    assert _no_root_cause_failures(report)
