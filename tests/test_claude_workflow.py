"""The @claude workflow's own shape is enforced, so its security decisions cannot rot silently.

`.github/workflows/claude.yml` is the only workflow in this repo that starts from a
comment, holds an API key, and hands a write-capable agent a checkout of our own
tree. Every property that makes that safe is a single line that a well-meaning edit
can delete without anything turning red: drop the `author_association` half of the
`if:` and any drive-by account starts a job on our self-hosted runners; drop
`persist-credentials: false` and the job token sits in `.git/config` inside the tree
Claude's own tools run in; unpin an action and an upstream retag runs unreviewed code
next to the API key; delete `timeout-minutes` and a stalled agent holds a runner for
GitHub's six-hour default.

None of those failures are visible in a run. The workflow still triggers, still
passes, still looks fine. These tests pin the properties instead. They are pure YAML
and text assertions — no network, no runner, no token — so they run in the same
offline `pytest -v` as everything else.

They deliberately do NOT assert that Claude behaves well once it is running; the
action's own access-control and prompt-sanitizing are upstream's, and are documented
in the file's comments rather than tested here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "claude.yml"

# PyYAML parses a bare `on:` key as the boolean True, so the trigger block is read
# through this constant rather than the string "on".
_ON_KEY = True

# The maintainer classes GitHub reports for a commenter with standing in this repo.
# Anything outside this set — CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE — must not be
# able to start the job.
_MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert _WORKFLOW.is_file(), f"{_WORKFLOW} is missing"
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    jobs = workflow["jobs"]
    assert list(jobs) == ["claude"], "the workflow is expected to hold exactly one job"
    return jobs["claude"]


def test_triggers_are_comment_events_only(workflow: dict) -> None:
    """Only comment events, and only on creation.

    Widening this to `pull_request` or `pull_request_target` would change the trust
    model entirely — those carry fork context — and an `edited` type would let someone
    add "@claude" to an already-posted comment.
    """
    triggers = workflow[_ON_KEY]
    assert set(triggers) == {"issue_comment", "pull_request_review_comment"}
    for name, spec in triggers.items():
        assert spec["types"] == ["created"], f"{name} should fire on created only"


def test_job_gates_on_both_the_mention_and_the_commenter(job: dict) -> None:
    """The `if:` must require a maintainer AND a mention.

    The mention half alone is what the upstream example ships; on a public repo it
    lets any account start a job on our self-hosted runners. This test fails if
    either half is removed, or if the two are ever ORed together.
    """
    guard = " ".join(job["if"].split())

    assert "contains(github.event.comment.body, '@claude')" in guard
    assert "github.event.comment.author_association" in guard
    assert "&&" in guard, "the two conditions must both hold, not either"
    assert "||" not in guard, "an OR here would defeat the commenter gate"

    quoted = set(re.findall(r'"([A-Z_]+)"', guard))
    assert quoted == _MAINTAINER_ASSOCIATIONS, (
        f"the allowed commenter classes drifted to {sorted(quoted)}; "
        "adding CONTRIBUTOR or NONE would open the job to outsiders"
    )


def test_the_job_cannot_run_forever_or_race_itself(job: dict) -> None:
    """A bounded agent run, one at a time per issue/PR.

    Without the timeout a stalled agent holds a self-hosted runner for six hours.
    Without the concurrency group two mentions produce two agents pushing to one
    branch. `cancel-in-progress` must stay false: cancelling kills a Claude mid-push.
    """
    timeout = job["timeout-minutes"]
    assert isinstance(timeout, int) and 0 < timeout <= 60, (
        f"timeout-minutes is {timeout!r}; it must be set and modest"
    )

    concurrency = job["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "github.event.issue.number" in concurrency["group"]
    assert "github.event.pull_request.number" in concurrency["group"]


def test_id_token_write_is_present(job: dict) -> None:
    """`id-token: write` is load-bearing, not excess privilege.

    The action exchanges the job's OIDC token for the Claude App installation token
    that makes pushes come from the app rather than a maintainer. A least-privilege
    sweep that removes it breaks the workflow, so it is pinned with the reason.
    """
    assert job["permissions"]["id-token"] == "write"


def test_the_grant_set_is_exactly_what_the_action_needs(job: dict) -> None:
    """The whole permission block, not one key of it.

    This is the most privileged job in the repository — it holds the API key
    and runs an agent — and every grant here is argued for individually in the
    workflow's own comments. Pinning only `id-token` left the set open at the
    top: adding `packages: write` or `security-events: write` would widen the
    most dangerous job in the repo with nothing going red, while the equivalent
    test for the security gate already asserts its block exactly. The laxer
    test was guarding the riskier workflow.

    ci-secure's own `sec.permissions.write-scoped` fact does not cover this: it
    reads WORKFLOW-level grants, and these are job-level.
    """
    assert job["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
        "id-token": "write",
        "actions": "read",
    }, (
        "the @claude job's grants changed. Each is justified in the workflow's "
        "comments — id-token for the App token exchange, pull-requests: write "
        "for the buffered-review-comment fallback, actions: read for the "
        "read-only CI-status tool. Widening the job that holds the API key is "
        "a deliberate edit, not a drift."
    )


def test_no_workflow_level_permissions_block(workflow: dict) -> None:
    """Grants stay scoped to the one job that needs them."""
    assert "permissions" not in workflow


def test_every_action_is_pinned_to_a_full_sha(job: dict) -> None:
    """No mutable tags in a job that holds the API key and write permissions.

    A tag can be moved upstream; a SHA cannot. This matches every other workflow in
    the repo, and the `# vX.Y.Z` comment keeps the pin readable for humans.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    uses = [step["uses"] for step in job["steps"]]
    assert uses, "the job should have steps"

    for ref in uses:
        _, _, version = ref.partition("@")
        assert _SHA_RE.match(version), f"{ref} is not pinned to a 40-character commit SHA"
        assert re.search(rf"{re.escape(ref)}\s+# v", text), (
            f"{ref} has no `# vX.Y.Z` comment naming the version it pins"
        )


def test_checkout_does_not_persist_the_job_token(job: dict) -> None:
    """The job token must not be left in `.git/config`.

    Claude's Bash tool runs inside this working tree. The action swaps in its own App
    token for pushes either way, so this flag costs nothing and closes the window
    between checkout and that swap — including when the run fails in between.
    """
    checkout = next(s for s in job["steps"] if s["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_github_token_is_never_passed_to_the_action(job: dict) -> None:
    """Passing `github_token` would silently stop CI from running on Claude's commits.

    GitHub does not trigger workflows on commits made with the default GITHUB_TOKEN.
    Omitting the input is what makes the action authenticate as the Claude App, whose
    commits do trigger CI. This is easy to "helpfully" add back, and nothing would
    look broken — CI would just stop running on Claude's pushes.
    """
    action = next(
        s for s in job["steps"] if s["uses"].startswith("anthropics/claude-code-action@")
    )
    assert "github_token" not in action.get("with", {})


def test_runner_matches_the_rest_of_the_repo(job: dict) -> None:
    """This repo dogfoods StarSling runners for its own CI."""
    assert job["runs-on"] == "starsling-ubuntu-24.04"
