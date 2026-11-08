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
