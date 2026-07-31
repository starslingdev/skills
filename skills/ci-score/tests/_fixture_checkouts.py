"""Materialize the cloaked third-party control fixtures back to their exact
original trees at test time.

The two frozen control checkouts (mastra, better-auth) are real third-party
repo snapshots — workflow YAML, ``.github/actions/**`` composite actions, and
root build-tool configs, exactly the input surface the scorer reads. Stored
verbatim under ``.github/workflows/``, registry security scanners parse them as
THIS repo's live automation and attribute their contents here. So they ship
"cloaked": under ``tests/fixtures/checkouts-src/<name>/`` every ``.github`` path
segment is renamed to ``dot-github`` and every file is given a ``.fixture``
suffix — no shipped path is a workflow path and no shipped file has a bare
workflow-parseable name.

The cloak is a pure, invertible rename (byte content untouched). This module
inverts it: :func:`materialize` copies a fixture into a destination directory,
restoring the exact original relative paths and bytes, so the collector sees
precisely what it saw before the cloak. ``checkouts-manifest.json`` — built from
``origin/main`` before the rename — is the lossless-ness oracle
(``test_fixture_cloak.py`` proves the round trip byte-for-byte).
"""
from __future__ import annotations

import json
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_FIXTURES = _TESTS / "fixtures"
_SRC = _FIXTURES / "checkouts-src"
_MANIFEST_PATH = _FIXTURES / "checkouts-manifest.json"

_CLOAK_SUFFIX = ".fixture"
_CLOAK_SEGMENT = "dot-github"
_ORIG_SEGMENT = ".github"

# The fixture checkout names (one subtree each under checkouts-src/).
NAMES = ("better-auth", "mastra")


def _decloak_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Invert the cloak on a fixture-relative path: un-rename the ``.github``
    segment and strip the ``.fixture`` suffix from the filename."""
    out = [_ORIG_SEGMENT if p == _CLOAK_SEGMENT else p for p in parts]
    last = out[-1]
    assert last.endswith(_CLOAK_SUFFIX), (
        f"cloaked fixture file missing {_CLOAK_SUFFIX!r} suffix: {'/'.join(parts)}"
    )
    out[-1] = last[: -len(_CLOAK_SUFFIX)]
    return tuple(out)


def iter_cloaked_files(name: str):
    """Return every cloaked file path under fixture ``name`` (sorted, stable)."""
    base = _SRC / name
    return sorted(p for p in base.rglob("*") if p.is_file())


_WORKFLOW_SUFFIXES = (".yml", ".yaml")


def scan_uncloaked(skill_dir: Path, fixtures_dir: Path) -> dict[str, list[str]]:
    """Find cloak violations under a skill tree — the shape registry scanners
    attribute to this repo.

    Returns ``{"github": [...], "bare_workflow": [...]}`` (paths relative to
    ``skill_dir`` / ``fixtures_dir`` respectively): any path segment equal to
    ``.github`` anywhere under ``skill_dir``, and any bare workflow-parseable
    file (``*.yml``/``*.yaml``) under ``fixtures_dir``. Both empty => fully
    cloaked. Shared by the shipped whole-tree guard and its negative
    proof-of-detection test, so the detection logic itself is pinned and cannot
    be silently neutered by a refactor.
    """
    github = sorted(
        str(p.relative_to(skill_dir))
        for p in skill_dir.rglob("*")
        if "__pycache__" not in p.parts
        and _ORIG_SEGMENT in p.relative_to(skill_dir).parts
    )
    bare_workflow = sorted(
        str(p.relative_to(fixtures_dir))
        for p in fixtures_dir.rglob("*")
        if p.is_file() and p.suffix in _WORKFLOW_SUFFIXES
    )
    return {"github": github, "bare_workflow": bare_workflow}


def materialize(name: str, dest: Path) -> Path:
    """Reconstruct fixture ``name``'s original checkout tree inside ``dest``.

    Restores each file at its exact pre-cloak relative path with byte-identical
    content. Returns ``dest``. The caller owns turning ``dest`` into a git repo
    (the collector requires a checkout) — behavior identical to copying the
    pre-cloak tree in place.
    """
    base = _SRC / name
    assert base.is_dir(), f"missing cloaked control fixture {name!r} under {_SRC}"
    for src in iter_cloaked_files(name):
        rel = src.relative_to(base).parts
        target = dest.joinpath(*_decloak_parts(rel))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    return dest


def load_manifest() -> dict:
    """The byte-fidelity manifest (built from origin/main before the cloak)."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
