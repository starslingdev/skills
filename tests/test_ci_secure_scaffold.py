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
        assert "head.repo.full_name != github.repository" in expr, (
            f"{key} is not conditioned on the pull request being from a fork: "
            f"{expr}")
    assert "'auto'" not in str(env["CI_SECURE_GH_IMPOSTOR"]), (
        "`auto` makes a security check's presence depend on the runner image")


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
    "sec.permissions.workflow-declares",  # the adopter's OTHER workflows
    "sec.required-checks.skippable",     # admin-scoped API, unmeasurable in CI
    "sec.fork-approval.effective",       # admin-scoped API, unmeasurable in CI
}


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

    # The gate runs, finds its engine and its rule, and judges the tree.
    gate = subprocess.run(
        [sys.executable, str(vendored / "scripts" / "gate.py"), "--advisory"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin",
             "GITHUB_WORKSPACE": str(repo),
             "CI_SECURE_ENGINE": str(vendored / "scripts" / "scan.py"),
             "CI_SECURE_GH_IMPOSTOR": "off"})
    assert "rule (config.py) not found" not in gate.stdout, gate.stdout
    assert "engine not found" not in gate.stdout, gate.stdout
    assert gate.returncode == 0, (
        "a freshly vendored gate must not red on the workflow it installed, in "
        f"advisory mode:\n{gate.stdout}\n{gate.stderr}")


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
