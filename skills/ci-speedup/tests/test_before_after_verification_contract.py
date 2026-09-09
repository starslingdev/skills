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
   Where an amendment restates a locked decision in its own words (Decision 4's
   "continue automatically", Decision 1's "counts the automatic run among the
   N"), that sentence is pinned in the section it lives in as well.

Pins are section-scoped wherever a phrase also occurs elsewhere in the doc, and
rules that carry a scope condition are pinned against the scope, not the phrase:
a rule stated as one sentence can be inverted by appending an exception to it,
which leaves every substring pin intact.
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


#: Every workload-state token, as the doc writes them.
_STATES = ("`same`", "`reduced`", "`increased`", "`changed`", "`unknown`")


def _raw_section(heading: str) -> str:
    """The doc text under `heading`, up to the next heading of the same or a
    higher level (so a `##` section carries its `###` subsections with it).
    `heading` is matched as a line prefix, so a section is addressable without
    pinning the date its heading carries.

    Section scoping is what makes a pin local. A phrase asserted against the
    whole file can be satisfied by an unrelated occurrence elsewhere — which is
    exactly how "exact remote head SHA" survives rewriting eligibility item 2.
    """
    m = re.search(rf"^{re.escape(heading)}.*$", _SPEC, re.MULTILINE)
    assert m is not None, f"no heading starts with {heading!r}"
    level = len(heading) - len(heading.lstrip("#"))
    rest = _SPEC[m.end() :]
    nxt = re.search(rf"\n#{{1,{level}}} ", rest)
    return m.group(0) + (rest[: nxt.start()] if nxt else rest)


def _section(heading: str) -> str:
    """`_raw_section`, whitespace-flattened for phrase matching."""
    return re.sub(r"\s+", " ", _raw_section(heading))


def _bullets(heading: str) -> list[str]:
    """The section's markdown bullets, each flattened to a single line.

    Rules that live in one bullet are pinned against that bullet, not the whole
    doc: a scope condition appended to a rule ("...when the workload is X")
    stays inside its bullet, so the bullet is the unit that can prove the rule
    still binds to what it is supposed to bind to.
    """
    body = _raw_section(heading)
    bullets: list[str] = []
    in_bullet = False
    for line in body.splitlines():
        if re.match(r"\s*[-*] ", line):
            bullets.append(line.strip()[2:])
            in_bullet = True
        elif not line.strip():
            in_bullet = False
        elif in_bullet and line.startswith((" ", "\t")):
            bullets[-1] += " " + line.strip()
        else:
            in_bullet = False
    return [re.sub(r"\s+", " ", b) for b in bullets]


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

    # The phrase alone pins nothing about WHICH states the rule binds to: the
    # rule can be inverted by appending a scope condition to the very sentence
    # that states it ("...never a lower bound on the fix's benefit when the
    # workload is `reduced`; for `increased`, `changed`, or `unknown` the delta
    # MAY be reported as a floor under the fix's benefit"). So pin the rule's
    # scope, in its own bullet: it binds to the three confounded states, and to
    # no other.
    rule = [
        b
        for b in _bullets("### Reporting a confounded workload")
        if "never a lower bound on the fix's benefit" in b
    ]
    assert len(rule) == 1, "the never-a-lower-bound rule is not stated in exactly one bullet"
    (rule,) = rule
    for state in ("`increased`", "`changed`", "`unknown`"):
        assert state in rule, (
            f"the never-a-lower-bound rule no longer binds to {state}: {rule!r}"
        )
    assert "`same`" not in rule and "`reduced`" not in rule, (
        "the never-a-lower-bound rule has been re-scoped to a state other than "
        f"the three confounded ones: {rule!r}"
    )

    # ...and nowhere may the doc GRANT a floor, however the grant is phrased.
    # This catches the rewrite by meaning rather than by substring: any clause
    # that permits a delta to be read as a lower bound / floor is out of
    # contract, whatever verb it uses.
    granted = [
        m.group(0)
        for m in re.finditer(
            r"(?i)\b(may|can|could|might|is permitted|is allowed|permissible|"
            r"acceptable)\b[^.;]{0,140}?\b(lower bound|floor)\b",
            _FLAT,
        )
    ]
    assert not granted, (
        f"the doc now permits quoting a floor under the fix's benefit: {granted!r}"
    )

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
    # Anchored to the end of the sentence: an un-anchored prefix match is
    # satisfied by "unless the state is `same` or `reduced`", which grants the
    # clean headline to exactly the state the gate exists to withhold it from.
    assert "do not headline a clean speedup unless the state is `same`." in _FLAT

    # Independently of the sentence above: no state other than `same` may appear
    # in any clause that grants the clean attribution, however that clause is
    # worded or wherever in the doc it is added.
    granting = re.findall(r"unless the state is ([^.]{0,160})\.", _FLAT)
    granting += re.findall(r"([^.]{0,80})\bunlocks\b", _FLAT)
    assert granting, "no attribution-granting clause found to check for exclusivity"
    for clause in granting:
        named = {s for s in _STATES if s in clause}
        assert named == {"`same`"}, (
            "a state other than `same` appears in an attribution-granting "
            f"clause: {clause!r} names {sorted(named)}"
        )


# --------------------------------------------------------------------------
# 2. Trigger and resume
# --------------------------------------------------------------------------


def test_eligibility_binds_to_an_exact_remote_head_sha():
    assert "exact remote head SHA" in _FLAT
    assert "authorized commit/push" in _FLAT

    # Both phrases above occur elsewhere in the doc (the phase-7 placement
    # section states the trigger condition too), so they survive rewriting the
    # eligibility item itself to bind to a branch name. Pin the item where it
    # lives, together with the reason it is a SHA.
    eligibility = _section("### Eligibility")
    assert "bound to an **exact remote head SHA**, not a branch name" in eligibility, (
        "eligibility item 2 no longer binds the fix to an exact remote head SHA"
    )
    assert "A branch name is a moving target" in eligibility


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


def test_locked_decisions_are_pinned_where_the_amendments_implement_them():
    """`test_locked_decisions_are_unchanged` scans only the Decisions section,
    so it cannot see a decision moved by amended text elsewhere. These are the
    two places the amendments implement a locked decision in their own words.

    Decision 4 (automatic, with the cost line disclosed) lives in the trigger
    contract as "continue automatically"; rewriting that to "ask the user to
    confirm before continuing" turns an automatic phase into a gated one while
    the Decisions section still reads as written. Decision 1 (N = 2, adaptive to
    4) lives in the bootstrap clause as "counts the automatic run among the N";
    dropping that turns N into N + 1 runs of spend.
    """
    trigger = _section("## Trigger and resume contract")
    assert "continue automatically when those facts become available" in trigger, (
        "the trigger contract no longer starts phase 7 automatically (Decision 4)"
    )
    assert "that is Decision 4's automatic behavior" in trigger
    for gate in ("ask the user", "confirm before", "await confirmation", "prompt the user"):
        assert gate not in trigger.lower(), (
            f"the trigger contract now gates the automatic start on {gate!r} "
            "(Decision 4 is automatic-with-disclosed-cost)"
        )

    triggering = _section("## Triggering the runs (Decision 3)")
    assert "counts the automatic run among the N" in triggering, (
        "the bootstrap clause no longer counts the automatic branch run toward "
        "N, which raises the sampling spend Decision 1 locked"
    )
    assert "it does not raise or lower N" in triggering


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
