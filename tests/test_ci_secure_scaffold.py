"""What an outside repository gets when someone sets ci-secure up as a gate.

The scaffold vendors the engine, the gate and the licence into the adopter's
repository and adds one workflow. That copy has to be as strong as the check we
run on ourselves, and there is no way to notice from inside our own CI if it
stops being — nobody here runs it. So these tests do three separate jobs:

1. **Identity.** The vendored gate is byte-for-byte the gate this repository
   runs. Two copies of a security-critical file exist because an installed
   skill has no `.github/` to read from; the cost of that is drift, and this is
   what refuses to let it happen.

2. **The declared delta.** The template is allowed to differ from our workflow
   in specific, enumerated ways — paths, runner topology, the advisory default.
   Anything else is a difference nobody decided on. The most dangerous one is
   the verdict job: ours arbitrates between two mutually exclusive scans and an
   adopter has one, so a verbatim copy would `needs:` a job that does not
   exist. The test asserts the predicate is well-formed against the template's
   OWN job set, which is what a "same shape" check would miss.

3. **Cleanliness.** The template is a workflow file, installed into
   `.github/workflows/`, and therefore scanned by the gate it just installed.
   If it trips a fact, every adopter reds the moment they go blocking — on a
   file we wrote. Delta conformance does not imply this: the declared delta
   itself could be the thing that trips a fact.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "skills" / "ci-secure"
_SCAFFOLD = _SKILL / "scaffold"
_TEMPLATE = _SCAFFOLD / "ci-secure.yml"
_OUR_WORKFLOW = _REPO / ".github" / "workflows" / "ci-secure-check.yml"
_OUR_GATE = _REPO / ".github" / "scripts" / "ci_secure_gate.py"
_VENDOR = _SKILL / "scripts" / "vendor.py"


def _load_vendor_module():
    """Import vendor.py directly, for the paths that need to fail mid-install."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ci_secure_vendor", _VENDOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ours() -> dict:
    return yaml.safe_load(_OUR_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses a bare `on:` key as the boolean True.
    return workflow[True] if True in workflow else workflow["on"]


# --------------------------------------------------------------------------
# 1. Identity
# --------------------------------------------------------------------------

def test_the_vendored_gate_is_byte_identical_to_the_one_we_run() -> None:
    """One gate, two locations. The day they differ, an adopter runs the weaker one."""
    assert _SCAFFOLD.joinpath("gate.py").read_bytes() == _OUR_GATE.read_bytes(), (
        "skills/ci-secure/scaffold/gate.py has drifted from "
        ".github/scripts/ci_secure_gate.py — copy the one we run on ourselves "
        "over the one we hand to other people, never the other way round")


# --------------------------------------------------------------------------
# 2. The declared delta
# --------------------------------------------------------------------------

def test_the_template_runs_the_gate_and_never_the_engine(template: dict) -> None:
    """`scan.py` exits 0 whatever it finds, so calling it directly is green forever."""
    steps = template["jobs"]["scan"]["steps"]
    run = [s["run"] for s in steps if "run" in s][-1]
    assert run.startswith("python3 ci-secure/scripts/gate.py"), run
    assert "scan.py" not in run, (
        "the engine reports; the gate decides. A step that calls the engine "
        "directly passes no matter what the scan found")
    assert steps[-1]["env"]["CI_SECURE_ENGINE"] == "ci-secure/scripts/scan.py", (
        "the vendored layout needs the engine pointer set; our in-tree default "
        "resolves to a path an adopter does not have")


def test_the_adopters_verdict_job_is_well_formed_against_its_own_jobs(
        template: dict) -> None:
    """The collapsed shape, checked against the template's real job set.

    Our verdict job `needs: [scan, scan-fork]` and passes when exactly one of
    them succeeded while the other skipped. Copied verbatim into a repository
    with one scan job, `needs.scan-fork.result` is empty, the arithmetic never
    matches, and the required check reds forever or the workflow is invalid.
    """
    jobs = template["jobs"]
    verdict = jobs["verdict"]

    assert verdict["name"] == "ci-secure", "the verdict job owns the required name"
    assert set(verdict["needs"]) <= set(jobs), (
        f"the verdict job needs {set(verdict['needs']) - set(jobs)}, which this "
        "template does not define")
    assert set(verdict["needs"]) == {"scan"}
    assert " ".join(str(verdict["if"]).split()) == "always()", (
        "a verdict job that does not always run is a required check that can be "
        "skipped, which GitHub reads as green")

    script = verdict["steps"][-1]["run"]
    assert 'if [ "${SCAN}" = "success" ]' in script, (
        "the pass condition must be scan-succeeded, not scan-did-not-fail: "
        "skipped and cancelled are not passes")
    assert "${{" not in script, "job results are read through env, never interpolated"

    # And no scan job may claim the required name for itself.
    for name, job in jobs.items():
        if name != "verdict":
            assert job.get("name") != "ci-secure"


def test_the_template_differs_from_ours_only_where_declared(
        template: dict, ours: dict) -> None:
    """Everything outside the enumerated delta must match, or nobody decided it.

    The declared delta, in full:
      - paths          the gate and engine live in a vendored `ci-secure/` dir
      - topology       one hosted scan job, not our self-hosted + fork pair, so
                       one `needs:` in the verdict job
      - advisory       the template ships `--advisory`; we do not
      - judge-from-main  absent: that is our-repo hardening, and an adopter's
                       fork exposure is theirs to decide
      - impostor check conditional on the event, because one job serves both
                       fork and non-fork pull requests
      - drift check    the template verifies its vendored copy; we have no
                       vendored copy to verify
    """
    assert set(_triggers(template)) == set(_triggers(ours)), (
        "the trigger sets are NOT part of the declared delta: an adopter gets "
        "the same weekly rot check we do")
    assert _triggers(template)["push"]["branches"] == ["main"]
    assert [e["cron"] for e in _triggers(template)["schedule"]], "no cron expression"

    assert template["permissions"] == ours["permissions"] == {"contents": "read"}
    assert "github.event_name" in template["concurrency"]["group"], (
        "without the event in the key, a push can cancel the weekly run")
    assert template["concurrency"]["cancel-in-progress"] is True

    # Topology: exactly the declared difference, nothing more.
    assert set(template["jobs"]) == {"scan", "verdict"}
    assert set(ours["jobs"]) == {"scan", "scan-fork", "verdict"}
    assert template["jobs"]["scan"]["runs-on"] == "ubuntu-latest", (
        "an adopter has no StarSling runners")

    # Every action pinned to a full SHA, same as ours — a template that
    # installed a floating tag would fail the check it installs.
    for job in template["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert "@" in step["uses"] and len(step["uses"].split("@")[1]) == 40, (
                    f"unpinned action in the template: {step['uses']}")

    assert "--advisory" in template["jobs"]["scan"]["steps"][-1]["run"], (
        "the template ships advisory so the installing pull request does not "
        "brick the adopter's merge path on day one")


def test_the_template_never_hands_fork_code_a_token(template: dict) -> None:
    """One job serves fork and non-fork pull requests; only one of them gets a token."""
    env = template["jobs"]["scan"]["steps"][-1]["env"]

    for key in ("CI_SECURE_GH_IMPOSTOR", "GH_TOKEN"):
        expr = " ".join(str(env[key]).split())
        # Either polarity is fine here — which way round it has to be written
        # is pinned by the evaluating test below, not by a substring.
        assert "head.repo.full_name" in expr and "github.repository" in expr, (
            f"{key} is not conditioned on the pull request being from a fork: "
            f"{expr}")
    assert "'auto'" not in str(env["CI_SECURE_GH_IMPOSTOR"]), (
        "`auto` makes a security check's presence depend on the runner image")


def _evaluate(expr: str, context: dict) -> str:
    """Evaluate a `${{ COND && A || B }}` workflow expression the way Actions does.

    The rule that matters here is that Actions has no ternary operator, only
    `&&`/`||` over truthiness — and an EMPTY STRING is falsy. So `cond && '' ||
    x` does not yield the empty string when `cond` holds; the empty middle is
    falsy, the `||` takes over, and the answer is always `x`. Asserting that an
    expression mentions the right condition cannot see that. Evaluating it can.

    This models `&&`, `||`, `==`, `!=` and parentheses over context values and
    single-quoted literals, which is all the two expressions here use. It does
    NOT model `!`, the functions, or Actions' type coercion and case-insensitive
    string comparison. Anything it cannot parse RAISES rather than guessing, so
    an expression written in a shape this does not cover fails the test instead
    of quietly passing it.
    """
    body = expr.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    body = " ".join(body[3:-2].split())

    def truthy(value: str) -> bool:
        # Actions: '' and false are falsy; every other string is truthy.
        return value not in ("", "false")

    def split_top(text: str, op: str) -> list[str]:
        """Split on `op` at parenthesis depth 0."""
        parts, depth, start, i = [], 0, 0, 0
        while i < len(text):
            char = text[i]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and text.startswith(op, i):
                parts.append(text[start:i])
                i += len(op)
                start = i
                continue
            i += 1
        parts.append(text[start:])
        return [p.strip() for p in parts]

    def evaluate(text: str) -> str:
        text = text.strip()
        alternatives = split_top(text, "||")
        if len(alternatives) > 1:
            result = ""
            for alternative in alternatives:
                result = evaluate(alternative)
                if truthy(result):
                    return result
            return result
        conjuncts = split_top(text, "&&")
        if len(conjuncts) > 1:
            result = "true"
            for conjunct in conjuncts:
                result = evaluate(conjunct)
                if not truthy(result):
                    return result
            return result
        return term(text)

    def term(token: str) -> str:
        token = token.strip()
        # A fully parenthesised group — `(a && b)`. Equal counts are NOT proof
        # of that: `(a) == (b)` also balances, and stripping its outer
        # characters yields `a) == (b`. The test is that depth never returns to
        # zero before the end, which is what makes the outer pair a matched
        # pair rather than two adjacent groups.
        if token.startswith("(") and token.endswith(")"):
            depth = 0
            for i, char in enumerate(token):
                depth += (char == "(") - (char == ")")
                if depth == 0 and i < len(token) - 1:
                    break
            else:
                return evaluate(token[1:-1])
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1]
        if token in context:
            return context[token]
        # A comparison, `a == b` or `a != b`, over context values.
        for op in ("!=", "=="):
            if op in token:
                left, right = (p.strip() for p in token.split(op, 1))
                equal = term(left) == term(right)
                return "true" if (equal if op == "==" else not equal) else ""
        raise AssertionError(f"unknown token in template expression: {token!r}")

    return evaluate(body)


_FORK_PR = {
    "github.event_name": "pull_request",
    "github.event.pull_request.head.repo.full_name": "someone-else/skills",
    "github.repository": "adopter/repo",
    "github.token": "TOKEN",
}
_SAME_REPO_PR = {
    "github.event_name": "pull_request",
    "github.event.pull_request.head.repo.full_name": "adopter/repo",
    "github.repository": "adopter/repo",
    "github.token": "TOKEN",
}
_DELETED_FORK_PR = {
    # GitHub nulls out `head.repo` once the fork is deleted; an unset context
    # value renders as the empty string.
    "github.event_name": "pull_request",
    "github.event.pull_request.head.repo.full_name": "",
    "github.repository": "adopter/repo",
    "github.token": "TOKEN",
}
_PUSH = {
    "github.event_name": "push",
    "github.event.pull_request.head.repo.full_name": "",
    "github.repository": "adopter/repo",
    "github.token": "TOKEN",
}
_SCHEDULE = dict(_PUSH, **{"github.event_name": "schedule"})


@pytest.mark.parametrize(
    ("name", "context", "impostor", "token"),
    [
        # Fork pull request: the check is disclosed as not run, and the step
        # gets NO credential. This is the case the whole expression exists for.
        ("fork pull request", _FORK_PR, "off", ""),
        ("deleted-fork pull request", _DELETED_FORK_PR, "off", ""),
        # Everything else is the adopter's own code and gets the check.
        ("same-repo pull request", _SAME_REPO_PR, "on", "TOKEN"),
        ("push", _PUSH, "on", "TOKEN"),
        ("schedule", _SCHEDULE, "on", "TOKEN"),
    ],
)
def test_the_template_withholds_the_token_on_fork_runs_when_evaluated(
    template: dict, name: str, context: dict, impostor: str, token: str,
) -> None:
    """Not "mentions the fork condition" — what the expression actually returns.

    A previous version conditioned the token correctly and still handed it to
    every fork pull request, because the withholding branch was an empty string
    on the left of `||` and Actions treats that as falsy.
    """
    env = template["jobs"]["scan"]["steps"][-1]["env"]
    assert _evaluate(str(env["CI_SECURE_GH_IMPOSTOR"]), context) == impostor, (
        f"on a {name} the impostor check should be {impostor!r}")
    assert _evaluate(str(env["GH_TOKEN"]), context) == token, (
        f"on a {name} GH_TOKEN should evaluate to {token!r}; a fork pull "
        f"request runs code its author wrote and must not be handed a "
        f"credential")


def test_nothing_in_the_template_can_swallow_a_failing_gate(
        template: dict) -> None:
    """The verdict is only worth what the FAILURE path is worth.

    Everything else here pins which command runs and what the pass predicate
    says. That is all satisfiable by a scaffold whose gate can never fail:
    `continue-on-error` on the step or the job (GitHub reports a
    continue-on-error job to `needs` as a SUCCESS), a `|| true` on the run
    line, or a step-level `if:` that skips the gate on pull requests — the same
    skipped-check bypass the file's own header warns about, one level down.
    Each of those ships a green required check over an unscanned repository.
    """
    scan = template["jobs"]["scan"]
    assert "continue-on-error" not in scan, (
        "a continue-on-error scan job reports SUCCESS to `needs`, so the "
        "verdict greens over a gate that failed")
    assert "if" not in scan, (
        "a condition on the scan job makes it skippable, and the verdict reads "
        "a skipped dependency as something other than a failure")

    for step in scan["steps"]:
        assert "continue-on-error" not in step, (
            f"continue-on-error on {step.get('name', step.get('uses'))} lets "
            "the step fail without failing the job")
        assert "if" not in step, (
            f"a condition on {step.get('name', step.get('uses'))} lets the "
            "gate be skipped while the job still reports success")

    for step in scan["steps"]:
        run = step.get("run", "")
        if "gate.py" in run or "vendor.py" in run:
            for swallow in ("||", "&&", ";", "| true", "set +e"):
                assert swallow not in run, (
                    f"`{swallow}` in {run!r} can turn a non-zero exit into a "
                    "passing step")

    # The verdict job's own STEPS, not just the job. This is the job whose name
    # branch protection requires, and the sweep above stopped at the job key:
    # `continue-on-error: true` on the single step that computes the verdict
    # let it fail without failing the job, so `ci-secure` reported green over a
    # failed scan - the bypass this test is named for, one level further down
    # than it was looking, in the one job that matters most.
    verdict = template["jobs"]["verdict"]
    assert "continue-on-error" not in verdict
    for step in verdict["steps"]:
        assert "continue-on-error" not in step, (
            f"continue-on-error on {step.get('name')} lets the REQUIRED check's "
            "own step fail while the check still reports success")
        assert "if" not in step, (
            f"a condition on {step.get('name')} lets the required check green "
            "by skipping the step that decides the verdict")
        for swallow in ("|| true", "|| exit 0", "set +e"):
            assert swallow not in step.get("run", ""), (
                f"`{swallow}` in the verdict step turns its exit 1 into a pass")

    # `always()` is what stops a cancelled or skipped scan reporting green, so
    # the verdict job's condition is load-bearing rather than incidental.
    assert str(verdict.get("if")).strip() == "always()", (
        "the verdict job must run unconditionally: `!cancelled()` reports it "
        "as skipped on a cancelled run, which a required-check rule reads as "
        f"green - found {verdict.get('if')!r}")


@pytest.mark.parametrize(
    ("scan_result", "expected_zero"),
    [("success", True), ("failure", False), ("cancelled", False),
     ("skipped", False), ("", False)],
)
def test_the_verdict_script_is_run_not_read(
        template: dict, scan_result: str, expected_zero: bool) -> None:
    """Grepping the script for `exit 1` is not the same as it exiting 1.

    An `exit 0` inserted anywhere above the check satisfies every substring
    assertion — `needs`, `always()`, the predicate, no `${{`, an `exit 1`
    present somewhere — while the required check greens over a scan that
    failed. The script is plain bash with one input, so the honest test runs
    it, on every result GitHub can report.
    """
    script = template["jobs"]["verdict"]["steps"][-1]["run"]
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "SCAN": scan_result})
    if expected_zero:
        assert proc.returncode == 0, (
            f"the verdict reds on scan={scan_result!r}:\n{proc.stdout}{proc.stderr}")
    else:
        assert proc.returncode != 0, (
            f"the verdict PASSES on scan={scan_result!r} - a scan that did not "
            f"run, was cancelled or failed is not a pass:\n{proc.stdout}")


def test_the_template_triggers_carry_no_narrowing_filter(template: dict) -> None:
    """`pull_request:` with a filter is a gate that mostly does not run.

    `types: [labeled]` or `paths: [...]` under `pull_request` leaves the check
    reporting green on every pull request that does not match — which a
    required-check rule cannot tell apart from a scan that passed.
    """
    triggers = _triggers(template)
    assert triggers["pull_request"] is None, (
        f"the pull_request trigger carries a filter ({triggers['pull_request']}); "
        "every pull request has to be scanned or the required check is a "
        "rule that can be satisfied by not matching it")


def test_the_installer_installs_exactly_the_files_under_test(
        tmp_path: Path) -> None:
    """The suite inspects `scaffold/`; the installer reads two constants.

    Nothing otherwise binds the two together, so repointing `GATE_SOURCE` at a
    five-line gate that exits 0 ships that gate to every adopter with the whole
    suite green — the reviewed file and the installed file are simply different
    files. This is what ties them.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0

    assert (repo / "ci-secure" / "scripts" / "gate.py").read_bytes() == \
        (_SCAFFOLD / "gate.py").read_bytes(), (
            "the gate that gets installed is not the gate the identity test "
            "holds to the one we run on ourselves")
    assert (repo / ".github" / "workflows" / "ci-secure.yml").read_bytes() == \
        _TEMPLATE.read_bytes(), (
            "the workflow that gets installed is not the template every other "
            "test in this file inspects")


def test_the_template_verifies_its_own_vendored_copy(template: dict) -> None:
    """Drift in the vendored gate must be loud, not discovered at the next refresh.

    Asserted as "this line RUNS the checker", not "this line mentions it". A
    substring test is satisfied by `echo python3 ... --verify`, and the drift
    check is the only thing between an edited `config.py` — the rule that says
    which outcomes block — and a green required check: with the step inert, a
    tampered rule makes the gate exit 0 while its own summary still prints
    **FAIL** for the fact that failed.
    """
    steps = template["jobs"]["scan"]["steps"]
    verify = [s for s in steps if "vendor.py --verify" in s.get("run", "")]
    assert len(verify) == 1, (
        "nothing checks the vendored files against VENDORED.json, so a local "
        "edit to the gate would weaken it invisibly")
    run = verify[0]["run"].strip()
    assert run.startswith("python3 ci-secure/scripts/vendor.py --verify"), (
        f"the drift check has to be the command, not a word inside one: {run!r}")


def test_the_scan_job_runs_exactly_the_steps_it_declares(template: dict) -> None:
    """No step may be inserted between the drift check and the gate.

    Everything else here pins what individual steps say. None of it constrains
    the LIST, and the drift check has already passed by the time a later step
    runs - so a step slipped in between them can edit the vendored tree with
    the manifest no longer looking, and the gate then runs the edited rule.
    Pinning the sequence is what makes the drift check mean "the tree the gate
    is about to read" rather than "the tree at some earlier moment".
    """
    steps = template["jobs"]["scan"]["steps"]
    shape = [s.get("uses", "").split("@")[0] or s.get("run", "").split()[0]
             for s in steps]
    assert shape == ["actions/checkout", "actions/setup-python", "pip",
                     "python3", "python3"], (
        f"the scan job's step list changed: {shape}. Every step here runs "
        "before a verdict that judges the tree, so an addition is a decision, "
        "not a detail - add it to this list deliberately")

    assert "vendor.py --verify" in steps[-2].get("run", ""), (
        "the drift check must be the step IMMEDIATELY before the gate")
    assert steps[-1]["run"].strip().startswith(
        "python3 ci-secure/scripts/gate.py"), "the gate must be the last step"


def test_the_template_keeps_the_pins_and_hardening_it_argues_for(
        template: dict) -> None:
    """Three values the file's own comments call load-bearing, and nothing held.

    Each of these could be changed to its weaker form with the whole suite
    green, while the header went on explaining why it mattered: the weekly run
    is the only one that demands a COMPLETE network check ("the run that must
    not be mutable by exhausting API quota"), PyYAML sits "inside the verdict's
    trust path" because the engine parses workflows with it, and the checkout
    has no reason to carry a credential.
    """
    scan = template["jobs"]["scan"]
    gate_env = scan["steps"][-1]["env"]

    strict = gate_env["CI_SECURE_GH_STRICT"]
    assert "github.event_name == 'schedule'" in strict and "'1'" in strict, (
        "the weekly run must demand a complete network check; a constant here "
        f"silences it on every trigger: {strict!r}")

    pip = next(s["run"] for s in scan["steps"] if "pip install" in s.get("run", ""))
    assert "pyyaml==" in pip, (
        f"PyYAML is what the engine parses workflows with - pin it: {pip!r}")

    checkout = next(s for s in scan["steps"]
                    if "actions/checkout" in s.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False, (
        "the scan reads the tree and has no reason to hold a credential - and "
        "the scan itself will not tell you if this is flipped, because the "
        "credentials fact only looks at untrusted triggers")


# --------------------------------------------------------------------------
# 3. Cleanliness — the template passes the gate it installs
# --------------------------------------------------------------------------

# Facts a repository that has never been scanned is expected to red or be
# unable to measure on its FIRST run. They are the reason `--advisory` exists;
# they are not licence for the template itself to be dirty.
_FIRST_RUN_FACTS = {
    "sec.codeowners.workflows",          # no CODEOWNERS entry yet
    "sec.required-checks.skippable",     # admin-scoped API, unmeasurable in CI
    "sec.fork-approval.effective",       # admin-scoped API, unmeasurable in CI
}

# `sec.permissions.workflow-declares` is deliberately NOT excused here. The
# fixture repository's only workflow is the one we ship, so that fact is a
# statement about OUR file and nothing else — excusing it as "the adopter's
# other workflows" would let the template drop its `permissions:` block with
# this test still green.


def test_the_installed_template_passes_the_gate_it_installs(tmp_path: Path) -> None:
    """Scan a repository whose only workflow is the one we ship.

    Whatever the template does becomes every adopter's `.github/workflows`, and
    the gate scans exactly that. If the template trips a fact, the adopter's
    first blocking run reds on a file they did not write — and they would be
    right to conclude the tool is broken.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    shutil.copy2(_TEMPLATE, repo / ".github" / "workflows" / "ci-secure.yml")

    proc = subprocess.run(
        [sys.executable, str(_SKILL / "scripts" / "scan.py"), "--root", str(repo),
         "--gh-impostor", "off"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]

    scan = json.loads(proc.stdout)
    failed = [f["fact_id"] for f in scan["security_score"]["facts"]
              if f["outcome"] == "fail"]
    unexpected = sorted(set(failed) - _FIRST_RUN_FACTS)
    assert not unexpected, (
        f"the workflow we hand to adopters fails {unexpected} against the gate "
        "it installs")

    assert not scan["scan_incomplete"], scan["scan_incomplete"]
    assert not scan["dropped_matches"], scan["dropped_matches"]

    findings = [f"{f['pattern']}: {f['title']}" for f in scan["findings"]]
    assert not findings, (
        f"the template trips pattern findings an adopter would have to triage "
        f"on day one: {findings}")


# --------------------------------------------------------------------------
# The install itself
# --------------------------------------------------------------------------

def test_vendoring_produces_a_working_gate_and_a_manifest_that_verifies(
        tmp_path: Path) -> None:
    """End to end: vendor into an empty repo, then run the vendored gate on it.

    This is the adopter's first five minutes, and it is where a layout mistake
    shows up — the vendored gate resolving `config.py` from a path that only
    exists in OUR tree would red with "rule not found" on every install.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)

    install = subprocess.run(
        [sys.executable, str(_VENDOR), "--into", str(repo)],
        capture_output=True, text=True)
    assert install.returncode == 0, install.stderr

    vendored = repo / "ci-secure"
    assert (vendored / "LICENSE").is_file(), (
        "copying the code without its licence is a licence violation")
    assert (vendored / "scripts" / "config.py").is_file(), (
        "config.py must land beside the engine, where the gate looks for it")
    assert (repo / ".github" / "workflows" / "ci-secure.yml").is_file()

    manifest = json.loads((vendored / "VENDORED.json").read_text(encoding="utf-8"))
    assert manifest["skill_version"]
    assert manifest["source_commit"]
    assert set(manifest["files"]) >= {"scripts/scan.py", "scripts/gate.py",
                                      "scripts/config.py", "LICENSE"}

    check = subprocess.run(
        [sys.executable, str(vendored / "scripts" / "vendor.py"),
         "--verify", str(vendored)],
        capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr

    # RUN the vendored gate, which is what this test's name has always claimed
    # and what nothing in the suite actually did. Placement and hashes are not
    # the property that matters: the adopter's first run either reaches a
    # verdict or it does not. Checking only the file list let the vendored set
    # be shrunk - dropping `scripts/gh_utils.py`, which `config_facts.py` loads
    # by location from beside the engine, shipped an install whose engine died
    # on import for every adopter with the whole suite green.
    gate = subprocess.run(
        [sys.executable, str(vendored / "scripts" / "gate.py")],
        cwd=repo, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "GITHUB_WORKSPACE": str(repo),
             "CI_SECURE_ENGINE": "ci-secure/scripts/scan.py",
             "CI_SECURE_GH_IMPOSTOR": "off",
             "PYTHONDONTWRITEBYTECODE": "1"})

    assert "engine failed to run" not in gate.stdout, (
        "the vendored engine could not start - the vendored file set is not "
        "closed under the engine's imports:\n" + gate.stdout + gate.stderr)
    assert "rule (config.py)" not in gate.stdout, (
        "the vendored gate looked for its rule at a path that only exists in "
        "OUR tree:\n" + gate.stdout)
    assert "no readable verdict" not in gate.stdout, gate.stdout
    assert gate.returncode == 1, (
        "a repository that has never been scanned reds on its first blocking "
        "run - a 0 here means the gate reached no verdict at all:\n"
        + gate.stdout + gate.stderr)

    # The same scan, ramped: this is the mode the shipped workflow installs.
    ramped = subprocess.run(
        [sys.executable, str(vendored / "scripts" / "gate.py"), "--advisory"],
        cwd=repo, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "GITHUB_WORKSPACE": str(repo),
             "CI_SECURE_ENGINE": "ci-secure/scripts/scan.py",
             "CI_SECURE_GH_IMPOSTOR": "off",
             "PYTHONDONTWRITEBYTECODE": "1"})
    assert ramped.returncode == 0, (
        "the flag the template ships did not clear the first-run facts:\n"
        + ramped.stdout + ramped.stderr)

    assert not list(vendored.rglob("__pycache__")), (
        "the gate wrote bytecode into the vendored tree, which its own drift "
        "check reds on")


def _vendor(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_VENDOR), "--into", str(repo)],
                          capture_output=True, text=True)


def _verify(vendored: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(vendored / "scripts" / "vendor.py"),
         "--verify", str(vendored)], capture_output=True, text=True)


def test_refresh_leaves_the_adopters_own_workflow_alone(tmp_path: Path) -> None:
    """Refreshing the vendored CODE must not rewrite the workflow they tuned.

    The workflow is the one file the adopter is invited to change: their runner,
    their triggers, and — the whole point of the ramp — deleting `--advisory`
    once the first-run findings are burned down. Copying the template back over
    it on refresh silently returns a blocking gate to advisory, which is the
    worst outcome this feature can produce, and the manifest cannot catch it
    because the workflow is deliberately not checksummed.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0

    installed = repo / ".github" / "workflows" / "ci-secure.yml"
    tuned = installed.read_text(encoding="utf-8").replace(
        "gate.py --advisory", "gate.py")
    assert "gate.py --advisory" not in tuned
    installed.write_text(tuned, encoding="utf-8")

    refresh = _vendor(repo)
    assert refresh.returncode == 0, refresh.stderr
    assert installed.read_text(encoding="utf-8") == tuned, (
        "refresh overwrote the adopter's workflow - a gate they had taken "
        "blocking is advisory again and nothing told them")
    assert "workflow" in refresh.stdout.lower(), (
        "refresh left the workflow alone but did not say so, and an unsaid "
        "skip is indistinguishable from an update that happened")


@pytest.mark.parametrize("what_they_did", ["renamed", "deleted"])
def test_refresh_never_re_adds_the_workflow_template(
        tmp_path: Path, what_they_did: str) -> None:
    """"Left exactly as it is" has to hold for a workflow that MOVED, too.

    The guarantee was enforced by "do not overwrite a file that is there",
    which is a different promise. An adopter who renamed the file to fit their
    conventions — or deleted it while backing the gate out — got the advisory
    template silently re-added on the next refresh. That is the outcome the
    design calls the worst thing this feature can do, reached by adding rather
    than overwriting: two workflows both publishing the required check name
    `ci-secure`, one of them advisory, and nothing said so.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0

    workflows = repo / ".github" / "workflows"
    installed = workflows / "ci-secure.yml"
    blocking = installed.read_text(encoding="utf-8").replace(
        "gate.py --advisory", "gate.py")
    if what_they_did == "renamed":
        (workflows / "security.yml").write_text(blocking, encoding="utf-8")
    installed.unlink()

    refresh = _vendor(repo)
    assert refresh.returncode == 0, refresh.stderr
    assert not installed.exists(), (
        "a refresh re-added the advisory template at the path the adopter had "
        "moved the workflow away from - two jobs now carry the required check "
        "name and one of them cannot block")
    if what_they_did == "renamed":
        assert "gate.py --advisory" not in (workflows / "security.yml").read_text(
            encoding="utf-8")


def test_installing_over_someone_elses_ci_secure_directory_refuses(
        tmp_path: Path) -> None:
    """A destination that is already in use is refused, not merged into.

    `ci-secure/` is a plausible name for a directory an adopter already keeps —
    policies, notes — and the install used to copy in beside whatever was
    there and exit 0. The manifest then lists only our files, so the very first
    CI run reds on `--verify` with "not in the manifest" for the adopter's own
    files, before the gate runs at all. Neither documented remedy reaches it:
    `--advisory` downgrades failed FACTS, and this is not a fact. The install
    reported success, so nobody is looking at the install.
    """
    repo = tmp_path / "acme-app"
    (repo / "ci-secure").mkdir(parents=True)
    theirs = repo / "ci-secure" / "NOTES.md"
    theirs.write_text("our security policies\n", encoding="utf-8")

    result = _vendor(repo)

    assert result.returncode != 0, (
        "installed into a directory already holding the adopter's files, "
        "which reds their CI on every run from the first one")
    assert "NOTES.md" in result.stdout + result.stderr, (
        "the refusal did not name the file in the way, so the adopter cannot "
        "act on it")
    assert theirs.read_text(encoding="utf-8") == "our security policies\n"
    assert not (repo / "ci-secure" / "VENDORED.json").exists()
    assert not (repo / ".github" / "workflows" / "ci-secure.yml").exists()


def test_installing_into_a_subdirectory_of_a_repo_refuses(
        tmp_path: Path) -> None:
    """`--into` must be the repository root, and the tool can tell.

    A workflow written to `services/api/.github/workflows/` is a file GitHub
    never reads. The install printed the same success it prints for a correct
    one, so the adopter is told they have a gate and every pull request merges
    unscanned — the one silent failure the documentation itself calls out, left
    to the caller to avoid when the tool has the path in hand.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)

    result = _vendor(nested)

    assert result.returncode != 0, (
        "vendored into a subdirectory of a repository and reported success - "
        "the workflow written there is one GitHub never runs")
    assert not (nested / "ci-secure").exists()
    assert not (nested / ".github").exists()


def test_a_symlinked_destination_inside_the_repo_is_refused(
        tmp_path: Path) -> None:
    """Containment is not enough: a symlink can aim at a file in the same repo.

    `ci-secure/LICENSE -> ../.github/workflows/release.yml` stays inside the
    repository, so a check that only asks "does this leave the repo?" passes
    it, and `copy2` then writes the licence text over the adopter's release
    workflow. It is reached by an ordinary contributor pull request planting a
    symlink under a directory reviewers read as "vendored tool, do not touch",
    followed by the documented refresh — and the refresh reports success.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0

    bystander = repo / ".github" / "workflows" / "release.yml"
    bystander.write_text("name: release\n", encoding="utf-8")
    licence = repo / "ci-secure" / "LICENSE"
    licence.unlink()
    licence.symlink_to(Path("..") / ".github" / "workflows" / "release.yml")

    refresh = _vendor(repo)

    assert refresh.returncode != 0, (
        "a refresh wrote through a symlink under the vendored tree")
    assert bystander.read_text(encoding="utf-8") == "name: release\n", (
        "a refresh destroyed the adopter's release workflow, and said it had "
        "succeeded")


def test_a_destination_that_is_a_plain_file_is_refused_in_words(
        tmp_path: Path) -> None:
    """`ci-secure` already existing as a FILE gets a sentence, not a traceback."""
    repo = tmp_path / "acme-app"
    repo.mkdir()
    (repo / "ci-secure").write_text("not a directory\n", encoding="utf-8")

    result = _vendor(repo)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr, (
        "the refusal is a stack trace, in a tool whose whole argument is that "
        f"a failure states its cause: {result.stderr}")
    assert "ci-secure" in result.stdout + result.stderr


@pytest.mark.parametrize("plant", ["listed_in_manifest", "symlinked_cache"])
def test_planted_bytecode_is_drift_even_when_the_manifest_blesses_it(
        tmp_path: Path, plant: str) -> None:
    """The manifest is repository content, so it cannot exempt a `.pyc`.

    The gate loads `config.py` — which defines what blocks — with
    `exec_module`, and Python runs a `.pyc` beside it without ever comparing
    it to its source. So one planted in the vendored tree empties the set of
    outcomes that block while every source file still hashes correctly: a
    green required check over a repository whose own job summary says FAIL.

    Two routes had to be closed. Adding the `.pyc` to `VENDORED.json` — the
    attacker controls that file too — made it "recorded", and the recorded
    short-circuit ran before the bytecode test. Hiding it behind a symlinked
    `__pycache__` made it invisible to a walk that neither descends symlinks
    nor reports them. The delete loop already refuses to let the manifest
    steer it; this sweep was still trusting it.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    if plant == "listed_in_manifest":
        cache = vendored / "scripts" / "__pycache__"
        cache.mkdir()
        pyc = cache / "config.cpython-312.pyc"
        pyc.write_bytes(b"planted bytecode")
        manifest_path = vendored / "VENDORED.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["scripts/__pycache__/config.cpython-312.pyc"] = (
            hashlib.sha256(pyc.read_bytes()).hexdigest())
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    else:
        elsewhere = tmp_path / "build-cache"
        elsewhere.mkdir()
        (elsewhere / "config.cpython-312.pyc").write_bytes(b"planted bytecode")
        (vendored / "scripts" / "__pycache__").symlink_to(elsewhere)

    check = _verify(vendored)

    assert check.returncode == 1, (
        f"planted bytecode ({plant}) passed --verify, so the adopter's "
        "required check goes green while the gate executes it")
    assert "bytecode" in check.stdout, (
        f"--verify reds but never names the bytecode: {check.stdout}")


@pytest.mark.parametrize("manifest_text", [
    '{"files": []}', '{"files": "scripts/scan.py"}', '{"files": null}',
    "[]", "null", '{"files": {"scripts/scan.py": null}}',
])
def test_a_type_confused_manifest_reds_with_a_reason(
        tmp_path: Path, manifest_text: str) -> None:
    """`VENDORED.json` is repository content, so its SHAPE is untrusted too.

    Every shape already failed closed, which is the part that matters, but
    four of them failed with a bare traceback: a security check going red in
    someone's pull request with no stated cause, in a tool whose whole
    argument is that a red says what is wrong and what to do about it.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"
    (vendored / "VENDORED.json").write_text(manifest_text, encoding="utf-8")

    check = _verify(vendored)

    assert check.returncode == 1
    assert "Traceback" not in check.stderr, (
        f"a malformed manifest reds with a stack trace: {check.stderr}")
    assert "::error::" in check.stdout, (
        f"a malformed manifest reds with no annotation: {check.stdout!r}")


def test_committed_bytecode_is_drift_and_says_what_to_do_about_it(
        tmp_path: Path) -> None:
    """A `.pyc` beside a verified source file is not noise — it OVERRIDES it.

    Python does not always re-derive bytecode from source: a `.pyc` written
    with an unchecked hash is loaded as-is, which is exactly how the gate loads
    `config.py`. So a planted one can empty the set of outcomes that block
    while every source file still hashes correctly and the manifest is
    untouched — a green required check over a repository with failing facts.
    Exempting bytecode to quieten the alarm would remove the only thing that
    catches it. It is reported, with the fix, and the workflow stops the gate
    writing any in the first place.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    cache = vendored / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "config.cpython-312.pyc").write_bytes(b"\x00compiled")

    check = _verify(vendored)
    assert check.returncode == 1, (
        "bytecode that can override a hash-verified source file was accepted "
        "as a clean copy:\n" + check.stdout)
    assert "__pycache__" in check.stdout, (
        "the report does not name the bytecode, so the reader cannot act on it")


def test_the_template_stops_the_gate_writing_bytecode(template: dict) -> None:
    """The noise the exemption would have papered over is removable at source.

    Committed bytecode is drift and reds. Bytecode the gate wrote a second
    earlier is the same file with an innocent cause, so the workflow tells
    Python not to produce it — which is what makes reporting it affordable.

    Asserted on the JOB, not on the gate step. Scoped to that one step the
    guard stopped one step short of the drift check, which runs python first:
    the step whose whole purpose is rejecting bytecode was the step free to
    write it. Safe today only because `--verify` happens to import nothing,
    which is a property of one function's import graph holding up a stated
    invariant about the whole job.
    """
    scan = template["jobs"]["scan"]
    assert scan.get("env", {}).get("PYTHONDONTWRITEBYTECODE") == "1", (
        "set this on the scan JOB: any python step here may otherwise leave "
        "__pycache__ in the vendored tree, which the next --verify correctly "
        "reds - stop it being written at all")

    for step in scan["steps"]:
        if "python" not in step.get("run", ""):
            continue
        effective = dict(scan.get("env", {}), **step.get("env", {}))
        assert effective.get("PYTHONDONTWRITEBYTECODE") == "1", (
            f"{step.get('name', step.get('run'))!r} runs python without the "
            "no-bytecode guard in effect")


def test_refresh_removes_a_file_a_later_version_stopped_vendoring(
        tmp_path: Path) -> None:
    """Otherwise the refresh that drops a file reds the adopter's next run.

    `--verify` flags anything under the vendored directory that the manifest
    does not list. A file removed from the vendored set in a later version is
    exactly that, so a correct refresh would hand the adopter a red required
    check for a file they never touched.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    # Stand in for a file an earlier version vendored and this one does not.
    stale = vendored / "scripts" / "retired_helper.py"
    stale.write_text("# vendored by an older ci-secure\n", encoding="utf-8")
    manifest_path = vendored / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["scripts/retired_helper.py"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    assert _vendor(repo).returncode == 0
    assert not stale.exists(), (
        "refresh left behind a file it no longer vendors; the adopter's next "
        "run reds with 'not in the manifest'")
    check = _verify(vendored)
    assert check.returncode == 0, check.stdout


def test_pruning_cannot_reach_outside_the_vendored_directory(
        tmp_path: Path) -> None:
    """The manifest is repository content, so it is untrusted input to a delete.

    It arrives with a branch, a pull request checkout or a fork clone, and the
    refresh runs on someone's machine with their privileges. A `files` entry of
    `../.github/workflows/release.yml` would otherwise be repo-controlled
    arbitrary file deletion, aimed at exactly the directory this tool exists to
    protect — and a merely CORRUPT manifest would do the same damage.
    """
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    assert _vendor(repo).returncode == 0

    bystander = repo / ".github" / "workflows" / "release.yml"
    bystander.write_text("name: release\n", encoding="utf-8")
    outsider = tmp_path / "outside.txt"
    outsider.write_text("not ours\n", encoding="utf-8")

    manifest_path = repo / "ci-secure" / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../.github/workflows/release.yml"] = "0" * 64
    manifest["files"][str(outsider)] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    assert _vendor(repo).returncode == 0
    assert bystander.is_file(), (
        "a refresh deleted a workflow because the manifest named it - the "
        "manifest is repository content and must never steer a delete")
    assert outsider.is_file(), (
        "a refresh deleted a file outside the repository entirely")


@pytest.mark.parametrize("link_at", ["ci-secure", "ci-secure/scripts",
                                     "ci-secure/references",
                                     "ci-secure/scripts/gate.py", ".github",
                                     ".github/workflows",
                                     ".github/workflows/ci-secure.yml"])
def test_an_install_never_writes_outside_the_repository(
        tmp_path: Path, link_at: str) -> None:
    """Every destination is inside the repository, or nothing is written at all.

    The install is aimed at a directory the adopter controls, and a symlink is
    ordinary repository content — it survives a clone, a pull request checkout
    and a fork. If one sitting at `ci-secure/`, at any directory beneath it, or
    at `.github/` is followed, the tool writes the engine, the gate and a live
    workflow somewhere the adopter never looked, reports success, and leaves the
    repository with no gate at all while its own documentation states exactly
    what it wrote and where.
    """
    repo = tmp_path / "acme-app"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = repo / link_at
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    result = _vendor(repo)

    assert result.returncode != 0, (
        f"a symlink at {link_at} was followed and the install reported success")
    assert not list(outside.rglob("*")), (
        f"the install wrote outside the repository via {link_at}: "
        f"{[p.name for p in outside.rglob('*')]}")
    # `is_file`, not `exists`: in the cases where the planted symlink IS one of
    # these two paths it already "exists", and that is the fixture, not a write.
    assert not (repo / "ci-secure" / "VENDORED.json").is_file(), (
        "a refused install left a vendored tree behind")
    assert not (repo / ".github" / "workflows" / "ci-secure.yml").is_file(), (
        "a refused install left a live ci-secure workflow behind")


def test_an_install_that_cannot_finish_writes_nothing(tmp_path: Path) -> None:
    """Validate first, then write. A half-install is a live workflow with no gate.

    The workflow used to be copied in before the licence was resolved and
    before the manifest was written, so a refusal partway through left the
    adopter with a workflow GitHub runs, a vendored tree, and no
    `VENDORED.json` — whose very first CI run reds on `--verify` with "cannot
    tell what this copy was supposed to be".
    """
    vendor = _load_vendor_module()
    repo = tmp_path / "acme-app"
    (repo / ".github" / "workflows").mkdir(parents=True)

    original = vendor._license_source
    vendor._license_source = lambda: None
    try:
        with pytest.raises(SystemExit):
            vendor.install(repo)
    finally:
        vendor._license_source = original

    assert not (repo / ".github" / "workflows" / "ci-secure.yml").exists(), (
        "a refused install left a live ci-secure workflow behind")
    assert not (repo / "ci-secure").exists(), (
        "a refused install left a vendored tree with no manifest behind")


def test_a_hand_edited_vendored_file_is_caught(tmp_path: Path) -> None:
    """The manifest earns its place only if drift actually fails something."""
    repo = tmp_path / "acme-app"
    repo.mkdir()
    subprocess.run([sys.executable, str(_VENDOR), "--into", str(repo)],
                   capture_output=True, text=True, check=True)
    vendored = repo / "ci-secure"

    gate = vendored / "scripts" / "gate.py"
    gate.write_text(gate.read_text(encoding="utf-8").replace(
        'BLOCKING_OUTCOMES', 'BLOCKING_OUTCOMES_DISABLED', 1), encoding="utf-8")

    check = subprocess.run(
        [sys.executable, str(vendored / "scripts" / "vendor.py"),
         "--verify", str(vendored)],
        capture_output=True, text=True)
    assert check.returncode == 1
    assert "scripts/gate.py: modified" in check.stdout
    assert "refresh" in check.stdout


# --------------------------------------------------------------------------
# The manifest defines what must be there, not merely what happens to be there
# --------------------------------------------------------------------------

def test_a_file_dropped_from_the_copy_AND_the_manifest_is_still_drift(
        tmp_path: Path) -> None:
    """Deleting a vendored file and its manifest entry must not verify clean.

    This is the hole the "anyone who can edit the gate can edit the manifest"
    caveat does NOT cover. Nothing here is edited-with-a-matching-hash: a file
    is removed from the manifest's DOMAIN, so every remaining hash still agrees
    and the drift check - sold as the thing that notices the vendored copy is
    no longer what was reviewed - reports a match.

    It is not cosmetic. `config.py` is the rule that says which outcomes block,
    and the gate resolves a fallback rule from `<gate>/../../skills/ci-secure/
    scripts/config.py`, which in the vendored layout is a path INSIDE the
    repository being audited. Delete the vendored `config.py` plus its manifest
    entry, add that path in the same pull request, and the gate executes a rule
    the pull request wrote - with a green drift check above it.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    (vendored / "scripts" / "config.py").unlink()
    manifest_path = vendored / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]["scripts/config.py"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    check = _verify(vendored)

    assert check.returncode == 1, (
        "a manifest that no longer lists config.py verified CLEAN - the copy "
        "can be shrunk out from under the check that exists to notice it:\n"
        + check.stdout)
    assert "scripts/config.py" in check.stdout, (
        "the red has to name the file that went missing from the manifest")


def test_a_vendored_file_deleted_while_the_manifest_still_lists_it_is_drift(
        tmp_path: Path) -> None:
    """The other half: listed but absent. `--verify` must not read that as clean."""
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    (vendored / "scripts" / "config.py").unlink()

    check = _verify(vendored)
    assert check.returncode == 1, check.stdout
    assert "scripts/config.py: missing" in check.stdout


def test_verify_cannot_be_made_to_forge_or_silence_its_own_annotations(
        tmp_path: Path) -> None:
    """`VENDORED.json` is repository content, and it is printed into a log sink.

    The gate escapes everything it reads out of the repository for exactly this
    reason, and says so at length. `--verify` reads from the same trust class -
    a pull request checkout, including a fork's - and runs in the step ABOVE
    the gate. Unescaped, a newline in the recorded version forges a `::notice::`
    on the check run, and a newline in a manifest key emits `::stop-commands::`,
    which swallows every drift reason printed after it: the step reds with no
    stated cause, which is the unexplained red this codebase argues against.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"
    manifest_path = vendored / "VENDORED.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skill_version"] = "0.2.0\n::notice::all clear"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    forged = _verify(vendored)
    assert "\n::notice::" not in forged.stdout, (
        "repository content emitted a workflow command of its own:\n"
        + forged.stdout)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["\n::stop-commands::deadbeef\nzzz.py"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    silenced = _verify(vendored)
    assert silenced.returncode == 1
    assert "\n::stop-commands::" not in silenced.stdout, (
        "a manifest key stopped the workflow commands that report the drift:\n"
        + silenced.stdout)


def test_the_installer_ignores_an_ambient_git_environment(tmp_path: Path) -> None:
    """`GIT_DIR` overrides `-C`, so the guards must not ask the ambient git.

    Every `git` call here is asking about a SPECIFIC directory - is this the
    root of its repository, what commit is this skill at - and `-C` is not
    enough to make that true: an exported `GIT_DIR` (a hook, a `rebase -x`, a
    worktree-driven session) silently redirects the answer to an unrelated
    repository. The subdirectory guard then refuses a perfectly good install
    and tells the adopter to vendor a live workflow into that other repository
    instead, which is the "wrote it somewhere you never looked" outcome the
    sibling guard exists to prevent, reached THROUGH a guard.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True,
                   capture_output=True)

    env = dict(os.environ, GIT_DIR=str(elsewhere / ".git"),
               GIT_WORK_TREE=str(elsewhere))
    install = subprocess.run(
        [sys.executable, str(_VENDOR), "--into", str(repo)],
        capture_output=True, text=True, env=env)

    assert install.returncode == 0, (
        "an unrelated GIT_DIR in the environment made the install refuse:\n"
        + install.stdout + install.stderr)
    assert (repo / "ci-secure" / "VENDORED.json").is_file()
    assert not (elsewhere / "ci-secure").exists(), (
        "the install must never touch the repository GIT_DIR pointed at")


def test_a_file_planted_under_the_vendored_tree_is_drift(tmp_path: Path) -> None:
    """The manifest says what belongs there, so an ADDITION is drift too.

    This arm is what two of the installer's refusals are built on - the
    occupied-directory refusal exists precisely because `--verify` reds on
    anything the manifest does not list. Without it a pull request can add a
    second `config.py`, or a shim beside the engine, and the drift check that
    exists to notice the copy is not what was reviewed says it matches.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    (vendored / "scripts" / "sitecustomize.py").write_text(
        "# not ours\n", encoding="utf-8")

    check = _verify(vendored)
    assert check.returncode == 1, (
        "a file added under the vendored tree verified clean:\n" + check.stdout)
    assert "scripts/sitecustomize.py" in check.stdout
    assert "not in the manifest" in check.stdout


def test_a_bare_pyc_beside_its_source_is_drift(tmp_path: Path) -> None:
    """Bytecode does not have to sit in `__pycache__` to be loaded first.

    The existing cases all plant under `__pycache__`, so the `.pyc` SUFFIX arm
    of the same test carried nothing: a manifest-listed
    `ci-secure/scripts/config.pyc` would have been covered by neither.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    (vendored / "scripts" / "config.pyc").write_bytes(b"\x00compiled")

    check = _verify(vendored)
    assert check.returncode == 1, check.stdout
    assert "config.pyc" in check.stdout
    assert "bytecode" in check.stdout, (
        "a bare .pyc has to be reported as bytecode that can override its "
        "source, not merely as an unexpected file")


def test_a_manifest_entry_that_escapes_through_a_symlink_is_not_deleted(
        tmp_path: Path) -> None:
    """The prune loop's two guards are separate, and each needs its own case.

    The `..`/absolute test and the resolve-containment test were only ever
    exercised together, so either could be deleted silently. This entry has no
    `..` and is not absolute - it escapes because a directory in the middle of
    it is a symlink - so only the second guard can stop it.
    """
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0
    vendored = repo / "ci-secure"

    outside = tmp_path / "precious"
    outside.mkdir()
    victim = outside / "release.yml"
    victim.write_text("name: theirs\n", encoding="utf-8")
    (vendored / "escape").symlink_to(outside, target_is_directory=True)

    manifest_path = vendored / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["escape/release.yml"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    refresh = _vendor(repo)

    assert victim.is_file(), (
        "a manifest entry deleted a file outside the vendored tree, reached "
        "through a symlinked directory rather than through `..`:\n"
        + refresh.stdout)
    assert victim.read_text(encoding="utf-8") == "name: theirs\n"


def test_the_recorded_source_commit_is_a_sha_or_says_why_it_is_not(
        tmp_path: Path) -> None:
    """"Which commit is this copy from?" is the manifest's stated reason to exist.

    Asserting only that the field is truthy accepts every `unknown (...)`
    marker, so provenance could silently become unavailable for every adopter
    with the suite green - and this repo's rule is that provenance is honest,
    including the `-dirty` suffix, or it is not recorded at all.
    """
    import re
    repo = tmp_path / "acme-app"
    repo.mkdir()
    assert _vendor(repo).returncode == 0

    manifest = json.loads(
        (repo / "ci-secure" / "VENDORED.json").read_text(encoding="utf-8"))
    recorded = manifest["source_commit"]

    assert re.fullmatch(r"[0-9a-f]{40}(-dirty| \(working tree state unknown\))?",
                        recorded), (
        "vendoring from this source checkout must record its real commit, "
        f"not a marker: {recorded!r}")
