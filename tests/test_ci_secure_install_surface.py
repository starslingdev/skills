"""Install-surface invariant: ci-secure's maintainer-only infrastructure must
live OUTSIDE the installable ``skills/ci-secure/`` tree.

The ``skills`` CLI (vercel-labs/skills, ``src/installer.ts``) copies
``skills/<name>/`` recursively into an end-user install, excluding only a small
hardcoded blocklist (``.git``, ``__pycache__``, ``__pypackages__``) — there is
no ``.skillignore`` / frontmatter allowlist / dotfile exclusion. So the ONLY way
to keep maintainer-only files out of an end-user install is to keep them out of
``skills/ci-secure/`` entirely.

ci-secure now HAS a maintainer-only tree — ``maintainers/ci-secure/`` holds
``MAINTAINERS.md`` plus the behavioral-eval harness (``scripts/run_skill_evals.py``
and its tests), which drives real ``claude -p`` sessions and is of no use to
someone who installed the skill. So this guard is no longer purely
forward-looking: it pins where that tree lives, and ``run_skill_evals.py`` is a
forbidden basename under ``skills/ci-secure/`` for exactly that reason.

The leak shape still to expect is ci-speedup's: self-improvement loop infra
(loop prompts, a summary schema, drafting scripts) plus the runtime capture
directories those loops write into, which on a maintainer's machine hold
third-party job logs and session transcripts. Rooted under the skill, every one
of them would ship.

Independent checks (each its own test function, so a broken assertion in one
never silently disables the others), plus a positive control. Every other
assertion here is a "nothing matched", and the forbidden infra is absent — so
they would ALSO pass if a detector were mangled, or if ``_SKILL`` pointed at a
directory that does not exist (``rglob`` on a missing path yields nothing and
the whole file goes green against an empty world).

Deliberate NON-target: the fixture ``.github/`` directories that
``skills/ci-secure/tests/conftest.py`` materializes from the tracked, cloaked
``dot-github/*.fixture`` sources. They are gitignored build output of the test
run, never tracked, and the same carve-out ``test_ci_score_install_surface.py``
makes for its checkout fixtures. What keeps attack-shaped workflow YAML out of
the *published repository* — the surface a registry scanner reads — is the
cloak itself, which that conftest keeps honest with a manifest census over
every cloaked fixture (hash round-trip in both directions, plus a prune of
anything materialized without a manifest entry behind it).
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "skills" / "ci-secure"

# Basenames that mark maintainer-only loop infrastructure. A file with one of
# these names anywhere under the installable skill is a leak. Compared
# case-INSENSITIVELY, like the directory names below: macOS and Windows
# checkouts are case-insensitive, so `Draft_Detector.py` is the same file to the
# installer and matching it exactly was a rename-shaped hole on one side of the
# guard only.
_FORBIDDEN_FILE_NAMES = {
    "MAINTAINERS.md",
    "loop-analysis-prompt.md",
    "gap-to-catalog-prompt.md",
    "loop-summary.schema.json",
    "draft_detector.py",
    "aggregate_lessons.py",
    # The behavioral-eval harness. It drives a real, billed agent session per
    # case, so it must never reach an end user's install and must never be
    # collected by the ordinary `pytest` run.
    "run_skill_evals.py",
}

# Directory names that mark the same thing.
_FORBIDDEN_DIR_NAMES = {
    "loop",
    "loops",
    "dogfood",
    "calibration",
    "specs",
}

# Runtime-capture dirs are all named `.ci-secure-*`. The installer strips no
# dotfiles beyond {.git, __pycache__, __pypackages__}, so any such dir ships.
# Catching the whole prefixed family closes the rename gap a fixed name list
# leaves open (`.ci-secure-loop`, `.ci-secure-gaps`, a future
# `.ci-secure-runs`). Scoped to the prefix rather than "every dot-dir" because
# the test run materializes legitimate `.github/` fixture dirs — see the module
# docstring.
_CAPTURE_DOTDIR_PREFIX = ".ci-secure-"


def _is_capture_dotdir(name: str) -> bool:
    return name.lower().startswith(_CAPTURE_DOTDIR_PREFIX)


def _is_forbidden_file(name: str) -> bool:
    return name.lower() in {n.lower() for n in _FORBIDDEN_FILE_NAMES}


def _is_forbidden_dir(name: str) -> bool:
    return name.lower() in {n.lower() for n in _FORBIDDEN_DIR_NAMES}


def _iter_skill_files():
    for p in _SKILL.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            yield p


def test_no_maintainer_only_files_under_skill():
    """No maintainer-only loop file may sit under the installable skill."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _iter_skill_files()
        if _is_forbidden_file(p.name)
    )
    assert not leaked, (
        "maintainer-only infrastructure leaked into the installable skill "
        "(keep it outside skills/ci-secure/): " + ", ".join(leaked)
    )


def test_no_eval_results_under_skill():
    """`claude plugin eval` writes `aggregate-result.json`, `report.html` and
    kept sandbox copies to `<discovery root>/evals/results/` unless the operator
    passes `--output-dir`. For ci-secure the discovery root IS the installable
    skill, so the default lands run artifacts inside the tree the `skills` CLI
    copies wholesale into every end-user install.

    Gitignoring the path (which `.gitignore` also does) is NOT sufficient on its
    own: the installer honours no ignore file, so an ignored directory ships
    exactly like a tracked one. Those artifacts embed full prompts, transcript
    excerpts and grader evidence from whatever repository the maintainer ran
    against, which makes this a disclosure leak and not just clutter. The
    remedy is `--output-dir` outside the skill; `evals/README.md` documents the
    invocation.
    """
    results = _SKILL / "evals" / "results"
    assert not results.exists(), (
        f"{results.relative_to(_REPO)} exists and would ship into every "
        "end-user install (gitignore does not stop the installer) — delete it "
        "and re-run with `--output-dir` pointing outside skills/ci-secure/")


def test_no_tracked_eval_results():
    """The same directory, from the other side: nothing under it may be tracked.
    The disk check above only fires on the machine that ran the suite, so this
    is what stops a run artifact reaching `main` from someone else's checkout."""
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "-z",
         "skills/ci-secure/evals/results"],
        capture_output=True, text=True, check=True,
    ).stdout
    leaked = sorted(rel for rel in out.split("\0") if rel)
    assert not leaked, (
        "eval run artifacts are tracked under the installable skill: "
        + ", ".join(leaked)
    )


def test_no_maintainer_only_dirs_under_skill():
    """Catch the same infra when it arrives as a whole directory."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_dir() and _is_forbidden_dir(p.name)
    )
    assert not leaked, (
        "maintainer-only directory leaked into the installable skill "
        "(keep it outside skills/ci-secure/): " + ", ".join(leaked)
    )


def test_no_capture_dotdir_under_skill():
    """A `.ci-secure-*` runtime-capture dir would ship into every end-user
    install. These hold third-party job logs and session transcripts on a
    maintainer's machine, so the leak is a data-disclosure one, not just
    clutter."""
    leaked = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _SKILL.rglob("*")
        if p.is_dir() and _is_capture_dotdir(p.name)
    )
    assert not leaked, (
        "a .ci-secure-* runtime-capture dir would ship into every end-user "
        "install (relocate outside skills/ci-secure/): " + ", ".join(leaked)
    )


def test_no_tracked_workflow_shaped_fixture_paths():
    """The tracked tree must carry NO file at a literal ``.github/workflows/``
    path under the skill. The fixtures are intentionally-vulnerable workflow
    YAML; tracked at their real paths, a registry scanner reads them as this
    repository's own live automation — the class that once rated a sibling
    skill CRITICAL. They ship cloaked (``dot-github/*.fixture``) instead, and
    the test-time materialization is gitignored. This is the invariant that
    keeps the cloak from quietly rotting."""
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "-z", "skills/ci-secure"],
        capture_output=True, text=True, check=True,
    ).stdout
    leaked = sorted(
        rel for rel in out.split("\0")
        if rel and "/.github/" in "/" + rel
    )
    assert not leaked, (
        "workflow-shaped fixture tracked at a real .github/ path under the "
        "skill — store it cloaked under dot-github/ with a .fixture suffix: "
        + ", ".join(leaked)
    )


def test_detectors_actually_fire():
    """Positive control against vacuous passing. Every other assertion in this
    file is a "nothing matched" — and the forbidden infra is maintainer-local
    and absent from this repo, so they would ALSO pass if a detector were
    silently mangled (name set emptied, prefix broken) — or if `_SKILL` stopped
    pointing at a real directory, because `rglob` on a missing path yields
    nothing and every check goes green against an empty world."""
    assert _SKILL.is_dir(), (
        f"{_SKILL} is not a directory — the skill was renamed or moved and "
        "every check in this file is now scanning nothing")
    scanned = sum(1 for _ in _iter_skill_files())
    assert scanned > 50, (
        f"only {scanned} file(s) under {_SKILL}; the skill ships a SKILL.md, "
        "scripts, references, evals and tests, so a count this low means the "
        "walker is broken and the 'nothing matched' assertions are vacuous")
    assert _is_capture_dotdir(".ci-secure-loop")
    assert _is_capture_dotdir(".CI-Secure-Loop")     # case-insensitive
    assert _is_forbidden_file("MAINTAINERS.md")
    assert _is_forbidden_file("maintainers.md")      # case-insensitive
    assert not _is_forbidden_file("SKILL.md")
    assert _is_forbidden_dir("loops") and _is_forbidden_dir("Loops")
    assert not _is_forbidden_dir("references")
    assert _is_capture_dotdir(".ci-secure-gaps")
    assert _is_capture_dotdir(".ci-secure-runs")     # the rename gap
    assert not _is_capture_dotdir(".github")         # materialized fixtures
    assert not _is_capture_dotdir(".git")
    assert "MAINTAINERS.md" in _FORBIDDEN_FILE_NAMES
    assert "draft_detector.py" in _FORBIDDEN_FILE_NAMES
    assert "loops" in _FORBIDDEN_DIR_NAMES
    assert "dogfood" in _FORBIDDEN_DIR_NAMES
