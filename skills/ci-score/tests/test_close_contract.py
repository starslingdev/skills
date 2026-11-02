"""The relocated close-honesty pin (moved from ci-speedup's test_close_guidance
by the score-ectomy, 2026-07-16).

ci-score's close opens with the CI Score read straight off the stamp and NEVER
fabricates a score — the no-score cases (refusal / `ci_score_error` / absent
stamp) are each handled honestly. The close is LLM-driven, so this prose is the
only guardable artifact for the honesty property. The full close/kickoff
protocol was extracted to `references/close-contract.md` (2026-07-30,
behavior-neutral), where these honesty rules now live verbatim; this pin reads
the reference so the phrases stay guarded at their new home.

This module now holds two tests with two targets: the honesty pin above reads
the reference, and `test_skill_md_keeps_the_entrypoint_handoff_to_the_reference`
reads `SKILL.md` to guard the entrypoint handoff (link + read instruction +
point-of-use invariants) so the relocated protocol can't become present but
undiscoverable.
"""
from __future__ import annotations

import re
from pathlib import Path

_SKILL_MD = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text()
_CLOSE_CONTRACT = (
    Path(__file__).resolve().parents[1] / "references" / "close-contract.md"
).read_text()


def test_skill_md_keeps_the_entrypoint_handoff_to_the_reference():
    """The extraction stays safe only while SKILL.md keeps the entrypoint
    handoff intact: (a) a live link to the reference, (b) the imperative to
    read it before every close, and (c) the point-of-use invariants. Both
    relocated pins now read the reference directly, so without this guard a
    later edit could strip the link / read instruction / invariants from
    SKILL.md and the full protocol would become present but undiscoverable
    while the suite stayed green (Greptile P2, PR #265)."""
    skill = re.sub(r"\s+", " ", _SKILL_MD)
    # (a) the reference is linked, so an agent can find it.
    assert "references/close-contract.md" in skill
    # (b) the imperative read-before-composing instruction survives.
    assert "READ" in skill
    assert "BEFORE composing the close" in skill
    # (c) the point-of-use invariants section stays, with a representative,
    # live-miss-derived set of its non-negotiables at the entrypoint.
    assert "## Close contract — invariants" in skill   # the section heading itself
    assert "copy it VERBATIM" in skill                 # banner-verbatim
    assert "Never narrate the menu in prose" in skill  # menu-narration ban
    assert "Only the save pick writes the report" in skill


def test_close_surfaces_the_ci_score_honestly():
    close = re.sub(r"\s+", " ", _CLOSE_CONTRACT)
    # Opens with the score, read from the stamp — never invented or recomputed.
    assert "Open with the CI Score" in close
    assert "never invent or recompute a score" in close
    # The anti-fabrication clause and all three no-score cases are pinned, so a
    # future edit can't silently drop the honest handling and stay green.
    assert "Never fabricate a score" in close
    assert "states the refusal reason instead of a score" in close  # refusal
    assert "data_sources.ci_score_error" in close                   # scoring failed
    assert "omits the score line" in close                          # absent stamp


def test_experimental_question_tool_is_mirrored_in_the_plain_message():
    """Mirror of ci-speedup's pin (live Codex run, 2026-10-29): the experimental
    `request_user_input` tool can be called and never render, stranding the user.
    The shared contract must mirror the question + options in the plain message
    and forbid claiming to wait on a choice the user cannot see."""
    start = _SKILL_MD.index("**Interaction contract")
    end = _SKILL_MD.index("**agent-independent**", start)
    contract = re.sub(r"\s+", " ", _SKILL_MD[start:end]).replace("**", "")
    # Long contiguous phrases: a shortened, negated or re-scoped rule goes red.
    assert "call it when exposed, never predict it absent" in contract
    assert ("and ALSO end your plain message with the same question as a "
            "numbered option list, same options, same order, sent before the "
            "call") in contract, "mirror missing, re-scoped, or sent after a call that may hang"
    assert ("Never say you are waiting on a choice unless its options are "
            "visible in your own last message.") in contract
    assert "(always ≥2 options unless a step below drops one by name)" in contract
    assert ("the close's save option still last and verbatim "
            "(`None, just save the report (.md)`)") in contract


def test_every_close_delivery_passage_carries_the_experimental_tool_mirror():
    """The interaction contract gained the experimental-tool mirror, but the
    step-4 kickoff, the close-invariants bullet and references/close-contract.md
    still split delivery into "tool where one exists" vs "printed list where none
    does". Followed literally, a Codex close calls the experimental tool, prints
    no list, and strands the user when the tool fails to render. Every passage
    that states that split must also carry the mirror rule."""
    passages = {
        "SKILL.md step-4 kickoff": _SKILL_MD[
            _SKILL_MD.index("a SEPARATE structured-question tool call"):
            _SKILL_MD.index("Ending the turn after (a)")],
        "SKILL.md close invariants": _SKILL_MD[
            _SKILL_MD.index("**The close = the close text, THEN the question"):
            _SKILL_MD.index("- **Menu shapes.**")],
        "close-contract.md": _CLOSE_CONTRACT[
            _CLOSE_CONTRACT.index("**End the close BY ASKING the question"):
            _CLOSE_CONTRACT.index("What never completes it")],
    }
    for name, text in passages.items():
        flat = re.sub(r"\s+", " ", text)
        assert "experimental" in flat and "ALSO print" in flat, (
            f"{name}: delivery split omits the experimental-tool mirror "
            "(an unrendered Codex question tool leaves no visible options)")
