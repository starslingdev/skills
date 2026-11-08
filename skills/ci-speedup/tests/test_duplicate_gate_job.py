"""Duplicate test work across merge and publish jobs."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCAN = SKILL / "scripts" / "scan.py"
sys.path.insert(0, str(SKILL / "scripts"))
import collect_runs as cr  # noqa: E402


def _write(root: Path, name: str, text: str) -> None:
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _scan(root: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCAN), "--root", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["findings"]


def _duplicate_findings(root: Path) -> list[dict]:
    return [finding for finding in _scan(root) if finding["pattern"] == "OPT12"]


def test_duplicate_publish_gate_names_both_jobs_and_safe_handoffs(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on:
  push:
    branches: [main]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
""")
    _write(tmp_path, "publish.yml", """name: Publish
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  build-and-push:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - run: docker build -t example/app .
""")

    finding = next(
        finding for finding in _duplicate_findings(tmp_path)
        if "build-and-push" in finding["evidence"]
    )
    evidence = finding["evidence"].lower()
    assert "ci.yml" in evidence and "tests" in evidence
    assert "publish.yml" in evidence and "test" in evidence
    assert "workflow_run" in evidence and "needs" in evidence
    assert "remove" not in evidence
    facts = finding["opt12_duplicate_test_work"]
    assert facts["variant"] == "needs_chain"
    assert facts["existing_job"] == "tests"
    assert facts["duplicate_job"] == "test"
    assert facts["downstream_job"] == "build-and-push"


def test_duplicate_test_requires_the_same_trigger_and_runner_platform(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: pull_request
jobs:
  tests-linux:
    runs-on: ubuntu-latest
    steps:
      - run: make test
""")
    _write(tmp_path, "publish.yml", """name: Publish
on: push
jobs:
  tests-apple:
    runs-on: macos-latest
    steps:
      - run: make test
""")

    assert _duplicate_findings(tmp_path) == []


def test_branch_filters_compare_as_sets(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on:
  push:
    branches: [main, release]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: make test
""")
    _write(tmp_path, "publish.yml", """name: Publish
on:
  push:
    branches: [release, main]
jobs:
  duplicate:
    runs-on: ubuntu-24.04
    steps:
      - run: make test
  publish:
    needs: duplicate
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
""")

    finding = next(
        row for row in _duplicate_findings(tmp_path)
        if row.get("opt12_duplicate_test_work"))
    assert finding["opt12_duplicate_test_work"]["duplicate_job"] == "duplicate"


def test_duplicate_test_with_different_commands_is_silent(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  first:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  second:
    needs: first
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
""")

    assert _duplicate_findings(tmp_path) == []


def test_same_command_in_different_working_directories_is_silent(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  package-a:
    runs-on: ubuntu-latest
    steps:
      - working-directory: packages/a
        run: npm test
  package-b:
    needs: package-a
    runs-on: ubuntu-latest
    steps:
      - working-directory: packages/b
        run: npm test
""")

    assert _duplicate_findings(tmp_path) == []


def test_mutually_exclusive_event_jobs_are_silent(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: [push, pull_request]
jobs:
  push-tests:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  pr-tests:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  publish:
    needs: push-tests
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
""")

    assert _duplicate_findings(tmp_path) == []


def test_job_with_unique_work_is_not_credited_as_a_removable_copy(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  mixed:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
      - run: npm run integration
  publish:
    needs: mixed
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
""")

    assert _duplicate_findings(tmp_path) == []


def test_case_sensitive_test_arguments_remain_distinct(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  api-upper:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/API
  api-lower:
    needs: api-upper
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/api
""")

    assert _duplicate_findings(tmp_path) == []


def test_matrix_legs_on_different_architectures_are_platform_coverage(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  tests:
    strategy:
      matrix:
        runner: [ubuntu-latest]
    runs-on: ${{ matrix.runner }}
    steps:
      - run: make test
  native-arm:
    needs: tests
    runs-on: ubuntu-24.04-arm
    steps:
      - run: make test
""")

    assert _duplicate_findings(tmp_path) == []


def test_equivalent_linux_x64_hosted_labels_can_be_duplicates(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: make test
  duplicate:
    runs-on: ubuntu-24.04
    steps:
      - run: make test
  publish:
    needs: duplicate
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
""")

    findings = [
        finding for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work")
    ]
    assert len(findings) == 1
    assert findings[0]["opt12_duplicate_test_work"]["duplicate_job"] == "duplicate"


def test_coverage_only_rerun_is_reported_as_minutes_work(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: pull_request
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  coverage:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ --cov=src --cov-report=xml
""")

    finding = next(finding for finding in _duplicate_findings(tmp_path)
                   if "coverage" in finding["evidence"].lower())
    assert "runner-minute" in finding["evidence"].lower()
    assert "existing test run" in finding["evidence"].lower()


def test_coverage_only_rerun_normalizes_common_flag_forms(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: pull_request
jobs:
  pytest-tests:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  pytest-coverage:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ --cov src --cov-report xml
  npm-tests:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  npm-coverage:
    runs-on: ubuntu-latest
    steps:
      - run: npm test -- --coverage
""")

    coverage_jobs = {
        finding["opt12_duplicate_test_work"]["duplicate_job"]
        for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work", {}).get("variant") == "coverage"
    }
    assert coverage_jobs == {"pytest-coverage", "npm-coverage"}


def test_needs_chain_variant_wins_when_the_duplicate_also_collects_coverage(
        tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  coverage:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ --cov=src
  publish:
    needs: coverage
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
""")

    finding = next(
        finding for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work", {}).get("duplicate_job") == "coverage"
    )
    assert finding["opt12_duplicate_test_work"]["variant"] == "needs_chain"
    assert "workflow_run" in finding["evidence"]
    assert "needs" in finding["evidence"]


def test_each_duplicate_job_is_emitted_once_at_its_best_rank(tmp_path: Path):
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  duplicate:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  publish:
    needs: duplicate
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
  coverage:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ --cov=src
""")

    stamps = [
        finding["opt12_duplicate_test_work"]
        for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work")
    ]
    assert [(stamp["duplicate_job"], stamp["variant"]) for stamp in stamps] == [
        ("duplicate", "needs_chain"),
        ("coverage", "coverage"),
    ]


def test_local_composite_test_commands_are_expanded_in_place(tmp_path: Path):
    action = tmp_path / ".github" / "actions" / "native-tests" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("""name: Native tests
runs:
  using: composite
  steps:
    - shell: bash
      run: pytest tests/
    - shell: bash
      run: ctest --test-dir build
""", encoding="utf-8")
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  first:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/native-tests
  second:
    needs: first
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/native-tests
""")

    finding = next(finding for finding in _duplicate_findings(tmp_path)
                   if "native-tests" in finding["evidence"])
    assert "pytest tests/" in finding["evidence"]
    assert "ctest" in finding["evidence"]
    assert "runner-minute" in finding["evidence"].lower()
    stamp = finding["opt12_duplicate_test_work"]
    assert stamp["existing_job"] == "first"
    assert stamp["duplicate_job"] == "second"


def test_direct_and_local_composite_commands_compare_semantically(tmp_path: Path):
    action = tmp_path / ".github" / "actions" / "tests" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("""name: Tests
runs:
  using: composite
  steps:
    - shell: bash
      run: pytest tests/
""", encoding="utf-8")
    _write(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  direct:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
  composite:
    needs: direct
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/tests
""")

    finding = next(
        finding for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work"))
    assert finding["opt12_duplicate_test_work"]["existing_job"] == "direct"
    assert finding["opt12_duplicate_test_work"]["duplicate_job"] == "composite"


def test_coverage_finding_belongs_to_the_duplicate_jobs_workflow(tmp_path: Path):
    _write(tmp_path, "a-coverage.yml", """name: Coverage
on: push
jobs:
  coverage:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ --cov=src
""")
    _write(tmp_path, "z-ci.yml", """name: CI
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
""")

    finding = next(
        finding for finding in _duplicate_findings(tmp_path)
        if finding.get("opt12_duplicate_test_work"))
    assert finding["workflow_file"].endswith("a-coverage.yml")
    assert finding["affected_jobs"] == ["coverage"]


def test_workflow_run_sizing_waits_for_the_producer_and_bills_the_suite():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def stamp(seconds: int) -> str:
        return (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    finding = {
        "pattern": "OPT12",
        "opt12_duplicate_test_work": {
            "variant": "needs_chain",
            "existing_workflow": ".github/workflows/ci.yml",
            "existing_job": "tests",
            "duplicate_workflow": ".github/workflows/publish.yml",
            "duplicate_job": "test",
            "downstream_job": "build-and-push",
            "same_workflow": False,
            "commands": ["npm test"],
        },
    }
    cr._ground_opt12_duplicate_test_work([finding], {
        "pushes_30d": 1,
        "facts": [{
            "sha": "abc",
            "created_at": stamp(0),
            "partial": False,
            "jobs": [
                {"workflow_file": ".github/workflows/ci.yml", "name": "tests",
                 "started_at": stamp(0), "completed_at": stamp(140)},
                {"workflow_file": ".github/workflows/publish.yml", "name": "test",
                 "started_at": stamp(0), "completed_at": stamp(90)},
            ],
        }],
    })

    assert finding["wall_clock_p50_s"] == 0.0
    assert finding["runner_min_saving"] == 2.0
    facts = finding["opt12_duplicate_test_work"]
    assert facts["duplicate_p50_s"] == 90.0
    assert facts["workflow_run_wall_p50_s"] == 0.0
