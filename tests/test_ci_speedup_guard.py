"""Differential policy tests for the repository-local ci-speedup guard."""
from __future__ import annotations

import copy
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


def repeated_setup_matrix(*, shard_legs: int = 8, axis: str = "shard",
                          artifact_handoff: bool = False,
                          exclude_shards: tuple[int, ...] = ()) -> str:
    values = ", ".join(str(i) for i in range(1, shard_legs + 1))
    exclusions = ("\n        exclude:" + "".join(
        f"\n          - {axis}: {value}" for value in exclude_shards)
        if exclude_shards else "")
    handoff = """
      - uses: actions/download-artifact@v4
        with: {name: app}
""" if artifact_handoff else ""
    upload = """
      - uses: actions/upload-artifact@v4
        with: {name: app, path: dist}
""" if artifact_handoff else ""
    sibling = f"""  sibling:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
{upload}""" if artifact_handoff else ""
    return f"""name: Matrix setup
on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        {axis}: [{values}]{exclusions}
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
      - run: npm test -- --shard=${{{{ matrix.{axis} }}}}/{shard_legs}
{handoff}{sibling}"""


def playwright_report_matrix(*, shard_legs: int) -> str:
    """The blob-report handoff shape from Playwright's sharding guide."""
    values = ", ".join(str(i) for i in range(1, shard_legs + 1))
    return f"""name: Playwright tests
on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        index: [{values}]
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npx playwright test --shard=${{{{ matrix.index }}}}/{shard_legs}
      - uses: actions/upload-artifact@v4
        with:
          name: blob-report-${{{{ matrix.index }}}}
          path: blob-report
  merge-reports:
    needs: shards
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          pattern: blob-report-*
          merge-multiple: true
          path: all-blob-reports
      - run: npx playwright merge-reports --reporter html ./all-blob-reports
"""


def playwright_include_report_matrix(*, shard_legs: int) -> str:
    """The same report layout with shard indexes supplied by include rows."""
    workflow = playwright_report_matrix(shard_legs=shard_legs)
    values = ", ".join(str(i) for i in range(1, shard_legs + 1))
    include = "\n".join(
        f"          - index: {value}" for value in range(1, shard_legs + 1))
    return workflow.replace(
        f"        index: [{values}]",
        f"        include:\n{include}",
    )


def index_metadata_matrix(*, matrix_legs: int) -> str:
    """An index dimension used beside, rather than inside, --shard."""
    workflow = repeated_setup_matrix(shard_legs=matrix_legs, axis="index")
    return workflow.replace(
        f"--shard=${{{{ matrix.index }}}}/{matrix_legs}",
        "--shard=1/1 --project=${{ matrix.index }}",
    )


def sharded_build_export_matrix(*, shard_legs: int) -> str:
    """Shards export their own builds; they do not consume a shared build."""
    values = ", ".join(str(i) for i in range(1, shard_legs + 1))
    return f"""name: Sharded build export
on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [{values}]
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
      - run: npm test -- --shard=${{{{ matrix.shard }}}}/{shard_legs}
      - uses: actions/upload-artifact@v4
        with:
          name: app-${{{{ matrix.shard }}}}
          path: dist
  deploy:
    needs: shards
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          pattern: app-*
"""


def build_once_with_lint(*, include_lint: bool) -> str:
    """One job builds an artifact consumed by tests beside an ordinary lint."""
    lint = """  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint
""" if include_lint else ""
    return f"""name: Build once
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
      - uses: actions/upload-artifact@v4
        with: {{name: app, path: dist}}
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with: {{name: app}}
      - run: npm test
{lint}"""


def trees(tmp_path: Path, base_text: str, merge_text: str):
    base, merge = tmp_path / "base", tmp_path / "merge"
    for root, text in ((base, base_text), (merge, merge_text)):
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(text, encoding="utf-8")
    return base, merge


def run(guard, base: Path, merge: Path, capsys, monkeypatch, allow=(),
        summary: Path | None = None):
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset(allow))
    args = ["--base", str(base), "--merge", str(merge),
            "--default-branch", "main"]
    if summary is not None:
        args.extend(["--summary", str(summary)])
    code = guard.main(args)
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


def test_brand_new_aggregate_finding_is_not_also_reported_as_scope_growth(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        "name: Test\non: pull_request\njobs: {}\n",
        two_duplicate_setup_groups(),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT12" in output
    assert "OPT12 scope grew" not in output


def test_new_group_under_existing_identity_is_not_also_scope_growth(
        guard, tmp_path, capsys, monkeypatch):
    merge_text = (two_duplicate_setup_groups()
                  .replace("  guard-verify:\n", "  a:\n")
                  .replace("  test:\n", "  b:\n")
                  .replace("  lint:\n", "  c:\n")
                  .replace("  docs:\n", "  d:\n"))
    base_text = merge_text.split("  c:\n", 1)[0]
    base, merge = trees(tmp_path, base_text, merge_text)

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "ci-speedup-guard warning: OPT12" in output
    assert "OPT12 scope grew to jobs: c, d" not in output


def test_same_identity_aggregate_finding_reports_only_the_added_job(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path, two_duplicate_setup_groups(),
        two_duplicate_setup_groups(grow_first=True))

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    growth = [line for line in output.splitlines() if "scope grew" in line]
    assert growth
    assert any("OPT12 scope grew to jobs: added" in line for line in growth)
    assert all("added" in line for line in growth)
    assert all("guard-verify" not in line and "test" not in line for line in growth)


def test_shard_matrix_leg_growth_warns_with_repeated_setup(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path, repeated_setup_matrix(shard_legs=8),
        repeated_setup_matrix(shard_legs=16))

    summary = tmp_path / "summary.md"
    code, output = run(
        guard, base, merge, capsys, monkeypatch, summary=summary)

    assert code == 0
    warnings = [line for line in output.splitlines() if "8 -> 16 legs" in line]
    assert len(warnings) == 1
    assert warnings[0].startswith("::warning file=.github/workflows/ci.yml,line=1::")
    assert "shards" in warnings[0]
    assert "repeats checkout, install and build" in warnings[0]
    rendered = summary.read_text(encoding="utf-8")
    assert "### New warnings" in rendered
    assert "OPT14 job shards: 8 -> 16 legs" in rendered


def test_removing_matrix_exclusions_warns_on_effective_shard_growth(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        repeated_setup_matrix(
            shard_legs=16, exclude_shards=tuple(range(9, 17))),
        repeated_setup_matrix(shard_legs=16),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14 job shards: 8 -> 16 legs" in output


def test_playwright_blob_report_handoff_does_not_hide_shard_growth(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        playwright_report_matrix(shard_legs=8),
        playwright_report_matrix(shard_legs=16),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14 job shards: 8 -> 16 legs" in output


def test_playwright_include_indexes_feed_shard_growth(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        playwright_include_report_matrix(shard_legs=8),
        playwright_include_report_matrix(shard_legs=16),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14 job shards: 8 -> 16 legs" in output


def test_index_axis_beside_shard_argument_is_not_a_shard_dimension(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        index_metadata_matrix(matrix_legs=8),
        index_metadata_matrix(matrix_legs=16),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14 job shards" not in output


def test_sharded_producer_is_not_a_build_handoff_consumer(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        sharded_build_export_matrix(shard_legs=8),
        sharded_build_export_matrix(shard_legs=16),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14 job shards: 8 -> 16 legs" in output


def test_build_once_handoff_keeps_ordinary_setup_group_silent(
        guard, tmp_path, capsys, monkeypatch):
    base, merge = trees(
        tmp_path,
        build_once_with_lint(include_lint=False),
        build_once_with_lint(include_lint=True),
    )

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "OPT14" not in output


@pytest.mark.parametrize("merge_text", [
    repeated_setup_matrix(shard_legs=8),
    repeated_setup_matrix(shard_legs=16, axis="os"),
    repeated_setup_matrix(shard_legs=16, axis="version"),
    repeated_setup_matrix(shard_legs=16, artifact_handoff=True),
])
def test_matrix_growth_exception_stays_silent_for_non_growth_or_non_shards(
        guard, tmp_path, capsys, monkeypatch, merge_text):
    base, merge = trees(tmp_path, repeated_setup_matrix(shard_legs=8), merge_text)

    code, output = run(guard, base, merge, capsys, monkeypatch)

    assert code == 0
    assert "legs; each leg repeats" not in output
    assert "OPT14 job shards" not in output


def test_same_identity_aggregate_finding_reports_regrouped_job(
        guard, tmp_path, capsys, monkeypatch):
    base = two_duplicate_setup_groups()
    merge = base.replace(
        "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "      - run: pip install ruff\n"
        "      - run: ruff check .\n",
        "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-node@v4\n"
        "      - run: npm install\n"
        "      - run: npm test\n",
    )
    base_root, merge_root = trees(tmp_path, base, merge)

    code, output = run(guard, base_root, merge_root, capsys, monkeypatch)

    assert code == 0
    growth = [line for line in output.splitlines()
              if "OPT12 scope grew" in line]
    assert growth
    assert all("lint" in line for line in growth)


def test_new_same_identity_aggregate_group_names_its_jobs(
        guard, tmp_path, capsys, monkeypatch):
    base = two_duplicate_setup_groups()
    added_group = """
  added-a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
      - run: go mod download
      - run: go test ./...
  added-b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
      - run: go mod download
      - run: go test ./...
"""
    merge = base.replace("jobs:\n", "jobs:\n" + added_group, 1)
    base_root, merge_root = trees(tmp_path, base, merge)

    code, output = run(guard, base_root, merge_root, capsys, monkeypatch)

    assert code == 0
    growth = [line for line in output.splitlines()
              if "OPT12 scope grew" in line]
    assert growth
    assert all("added-a" in line and "added-b" in line for line in growth)


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


def test_malformed_matrix_setup_facts_fail_closed(guard, tmp_path, monkeypatch):
    root, _ = trees(
        tmp_path, repeated_setup_matrix(shard_legs=8),
        repeated_setup_matrix(shard_legs=8))
    engine, _refs, _layout = guard.resolve_layout()
    module = guard.load_engine(engine)
    doc = guard.run_scan(engine, root, "main")
    monkeypatch.setattr(guard, "BLOCKING_ALLOWLIST", frozenset())

    mutations = (
        lambda facts: facts.__setitem__(".github/workflows/ci.yml", []),
        lambda facts: facts[".github/workflows/ci.yml"].__setitem__("shards", []),
        lambda facts: facts[".github/workflows/ci.yml"]["shards"].__setitem__("legs", "8"),
        lambda facts: facts[".github/workflows/ci.yml"]["shards"].__setitem__(
            "repeated", ["checkout", 1]),
    )
    for mutate in mutations:
        malformed = copy.deepcopy(doc)
        mutate(malformed["matrix_setup_facts"])
        with pytest.raises(guard.OperationalFailure, match="matrix_setup_facts"):
            guard.validate_scan(malformed, module)


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
