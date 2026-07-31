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
    scan_uncloaked,
)

_SRC = Path(__file__).resolve().parent / "fixtures" / "checkouts-src"
_SKILL_DIR = Path(__file__).resolve().parents[1]
_FIXTURES = _SRC.parent


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


def test_no_uncloaked_workflow_shape_anywhere_under_skill():
    """Whole-tree backstop — NOT scoped to the two known control names.

    The per-name test above only walks ``checkouts-src/{better-auth,mastra}/``.
    The realistic un-cloak regression is adding a *new* third-party control
    checkout (a new name, or a bare workflow file dropped elsewhere under the
    fixtures tree) that ships a raw ``.github/workflows/*.yml`` — exactly what
    registry scanners attribute to this repo — which the per-name test would
    never see. This scans the entire skill: NO path segment anywhere under
    ``skills/ci-score/`` may equal ``.github``, and NO file under the fixtures
    tree may have a bare workflow-parseable name (``*.yml``/``*.yaml``); the
    only workflow-shaped fixtures that ship must carry the ``.fixture`` cloak
    suffix. ci-score ships zero real workflow files, so both sets are empty.
    """
    violations = scan_uncloaked(_SKILL_DIR, _FIXTURES)
    assert not violations["github"], (
        "un-cloaked '.github' path segment(s) under the skill — registry "
        f"scanners attribute these workflows to this repo: {violations['github']}"
    )
    assert not violations["bare_workflow"], (
        "bare workflow-parseable fixture file(s) — must carry the '.fixture' "
        f"cloak suffix so no shipped path is a raw workflow: "
        f"{violations['bare_workflow']}"
    )


def test_cloak_scan_actually_detects_violations(tmp_path):
    """Proof-of-detection for the whole-tree guard above: on a synthetic tree
    that DOES carry the scanner-attributable shapes, ``scan_uncloaked`` must
    flag them — so a future refactor that silently neuters the detection (e.g.
    scoping the walk to the wrong root, or dropping the ``.github`` membership
    test) turns this red instead of passing forever on a clean tree."""
    skill = tmp_path / "skill"
    fixtures = skill / "tests" / "fixtures"
    # a new un-cloaked third-party checkout — the realistic regression
    new_wf = fixtures / "checkouts-src" / "newctl" / ".github" / "workflows"
    new_wf.mkdir(parents=True)
    (new_wf / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    # a bare workflow file dropped elsewhere under the fixtures tree
    corpora = fixtures / "corpora"
    corpora.mkdir(parents=True)
    (corpora / "loose.yaml").write_text("on: push\n", encoding="utf-8")
    # a correctly cloaked file must NOT be flagged
    cloaked = fixtures / "checkouts-src" / "mastra" / "dot-github" / "workflows"
    cloaked.mkdir(parents=True)
    (cloaked / "ci.yml.fixture").write_text("name: ci\n", encoding="utf-8")

    violations = scan_uncloaked(skill, fixtures)

    assert any(
        v.startswith("tests/fixtures/checkouts-src/newctl/.github")
        for v in violations["github"]
    ), violations["github"]
    assert "corpora/loose.yaml" in violations["bare_workflow"], (
        violations["bare_workflow"]
    )
    # the cloaked file trips neither check
    assert not any("mastra" in v for v in violations["github"])
    assert not any("ci.yml.fixture" in v for v in violations["bare_workflow"])

    # and a fully-cloaked tree yields no violations at all
    (new_wf / "ci.yml").unlink()
    new_wf.rmdir()
    new_wf.parent.rmdir()  # remove the .github dir
    (corpora / "loose.yaml").unlink()
    clean = scan_uncloaked(skill, fixtures)
    assert clean == {"github": [], "bare_workflow": []}, clean
