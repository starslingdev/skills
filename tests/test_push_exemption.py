"""The push exemption is ci-land's alone.

CLAUDE.md, AGENTS.md and the README promise that every skill but `ci-land`
leaves the user's repository untouched: fixes land in the working tree and
nothing is committed, pushed, or posted on a pull request. Until this file that
promise was prose. A skill could grow a `git push` step and every test would
stay green.

This guard reads the text an agent executes (each skill's SKILL.md and
references/) and fails when any skill other than `ci-land` instructs one of the
three writes that promise names: a commit, a push, or opening a pull request.
Test fixtures are out of scope on purpose: ci-score's corpora are other
projects' workflow files and legitimately push.

The logic is a pure function with a positive control, so the guard is checked
against a planted violation on every run rather than trusted to notice one.

Run from the repo root:

    pytest -v tests/test_push_exemption.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SKILLS = _ROOT / "skills"

# The one skill allowed to write to git history and to a pull request.
EXEMPT_SKILL = "ci-land"

# The three writes the README promise names ("nothing is ever committed, pushed,
# or opened as a PR"), as the literals an agent would run. Prose such as "your
# last push" or "nothing is pushed" matches none of them. Kept to the promise on
# purpose: ci-secure's references quote other repos' workflow lines (for example
# a `gh pr comment ... || true` step) as facts, and those are not instructions.
# `git -C <dir> push` is how a skill working in a worktree spells it, so the
# optional `-C <dir>` is part of the shape, not a way around it.
WRITE_INSTRUCTIONS = (
    "git commit",
    "git push",
    "gh pr create",
)

_GIT_C = r"(?:\s+-C\s+\S+)?"
_PATTERN = re.compile(
    r"\bgit" + _GIT_C + r"\s+(?:commit|push)\b" + r"|" + r"\bgh\s+pr\s+create\b"
)


def _canonical(match: str) -> str:
    """Fold `git -C <dir> push` onto `git push` so a hit names the instruction, not the spelling."""
    words = match.split()
    return f"{words[0]} {words[-1]}" if words[0] == "git" else " ".join(words)


def write_instructions_in(text: str) -> list[tuple[int, str]]:
    """Return (1-based line number, matched instruction) for every write instruction in ``text``."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _PATTERN.finditer(line):
            hits.append((lineno, _canonical(match.group(0))))
    return hits


def executed_files(skill_dir: Path) -> list[Path]:
    """The text an agent executes for a skill: SKILL.md plus everything under references/."""
    files = [skill_dir / "SKILL.md"]
    refs = skill_dir / "references"
    if refs.is_dir():
        files.extend(sorted(p for p in refs.rglob("*") if p.is_file()))
    return [p for p in files if p.exists()]


def _skill_dirs() -> list[Path]:
    return sorted(p for p in _SKILLS.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


# --- positive controls: the detector must fire on a planted violation ---------


@pytest.mark.parametrize("instruction", WRITE_INSTRUCTIONS)
def test_detector_fires_on_each_write_instruction(instruction):
    planted = f"Then run `{instruction} ...` to finish.\n"
    assert write_instructions_in(planted) == [(1, instruction)]


def test_detector_ignores_prose_about_pushing():
    prose = (
        "why did CI fail on my last push\n"
        "nothing is ever committed, pushed, or opened as a PR\n"
        "the pusher's login is recorded\n"
        "`gh pr comment --body 'eslint found 3 issues' || true` carries an allowlisted name\n"
    )
    assert write_instructions_in(prose) == []


def test_detector_reports_the_line_number():
    text = "clean\nclean\nrun git push origin HEAD\n"
    assert write_instructions_in(text) == [(3, "git push")]


def test_detector_sees_through_the_worktree_spelling():
    text = 'git -C "$wt" push origin "HEAD:refs/heads/$branch"\ngit -C /tmp/x commit -m fix\n'
    assert write_instructions_in(text) == [(1, "git push"), (2, "git commit")]


def test_detector_ignores_other_git_verbs():
    assert write_instructions_in("git -C $wt fetch origin main\ngit pushd\ngit committed\n") == []


# --- the guard ---------------------------------------------------------------


def test_the_exempt_skill_exists_and_does_write():
    """If ci-land ever stops instructing a push, the exemption in CLAUDE.md is stale."""
    ci_land = _SKILLS / EXEMPT_SKILL
    assert ci_land.is_dir(), f"skills/{EXEMPT_SKILL} is missing; update EXEMPT_SKILL and CLAUDE.md"
    hits = [h for f in executed_files(ci_land) for h in write_instructions_in(f.read_text(encoding="utf-8"))]
    assert "git push" in {h[1] for h in hits}, (
        f"skills/{EXEMPT_SKILL} no longer instructs a push; the exemption no longer describes it")


def test_the_guard_covers_every_other_shipped_skill():
    others = {p.name for p in _skill_dirs()} - {EXEMPT_SKILL}
    assert {"ci-speedup", "ci-score", "ci-secure", "sling"} <= others


def test_no_skill_but_ci_land_instructs_a_write():
    offenders: list[str] = []
    for skill_dir in _skill_dirs():
        if skill_dir.name == EXEMPT_SKILL:
            continue
        for path in executed_files(skill_dir):
            for lineno, instruction in write_instructions_in(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_ROOT)}:{lineno}: `{instruction}`")
    assert not offenders, (
        "A skill other than ci-land instructs a commit, a push, or opening a pull request. "
        "CLAUDE.md promises the push exemption is ci-land's alone; either remove the "
        "instruction or change that promise deliberately:\n  " + "\n  ".join(offenders)
    )
