"""First-party script calls keep load-bearing full-history checkouts intact."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEED_PATH = ROOT / "skills" / "ci-speedup" / "scripts" / "scan.py"
SCORE_PATH = ROOT / "skills" / "ci-score" / "scripts" / "practice_facts.py"
CATALOG = ROOT / "skills" / "ci-speedup" / "references" / "patterns"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engines():
    return (_load("_script_hop_speed", SPEED_PATH),
            _load("_script_hop_score", SCORE_PATH))


def _case(root: Path, run: str, script_path: str, script: str):
    target = root / script_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    raw = f"""name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: {run}
"""
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(raw, encoding="utf-8")
    return [(".github/workflows/ci.yml", yaml.safe_load(raw), raw)]


def _verdicts(engines, root: Path, parsed):
    speed, score = engines
    findings = speed.scan(root, CATALOG, default_branch="main")["findings"]
    speed_flags = any(f.get("pattern") == "OPT28" for f in findings)
    score_state = score._practice_facts(parsed, root)["ci.checkout.shallow-clone"]["state"]
    return speed_flags, score_state


@pytest.mark.parametrize(("run", "path"), [
    ("./scripts/check.sh", "scripts/check.sh"),
    ("bash scripts/check.sh", "scripts/check.sh"),
    ("python scripts/check.py", "scripts/check.py"),
    ("node .github/scripts/check.js", ".github/scripts/check.js"),
])
def test_history_operation_one_script_hop_away_keeps_full_history(
        engines, tmp_path: Path, run: str, path: str):
    parsed = _case(tmp_path, run, path, "git rev-list --max-parents=0 HEAD\n")
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"


def test_working_tree_only_script_still_allows_shallow_advice(engines, tmp_path: Path):
    parsed = _case(tmp_path, "./scripts/check.sh", "scripts/check.sh",
                   "git rev-parse HEAD\n")
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is True
    assert score_state == "fail"


@pytest.mark.parametrize(("run", "captured"), [
    ("python -m pytest", "-m"),
    ("node --version", "--version"),
])
def test_interpreter_options_are_not_treated_as_script_paths(
        engines, tmp_path: Path, run: str, captured: str):
    parsed = _case(tmp_path, run, captured, "echo clean\n")
    (tmp_path / captured).unlink()
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is True
    assert score_state == "fail"


@pytest.mark.parametrize("script", [
    'import subprocess\nsubprocess.run(["git", "rev-list", "--count", "HEAD"])\n',
    'const { spawnSync } = require("child_process");\n'
    'spawnSync("git", ["rev-list", "--count", "HEAD"]);\n',
])
def test_literal_argument_list_history_calls_keep_full_history(
        engines, tmp_path: Path, script: str):
    parsed = _case(tmp_path, "python scripts/check.py", "scripts/check.py", script)
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"


def test_step_working_directory_selects_the_invoked_script(engines, tmp_path: Path):
    parsed = _case(tmp_path, "bash check.sh", "check.sh", "echo clean\n")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "check.sh").write_text(
        "git rev-list --count HEAD\n", encoding="utf-8")
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    raw = workflow.read_text(encoding="utf-8").replace(
        "      - run: bash check.sh\n",
        "      - run: bash check.sh\n        working-directory: tools\n")
    workflow.write_text(raw, encoding="utf-8")
    parsed = [(parsed[0][0], yaml.safe_load(raw), raw)]
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"


@pytest.mark.parametrize("script", [
    "# git merge-base origin/main HEAD\necho clean\n",
    '# subprocess.run(["git", "rev-list", "HEAD"])\nprint("clean")\n',
    '// spawnSync("git", ["rev-list", "HEAD"]);\nconsole.log("clean");\n',
])
def test_commented_history_command_does_not_hide_shallow_advice(
        engines, tmp_path: Path, script: str):
    parsed = _case(tmp_path, "./scripts/check.sh", "scripts/check.sh", script)
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is True
    assert score_state == "fail"


@pytest.mark.parametrize("script", [
    "git log origin/main..HEAD\n",
    "git describe --tags\n",
    "git merge-base origin/main HEAD\n",
    "git diff origin/main...HEAD\n",
    'git diff "$BASE" HEAD\n',
    "git fetch --unshallow\n",
    "git fetch --depth=50 origin main\n",
    "git fetch --tags\n",
    "pnpm changeset status\n",
    "dorny/paths-filter@v3\n",
    "tj-actions/changed-files@v45\n",
])
def test_narrow_script_history_patterns_keep_full_history(
        engines, tmp_path: Path, script: str):
    parsed = _case(tmp_path, "./scripts/check.sh", "scripts/check.sh", script)
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"


def test_unreadable_or_outside_script_fails_closed(engines, tmp_path: Path):
    outside = tmp_path.parent / "outside-check.sh"
    outside.write_text("echo clean\n", encoding="utf-8")
    link = tmp_path / "scripts" / "check.sh"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    parsed = _case(tmp_path, "./scripts/check.sh", "scripts/check.sh",
                   "echo overwritten only when symlinks are followed\n")
    # Restore the outside-root symlink after `_case` wrote through it.
    link.unlink()
    link.symlink_to(outside)
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"


def test_missing_script_fails_closed(engines, tmp_path: Path):
    parsed = _case(tmp_path, "./scripts/check.sh", "scripts/check.sh", "echo clean\n")
    (tmp_path / "scripts" / "check.sh").unlink()
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is False
    assert score_state == "pass"
    evidence = engines[1]._practice_facts(parsed, tmp_path)[
        "ci.checkout.shallow-clone"]["evidence"]
    assert "could not be read" in evidence


def test_history_operation_two_script_hops_away_is_not_followed(engines, tmp_path: Path):
    parsed = _case(tmp_path, "./scripts/outer.sh", "scripts/outer.sh",
                   "./scripts/inner.sh\n")
    (tmp_path / "scripts" / "inner.sh").write_text(
        "git rev-list --max-parents=0 HEAD\n", encoding="utf-8")
    speed_flags, score_state = _verdicts(engines, tmp_path, parsed)
    assert speed_flags is True
    assert score_state == "fail"


def test_script_history_indexes_and_predicates_stay_in_parity(engines, tmp_path: Path):
    parsed = _case(tmp_path, "bash scripts/check.sh", "scripts/check.sh",
                   "git merge-base origin/main HEAD\n")
    speed, score = engines
    speed_index = speed._index_local_git_scripts(tmp_path, parsed)
    score_index = score._index_local_git_scripts(tmp_path, parsed)
    assert speed_index == score_index == {"scripts/check.sh"}
    job = parsed[0][1]["jobs"]["test"]
    speed._GIT_HISTORY_LOCAL_SCRIPTS = speed_index
    assert speed._job_needs_git_history(job, "test") is True
    assert score._job_needs_git_history(job, "test", set(), score_index) is True
