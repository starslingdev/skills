from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "maintainers" / "ci-speedup" / "mutation-prune-report.json"
SCRIPT = ROOT / "maintainers" / "ci-speedup" / "scripts" / "mutation_prune.py"


def _module() -> dict:
    return runpy.run_path(str(SCRIPT), run_name="mutation_prune_test_module")


def _commit_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=audit@example.invalid", "-c",
                    "user.name=Audit Test", "commit", "-qm", "fixture"], cwd=root, check=True)


def test_committed_mutation_report_records_the_reproducible_baseline() -> None:
    assert REPORT.exists(), "the offline mutation-prune report is missing"
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == 1
    assert report["audit_test_count"] == 11067
    assert report["baseline"]["test_count"] > 0
    assert report["baseline"]["local_suite_wall_seconds"] > 0
    assert report["baseline"]["ci_suite_wall_seconds"] > 0
    assert {row["module"] for row in report["mutants"]} == {
        "blocking_path.py", "collect_runs.py", "scan.py", "wall_clock.py"}
    assert all(row["killers"] for row in report["mutants"])
    assert report["e2e_test_files"] == list((
        "skills/ci-speedup/tests/test_e2e_fixture_set.py",
        "skills/ci-speedup/tests/test_offline_pipeline_e2e.py"))
    assert isinstance(report["overlap_candidates"], list)
    assert isinstance(report["phrase_pin_clusters"], list)
    assert report["deletion_candidates"] == [{
        "delete": ("skills/ci-speedup/tests/test_blocking_path.py::"
                   "test_render_no_poles_is_graceful"),
        "keep": ("skills/ci-speedup/tests/test_blocking_path.py::"
                 "test_render_no_poles_no_findings_still_dead_ends_gracefully"),
        "mutants": [],
        "phrase": "No measured critical path",
        "prerequisite": "fold both input shapes into the kept test",
    }]


def test_mutant_run_is_offline_and_restores_the_source(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "engine.py"
    source.write_text("ENABLED = True\n", encoding="utf-8")
    tests = tmp_path / "test_engine.py"
    tests.write_text(
        "import engine\n\n"
        "def test_engine_stays_enabled():\n"
        "    assert engine.ENABLED\n",
        encoding="utf-8",
    )
    _commit_repo(tmp_path)
    mutant = module["Mutant"]("disable-engine", "engine.py", "True", "False")

    row = module["_run_mutant"](
        tmp_path, mutant, ["test_engine.py::test_engine_stays_enabled"])

    assert row["killers"] == ["test_engine.py::test_engine_stays_enabled"]
    assert source.read_text(encoding="utf-8") == "ENABLED = True\n"
    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    _assert_interrupted_run_preserves_source(interrupted)


def _assert_interrupted_run_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "engine.py"
    source.write_text("ENABLED = True\n", encoding="utf-8")
    (tmp_path / "test_engine.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "from engine import ENABLED\n\n"
        "def test_engine_stays_enabled():\n"
        "    Path(os.environ['MUTATION_MARKER']).write_text('started')\n"
        "    time.sleep(30)\n"
        "    assert ENABLED\n",
        encoding="utf-8",
    )
    _commit_repo(tmp_path)
    marker = tmp_path / "started"
    command = ("import runpy; from pathlib import Path; m=runpy.run_path(%r); "
               "x=m['Mutant']('disable-engine','engine.py','True','False'); "
               "m['_run_mutant'](Path(%r),x,['test_engine.py::test_engine_stays_enabled'])"
               ) % (str(SCRIPT), str(tmp_path))
    env = {**os.environ, "MUTATION_MARKER": str(marker)}
    process = subprocess.Popen(
        [sys.executable, "-c", command], env=env, start_new_session=True)
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "mutant suite did not start"
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    assert source.read_text(encoding="utf-8") == "ENABLED = True\n"


def test_phrase_clusters_require_a_single_wording_assertion(tmp_path: Path) -> None:
    module = _module()
    tests = tmp_path / "skills" / "ci-speedup" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_messages.py").write_text(
        "def test_first():\n"
        "    assert 'No measured critical path' in 'No measured critical path'\n\n"
        "def test_second():\n"
        "    assert 'No measured critical path' in 'No measured critical path'\n\n"
        "def test_opposite():\n"
        "    assert 'No measured critical path' not in 'another message'\n\n"
        "def test_not_wording_only():\n"
        "    assert 'No measured critical path' in 'No measured critical path'\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    assert module["_phrase_clusters"](tmp_path) == [{
        "expectation": "present",
        "phrase": "No measured critical path",
        "tests": [
            "skills/ci-speedup/tests/test_messages.py::test_first",
            "skills/ci-speedup/tests/test_messages.py::test_second",
        ],
    }]
