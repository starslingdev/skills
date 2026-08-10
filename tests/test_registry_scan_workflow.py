"""The registry-scan gate's own shape is enforced, so it cannot rot silently.

`.github/workflows/registry-scan.yml` runs a third-party security scanner over the
installable skill trees, so a rule violation fails our build instead of surfacing
as a public FAIL badge days after release. Almost every way that gate can decay is
invisible: a dropped `--ci` makes findings advisory, a widened ignore list hides a
real finding, a deleted `schedule` stops catching rule-catalog changes that need no
commit of ours, a drifted runner label quietly moves the job off the runners the
rest of this repo's CI dogfoods. In every one of those cases the check still runs
and still reports green.

These tests pin the properties the gate's value depends on. They are pure YAML and
text assertions — no network, no scanner, no token — so they run in the same
offline `pytest -v` as everything else. They cannot prove the scanner still
detects anything; that is `.github/scripts/registry_scan_redprove.py`, which runs
inside the workflow itself on every run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "registry-scan.yml"
_CI_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"
_REDPROVE = _REPO / ".github" / "scripts" / "registry_scan_redprove.py"

# The one accepted exclusion. Widening this set is a deliberate, reviewed change to
# both this list and the rationale comment beside it in the workflow.
_ACCEPTED_IGNORED_CODES = {"W011"}


@pytest.fixture(scope="module")
def workflow() -> dict:
    # PyYAML parses the `on:` key as the boolean True (YAML 1.1); read it back the
    # same way rather than fighting it.
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def triggers(workflow: dict) -> dict:
    return workflow[True] if True in workflow else workflow["on"]


def _scan_job(workflow: dict) -> dict:
    return workflow["jobs"]["scan"]


def _steps_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_workflow_exists_and_parses(workflow: dict):
    assert workflow["jobs"], "registry-scan.yml has no jobs"


def test_runs_on_pull_requests_and_pushes(triggers: dict):
    assert "pull_request" in triggers, "the gate must run on pull requests"
    assert "push" in triggers, "the gate must run on pushes to main"


def test_scheduled_run_exists(triggers: dict):
    """The scanner's rules change with no commit of ours — only a scheduled run
    notices a skill that went red on its own."""
    schedule = triggers.get("schedule")
    assert schedule, "the gate must run on a schedule; a rule revamp needs no commit of ours"
    assert any(entry.get("cron") for entry in schedule), "schedule entry has no cron expression"


def test_manually_dispatchable(triggers: dict):
    """A maintainer investigating a registry audit must be able to run it on demand."""
    assert "workflow_dispatch" in triggers


def test_runner_matches_the_repo_ci_runner(workflow: dict):
    """This repo dogfoods StarSling runners for its internal CI. If ci.yml's runner
    moves, this gate moves with it rather than drifting onto different infrastructure."""
    ci = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert _scan_job(workflow)["runs-on"] == ci["jobs"]["test"]["runs-on"]


def test_fork_prs_never_reach_the_self_hosted_runner(workflow: dict):
    """Fork code must not execute on self-hosted runners (the ci.yml contract)."""
    guard = _scan_job(workflow)["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in guard


def test_gate_uses_ci_flag(workflow: dict):
    """Without --ci a finding is printed and the build stays green."""
    assert "--ci" in _steps_text(_scan_job(workflow))


def test_ignore_list_is_exactly_the_accepted_exclusion(workflow: dict):
    declared = {
        code.strip()
        for code in workflow["env"]["IGNORED_ISSUE_CODES"].split(",")
        if code.strip()
    }
    assert declared == _ACCEPTED_IGNORED_CODES, (
        "the gate's ignore list changed. Every excluded code hides a real finding from "
        "the build's exit status, so each one needs a reason written next to it in the "
        "workflow and a matching update here."
    )


def test_exclusion_carries_its_reason(workflow_text: str):
    """An exclusion nobody can see is how a real finding gets hidden later."""
    for code in _ACCEPTED_IGNORED_CODES:
        assert code in workflow_text
    assert "untrusted third-party content" in workflow_text, (
        "the W011 exclusion must state, in the workflow, what the finding is and why "
        "it is accepted"
    )


def test_full_findings_are_printed_unfiltered(workflow: dict):
    """The scanner strips ignored findings from its printed report as well as from
    its exit status, so the gate runs an unfiltered pass first. If that pass ever
    grows an ignore list, accepted findings stop appearing in the log entirely."""
    steps = _scan_job(workflow)["steps"]
    visibility = [
        step for step in steps
        if "scan" in step.get("run", "") and "--ignore-issues-codes" not in step.get("run", "")
        and "snyk-agent-scan" in step.get("run", "")
    ]
    assert visibility, (
        "no unfiltered scan pass found — accepted findings would never be printed"
    )


def test_red_proof_runs_on_every_run(workflow: dict):
    """A check that cannot fail is not a check."""
    assert _REDPROVE.exists(), "the red-proof script is missing"
    assert "registry_scan_redprove.py" in _steps_text(_scan_job(workflow))


def test_missing_token_fails_rather_than_passing_quietly(workflow: dict):
    """A skipped security scan is the failure we are guarding against, wearing a
    green check."""
    text = _steps_text(_scan_job(workflow))
    assert "SNYK_TOKEN" in text
    assert "DID NOT RUN" in text, "a missing token must say so unmistakably in the log"
    assert "exit 1" in text, "on an internal run, a missing token must fail the build"


def test_fork_prs_report_the_coverage_gap(workflow: dict):
    """Fork PRs cannot reach the token. The check that remains must not read as
    'scanned, clean' — its own name has to carry the gap."""
    fork_job = workflow["jobs"]["fork-not-scanned"]
    assert "NOT RUN" in fork_job["name"]
    assert "DID NOT RUN" in _steps_text(fork_job)


def test_scanner_coverage_limits_are_documented(workflow_text: str):
    """Registries run more than one scanner; a green check here is not a clean bill
    of health from all of them."""
    for scanner in ("Gen Agent Trust Hub", "Socket"):
        assert scanner in workflow_text, (
            f"the workflow must say that {scanner} is not covered by this gate"
        )


def test_redprove_builds_a_violating_skill_without_committing_one(tmp_path):
    """The fixture is assembled at runtime: the literal that got a shipped skill
    flagged must not exist anywhere in this repository, this script included."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("registry_scan_redprove", _REDPROVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    scan_root = module.build_violating_skill(tmp_path)
    skill = scan_root / "redprove-fixture" / "SKILL.md"
    assert skill.exists(), "the fixture must be a <parent>/<name>/SKILL.md tree"

    body = skill.read_text(encoding="utf-8")
    assert "curl" in body and "| bash" in body, "fixture lost the violating shape"
    assert ".example.com/" in body, "fixture host must stay under the reserved example.com"

    # The assembled string must not appear verbatim in the script that assembles it.
    marker = "curl -sSL http" + "s://get.redprove-fixture.example.com"
    assert marker not in _REDPROVE.read_text(encoding="utf-8"), (
        "the red-proof script now contains the literal it is supposed to construct"
    )
