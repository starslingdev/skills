"""Regression: a triaged-fast workflow must never leak into the critical-path poles.

A workflow whose slowest sampled run finishes under `_TRIAGE_WALLCLOCK_FLOOR_S` is
TRIAGED — its per-run job fetch is skipped and it is disclosed in
`data_sources.triaged_fast_workflows` as "can't hold the merge pole". But its check-run
still rides along on the sampled PRs, so its check-run p50 lands in `pr_checks_tuple` and
could rank into the structural top-N. Because its jobs were never fetched, decomposing it
yields a BARE pole (no `dominant_step` / `steps`) that renders "no captured log" + "NO
CATALOG PATTERN MATCHED" — directly contradicting the report's own triage coverage note
(seen on alwaysmeticulous/report-diffs-action, `test-meticulous-upload-container.yaml`).

Two layers, both pinned here:
  - ENGINE: `collect_runs._structural_pole_candidates` drops a triaged workflow's check
    from the decomposed pole set (it stays on the `checks` spine, never a drilled pole).
  - INVARIANT (class guard): `verify_report.check_speed_poles_complete` re-derives the
    contradiction from the findings JSON (a triaged `workflow_file` appearing in
    `pr_critical_path.poles`) and FAILS, so any future repo that regrows the bug is caught.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_triaged_pole_leak.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import collect_runs as cr  # uniquely-named module; on pythonpath via pyproject

_SKILL_DIR = Path(__file__).resolve().parents[1]
_VERIFY = _SKILL_DIR / "tests" / "verify_report.py"


def _load_verify_report():
    # By-path load under a unique name: ci-secure also ships a tests/verify_report.py, so a
    # plain `import verify_report` can bind the wrong module on the shared pytest pythonpath.
    spec = importlib.util.spec_from_file_location(
        "ci_speedup_verify_report_triaged", _VERIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_speedup_verify_report_triaged"] = mod
    spec.loader.exec_module(mod)
    return mod


# A triaged workflow whose check-run ranks ABOVE a real, drillable gate. The triaged
# workflow carries an empty `job_p50` (its jobs were never fetched — the triage stub), so
# the timing mapper misses it; the scanned job graph still ties its check to its file.
_CRIT_BY_WF = {
    "tw.yml": {"long_pole_job": "", "long_pole_p50": 0.0, "floor_p50": 0.0,
               "job_p50": {}, "concurrent_wall_p50": 52.0},
    "rw.yml": {"long_pole_job": "Build", "long_pole_p50": 300.0, "floor_p50": 300.0,
               "job_p50": {"Build": 300.0}},
}
_JOB_GRAPH = {
    "tw.yml": {"report": {"name": "Report diffs"}},
    "rw.yml": {"build": {"name": "Build"}},
}
# Ranked typical-first; the fast triaged check sits ABOVE the real gate to prove the
# exclusion is by workflow-triage, not by rank.
_PR_CHECKS_TUPLE = (("Report diffs", 52.5), ("Build", 300.0))


def test_structural_pole_candidates_excludes_triaged_workflow():
    out = cr._structural_pole_candidates(
        _PR_CHECKS_TUPLE, _CRIT_BY_WF, _JOB_GRAPH,
        triaged_fast_workflows=["tw.yml"], top_n=5)
    names = [n for n, _ in out]
    assert "Report diffs" not in names, (
        "triaged-fast workflow tw.yml leaked into the structural poles as a bare, "
        f"undrillable lever (got {names})")
    assert "Build" in names, "the real drillable gate must still be a pole"


def test_structural_pole_candidates_keeps_check_when_not_triaged():
    # Same inputs, but nothing is triaged → the (now drillable) check stays.
    out = cr._structural_pole_candidates(
        _PR_CHECKS_TUPLE, _CRIT_BY_WF, _JOB_GRAPH,
        triaged_fast_workflows=[], top_n=5)
    assert "Report diffs" in [n for n, _ in out]


def _bad_findings() -> dict:
    """A findings doc that exhibits the contradiction: tw.yml is disclosed as triaged-fast
    yet still appears as a decomposed pole with no dominant_step (a bare pole)."""
    return {
        "data_sources": {"triaged_fast_workflows": ["tw.yml"], "triaged_fast_count": 1},
        "pr_critical_path": {
            "critical_path_check": "Build",
            "poles": [
                {"check": "Build", "p50_s": 300.0, "workflow_file": "rw.yml",
                 "job": "Build", "dominant_step": "compile", "steps": [
                     {"step": "compile", "category": "build", "p50_s": 280.0}]},
                # The leaked bare pole — workflow_file is the triaged file, no steps.
                {"check": "Report diffs", "p50_s": 52.5, "workflow_file": "tw.yml",
                 "job": "report"},
            ],
        },
    }


def _clean_findings() -> dict:
    f = _bad_findings()
    # Drop the leaked bare pole — the triaged workflow stays disclosed, off the poles.
    f["pr_critical_path"]["poles"] = f["pr_critical_path"]["poles"][:1]
    return f


def test_invariant_flags_triaged_pole_offender(tmp_path: Path):
    vr = _load_verify_report()
    fp = tmp_path / "findings.json"
    fp.write_text(json.dumps(_bad_findings()), encoding="utf-8")
    offenders = vr._triaged_pole_offenders(fp)
    assert offenders, "the triaged-fast pole contradiction must be flagged"
    assert any("tw.yml" in o for o in offenders)


def test_invariant_clean_when_triaged_workflow_off_the_poles(tmp_path: Path):
    vr = _load_verify_report()
    fp = tmp_path / "findings.json"
    fp.write_text(json.dumps(_clean_findings()), encoding="utf-8")
    assert vr._triaged_pole_offenders(fp) == []


def test_check_speed_poles_complete_fails_on_triaged_pole(tmp_path: Path):
    vr = _load_verify_report()
    fp = tmp_path / "findings.json"
    fp.write_text(json.dumps(_bad_findings()), encoding="utf-8")
    # A WELL-FORMED report that drills BOTH poles each with a prompt — so the count /
    # prompt halves of this check PASS and the ONLY thing that can fail it is the
    # findings-internal triaged-fast contradiction (the exact real-world shape: the bare
    # pole still ships a generic agent prompt, which is why the old symmetric-pole gate
    # waved it through).
    report = (
        "# report\n\n"
        "## Long pole 1: Build\nPrompt for your coding agent\n\n"
        "## Long pole 2: Report diffs\nPrompt for your coding agent\n")
    chk = vr.check_speed_poles_complete(report, fp)
    assert chk.ok is False
    assert "triaged-fast" in chk.detail


# --- headline-crown recovery (paradedb/paradedb class) ------------------------
# The CROWN analog: when the crown (`critical_path_check` = the slowest TYPICAL check) falls to a
# sub-floor lint whose workflow was triaged, the headline pole dead-ends. `_crown_recovery_wf`
# picks that workflow for a one-shot job-fetch so the headline is drillable; the poles-keyed
# exclusion (above) alone doesn't help because the crown lives on the `checks` spine, not `poles`.
def test_crown_recovery_wf_selects_a_triaged_crown_with_retained_runs():
    # Crown resolves (via the scanned graph, since its jobs weren't fetched) to the triaged tw.yml,
    # which has retained runs → recover it.
    assert cr._crown_recovery_wf(
        "Report diffs", ["tw.yml"], _JOB_GRAPH, {"tw.yml": [{"id": 1}]}) == "tw.yml"


def test_crown_recovery_wf_none_when_crown_not_triaged():
    # Crown maps to the already-drillable rw.yml (not triaged) → nothing to recover.
    assert cr._crown_recovery_wf(
        "Build", ["tw.yml"], _JOB_GRAPH, {"tw.yml": [{"id": 1}]}) is None


def test_crown_recovery_wf_none_without_retained_runs():
    # Triaged crown but no stashed runs to fetch (a total triage-stash miss) → can't recover here.
    assert cr._crown_recovery_wf("Report diffs", ["tw.yml"], _JOB_GRAPH, {}) is None


def test_crown_recovery_wf_none_when_stash_entry_is_empty():
    # Boundary: the stash KEY is present but its run list is empty (`{tw.yml: []}`). The contract
    # is "has retained runs to fetch" — an empty list is nothing to job-fetch, so the crown can't
    # be made drillable and the helper must return None (not the workflow).
    assert cr._crown_recovery_wf("Report diffs", ["tw.yml"], _JOB_GRAPH, {"tw.yml": []}) is None


def test_crown_recovery_wf_none_for_fileless_crown():
    # A fileless/external crown (no scanned job maps it) → no workflow to recover.
    assert cr._crown_recovery_wf(
        "Some Review Bot", ["tw.yml"], _JOB_GRAPH, {"tw.yml": [{"id": 1}]}) is None


def test_crown_recovery_wf_none_on_empty_crown():
    assert cr._crown_recovery_wf(None, ["tw.yml"], _JOB_GRAPH, {"tw.yml": [{"id": 1}]}) is None
