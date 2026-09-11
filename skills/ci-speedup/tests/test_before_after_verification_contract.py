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

Where a rule is pinned by meaning rather than by phrasing, the check is
**closed-world**: it finds every sentence in the doc that touches the rule and
requires each one to state or withhold it. The open-world shape — listing the
verbs or sentence templates a violation might use — was tried first and loses to
the first wording nobody listed ("the delta serves as a floor", "`reduced` also
qualifies for the headline", "only after the user approves"). Closed-world costs
a false positive when the doc says something true in a shape the sweep does not
recognise; that is the trade, and it is the safe direction for a contract pin.
A known one: a prohibition opening with a bare "No ..." ("No reader can treat the
delta as a lower bound") reads to the sweep as a sentence about a floor that does
not refuse one. Widening the refusal words to a bare "no" was tried and rejected
— it exempts "With no confound present, the delta is a floor", which is the
inversion itself. Reword the doc rather than loosen the sweep.
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

#: The doc with fenced code blocks blanked out. Section slicing counts markdown
#: headings, and a ``# `` line inside a fence is not one — it would truncate a
#: section early and leave its pins reading a prefix of the text they name.
_SPEC_NO_FENCES = re.sub(
    r"^```.*?^```", lambda m: "\n" * m.group(0).count("\n"), _SPEC,
    flags=re.MULTILINE | re.DOTALL,
)

#: Words that turn a sentence about a rule into a statement WITHHOLDING it.
#: The prose pins below are closed-world — every sentence that touches a rule
#: must either restate it or refuse it — so this is the list that decides which.
#: Matched on WORD boundaries. Substring matching reads "whenever" as "never"
#: and quietly exempts the sentence it was meant to catch.
_WITHHOLDING = re.compile(
    r"(?i)\b(never|not|cannot|no floor|no lower bound|disallow\w*|withhold\w*|"
    r"refuse\w*|denied|unsupported)\b|n't\b"
)


def _sentences(text: str) -> list[str]:
    """The text's sentences, each flattened to one line.

    Pass RAW text (`_SPEC`, `_raw_section`), never `_section`: the markdown line
    structure is what separates a bullet, a table and a heading from the prose
    after them, and `_section` has already flattened it away.

    Prose rules live in sentences, so a sentence is the unit a pin can hold. A
    doc-wide substring search cannot tell "never a floor" from "a floor";
    splitting first and then asking what each sentence does is what makes the
    checks below closed-world rather than a list of forbidden phrasings.
    """
    # Split on markdown block starts BEFORE splitting on punctuation. A table
    # row or a bullet often carries no sentence-final punctuation at all, so
    # flattening first glues it to the next block — and if that block happens to
    # be a withholding sentence, the glued unit inherits its "not"/"disallow"
    # and the sweep reads a grant as a refusal.
    units: list[str] = []
    for line in text.splitlines():
        is_row = line.lstrip().startswith("|")
        # A table is one unit. Row-per-unit would let "| clean attribution |" and
        # "| `reduced` | yes |" sit in different units, so neither one names both
        # the rule and the state it grants it to.
        if is_row and units and units[-1].lstrip().startswith("|"):
            units[-1] += " " + line.strip()
        elif re.match(r"\s*([-*+]\s|\d+\.\s|\||#{1,6}\s|>)", line) or not units:
            units.append(line)
        elif not line.strip():
            units.append("")
        else:
            units[-1] += " " + line
    out: list[str] = []
    for unit in units:
        flat = re.sub(r"\s+", " ", unit).strip()
        if not flat:
            continue
        out += [s.strip() for s in re.split(r"(?<=[.:;])\s+(?=[A-Z`\"*(-])", flat) if s.strip()]
    return out


#: Phrases by which the doc states a rule's EXCLUSIVITY rather than refusing it
#: ("only `same` supports a clean claim, and any other state is reported with
#: its confound"). Such a sentence names every state and grants to none of them,
#: so it is as compliant as a withholding one.
_EXCLUSIVE = re.compile(
    r"(?i)only `same`|any other state|every other state|and no other"
)


def _withholds(sentence: str) -> bool:
    return bool(_WITHHOLDING.search(sentence) or _EXCLUSIVE.search(sentence))


def _raw_section(heading: str) -> str:
    """The doc text under `heading`, up to the next heading of the same or a
    higher level (so a `##` section carries its `###` subsections with it).
    `heading` is matched as a line prefix, so a section is addressable without
    pinning the date its heading carries.

    Section scoping is what makes a pin local. A phrase asserted against the
    whole file can be satisfied by an unrelated occurrence elsewhere — which is
    exactly how "exact remote head SHA" survives rewriting eligibility item 2.
    """
    body = _SPEC_NO_FENCES
    hits = re.findall(rf"^{re.escape(heading)}.*$", body, re.MULTILINE)
    assert hits, f"no heading starts with {heading!r}"
    # Exactly one, or the slice is forgeable: `re.search` takes the FIRST
    # prefix match, so planting an earlier "### Eligibility (superseded)" that
    # carries the pinned wording captures every section-scoped pin and frees the
    # real section to be rewritten. Ambiguity is the bug, so it is the failure.
    assert len(hits) == 1, (
        f"heading {heading!r} matches {len(hits)} headings ({hits!r}); a "
        "section-scoped pin cannot say which one it is reading"
    )
    m = re.search(rf"^{re.escape(heading)}.*$", body, re.MULTILINE)
    level = len(heading) - len(heading.lstrip("#"))
    rest = body[m.end() :]
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

    # ...and nowhere may the doc GRANT a floor. Listing the verbs a grant might
    # use is an open-world check and loses: "the delta serves as a floor",
    # "treat the delta as a minimum", "the fix is worth at least the delta" name
    # no modal verb at all and would each walk through such a list. Invert it —
    # find every sentence that talks about a floor AT ALL and require each one
    # to be withholding one. A new sentence granting a floor has to be written
    # as a grant, so it arrives without a withholding word and reddens; and the
    # inversion also stops the old check's false positive, where the correct
    # sentence "the delta may NOT be reported as a floor" read as a permission.
    floor_talk = [
        s
        for s in _sentences(_SPEC)
        if re.search(r"(?i)\b(lower bound|floor|worth at least|at least this "
                     r"big|as a minimum|bounds? the [a-z ]+ from below)\b", s)
    ]
    assert floor_talk, (
        "the doc no longer says anything about a floor under the fix's "
        "benefit; the prohibition has been deleted rather than weakened"
    )
    for sentence in floor_talk:
        assert _withholds(sentence), (
            "a sentence lets the observed delta stand as a floor under the "
            f"fix's benefit: {sentence!r}"
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
    assert granting, "no attribution-granting clause found to check for exclusivity"
    for clause in granting:
        # A clause naming NO state is left to the doc-wide sweep below; demanding
        # every `unlocks` clause name `same` reddens on a correct sentence like
        # "the gate unlocks the headline claim". What is out of contract is a
        # clause that names a state OTHER than `same`.
        named = {s for s in _STATES if s in clause}
        assert named <= {"`same`"}, (
            "a state other than `same` appears in an attribution-granting "
            f"clause: {clause!r} names {sorted(named)}"
        )

    # The two shapes above are the doc's current wording, so pinning only those
    # enumerates two ways to grant the headline and misses every other: a new
    # bullet, a new sentence, or a table row saying `reduced` qualifies is
    # invisible to them. Sweep the whole doc instead — any sentence that talks
    # about the clean attribution and names a state other than `same` must be
    # WITHHOLDING it, which is what "`same`, and no other" means as a rule.
    attribution_talk = [
        s
        for s in _sentences(_SPEC)
        if re.search(r"(?i)(clean speedup|clean attribution|clean ci-only "
                     r"attribution|same work, faster|headline)", s)
    ]
    assert attribution_talk, "the doc no longer states the clean-attribution rule"
    # ...and the exclusivity phrasing that exempts a sentence from the sweep may
    # not itself be widened: "only `same` and `reduced`" reads as exclusive to
    # the sweep while granting to two states.
    widened = re.findall(r"only `same`[^.]{0,40}", _FLAT)
    for clause in widened:
        assert not ({s for s in _STATES if s in clause} - {"`same`"}), (
            f"the `same`-only exclusivity has been widened: {clause!r}"
        )
    for sentence in attribution_talk:
        named = {s for s in _STATES if s in sentence}
        if named <= {"`same`"}:
            continue
        assert _withholds(sentence), (
            "a sentence grants the clean same-work attribution to a state "
            f"other than `same`: {sentence!r} names {sorted(named)}"
        )


# --------------------------------------------------------------------------
# 2. Trigger and resume
# --------------------------------------------------------------------------


def test_eligibility_binds_to_an_exact_remote_head_sha():
    assert "exact remote head SHA" in _FLAT
    assert "authorized commit/push" in _FLAT

    # "exact remote head SHA" occurs twice (the phase-7 placement section states
    # the trigger condition too), so the flat pin above survives rewriting the
    # eligibility item itself to bind to a branch name. "authorized commit/push"
    # happens to occur once today, which pins item 1 only by luck — a second
    # occurrence anywhere would unpin it. So pin BOTH items where they live.
    eligibility = _section("### Eligibility")
    assert "The **authorized commit/push** has occurred" in eligibility, (
        "eligibility item 1 no longer requires the authorized commit/push"
    )
    assert "bound to an **exact remote head SHA**, not a branch name" in eligibility, (
        "eligibility item 2 no longer binds the fix to an exact remote head SHA"
    )
    assert "A branch name is a moving target" in eligibility
    # Closed-world: the two sentences above are the ONLY places eligibility may
    # mention a branch name. An exception appended to the item ("where a SHA is
    # unavailable, the branch name is an acceptable substitute") leaves both
    # phrases intact and undoes the rule they state.
    stray = [
        s
        for s in _sentences(_raw_section("### Eligibility"))
        if "branch name" in s
        and "**exact remote head SHA**, not a branch name" not in s
        and "A branch name is a moving target" not in s
    ]
    assert not stray, (
        f"eligibility mentions a branch name outside the binding rule: {stray!r}"
    )


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
    so it cannot see a decision moved by amended text elsewhere. Pinned here are
    the amendment sentences that carry a locked NUMBER or a locked automatic/
    manual choice in their own words — the ones where a rewrite changes spend or
    changes who starts the phase. Other restatements defer to the decisions
    generically and are covered by `test_amendments_defer_to_the_locked_decisions`.

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
    # Naming four gating phrases enumerates four ways to gate the start, and a
    # fifth wording ("only after the user approves", "requires an explicit
    # re-invocation", "user sign-off") inverts Decision 4 with the pin green.
    # Pin the pairing instead: no sentence in this section may put a user's
    # permission in the way of the phase starting, however that is worded.
    starts = re.compile(
        r"(?i)\b(start|starts|starting|begin|begins|continue|continuing|"
        r"proceed|proceeds|dispatch|dispatched|sampl\w+|phase 7|the runs)\b"
    )
    gating = re.compile(
        r"(?i)(\bask\w*\b|\bconfirm\w*|\bapprov\w*|\bconsent\w*|"
        r"\bprompt\w*|\bpermission\b|\bawait\w*|\bsign-?off\b|"
        r"\bgo-?ahead\b|\bmanual\w*|\bre-?invocation\b|\bre-?invoke\w*)"
    )
    gated = [
        s
        for s in _sentences(_raw_section("## Trigger and resume contract"))
        if starts.search(s) and gating.search(s)
    ]
    assert not gated, (
        "the trigger contract now gates phase 7's start on a user action "
        f"(Decision 4 is automatic-with-disclosed-cost): {gated!r}"
    )

    # Decision-adjacent: the doc's evidence rule is that equal job counts do not
    # prove equal work. A sentence that classifies `same` from job counts alone
    # reinstates the universal label this whole amendment removed, and it can be
    # added far from any pinned phrase — so every sentence about job counts must
    # be a withholding one.
    for sentence in _sentences(_SPEC):
        if not re.search(r"(?i)\bjob counts?\b", sentence):
            continue
        assert _withholds(sentence), (
            "a sentence infers a workload state from job counts, which the doc "
            f"elsewhere says does not prove equal work: {sentence!r}"
        )

    triggering = _section("## Triggering the runs (Decision 3)")
    assert "counts the automatic run among the N" in triggering, (
        "the bootstrap clause no longer counts the automatic branch run toward "
        "N, which raises the sampling spend Decision 1 locked"
    )
    assert "it does not raise or lower N" in triggering

    # Decision 1 + 2 again, in the clause that governs a resample after the head
    # moves: re-scoping THAT to its own N or its own threshold is a spend change
    # the Decisions section would still read as locked.
    assert "under the same locked N and threshold" in _FLAT, (
        "a resample after the head moves no longer runs under the locked N and "
        "variance threshold (Decisions 1 and 2)"
    )


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
