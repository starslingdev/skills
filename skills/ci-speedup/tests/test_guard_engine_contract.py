"""Scanner output guarantees consumed by ci-speedup-guard."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import scan

REPO = Path(__file__).resolve().parents[3]
CATALOG = REPO / "skills" / "ci-speedup" / "references" / "patterns"


def _repo(tmp_path: Path, text: str) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_every_finding_on_the_repository_has_a_stable_nonempty_subject():
    before = scan.scan(REPO, CATALOG, "main")["findings"]
    assert before
    assert all(isinstance(f.get("subject"), str) and f["subject"] for f in before)

    # Adding an unrelated clean job cannot re-key any existing occurrence.
    ci = REPO / ".github" / "workflows" / "ci.yml"
    original = ci.read_text(encoding="utf-8")
    doc = scan.yaml.safe_load(original)
    original_hits = {p: [h.match_text for h in fn(doc, original)]
                     for p, fn in scan._DETECTORS.items()}
    doc["jobs"]["guard-subject-control"] = {
        "runs-on": "ubuntu-latest", "steps": [{"run": "echo control"}]}
    changed = scan.yaml.safe_dump(doc, sort_keys=False)
    # Exercise the detector layer directly without mutating the checkout.
    changed_hits = {p: [h.match_text for h in fn(doc, changed)]
                    for p, fn in scan._DETECTORS.items()}
    assert all(all(subject for subject in subjects) for subjects in changed_hits.values())
    for pattern, subjects in original_hits.items():
        assert not (Counter(subjects) - Counter(changed_hits[pattern]))


def test_opt23_is_instance_granular_in_new_jobs_and_same_workflow(tmp_path):
    one = """on: pull_request
jobs:
  one:
    runs-on: ubuntu-latest
    strategy: {matrix: {x: [1, 2]}, max-parallel: 1}
    steps: [{run: echo one}]
"""
    two = one + """  two:
    runs-on: ubuntu-latest
    strategy: {matrix: {x: [1, 2]}, max-parallel: 1}
    steps: [{run: echo two}]
"""
    root = _repo(tmp_path, one)
    first = [f for f in scan.scan(root, CATALOG)["findings"] if f["pattern"] == "OPT23"]
    (root / ".github" / "workflows" / "ci.yml").write_text(two, encoding="utf-8")
    second = [f for f in scan.scan(root, CATALOG)["findings"] if f["pattern"] == "OPT23"]
    assert scan.INSTANCE_GRANULAR == frozenset({"OPT23"})
    assert [f["subject"] for f in first] == ["one"]
    assert [f["subject"] for f in second] == ["one", "two"]


def test_opt14_stamps_only_shard_axis_leg_counts(tmp_path):
    root = _repo(tmp_path, """on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shardIndex: [1, 2, 3, 4]
        os: [ubuntu-latest, windows-latest]
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
      - run: npm test -- --shard=${{ matrix.shardIndex }}/4
""")

    doc = scan.scan(root, CATALOG)

    assert not [row for row in doc["findings"] if row["pattern"] == "OPT14"]
    assert doc["matrix_setup_facts"] == {
        ".github/workflows/ci.yml": {
            "shards": {
                "legs": 4,
                "repeated": ["checkout", "install", "build"],
            }
        }
    }

    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8")
        + "      - parallel:\n          run: echo unreadable\n",
        encoding="utf-8",
    )
    unreadable = scan.scan(root, CATALOG)
    assert ".github/workflows/ci.yml" not in unreadable.get("matrix_setup_facts", {})


def test_opt14_ignores_a_single_effective_shard_leg(tmp_path):
    root = _repo(tmp_path, """on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1]
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
      - run: npm test -- --shard=${{ matrix.shard }}/1
""")

    findings = scan.scan(root, CATALOG)["findings"]

    assert not [row for row in findings if row["pattern"] == "OPT14"]


def test_opt14_counts_effective_shard_legs_after_include_and_exclude(tmp_path):
    root = _repo(tmp_path, """on: pull_request
jobs:
  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2, 3, 4]
        os: [ubuntu-latest, windows-latest]
        exclude:
          - shard: 3
          - shard: 4
        include:
          - shard: 5
            os: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
  sibling:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
""")

    finding = next(
        row for row in scan.scan(root, CATALOG)["findings"]
        if row["pattern"] == "OPT14")

    assert finding["matrix_setup_legs"] == {"shards": 3}


def test_opt14_does_not_claim_conditioned_or_cache_only_setup_repeats(tmp_path):
    root = _repo(tmp_path, """on: pull_request
jobs:
  cache-only:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {cache: npm}
  conditional-install:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - uses: actions/checkout@v4
      - if: matrix.shard == 1
        run: npm ci
""")

    finding = next(
        row for row in scan.scan(root, CATALOG)["findings"]
        if row["pattern"] == "OPT14")

    assert "matrix_setup_legs" not in finding
    assert "matrix_setup_repeated" not in finding


def test_all_scan_incomplete_records_have_a_known_kind(tmp_path):
    root = _repo(tmp_path, "jobs: [unterminated")
    records = scan.scan(root, CATALOG)["scan_incomplete"]
    assert records
    assert {row["kind"] for row in records} <= scan.SCAN_INCOMPLETE_KINDS
    assert records[0]["kind"] == "workflow-unparseable"


def test_each_non_permission_coverage_gap_emits_its_machine_kind(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    missing.mkdir()
    assert scan.scan(missing, CATALOG)["scan_incomplete"][0]["kind"] == "no-workflows-dir"

    not_mapping = _repo(tmp_path / "not-mapping", "- one\n- two\n")
    assert {r["kind"] for r in scan.scan(not_mapping, CATALOG)["scan_incomplete"]} == {
        "workflow-not-mapping"}

    turbo = _repo(tmp_path / "turbo", "on: pull_request\njobs: {}\n")
    (turbo / "turbo.json").write_text("{not-json", encoding="utf-8")
    assert "turbo-unreadable" in {
        r["kind"] for r in scan.scan(turbo, CATALOG)["scan_incomplete"]}

    parallel = _repo(tmp_path / "parallel", """on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - parallel: not-a-list
""")
    assert "parallel-group-unread" in {
        r["kind"] for r in scan.scan(parallel, CATALOG)["scan_incomplete"]}

    capped = _repo(tmp_path / "capped", "on: pull_request\njobs: {}\n")
    source = capped / "tests" / "x.test.js"
    source.parent.mkdir()
    source.write_text("await sleep(1000)\n", encoding="utf-8")
    monkeypatch.setattr(scan, "_OPT19_FILE_CAP", 0)
    assert "opt19-file-cap" in {
        r["kind"] for r in scan.scan(capped, CATALOG)["scan_incomplete"]}


def test_out_of_tree_workflow_symlink_is_recorded_and_not_scanned(tmp_path):
    root = _repo(tmp_path / "repo", "on: pull_request\njobs: {}\n")
    outside = tmp_path / "outside.yml"
    outside.write_text("jobs:\n  serial:\n    strategy: {max-parallel: 1}\n", encoding="utf-8")
    (root / ".github" / "workflows" / "outside.yml").symlink_to(outside)
    result = scan.scan(root, CATALOG)
    assert any(row["kind"] == "symlink-out-of-tree" for row in result["scan_incomplete"])
    assert not any(f["pattern"] == "OPT23" for f in result["findings"])


def test_out_of_tree_config_symlink_is_recorded_and_not_read(tmp_path):
    root = _repo(tmp_path / "repo", "on: pull_request\njobs: {}\n")
    outside = tmp_path / "outside-vitest.config.ts"
    outside.write_text("export default { test: { isolate: false } }\n", encoding="utf-8")
    (root / "vitest.config.ts").symlink_to(outside)

    result = scan.scan(root, CATALOG)

    assert any(row["path"] == "vitest.config.ts" and row["kind"] == "symlink-out-of-tree"
               for row in result["scan_incomplete"])
    assert result["test_runner_isolation"]["isolation_opt_out"] is False
    assert result["test_runner_isolation"]["opt_out_evidence"] == []
