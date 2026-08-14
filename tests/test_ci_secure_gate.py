"""The gate's verdict rule, exercised against every shape the engine can return.

`.github/scripts/ci_secure_gate.py` is the only thing standing between "the scan
process exited 0" and "this repo passed its security scan", and those are very
different claims. The engine is deliberately crash-tolerant: its config-facts
layer is wrapped in a catch-all that returns an empty `facts` list with a null
score rather than killing the scan, and a scan with incomplete coverage still
exits 0. An empty list of facts contains no failures, so the naive reading of the
engine's output turns "the scoring layer crashed" into "0/0 facts pass" and ships
it green — a scan that measured nothing, reported as clean.

Every test below drives the real gate script as a subprocess against a stub
engine (via `CI_SECURE_ENGINE`), because the gate's contract IS its exit code.
The stub lets us produce shapes the real engine only reaches when something has
gone wrong, which is exactly where a gate earns its keep.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GATE = _REPO / ".github" / "scripts" / "ci_secure_gate.py"

# A scan of a repo with nothing wrong with it: the only shape that may exit 0.
_CLEAN = {
    "scanned_workflows": 3,
    "findings": [],
    "scan_incomplete": [],
    "gh_checks": {"P14.11": "skipped: disabled via --gh-impostor=off"},
    "security_score": {
        "facts": [
            {"fact_id": "sec.codeowners.workflows", "outcome": "pass",
             "evidence": "CODEOWNERS covers .github/"},
            {"fact_id": "sec.actions.pinned", "outcome": "pass",
             "evidence": "every action is pinned to a commit SHA"},
        ],
        "score": 100, "passed": 2, "scored_count": 2,
        "applicable_count": 2, "unmeasured": [],
    },
}


def _scan(**overrides) -> dict:
    """A copy of the clean scan with top-level keys replaced."""
    scan = json.loads(json.dumps(_CLEAN))
    for key, value in overrides.items():
        if key == "security_score":
            scan["security_score"].update(value)
        else:
            scan[key] = value
    return scan


def run_gate(tmp_path: Path, *, stdout: str, returncode: int = 0, engine: str | None = None):
    """Run the real gate against a stub engine that prints `stdout` and exits `returncode`."""
    stub = tmp_path / "stub_engine.py"
    stub.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    proc = subprocess.run(
        [sys.executable, str(_GATE)],
        capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_WORKSPACE": str(workspace),
            "CI_SECURE_ENGINE": engine if engine is not None else str(stub),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    proc.summary = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return proc


def run_scan(tmp_path: Path, scan: dict, **kwargs):
    return run_gate(tmp_path, stdout=json.dumps(scan), **kwargs)


# --------------------------------------------------------------------------
# The one green path
# --------------------------------------------------------------------------

def test_a_clean_scan_passes(tmp_path: Path) -> None:
    proc = run_scan(tmp_path, _CLEAN)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::error::" not in proc.stdout
    assert "sec.actions.pinned" in proc.summary, "the summary must show what was checked"
    assert "P14.11" in proc.summary, (
        "a detector that did not run must be reported: 'no findings from it' and "
        "'it never ran' cannot look the same to a reader"
    )


# --------------------------------------------------------------------------
# Real failures
# --------------------------------------------------------------------------

def test_a_failed_fact_is_red_and_named(tmp_path: Path) -> None:
    scan = _scan()
    scan["security_score"]["facts"][0].update(
        outcome="fail", evidence="no CODEOWNERS entry covers .github/")
    scan["security_score"].update(score=50, passed=1)
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 1
    assert "::error::ci-secure fact failed: sec.codeowners.workflows" in proc.stdout
    assert "**FAIL**" in proc.summary


def test_incomplete_coverage_is_red(tmp_path: Path) -> None:
    """A workflow the engine could not read is a false negative, not a pass."""
    proc = run_scan(tmp_path, _scan(scan_incomplete=[
        {"workflow_file": ".github/workflows/x.yml", "reason": "unparseable YAML"}]))
    assert proc.returncode == 1
    assert "scan incomplete" in proc.stdout


# --------------------------------------------------------------------------
# Degraded shapes: the engine exits 0, but there is no verdict in what it said
# --------------------------------------------------------------------------

def test_engine_crash_is_red(tmp_path: Path) -> None:
    proc = run_gate(tmp_path, stdout="", returncode=2)
    assert proc.returncode == 1
    assert "engine failed to run" in proc.stdout


def test_a_missing_engine_is_red_and_names_the_path(tmp_path: Path) -> None:
    """The portability escape hatch must not turn a typo into a silent pass."""
    missing = tmp_path / "nowhere" / "scan.py"
    proc = run_gate(tmp_path, stdout="", engine=str(missing))
    assert proc.returncode == 1
    assert "engine not found" in proc.stdout and str(missing) in proc.stdout


@pytest.mark.parametrize("payload", ["", "not json at all", "null", "[]", "{}"])
def test_unreadable_output_is_red(tmp_path: Path, payload: str) -> None:
    proc = run_gate(tmp_path, stdout=payload)
    assert proc.returncode == 1, f"{payload!r} exited {proc.returncode}"
    assert "no readable verdict" in proc.stdout


@pytest.mark.parametrize("missing_key", ["findings", "scan_incomplete"])
def test_a_dropped_schema_key_is_red_not_assumed_empty(tmp_path: Path, missing_key: str) -> None:
    """If the engine stops reporting coverage gaps, the gate must not read that as zero gaps."""
    scan = _scan()
    del scan[missing_key]
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 1
    assert "no readable verdict" in proc.stdout


def test_the_facts_layer_degrading_to_empty_is_red(tmp_path: Path) -> None:
    """The engine's own catch-all shape: facts=[], score=None, still exit 0.

    This is the headline case. Nothing here is a "fail", so a gate that only
    looks for failures reports `0/0 facts pass` and goes green on a scan that
    measured nothing.
    """
    proc = run_scan(tmp_path, _scan(security_score={
        "facts": [], "score": None, "passed": 0, "scored_count": 0,
        "applicable_count": 0, "unmeasured": [],
        "reason": "config-facts layer failed: ImportError(...)",
    }))
    assert proc.returncode == 1
    assert "no usable verdict" in proc.stdout
    assert "config-facts layer failed" in proc.stdout, "the engine's own reason must reach the log"


def test_scanning_nothing_is_red(tmp_path: Path) -> None:
    """Zero workflow files scanned is an unpointed gate, not a clean repo."""
    proc = run_scan(tmp_path, _scan(scanned_workflows=0))
    assert proc.returncode == 1
    assert "no workflow files were scanned" in proc.stdout


def test_a_null_score_is_red(tmp_path: Path) -> None:
    proc = run_scan(tmp_path, _scan(security_score={"score": None}))
    assert proc.returncode == 1
    assert "no usable verdict" in proc.stdout


def test_an_unrecognised_outcome_is_red(tmp_path: Path) -> None:
    """A new outcome the gate has never seen must not be bucketed as "not a fail".

    Treating unknown values as passing is how a future engine release that adds,
    say, an "error" outcome would ship silently green.
    """
    scan = _scan()
    scan["security_score"]["facts"][0]["outcome"] = "error"
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 1
    assert "unrecognised fact outcome" in proc.stdout
    assert "**ERROR**" in proc.summary, (
        "the summary a human reads must not disagree with the exit code: an unknown "
        "outcome may not be rendered as PASS"
    )


# --------------------------------------------------------------------------
# Loud but non-blocking
# --------------------------------------------------------------------------

def test_findings_warn_without_blocking(tmp_path: Path) -> None:
    """Severity-rated findings are accepted decisions, but never silent ones."""
    proc = run_scan(tmp_path, _scan(findings=[{
        "severity": "MEDIUM", "pattern": "P14.18", "title": "write token on comment trigger",
        "workflow_file": ".github/workflows/claude.yml", "line": 62}]))
    assert proc.returncode == 0, proc.stdout
    assert "::warning file=.github/workflows/claude.yml,line=62::" in proc.stdout
    assert "P14.18" in proc.summary


def test_an_unmeasured_fact_warns_without_blocking(tmp_path: Path) -> None:
    """Unmeasured is neither a pass nor a fail, and must render as neither."""
    scan = _scan()
    scan["security_score"]["facts"][0].update(
        outcome="unmeasured", evidence="no CODEOWNERS file to evaluate")
    scan["security_score"].update(scored_count=1, passed=1, score=100)
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 0, proc.stdout
    assert "::warning::ci-secure fact unmeasured: sec.codeowners.workflows" in proc.stdout
    assert "UNMEASURED `sec.codeowners.workflows`" in proc.summary
    assert "PASS `sec.codeowners.workflows`" not in proc.summary


def test_every_failure_reason_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """A red build must list all of its causes, so one fix does not reveal the next."""
    scan = _scan(scanned_workflows=0, scan_incomplete=[
        {"workflow_file": "a.yml", "reason": "unreadable"}])
    scan["security_score"]["facts"][1].update(outcome="fail", evidence="an unpinned action")
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 1
    assert "no workflow files were scanned" in proc.stdout
    assert "scan incomplete" in proc.stdout
    assert "ci-secure fact failed: sec.actions.pinned" in proc.stdout
