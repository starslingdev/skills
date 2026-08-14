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
    "dropped_matches": [],
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


@pytest.mark.parametrize("missing_key", [
    "findings", "scan_incomplete", "dropped_matches", "gh_checks"])
def test_a_dropped_schema_key_is_red_not_assumed_empty(tmp_path: Path, missing_key: str) -> None:
    """If the engine stops reporting coverage gaps, the gate must not read that as zero gaps."""
    scan = _scan()
    del scan[missing_key]
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 1
    assert "no readable verdict" in proc.stdout


def test_a_dropped_match_is_red(tmp_path: Path) -> None:
    """A finding the detector produced and then discarded is a false negative.

    The engine separates "this file could not be read" (`scan_incomplete`) from
    "a detector matched inside a run: step it could not anchor to a line, so the
    match was thrown away" (`dropped_matches`). Its own coverage predicate treats
    them as the same class of hole, so a gate that blocks on one and ignores the
    other reports 100 over a scan that admits it dropped a hit.
    """
    proc = run_scan(tmp_path, _scan(dropped_matches=[
        {"workflow_file": ".github/workflows/deploy.yml",
         "reason": "run: step could not be anchored to a raw line"}]))
    assert proc.returncode == 1
    assert "dropped" in proc.stdout and "deploy.yml" in proc.stdout


def test_an_empty_facts_list_alone_is_red(tmp_path: Path) -> None:
    """The `evaluated nothing` clause, isolated from the other degraded checks.

    Covered only in combination before, so deleting the clause changed nothing:
    the crash-shape test also sets `score: None` and `reason`, which fire on their
    own. This is the drift the clause exists for — a numeric score over no facts.
    """
    proc = run_scan(tmp_path, _scan(security_score={
        "facts": [], "score": 0, "passed": 0, "scored_count": 0,
        "applicable_count": 0, "unmeasured": []}))
    assert proc.returncode == 1
    assert "evaluated nothing" in proc.stdout


def test_a_missing_network_detector_status_is_red(tmp_path: Path) -> None:
    """The gate always disables P14.11 explicitly, so a status must always exist.

    Reading `gh_checks` with a default would make "the detector was skipped" and
    "the engine stopped saying whether it ran" look identical — the exact
    collapse the surrounding comment promises not to allow.
    """
    proc = run_scan(tmp_path, _scan(gh_checks={}))
    assert proc.returncode == 1
    assert "network-gated detector status" in proc.stdout


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


# --------------------------------------------------------------------------
# Untrusted text reaching the annotation and summary sinks
# --------------------------------------------------------------------------

# Everything the gate reports is read out of scanned workflow files, and on a
# fork pull request an attacker writes those files — including their names, which
# the engine reports verbatim. A newline in one of those strings would otherwise
# let it emit workflow commands of its own. Each command name is assembled at run
# time rather than written out, so no scanner reading this file finds a literal
# workflow command sitting in a test fixture.
_STOP = "::" + "stop" + "-commands::"
_MASK = "::" + "add" + "-mask::"
_ERR = "::" + "err" + "or::"


def _hostile(text: str) -> dict:
    """A scan whose every untrusted string carries an embedded workflow command."""
    scan = _scan(findings=[{
        "severity": "HIGH", "pattern": "P14.10", "title": text,
        "workflow_file": text, "line": text}])
    scan["security_score"]["facts"][0].update(
        outcome="fail", fact_id=text, evidence=text)
    scan["gh_checks"] = {text: text}
    return scan


@pytest.mark.parametrize("payload", [
    "evil\n" + _STOP + "deadbeef\nfoo.yml",
    "a\r\n" + _ERR + "forged\r\nb.yml",
    "x\n" + _MASK + "secret\ny.yml",
])
def test_a_crafted_filename_cannot_emit_its_own_workflow_commands(
    tmp_path: Path, payload: str
) -> None:
    """No line the gate prints may begin with `::` unless the gate wrote it.

    `::stop-commands::` is the one that matters: it switches command parsing off
    for the rest of the step, which would silence the `::error::` annotations
    this gate emits for genuine failures. The exit code is computed in Python and
    no workflow command can touch it, so the build still goes red either way —
    but "the finding does not block" must not decay into "the finding does not
    appear", and a fork must not be able to blind the check tab at will.
    """
    proc = run_scan(tmp_path, _hostile(payload))
    assert proc.returncode == 1, "a failed fact is still a failed fact"

    # Actions reads a workflow command only where one STARTS a line, so the
    # invariant is per-line. Counting, not prefix-matching: a forged `::error::`
    # would satisfy "starts with ::error", so the assertion has to be that the
    # gate emitted EXACTLY the commands it meant to — one warning for the one
    # finding, one error for the one failed fact — and nothing else.
    emitted = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
               if ln.strip().startswith("::")]
    assert len(emitted) == 2, f"expected exactly 2 workflow commands, got {emitted}"
    assert sum(ln.startswith("::warning file=") for ln in emitted) == 1
    assert sum(ln.startswith("::error::ci-secure fact failed") for ln in emitted) == 1
    # Escaped rather than dropped: the payload survives mid-line, percent-encoded,
    # so the evidence a reviewer needs is still legible.
    assert "%0A" in proc.stdout


def test_a_crafted_filename_cannot_rewrite_an_annotation_s_properties(
    tmp_path: Path
) -> None:
    """`:` and `,` terminate a workflow command's property list.

    Left unescaped in `file=`, a `::` in the filename ends the property list and
    the attacker's text becomes the annotation body, while an embedded `,line=`
    re-points the annotation at a file and line of their choosing — a real finding
    displayed against innocent code.
    """
    proc = run_scan(tmp_path, _scan(findings=[{
        "severity": "HIGH", "pattern": "P14.10", "title": "t",
        "workflow_file": "a.yml" + _ERR + "forged clean,line=1", "line": 7}]))
    assert proc.returncode == 0

    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("::warning"))
    assert "%3A" in line and "%2C" in line, f"properties not escaped: {line}"
    # The command's own structure is intact: exactly one property list, ending at
    # the real line number the engine reported.
    assert line.count("::") == 2, f"the payload split the command: {line}"
    assert ",line=7::" in line


@pytest.mark.parametrize("sink", ["crash", "unreadable"])
def test_engine_output_cannot_emit_workflow_commands(tmp_path: Path, sink: str) -> None:
    """The engine's own stdout and stderr are attacker-controlled on a fork PR.

    Both the crash path and the unreadable-verdict path echo engine output to the
    log. Neither had a test, and `::stop-commands::` reaching the log from either
    would silence the `::error::` the gate emits immediately afterwards.
    """
    payload = "boom\n" + _STOP + "deadbeef\nmore"
    if sink == "crash":
        # A separate filename: run_gate() writes its own stub_engine.py, which
        # would overwrite this one and quietly test nothing.
        stub = tmp_path / "noisy_engine.py"
        stub.write_text(
            "import sys\n"
            f"sys.stderr.write({payload!r})\n"
            "sys.exit(2)\n", encoding="utf-8")
        proc = run_gate(tmp_path, stdout="", engine=str(stub))
    else:
        proc = run_gate(tmp_path, stdout=payload)

    assert proc.returncode == 1
    emitted = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
               if ln.strip().startswith("::")]
    assert len(emitted) == 1, f"expected only the gate's own error, got {emitted}"
    assert emitted[0].startswith("::error::ci-secure")
    # Echoed legibly rather than percent-encoded: this is what a maintainer reads
    # when the gate goes red for a reason that is not a security finding.
    assert "engine| boom" in proc.stderr


def test_a_crafted_filename_cannot_break_out_of_the_summary(tmp_path: Path) -> None:
    """A newline in the Markdown sink would let a filename render its own list items."""
    proc = run_scan(tmp_path, _hostile("evil\n- PASS `all good`\nfoo.yml"))
    assert proc.returncode == 1
    # Collapsed onto one line, the payload is inert text inside a row the gate
    # wrote. On its own line it would be a row of its own, reading as a pass that
    # the exit code contradicts — so the invariant is that no summary line the
    # gate did not author exists at all.
    forged = [ln for ln in proc.summary.splitlines() if ln.strip() == "- PASS `all good`"]
    assert not forged, "a crafted filename rendered its own summary row"
    assert any("evil - PASS `all good` foo.yml" in ln for ln in proc.summary.splitlines()), (
        "the filename should still be readable in the row the gate wrote, just flattened"
    )


GITHUB_SUMMARY_LIMIT_BYTES = 1024 * 1024  # 1 MiB, GitHub's documented cap


@pytest.mark.parametrize("filler,label", [
    ("x", "ascii"),
    # Three bytes per character. A budget counted in characters passes this
    # happily while the upload is ~3x over the real limit — and on a fork PR the
    # filenames and evidence in a summary are attacker-chosen, so non-ASCII here
    # is a choice an attacker gets to make, not an accident of someone's locale.
    ("漢", "multibyte"),
])
def test_the_summary_is_bounded_in_bytes(tmp_path: Path, filler: str, label: str) -> None:
    """GitHub rejects an oversized summary outright, so too big means none at all.

    The limit it enforces is bytes; measuring characters is the bug this pins.
    """
    scan = _scan(findings=[
        {"severity": "LOW", "pattern": f"P{i}", "title": filler * 500,
         "workflow_file": "w.yml", "line": i}
        for i in range(4000)])
    proc = run_scan(tmp_path, scan)
    assert proc.returncode == 0

    size = len(proc.summary.encode("utf-8"))
    assert size < GITHUB_SUMMARY_LIMIT_BYTES, (
        f"{label}: summary is {size} bytes, over GitHub's {GITHUB_SUMMARY_LIMIT_BYTES}"
    )
    assert "truncated" in proc.summary
    # Truncation cuts on a line boundary, so the last row is never half-rendered.
    assert proc.summary.rstrip().endswith("_(summary truncated; see the step log)_")


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
