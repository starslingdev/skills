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
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO / ".github" / "workflows"
_CI = _WORKFLOWS / "ci.yml"


def _jobs():
    doc = yaml.safe_load(_CI.read_text())
    return doc["jobs"]


def _display_name(job_id: str, job: dict) -> str:
    return str(job.get("name", job_id))


def _forges_the_check_name(job_id: str, job: dict) -> bool:
    """Would this job report a check run named `test`?

    A literal `name: test` is the obvious case. An expression name —
    `name: ${{ 'test' }}` — renders as `test` in the check run that branch
    protection reads, but never equals the string "test" here. This census
    cannot evaluate GitHub expressions, so outside ci.yml an expression-valued
    job name IS the offence: it makes the required check name unknowable.
    """
    name = _display_name(job_id, job)
    return name == "test" or "${{" in name


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


_SHARDS = 2
_SUITE_COMMAND = f'pytest -v -n 4 --dist worksteal --shard "${{SHARD}}/{_SHARDS}"'

_RESULTS = ("success", "failure", "cancelled", "skipped", "")
# The only two states in which a suite that was supposed to run actually ran and
# passed. Every other pair is a non-pass — including both-skipped.
_ACCEPTED = {("success", "skipped"), ("skipped", "success")}


@pytest.mark.parametrize("self_hosted", _RESULTS)
@pytest.mark.parametrize("fork", _RESULTS)
def test_verdict_passes_only_when_the_right_job_ran(self_hosted, fork):
    """Both-skipped, either-failed, and either-cancelled are not passes:
    a suite that did not run is not a suite that passed.

    This RUNS the verdict's shell over every combination of job results rather
    than grepping it for substrings. A text check cannot see an ADDED permissive
    clause — inserting `if [ "${FORK}" = "cancelled" ]; then exit 0; fi` above
    the real clauses leaves every substring assertion satisfied while reopening
    exactly the bypass this file exists to block.
    """
    # bash, not sh: GitHub Actions runs a `run:` block under bash by default, and the script
    # opens with `set -euo pipefail`, which dash rejects outright ("Illegal option -o
    # pipefail"). Executing it under /bin/sh would test a shell CI never uses — and on Linux,
    # where sh IS dash, every case would fail for a reason the workflow doesn't have.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable; the verdict script is a bash script in CI")
    run = _verdict()["steps"][-1]["run"]
    proc = subprocess.run(
        [bash, "-c", run],
        env={"SELF_HOSTED": self_hosted, "FORK": fork, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )
    should_pass = (self_hosted, fork) in _ACCEPTED
    assert (proc.returncode == 0) is should_pass, (
        f"verdict exited {proc.returncode} for "
        f"(self-hosted={self_hosted!r}, fork={fork!r}); expected "
        f"{'pass' if should_pass else 'FAIL'}. A suite that did not run, was "
        f"cancelled, or failed is not a suite that passed.\n{proc.stdout}{proc.stderr}")


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
    assert self_hosted["runs-on"] == "starsling-ubuntu-26.04"
    assert "!=" not in str(self_hosted.get("if", "")) and \
        "== github.repository" in str(self_hosted.get("if", "")), (
        "self-hosted suite must run only for pushes and same-repo PRs")
    assert fork["runs-on"] == "ubuntu-latest"
    assert "!= github.repository" in str(fork.get("if", "")), (
        "fork suite must run only for PRs whose head repo is not this repo")
    # A job-level `success` says nothing about steps skipped by a step-level
    # `if:` — the verdict only ever reads the JOB result, so a suite whose last
    # step is `echo ok`, or is skipped, still reports success and goes green.
    for job_id, job in (("test-self", self_hosted), ("test-fork", fork)):
        last = job["steps"][-1]
        assert last.get("run", "").strip() == _SUITE_COMMAND, (
            f"{job_id}'s last step must be the suite itself, got {last!r}")
        assert last.get("env") == {"SHARD": "${{ matrix.shard }}"}, (
            f"{job_id}'s suite step must take its part number from the matrix")
        assert "if" not in last, (
            f"{job_id}'s suite step must not carry a step-level `if:` — it would "
            "skip the tests while the job still reports success to the verdict")


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
            if _forges_the_check_name(jid, job):
                offenders.append(f"{wf.name}:{jid}")
    assert not offenders, (
        f"job(s) outside ci.yml carry the required check name 'test': {offenders}")


@pytest.mark.parametrize("job_id", ["test-self", "test-fork"])
def test_every_suite_part_runs(job_id):
    """The suite is split into parts that run as one matrix job. A part that is
    never scheduled is a set of tests that never runs while the job still reports
    success: the matrix must list exactly parts 1..N for the N the command divides
    by, with no include/exclude that could add or drop a leg."""
    job = _jobs()[job_id]
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    assert set(matrix) == {"shard"}, (
        f"{job_id}'s matrix must carry only `shard` (no include/exclude): {matrix}")
    assert matrix["shard"] == list(range(1, _SHARDS + 1)), (
        f"{job_id} must run every part 1..{_SHARDS} of the suite, got {matrix['shard']}")


@pytest.mark.parametrize("job_id", ["test-self", "test-fork"])
def test_no_suite_part_may_fail_quietly(job_id):
    """`continue-on-error` on a matrix job lets a failing part report success to
    `needs.<job>.result`, so the verdict would pass a suite with failing tests."""
    job = _jobs()[job_id]
    assert "continue-on-error" not in job, (
        f"{job_id} must not set continue-on-error: a failed part would read as a pass")
    assert all("continue-on-error" not in step for step in job["steps"]), (
        f"{job_id}'s steps must not set continue-on-error")


# --- merge queue -----------------------------------------------------------------------------
# With a merge queue on main, GitHub builds each entry on a temporary
# `gh-readonly-queue/main/...` branch and waits for the required checks there,
# delivered as a `merge_group` event. A required check whose workflow does not
# trigger on merge_group never reports, and the queue waits forever. A
# merge_group event also carries no `github.event.pull_request`, so a suite
# guard written only in terms of the PR head repo silently routes queue entries
# to the wrong job. These tests EVALUATE the guards (tests/_gha_expr.py, fixture
# tested in tests/test_gha_expr.py) instead of grepping them.

import importlib.util as _ilu
import sys as _sys

_gx_spec = _ilu.spec_from_file_location("_gha_expr", Path(__file__).resolve().parent / "_gha_expr.py")
gx = _ilu.module_from_spec(_gx_spec)
_sys.modules[_gx_spec.name] = gx
_gx_spec.loader.exec_module(gx)

# Every check branch protection requires on main, and the workflow that produces it.
_REQUIRED_CHECKS = {
    "test": "ci.yml",
    "ci-secure": "ci-secure-check.yml",
    "python floor": "python-floor.yml",
}


def _triggers(doc):
    return doc[True] if True in doc else doc["on"]


def test_ci_triggers_include_the_merge_queue():
    triggers = _triggers(yaml.safe_load(_CI.read_text()))
    assert "pull_request" in triggers and "merge_group" in triggers, (
        "ci.yml must trigger on merge_group, or the required `test` check never reports "
        "for a merge-queue entry and the queue waits forever")
    assert triggers["push"]["branches"] == ["main"]


@pytest.mark.parametrize("route", sorted(gx.ROUTES))
def test_exactly_the_expected_suite_runs_for_every_event(route):
    """Self-hosted for pushes, same-repo PRs and merge-queue entries; hosted for
    fork PRs (including a deleted fork's). Never both, never neither."""
    name, kw, where = gx.ROUTES[route]
    ctx = gx.event(name, **kw)
    jobs = _jobs()
    runs = {jid: gx.job_runs(jobs[jid], ctx) for jid in ("test-self", "test-fork")}
    assert runs == {"test-self": where == "self", "test-fork": where == "fork"}, (
        f"{route}: expected only the {where} suite to run, got {runs}")
    assert gx.job_runs(_verdict(), ctx), f"{route}: the `test` verdict must always run"


@pytest.mark.parametrize("route", sorted(gx.ROUTES))
@pytest.mark.parametrize("suite_result", ["success", "failure", "cancelled"])
def test_the_verdict_tracks_the_suite_that_actually_ran(route, suite_result):
    """End to end per event: route the event through both guards, give the job
    that runs `suite_result` and the other `skipped`, and run the real verdict
    script. It passes only when the suite that ran passed — so a merge_group
    run that skipped its suite cannot report a green `test`."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable; the verdict script is a bash script in CI")
    name, kw, _ = gx.ROUTES[route]
    ctx = gx.event(name, **kw)
    jobs = _jobs()
    result = {jid: (suite_result if gx.job_runs(jobs[jid], ctx) else "skipped")
              for jid in ("test-self", "test-fork")}
    proc = subprocess.run(
        [bash, "-c", _verdict()["steps"][-1]["run"]],
        env={"SELF_HOSTED": result["test-self"], "FORK": result["test-fork"],
             "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert (proc.returncode == 0) is (suite_result == "success"), (
        f"{route}: verdict exited {proc.returncode} for {result}\n{proc.stdout}{proc.stderr}")


@pytest.mark.parametrize("check", sorted(_REQUIRED_CHECKS))
def test_every_required_check_reports_on_a_merge_group(check):
    """Each required check's workflow triggers on merge_group with no filter that
    could keep it from firing, and the job carrying the check name is scheduled
    on a merge_group event."""
    doc = yaml.safe_load((_WORKFLOWS / _REQUIRED_CHECKS[check]).read_text())
    triggers = _triggers(doc)
    assert "merge_group" in triggers, (
        f"{_REQUIRED_CHECKS[check]} produces the required `{check}` check but does not "
        "trigger on merge_group: the merge queue would wait for it forever")
    assert not (triggers["merge_group"] or {}).get("branches-ignore"), (
        f"{_REQUIRED_CHECKS[check]}: a merge_group filter could drop main's queue")
    branches = (triggers["merge_group"] or {}).get("branches")
    assert branches in (None, ["main"]), branches
    for key in ("paths", "paths-ignore"):
        assert key not in (triggers.get("pull_request") or {}), (
            f"{_REQUIRED_CHECKS[check]}: a path-filtered pull_request trigger makes "
            f"`{check}` absent on some PRs, and an absent required check blocks the merge")
    named = [j for jid, j in doc["jobs"].items() if _display_name(jid, j) == check]
    assert len(named) == 1, f"exactly one job must carry `{check}`"
    assert gx.job_runs(named[0], gx.event("merge_group")), (
        f"the `{check}` job is not scheduled on merge_group")
