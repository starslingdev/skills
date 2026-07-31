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

Independent checks (each is its own test function, so a broken assertion in one
never silently disables the others), plus a positive control that proves the
detectors actually fire (the forbidden corpus is maintainer-local and absent from
this repo, so every "nothing matched" assertion would pass vacuously if a detector
were mangled — the positive control is what keeps them load-bearing):

1. **Name/path guard** — no directory or file under ``skills/ci-score/`` whose
   name marks it as maintainer-only development data (e.g. ``calibration``,
   ``collected``, ``dogfood``, a ``.ci-score-*`` runtime-capture dir, a
   ``loop``/``loops`` dir, or a ``specs`` dir — see ``_FORBIDDEN_DIR_NAMES`` and
   ``_FORBIDDEN_FILE_SUBSTRINGS`` for the exhaustive lists).
2. **Capture-dir catch-all** — no ``.ci-score-*`` runtime-capture dir may sit
   under the skill (the installer ships any such dir, so catching the whole
   prefixed family closes the rename gap the fixed name list leaves open; the
   real-repo checkout fixtures' legitimate ``.github`` dirs are deliberately not
   flagged).
3. **Content guard** — no *shipped* file carries a third-party repo **grade
   table**: the calibration dry-run tables are markdown tables whose header row
   pairs a ``Repo``/``Repository`` column with a ``Score``/``Grade`` column (both
   header spellings occur in the corpus). The legitimate per-repo
   ``corpora/*/findings.json`` fixtures are single-repo JSON inputs, not
   multi-repo grade tables, and do not match this signature.

Note on scope: the *frozen* ``references/ci-score-spec.json`` decision-record
prose and the ``CHANGELOG.md`` legitimately cite a few public positive-control
reference scores (governance receipts, computed from public CI config). Those are
part of the frozen registry and ship by design; what this invariant blocks is the
bulk maintainer **calibration corpus** — the dry-run grade *tables* and collected
per-repo receipts — never entering the installable tree.

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

# Runtime-capture dirs are all named `.ci-score-*` (gaps/dogfood/loop today). The
# installer strips no dotfiles beyond {.git, __pycache__, __pypackages__}, so any
# such dir ships. Catch the whole `.ci-score-*` family — not just the three names
# hardcoded in _FORBIDDEN_DIR_NAMES — so a renamed/new capture dir
# (`.ci-score-cache`, `.ci-score-runs`, …) cannot leak through the fixed list.
# (Scoped to the `.ci-score-*` prefix rather than "every dot-dir" because the
# skill legitimately ships real-repo checkout fixtures that carry a `.github/`
# dir — the workflow files the scorer reads — which must NOT be flagged.)
_CAPTURE_DOTDIR_PREFIX = ".ci-score-"

# Filename substrings that mark the same data even as a loose file (not a dir).
# Every synonym the module docstring uses for the sensitive corpus is covered so a
# benignly-renamed file (`graded-repos.md`, `corpus.md`, `receipts.json`) cannot
# slip the name guard.
_FORBIDDEN_FILE_SUBSTRINGS = (
    "calibration",
    "dry-run",
    "dogfood",
    "collected",
    "grade",
    "corpus",
    "receipts",
)

# A calibration grade table: a markdown header row that pairs a Repo column with
# a Score/Grade column, e.g. ``| Repo | Score | Pass/appl | ... |`` or
# ``| repo | grade | ... |``. Both header spellings appear in the maintainer
# calibration corpus, so the backstop must not hinge on one exact adjacency: a
# line trips only when it carries BOTH a ``| Repo |``/``| Repository |`` cell and
# a ``| Score |``/``| Grade |`` cell (they need not be adjacent). Anchored on the
# pipe delimiters so prose mentioning "score" or "grade" never trips it.
_REPO_COL = re.compile(r"\|\s*repo(?:sitory)?\s*\|", re.IGNORECASE)
_GRADE_COL = re.compile(r"\|\s*(?:score|grade)\s*\|", re.IGNORECASE)


def _is_grade_table_header(line: str) -> bool:
    return bool(_REPO_COL.search(line) and _GRADE_COL.search(line))


def _is_capture_dotdir(name: str) -> bool:
    """A `.ci-score-*` runtime-capture dir (all of which the installer would ship)."""
    return name.startswith(_CAPTURE_DOTDIR_PREFIX)


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
            if _is_grade_table_header(line):
                offenders.append(f"{p.relative_to(_REPO).as_posix()}:{lineno}")
    assert not offenders, (
        "third-party repo grade table leaked into the installable skill — the "
        "calibration corpus must never ship (see maintainers/ci-score/MAINTAINERS.md): "
        + ", ".join(offenders)
    )


def test_no_capture_dotdir_under_skill():
    """A ``.ci-score-*`` runtime-capture dir (gaps/dogfood/loop, or any future
    ``.ci-score-cache``/``.ci-score-runs``) would ship into every install even if it
    is absent from ``_FORBIDDEN_DIR_NAMES``. Catch the whole prefixed family so the
    fixed name list's rename gap is closed."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_dir() and _is_capture_dotdir(p.name)
    )
    assert not leaked, (
        "a .ci-score-* runtime-capture dir would ship into every end-user install "
        "(relocate outside skills/ci-score/): " + ", ".join(leaked)
    )


def test_detectors_actually_fire():
    """Positive control against vacuous passing. Every other test in this file
    asserts "nothing matched" — and because the forbidden corpus is maintainer-local
    and absent from this repo, they would ALSO pass if a detector were silently
    mangled (regex broken, forbidden set emptied). This test pins that each detector
    fires on a known-bad sample, so a broken guard turns CI red instead of green."""
    # Grade-table content guard fires on both real header spellings, adjacent or not,
    # but not on prose.
    assert _is_grade_table_header("| Repo | Score | Pass/appl |")
    assert _is_grade_table_header("| repo | grade | calib row |")
    assert _is_grade_table_header("| Repository | Pass | Score |")  # non-adjacent
    assert not _is_grade_table_header("the score for that repo was high")  # prose
    assert not _is_grade_table_header('{"repo": "x", "score": 83}')  # not a md row
    # Capture-dir catch-all fires on a renamed .ci-score-* dir but spares the
    # legitimate .github fixture dirs (and non-capture dot-dirs generally).
    assert _is_capture_dotdir(".ci-score-cache")
    assert _is_capture_dotdir(".ci-score-gaps")
    assert not _is_capture_dotdir(".github")
    assert not _is_capture_dotdir(".git")
    # Name guards retain their canonical members.
    assert "calibration" in _FORBIDDEN_DIR_NAMES
    assert "specs" in _FORBIDDEN_DIR_NAMES
    for needle in ("calibration", "dry-run", "dogfood", "collected"):
        assert needle in _FORBIDDEN_FILE_SUBSTRINGS
