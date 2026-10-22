"""The skill-evals gate cannot go green over a session that did not happen.

This workflow exists to run ci-secure's and ci-speedup's behavioural evals against a live agent.
Its entire value is that a green `skill evals` check means five real sessions ran
and behaved. Every way that decays is invisible in the check list: a fork PR that
reports green having reached no API key, a verdict job that exits 0 on a skip, a
harness that could not start and is read as a behaviour change, a floating CLI
version that turns an upstream release into a "skill regression".

A text reading of the YAML cannot see an added permissive clause, so the verdict
script is EXECUTED here over every result it can be handed — the same technique
tests/test_ci_required_check_verdict.py uses on ci.yml. No network, no agent, no
token: this is the offline guard on the gate that costs money to run.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "skill-evals.yml"
_EVALS = _REPO / "skills" / "ci-secure" / "evals"


def _doc() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job(job_id: str) -> dict:
    jobs = _doc()["jobs"]
    assert job_id in jobs, f"job {job_id!r} is gone; found {sorted(jobs)}"
    return jobs[job_id]


def _squash(expr: object) -> str:
    return re.sub(r"\s+", " ", str(expr)).strip()


def test_the_verdict_never_reports_on_a_fork_pull_request():
    """The bug this pins: the verdict used to `exit 0` on a fork PR, so an
    external contribution got a green check named `skill evals` with no session
    run at all — a `::warning::` annotation is not a check status."""
    cond = _squash(_job("verdict").get("if", ""))
    assert "always()" in cond, "a verdict that can be skipped can be bypassed"
    assert "head.repo.full_name != github.repository" in cond, (
        "the verdict must exclude fork PRs; a green 'skill evals' on a fork is "
        "a pass over a session that could not happen")
    assert cond.lstrip().startswith("always() &&"), (
        "always() must gate the whole expression, or a cancelled evals job "
        "reports the verdict as skipped, which GitHub reads as green")


def test_the_fork_gap_is_carried_by_a_check_whose_name_says_so():
    """registry-scan.yml's pattern: report the gap rather than blocking an
    outside contributor, but name the check so green cannot be misread."""
    fork = _job("fork-did-not-run")
    name = str(fork.get("name", ""))
    assert "NOT RUN" in name, (
        f"the fork check's name is the message; got {name!r}")
    assert "fork" in name.lower()


def test_the_fork_and_verdict_conditions_are_exact_complements():
    """Exactly one of the two reports on any event. Overlap prints a green
    'skill evals' next to the NOT RUN notice; a gap prints neither."""
    fork_clause = ("github.event_name == 'pull_request' && "
                   "github.event.pull_request.head.repo.full_name != github.repository")
    assert _squash(_job("fork-did-not-run")["if"]) == fork_clause
    assert _squash(_job("verdict")["if"]) == f"always() && !({fork_clause})"


def test_only_the_verdict_job_carries_the_check_name_skill_evals():
    named = [jid for jid, j in _doc()["jobs"].items()
             if str(j.get("name", jid)) == "skill evals"]
    assert named == ["verdict"], (
        f"exactly one job may report the check name 'skill evals'; got {named}")


@pytest.mark.parametrize("result,speedup,expected", [
    ("success", "success", 0),
    ("failure", "success", 1),
    ("cancelled", "success", 1),
    ("skipped", "success", 1),
    ("", "success", 1),
    ("neutral", "success", 1),
    ("success", "failure", 1),
    ("success", "skipped", 1),
    ("success", "", 1),
])
def test_the_verdict_script_passes_only_on_a_real_success(result, speedup, expected):
    """Executed, not read. `skipped` reads as green to branch protection, so the
    verdict must turn every non-success of EITHER suite into a non-zero exit."""
    script = _job("verdict")["steps"][0]["run"]
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"RESULT": result, "SPEEDUP_RESULT": speedup, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode == expected, (
        f"RESULT={result!r} exited {proc.returncode}, expected {expected}: "
        f"{proc.stdout}{proc.stderr}")


def test_the_verdict_waits_on_both_suites_and_they_share_the_fork_gate():
    """A suite the verdict does not `needs:` can be skipped or red unseen."""
    assert set(_job("verdict")["needs"]) == {"evals", "ci-speedup-evals"}
    assert _squash(_job("ci-speedup-evals")["if"]) == _squash(_job("evals")["if"])


@pytest.mark.parametrize("job_id,skill", [("evals", "ci-secure"),
                                          ("ci-speedup-evals", "ci-speedup")])
def test_each_suite_runs_on_the_shared_runner(job_id, skill):
    run = _job(job_id)["steps"]
    step = next(s for s in run if s.get("name") == "Run the eval cases")
    assert f"maintainers/scripts/run_skill_evals.py --skill {skill}" in _squash(step["run"])


@pytest.mark.parametrize("job_id", ["evals", "ci-speedup-evals"])
def test_each_suite_keeps_sandbox_exit2_and_pinned_cli(job_id):
    steps = {s.get("name"): s for s in _job(job_id)["steps"]}
    assert steps["Run the eval cases"]["env"].get("IS_SANDBOX") == "1"
    assert "-eq 2" in steps["Run the eval cases"]["run"]
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+",
                     steps["Install Claude Code"]["run"])


def test_the_agent_step_declares_the_sandbox_the_cli_requires_under_root():
    """The first CI run failed every grader in 1.6 seconds: the runner is root,
    and `claude` refuses `--permission-mode bypassPermissions` under uid 0
    unless a deliberate sandbox is declared. Dropping this env var silently
    reverts the workflow to grading five sessions that never started."""
    step = next(s for s in _job("evals")["steps"]
                if s.get("name") == "Run the eval cases")
    assert step["env"].get("IS_SANDBOX") == "1"


def test_the_agent_step_preserves_the_could_not_run_exit_code():
    """Exit 2 is the harness saying "nothing was verified". Collapsing it into
    the same red as exit 1 tells the reader the agent's behaviour changed when
    the truth is that no agent ran."""
    step = next(s for s in _job("evals")["steps"]
                if s.get("name") == "Run the eval cases")
    assert "-eq 2" in step["run"], "the step must branch on the harness's exit 2"
    assert "DID NOT RUN" in step["run"]


def test_the_claude_cli_is_pinned():
    """A suite whose failure message asserts "the agent's behaviour changed"
    must hold the runtime still, or an upstream release reads as a regression."""
    step = next(s for s in _job("evals")["steps"]
                if s.get("name") == "Install Claude Code")
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", step["run"]), (
        f"pin the CLI version; got {step['run'].strip()!r}")


def test_the_job_budget_exceeds_the_harness_own_worst_case():
    """A slow-but-healthy suite killed by the job timeout is reported as a
    behaviour change. 1500 is the shared runner's default --timeout."""
    import json
    spec = json.loads((_EVALS / "evals.json").read_text(encoding="utf-8"))
    worst_seconds = sum(e.get("timeout", 1500) for e in spec["evals"] if e.get("checks"))
    assert worst_seconds > 0
    # Setup (checkout, pip, the CLI install, five scaffolds) also spends the job
    # budget, and a job killed at its timeout writes no results file to upload.
    setup_minutes = 10
    assert _job("evals")["timeout-minutes"] >= worst_seconds / 60 + setup_minutes, (
        f"{worst_seconds / 60:.0f} minutes of per-run budget plus {setup_minutes} of "
        f"setup under a {_job('evals')['timeout-minutes']}-minute job timeout")


def test_every_third_party_action_is_pinned_to_a_sha():
    for jid, job in _doc()["jobs"].items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), (
                    f"{jid}: {uses} is not pinned to a full commit SHA")


def test_the_workflow_asks_for_no_more_than_read_access():
    assert _doc()["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("rel", [
    "skills/ci-speedup/tests/verify_report.py",
    "skills/ci-speedup/tests/test_offline_pipeline_e2e.py",
    "skills/ci-speedup/tests/fixtures/gh_replay/"
    "repos_synthetic_repo_branches_main_protection_required_status_checks.json",
])
def test_the_path_filter_covers_the_ci_speedup_eval_inputs(rel):
    """The session runs verify_report.py, and the synthetic fixture is built by
    the e2e test's helpers from tests/fixtures/gh_replay/. A PR touching only
    those files must still run the sessions."""
    from fnmatch import fnmatch
    assert (_REPO / rel).is_file(), rel
    doc = _doc()
    paths = (doc.get("on") or doc[True])["pull_request"]["paths"]
    assert any(fnmatch(rel, p) for p in paths), f"{rel} matches no filter path"
