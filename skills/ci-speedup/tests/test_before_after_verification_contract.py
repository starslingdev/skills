"""Contract pins for the phase-7 before/after verification methodology doc.

`references/before-after-verification-spec.md` is an approved *plan*: it is the
contract a later implementation follows, so its wording is the only thing that
can regress before any code exists. These are content checks over that doc — the
prose-change form of a regression test (proposal §9: contract/content checks
where meaningful, no blanket doc exemption).

Two things are pinned:

1. The 2026-09-08 methodology amendments — the evidence-backed workload states
   that replace the old universal "less work run" label, the confound rule for
   increased/changed/unknown workload, the trigger/resume contract, the
   gate-migration rule, and the bootstrap clarification.
2. That those amendments did NOT quietly rewrite any of the six locked
   decisions. The amendments narrow disclosure; they never move the sampling
   defaults, the variance threshold, the trigger mechanism, or v1's scope.
"""
from __future__ import annotations

import re
from pathlib import Path

_SPEC = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "before-after-verification-spec.md"
).read_text()

# Markdown hard-wraps prose, so a pinned phrase can straddle a newline. Assert
# against a whitespace-flattened copy; only the per-line scan below needs lines.
_FLAT = re.sub(r"\s+", " ", _SPEC)


def _decisions() -> str:
    """The locked "Decisions" section, to the next top-level heading."""
    start = _SPEC.index("## Decisions (locked)")
    end = _SPEC.index("\n## ", start + 1)
    return _SPEC[start:end]


# --------------------------------------------------------------------------
# 1. The amendments
# --------------------------------------------------------------------------


def test_workload_states_replace_the_universal_less_work_label():
    """Direction is classified only where evidence supports it, across five
    named states — not collapsed into one "less work run" verdict."""
    for state in ("`same`", "`reduced`", "`increased`", "`changed`", "`unknown`"):
        assert state in _FLAT, f"workload state {state} missing from the methodology"

    # The old label may only survive where the doc says it is being replaced.
    for line in _SPEC.splitlines():
        if "less work run" in line:
            assert "replace" in line.lower(), (
                "the universal 'less work run' label is still used as a verdict: " + line
            )


def test_equal_job_counts_and_sharding_do_not_prove_a_direction():
    assert "Equal job counts alone do not prove equal work" in _FLAT
    assert "sharding alone does not prove coverage reduction" in _FLAT.lower()


def test_confounded_workload_is_never_a_lower_bound():
    """Added or unknown work makes the delta ambiguous in BOTH directions; it
    must never be sold as a floor under the fix's benefit."""
    assert "never a lower bound on the fix's benefit" in _FLAT
    assert re.search(
        r"increased.*changed.*unknown", _FLAT, re.IGNORECASE | re.DOTALL
    ), "the confounded-workload states are not named together"


def test_the_same_work_attribution_gate_is_preserved():
    assert "The same work ran" in _FLAT
    assert "conservative same-work attribution gate" in _FLAT


# --------------------------------------------------------------------------
# 2. Trigger and resume
# --------------------------------------------------------------------------


def test_eligibility_binds_to_an_exact_remote_head_sha():
    assert "exact remote head SHA" in _FLAT
    assert "authorized commit/push" in _FLAT


def test_resume_is_scratch_context_not_a_daemon():
    assert "not a background daemon" in _FLAT
    assert "saved scratch context" in _FLAT


def test_repeat_invocation_does_not_resample():
    assert "must not spend another set of reruns" in _FLAT
    assert "A changed head invalidates in-flight comparison" in _FLAT


def test_phase_six_checkpoint_is_not_bypassed():
    assert "does not authorize bypassing that checkpoint" in _FLAT


# --------------------------------------------------------------------------
# 3. Gate migration
# --------------------------------------------------------------------------


def test_after_metric_describes_the_current_gate():
    assert "The after metric must describe the current merge gate" in _FLAT
    assert "merge-wait change unavailable" in _FLAT
    assert "Do not expand sampling to unrelated workflows" in _FLAT


# --------------------------------------------------------------------------
# 4. Bootstrap
# --------------------------------------------------------------------------


def test_bootstrap_does_not_deadlock():
    assert "deadlock" in _FLAT
    assert "supplies the first sample" in _FLAT


# --------------------------------------------------------------------------
# 5. The six locked decisions survive the amendments
# --------------------------------------------------------------------------


def test_locked_decisions_are_unchanged():
    d = re.sub(r"\s+", " ", _decisions())
    assert "**Default N = 2, adaptive up to 4.**" in d
    assert "**Variance threshold = 20%**" in d
    assert "`CI_SPEEDUP_VERIFY_VARIANCE_PCT`" in d
    assert "never re-push" in d
    assert "not gated" in d
    assert "Env-configurable, out of user-facing SKILL.md" in d
    assert "Wall-clock only in v1" in d


def test_amendments_defer_to_the_locked_decisions():
    """Where an amendment touches a locked decision, the doc must say the
    locked decision governs — the amendments are disclosure-only."""
    assert "the locked decision governs" in _FLAT
    assert "must not replace the locked adaptive threshold" in _FLAT
    assert "not become a significance test" in _FLAT
