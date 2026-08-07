"""Install-surface invariant: maintainer-only loop infra must live OUTSIDE the
installable ``skills/ci-speedup/`` tree.

The ``skills`` CLI (vercel-labs/skills, ``src/installer.ts``) copies
``skills/<name>/`` recursively into an end-user install, excluding only a small
hardcoded blocklist — there is no ``.skillignore`` / frontmatter allowlist / dotfile
exclusion. So the ONLY way to keep maintainer-only loop files (the gap→catalog,
transcript, and dogfood loops) out of an end-user install is to keep them out of
``skills/ci-speedup/`` entirely, in a sibling ``maintainers/ci-speedup/`` tree the
installer never sees.

This makes that boundary a PASS/FAIL invariant instead of a convention that drifts:
the leak it guards already regrew once — after the dir was first curated, #67/#68/#69
added loop tests/scripts (``dogfood-retry.test.mjs``, ``aggregate_lessons.py``, …)
straight back into ``skills/ci-speedup/``. A name-pattern catch-all below trips on the
*next* such file even if it isn't in the explicit list yet.

Scope: ci-speedup only. Each sibling skill carries its own purpose-built guard,
because each one's leak shape differs — ``test_ci_score_install_surface.py`` (the
graded third-party calibration corpus) and ``test_ci_secure_install_surface.py``
(loop infra, runtime captures, and workflow-shaped fixtures tracked at real
``.github/`` paths). This file is deliberately not parametrized over them.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "skills" / "ci-speedup"
_MAINT = _REPO / "maintainers" / "ci-speedup"

# Basenames of the relocated maintainer-only loop infra. A file with one of these names
# under skills/ci-speedup/ is a leak; a shipped-doc markdown link to one of them dangles
# in an install (the file isn't copied).
_MOVED_NAMES = {
    "MAINTAINERS.md",
    "gap-to-catalog-prompt.md",
    "loop-analysis-prompt.md",
    "loop-summary.schema.json",
    "draft_detector.py",
    "aggregate_lessons.py",
    "ci-speedup-dogfood.js",
    "ci-speedup-dogfood.command.md",
    "test_draft_detector.py",
    "test_aggregate_lessons.py",
    "test_loop_summary_infra.py",
    "dogfood-retry.test.mjs",
    "test_dogfood_retry_node.py",
}


def test_skill_dir_has_no_maintainer_only_files():
    """No maintainer-only loop infra (by exact path) may sit under the installable skill."""
    forbidden = [
        _SKILL / "MAINTAINERS.md",
        _SKILL / "workflows",                                # the whole dogfood workflows/ dir
        _SKILL / "scripts" / "draft_detector.py",
        _SKILL / "scripts" / "aggregate_lessons.py",
        _SKILL / "references" / "gap-to-catalog-prompt.md",
        _SKILL / "references" / "loop-analysis-prompt.md",
        _SKILL / "references" / "loop-summary.schema.json",
        _SKILL / "tests" / "test_draft_detector.py",
        _SKILL / "tests" / "test_aggregate_lessons.py",
        _SKILL / "tests" / "test_loop_summary_infra.py",
        _SKILL / "tests" / "dogfood-retry.test.mjs",
        _SKILL / "tests" / "test_dogfood_retry_node.py",
    ]
    leaked = [p.relative_to(_REPO).as_posix() for p in forbidden if p.exists()]
    assert not leaked, (
        "maintainer-only loop infra leaked back into the installable skill dir "
        "(relocate it under maintainers/ci-speedup/): " + ", ".join(leaked)
    )


def test_no_loop_named_files_anywhere_under_skill():
    """Catch-all by name: a NEW loop file dropped anywhere under skills/ci-speedup/
    is caught even before it's added to the explicit list above."""
    # This name-based check skips dot-dirs / __pycache__ for ITS OWN scope (relocated loop-file
    # *names*). It does NOT certify dot-dirs as install-safe — the installer strips only
    # {.git, __pycache__, __pypackages__}, so other dot-dirs (the runtime capture dirs) DO ship;
    # that leak is guarded separately by test_install_surface_has_no_runtime_capture_dirs below.
    def _hidden(p: Path) -> bool:
        return any(part.startswith(".") or part == "__pycache__"
                   for part in p.relative_to(_SKILL).parts)

    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_file() and p.name in _MOVED_NAMES and not _hidden(p)
    )
    assert not leaked, (
        "file(s) under skills/ci-speedup/ are maintainer-only loop infra and belong "
        "under maintainers/ci-speedup/: " + ", ".join(leaked)
    )


def test_install_surface_has_no_runtime_capture_dirs():
    """The `skills` installer copies skills/<name>/ recursively, excluding ONLY
    {.git, __pycache__, __pypackages__} (vercel-labs/skills src/installer.ts) — there is NO
    general dotfile exclusion. So any OTHER dot-directory under the skill ships to end users.
    The gap → catalog and transcript loops capture third-party job logs + session transcripts
    (MBs, possibly repo internals) into `.ci-speedup-gaps/` / `.ci-speedup-loop/` at runtime;
    rooted under the skill they would ship. Captures now root OUTSIDE skills/<name>/ at the repo
    root (blocking_path.py `_gaps_root_default`, draft_detector.py `_GAPS_ROOT`). Assert the
    install surface carries no such dot-directory so the leak can't silently regrow a third time
    (#71 moved the loop *infra* out but left the runtime capture dirs; this guards those)."""
    installer_strips = {".git", "__pycache__", "__pypackages__"}
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_dir() and p.name.startswith(".") and p.name not in installer_strips
    )
    assert not leaked, (
        "dot-directory under the installable skill ships to end users (the installer has no "
        "dotfile exclusion) — relocate runtime captures outside skills/ci-speedup/: "
        + ", ".join(leaked)
    )


def test_relocated_infra_actually_present():
    """The flip side — the infra must EXIST under maintainers/ci-speedup/, so the
    absence checks above can't pass vacuously if the files were deleted, not moved."""
    required = [
        _MAINT / "MAINTAINERS.md",
        _MAINT / "loops" / "gap-to-catalog-prompt.md",
        _MAINT / "loops" / "loop-analysis-prompt.md",
        _MAINT / "loops" / "loop-summary.schema.json",
        _MAINT / "scripts" / "draft_detector.py",
        _MAINT / "scripts" / "aggregate_lessons.py",
        _MAINT / "workflows" / "ci-speedup-dogfood.js",
        _MAINT / "workflows" / "ci-speedup-dogfood.command.md",
        _MAINT / "tests" / "test_draft_detector.py",
        _MAINT / "tests" / "test_aggregate_lessons.py",
        _MAINT / "tests" / "test_loop_summary_infra.py",
        _MAINT / "tests" / "dogfood-retry.test.mjs",
        _MAINT / "tests" / "test_dogfood_retry_node.py",
    ]
    missing = [p.relative_to(_REPO).as_posix() for p in required if not p.exists()]
    assert not missing, "relocated maintainer infra is missing: " + ", ".join(missing)


def test_shipped_docs_have_no_dangling_links_to_moved_infra():
    """No shipped markdown doc may carry a markdown LINK to relocated infra — it would
    dangle in an install. The installer copies the WHOLE skill dir, so this scans every
    ``*.md`` under skills/ci-speedup/ (not just SKILL.md/ARCHITECTURE.md), skipping the
    hidden runtime dirs the installer never copies. Plain-text mentions (no ``](...)``)
    are fine and are how these are referenced post-move."""
    link_re = re.compile(r"\]\(([^)]+)\)")
    offenders = []
    for doc in sorted(_SKILL.rglob("*.md")):
        if any(part.startswith(".") or part == "__pycache__"
               for part in doc.relative_to(_SKILL).parts):
            continue
        text = doc.read_text(encoding="utf-8")
        for raw in link_re.findall(text):
            target = raw.split("#", 1)[0].strip()           # drop any anchor
            if not target:
                continue
            base = target.rsplit("/", 1)[-1]
            if "maintainers/" in target or base in _MOVED_NAMES:
                offenders.append(f"{doc.relative_to(_REPO).as_posix()}: ]({raw})")
    assert not offenders, (
        "shipped doc has a markdown link to relocated maintainer infra (it would dangle "
        "in an install — use a plain-text mention): " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# Content guard (issue #117): no INTERNAL identifier may ship in any tracked file.
#
# Distinct from the checks above, which are name-pinned to *file*/dir leaks. This is the
# *content* catch-all that closes the #117 R4 gap: the internal dev repo's real name leaked
# 10× into shipped reader-facing docs + a code comment — a regression of the #60
# neutralization, which had no test to keep it from creeping back. This sweeps a small
# denylist of internal identifiers over every git-tracked file so an internal repo/skill
# name can't ship again.
# Terms are stored LOWERCASE; the scan lowercases every line, so a capitalized variant
# can't slip past a case-sensitive compare.
_INTERNAL_IDENTIFIER_DENYLIST = (
    "starsling-website",     # the internal dev repo (the #117 leak)  content-guard:allow
    "skills-next",           # internal skills staging repo           content-guard:allow
    "starsling-internal",    # internal org / runner-label prefix     content-guard:allow
)

# The guard scans EVERY tracked file, including this one. A line that legitimately names a
# denied term (only the denylist declaration above, and any comment that must spell one out)
# opts out by carrying this marker — so exemption is line-scoped, not whole-file. A future
# fixture/docstring/assertion here that names an internal identifier WITHOUT the marker still
# fails, closing the "whole-file allowlist hides leaks" gap. As of #117 no example/report
# contains any denied term: shipped examples audit public repos on `ubuntu-latest`, so no
# internal runner label (starsling-internal-*) appears.  content-guard:allow
_CONTENT_GUARD_ALLOW_MARKER = "content-guard:allow"

# Byte encodings an ASCII identifier can hide behind. The utf-8 view (errors replaced)
# preserves ASCII even when an unrelated byte is invalid; when NUL bytes suggest a 2-byte
# encoding, BOTH utf-16 byte orders are added (a BOM-less utf-16be file would otherwise
# mis-decode under the platform-default order and mask a leak) — so neither a stray bad byte
# nor utf-16 masks a leak.
def _decoded_views(raw: bytes):
    views = [raw.decode("utf-8", errors="replace")]
    if b"\x00" in raw:
        views.append(raw.decode("utf-16-le", errors="replace"))
        views.append(raw.decode("utf-16-be", errors="replace"))
    return views


def _tracked_files():
    """Every git-tracked file, repo-relative POSIX path. Uses the worktree's index so the
    scan matches exactly what would ship / be published (gitignored capture dirs excluded).
    Skips (not fails) when git is unavailable — an exported source tree has no index to scan."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO), "ls-files", "-z"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git unavailable; cannot enumerate tracked files: {exc}")
    return [rel for rel in out.split("\0") if rel]


def test_no_internal_identifier_in_tracked_files():
    """No internal repo/skill/org identifier may appear in any tracked file (issue #117).

    Reader-facing docs and shipped code must carry only public/neutral slugs; internal
    names belong nowhere in a public repo. Matching is case-insensitive and byte-aware
    (utf-8 + utf-16); the only legitimate mentions (this file's denylist + its own
    explanatory comments) opt out per-line via the content-guard:allow marker."""
    offenders = []
    for rel in _tracked_files():
        p = _REPO / rel
        if not p.is_file():
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        for view in _decoded_views(raw):
            for lineno, line in enumerate(view.splitlines(), 1):
                low = line.lower()
                if _CONTENT_GUARD_ALLOW_MARKER in low:
                    continue
                for term in _INTERNAL_IDENTIFIER_DENYLIST:
                    if term in low:
                        offenders.append(f"{rel}:{lineno}: '{term}'")
    # De-dup: utf-8 and utf-16 views of the same line can both match.
    offenders = sorted(set(offenders))
    assert not offenders, (
        "internal identifier(s) leaked into tracked file(s) — neutralize to a public/neutral "
        "slug (see issue #117 and the #60 neutralization precedent). If a match is a legitimate "
        "PUBLIC label, mark that line with the content-guard:allow marker and justify it:\n"
        + "\n".join(offenders)
    )
