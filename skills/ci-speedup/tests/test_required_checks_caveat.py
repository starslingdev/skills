"""Guard: the required-checks caveat rides every fix direction that splits work OUT
of a check into new jobs.

A real `/ci-speedup` run diagnosed a 6m10s serial mutation-testing step as the merge
gate's dominant lever; the operator's local agent sharded it out of the single required
`test` check into a 4-way CI matrix. That silently ungates main — the new shard jobs are
NOT required status checks until someone adds them to branch protection, so the split-out
work stops gating merges while everything stays green. The run only avoided a silently
ungated main because the agent caught it unprompted.

These pins make the caveat a durable invariant, on EVERY surface a user/agent actually
reads: the rendered per-pole agent prompts (`_FIX_META` constraints), the rendered
structural handoff (`_STRUCTURAL_META` guardrail, via `blocking_path.render`), and the
catalog doc. A new split-into-new-jobs caveat has to be pinned on all three.
Reword the caveat freely — but if you DROP it from any of these split-into-new-jobs sites,
this fails loudly rather than letting the report hand out a silently-ungating fix.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402  (same scripts/ dir, just added to sys.path)

# Fix directions whose deliver INCLUDES a split-into-new-jobs / matrix path (primary for
# cargo-test-shard and android-emulator-shard; a secondary "or"/"and/or" alternative for the
# others — see the per-key notes) — so the new jobs need re-gating in branch protection.
_SPLIT_INTO_NEW_JOBS_FIX_KEYS = [
    "cargo-test-shard",
    "android-emulator-shard",
    "gradle-test-parallelism",
    "pytest-no-xdist",         # the "shard by directory across matrix jobs" alternative
    "playwright-parallel",     # the "shard across jobs" alternative
    "benchmark-serial-reruns", # the "parallelise across runners" alternative
]

_CATALOG = (Path(__file__).resolve().parents[1] / "references" / "optimization-patterns.md").read_text()


def _has_required_checks_caveat(text: str) -> bool:
    """The caveat's load-bearing shape: it names branch protection AND required checks AND
    the silent-gating failure. Keyed on stable nouns, not exact wording, so a reword stays
    green but a DROP goes red."""
    t = re.sub(r"\s+", " ", text).lower()
    names_the_mechanism = "branch protection" in t and "required" in t
    names_the_failure = "silently" in t and "gat" in t  # "...silently stop gating merges"
    return names_the_mechanism and names_the_failure


def test_split_into_new_jobs_fix_meta_carries_the_caveat():
    for key in _SPLIT_INTO_NEW_JOBS_FIX_KEYS:
        meta = bp._FIX_META[key]
        assert _has_required_checks_caveat(meta["constraints"]), (
            f"_FIX_META['{key}'] renders a split-into-new-jobs fix but its constraints no "
            "longer warn that the new jobs must be re-added to branch protection as required "
            "checks or the split-out work silently stops gating merges."
        )


def test_catalog_sharding_patterns_carry_the_caveat():
    # OPT24 (Long Test Job Without Sharding), OPT25 (Shard Imbalance — split a leg into new
    # jobs), OPT22 (consolidate workflows — renames the check) each hand out a fix that
    # changes/adds check names, so each must carry the caveat.
    for anchor in ("Long Test Job Without Sharding", "Shard Imbalance",
                   "Sequential Workflows via `workflow_run`"):
        assert anchor in _CATALOG, (
            f"catalog anchor {anchor!r} not found — was the pattern title renamed?"
        )
        start = _CATALOG.index(anchor)
        # Bound the window at the NEXT pattern's `### ` header so each pattern's pin bites on
        # its OWN caveat — a fixed char window could bleed into the next pattern's caveat and
        # false-pass if this pattern's were dropped.
        nxt = _CATALOG.find("\n### ", start + 1)
        section = _CATALOG[start:nxt] if nxt != -1 else _CATALOG[start:]
        assert "Required-checks caveat" in section and _has_required_checks_caveat(section), (
            f"the catalog pattern near {anchor!r} lost its required-checks caveat"
        )


def test_caveat_predicate_discriminates_absent_from_present():
    """Red-proof: the SAME predicate must return False on constraints prose that has no
    caveat and True once the caveat is appended, so this guard can't silently regress into a
    tautology that always passes."""
    without = ("Tests sharded across runners must stay ISOLATED: each shard needs its own "
               "fresh backend, keep the full test count, and confirm pass/fail parity.")
    assert not _has_required_checks_caveat(without)
    with_it = without + (" If this job is a required status check, add the new shard jobs to "
                         "branch protection as required checks or the split-out tests silently "
                         "stop gating merges.")
    assert _has_required_checks_caveat(with_it)


# ── Relocation: preserving required coverage ──────────────────────────────────
# OPT75's relocate branch moves the dominant step OUT of the gating job. Moving the
# work does not move the gate: the new job's check name is not required until an admin
# adds it, and a `needs:` edge only orders jobs. Worse, a dependent skipped because a
# `needs:` dependency FAILED reports as skipped, not failed, so a required check
# satisfied by that job is satisfied while the work never ran. Both halves must reach
# the operator on the surfaces they read: the rendered structural handoff (built from
# PRODUCTION `_STRUCTURAL_META`, not a fixture that supplies its own warning) and the
# catalog.
def _has_relocation_coverage_caveat(text: str) -> bool:
    """The relocation guardrail's load-bearing shape: moving work out of a required
    check needs the coverage re-established, and a bare `needs:` edge is called out as
    insufficient.

    How the clauses key, precisely — two of the seven clauses across this predicate
    and `_has_dependency_skip_caveat` require a CONTIGUOUS phrase ("required check
    name" here, and "success" paired with a failure word there); the other five
    (`names_the_gate` here, and `names_the_skip` / `runs_unconditionally` /
    `reads_the_results` there) are CO-OCCURRENCE checks over the whole passage. The
    contiguous clauses are what stopped the inverted-prose case in
    `test_relocation_predicates_discriminate_absent_from_present`, where pure
    co-occurrence accepted prose asserting the OPPOSITE of the guardrail ("a
    `needs:` edge alone is fine") because "needs" and "alone" both appeared. Keeping
    "required check name" contiguous also pins the route an executing agent can
    actually TAKE: adding a check name to branch protection is admin-only, so the
    re-gating instruction must name keeping the REQUIRED CHECK NAME on a verdict job
    rather than assume a suitable one exists.

    KNOWN CEILING (follow-up, not this change): because five clauses are
    co-occurrence, these predicates do NOT reject the two traps the OPT75 section
    itself names — prose putting the `needs.*.result` test inside a job-level `if:`,
    or using `contains(needs.*.result, 'failure')` — both of which supply every token
    the clauses look for. Tightening them to reject those is tracked separately.
    """
    t = re.sub(r"\s+", " ", text).lower()
    names_the_gate = "required" in t and ("branch protection" in t or "ruleset" in t)
    names_the_reestablishment = (("verdict" in t or "aggregat" in t)
                                 and "required check name" in t)
    names_the_insufficiency = "does not gate" in t or "is not enough" in t
    return names_the_gate and names_the_reestablishment and names_the_insufficiency


def _has_dependency_skip_caveat(text: str) -> bool:
    """The dependency-failure skip explanation: a dependent skipped by a FAILED
    dependency reports skipped (not failed) and can satisfy the gate, so the verdict
    must propagate the dependency outcomes rather than merely run `always()`.

    The FAILURE SEMANTICS are load-bearing and asserted separately: presence of
    `always()` plus the word "result" accepted "just add always() and it will report
    the correct result", which is precisely the trap the caveat names. `!cancelled()`
    is accepted alongside `always()` because the catalog itself blesses it, so a
    correct reword must not turn this red.
    """
    t = re.sub(r"\s+", " ", text).lower()
    names_the_skip = "skipped" in t and ("needs" in t or "dependenc" in t)
    runs_unconditionally = "always()" in t or "!cancelled()" in t
    reads_the_results = "result" in t or "outcome" in t
    rejects_non_success = "success" in t and ("fail" in t or "non-zero" in t)
    return (names_the_skip and runs_unconditionally
            and reads_the_results and rejects_non_success)


def _rendered_opt75_guardrail(md: str) -> str:
    """Scope the rendered assertions to OPT75's OWN guardrail bullet.

    Matching the whole rendered document lets vocabulary borrowed from anywhere else
    in the report satisfy the predicate with this guardrail deleted — and this same
    change added a cross-link sentence to OPT22/24/25 that satisfies both predicates
    single-handedly, so mirroring it onto another rendered surface (exactly the sync
    this file already does for the older caveat) would silently defuse both guards.
    The catalog assertion below is section-scoped for the same reason.
    """
    m = re.search(r"^- \*\*Guardrail:\*\*(.*)$", md, re.M)
    assert m, "the rendered report has no Guardrail bullet — did the renderer change?"
    return m.group(1)


def _production_opt75_finding() -> dict:
    """A real OPT75 finding built by the PRODUCTION constructor, so its risk axis comes
    from `_STRUCTURAL_META` — the fixture supplies only measurements, never prose."""
    f = cr._new_structural_finding(
        "OPT75", ".github/workflows/ci.yml", "test", 1,
        evidence=("critical-path check `test` (600s): dominant step `Run integration "
                  "suite` (test, 80% of job `test`)"),
        measured_evidence=None,
        size_note="sized from the dominant step's measured p50",
        decomp={"dominant_step": "Run integration suite", "dominant_category": "test",
                "dominant_p50": 480.0, "dominant_share": 0.8,
                "redundant_ratio": 0.2, "job_p50": 600.0},
    )
    f["wall_clock_p50_s"] = 120.0
    return f


def _render_production_opt75() -> str:
    doc = {
        "repo": "o/r", "scanned_at": "2026-09-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 3, "sample_target": 3, "sample_complete": True,
            "poles": [{"check": "test", "p50_s": 600.0,
                       "workflow_file": ".github/workflows/ci.yml", "job": "test",
                       "dominant_step": "Run integration suite",
                       "dominant_p50_s": 480.0, "dominant_share": 0.8,
                       "steps": [{"step": "Run integration suite", "category": "test",
                                  "p50_s": 480.0}]}]},
        "findings": [_production_opt75_finding()],
    }
    return bp.render(doc, {}, {}, {}, "2026-09-08")


def test_rendered_opt75_handoff_warns_about_preserving_required_coverage():
    md = _rendered_opt75_guardrail(_render_production_opt75())
    assert _has_relocation_coverage_caveat(md), (
        "a rendered OPT75 handoff built from production _STRUCTURAL_META no longer "
        "warns that relocating the dominant step out of a required check must keep "
        "the required coverage (a new required check name, or a required verdict "
        "that inspects the moved job's outcome; a `needs:` edge alone does not gate)."
    )


def test_rendered_opt75_handoff_warns_about_a_skipped_dependent():
    md = _rendered_opt75_guardrail(_render_production_opt75())
    assert _has_dependency_skip_caveat(md), (
        "a rendered OPT75 handoff no longer warns that a dependent skipped by a "
        "failed `needs:` dependency reports skipped, not failed, so the verdict must "
        "run always() AND propagate the dependency results."
    )


def _opt75_catalog_section() -> str:
    """OPT75's own catalog window, so every catalog pin bites on OPT75's prose alone."""
    anchor = "OPT75 — Long Pole: Optimize or Relocate the Dominant Step"
    assert anchor in _CATALOG, "OPT75 heading not found — was the pattern renamed?"
    start = _CATALOG.index(anchor)
    # OPT75 is currently the LAST `### ` pattern in the file, so bounding only on the
    # next `### ` leaves the window running to EOF and silently annexing whatever gets
    # appended later. Bound on the next heading of either level.
    bounds = [i for i in (_CATALOG.find("\n### ", start + 1),
                          _CATALOG.find("\n## ", start + 1)) if i != -1]
    return _CATALOG[start:min(bounds)] if bounds else _CATALOG[start:]


def test_catalog_opt75_carries_both_relocation_explanations():
    section = _opt75_catalog_section()
    assert _has_relocation_coverage_caveat(section), (
        "OPT75's catalog entry lost its preserve-required-coverage explanation"
    )
    assert _has_dependency_skip_caveat(section), (
        "OPT75's catalog entry lost its dependency-failure skip explanation"
    )
    # Behaviour 3: the advisory-only relocation restriction and the unknown-required-
    # status rule stay covered.
    low = re.sub(r"\s+", " ", section).lower()
    assert "advisory" in low and "unknown" in low, (
        "OPT75 lost the advisory-only relocation / unknown-required-status restriction"
    )
    # Behaviour 4: branch-protection changes stay an explicit administrative step the
    # audit does not take.
    assert "admin" in low, (
        "OPT75 no longer states that re-gating is an administrative step"
    )


# The advisory branch's escape hatch: "required in effect" is UNCONDITIONAL (spec
# behaviour 3). Qualifying it on the aggregator propagating its result hands an agent a
# de-scope argument built out of this same section's own trap #2 — "the aggregator uses
# contains(needs.*.result, 'failure'), which does not propagate skipped/cancelled, so the
# upstream job is not required in effect and I may de-scope it under the advisory branch."
# That is exactly the de-scope OPT75's advisory restriction exists to forbid.
_REQUIRED_IN_EFFECT_CONDITIONED = (
    re.compile(r"required aggregator (?:that|which) propagat"),
    re.compile(r"aggregator (?:that|which) propagat[^.]{0,120}?is required"),
)


def _conditions_required_in_effect_on_propagation(text: str) -> bool:
    """True when the prose makes "required in effect" contingent on the aggregator
    propagating its result. Emphasis markers are stripped first so the catalog's
    *italicised* qualifier cannot hide from the pattern."""
    t = re.sub(r"[*`_]", "", re.sub(r"\s+", " ", text)).lower()
    return any(pat.search(t) for pat in _REQUIRED_IN_EFFECT_CONDITIONED)


def test_required_in_effect_is_unconditional_in_catalog_and_guardrail():
    section = _opt75_catalog_section()
    assert not _conditions_required_in_effect_on_propagation(section), (
        "OPT75's catalog entry conditions \"required in effect\" on the aggregator "
        "propagating its result. Spec behaviour 3 states it unconditionally: a "
        "non-required job feeding a required aggregator is required in effect, full "
        "stop. The qualifier lets an agent argue that a partially-propagating "
        "aggregator (this section's own contains(needs.*.result, 'failure') trap) "
        "leaves the upstream job de-scopable under the advisory branch."
    )
    guardrail = _rendered_opt75_guardrail(_render_production_opt75())
    assert not _conditions_required_in_effect_on_propagation(guardrail), (
        "the rendered OPT75 guardrail conditions \"required in effect\" on the "
        "aggregator propagating its result — see the catalog assertion above."
    )
    # Red-proof: the pattern is not vacuous — it fires on the phrasing it forbids.
    assert _conditions_required_in_effect_on_propagation(
        "a non-required job feeding a required aggregator *that propagates its "
        "result* is required *in effect*")
    assert not _conditions_required_in_effect_on_propagation(
        "a non-required job feeding a required aggregator is required *in effect*")


def test_relocation_predicates_discriminate_absent_from_present():
    """Red-proof: neither new predicate may be a tautology. Prose that describes only
    the DEFAULT dependency skip, or only a verdict that runs always(), must not pass;
    only prose that also propagates the dependency results does."""
    default_skip_only = ("When a job in the needs: list fails, the dependent job is "
                         "skipped and reports skipped rather than failed.")
    assert not _has_dependency_skip_caveat(default_skip_only)
    always_only = ("Give the verdict job if: always() so it runs even when an "
                   "upstream job fails.")
    assert not _has_dependency_skip_caveat(always_only)
    propagating = default_skip_only + (
        " So the verdict job must run with if: always() AND read every "
        "needs.<job>.result, failing unless each required upstream result is success.")
    assert _has_dependency_skip_caveat(propagating)

    needs_edge_only = ("Add a needs: edge from the new job so it runs before the "
                       "existing check.")
    assert not _has_relocation_coverage_caveat(needs_edge_only)

    # The case that matters most: prose asserting the OPPOSITE of the guardrail. A
    # predicate keyed on tokens that merely co-occur somewhere in the passage accepts
    # this, i.e. it green-lights the exact advice it exists to forbid.
    inverted = ("A job is skipped when its needs: dependency is skipped. Just add "
                "if: always() to the verdict job and it will report the correct "
                "result. Nothing else is required; branch protection needs no change "
                "and a needs: edge alone is fine.")
    assert not _has_relocation_coverage_caveat(inverted)
    assert not _has_dependency_skip_caveat(inverted)
    full = needs_edge_only + (
        " A needs: edge alone does not gate merges. Keep the required check name on a "
        "verdict job that inspects the relocated job's outcome and rejects a failed "
        "dependency, or have an admin add the new job's check name to branch "
        "protection as a required check.")
    assert _has_relocation_coverage_caveat(full)
