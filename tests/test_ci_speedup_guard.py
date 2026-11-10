"""Differential policy tests for the repository-local ci-speedup guard."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD_PATH = REPO / ".github" / "scripts" / "ci_speedup_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("test_ci_speedup_guard_module", GUARD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard():
    return load_guard()


def workflow(job: str = "test", *, opt23: bool = False) -> str:
    strategy = """
    strategy:
      matrix:
        python: ['3.11', '3.12']
      max-parallel: 1
""" if opt23 else ""
    return f"""name: Test
on: pull_request
jobs:
  {job}:
    runs-on: ubuntu-latest
{strategy}    steps:
      - run: echo ok
"""


def two_duplicate_setup_groups(*, grow_first: bool = False) -> str:
    extra = """
  added:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - run: npm install
      - run: npm test
""" if grow_first else ""
    return """name: Test
on: pull_request
jobs:
  guard-verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - run: npm install
      - run: npm test
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - run: npm install
      - run: npm test
""" + extra + """
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install ruff
      - run: ruff check .
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install ruff
      - run: ruff check .
"""


def trees(tmp_path: Path, base_text: str, merge_text: str):
    base, merge = tmp_path / "base", tmp_path / "merge"
    for root, text in ((base, base_text), (merge, merge_text)):
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(text, encoding="utf-8")
    return base, merge


def run(guard, base: Path, merge: Path, capsys, monkeypatch, allow=()):
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset(allow))
    code = guard.main(["--base", str(base), "--merge", str(merge),
                       "--default-branch", "main"])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_identical_trees_pass_with_existing_debt(guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(opt23=True), workflow(opt23=True))
    code, output = run(guard, base, merge, capsys, monkeypatch, {"OPT23"})
    assert code == 0
    assert "Pass" in output
    assert "blocked:" not in output


def test_identical_same_identity_aggregate_findings_do_not_report_scope_growth(
        guard, tmp_path, capsys, monkeypatch):
    text = two_duplicate_setup_groups()
    base, merge = trees(tmp_path, text, text)

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "scope grew" not in output


def test_same_identity_aggregate_finding_reports_only_the_added_job(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path, two_duplicate_setup_groups(),
        two_duplicate_setup_groups(grow_first=True))

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    growth = [line for line in output.splitlines() if "scope grew" in line]
    assert growth
    assert all("added" in line for line in growth)
    assert all("guard-verify" not in line and "test" not in line for line in growth)


def test_new_allowlisted_instance_is_policy_block_not_operational_failure(
        guard, tmp_path, capsys, monkeypatch):
    second = """
  second:
    runs-on: ubuntu-latest
    strategy:
      matrix: {python: ['3.12']}
      max-parallel: 1
    steps:
      - run: echo second
"""
    base, merge = trees(tmp_path, workflow(opt23=True), workflow(opt23=True) + second)
    code, output = run(guard, base, merge, capsys, monkeypatch, {"OPT23"})
    assert code == 1
    assert "ci-speedup-guard blocked: OPT23 -" in output
    assert "operational failure" not in output
    assert "count 1 -> 2" in output


def test_new_non_allowlisted_finding_warns(guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow(opt23=True))
    code, output = run(guard, base, merge, capsys, monkeypatch)
    assert code == 0
    assert "ci-speedup-guard warning: OPT23 -" in output


def test_replacing_one_instance_with_another_offsets_and_passes(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow("old", opt23=True),
                        workflow("replacement", opt23=True))
    code, output = run(guard, base, merge, capsys, monkeypatch, {"OPT23"})
    assert code == 0
    assert "OPT23 offset" in output
    assert "blocked:" not in output


def test_policy_error_lands_on_added_job_not_renamed_job(
        guard, tmp_path, capsys, monkeypatch):
    added = """
  added:
    runs-on: ubuntu-latest
    strategy:
      matrix: {python: ['3.12']}
      max-parallel: 1
    steps:
      - run: echo genuinely-new-body
"""
    base, merge = trees(tmp_path, workflow("old", opt23=True),
                        workflow("renamed", opt23=True) + added)
    code, output = run(guard, base, merge, capsys, monkeypatch, {"OPT23"})
    assert code == 1
    error_lines = [line for line in output.splitlines() if line.startswith("::error")]
    assert len(error_lines) == 1
    assert "job 'added'" in error_lines[0]
    assert "job 'renamed'" not in error_lines[0]


def test_new_unparseable_workflow_is_operational_failure(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), "jobs: [unterminated")
    code, output = run(guard, base, merge, capsys, monkeypatch)
    assert code == 2
    assert "ci-speedup-guard operational failure:" in output
    assert "workflow-unparseable" in output


def test_preexisting_unparseable_workflow_is_disclosed_but_not_red(
        guard, tmp_path, capsys, monkeypatch):
    bad = "jobs: [unterminated"
    base, merge = trees(tmp_path, bad, bad)
    code, output = run(guard, base, merge, capsys, monkeypatch)
    assert code == 0
    assert "not scanned on either side: workflow-unparseable" in output


def test_out_of_tree_symlink_is_never_followed_and_fails_stated(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow())
    secret = tmp_path / "outside.yml"
    secret.write_text(workflow(opt23=True), encoding="utf-8")
    (merge / ".github" / "workflows" / "outside.yml").symlink_to(secret)
    code, output = run(guard, base, merge, capsys, monkeypatch)
    assert code == 2
    assert "symlink-out-of-tree" in output
    assert "OPT23" not in output


def test_exact_workflow_rename_preserves_identity(guard, tmp_path):
    base, merge = trees(tmp_path, workflow(opt23=True), workflow(opt23=True))
    source = merge / ".github" / "workflows" / "ci.yml"
    source.rename(source.with_name("renamed.yml"))
    assert guard.rename_map(base, merge)[".github/workflows/renamed.yml"] == \
        ".github/workflows/ci.yml"


def test_unknown_flag_is_accepted_with_warning(guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow())
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset())
    code = guard.main(["--base", str(base), "--merge", str(merge),
                       "--default-branch", "main", "--future-flag", "value"])
    output = capsys.readouterr().out
    assert code == 0
    assert "ignoring unknown guard argument" in output


def test_unwritable_summary_does_not_change_the_verdict(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow())
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset())
    code = guard.main(["--base", str(base), "--merge", str(merge),
                       "--default-branch", "main", "--summary", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 0
    assert "could not write job summary" in output


def test_bad_allowlist_entry_fails_closed(guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow())
    code, output = run(guard, base, merge, capsys, monkeypatch, {"OPT9999"})
    assert code == 2
    assert "ci-speedup-guard operational failure:" in output
    assert "OPT9999" in output


@pytest.mark.parametrize("mutation, message", [
    (lambda doc: doc["findings"][0].pop("subject"), "subject"),
    (lambda doc: doc["findings"][0].__setitem__("severity", "CRITICAL"), "severity"),
    (lambda doc: doc["findings"][0].__setitem__("advisory", "yes"), "advisory"),
])
def test_malformed_finding_schema_fails_closed(
        guard, tmp_path, monkeypatch, mutation, message):
    root, _ = trees(tmp_path, workflow(opt23=True), workflow(opt23=True))
    engine, _refs, _layout = guard.resolve_layout()
    module = guard.load_engine(engine)
    doc = guard.run_scan(engine, root, "main")
    mutation(doc)
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset())
    with pytest.raises(guard.OperationalFailure, match=message):
        guard.validate_scan(doc, module)


def test_unknown_scan_gap_kind_fails_closed(guard, tmp_path, monkeypatch):
    root, _ = trees(tmp_path, "jobs: [unterminated", "jobs: [unterminated")
    engine, _refs, _layout = guard.resolve_layout()
    module = guard.load_engine(engine)
    doc = guard.run_scan(engine, root, "main")
    doc["scan_incomplete"][0]["kind"] = "future-unknown-kind"
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset())
    with pytest.raises(guard.OperationalFailure, match="unknown kind"):
        guard.validate_scan(doc, module)


def test_bootstrap_copy_runs_report_only(tmp_path, capsys, monkeypatch):
    base, merge = trees(tmp_path, workflow(), workflow(opt23=True))
    target = merge / ".github" / "scripts"
    target.mkdir(parents=True)
    shutil.copy2(GUARD_PATH, target / GUARD_PATH.name)
    shutil.copytree(REPO / "skills" / "ci-speedup", merge / "skills" / "ci-speedup")
    copied = importlib.util.spec_from_file_location("bootstrap_guard", target / GUARD_PATH.name)
    assert copied and copied.loader
    module = importlib.util.module_from_spec(copied)
    sys.modules[copied.name] = module
    copied.loader.exec_module(module)
    monkeypatch.setattr(module, "BLOCKING_ALLOWLIST", frozenset({"OPT23"}))
    code = module.main(["--base", str(base), "--merge", str(merge),
                        "--default-branch", "main"])
    output = capsys.readouterr().out
    assert code == 0
    assert "report-only bootstrap" in output
    assert "warning: OPT23" in output


def test_layout_resolution_does_not_fall_back_to_scanned_tree(guard, tmp_path):
    fake = tmp_path / "guard.py"
    fake.write_text("", encoding="utf-8")
    with pytest.raises(guard.OperationalFailure, match="no supported"):
        guard.resolve_layout(fake)
