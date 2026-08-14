"""The required `test` check must be produced on EVERY pull request.

GitHub counts a skipped required status check as passing. Before this guard
existed, the required check `test` was a job that skips itself on fork PRs
(fork code must not run on the self-hosted runners), and the fork twin ran
under a different check name in a separate workflow — so a fork PR's required
check was reported "skipped", which branch protection reads as green, and the
PR could merge with zero tests executed.

The fix this file pins: the suite runs as two mutually exclusive jobs
(self-hosted for our own branches, GitHub-hosted for forks), and an
always-running verdict job carries the single check name `test` — passing only
when the one suite job that was supposed to run actually ran and passed. The
protection rule requires `test`, so the verdict must exist on every event.

Each assertion here names the bypass it blocks. Deleting one weakens branch
protection as much as editing the workflow it guards.
"""
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO / ".github" / "workflows"
_CI = _WORKFLOWS / "ci.yml"


def _jobs():
    doc = yaml.safe_load(_CI.read_text())
    return doc["jobs"]


def _display_name(job_id: str, job: dict) -> str:
    return job.get("name", job_id)


def _verdict():
    jobs = _jobs()
    named_test = {jid: j for jid, j in jobs.items()
                  if _display_name(jid, j) == "test"}
    assert len(named_test) == 1, (
        "exactly one job in ci.yml must carry the required check name 'test'; "
        f"found {sorted(named_test)}")
    return next(iter(named_test.values()))


def test_verdict_job_always_runs():
    """A verdict that can be skipped is a verdict that can be bypassed:
    GitHub reads a skipped required check as a pass."""
    v = _verdict()
    assert str(v.get("if", "")).strip() == "always()", (
        "the 'test' verdict job must run unconditionally (if: always()); "
        "!cancelled() reports skipped on a cancelled run, which reads as green")


def test_verdict_needs_both_suite_jobs():
    """The verdict must see both suite jobs' results, or one path escapes it."""
    v = _verdict()
    needs = v.get("needs", [])
    assert sorted(needs) == ["test-fork", "test-self"], (
        f"the verdict must depend on both suite jobs, got {needs}")


def test_verdict_passes_only_when_the_right_job_ran():
    """Both-skipped, either-failed, and either-cancelled are not passes:
    a suite that did not run is not a suite that passed."""
    v = _verdict()
    run = v["steps"][-1]["run"]
    assert '"success" ] && [ "${FORK}" = "skipped"' in run.replace("'", '"'), (
        "verdict must accept self-hosted success only when the fork job "
        "was skipped")
    assert '"success" ] && [ "${SELF_HOSTED}" = "skipped"' in run.replace("'", '"'), (
        "verdict must accept fork success only when the self-hosted job "
        "was skipped")
    assert "exit 1" in run, "every other combination must fail the check"


def test_verdict_runs_hosted_and_checks_out_nothing():
    """The verdict runs on fork PRs too, so its shell travels with the head
    ref: it must not run on self-hosted infrastructure, and it must not
    check out code."""
    v = _verdict()
    assert v.get("runs-on") == "ubuntu-latest"
    assert not any("checkout" in str(s.get("uses", "")) for s in v["steps"])


def test_suite_jobs_split_by_fork_and_runner():
    """Fork code stays off the self-hosted runners; fork PRs still get the
    identical suite on GitHub-hosted ones."""
    jobs = _jobs()
    self_hosted = jobs["test-self"]
    fork = jobs["test-fork"]
    assert self_hosted["runs-on"] == "starsling-ubuntu-24.04"
    assert "!=" not in str(self_hosted.get("if", "")) and \
        "== github.repository" in str(self_hosted.get("if", "")), (
        "self-hosted suite must run only for pushes and same-repo PRs")
    assert fork["runs-on"] == "ubuntu-latest"
    assert "!= github.repository" in str(fork.get("if", "")), (
        "fork suite must run only for PRs whose head repo is not this repo")


def test_fork_twin_workflow_is_retired():
    """The fork twin now lives inside ci.yml so the verdict can `needs:` it —
    a separate workflow's jobs are invisible to `needs`, which is how the
    two check names diverged in the first place."""
    twins = [p.name for p in (_WORKFLOWS / "ci-fork.yml",
                              _WORKFLOWS / "ci-fork.yaml") if p.exists()]
    assert not twins, (
        f"the fork twin workflow must not exist; its job moved into ci.yml: {twins}")


def test_no_other_workflow_forges_the_required_check_name():
    """Required checks match by check NAME, not by workflow: a job named
    'test' in any other workflow could satisfy branch protection by simply
    exiting 0. Census every workflow, not just ci.yml — GitHub Actions reads
    both .yml and .yaml, so a `*.yml`-only census would pass vacuously while a
    forged `.yaml` sat next to it."""
    offenders = []
    for wf in sorted(_WORKFLOWS.glob("*.y*ml")):
        if wf.name == "ci.yml":
            continue
        doc = yaml.safe_load(wf.read_text())
        for jid, job in (doc.get("jobs") or {}).items():
            if _display_name(jid, job) == "test":
                offenders.append(f"{wf.name}:{jid}")
    assert not offenders, (
        f"job(s) outside ci.yml carry the required check name 'test': {offenders}")
