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


def test_scoping_recipe_keeps_full_run_safety_nets_and_exercises_skip_logic():
    text = (bc.PATTERNS / "opt70-scope-build-test-to-changed.md").read_text()
    for phrase in (
        "**Run everything when the ground shifts:**",
        "any change to the lockfile(s), to the scoping tool's config "
        "(`nx.json`, `turbo.json`, `pnpm-workspace.yaml`, root `package.json`), "
        "or to `.github/workflows/**` must bypass scoping and run the full set",
        "run the unscoped full suite on every push to the default branch, "
        "or at least on a schedule",
        "[OPT90's post-merge guardrail]",
        "every merged commit must be covered",
        "**Test the skip logic once:**",
        "Make a throwaway branch with a change the scoper should catch, "
        "such as a change in a shared package",
        "Confirm that the affected jobs run",
        "make a change that is deliberately hidden from the scoper's dependency graph",
        "confirm the run fails or runs everything rather than going green with nothing tested",
    ):
        assert phrase in text
    guardrail = text.split("**Mandatory guardrail", 1)[1].split("**Conservative rollout", 1)[0]
    assert "Lockfile, scoping-config and workflow changes must run the full set" in guardrail
    assert "The full suite must run on every push to the default branch or at least on a schedule" in guardrail


def test_bun_check_recipe_is_an_alternative_with_bun_only_benchmarks():
    text = (bc.PATTERNS / "opt84-slow-tool-on-pole-has-native-drop-in.md").read_text()
    row = next(line for line in text.splitlines() if "| **`bun check`**" in line)
    for phrase in (
        "**`tsc --noEmit`**",
        "recipe only, not detected in v1",
        "alternative, not a drop-in: requires Bun in the job; type-check behavior "
        "must be verified against `tsc` on the repo before switching",
        "1.4–7.8× faster than v1.4.3",
        "up to 15× for `bun check -b` versus the previous Bun",
        "both compare against older Bun, not `tsc`; measure per repo",
        "https://bun.com/blog/bun-v1.4.4#new-in-bun-check",
    ):
        assert phrase in row
    assert "**TypeScript 7 stays the default recommendation** for `tsc --noEmit`, " \
           "because it is the true drop-in" in text


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


def test_a_yaml_syntax_error_is_a_source_error_not_drift(tree, monkeypatch, capsys):
    p = next(tree.glob("opt33-*.md"))
    p.write_text(p.read_text().replace("\ncategory: 7\n", "\ncategory: 7\nyaml_path: jobs: build\n", 1))
    real_build = bc.build
    monkeypatch.setattr(bc, "build", lambda: real_build(tree))
    assert bc.main(["--check"]) == 2
    assert "build_catalog:" in capsys.readouterr().err


def test_agent_volume_recipes_preserve_quarantine_and_capacity_guardrails():
    quarantine = next(bc.PATTERNS.glob("opt48-*.md")).read_text()
    for phrase in (
        "fails then passes on the same commit",
        "still runs but does not gate the merge",
        "Every quarantine entry has an owner and an expiry date",
        "An expired entry fails CI",
        "Count quarantined tests in that job's report",
        "Never quarantine a test whose failures also appear on the default branch with a product-class cause",
    ):
        assert phrase in quarantine
    capacity = next(bc.PATTERNS.glob("opt81-*.md")).read_text()
    for phrase in (
        "**Capacity before you move**",
        "instance quotas, IP limits and available capacity",
        "Check those limits before peak traffic",
        "keep the hosted fallback explicit",
        "jobs can spill back to the hosted fallback at peak demand",
    ):
        assert phrase in capacity
    queue = next(bc.PATTERNS.glob("opt43-*.md")).read_text()
    assert "See OPT97, Queue Caused by a One-at-a-Time Resource" in queue
    assert "keep jobs waiting while the runner is mostly idle" in queue
