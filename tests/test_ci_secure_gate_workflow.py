"""The ci-secure gate must report one check name, and that name must mean "it passed".

`.github/workflows/ci-secure-check.yml` splits the scan in two: fork PRs run on a
GitHub-hosted runner, everything else dogfoods StarSling's. Those two jobs are
mutually exclusive, so neither of their check names is safe to require in branch
protection — a required check that never runs is reported as absent, not as red, and
GitHub lets the pull request merge. A fork could therefore fail the security scan and
still merge, which is precisely the hole the gate exists to close.

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
    # makes that true: `head.repo` is null once a fork is deleted while its PR is
    # open, and in GitHub expressions `null == false` and `null == true` are BOTH
    # false, so a `.fork`-based pair would skip both jobs on exactly that PR.
    predicate = "github.event.pull_request.head.repo.full_name == github.repository"
    assert " ".join(scan["if"].split()) == f"github.event_name != 'pull_request' || {predicate}"
    assert " ".join(fork["if"].split()) == (
        f"github.event_name == 'pull_request' && {predicate.replace(' == ', ' != ')}"
    )

    assert scan["steps"][-1]["run"] == fork["steps"][-1]["run"], (
        "both runners must execute the identical gate, or the fork twin is a weaker check"
    )
    assert scan["steps"][-1]["run"] == "python3 .github/scripts/ci_secure_gate.py", (
        "the jobs must run the gate, not the engine: scan.py prints its JSON and exits 0 "
        "whatever it found, so a step that calls it directly is a green-forever check"
    )


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
    assert set(env.values()) == {
        "${{ needs.scan.result }}",
        "${{ needs.scan-fork.result }}",
    }
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
