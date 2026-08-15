"""What an outside repository gets when ci-secure installs itself as a gate.

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

import json
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

    verdict = template["jobs"]["verdict"]
    assert "continue-on-error" not in verdict


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
    """Drift in the vendored gate must be loud, not discovered at the next refresh."""
    runs = [s.get("run", "") for s in template["jobs"]["scan"]["steps"]]
    assert any("vendor.py --verify" in r for r in runs), (
        "nothing checks the vendored files against VENDORED.json, so a local "
        "edit to the gate would weaken it invisibly")


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
    """
    gate_step = next(s for s in template["jobs"]["scan"]["steps"]
                     if "gate.py" in s.get("run", ""))
    assert gate_step.get("env", {}).get("PYTHONDONTWRITEBYTECODE") == "1", (
        "the gate step may leave __pycache__ in the vendored tree, which the "
        "next --verify correctly reds - stop it being written at all")


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
