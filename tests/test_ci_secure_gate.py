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


def run_gate(tmp_path: Path, *, stdout: str, returncode: int = 0,
             engine: str | None = None, env_extra: dict[str, str] | None = None,
             args: tuple[str, ...] = ()):
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

    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_WORKSPACE": str(workspace),
        "CI_SECURE_ENGINE": engine if engine is not None else str(stub),
        "GITHUB_STEP_SUMMARY": str(summary),
        # Explicit, because the gate refuses an unset value on purpose. A
        # harness that omitted it would exercise a shape no real job produces.
        "CI_SECURE_GH_IMPOSTOR": "off",
    }
    env.update(env_extra or {})
    # A None in `env_extra` UNSETS the variable, which is how a test reaches
    # "the job did not decide" rather than merely "the job decided badly".
    env = {k: v for k, v in env.items() if v is not None}
    proc = subprocess.run(
        [sys.executable, str(_GATE), *args],
        capture_output=True, text=True, env=env,
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
    # finding, one warning for the network-gated check this fixture reports as
    # incomplete, one error for the one failed fact — and nothing else.
    emitted = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
               if ln.strip().startswith("::")]
    assert len(emitted) == 3, f"expected exactly 3 workflow commands, got {emitted}"
    assert sum(ln.startswith("::warning file=") for ln in emitted) == 1
    assert sum(ln.startswith("::warning::ci-secure network-gated check")
               for ln in emitted) == 1
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
    # The backticks in the payload are neutralized as well as flattened: they
    # would otherwise close the code span the gate wraps values in, which is a
    # second breakout that needs no newline at all.
    assert any("evil - PASS 'all good' foo.yml" in ln for ln in proc.summary.splitlines()), (
        "the filename should still be readable in the row the gate wrote, just flattened"
    )


# --------------------------------------------------------------------------
# The network-gated check: on or off, never "whatever the runner happened to have"
# --------------------------------------------------------------------------

def _engine_argv(tmp_path: Path, env_extra: dict[str, str] | None = None):
    """Run a stub engine that reports the argv the gate invoked it with."""
    stub = tmp_path / "argv_engine.py"
    argv_file = tmp_path / "argv.json"
    # Written to a file, not to stderr: the gate captures the engine's stderr
    # and only echoes it on a failing run, so a stub that reported there would
    # be invisible on exactly the green path this asserts about.
    stub.write_text(
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write({json.dumps(_CLEAN)!r})\n",
        encoding="utf-8")
    summary = tmp_path / "summary.md"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": str(workspace),
           "CI_SECURE_ENGINE": str(stub), "GITHUB_STEP_SUMMARY": str(summary),
           "CI_SECURE_GH_IMPOSTOR": "off"}
    env.update(env_extra or {})
    env = {k: v for k, v in env.items() if v is not None}   # None unsets
    proc = subprocess.run([sys.executable, str(_GATE)],
                          capture_output=True, text=True, env=env)
    proc.summary = summary.read_text(encoding="utf-8") if summary.exists() else ""
    proc.argv = (json.loads(argv_file.read_text(encoding="utf-8"))
                 if argv_file.exists() else [])
    return proc


def test_the_summary_header_is_escaped_like_every_other_row(tmp_path: Path) -> None:
    """The headline interpolates engine fields too, and they are not trustworthy.

    On a fork pull request the engine IS attacker code, so the counts it
    reports are attacker-chosen strings, not integers. The header was the one
    line where that was easy to forget — it renders numbers, and numbers feel
    safe — but a newline in `passed` starts a row of its own, above every real
    result, where a reader is most likely to stop.
    """
    scan = _scan()
    scan["security_score"].update(passed="2\n- PASS `everything is fine`")
    scan["security_score"]["facts"][0].update(outcome="fail")

    proc = run_scan(tmp_path, scan)

    assert proc.returncode == 1
    # Anchored at the START of a line, not compared whole: the payload lands
    # mid-sentence, with the rest of the header trailing after it, so an
    # equality check would pass while the forged row rendered perfectly well.
    forged = [ln for ln in proc.summary.splitlines()
              if ln.strip().startswith("- PASS `everything is fine`")]
    assert not forged, (
        f"the header forged a summary row above the real results:\n{proc.summary}")


def test_a_summary_that_cannot_be_written_still_leaves_a_verdict(tmp_path: Path) -> None:
    """The verdict is emitted BEFORE the summary, so a cosmetic sink cannot eat it.

    GITHUB_STEP_SUMMARY is a file GitHub provisions and occasionally cannot be
    written. If the summary were written first, an OSError there would take the
    whole failure list down with it and the operator would get a traceback
    about a summary file instead of the security failure that caused the red.
    The build still fails either way — but "the gate went red for an unstated
    reason" is how a real finding gets waved through as flakiness.
    """
    scan = _scan()
    scan["security_score"]["facts"][0].update(
        outcome="fail", evidence="no CODEOWNERS entry covers .github/")

    # A directory where a file is expected: writing to it raises OSError.
    unwritable = tmp_path / "summary_dir"
    unwritable.mkdir()
    proc = run_scan(tmp_path, scan, env_extra={"GITHUB_STEP_SUMMARY": str(unwritable)})

    assert proc.returncode == 1
    assert "::error::ci-secure fact failed: sec.codeowners.workflows" in proc.stdout, (
        "the verdict was lost when the summary could not be written")
    assert "could not write the job summary" in proc.stdout


def test_the_scan_root_defaults_to_the_gates_own_tree_off_actions(tmp_path: Path) -> None:
    """With no GITHUB_WORKSPACE, the scan root is the repository the gate is in.

    Two paths matter and only one of them is exercised by CI. Under Actions
    GITHUB_WORKSPACE is always set, so the fallback is the path a maintainer
    hits running the gate by hand — and if it resolved to the wrong directory
    the engine would scan a tree with no workflows in it, which the gate calls
    "no workflow files were scanned" and reds. Silent, confusing, and only
    reachable off CI, which is exactly why it is pinned here.

    The scan root is also the ONLY thing GITHUB_WORKSPACE may decide. The
    engine and the rule are resolved from the gate's own location regardless;
    see test_ci_secure_gate_resolution.py.
    """
    proc = _engine_argv(tmp_path, {"GITHUB_WORKSPACE": ""})

    assert "--root" in proc.argv, "the gate must always tell the engine what to scan"
    assert proc.argv[proc.argv.index("--root") + 1] == str(_REPO), (
        "off Actions the scan root falls back to the gate's own repository root")


def test_the_impostor_check_is_always_passed_explicitly(tmp_path: Path) -> None:
    """`auto` is never sent, and the flag is never simply omitted.

    Omitting it lands on the engine's `auto` default, which runs the check iff
    an authenticated `gh` happens to be on the runner. That makes "did this
    security check run?" a property of the runner image rather than of the
    workflow — the silent-skip trap, one image rebuild away.
    """
    proc = _engine_argv(tmp_path, {"CI_SECURE_GH_IMPOSTOR": "off"})
    assert "--gh-impostor" in proc.argv, "the flag must be explicit, never defaulted"
    assert proc.argv[proc.argv.index("--gh-impostor") + 1] == "off"


def test_an_unset_impostor_setting_is_red_like_any_other_non_decision(
        tmp_path: Path) -> None:
    """UNSET is refused, not quietly read as "off".

    This is the case the variable exists for. A default of "off" made deleting
    the `env:` block from a scan job — a plausible refactor — silently stop the
    network-gated check on every run, which is the precise outcome the gate's
    own refusal message claims is impossible. "Nobody decided" and "the job
    decided not to" must not produce the same scan.
    """
    proc = _engine_argv(tmp_path, {"CI_SECURE_GH_IMPOSTOR": None})

    assert proc.returncode == 1, (
        "an unset CI_SECURE_GH_IMPOSTOR was accepted; whether the network-gated "
        "check runs must be a decision the job makes, never a default")
    assert "CI_SECURE_GH_IMPOSTOR" in proc.stdout
    assert proc.argv == [], "the engine must not even be invoked without a decision"


def test_a_strict_setting_that_is_not_a_flag_is_red(tmp_path: Path) -> None:
    """`true`/`yes`/`schedule` read as strict to a human and all meant lax.

    Strict mode is what makes the weekly run red on an incomplete network
    check, and that run is the only way a pin that ROTS after merge is ever
    noticed. A value that silently disables it mutes exactly that.
    """
    for value in ("true", "yes", "schedule", "on"):
        proc = run_scan(tmp_path, _CLEAN, env_extra={"CI_SECURE_GH_STRICT": value})
        assert proc.returncode == 1, f"{value!r} was accepted as a strict setting"
        assert "CI_SECURE_GH_STRICT" in proc.stdout


def test_an_unset_strict_setting_means_lax_and_is_allowed(tmp_path: Path) -> None:
    """Unlike the impostor flag, unset strict is legitimate: the fork job omits it.

    The dial changes SEVERITY, not coverage — lax still discloses on both
    surfaces — so there is no silent-skip to prevent here, and refusing unset
    would red the fork job for doing the right thing.
    """
    proc = run_scan(tmp_path, _CLEAN, env_extra={"CI_SECURE_GH_STRICT": None})
    assert proc.returncode == 0, proc.stdout


def test_the_scan_root_is_the_workspace_the_job_checked_out(tmp_path: Path) -> None:
    """GITHUB_WORKSPACE decides the scan root — the audited half of the two-tree rule.

    Only the FALLBACK was pinned before, and in this repo the two paths
    coincide, so a gate that ignored the workspace entirely would pass CI here
    and silently scan its own directory instead of the adopter's repository in
    a vendored install.
    """
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    proc = _engine_argv(tmp_path, {"GITHUB_WORKSPACE": str(workspace)})

    assert proc.argv[proc.argv.index("--root") + 1] == str(workspace), (
        "the scan root must be the tree the job checked out, not the gate's own")


def test_a_rule_that_raises_on_load_is_red_with_a_stated_cause(tmp_path: Path) -> None:
    """A config.py that blows up on import must not be an unexplained red.

    Loading the rule EXECUTES it, and in a vendored layout that code sits
    beside the engine — so this is the likeliest line in the gate to raise, not
    the least. It runs at module scope, outside main()'s handler, so it used to
    exit non-zero with a bare traceback and no ::error:: annotation.
    """
    engine_dir = tmp_path / "vendored"
    engine_dir.mkdir()
    (engine_dir / "config.py").write_text(
        "raise RuntimeError('rule exploded on import')\n", encoding="utf-8")
    engine = engine_dir / "scan.py"
    engine.write_text("import sys\nsys.stdout.write('{}')\n", encoding="utf-8")

    proc = run_scan(tmp_path, _CLEAN, engine=str(engine))

    assert proc.returncode == 1
    assert "::error::" in proc.stdout, (
        "a rule that raises on load produced a red with no stated cause")
    assert "could not be loaded" in proc.stdout


def test_a_rule_missing_the_coherence_predicate_says_so(tmp_path: Path) -> None:
    """`coverage_is_complete` is CALLED, so its absence is a named disagreement.

    Left out of the presence check, a vendored rule missing only that name
    reached the crash handler and reported an AttributeError — still red, but
    the adopter is told about a Python attribute instead of about the rule.
    """
    engine_dir = tmp_path / "vendored_partial"
    engine_dir.mkdir()
    (engine_dir / "config.py").write_text(
        "BLOCKING_OUTCOMES = frozenset({'fail'})\n"
        "KNOWN_OUTCOMES = frozenset({'pass', 'fail'})\n"
        "OUTCOME_MARKS = {'pass': 'PASS', 'fail': 'FAIL'}\n"
        "def flatten_scanned(v):\n    return '' if v is None else str(v)\n",
        encoding="utf-8")
    engine = engine_dir / "scan.py"
    engine.write_text("import sys\nsys.stdout.write('{}')\n", encoding="utf-8")

    proc = run_scan(tmp_path, _CLEAN, engine=str(engine))

    assert proc.returncode == 1
    assert "coverage_is_complete" in proc.stdout
    assert "engine and gate disagree" in proc.stdout


def test_a_hung_engine_is_red_with_a_named_cause(tmp_path: Path) -> None:
    """The engine timeout must actually fire, and say why.

    Without it a hung engine runs until the JOB's own timeout cancels it, which
    reaches the check run as an unexplained cancellation rather than a stated
    verdict — and the job timeout is the thing this constant must stay under.
    """
    stub = tmp_path / "hang.py"
    stub.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_GATE)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": str(workspace),
             "CI_SECURE_ENGINE": str(stub), "GITHUB_STEP_SUMMARY": str(summary),
             "CI_SECURE_GH_IMPOSTOR": "off",
             "CI_SECURE_ENGINE_TIMEOUT_S": "1"})

    assert proc.returncode == 1
    assert "did not finish within" in proc.stdout, proc.stdout + proc.stderr


def test_the_job_decides_whether_the_impostor_check_runs(tmp_path: Path) -> None:
    """A job holding a token turns it ON; the same script, a different job, does not."""
    proc = _engine_argv(tmp_path, {"CI_SECURE_GH_IMPOSTOR": "on"})
    assert proc.argv[proc.argv.index("--gh-impostor") + 1] == "on"


def test_an_unrecognised_impostor_setting_is_red(tmp_path: Path) -> None:
    """Anything but on/off is red, and `auto` especially so.

    A typo must not silently become "off" — that is the difference between a
    check that was turned off deliberately and one that stopped running.
    """
    for value in ("auto", "yes", ""):
        proc = _engine_argv(tmp_path, {"CI_SECURE_GH_IMPOSTOR": value})
        assert proc.returncode == 1, f"{value!r} was accepted"
        assert "CI_SECURE_GH_IMPOSTOR" in proc.stdout


def test_a_partial_network_result_is_disclosed_and_never_reads_as_clean(
        tmp_path: Path) -> None:
    """`partial:` means some pins were never verified. That is not "no findings".

    Rate limiting is the ordinary cause, and it is reachable on purpose: an
    attacker who can burn the API quota can otherwise mute this check.
    """
    scan = _scan(gh_checks={"P14.11": "partial: 3 of 9 unique pin(s) verified, 0 "
                                      "flagged, 6 UNVERIFIED (network/rate-limit) "
                                      "— not treated as clean"})
    proc = run_scan(tmp_path, scan)

    assert proc.returncode == 0, "a partial result discloses on a PR, it does not block"
    assert "::warning::" in proc.stdout
    assert "did not complete" in proc.stdout.lower() or "partial" in proc.stdout.lower()
    assert "P14.11" in proc.summary


def test_a_partial_network_result_is_red_when_the_run_demands_completeness(
        tmp_path: Path) -> None:
    """The scheduled run against the default branch is the one that must be complete.

    A pull request cannot be held hostage to somebody else's rate limit, but
    the weekly run has no deadline and nothing to race — so there, anything
    short of "ran" is a red the maintainer sees rather than a warning nobody
    reads.
    """
    scan = _scan(gh_checks={"P14.11": "partial: 1 of 9 unique pin(s) verified, 0 "
                                      "flagged, 8 UNVERIFIED (network/rate-limit)"})
    proc = run_scan(tmp_path, scan, env_extra={"CI_SECURE_GH_STRICT": "1"})

    assert proc.returncode == 1
    assert "::error::" in proc.stdout
    assert "P14.11" in proc.stdout


def test_a_skipped_check_is_disclosed_in_the_summarys_first_lines(
        tmp_path: Path) -> None:
    """Both surfaces say it, and the summary says it before anything reassuring.

    A reader who stops after the headline must not come away thinking the scan
    covered what it did not. "No findings from P14.11" and "P14.11 never ran"
    are different claims, and only one of them is true here.
    """
    proc = run_scan(tmp_path, _CLEAN)

    head = "\n".join(proc.summary.splitlines()[:6])
    assert "did NOT run" in head or "did not run" in head, (
        f"the skip is not disclosed in the summary's first lines:\n{head}")


def test_the_summary_reports_counts_and_never_the_bare_aggregate(tmp_path: Path) -> None:
    """The CI surface obeys the same rule as the report: no bare score.

    `config_facts.py` registers the aggregate as machine-only — it exists so
    the scores of several engines can be blended, and the report renderer is
    forbidden from showing it, because a single number invites a reader to
    manage the number instead of the findings. "94" says nothing about which
    check failed; "13/14 facts pass of 14 applicable" says what to go fix.

    The gate is a second reader-facing surface and was quietly exempt. The
    assertion keys on the rendered CONSTRUCT rather than the digits: a bare
    numeral collides with line numbers and byte counts that legitimately
    appear in evidence text, so it could never fail honestly.
    """
    proc = run_scan(tmp_path, _CLEAN)

    assert proc.returncode == 0
    assert "score: **" not in proc.summary, (
        "the machine-only aggregate is rendered on the CI surface")
    assert "score: **" not in proc.stdout, (
        "the summary is printed to the step log as well as written to the "
        "summary file; the ban has to hold on both")
    assert "2/2 facts pass of 2 applicable" in proc.summary, (
        "dropping the number must not drop the counts that replace it")
    assert "3 workflow file(s) scanned" in proc.summary


def test_a_backtick_cannot_break_out_of_an_inline_code_span(tmp_path: Path) -> None:
    """Flattening newlines is not enough: the backtick is its own breakout.

    Three summary sinks wrap a value in an inline code span — the fact id, the
    finding's pattern, the network-detector name. A value carrying a backtick
    closes that span early and renders whatever follows as live Markdown, on
    the SAME line, which the newline-flattening rule never touches. The forgery
    that matters is a row reading like a passing check the gate did not write,
    on a run that is going red.
    """
    scan = _scan()
    scan["security_score"]["facts"][0].update(
        fact_id="sec.a` — **PASS** `sec.b",
        outcome="fail",
        evidence="`` broken out ``")
    scan["gh_checks"] = {"P14.11` — **ran** `x": "skipped"}
    scan["findings"] = [{"severity": "LOW", "pattern": "P1` — **clean** `P2",
                         "title": "t", "workflow_file": "w.yml", "line": 1}]

    proc = run_scan(tmp_path, scan)

    assert proc.returncode == 1

    # The invariant is span integrity, not the absence of the payload's text:
    # inside a code span `**PASS**` renders as those nine literal characters,
    # which is honest. It is the ESCAPE from the span that turns it into a
    # forged verdict, and an unbalanced backtick count is exactly that escape.
    for line in proc.summary.splitlines():
        assert line.count("`") % 2 == 0, (
            f"an odd number of backticks leaves a code span open: {line!r}")

    # Every crafted value keeps its own backticks neutralized, so none of them
    # can close the span the gate opened around it.
    assert "`sec.a' — **PASS** 'sec.b`" in proc.summary
    assert "`P1' — **clean** 'P2`" in proc.summary
    assert "`P14.11' — **ran** 'x`" in proc.summary


def test_a_pipe_cannot_forge_table_columns(tmp_path: Path) -> None:
    """The same values are copied into contexts where `|` splits a row.

    The gate's summary is a list today, but these values come from the same
    flattening rule the report's tables use, and a summary that grows a table
    later must not quietly reopen this. Escaping where the untrusted value
    enters keeps the guarantee independent of where it lands.
    """
    scan = _scan()
    scan["security_score"]["facts"][0].update(
        fact_id="sec.a|b", outcome="fail", evidence="ev|il")

    proc = run_scan(tmp_path, scan)

    assert proc.returncode == 1
    assert "sec.a\\|b" in proc.summary, "an unescaped pipe can split a row"
    assert "ev\\|il" in proc.summary


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


def test_a_rule_that_exits_cleanly_on_load_cannot_forge_a_pass(tmp_path: Path) -> None:
    """`sys.exit(0)` in a rule file must not become a green gate.

    This is why the handler around the config load catches `BaseException` and
    not `Exception`: `SystemExit` inherits from the former, so a `config.py`
    whose module body calls `sys.exit(0)` used to end the gate process right
    there — before any scan, with exit status 0 and no output at all. A scan
    that never ran, reported as a pass, which is the one outcome this whole
    file exists to make impossible.

    Loading the rule EXECUTES it, and in a vendored layout that file sits
    beside the engine rather than in this tree, so this is reachable by anyone
    who can write there. Narrowing the clause to `Exception` — a natural-looking
    tidy-up — brings the forged pass straight back.
    """
    engine_dir = tmp_path / "vendored_exit"
    engine_dir.mkdir()
    (engine_dir / "config.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8")
    engine = engine_dir / "scan.py"
    engine.write_text("import sys\nsys.stdout.write('{}')\n", encoding="utf-8")

    proc = run_scan(tmp_path, _CLEAN, engine=str(engine))

    assert proc.returncode == 1, (
        "a rule file that called sys.exit(0) produced a GREEN gate with no scan")
    assert "::error::" in proc.stdout, "the forged pass was turned red with no stated cause"
    assert "could not be loaded" in proc.stdout


# --------------------------------------------------------------------------
# `--advisory`: a ramp for findings, never a mute button for a broken scan
# --------------------------------------------------------------------------
#
# The flag ships ON in the workflow every adopter installs, so its SCOPE is the
# most load-bearing untested thing about it. Three separate surfaces promise
# that it downgrades failed FACTS and nothing else. Until these tests existed
# that promise was held up by a coincidence: the flag defaults off, so the
# blocking-mode tests above passed no matter how wide advisory's reach was.
# Widening it - `red = not ADVISORY` in the degraded loop, or gating STRICT_GH
# on it - left the whole suite green, which is exactly how a ramp becomes a mute
# button. `ADVISORY = False` (the flag as a silent no-op) was equally invisible.

_A_FAILED_FACT = _scan(security_score={
    "facts": [{"fact_id": "sec.codeowners.workflows", "outcome": "fail",
               "evidence": "no CODEOWNERS entry for .github/"}],
    "score": 0, "passed": 0, "scored_count": 1, "applicable_count": 1,
})


def test_advisory_downgrades_a_failed_fact_and_still_reports_it(
        tmp_path: Path) -> None:
    """The whole point of the flag: day one does not brick the merge path.

    Reported at full volume, just not blocking - "this passed" and "this failed
    and you have chosen not to be stopped by it yet" must stay distinguishable,
    or the ramp has quietly become a clean bill of health.
    """
    blocking = run_scan(tmp_path, _A_FAILED_FACT)
    assert blocking.returncode == 1, "without the flag a failed fact must block"

    proc = run_scan(tmp_path, _A_FAILED_FACT, args=("--advisory",))

    assert proc.returncode == 0, (
        "--advisory must downgrade a failed FACT, or the flag does nothing and "
        "the install bricks the adopter's merges on day one:\n" + proc.stdout)
    assert "::warning::" in proc.stdout and "advisory" in proc.stdout, (
        "a downgraded fact still has to be reported, loudly:\n" + proc.stdout)
    assert "sec.codeowners.workflows" in proc.stdout, (
        "the downgraded fact must still be named")
    assert "::error::" not in proc.stdout, (
        "nothing else in this scan is wrong, so nothing may red")
    assert "Advisory mode" in proc.summary, (
        "the summary a human reads must say the verdict was downgraded")


@pytest.mark.parametrize("label,scan,engine_rc", [
    ("the facts layer crashed to empty",
     _scan(security_score={"facts": [], "score": None, "passed": 0,
                           "scored_count": 0, "applicable_count": 0}), 0),
    ("nothing was scanned", _scan(scanned_workflows=0), 0),
    ("an outcome the rule cannot classify",
     _scan(security_score={
         "facts": [{"fact_id": "sec.demo", "outcome": "probably-fine",
                    "evidence": "x"}],
         "score": 100, "passed": 1, "scored_count": 1, "applicable_count": 1}), 0),
    ("the scan was incomplete", _scan(scan_incomplete=["ci.yml: unparseable"]), 0),
    ("a match was dropped", _scan(dropped_matches=["P14.1: regex timeout"]), 0),
    ("the engine crashed", _CLEAN, 3),
])
def test_advisory_never_downgrades_a_scan_that_cannot_be_trusted(
        tmp_path: Path, label: str, scan: dict, engine_rc: int) -> None:
    """Every shape that means the scan itself is untrustworthy stays red.

    These and a failed fact look identical from outside - a red check - and
    only one of them is safe to ignore. If advisory covered both, an adopter
    burning down findings would be silencing a broken scanner at the same time
    and could not tell.
    """
    proc = run_scan(tmp_path, scan, returncode=engine_rc, args=("--advisory",))

    assert proc.returncode == 1, (
        f"--advisory must NOT downgrade {label!r} - advisory is a ramp for "
        "findings, never a mute button for a broken scan:\n" + proc.stdout)
    assert "::error::" in proc.stdout, (
        "a red with no stated cause is the thing this gate argues against")


def test_advisory_never_downgrades_the_weekly_networks_completeness_demand(
        tmp_path: Path) -> None:
    """The one run that must not be silenceable by burning API quota.

    `CI_SECURE_GH_STRICT=1` is set only on the schedule trigger, whose whole
    job is catching a pin that ROTS. Letting `--advisory` reach it would mean
    an adopter still ramping their findings had no rot detection at all, and
    nothing would say so.
    """
    partial = _scan(gh_checks={"P14.11": "partial: rate limit exhausted"})

    proc = run_scan(tmp_path, partial, args=("--advisory",),
                    env_extra={"CI_SECURE_GH_STRICT": "1"})

    assert proc.returncode == 1, (
        "--advisory must not reach the weekly run's completeness demand:\n"
        + proc.stdout)
    assert "::error::" in proc.stdout


@pytest.mark.parametrize("argv", [
    ("--advisery",),               # the typo that must not silently block
    ("--advisory", "--no-block"),  # a second flag that must not be ignored
    ("-a",),
    ("--advisory=true",),
])
def test_the_gate_refuses_an_argument_it_does_not_recognise(
        tmp_path: Path, argv: tuple[str, ...]) -> None:
    """An unrecognised flag is a decision that did not land, so it reds.

    Without this, `--advisery` runs the gate in BLOCKING mode while the adopter
    believes they are ramping, and `--advisory --no-block` silently ignores the
    half the author cared about. Both are quiet, and both are wrong.
    """
    proc = run_scan(tmp_path, _CLEAN, args=argv)

    assert proc.returncode == 1, (
        f"the gate accepted {argv!r} instead of refusing it:\n" + proc.stdout)
    assert "unrecognised argument" in proc.stdout
    assert argv[-1] in proc.stdout or argv[0] in proc.stdout, (
        "the refusal has to name what it did not understand")
