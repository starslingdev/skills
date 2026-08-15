"""The ci-secure gate must report one check name, and that name must mean "it passed".

`.github/workflows/ci-secure-check.yml` splits the scan in two: fork PRs run on a
GitHub-hosted runner, everything else dogfoods StarSling's. Those two jobs are
mutually exclusive, so neither of their check names is safe to require in branch
protection — a job skipped by a conditional reports Success to the required-check rule,
so requiring the self-hosted job's name would be satisfied by a fork PR never running
it. A fork could therefore fail the security scan and still merge, which is precisely
the hole the gate exists to close. (A check that is genuinely absent stays Pending and
does block; it is the skip that reads as green, which is why this is easy to miss.)

The fix is a third job that always runs and carries the bare `ci-secure` name: it
passes only when the one scan that was supposed to run actually ran and passed. These
tests pin both halves — the shape (one always-running job owns the name, and nothing
else claims it) and the behaviour (the verdict script itself, executed against every
combination of upstream job results).

Pure YAML and shell; no network, no runner, no token.
"""
from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "ci-secure-check.yml"

# The check name branch protection is told to require. It must belong to exactly one
# job, and that job must run on every event the workflow fires on.
_REQUIRED_CHECK = "ci-secure"

# Every result GitHub can report for a `needs:` dependency.
_RESULTS = ("success", "failure", "cancelled", "skipped")

# The only two ways the gate can legitimately be satisfied: one scan ran and passed,
# and the other was skipped because it was not applicable to this event.
_PASSING = {("success", "skipped"), ("skipped", "success")}


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert _WORKFLOW.is_file(), f"{_WORKFLOW} is missing"
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def verdict(workflow: dict) -> dict:
    named = [job for job in workflow["jobs"].values() if job.get("name") == _REQUIRED_CHECK]
    assert len(named) == 1, (
        f"exactly one job may be called {_REQUIRED_CHECK!r} - branch protection requires "
        f"that name, and {len(named)} jobs claim it"
    )
    return named[0]


@pytest.fixture(scope="module")
def verdict_script(verdict: dict) -> str:
    steps = verdict["steps"]
    assert len(steps) == 1, "the verdict job should be a single script step"
    return steps[0]["run"]


def test_the_required_check_is_a_verdict_over_both_scans(workflow: dict, verdict: dict) -> None:
    """The `ci-secure` name belongs to a job that always runs and needs both scans.

    If it needed only one scan, or carried an `if:` that a fork PR could fail, the
    required check would go missing on exactly the pull requests it most needs to
    block.
    """
    assert set(verdict["needs"]) == {"scan", "scan-fork"}, (
        f"the verdict must depend on both scan jobs, not {verdict.get('needs')!r}"
    )
    assert " ".join(str(verdict["if"]).split()) == "always()", (
        f"the verdict job's if: is {verdict['if']!r}; anything narrower than always() "
        "lets the required check go absent when an upstream scan fails or is skipped"
    )
    assert "verdict" not in workflow["jobs"] or workflow["jobs"]["verdict"] is verdict


def test_the_gate_still_fires_on_pull_requests(workflow: dict) -> None:
    """The triggers themselves, which nothing else here pins.

    Every other test in this file assumes the workflow runs. Delete `pull_request`
    from the `on:` block and all of them still pass while the required `ci-secure`
    check silently stops being produced on pull requests — the exact outcome this
    whole PR exists to prevent, reached by deleting one line.
    """
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = workflow[True]
    assert "pull_request" in triggers, (
        "the gate must run on pull requests, or the required check is never reported"
    )
    assert "push" in triggers and triggers["push"]["branches"] == ["main"], (
        "main must be scanned too, or a direct push could land what a PR could not"
    )


def test_the_verdict_job_has_no_event_narrowing_condition(verdict: dict) -> None:
    """`if: always()` and nothing else.

    An added `github.event_name == ...` clause would make the check disappear on
    some events — and a required check that is skipped reads as a pass.
    """
    assert " ".join(str(verdict["if"]).split()) == "always()"


def test_the_scan_jobs_do_not_claim_the_required_name(workflow: dict) -> None:
    """The scan jobs are reported under distinguishable names.

    A scan job called `ci-secure` would let branch protection be pointed at a check
    that a fork PR never produces.
    """
    scans = {jid: workflow["jobs"][jid] for jid in ("scan", "scan-fork")}
    for jid, job in scans.items():
        assert job["name"] != _REQUIRED_CHECK, f"{jid} must not claim the required check name"
        assert _REQUIRED_CHECK in job["name"], f"{jid}'s name should still read as a ci-secure run"
    assert scans["scan"]["name"] != scans["scan-fork"]["name"]


def test_the_scan_jobs_stay_mutually_exclusive_and_cover_every_event(workflow: dict) -> None:
    """Fork code never reaches a self-hosted runner, and every event still gets a scan."""
    scan = workflow["jobs"]["scan"]
    fork = workflow["jobs"]["scan-fork"]

    assert scan["runs-on"].startswith("starsling-"), "non-fork scans dogfood StarSling runners"
    assert fork["runs-on"] == "ubuntu-latest", "fork code runs only on GitHub-hosted runners"

    # The two guards are exact complements of one predicate, so their union is
    # total and their intersection empty for every event. Comparing full_name to
    # the repository — the predicate ci.yml and ci-fork.yml already use — is what
    # makes that true, and the reason is worth stating precisely, because the
    # obvious alternative fails in the dangerous direction.
    #
    # `head.repo` is null once a fork is deleted while its PR is still open.
    # GitHub expressions compare mismatched types by casting to a number, and null
    # casts to 0 — the same as `false`. So `head.repo.fork == false` is TRUE on
    # that PR, and a `.fork`-based pair would hand fork-ref code to the SELF-HOSTED
    # job. Comparing against a repository name casts to NaN instead, and NaN equals
    # nothing: the `==` guard is false and the `!=` guard is true, so the
    # deleted-fork PR goes to the GitHub-hosted job, where it belongs.
    predicate = "github.event.pull_request.head.repo.full_name == github.repository"
    assert " ".join(scan["if"].split()) == f"github.event_name != 'pull_request' || {predicate}"
    assert " ".join(fork["if"].split()) == (
        f"github.event_name == 'pull_request' && {predicate.replace(' == ', ' != ')}"
    )

    assert scan["steps"][-1]["run"] == fork["steps"][-1]["run"], (
        "both runners must execute the identical gate, or the fork twin is a weaker check"
    )
    run = scan["steps"][-1]["run"]
    assert run == "python3 .github/scripts/ci_secure_gate.py", (
        "the jobs must run the gate, not the engine: scan.py prints its JSON and exits 0 "
        "whatever it found, so a step that calls it directly is a green-forever check"
    )
    # String identity alone is not enough, and neither is existence alone. A
    # rename that updates the workflow and forgets nothing still has to point at
    # a file that is there; and `is_file()` on its own would be satisfied just as
    # happily by `python3 skills/ci-secure/scripts/scan.py`, which is the
    # green-forever substitution the string assert above exists to block.
    assert (_REPO / run.split()[-1]).is_file(), (
        f"the workflow runs {run.split()[-1]!r}, which does not exist")


def test_only_the_trusted_job_can_run_the_network_gated_check(workflow: dict) -> None:
    """The impostor check runs for real where there is a token, and nowhere else.

    P14.11 verifies that each pinned action SHA actually exists in the action's
    canonical repository — the check that catches an impostor pin pointing at a
    fork-only or dangling commit. It needs the network and an authenticated
    `gh`, so it only runs in the job that has one.

    The fork twin deliberately does not get one. That job executes code the
    pull request author wrote, and handing it a token variable would be
    handing attacker-authored steps a credential to use — a much larger loss
    than the coverage gained. It runs with the check OFF and says so on both
    surfaces instead, because a check that did not run is never a pass.
    """
    scan = workflow["jobs"]["scan"]["steps"][-1]
    fork = workflow["jobs"]["scan-fork"]["steps"][-1]

    assert scan["env"]["CI_SECURE_GH_IMPOSTOR"] == "on", (
        "the trusted job must turn the check on explicitly; omitting it lands on "
        "the engine's `auto`, where a runner image decides whether a security "
        "check runs")
    assert scan["env"]["GH_TOKEN"] == "${{ github.token }}", (
        "the check needs an authenticated gh; the job's own read-only token is "
        "the least privilege that works")

    assert fork["env"]["CI_SECURE_GH_IMPOSTOR"] == "off", (
        "the fork twin must turn it off explicitly, not leave it to a default")
    # Checked at all THREE scopes a variable can be set at, because they all
    # reach the same step. Asserting only on the step's own `env:` let a
    # job-level `GH_TOKEN` — the identical exposure — through.
    for scope, env in (("workflow", workflow.get("env") or {}),
                       ("job", workflow["jobs"]["scan-fork"].get("env") or {}),
                       ("step", fork.get("env") or {})):
        assert not any("TOKEN" in key.upper() for key in env), (
            f"a token-shaped variable is set at {scope} scope and reaches the "
            "fork job, which runs pull-request-authored code and must not be "
            "handed a credential of any kind")

    # Belt and braces: no `secrets.` reference anywhere in the fork job,
    # whatever the variable happens to be named.
    fork_yaml = yaml.safe_dump(workflow["jobs"]["scan-fork"])
    assert "secrets." not in fork_yaml, (
        f"the fork job references a secret:\n{fork_yaml}")


def test_the_scan_jobs_do_not_leave_the_job_token_in_the_checkout(
        workflow: dict) -> None:
    """This workflow gets the same credential scoping it enforces on everyone else.

    The gate is a Python program reading the checked-out tree, and on a fork PR
    the author wrote that tree. GitHub's default checkout leaves the job token
    in `.git/config`; nothing here needs it, so nothing here should carry it.
    The engine's own `sec.checkout.credentials-scoped` fact is scoped to
    untrusted triggers and passes either way, so only this test holds the line —
    which is why removing both `persist-credentials: false` lines was previously
    green everywhere.
    """
    for name in ("scan", "scan-fork"):
        checkouts = [step for step in workflow["jobs"][name]["steps"]
                     if "actions/checkout" in str(step.get("uses", ""))]
        assert checkouts, f"{name} checks nothing out"
        for step in checkouts:
            assert step.get("with", {}).get("persist-credentials") is False, (
                f"{name}'s checkout persists the job token into .git/config")


def test_the_engine_timeout_stays_inside_the_jobs_own_clock() -> None:
    """A timeout at parity with the job clock can never fire.

    The gate's `ENGINE_TIMEOUT_S` exists so a hung engine fails with a STATED
    cause instead of an unexplained job cancellation. That only works while it
    is strictly shorter than the job's `timeout-minutes` — and it must clear it
    by enough to cover checkout and pip, which have already spent part of that
    clock before the gate starts. This is the regression that shipped once
    already, and nothing but arithmetic catches it.
    """
    gate = (_REPO / ".github" / "scripts" / "ci_secure_gate.py").read_text(
        encoding="utf-8")
    # Anchored on the ceiling's own name rather than on `or NNN)`, which would
    # match the first such fragment anywhere in the file.
    match = re.search(r"^_TIMEOUT_CEILING = (\d+)$", gate, re.M) or re.search(
        r"^ENGINE_TIMEOUT_S = (\d+)$", gate, re.M)
    assert match, "the engine timeout is not a readable literal any more"
    engine_timeout = int(match.group(1))

    # The env override must be clamped to that ceiling, or a workflow could set
    # the timeout back out of reach and this test — which reads the source —
    # would still pass.
    assert "_TIMEOUT_CEILING" in gate and "ENGINE_TIMEOUT_S = _TIMEOUT_CEILING" in gate, (
        "the engine timeout override is no longer clamped to a ceiling")

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    for name in ("scan", "scan-fork"):
        job_budget = workflow["jobs"][name]["timeout-minutes"] * 60
        assert engine_timeout < job_budget, (
            f"{name}: the engine timeout ({engine_timeout}s) is not shorter than "
            f"the job's own budget ({job_budget}s), so it can never fire and a "
            "hung engine reaches the check run as an unexplained cancellation")


def test_a_weekly_run_rechecks_the_default_branch_and_demands_completeness(
        workflow: dict) -> None:
    """Only a scheduled run catches a pin that rots AFTER it merged.

    Every other trigger here judges a proposed change. But an action SHA that
    is legitimate today can be deleted or orphaned tomorrow, in a repository
    nobody here controls — nothing about our own pull requests would ever
    notice. The weekly run against the default branch is the only shape that
    does.

    It also runs strict: a network check that could not complete is red there,
    where a pull request only warns. A pull request must not be blocked by
    someone else's rate limit, but the weekly run has no deadline and nothing
    to race, and letting it pass on an incomplete check would make the one run
    that exists to catch rot the easiest one to mute.
    """
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "schedule" in triggers, "no scheduled run: post-merge pin rot goes unnoticed"
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert crons, "the schedule trigger carries no cron expression"

    env = workflow["jobs"]["scan"]["steps"][-1]["env"]
    strict = " ".join(str(env["CI_SECURE_GH_STRICT"]).split())
    # Both branches, not a substring. `"schedule" in strict` is satisfied by the
    # exact INVERSION of this policy — strict on every pull request and lax on
    # the weekly run — which would block PRs on somebody else's rate limit while
    # muting the one run that catches rot.
    assert strict == "${{ github.event_name == 'schedule' && '1' || '0' }}", (
        "strict mode must be ON for the scheduled run and OFF elsewhere; it is "
        f"wired as {strict!r}")

    # The concurrency key separates events, so the weekly run cannot be
    # cancelled by an ordinary push — which would silently cost us the one run
    # that catches rot.
    assert "github.event_name" in workflow["concurrency"]["group"]


def test_the_verdict_job_is_hosted_and_checks_out_nothing(verdict: dict) -> None:
    """A fork PR can rewrite this job's script, so it must not run on our own runners."""
    assert verdict["runs-on"] == "ubuntu-latest", (
        "the verdict job runs on fork pull requests and its script travels with the head "
        "ref; a self-hosted runner here would execute fork-authored shell on our hardware"
    )
    assert isinstance(verdict["timeout-minutes"], int) and 0 < verdict["timeout-minutes"] <= 15
    assert not any("uses" in step for step in verdict["steps"]), (
        "the verdict job compares two strings; it has no reason to check out code"
    )


def test_the_verdict_reads_both_results_from_env_not_interpolation(verdict: dict) -> None:
    """Job results reach the script through `env:`, never spliced into the shell text."""
    step = verdict["steps"][0]
    env = step["env"]
    assert env == {
        "SELF_HOSTED": "${{ needs.scan.result }}",
        "FORK": "${{ needs.scan-fork.result }}",
    }
    # Names, not just values: renaming a key in the YAML while the script still
    # reads the old one makes the required check permanently red under `set -u`.
    for name in env:
        assert f"${{{name}}}" in step["run"], f"{name} is set but never read"
    assert "${{" not in step["run"], (
        "expressions interpolated straight into a run: block are a template-injection "
        "shape; pass them through env: instead"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run the gate script")
@pytest.mark.parametrize("self_hosted,fork", list(itertools.product(_RESULTS, repeat=2)))
def test_the_verdict_passes_only_when_a_scan_actually_passed(
    verdict_script: str, self_hosted: str, fork: str, tmp_path: Path
) -> None:
    """Run the real script from the workflow against every pair of upstream results.

    This is the assertion that would have caught the original hole: with the two scan
    jobs reporting under separate names there was no script to run at all, and the
    fork twin's failure simply left the required check unreported.
    """
    script = tmp_path / "verdict.sh"
    script.write_text(verdict_script, encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(script)],
        env={"PATH": "/usr/bin:/bin", "SELF_HOSTED": self_hosted, "FORK": fork},
        capture_output=True,
        text=True,
    )

    should_pass = (self_hosted, fork) in _PASSING
    assert (proc.returncode == 0) is should_pass, (
        f"self-hosted={self_hosted}, fork={fork} exited {proc.returncode}; "
        f"expected {'pass' if should_pass else 'fail'}\n{proc.stdout}\n{proc.stderr}"
    )
    if not should_pass:
        assert "::error::" in proc.stdout, "a failing verdict must annotate the check run"


def test_the_workflow_grants_no_more_than_read(workflow: dict) -> None:
    """The gate only reads the checked-out tree; no job may escalate."""
    assert workflow["permissions"] == {"contents": "read"}
    for jid, job in workflow["jobs"].items():
        assert "permissions" not in job, f"{jid} must not widen the workflow's read-only grant"


def test_actions_are_pinned_to_full_shas(workflow: dict) -> None:
    """Matches every other workflow in this repo: no mutable tags."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            ref = step.get("uses")
            if ref is None:
                continue
            _, _, version = ref.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", version), f"{ref} is not pinned to a commit SHA"
            assert re.search(rf"{re.escape(ref)}\s+# v", text), f"{ref} has no `# vX.Y.Z` comment"


def test_codeowners_covers_every_path_that_could_forge_a_pass() -> None:
    """The three paths the workflow's own comments say review must watch.

    On a fork pull request the gate script, the engine and this workflow all
    come from the head ref, so nothing inside CI can vouch for the scan. What
    closes that is a human reading the diff, and CODEOWNERS is what routes it.
    The engine's `sec.codeowners.workflows` fact only asks whether
    `.github/workflows/` is covered, so deleting the other rules is green
    everywhere else — including in the gate's own scan of this repo.

    `pyproject.toml` is here because it is the off-switch for the tests: its
    `testpaths` list is what makes the suites below run at all, and removing an
    entry disables them without touching an owned path.
    """
    codeowners = _REPO / ".github" / "CODEOWNERS"
    assert codeowners.is_file(), "CODEOWNERS is missing; the fork caveat rests on it"
    rules = [line.split() for line in
             codeowners.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    owned = {parts[0]: parts[1:] for parts in rules if len(parts) > 1}

    for path in ("/.github/", "/skills/ci-secure/", "/tests/", "/pyproject.toml"):
        assert path in owned, (
            f"{path} has no CODEOWNERS rule. A pull request can edit the code "
            "that judges it, so every path that could forge a pass — the "
            "workflow, the engine, the tests that pin the rules, and the file "
            "that decides which tests run — must route to a human reviewer.")
        assert any(owner.startswith("@") for owner in owned[path]), (
            f"{path} has a CODEOWNERS rule with no owner")
