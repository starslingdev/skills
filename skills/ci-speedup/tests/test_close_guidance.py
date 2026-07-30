"""SKILL-lint for the phase-6 close's plain-language rules (UX pass, 2026-07-15).

A UX review of a real `/ci-speedup` run (read by a PM, not an engineer) found the
chat close speaking engineer: internal catalog OPT-ids, unglossed jargon, and
AskUserQuestion labels naming "pole". These are guidance-only invariants — they pin
the SKILL.md wording that keeps the close plain, so a future edit can't quietly
regress it back to codes-and-jargon.
"""
from __future__ import annotations

import re
from pathlib import Path

_SKILL_MD = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text()


def _phase6() -> str:
    """The phase-6 close section — from its '6. **Present & hand off' heading up to
    the `scripts/run.py orchestrates` sentence that begins the trailing orchestration
    prose after the numbered phases. That sentence (not the end of '## Phases') is the
    boundary: it deliberately excludes the descriptive prose so these pins cover only
    the phase-6 *instructions*. A new close rule added AFTER that sentence but still
    inside '## Phases' would fall outside this slice and go unlinted — keep close
    guidance above the boundary."""
    start = _SKILL_MD.index("6. **Present & hand off")
    end = _SKILL_MD.index("`scripts/run.py` orchestrates the deterministic phases", start)
    return _SKILL_MD[start:end]


def test_close_guidance_bans_opt_ids_in_the_chat():
    close = _phase6()
    # The rule itself must be stated...
    assert "NEVER surface an internal" in close and "OPT-id" in close
    # ...and the close guidance must not model an OPT-id as example close text.
    # (OPT-ids legitimately appear elsewhere in SKILL.md — this is scoped to the close.)
    assert not re.search(r"e\.g\.[^.]*OPT\d", close), \
        "the close guidance models an OPT-id in user-facing example text"


def test_close_guidance_glosses_jargon():
    close = re.sub(r"\s+", " ", _phase6())
    assert "the slowest check gating your merge" in close, "'pole' is not glossed"
    assert "cloud CI billing minutes" in close, "'runner-minutes' is not glossed"
    assert 'Avoid "lever" and "critical path"' in close


def test_askuserquestion_label_is_plain():
    close = re.sub(r"\s+", " ", _phase6())
    # Label = plain check name + measured wait; never "pole"/OPT-id in a label.
    assert "label is the plain check name" in close
    assert "Fix the test check (8m36s wait)" in close


def test_close_sets_honest_pick_expectation_not_a_proposal():
    close = re.sub(r"\s+", " ", _phase6())
    assert "makes the change and verifies it" in close
    assert "before committing or opening a PR" in close
    assert 'doesn\'t return a "proposal"' in close


def test_close_quotes_one_canonical_merge_wait_figure():
    close = re.sub(r"\s+", " ", _phase6())
    assert "Quote the report's merge-wait figure verbatim" in close
    assert "one canonical value" in close


# --- Issue #18: the full markdown report is opt-in (not the default deliverable) ---

def test_close_saves_the_report_via_the_always_last_option():
    close = re.sub(r"\s+", " ", _phase6())
    # The save is FUSED into the always-last option, verbatim-labelled. The old
    # standalone "Save the full report" option is gone; if it (or any two-question /
    # overflow split that hid it in a tab) comes back, these pins go red.
    assert "None, just save the report (.md)" in close, \
        "the verbatim always-last save option label is missing"
    assert "last option is ALWAYS" in close, "the save option's always-last placement is not pinned"
    assert "opt-in" in close and "fused into that last option" in close
    assert "save the full report" not in close.lower(), \
        "the removed standalone 'Save the full report' option resurfaced"


def test_close_is_one_question_one_page_never_tabs():
    close = re.sub(r"\s+", " ", _phase6())
    # ONE AskUserQuestion question, ONE page — Claude Code renders extra questions as
    # hidden tabs, which buried the save option on a real run (owner screenshot). A
    # resurrected two-question / Q1+Q2 overflow split must go red here.
    assert "ONE question, ONE page, never multiple questions" in close
    assert "tabs" in close
    assert "Q1" not in close and "Q2" not in close, "the two-question overflow split resurfaced"
    assert "SAME AskUserQuestion call" not in close, "the two-question overflow rule resurfaced"


def test_close_folds_extra_options_to_stay_within_four():
    close = re.sub(r"\s+", " ", _phase6())
    # Total ≤4 including the always-last save option, achieved by folding extra
    # per-pole options into "Fix all" — not by splitting into a second question.
    assert "fold" in close.lower()
    assert "≤4" in close
    assert 'Fix all gating checks' in close


def test_close_two_gating_poles_get_their_own_slots_and_bill_folds_to_prose():
    close = re.sub(r"\s+", " ", _phase6())
    # Live miss 2026-07-30: with exactly TWO poles the ≤4 fold used to collapse the
    # second pole into "Fix all", so a user who'd fixed pole 1 had no button for pole 2.
    # Fixed shape: pole 1 / pole 2 / "Fix both" / save; the bill option folds out to the
    # close prose. These pins keep that shape from regressing while CI stays green.
    assert "exactly TWO gating poles" in close
    assert 'plus "Fix both"' in close, "the two-pole combined 'Fix both' option is missing"
    assert "the bill option folds out to the close prose" in close
    # ≥3 poles keep the OLD fold (top pole + "Fix all"), unchanged by the two-pole rule.
    assert "With ≥3 poles" in close and "Fix all gating checks" in close
    # A folded-out bill must stay named in prose in BOTH the source-backed and modeled
    # cases (not only via the modeled "Also noticed" pointer), so a real R-row saving is
    # never silently dropped from the close.
    assert "named in the close prose either way" in close
    assert "stays reachable by free text" in close


def test_close_does_not_reoffer_the_fix_menu_after_a_save_pick():
    close = re.sub(r"\s+", " ", _phase6())
    # Picking the save option explicitly DECLINES fixes, so the fix menu is NOT
    # re-offered afterward — close with one line naming the remaining levers. The old
    # post-save "re-offer the fix selection" behavior must not come back.
    assert "explicitly" in close and "declined the fixes" in close
    assert "do **NOT** re-offer".replace("**", "") in close.replace("**", "")
    assert "re-offer the fix selection" not in close, \
        "the removed post-save re-offer behavior resurfaced"


def test_close_does_not_surface_the_report_by_default():
    close = re.sub(r"\s+", " ", _phase6())
    # The opening must NOT announce a report was written or point at a file path by
    # default — surfacing into the working tree is conditional on the save option.
    assert "Do NOT** announce that a report was written" in close \
        or "Do NOT announce that a report was written" in close
    assert "not the default deliverable" in close


def test_close_report_is_still_verified_internally_unconditionally():
    close = re.sub(r"\s+", " ", _phase6())
    # Declining the option must NOT skip the honesty gate: render + verify still ran
    # internally on this run regardless of the user's pick.
    assert "rendered and verify-gated" in close.replace("**", "")
    assert "unconditional" in close


# --- Progressive-disclosure line budget (#17, pinned by PR #32 at 498/500) -----------

_SKILL_BODY_LINE_BUDGET = 500


def _skill_body_line_count(text: str) -> int:
    """SKILL.md BODY line count = total lines minus the leading YAML frontmatter block
    (the `---` … `---` header, both delimiters inclusive). This is how PR #32 counted the
    body at 498 when it set the <500 budget. A doc with no frontmatter counts in full."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return len(lines)
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    assert close is not None, "SKILL.md opened frontmatter with `---` but never closed it"
    return len(lines) - (close + 1)


def test_skill_body_stays_under_the_progressive_disclosure_budget():
    """SKILL.md is the always-loaded context; deep material belongs in `references/` so it's
    pulled in on demand, not paid for on every run (#17). The body must stay UNDER 500 lines."""
    body = _skill_body_line_count(_SKILL_MD)
    assert body < _SKILL_BODY_LINE_BUDGET, (
        f"SKILL.md body is {body} lines (>= {_SKILL_BODY_LINE_BUDGET}) — over the "
        "progressive-disclosure budget (#17). Move detail into "
        "skills/ci-speedup/references/ (pulled in on demand) instead of growing the "
        "always-loaded SKILL.md body."
    )


def test_skill_body_line_budget_guard_actually_fires_when_over():
    """Red-proof: the SAME frontmatter-stripping + budget predicate must FAIL on a synthetic
    over-budget body, so the guard can't silently regress into a tautology that always passes."""
    over = "---\nname: x\n---\n" + "\n".join(f"line {i}" for i in range(_SKILL_BODY_LINE_BUDGET))
    body = _skill_body_line_count(over)
    assert body == _SKILL_BODY_LINE_BUDGET          # frontmatter stripped, body isolated
    assert not (body < _SKILL_BODY_LINE_BUDGET)     # exactly at budget → the guard trips
