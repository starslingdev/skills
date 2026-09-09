"""Contract pins for the phase-7 before/after verification methodology doc.

`references/before-after-verification-spec.md` is an approved *plan*: it is the
contract a later implementation follows, so its wording is the only thing that
can regress before any code exists. These are content checks over that doc — the
prose-change form of a regression test: a plan whose only artifact is prose still
gets contract checks over the load-bearing sentences, rather than a blanket
"documentation is exempt from tests".

Two things are pinned:

1. The 2026-09-08 methodology amendments — the evidence-backed workload states
   that replace the old universal "less work run" label, the confound rule for
   increased/changed/unknown workload, the trigger/resume contract, the
   gate-migration rule, and the bootstrap clarification.
2. That the six locked decisions are still stated, verbatim, in the Decisions
   section. Note what that pin does and does not buy: it proves the decisions'
   own WORDING is intact. It cannot detect a decision moved by amended text
   elsewhere in the file — a term redefined, a precondition added — which is why
   the amendment sections carry their own pins above and why the doc states that
   the locked decision governs wherever the two could be read as interacting.
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
    named states — not collapsed into one "less work run" verdict.

    Pin each state's DEFINITION, not just its token. The bare tokens also occur
    in the disclaimer list and the implementation sketch, so a presence check
    survives deleting the definitions outright — which is the half of the
    amendment that carries the meaning.
    """
    for state, defining_phrase in (
        ("`same`", "the same jobs/checks and the same units of work are identified"),
        ("`reduced`", "positive evidence that work present before is absent after"),
        ("`increased`", "positive evidence of work present after that was not present"),
        ("`changed`", "differs in identity without a defensible net direction"),
        ("`unknown`", "the evidence needed to compare workload identity is missing"),
    ):
        assert f"**{state}**" in _FLAT, f"workload state {state} is not defined"
        assert defining_phrase in _FLAT, (
            f"workload state {state} lost its definition: {defining_phrase!r}"
        )

    # `unknown` is the floor, so a classifier that guesses is out of contract.
    assert "`unknown` is the default; a state is only asserted when its evidence exists" in _FLAT

    # The retired label may appear exactly once, in the sentence that retires it.
    # An earlier form of this check allowed any line containing "replace", which
    # a sentence like "replace the state with the less work run label" satisfies.
    assert _FLAT.count("less work run") == 1, (
        "the retired 'less work run' label appears "
        f"{_FLAT.count('less work run')} times; it may appear only where the doc "
        "records that it was replaced"
    )
    assert 'This replaces the earlier universal "less work run" label' in _FLAT


def test_equal_job_counts_and_sharding_do_not_prove_a_direction():
    assert "Equal job counts alone do not prove equal work" in _FLAT
    assert "sharding alone does not prove coverage reduction" in _FLAT.lower()


def test_confounded_workload_is_never_a_lower_bound():
    """Added or unknown work makes the delta ambiguous in BOTH directions; it
    must never be sold as a floor under the fix's benefit."""
    assert "never a lower bound on the fix's benefit" in _FLAT

    # Pin the actual confound sentence. A `.*`/DOTALL search over the flattened
    # doc for the three state names in order proves nothing: the definition list
    # already names them in that order, so it passed even with the whole
    # confounded-reporting rule rewritten away.
    assert (
        "For **`increased`**, **`changed`**, or **`unknown`**, report the observed "
        "timing change *together with the confound*, in the same breath, and stop there."
    ) in _FLAT, "the confounded-workload reporting rule no longer names its three states"

    # A reduced workload is not a clean speedup either — the delta includes work
    # that simply did not run.
    assert "`reduced` is likewise not a clean speedup" in _FLAT

    # The two guards are independent: product code disqualifies attribution even
    # when the workload state is `same`.
    assert "guard 1 and guard 2 are independent and both must pass" in _FLAT


def test_the_same_work_attribution_gate_is_preserved():
    assert "The same work ran" in _FLAT
    assert "conservative same-work attribution gate" in _FLAT

    # The load-bearing half of the gate is its EXCLUSIVITY: `same` is the only
    # state that unlocks a clean claim. Asserting the two phrases above still
    # passes a doc that let `reduced` headline a clean speedup, so pin both the
    # positive grant and the withholding of every other state.
    assert 'Only this state permits a clean "same work, faster" attribution' in _FLAT
    assert (
        "it is `same` that unlocks a clean attribution, and every other state "
        "withholds it"
    ) in _FLAT
    assert "do not headline a clean speedup unless the state is `same`" in _FLAT


# --------------------------------------------------------------------------
# 2. Trigger and resume
# --------------------------------------------------------------------------


def test_eligibility_binds_to_an_exact_remote_head_sha():
    assert "exact remote head SHA" in _FLAT
    assert "authorized commit/push" in _FLAT


def test_resume_is_scratch_context_not_a_daemon():
    assert "not a background daemon" in _FLAT
    assert "saved scratch context" in _FLAT

    # "Not a daemon" is only honest if the output says so too, and if a session
    # that is never re-invoked simply stops rather than accruing samples.
    assert "the output must never imply that something is watching in the background" in _FLAT
    assert "If no one invokes the skill again, no further samples are ever collected" in _FLAT

    # Where the resume state lives is install-surface relevant: scratch context
    # inside tracked or installable content would ship to end users.
    assert (
        "Runtime context is persisted beside the run's scratch artifacts, outside "
        "tracked and installable content"
    ) in _FLAT


def test_resume_context_is_bound_to_the_run_it_describes():
    """Without the binding list, a resumed session cannot tell whether the saved
    samples belong to the head it is now looking at."""
    binding = _FLAT[_FLAT.index("### Context binding"):]
    binding = binding[: binding.index("###", 3)] if "###" in binding[3:] else binding
    for item in (
        "repository",
        'the baseline artifact the "before" came from',
        "the **fix head SHA**",
        "workflow / check identity being sampled",
    ):
        assert item in binding, f"context binding no longer includes {item!r}"


def test_repeat_invocation_does_not_resample():
    assert "must not spend another set of reruns" in _FLAT
    assert "A changed head invalidates in-flight comparison" in _FLAT

    # Reuse must not outrank the kill switch: `0` turns the phase off, so a
    # saved verdict is not rendered either.
    assert "`CI_SPEEDUP_VERIFY_RUNS=0` disables phase 7 outright" in _FLAT


def test_dispatch_sampling_cannot_drift_off_the_bound_head():
    """`workflow_dispatch` takes a ref, so it builds whatever the ref points at
    when it starts — the one sampling path that can silently swap the commit out
    from under the exact-head binding."""
    assert "Dispatch takes a ref, not a SHA" in _FLAT
    assert "discard any sample that does not match" in _FLAT
    assert "head_sha" in _FLAT, "no instruction to check the dispatched run's head SHA"


def test_phase_six_checkpoint_is_not_bypassed():
    assert "does not authorize bypassing that checkpoint" in _FLAT


# --------------------------------------------------------------------------
# 3. Gate migration
# --------------------------------------------------------------------------


def test_after_metric_describes_the_current_gate():
    assert "The after metric must describe the current merge gate" in _FLAT
    assert "Do not expand sampling to unrelated workflows" in _FLAT

    # The prohibition itself, not just the escape hatch. Asserting only the
    # phrases above left the load-bearing "Never" free to become "Always".
    assert "Never report a former pole's improvement as merge-wait improvement" in _FLAT

    # "unavailable" is stated in two places (the rule and the degradation table);
    # pin both, so deleting either one is caught.
    assert _FLAT.count("merge-wait change unavailable") >= 2, (
        "the merge-wait-unavailable fallback is no longer stated in both the "
        "gate-migration rule and the degraded-output list"
    )


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
