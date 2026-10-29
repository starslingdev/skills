"""Tests for scripts/summary.py — the agent-facing data-pass digest + render command.

The summary exists so the orchestrator acts on ONE structured block instead of
hand-spelunking findings.json, re-probing gh for gating, or reading blocking_path.py
source to reconstruct the render invocation. These lock the three guarantees:
the gating "don't re-probe" note, the fileless auto-demote flag, matrix-collapsed
poles, and — critically — that every emitted render KEY binds back to its own pole
via blocking_path._match_key (the actual matcher the renderer uses)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

# The public repo ships no committed worked-example corpus (legacy reports/ is not
# published; fresh examples come from a validation run). Corpus-dependent guards
# skip LOUDLY when it's absent, never pass vacuously — and run again the moment a
# corpus reappears (a generated examples/ report, or in the internal development repo).
_NO_CORPUS_REASON = ("no committed report corpora in this repo — corpus guards run "
                     "against generated reports / in the internal development repo")

from summary import (  # noqa: E402
    build_summary, render_command, _collapse_poles, _render_keys,
    _tier2_promoted as summary_tier2_promoted,
    funnel_reason_chain, empty_spine_diagnostics, _break_is_transient,
    _rerun_command, _total_30d_volume, _ACTIVE_30D_RUNS, _durable_hint,
)
import blocking_path as bp_mod  # noqa: E402
from blocking_path import (  # noqa: E402
    _match_key,
    _tier2_source_backed_ranked as renderer_tier2_source_backed_ranked,
)

_SKILL_DIR = Path(__file__).resolve().parent.parent
_REPORTS_DIR = _SKILL_DIR / "reports"


def _doc(**over):
    doc = {
        "scanned_at": "2026-06-11T07:10:57+00:00",
        "required_checks": None,
        "required_checks_complete": False,
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "sample_fetch_failures": 0,
            "critical_path_check": "Claude Code Review", "critical_path_s": 1865.0,
            "poles": [
                {"check": "Claude Code Review", "p50_s": 1865.0},  # fileless: no workflow_file
                {"check": "tests-web (node24, pg15, mode)", "p50_s": 223.5,
                 "workflow_file": ".github/workflows/pipeline.yml"},
                {"check": "tests-web (node24, pg12, mode)", "p50_s": 215.0,
                 "workflow_file": ".github/workflows/pipeline.yml"},
                {"check": "e2e-tests", "p50_s": 191.5,
                 "workflow_file": ".github/workflows/pipeline.yml"},
            ],
        },
        "data_bundle": {
            "logs_dir": "/tmp/x.data",
            "logs": [
                {"check": "tests-web (node24, pg15, mode)",
                 "workflow_file": ".github/workflows/pipeline.yml", "duration_s": 217.0,
                 "file": "tests-web-1.log", "steps_file": "tests-web-1.steps.json",
                 "mag_file": "tests-web-1.mag.json"},
                {"check": "e2e-tests", "workflow_file": ".github/workflows/pipeline.yml",
                 "duration_s": 182.0, "file": "e2e-2.log", "steps_file": "e2e-2.steps.json",
                 "mag_file": "e2e-2.mag.json"},
            ],
        },
        "findings": [
            {"id": "f87", "pattern": "OPT70", "risk": "HIGH", "title": "Scope build to changed",
             "workflow_file": ".github/workflows/pipeline.yml"},
            {"id": "f88", "pattern": "OPT70", "risk": "HIGH", "title": "Scope build to changed",
             "workflow_file": ".github/workflows/pipeline.yml"},
            {"id": "f89", "pattern": "OPT72", "risk": "MEDIUM", "title": "Mostly setup"},
            {"id": "h1", "pattern": "OPT33", "title": "draft gating"},
            {"id": "h2", "pattern": "OPT5", "title": "pnpm cache"},
        ],
    }
    doc.update(over)
    return doc


def _add_tier2_spine(doc):
    rows = []
    for f in doc.get("findings") or []:
        if f.get("sizing_basis") != "measured" or not f.get("tier2_neutrality"):
            continue
        wf = str(f.get("workflow_file") or "")
        if not wf:
            continue
        saving = float(f.get("runner_min_saving") or 0.0)
        rows.append({
            "workflow_file": wf,
            "job_name": f"source {f.get('id')}",
            "runner_label": "ubuntu-latest",
            "event_scope": "all-events",
            "status_filter": "success",
            "attempt_filter": "latest",
            "volume_filter": "all-status",
            "raw_compute_runner_min_per_month": saving + 10.0,
            "billable_equiv_min_per_month": saving + 20.0,
            "share_of_all_row_total": 0.0,
        })
    total_billable = sum(r["billable_equiv_min_per_month"] for r in rows)
    for row in rows:
        row["share_of_all_row_total"] = (
            round(row["billable_equiv_min_per_month"] / total_billable, 3)
            if total_billable else 0.0)
    doc["runner_minute_spine"] = {
        "render_ready": True,
        "rows": rows,
        "totals": {
            "row_count": len(rows),
            "raw_compute_runner_min_per_month": sum(
                r["raw_compute_runner_min_per_month"] for r in rows),
            "billable_equiv_min_per_month": total_billable,
            "percentage_denominator": "all_rows_billable_equiv_min_per_month",
        },
    }
    return doc


def test_gating_note_says_dont_reprobe_when_none_readable():
    s = build_summary(_doc())
    assert "do NOT re-probe gh" in s
    assert "rulesets + branch protection" in s


def test_gating_note_lists_required_checks_when_present():
    s = build_summary(_doc(required_checks=["ci / build", "ci / test"],
                           required_checks_complete=True))
    assert "ci / build" in s and "ci / test" in s
    assert "do NOT re-probe gh" not in s


def test_pr_floor_fallback_note_states_the_demotion():
    # External-gate repo: gate_kind=pr_floor_fallback. The summary must state, in one
    # line, that the spine fell back to the measured PR-floor (so the agent reports the
    # demotion instead of reverse-engineering it from a 0/external sample) and must NOT
    # tell the agent to hand-write or re-probe.
    s = build_summary(_doc(
        required_checks=["Enterprise CI/tests", "cla/mattermost"],
        required_checks_complete=True,
        pr_critical_path={
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "sample_fetch_failures": 0, "required_suite_unsatisfiable": True,
            "gate_kind": "pr_floor_fallback",
            "poles": [{"check": "server-ci", "p50_s": 628.0,
                       "workflow_file": ".github/workflows/server-ci.yml",
                       "pr_floor_fallback": True}],
        }))
    assert "PR-FLOOR FALLBACK" in s
    assert "measured PR-floor" in s
    assert "not the branch-protection gate" in s
    assert "do NOT re-probe gh or hand-write it" in s
    # The recency-only fallback scope is surfaced on the sample line, not just a count.
    assert "recency-only (external required suite, no PR carried it)" in s


def test_fileless_check_flagged_as_auto_demoted():
    # A fileless/managed pole that is NOT the headline (critical_path_check is a
    # file-backed check) IS auto-demoted by the renderer — the summary says so.
    s = build_summary(_doc(pr_critical_path={
        "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
        "sample_fetch_failures": 0,
        "critical_path_check": "tests-web (node24, pg15, mode)", "critical_path_s": 223.5,
        "poles": [
            {"check": "tests-web (node24, pg15, mode)", "p50_s": 223.5,
             "workflow_file": ".github/workflows/pipeline.yml"},
            {"check": "Claude Code Review", "p50_s": 90.0},  # fileless, below the headline
        ],
    }))
    assert "Fileless/managed check" in s
    assert "Claude Code Review" in s
    assert "Don't investigate its gating manually" in s
    assert "auto-demotes it from the headline" in s


def test_managed_headline_check_not_claimed_auto_demoted():
    # RevenueCat/purchases-ios regression: provenance=='unresolved', required_checks==null,
    # and the managed/fileless check ("Claude Code Review" in the fixture, "Size Analysis |
    # Emerge" in the wild) is BOTH critical_path_check and the rendered headline. The
    # renderer does NOT demote it (nothing file-backed to promote in its place), so the
    # summary must not promise auto-demotion — that would contradict the rendered report.
    s = build_summary(_doc())  # critical_path_check == the fileless "Claude Code Review"
    assert "auto-demotes it from the headline" not in s
    assert "Don't investigate its gating manually" not in s
    # It is still flagged (not a tunable lever) but truthfully called out as the headline.
    assert "HEADLINES the report" in s
    assert "Claude Code Review" in s


def test_multiple_fileless_poles_split_headline_vs_demoted():
    # Two managed/fileless poles in ONE report (e.g. an Emerge size check + an AI-review
    # bot): the slower is critical_path_check (→ HEADLINE), the other is below it (→ demote).
    # The per-pole loop must emit BOTH messages — a regression that hoisted the headline
    # comparison out of the loop ("if ANY fileless pole is the headline, treat all as
    # headline") would pass every single-pole test but be caught here.
    s = build_summary(_doc(pr_critical_path={
        "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
        "sample_fetch_failures": 0,
        "critical_path_check": "Size Analysis | Emerge", "critical_path_s": 600.0,
        "poles": [
            {"check": "Size Analysis | Emerge", "p50_s": 600.0},   # fileless, IS the headline
            {"check": "Claude Code Review", "p50_s": 90.0},        # fileless, below the headline
        ],
    }))
    # The headline managed check is called out as the headline, not auto-demoted.
    assert "Size Analysis | Emerge" in s
    assert "HEADLINES the report" in s
    # The second managed check IS auto-demoted (something else — the first managed check —
    # out-gates it), so the demote message is present too.
    assert "Claude Code Review" in s
    assert "auto-demotes it from the headline" in s


def test_job_backed_ambiguous_pole_renders_honest_not_fileless():
    # Issue #118 honest-labeling arm (reth's live shape): `test / ethereum` carries REAL
    # developer job timing (P50 13m20s) but maps to no single workflow file because BOTH
    # unit.yml and integration.yml produce it under the same check name (matrix-leg
    # collision). The `ambiguous_workflows` stamp flips the framing: it is a real CI job to
    # investigate (rename to disambiguate), NOT a fileless gate to ignore.
    s = build_summary(_doc(pr_critical_path={
        "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
        "sample_fetch_failures": 0,
        "critical_path_check": "crate-checks (2/3)", "critical_path_s": 900.0,
        "poles": [
            {"check": "crate-checks (2/3)", "p50_s": 900.0,
             "workflow_file": ".github/workflows/lint.yml", "job": "crate-checks (2/3)"},
            {"check": "test / ethereum", "p50_s": 800.0, "timing_source": "workflow_jobs",
             "ambiguous_workflows": [".github/workflows/integration.yml",
                                     ".github/workflows/unit.yml"]},
        ],
    }))
    # The honest ambiguous framing renders, with the REAL duration and BOTH workflow names.
    assert "`test / ethereum` is a REAL CI job (P50 13m 20s)" in s
    assert "integration.yml" in s and "unit.yml" in s
    assert "rename one job (or its matrix leg)" in s
    # The dishonest fileless framing must NOT apply to this job-backed check.
    assert "Don't investigate its gating manually" not in s
    assert "Fileless/managed check" not in s


def test_genuine_fileless_framing_unchanged_beside_an_ambiguous_pole():
    # The genuine bot/external check keeps today's framing byte-identical even when a
    # job-backed-ambiguous pole is present in the same report: the stamp is the ONLY signal
    # that flips the framing, and a fileless check never carries it.
    s = build_summary(_doc(pr_critical_path={
        "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
        "sample_fetch_failures": 0,
        "critical_path_check": "crate-checks (2/3)", "critical_path_s": 900.0,
        "poles": [
            {"check": "crate-checks (2/3)", "p50_s": 900.0,
             "workflow_file": ".github/workflows/lint.yml", "job": "crate-checks (2/3)"},
            {"check": "test / ethereum", "p50_s": 800.0, "timing_source": "workflow_jobs",
             "ambiguous_workflows": [".github/workflows/integration.yml",
                                     ".github/workflows/unit.yml"]},
            {"check": "Claude Code Review", "p50_s": 90.0},  # genuine fileless: no stamp
        ],
    }))
    # The genuine bot keeps the byte-identical fileless demotion framing.
    assert ("⚠ Fileless/managed check (no workflow file → NOT a tunable lever; the "
            "renderer auto-demotes it from the headline): Claude Code Review "
            "(1m 30s). Don't investigate its gating manually.") in s
    # ...while the job-backed-ambiguous pole still gets the honest framing (both coexist).
    assert "`test / ethereum` is a REAL CI job" in s


def test_ambiguous_pole_that_is_also_the_headline_renders_honest_not_managed():
    # Precedence pin (issue #118): a job-backed-ambiguous check can ALSO be the report headline
    # (reth's `test / ethereum` at 13m20s is exactly the kind of check that would headline). The
    # summary's `if ambiguous_workflows:` branch sits BEFORE the `elif ... == headline_check`
    # managed-headline branch, so the honest "REAL CI job" framing must win — a regression flipping
    # the order would print the "Managed/fileless check HEADLINES" wording, the dishonest framing
    # this PR exists to prevent.
    s = build_summary(_doc(pr_critical_path={
        "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
        "sample_fetch_failures": 0,
        # The ambiguous check IS the critical-path headline.
        "critical_path_check": "test / ethereum", "critical_path_s": 800.0,
        "poles": [
            {"check": "test / ethereum", "p50_s": 800.0, "timing_source": "workflow_jobs",
             "ambiguous_workflows": [".github/workflows/integration.yml",
                                     ".github/workflows/unit.yml"]},
        ],
    }))
    # Honest ambiguous framing wins over the managed-headline framing.
    assert "`test / ethereum` is a REAL CI job (P50 13m 20s)" in s
    assert "Managed/fileless check HEADLINES" not in s
    assert "Fileless/managed check" not in s


def test_addressable_poles_are_drilled_not_matrix_legs():
    # The two drilled poles (tests-web, e2e), NOT the 3 file-backed pre-collapse legs.
    s = build_summary(_doc())
    assert "Addressable long poles — the fixable critical path (2):" in s


def test_matrix_legs_collapse_in_fallback_when_no_bundle():
    doc = _doc()
    doc["data_bundle"] = {}  # force the pr_critical_path fallback
    collapsed = _collapse_poles([p for p in doc["pr_critical_path"]["poles"]
                                 if p.get("workflow_file")])
    # tests-web (pg15) + tests-web (pg12) -> one "tests-web"; e2e-tests stays.
    bases = [c for c, _wf, _p in collapsed]
    assert bases == ["tests-web", "e2e-tests"]
    # and the collapsed P50 is the max across the matrix legs.
    assert collapsed[0][2] == 223.5
    s = build_summary(doc)
    assert "fixable critical path (2):" in s


def test_structural_patterns_deduped():
    s = build_summary(_doc())
    # OPT70 appears twice in findings (per leg) but once in the summary.
    assert s.count("OPT70 ") == 1
    assert "Structural root-cause patterns (2):" in s  # OPT70 + OPT72


def test_tier2_summary_line_counts_promoted_findings_and_tops_by_minutes():
    # The ranking now lives in blocking_path (OD-F1 delegation) and is a plain
    # runner_min_saving sort (pricing excised — no SKU weighting). The top row is
    # the finding with the largest measured runner-minute saving.
    doc = _doc()
    doc["findings"] = [
        {"id": "r-linux", "pattern": "OPT46", "title": "Linux waste",
         "workflow_file": ".github/workflows/linux.yml",
         "runner_min_saving": 100.0, "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
        {"id": "r-macos", "pattern": "OPT46", "title": "macOS waste",
         "workflow_file": ".github/workflows/macos.yml",
         "runner_min_saving": 20.0, "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
        {"id": "modeled", "pattern": "OPT33", "title": "Modeled residual",
         "workflow_file": ".github/workflows/modeled.yml",
         "runner_min_saving": 500.0, "sizing_basis": "modeled",
         "tier2_neutrality": {"proof": "below_cluster_floor"}},
    ]
    _add_tier2_spine(doc)
    s = build_summary(doc)
    assert "Tier 2: 2 neutral bill findings, ~120 min/mo (top: OPT46 linux.yml)." in s
    assert "modeled.yml" not in s


def test_tier2_summary_line_ignores_unbacked_candidates():
    doc = _doc(findings=[
        {"id": "r-linux", "pattern": "OPT46", "title": "Linux waste",
         "workflow_file": ".github/workflows/linux.yml",
         "runner_min_saving": 100.0, "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
    ])
    s = build_summary(doc)
    assert "Tier 2: 0 neutral bill findings." in s
    assert "linux.yml" not in s


def test_tier2_summary_line_ignores_spine_without_totals():
    doc = _doc()
    doc["findings"] = [
        {"id": "r-linux", "pattern": "OPT46", "title": "Linux waste",
         "workflow_file": ".github/workflows/linux.yml",
         "runner_min_saving": 100.0, "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
    ]
    _add_tier2_spine(doc)
    doc["runner_minute_spine"].pop("totals")

    s = build_summary(doc)

    assert "Tier 2: 0 neutral bill findings." in s
    assert "linux.yml" not in s


def test_tier2_summary_order_stays_coupled_to_renderer():
    findings = [
        {"id": "r-linux", "pattern": "OPT46", "workflow_file": ".github/workflows/linux.yml",
         "runner_min_saving": "100.0", "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
        {"id": "r-macos", "pattern": "OPT46", "workflow_file": ".github/workflows/macos.yml",
         "runner_min_saving": 20.0, "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}},
        {"id": "modeled", "pattern": "OPT33", "workflow_file": ".github/workflows/modeled.yml",
         "runner_min_saving": 500.0, "sizing_basis": "modeled",
         "tier2_neutrality": {"proof": "below_cluster_floor"}},
    ]
    doc = _add_tier2_spine(_doc(findings=findings))
    summary_ids = [f["id"] for f in summary_tier2_promoted(doc)]
    # Dedupe the renderer side too — the renderer's own call sites consume
    # _dedupe_findings output, and the summary delegates over that same
    # population; comparing against a raw list would silently diverge on a
    # future fixture carrying a byte-duplicate entry (Greptile #219 P2).
    renderer_ids = [f["id"] for f in renderer_tier2_source_backed_ranked(
        bp_mod._dedupe_findings(findings), doc)]
    # Plain runner_min_saving sort (pricing excised): linux (100) ranks above macos (20).
    assert summary_ids == renderer_ids == ["r-linux", "r-macos"]


def test_tier2_summary_prefers_exact_job_over_matrix_base_decoys():
    finding = {
        "id": "r-cleanup",
        "pattern": "OPT46",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["cleanup"],
        "runner_min_saving": 120.0,
        "sizing_basis": "measured",
        "tier2_neutrality": {"proof": "post_completion_waste"},
    }
    rows = [
        {
            "workflow_file": ".github/workflows/ci.yml",
            "job_name": "cleanup",
            "runner_label": "ubuntu-latest",
            "event_scope": "all-events",
            "status_filter": "success",
            "attempt_filter": "latest",
            "volume_filter": "all-status",
            "raw_compute_runner_min_per_month": 50.0,
            "billable_equiv_min_per_month": 60.0,
            "share_of_all_row_total": 0.062,
        },
        {
            "workflow_file": ".github/workflows/ci.yml",
            "job_name": "cleanup (decoy)",
            "runner_label": "ubuntu-latest",
            "event_scope": "all-events",
            "status_filter": "success",
            "attempt_filter": "latest",
            "volume_filter": "all-status",
            "raw_compute_runner_min_per_month": 900.0,
            "billable_equiv_min_per_month": 900.0,
            "share_of_all_row_total": 0.938,
        },
    ]
    doc = _doc(findings=[finding], runner_minute_spine={
        "render_ready": True,
        "rows": rows,
        "totals": {
            "row_count": 2,
            "raw_compute_runner_min_per_month": 950.0,
            "billable_equiv_min_per_month": 960.0,
            "percentage_denominator": "all_rows_billable_equiv_min_per_month",
        },
    })

    assert summary_tier2_promoted(doc) == []
    assert "Tier 2: 0 neutral bill findings." in build_summary(doc)


def _summary_tier2_line(text: str) -> tuple[int, float]:
    """(count, total_min) parsed from a rendered 'Tier 2: …' summary line."""
    m = re.search(r"Tier 2: (\d+) neutral bill finding(?:s)?(?:, ~([\d,.]+) min/mo)?",
                  text)
    assert m, f"no Tier 2 summary line in:\n{text}"
    total = float(m.group(2).replace(",", "")) if m.group(2) else 0.0
    return int(m.group(1)), total


def test_tier2_summary_counts_the_renderers_promoted_set_on_every_committed_corpus():
    """Review V1 (plan tier2-review-fixes.md, PR-F1 cells 1+4): the stdout summary
    and the rendered report must count the SAME promoted Tier-2 set. PR-S2 (#203)
    widened the OPT64 source binding in the renderer/verifier/bill-gap copies but
    summary.py kept the narrow pre-S2 copy: on the committed requests corpus the
    summary printed '5 … ~469 min/mo' while the report renders 9 R-rows / 713
    min/mo. Legacy corpora (cell 1) must agree trivially (both zero or both n)."""
    mismatches = []
    for fj in sorted(_REPORTS_DIR.glob("*/findings.json")):
        doc = json.loads(fj.read_text(encoding="utf-8"))
        promoted = renderer_tier2_source_backed_ranked(
            bp_mod._dedupe_findings(list(doc.get("findings") or [])), doc)
        want_count = len(promoted)
        want_total = sum(float(f.get("runner_min_saving") or 0.0) for f in promoted)
        got_count, got_total = _summary_tier2_line(build_summary(doc))
        if got_count != want_count or abs(got_total - round(want_total)) > 1.0:
            mismatches.append(f"{fj.parent.name}: summary {got_count}/{got_total} "
                              f"!= renderer {want_count}/{round(want_total, 1)}")
    assert not mismatches, "summary Tier-2 line disagrees with the renderer's " \
        f"promoted set on: {mismatches}"


def test_tier2_summary_matches_the_committed_requests_report_artifact():
    """Cell 4, artifact-anchored: the un-enumerated OPT64 wide-binding cell V1
    lived in. On the committed requests corpus the summary's count/minutes/top
    must equal the rendered report's R-rows — re-derived from the committed
    report BYTES (not only the renderer), so a drifted committed artifact fails
    here rather than vouching for itself."""
    fj = _REPORTS_DIR / "requests" / "findings.json"
    if not fj.exists():
        pytest.skip(_NO_CORPUS_REASON)
    report = (_REPORTS_DIR / "requests" / "blocking-path-speed.md").read_text(
        encoding="utf-8")
    # The report's own accounting line: total + count over ALL promoted rows.
    m = re.search(r"\*\*After the gate\.\*\* ([\d,.]+) min/mo of wall-clock-neutral "
                  r"runner minutes is recoverable \((\d+) neutral finding", report)
    assert m, "requests report lost its 'After the gate' Tier-2 accounting line"
    report_total, report_count = float(m.group(1).replace(",", "")), int(m.group(2))
    shown = len(re.findall(r"^## 🟢 Runner saving", report, re.M))
    overflow = re.search(r"\*\*\+(\d+) more wall-clock-neutral", report)
    assert report_count == shown + (int(overflow.group(1)) if overflow else 0), \
        "report accounting line disagrees with its own R-rows — fix the corpus first"

    doc = json.loads(fj.read_text(encoding="utf-8"))
    s = build_summary(doc)
    got_count, got_total = _summary_tier2_line(s)
    assert (got_count, got_total) == (report_count, round(report_total)), (
        f"summary Tier-2 line ({got_count} findings, ~{got_total} min/mo) != the "
        f"committed report's promoted set ({report_count} R-rows, "
        f"{report_total} min/mo) — the pre-S2 narrow OPT64 binding is back (V1)")
    # The top row must be the report's R1 (same pattern + workflow).
    promoted = renderer_tier2_source_backed_ranked(
        bp_mod._dedupe_findings(list(doc.get("findings") or [])), doc)
    top = promoted[0]
    top_label = f"{top.get('pattern')} {Path(str(top.get('workflow_file'))).name}"
    assert f"(top: {top_label})" in s


def test_tier2_summary_standalone_invocation_agrees_with_the_renderer():
    """OD-F1's risk cell: delegation must not break `python3 scripts/summary.py
    FINDINGS.json` (the documented standalone entry point) or create an import
    cycle. Runs the real CLI on the committed requests corpus."""
    if not (_REPORTS_DIR / "requests" / "findings.json").exists():
        pytest.skip(_NO_CORPUS_REASON)
    proc = subprocess.run(
        [sys.executable, str(_SKILL_DIR / "scripts" / "summary.py"),
         str(_REPORTS_DIR / "requests" / "findings.json")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((_REPORTS_DIR / "requests" / "findings.json").read_text(
        encoding="utf-8"))
    promoted = renderer_tier2_source_backed_ranked(
        bp_mod._dedupe_findings(list(doc.get("findings") or [])), doc)
    got_count, got_total = _summary_tier2_line(proc.stdout)
    assert got_count == len(promoted)
    assert abs(got_total - round(sum(float(f.get("runner_min_saving") or 0.0)
                                     for f in promoted))) <= 1.0


def test_tier2_summary_counts_past_the_display_cap():
    """Cell 5: the renderer's 12-row cap is DISPLAY-only (§6) — a 13th
    source-backed row still counts in the section accounting, so the summary's
    count/total must cover ALL source-backed eligible rows, not the shown 12."""
    findings = []
    for i in range(1, 14):  # 13 > _TIER2_CAP (12)
        findings.append({
            "id": f"r{i}", "pattern": "OPT46",
            "workflow_file": f".github/workflows/wf{i}.yml",
            "runner_min_saving": float(10 * i), "sizing_basis": "measured",
            "tier2_neutrality": {"proof": "post_completion_waste"}})
    doc = _add_tier2_spine(_doc(findings=findings))
    promoted = renderer_tier2_source_backed_ranked(findings, doc)
    assert len(promoted) == 13, "fixture must promote past the display cap"
    got_count, got_total = _summary_tier2_line(build_summary(doc))
    assert got_count == 13
    assert got_total == round(sum(10.0 * i for i in range(1, 14)))
    assert "(top: OPT46 wf13.yml)" in build_summary(doc)


def test_render_command_keys_bind_to_their_own_pole():
    # The contract that justified emitting the command at all: each KEY must bind
    # back to ITS pole via the renderer's actual matcher, even with two poles in
    # the same workflow (pipeline.yml).
    doc = _doc()
    logs = doc["data_bundle"]["logs"]
    keys = _render_keys(logs)
    assert keys == ["tests-web", "e2e-tests"]
    dlog = {k: f"/d/{e['file']}" for k, e in zip(keys, logs)}
    for k, e in zip(keys, logs):
        wf = os.path.basename(e["workflow_file"])
        assert _match_key(dlog, wf, e["check"]) == f"/d/{e['file']}", \
            f"KEY {k} mis-binds for {e['check']}"


def test_render_keys_same_workflow_shared_first_token_no_misbind():
    # The bug the first cut shipped: two DISTINCT poles in one workflow whose check
    # names share a first token ('deploy staging' / 'deploy prod'). The old `-2`
    # de-dup produced a key the renderer could not bind, so the 2nd pole silently
    # rendered the 1st pole's drill log. Each must now bind to its OWN log via the
    # real matcher.
    logs = [
        {"check": "deploy staging", "workflow_file": ".github/workflows/cd.yml",
         "file": "staging.log"},
        {"check": "deploy prod", "workflow_file": ".github/workflows/cd.yml",
         "file": "prod.log"},
    ]
    keys = _render_keys(logs)
    assert len(set(keys)) == len(keys)
    dlog = {k: f"/d/{e['file']}" for k, e in zip(keys, logs)}
    for k, e in zip(keys, logs):
        wf = os.path.basename(e["workflow_file"])
        assert _match_key(dlog, wf, e["check"]) == f"/d/{e['file']}", \
            f"pole {e['check']!r} mis-binds via key {k!r}"


def test_render_keys_nested_check_names_bind_distinctly():
    # One check name is a substring of the other ('deploy' vs 'deploy prod'), same
    # workflow. Longest-substring matching must still bind each to its own log.
    logs = [
        {"check": "deploy", "workflow_file": ".github/workflows/cd.yml", "file": "a.log"},
        {"check": "deploy prod", "workflow_file": ".github/workflows/cd.yml", "file": "b.log"},
    ]
    keys = _render_keys(logs)
    dlog = {k: f"/d/{e['file']}" for k, e in zip(keys, logs)}
    for k, e in zip(keys, logs):
        wf = os.path.basename(e["workflow_file"])
        assert _match_key(dlog, wf, e["check"]) == f"/d/{e['file']}"


def test_render_keys_check_named_like_workflow_stem_no_hijack():
    # _match_key gives an exact workflow-stem match ABSOLUTE priority: a check named
    # exactly 'pipeline' in pipeline.yml would, as a bare key, hijack EVERY pole in
    # that workflow (the `full` tier wasn't round-trip-validated). Escalating to
    # fully-qualified keys must make each pole bind to its own log.
    logs = [
        {"check": "pipeline", "workflow_file": ".github/workflows/pipeline.yml", "file": "a.log"},
        {"check": "e2e", "workflow_file": ".github/workflows/pipeline.yml", "file": "b.log"},
    ]
    keys = _render_keys(logs)
    assert len(set(keys)) == len(keys)
    dlog = {k: f"/d/{e['file']}" for k, e in zip(keys, logs)}
    for k, e in zip(keys, logs):
        wf = os.path.basename(e["workflow_file"])
        assert _match_key(dlog, wf, e["check"]) == f"/d/{e['file']}", \
            f"pole {e['check']!r} mis-binds via key {k!r}"


def test_render_command_shell_quotes_in_and_out_paths():
    # A findings/report path with a space must not emit a command that breaks on the
    # shell boundary; --in/--out are shell-quoted like the binding tokens.
    cmd = render_command(_doc(), findings_path="/my dir/f.json", out_path="/my dir/r.md")
    assert "'/my dir/f.json'" in cmd
    assert "'/my dir/r.md'" in cmd
    # the no-space common case stays unquoted (clean output)
    plain = render_command(_doc(), findings_path="/tmp/f.json", out_path="/tmp/r.md")
    assert "--in /tmp/f.json --out /tmp/r.md" in plain


def test_render_command_quotes_path_half_with_spaces():
    # The quoting guard must catch whitespace in the PATH half (data dir / filename),
    # not only the KEY half.
    cmd = render_command(_doc(), data_dir="/my data dir")
    assert "'tests-web=/my data dir/tests-web-1.log'" in cmd


def test_render_command_quotes_keys_with_spaces():
    # Full-check fallback keys contain spaces; the emitted KEY=PATH token must be
    # shell-quoted so the command stays copy-paste runnable.
    logs = [
        {"check": "deploy staging", "workflow_file": ".github/workflows/cd.yml",
         "file": "s.log", "steps_file": "s.steps.json", "mag_file": "s.mag.json"},
        {"check": "deploy prod", "workflow_file": ".github/workflows/cd.yml",
         "file": "p.log", "steps_file": "p.steps.json", "mag_file": "p.mag.json"},
    ]
    doc = {"data_bundle": {"logs_dir": "/d", "logs": logs},
           "scanned_at": "2026-06-11T00:00:00+00:00"}
    cmd = render_command(doc, findings_path="/f.json", out_path="/r.md")
    assert "'deploy staging=/d/s.log'" in cmd
    assert "'deploy prod=/d/p.log'" in cmd


def test_render_command_quotes_metacharacter_keys_without_spaces():
    logs = [
        {"check": "build$danger", "workflow_file": ".github/workflows/ci.yml",
         "file": "build.log"},
        {"check": "test", "workflow_file": ".github/workflows/ci.yml",
         "file": "test.log"},
    ]
    doc = {"data_bundle": {"logs_dir": "/d", "logs": logs},
           "scanned_at": "2026-06-11T00:00:00+00:00"}
    cmd = render_command(doc, findings_path="/f.json", out_path="/r.md")
    assert "--log 'build$danger=/d/build.log'" in cmd


def test_render_keys_avoid_equals_delimiter_and_still_bind():
    logs = [
        {"check": "test os=linux", "workflow_file": ".github/workflows/pipeline.yml",
         "file": "linux.log"},
        {"check": "test os=mac", "workflow_file": ".github/workflows/pipeline.yml",
         "file": "mac.log"},
    ]
    keys = _render_keys(logs)
    assert keys == ["linux", "mac"]
    assert all("=" not in key for key in keys)
    dlog = {k: f"/d/{e['file']}" for k, e in zip(keys, logs)}
    for k, e in zip(keys, logs):
        wf = os.path.basename(e["workflow_file"])
        assert _match_key(dlog, wf, e["check"]) == f"/d/{e['file']}", \
            f"pole {e['check']!r} mis-binds via key {k!r}"

    doc = {"data_bundle": {"logs_dir": "/d", "logs": logs},
           "scanned_at": "2026-06-11T00:00:00+00:00"}
    cmd = render_command(doc, findings_path="/f.json", out_path="/r.md")
    assert "--log linux=/d/linux.log" in cmd
    assert "--log mac=/d/mac.log" in cmd
    assert "test os=linux=/d/linux.log" not in cmd


def test_render_command_is_runnable_and_complete():
    cmd = render_command(_doc(), findings_path="/tmp/f.json", out_path="/tmp/r.md",
                         data_dir="/tmp/x.data")
    assert "blocking_path.py --in /tmp/f.json --out /tmp/r.md" in cmd
    assert "--log tests-web=/tmp/x.data/tests-web-1.log" in cmd
    assert "--steps e2e-tests=/tmp/x.data/e2e-2.steps.json" in cmd
    assert "--captured-at 2026-06-11T07:10:57+00:00" in cmd


def test_render_command_empty_without_drill_logs():
    doc = _doc()
    doc["data_bundle"] = {}
    assert render_command(doc) == ""
    # build_summary still emits a runnable level-1 render line.
    assert "blocking_path.py --in" in build_summary(doc)


def test_no_log_fallback_quotes_paths_with_spaces():
    doc = _doc()
    doc["data_bundle"] = {}
    summary = build_summary(doc, findings_path="/my dir/findings.json",
                            out_path="/out dir/report.md")
    assert ("python3 scripts/blocking_path.py --in '/my dir/findings.json' "
            "--out '/out dir/report.md'") in summary


# ── Empty-spine diagnostics (issue #81) ──────────────────────────────────────
# The live double-failure: two default-target runs on an active repo (~766 runs/30d)
# printed only "No drill logs were captured" and rendered static-only reports, giving the
# driving agent nothing to act on (it guessed --target 100 and mislearned the cause). The
# summary must now walk the data-pass funnel from stamped facts and report the first empty
# stage — but ONLY on the anomaly (high volume + empty, or an outright collection failure);
# a genuinely quiet repo stays quiet.

def _fdoc(ds=None, cp=None, **over):
    """A findings doc whose funnel EMPTIED — default shape: an active repo (766
    runs/30d) whose gate sample fetched 16 PRs but KEPT 0 (the live-failure shape)."""
    doc = {
        "scanned_at": "2026-07-20T00:00:00+00:00",
        "repo": "acme/widgets",
        "required_checks": None,
        "required_checks_complete": False,
        "per_workflow_monthly_volume": {".github/workflows/ci.yml": 766},
        "data_sources": {
            "tiers_run": ["gh-timing"],
            "workflows_analyzed": 3,
            "runs_sampled": 40,
            "run_list_fetch_failures": [],
            "job_fetch_failures": [],
            "logs_fetched": None,
        },
        "pr_critical_path": {
            "sampled_pr_count": 0, "sample_target": 20,
            "sample_fetched": 16, "sample_fetch_failures": 0,
            "config_eras": [], "poles": [],
        },
        "findings": [],
    }
    if ds:
        doc["data_sources"].update(ds)
    if cp:
        doc["pr_critical_path"].update(cp)
    doc.update(over)
    return doc


def test_diag_silent_on_a_healthy_run():
    # The healthy _doc() (poles present, volume unset) must never emit diagnostics.
    assert empty_spine_diagnostics(_doc(), findings_path="/f.json") == []
    assert "EMPTY-SPINE DIAGNOSTICS" not in build_summary(_doc())


def test_diag_pr_kept_break_reports_fetched_and_kept():
    # The live-failure stage: PRs fetched, none kept. The reason names both counts.
    chain, stage = funnel_reason_chain(_fdoc())
    assert stage == "pr_kept"
    joined = "\n".join(chain)
    assert "16 PR(s) fetched, 0 carried a completed required suite in the window" in joined
    # Upstream stages that DID pass are shown with their counts (funnel visibility).
    assert "3 workflow(s) analyzed" in joined
    assert "40 run(s) sampled" in joined
    assert "16 PR candidate(s) fetched for the gate sample (target 20)" in joined


def test_diag_pr_kept_names_required_checks_when_readable():
    chain, stage = funnel_reason_chain(_fdoc(
        required_checks=["ci / build", "ci / test"], required_checks_complete=True))
    joined = "\n".join(chain)
    assert stage == "pr_kept"
    assert "ci / build" in joined and "ci / test" in joined


def test_diag_pr_kept_flags_fetch_failures_as_transient_note():
    chain, _ = funnel_reason_chain(_fdoc(cp={"sample_fetch_failures": 4}))
    joined = "\n".join(chain)
    assert "4 check-run fetch failure(s)" in joined
    assert "4 of those fetches FAILED" in joined


def test_diag_collection_break_when_gh_pass_never_ran():
    doc = _fdoc(
        ds={"tiers_run": [], "partial_kind": "collection_failed",
            "partial_reason": "the workflow-list fetch failed (gh API error)"},
        per_workflow_monthly_volume={})  # volume unknown — must fire anyway
    chain, stage = funnel_reason_chain(doc)
    assert stage == "collection"
    assert "gh data pass did NOT run" in chain[0]
    assert "workflow-list fetch failed" in chain[0]
    # Collection failure is never "quiet": diagnostics fire even at zero known volume.
    diag = empty_spine_diagnostics(doc, findings_path="/f.json")
    assert diag and "EMPTY-SPINE DIAGNOSTICS" in diag[0]
    assert _break_is_transient(doc, stage) is True


def test_diag_workflows_zero_is_durable():
    doc = _fdoc(ds={"workflows_analyzed": 0})
    chain, stage = funnel_reason_chain(doc)
    assert stage == "workflows"
    assert "0 workflows analyzed" in chain[0]
    assert _break_is_transient(doc, stage) is False


def test_diag_runs_zero_names_the_failed_workflows():
    doc = _fdoc(ds={"runs_sampled": 0, "run_list_fetch_failures": [
        {"workflow_file": ".github/workflows/ci.yml", "fetch": "success run sample"}]})
    chain, stage = funnel_reason_chain(doc)
    assert stage == "runs"
    assert "0 runs sampled" in chain[-1]
    assert "run-list fetch FAILED for: .github/workflows/ci.yml" in chain[-1]
    assert _break_is_transient(doc, stage) is True


def test_diag_pr_fetch_zero_is_developer_event_and_durable():
    doc = _fdoc(cp={"sample_fetched": 0})
    chain, stage = funnel_reason_chain(doc)
    assert stage == "pr_fetch"
    assert "0 PR / merge-queue candidates ran a timed check" in chain[-1]
    assert _break_is_transient(doc, stage) is False


def test_diag_poles_zero_with_job_fetch_wipeout_is_transient():
    doc = _fdoc(
        cp={"sampled_pr_count": 20, "poles": []},
        ds={"job_fetch_failures": [
            {"workflow_file": ".github/workflows/ci.yml", "fetch": "per-run job sample"}]})
    chain, stage = funnel_reason_chain(doc)
    assert stage == "poles"
    assert "0 long poles resolved" in chain[-1]
    assert "every per-run JOB fetch FAILED for: .github/workflows/ci.yml" in chain[-1]
    assert _break_is_transient(doc, stage) is True


def test_diag_drill_capture_failure_reported_when_poles_exist():
    doc = _fdoc(
        cp={"sampled_pr_count": 20,
            "poles": [{"check": "build", "workflow_file": ".github/workflows/ci.yml",
                       "p50_s": 300.0}]},
        ds={"logs_fetched": 0, "job_fetch_failures": [
            {"workflow_file": ".github/workflows/ci.yml", "fetch": "per-run job sample"}]})
    chain, stage = funnel_reason_chain(doc)
    assert stage == "drill"
    assert "1 pole(s) resolved but 0 drill logs captured" in chain[-1]
    assert _break_is_transient(doc, stage) is True


def test_diag_era_thin_flip_is_surfaced_when_present():
    doc = _fdoc(
        cp={"sampled_pr_count": 20, "poles": [],
            "config_eras": [{"workflow_file": ".github/workflows/ci.yml",
                             "kept_era": "post", "rule": "post_only_thin",
                             "pre_count": 5, "post_count": 3}]})
    chain, _ = funnel_reason_chain(doc)
    joined = "\n".join(chain)
    assert "1 workflow(s) straddled a config change; 1 thin-flipped to post-only" in joined


def test_diag_low_volume_empty_stays_quiet():
    # A genuinely quiet repo (few runs/30d) that completed the pass with no spine keeps
    # the current static-only outcome — no diagnostics, still the plain fallback line.
    doc = _fdoc(per_workflow_monthly_volume={".github/workflows/ci.yml": 5})
    assert _total_30d_volume(doc) < _ACTIVE_30D_RUNS
    assert empty_spine_diagnostics(doc, findings_path="/f.json") == []
    s = build_summary(doc)
    assert "EMPTY-SPINE DIAGNOSTICS" not in s
    assert "No drill logs were captured" in s


def test_diag_high_volume_empty_fires_with_chain_and_escalation():
    s = build_summary(_fdoc())
    assert "EMPTY-SPINE DIAGNOSTICS" in s
    assert "~766 run(s)/30d" in s
    assert "ANOMALY" in s


def test_diag_rerun_command_uses_runpy_and_never_a_raised_target():
    # Transient break → RE-RUN recommendation with the exact run.py command; it must NOT
    # tell the agent to raise --target (the issue #81 mislead) and must warn against it.
    doc = _fdoc(cp={"sample_fetch_failures": 3})  # a fetch gap → transient
    s = build_summary(doc, findings_path="/scratch/findings.json", root="/src/widgets")
    assert "RE-RUN the SAME audit" in s
    assert "Do NOT raise --target" in s
    # The emitted command carries no --target flag at all (the warning text may name it).
    cmd_line = s.split("Re-run:", 1)[1].strip().splitlines()[0]
    assert cmd_line == ("python3 scripts/run.py --root /src/widgets "
                        "--out /scratch/findings.json --repo acme/widgets --with-logs")
    assert "--target" not in cmd_line


def test_diag_rerun_command_placeholder_root_when_unknown():
    doc = _fdoc(cp={"sample_fetch_failures": 3})
    cmd = _rerun_command("acme/widgets", None, "/f.json")
    assert cmd == ("python3 scripts/run.py --root <YOUR_REPO_CHECKOUT> "
                   "--out /f.json --repo acme/widgets --with-logs")


def test_diag_durable_break_says_property_not_rerun():
    # pr_kept with a readable required set and NO fetch failures → a durable property of
    # the repo/window, not a transient gap: no RE-RUN framing.
    doc = _fdoc(required_checks=["ci / build"], required_checks_complete=True)
    s = build_summary(doc)
    assert "PROPERTY of the repo" in s
    assert "RE-RUN the SAME audit" not in s


def test_diag_gh_pass_that_never_ran_is_not_called_a_property_of_the_repo():
    # A gh pass that could not start (no gh, no auth — or a sandboxed / offline shell
    # where `gh auth status` fails) measured NOTHING about the repo. The diagnostics used
    # to say "a broken run" and then "a PROPERTY of the repo … re-running reproduces it"
    # in the same block — self-contradictory, and false for the sandbox case, where a
    # retry with network access recovers the full pass.
    doc = _fdoc(
        ds={"tiers_run": [], "partial_kind": "gh_unavailable",
            # The shape collect_runs._gh_unavailable_reason produces.
            "partial_reason": "gh data pass could not start: `gh auth status` exited 1: "
                              "error connecting to api.github.com. gh may be missing or "
                              "logged out — or this shell is sandboxed / has no network "
                              "access (gh works in a normal terminal but not here). If "
                              "`gh auth status` succeeds elsewhere, retry the audit with "
                              "network / host access."},
        per_workflow_monthly_volume={})
    s = build_summary(doc)
    assert "EMPTY-SPINE DIAGNOSTICS" in s
    assert "error connecting to api.github.com" in s
    assert "PROPERTY of the repo" not in s
    assert "re-running reproduces it" not in s
    assert "sandbox" in s and "network" in s


def test_gh_pass_that_never_ran_does_not_forbid_re_probing_gh():
    # "the data pass already queried rulesets ... (do NOT re-probe gh)" is false when the
    # gh pass never started — and the remedy in that case IS to retry gh.
    doc = _fdoc(
        ds={"tiers_run": [], "partial_kind": "gh_unavailable",
            "partial_reason": "gh data pass could not start: `gh auth status` exited 1: "
                              "Failed to log in. Retry the audit with network / host "
                              "access."},
        per_workflow_monthly_volume={})
    s = build_summary(doc)
    assert "do NOT re-probe gh" not in s
    assert "already queried" not in s
    assert "did NOT run — gh data pass could not start" not in s   # no doubled phrase
    assert s.count("network / host access") == 1                    # remedy said once


def test_diag_transient_classifier_pr_kept_keys_on_fetch_failures():
    assert _break_is_transient(_fdoc(cp={"sample_fetch_failures": 2}), "pr_kept") is True
    assert _break_is_transient(_fdoc(cp={"sample_fetch_failures": 0}), "pr_kept") is False


def test_diag_poles_durable_hint_names_the_actual_cause():
    # Greptile P2 (PR #86): the poles-stage durable hint must name the cause the FACTS
    # show. all_checks_fileless → the fileless message; a genuinely fast repo (poles
    # empty, nothing fileless, no job-fetch failures) → the below-threshold message,
    # never a fileless misdiagnosis.
    fileless = _fdoc(cp={"sampled_pr_count": 20, "poles": [], "all_checks_fileless": True})
    assert "fileless/managed" in _durable_hint("poles", fileless)
    fast = _fdoc(cp={"sampled_pr_count": 20, "poles": []})
    hint = _durable_hint("poles", fast)
    assert "below the long-pole threshold" in hint
    assert "fileless" not in hint


def test_total_30d_volume_skips_none_and_bool():
    doc = {"per_workflow_monthly_volume": {"a": 700, "b": None, "c": 66, "d": True}}
    assert _total_30d_volume(doc) == 766
