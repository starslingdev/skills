"""Every shipped skill's frontmatter ``description`` fits the 1024-character cap.

The description is the ROUTING surface: it is what decides whether a user's ask
reaches this skill at all, and it is loaded for every skill in every session, so
it is metered rather than free. A description over the cap is not a style
problem — it is rejected or silently truncated mid-sentence at load time, and a
truncated description takes the routing contract's "Do NOT trigger for …" tail
with it, which is exactly the half that keeps three sibling skills from
answering each other's questions.

There was no guard on this and the cap was reached the ordinary way: an
enumeration inside a description grew by one clause. ci-secure's went from 987
to 1037 characters when a vector was renamed to name both of its shapes — over
the cap, in a PR about something else, with a full green suite (it is back
under the cap, at 1019, on this branch's head). This is that
guard, at the repo level rather than per-skill, because the failure mode is
identical for all three and only one of them happened to be near the line.

Measured on the description with its YAML folding undone (a ``>-`` block's
newlines become spaces), which is the string the loader actually sees.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = sorted(p for p in (_REPO / "skills").iterdir() if (p / "SKILL.md").is_file())

# Anthropic's documented cap for a skill's frontmatter description.
_DESCRIPTION_CAP = 1024

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_DESCRIPTION_RE = re.compile(r"^description:\s*(.*?)(?=^[A-Za-z_][\w-]*:|\Z)", re.S | re.M)


def _description(skill: Path) -> str:
    front = _FRONTMATTER_RE.match((skill / "SKILL.md").read_text())
    assert front, f"{skill.name}/SKILL.md has no YAML frontmatter block"
    body = _DESCRIPTION_RE.search(front.group(1))
    assert body, f"{skill.name}/SKILL.md frontmatter has no description:"
    text = body.group(1)
    if text.lstrip().startswith((">", "|")):        # folded / literal block scalar
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return " ".join(text.split())


@pytest.mark.parametrize("skill", _SKILLS, ids=lambda p: p.name)
def test_description_fits_the_cap(skill: Path) -> None:
    length = len(_description(skill))
    assert length <= _DESCRIPTION_CAP, (
        f"{skill.name}'s frontmatter description is {length} characters, over "
        f"the {_DESCRIPTION_CAP}-character cap by {length - _DESCRIPTION_CAP}. "
        f"Trim it — the tail that gets truncated is the routing contract."
    )


@pytest.mark.parametrize("skill", _SKILLS, ids=lambda p: p.name)
def test_description_is_present_and_substantial(skill: Path) -> None:
    """Positive control: the parser above must be reading real text, or the cap
    test would pass vacuously on an empty string for every skill."""
    assert len(_description(skill)) > 200


def test_every_shipped_skill_is_covered() -> None:
    """A skill added without a SKILL.md frontmatter would silently drop out of
    the parametrization above rather than fail it."""
    on_disk = {p.name for p in (_REPO / "skills").iterdir() if p.is_dir()}
    assert {p.name for p in _SKILLS} == on_disk
