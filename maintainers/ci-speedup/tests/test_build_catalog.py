"""The compiled catalog is a build output of `references/patterns/`: it must be
current, banner-topped, and parse to exactly the entries the scanner reads
from the per-pattern files. The build's own source checks must reject a
broken source tree."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "maintainers" / "ci-speedup" / "scripts" / "build_catalog.py"
_spec = importlib.util.spec_from_file_location("build_catalog", _SCRIPT)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


def test_catalog_build_is_current():
    r = subprocess.run([sys.executable, str(_SCRIPT), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_compiled_file_is_the_build_plus_only_a_leading_banner():
    text = bc.COMPILED.read_text(encoding="utf-8")
    assert text.startswith(bc.BANNER) and text[len(bc.BANNER):].startswith("# ci-speedup Pattern Catalog\n")
    assert text.count("GENERATED FILE") == 1


def test_directory_and_compiled_file_parse_to_identical_entries():
    direct, compiled = bc.scan.load_catalog(bc.PATTERNS), bc.scan.load_catalog(bc.COMPILED)
    assert direct == compiled and len(direct) == len(list(bc.PATTERNS.glob("opt*.md")))


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "patterns"
    shutil.copytree(bc.PATTERNS, d)
    return d


def _breaks(d: Path, match: str):
    with pytest.raises(bc.BuildError, match=match):
        bc.build(d)


def test_a_file_missing_from_the_index_fails(tree):
    (tree / "opt999-new.md").write_text(next(tree.glob("opt33-*.md")).read_text())
    _breaks(tree, "not listed")


def test_a_file_whose_name_and_pattern_disagree_fails(tree):
    p = next(tree.glob("opt33-*.md"))
    p.write_text(p.read_text().replace("pattern: OPT33", "pattern: OPT34", 1))
    _breaks(tree, "must all be OPT33")


def test_an_index_line_whose_impact_disagrees_with_the_file_fails(tree):
    idx = tree / "INDEX.md"
    line = next(l for l in idx.read_text().splitlines() if l.startswith("- OPT33 — "))
    idx.write_text(idx.read_text().replace(line, line.replace("(MEDIUM, static)", "(HIGH, static)")))
    _breaks(tree, "INDEX.md line says")


def test_a_missing_required_key_fails(tree):
    p = next(tree.glob("opt33-*.md"))
    p.write_text(p.read_text().replace("\ncategory: 7\n", "\n", 1))
    _breaks(tree, "missing")


def test_an_index_line_with_no_pattern_file_fails(tree):
    idx = tree / "INDEX.md"
    idx.write_text(idx.read_text() + "- OPT999 — Nothing (LOW, static) — no file\n")
    _breaks(tree, "no single opt<n>")


def test_a_hand_edit_to_the_compiled_file_is_drift(tmp_path, monkeypatch):
    copy = tmp_path / "optimization-patterns.md"
    copy.write_text(bc.COMPILED.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
    monkeypatch.setattr(bc, "COMPILED", copy)
    monkeypatch.setattr(bc, "_REPO", tmp_path)
    assert bc.main(["--check"]) == 1
