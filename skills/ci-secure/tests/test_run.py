"""Oracle test for the ci-secure one-shot driver (``scripts/run.py``).

Subprocess-based, mirroring real invocation (the orchestrator runs the
driver as a shell step, not an import). Skips if PyYAML is absent, matching
``test_scan.py`` — the driver shells out to ``scan.py``, which needs YAML.

Contracts pinned:

(a) Happy path — on the shared fixtures the driver exits 0, writes a findings
    file whose ``timings`` carries the end-to-end timing fields
    (``run_start_epoch`` stamped by scan.py plus the driver's own
    ``scripted_end_epoch`` + ``scripted_total_s``), and stdout parses as the
    group list: a JSON array of pattern ids present — every one needs an
    attacker_scenario, because under the critical-only contract every group
    renders.

(b) Failure propagation — pointed at a dir with no ``.github/workflows``, the
    driver exits non-zero and does NOT leave a findings file (a partial write
    would let a caller render over a coverage failure — exactly the
    false-negative the NEVER rules forbid).

(c) Zero-but-garbage output from scan.py is a coverage failure, not a clean
    repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_FIXTURES = _SKILL_DIR / "tests" / "fixtures"
_RUN_SCRIPT = _SKILL_DIR / "scripts" / "run.py"


def test_the_script_under_test_is_ci_secures_own() -> None:
    """Which file won. `run.py` is a colliding name across the skills in this
    repo — these tests invoke it by absolute path rather than by module name
    for exactly that reason, and this pins the path they invoke."""
    assert _RUN_SCRIPT.resolve().parents[1].name == "ci-secure"
    assert _RUN_SCRIPT.is_file()


def _skip_if_no_yaml() -> None:
    if subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    ).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_RUN_SCRIPT), *args],
        capture_output=True, text=True,
    )


def test_happy_path_writes_findings_and_prints_group_list(tmp_path: Path) -> None:
    _skip_if_no_yaml()
    out = tmp_path / "findings.json"
    proc = _run(["--root", str(_FIXTURES), "--out", str(out), "--gh-impostor", "off"])
    assert proc.returncode == 0, (
        f"run.py exited {proc.returncode} on the fixtures corpus.\n"
        f"stderr:\n{proc.stderr}"
    )
    assert out.exists(), "run.py exited 0 but wrote no findings file"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data["findings"], list) and data["findings"]

    timings = data["timings"]
    assert timings["run_start_epoch"] > 0
    assert timings["scripted_end_epoch"] >= timings["run_start_epoch"]
    assert timings["scripted_total_s"] >= 0

    # The loud-skip contract survives the driver hop.
    assert data["gh_checks"]["P14.11"].startswith("skipped:")

    groups = json.loads(proc.stdout.strip())
    assert groups == sorted({f["pattern"] for f in data["findings"]})
    assert groups, "fixtures corpus should produce findings"

    # No stray partial file left behind.
    assert not (tmp_path / "findings.json.partial").exists()


def test_failure_propagation_no_findings_file_written(tmp_path: Path) -> None:
    _skip_if_no_yaml()
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    out = tmp_path / "findings.json"
    proc = _run(["--root", str(empty), "--out", str(out), "--gh-impostor", "off"])
    assert proc.returncode != 0
    assert not out.exists(), (
        "driver failed but left a findings file — a caller could render over "
        "a coverage failure"
    )
    assert "coverage failure" in proc.stderr


def test_garbage_scan_output_is_a_coverage_failure(tmp_path, monkeypatch) -> None:
    _skip_if_no_yaml()
    # A scan.py stand-in that exits 0 with non-JSON stdout.
    fake_dir = tmp_path / "scripts"
    fake_dir.mkdir()
    (fake_dir / "scan.py").write_text("print('not json')\n")
    # Copy the driver next to the fake so _DIR resolution picks it up.
    (fake_dir / "run.py").write_text(_RUN_SCRIPT.read_text())
    out = tmp_path / "findings.json"
    proc = subprocess.run(
        [sys.executable, str(fake_dir / "run.py"),
         "--root", str(_FIXTURES), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert not out.exists()
    assert "coverage failure" in proc.stderr


def test_exit_2_is_reported_as_invalid_argument_not_coverage_failure(
    tmp_path, monkeypatch
) -> None:
    """scan.py exit 2 is its INVALID-ARGUMENT code (malformed --repo, or
    `--gh-impostor on` with gh unauthenticated) — the repo was never scanned,
    so it must NOT be reported as a coverage failure. B10b.
    """
    _skip_if_no_yaml()
    fake_dir = tmp_path / "scripts"
    fake_dir.mkdir()
    # A scan.py stand-in that exits 2 like an argparse error would.
    (fake_dir / "scan.py").write_text("import sys; sys.exit(2)\n")
    (fake_dir / "run.py").write_text(_RUN_SCRIPT.read_text())
    out = tmp_path / "findings.json"
    proc = subprocess.run(
        [sys.executable, str(fake_dir / "run.py"),
         "--root", str(_FIXTURES), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert not out.exists()
    assert "invalid argument" in proc.stderr.lower()
    # It explicitly disclaims the coverage-failure framing rather than using it.
    assert "not a coverage failure" in proc.stderr


def test_failed_run_leaves_no_stale_findings_at_the_out_path(tmp_path: Path) -> None:
    """A failed run must not leave the PREVIOUS run's findings behind.

    SKILL.md deliberately uses one fixed literal findings path across runs so
    every phase can reference it without a pointer file. That makes a stale
    file dangerous in a way a random tmp name never was: run.py fails, the
    orchestrator is told to stop — but the file it promised would not exist is
    sitting there, full of a prior scan's chains. The next phase (or a human
    re-reading it) renders another repo's findings as this one's. Clearing the
    path BEFORE the scan makes absence-after-failure the contract.
    """
    _skip_if_no_yaml()
    out = tmp_path / "findings.json"
    out.write_text(
        '{"findings": [{"id": "stale", "pattern": "P14.9", '
        '"workflow_file": "old.yml", "line": 1}]}',
        encoding="utf-8",
    )
    empty = tmp_path / "empty-repo"
    empty.mkdir()

    proc = _run(["--root", str(empty), "--out", str(out), "--gh-impostor", "off"])
    assert proc.returncode != 0
    assert not out.exists(), (
        "a failed run left a previous run's findings at the fixed path "
        "SKILL.md promises is empty on failure"
    )


# A scan.py stand-in that records the argv it was handed and emits a minimal
# valid findings document, so the driver's forwarding is what's under test.
_FAKE_SCAN = '''\
import json, os, sys
from pathlib import Path
Path(os.environ["CI_SECURE_FAKE_ARGV_OUT"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"findings": [], "timings": {"run_start_epoch": 1.0}}))
'''


def test_flags_are_forwarded_to_scan(tmp_path: Path) -> None:
    """Every driver flag must actually reach scan.py.

    Asserting this against the real scan.py is environment-dependent — on a
    machine with no authenticated gh, `--gh-impostor on` and `off` produce the
    same visible outcome, so a driver that dropped the flag entirely would
    pass. A stand-in scan.py that records its own argv makes the forwarding
    the thing under test.
    """
    fake_dir = tmp_path / "scripts"
    fake_dir.mkdir()
    (fake_dir / "scan.py").write_text(_FAKE_SCAN, encoding="utf-8")
    (fake_dir / "run.py").write_text(_RUN_SCRIPT.read_text(), encoding="utf-8")
    argv_out = tmp_path / "argv.json"

    for mode in ("on", "off", "auto"):
        proc = subprocess.run(
            [sys.executable, str(fake_dir / "run.py"),
             "--root", str(_FIXTURES), "--out", str(tmp_path / "f.json"),
             "--gh-impostor", mode, "--repo", "owner/name",
             "--catalog", "/some/catalog.md"],
            capture_output=True, text=True,
            env={**os.environ, "CI_SECURE_FAKE_ARGV_OUT": str(argv_out)},
        )
        assert proc.returncode == 0, proc.stderr
        forwarded = json.loads(argv_out.read_text(encoding="utf-8"))
        assert ["--gh-impostor", mode] == _pair(forwarded, "--gh-impostor")
        assert ["--repo", "owner/name"] == _pair(forwarded, "--repo")
        assert ["--catalog", "/some/catalog.md"] == _pair(forwarded, "--catalog")
        assert ["--root", str(_FIXTURES)] == _pair(forwarded, "--root")


def _pair(argv: list[str], flag: str) -> list[str]:
    assert flag in argv, f"{flag} was never forwarded to scan.py: {argv}"
    i = argv.index(flag)
    return argv[i:i + 2]
