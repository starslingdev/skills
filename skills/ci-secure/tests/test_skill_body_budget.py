"""Progressive-disclosure line budget for ci-secure's SKILL.md.

SKILL.md is loaded into the agent's context on EVERY run, so Anthropic's
skill-authoring guidance caps the body at under 500 lines. ci-secure's body
reached 854 before anyone noticed, precisely because nothing measured it.
ci-speedup has carried this guard since PR #32
(`skills/ci-speedup/tests/test_close_guidance.py`); this is the same predicate
applied to ci-secure so the body cannot silently regrow.
"""

from pathlib import Path

_SKILL_MD = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(encoding="utf-8")

_SKILL_BODY_LINE_BUDGET = 500


def _skill_body_line_count(text: str) -> int:
    """SKILL.md BODY line count = total lines minus the leading YAML frontmatter
    block (the `---` … `---` header, both delimiters inclusive). Matches
    ci-speedup's `_skill_body_line_count` exactly, so the two skills are held to
    one definition of "body". A doc with no frontmatter counts in full."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return len(lines)
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    assert close is not None, "SKILL.md opened frontmatter with `---` but never closed it"
    return len(lines) - (close + 1)


def test_skill_body_stays_under_the_progressive_disclosure_budget():
    """The always-loaded body must stay UNDER 500 lines; deep material belongs in
    `references/`, pulled in on demand instead of paid for on every run."""
    body = _skill_body_line_count(_SKILL_MD)
    assert body < _SKILL_BODY_LINE_BUDGET, (
        f"ci-secure SKILL.md body is {body} lines (>= {_SKILL_BODY_LINE_BUDGET}) — over "
        "the progressive-disclosure budget. Move detail into "
        "skills/ci-secure/references/ (pulled in on demand) instead of growing the "
        "always-loaded SKILL.md body."
    )


def test_skill_body_line_budget_guard_actually_fires_when_over():
    """Red-proof: the SAME frontmatter-stripping + budget predicate must FAIL on a
    synthetic over-budget body, so the guard cannot regress into a tautology."""
    over = "---\nname: x\n---\n" + "\n".join(f"line {i}" for i in range(_SKILL_BODY_LINE_BUDGET))
    body = _skill_body_line_count(over)
    assert body == _SKILL_BODY_LINE_BUDGET          # frontmatter stripped, body isolated
    assert not (body < _SKILL_BODY_LINE_BUDGET)     # exactly at budget → the guard trips
