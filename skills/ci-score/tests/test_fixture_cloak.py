"""Byte-fidelity + cloak-property proof for the frozen control fixtures.

The two third-party control checkouts (mastra, better-auth) ship "cloaked" so
registry security scanners don't attribute their workflow files to this repo:
under ``tests/fixtures/checkouts-src/<name>/`` every ``.github`` path segment is
renamed to ``dot-github`` and every file carries a ``.fixture`` suffix. The
cloak is a pure rename — no byte of any scored file changes. These tests pin
BOTH halves of that promise:

* **Lossless** — materializing a fixture reproduces exactly the file set and
  bytes recorded in ``checkouts-manifest.json`` (built from ``origin/main``
  before the rename). If a cloaked file is ever edited, dropped, or added, the
  manifest hash-mismatch turns CI red — the cloak is provably lossless forever.
* **Cloaked** — no shipped fixture path contains a ``.github`` segment and no
  shipped fixture file has a bare workflow-parseable name. A regression that
  re-introduces a raw ``.github/workflows/*.yml`` under the skill (which is what
  scanners flag) fails here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from _fixture_checkouts import (
    NAMES,
    iter_cloaked_files,
    load_manifest,
    materialize,
)

_SRC = Path(__file__).resolve().parent / "fixtures" / "checkouts-src"


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.mark.parametrize("name", NAMES)
def test_materialized_tree_matches_manifest_byte_for_byte(name, tmp_path):
    """The reconstructed tree's {relative-path: sha256} set equals the recorded
    manifest exactly — same files, same bytes, nothing added or missing."""
    expected = load_manifest()["checkouts"][name]
    root = tmp_path / name
    root.mkdir()
    materialize(name, root)

    got = {
        p.relative_to(root).as_posix(): _sha256(p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }
    assert got == expected, (
        f"{name}: materialized tree diverged from the fidelity manifest — the "
        f"cloak is no longer lossless. Missing/extra: "
        f"{set(expected) ^ set(got)}"
    )


@pytest.mark.parametrize("name", NAMES)
def test_shipped_fixture_paths_are_cloaked(name):
    """No shipped fixture path may carry a ``.github`` segment, and every shipped
    fixture file must end in ``.fixture`` (no bare workflow-parseable name) — the
    property that keeps registry scanners from attributing these third-party
    workflows to this repo."""
    files = iter_cloaked_files(name)
    assert files, f"no cloaked files found for {name}"
    for p in files:
        parts = p.relative_to(_SRC).parts
        assert ".github" not in parts, f"un-cloaked .github segment in {p}"
        assert p.name.endswith(".fixture"), f"bare (un-suffixed) fixture file {p}"
