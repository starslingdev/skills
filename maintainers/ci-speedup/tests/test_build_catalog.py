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


# The pattern counts and id lists that SKILL.md, ARCHITECTURE.md and evals.json
# state are derived from the pattern files' frontmatter (`class: structural` or
# not) and regenerated by the build, so two PRs that each add a pattern never
# conflict on a hand-maintained count.

def _repo_copy(tmp_path: Path) -> Path:
    """A throwaway repo holding the build script and the ci-speedup files it
    reads and writes, so a test can stale a doc without touching the checkout."""
    root = tmp_path / "repo"
    scripts = root / "maintainers" / "ci-speedup" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_SCRIPT, scripts / _SCRIPT.name)
    src, dst = _REPO / "skills" / "ci-speedup", root / "skills" / "ci-speedup"
    for sub in ("scripts", "references", "evals"):
        shutil.copytree(src / sub, dst / sub, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("SKILL.md", "ARCHITECTURE.md"):
        shutil.copy2(src / name, dst / name)
    return root


def _check(root: Path) -> subprocess.CompletedProcess:
    script = root / "maintainers" / "ci-speedup" / "scripts" / _SCRIPT.name
    return subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)


def _stale(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} no longer contains {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_check_fails_when_a_doc_states_a_stale_pattern_count(tmp_path):
    root = _repo_copy(tmp_path)
    total = len(bc.scan.load_catalog(bc.PATTERNS))
    _stale(root / "skills" / "ci-speedup" / "SKILL.md", f"{total}-pattern", f"{total - 1}-pattern")
    r = _check(root)
    assert r.returncode == 1, "build_catalog.py --check passed with a stale SKILL.md count:\n" + r.stdout + r.stderr
    assert "SKILL.md" in r.stdout + r.stderr


def test_check_fails_when_an_id_list_or_the_evals_count_is_stale(tmp_path):
    root = _repo_copy(tmp_path)
    skill = root / "skills" / "ci-speedup"
    facts = bc.catalog_facts(bc.PATTERNS)
    first = facts["structural_ids"].split(", ")[0]
    _stale(skill / "ARCHITECTURE.md", f"patterns ({facts['structural_ids']};", f"patterns ({first};")
    r = _check(root)
    assert r.returncode == 1 and "ARCHITECTURE.md" in r.stderr, r.stdout + r.stderr
    root = _repo_copy(tmp_path / "evals")
    _stale(root / "skills" / "ci-speedup" / "evals" / "evals.json",
           f"the {facts['total']}-pattern detector", "the 1-pattern detector")
    r = _check(root)
    assert r.returncode == 1 and "evals.json" in r.stderr, r.stdout + r.stderr


def test_a_reworded_count_sentence_is_a_source_error_not_silently_skipped(tmp_path):
    root = _repo_copy(tmp_path)
    _stale(root / "skills" / "ci-speedup" / "SKILL.md", "almost every hygiene hit (", "most hygiene hits (")
    r = _check(root)
    assert r.returncode == 2 and "SKILL.md: expected exactly one catalog-count sentence" in r.stderr, r.stderr


def test_a_build_rewrites_stale_counts_and_then_checks_clean(tmp_path):
    root = _repo_copy(tmp_path)
    skill = root / "skills" / "ci-speedup"
    total = len(bc.scan.load_catalog(bc.PATTERNS))
    _stale(skill / "SKILL.md", f"{total}-pattern", "3-pattern")
    script = root / "maintainers" / "ci-speedup" / "scripts" / _SCRIPT.name
    assert subprocess.run([sys.executable, str(script)], capture_output=True).returncode == 0
    assert f"{total}-pattern" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert _check(root).returncode == 0


def test_a_new_structural_pattern_moves_both_counts(tree):
    before = bc.catalog_facts(tree)
    src = next(tree.glob("opt33-*.md")).read_text()
    assert "\nclass: static\n" in src
    (tree / "opt999-new.md").write_text(src.replace("pattern: OPT33", "pattern: OPT999", 1)
                                        .replace("\nclass: static\n", "\nclass: structural\n", 1)
                                        .replace("# OPT33 ", "# OPT999 ", 1))
    after = bc.catalog_facts(tree)
    assert int(after["total"]) == int(before["total"]) + 1
    assert int(after["structural"]) == int(before["structural"]) + 1
    assert after["hygiene"] == before["hygiene"] and after["structural_ids"].endswith("OPT999")


def test_id_lists_collapse_three_or_more_consecutive_ids():
    assert bc._id_list([3, 1, 2, 5, 6, 9, 10, 11, 12]) == "OPT1–OPT3, OPT5, OPT6, OPT9–OPT12"
    assert bc._id_list([7]) == "OPT7"
