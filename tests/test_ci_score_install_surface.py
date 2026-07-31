"""Install-surface invariant: ci-score's maintainer-only development data must
live OUTSIDE the installable ``skills/ci-score/`` tree.

The ``skills`` CLI (vercel-labs/skills, ``src/installer.ts``) copies
``skills/<name>/`` recursively into an end-user install, excluding only a small
hardcoded blocklist (``.git``, ``__pycache__``, ``__pypackages__``) — there is
no ``.skillignore`` / frontmatter allowlist / dotfile exclusion. So the ONLY way
to keep ci-score's maintainer-only development data out of an end-user install is
to keep it out of ``skills/ci-score/`` entirely.

Unlike ci-speedup — whose leak vector is *loop infra* (drafting scripts, dogfood
workflow, capture dirs) — ci-score's sensitive data is its **graded third-party
calibration corpus**: dry-run score tables and collected receipts for ~27 named
third-party repositories. Porting any of it would publish third-party grades into
public git history. This is a purpose-built guard for that shape, NOT a
parametrization of ``test_skill_install_surface.py``'s loop-file basename list.

Two independent checks:

1. **Name/path guard** — no directory or file under ``skills/ci-score/`` whose
   name marks it as maintainer-only development data (``calibration``,
   ``collected``, ``dogfood``, ``.ci-score-gaps``, a ``loop``/``loops`` dir, or
   a ``specs`` dir).
2. **Content guard** — no *shipped* file carries a third-party repo **grade
   table**: the calibration dry-run tables are markdown tables whose header row
   pairs a ``Repo`` column with a ``Score`` column. The legitimate per-repo
   ``corpora/*/findings.json`` fixtures are single-repo JSON inputs, not
   multi-repo grade tables, and do not match this signature.

(Repo-wide denylist scanning for internal dev-repo identifiers is already
enforced for every tracked file by
``test_skill_install_surface.py::test_no_internal_identifier_in_tracked_files``;
this file does not duplicate it.)
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "skills" / "ci-score"

# Directory names that mark maintainer-only development data. If a dir with one
# of these names appears anywhere under the installable skill, it ships.
_FORBIDDEN_DIR_NAMES = {
    "calibration",   # the graded third-party corpus (dry-run tables + receipts)
    "collected",     # collected per-repo calibration receipts
    "dogfood",       # dogfood-sweep captures
    "specs",         # internal build/launch planning specs
    "loop",          # self-improvement loop infra
    "loops",
    ".ci-score-gaps",  # runtime gap-capture dir
    ".ci-score-dogfood",
    ".ci-score-loop",
}

# Filename substrings that mark the same data even as a loose file (not a dir).
_FORBIDDEN_FILE_SUBSTRINGS = (
    "calibration",
    "dry-run",
    "dogfood",
)

# A calibration grade table: a markdown header row that pairs a Repo column with
# a Score column, e.g. ``| Repo | Score | Pass/appl | ... |``. Anchored on the
# pipe delimiters so prose mentioning "score" never trips it.
_GRADE_TABLE_HEADER = re.compile(r"\|\s*repo\s*\|\s*score\s*\|", re.IGNORECASE)


def _iter_skill_files():
    for p in _SKILL.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            yield p


def test_no_maintainer_data_dirs_under_skill():
    """No maintainer-only development-data directory may sit under the installable
    skill (the installer would copy it into every end-user install)."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_dir() and p.name.lower() in _FORBIDDEN_DIR_NAMES
    )
    assert not leaked, (
        "maintainer-only development data directory leaked into the installable "
        "skill (relocate outside skills/ci-score/): " + ", ".join(leaked)
    )


def test_no_maintainer_data_files_under_skill():
    """Catch a maintainer-only data file even when it is dropped loose (not inside
    a forbidden dir) — a planted ``calibration.md`` / ``dry-run-*.md`` etc."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _iter_skill_files()
        if any(s in p.name.lower() for s in _FORBIDDEN_FILE_SUBSTRINGS)
    )
    assert not leaked, (
        "maintainer-only development data file leaked into the installable skill "
        "(relocate outside skills/ci-score/): " + ", ".join(leaked)
    )


def test_no_third_party_grade_table_in_shipped_files():
    """No shipped file may carry a third-party repo grade table (the calibration
    dry-run signature: a markdown ``| Repo | Score | ...`` header). Publishing one
    would put graded analyses of named third-party repos into public history."""
    offenders = []
    for p in _iter_skill_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _GRADE_TABLE_HEADER.search(line):
                offenders.append(f"{p.relative_to(_REPO).as_posix()}:{lineno}")
    assert not offenders, (
        "third-party repo grade table leaked into the installable skill — the "
        "calibration corpus must never ship (see maintainers/ci-score/MAINTAINERS.md): "
        + ", ".join(offenders)
    )
