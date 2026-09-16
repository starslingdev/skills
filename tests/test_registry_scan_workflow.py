"""The registry-scan gate's own shape is enforced, so it cannot rot silently.

`.github/workflows/registry-scan.yml` runs a third-party security scanner over the
installable skill trees, so a rule violation fails our build instead of surfacing
as a public FAIL badge days after release. Almost every way that gate can decay is
invisible: a gate that stops reading the scan's document makes findings advisory, a
widened ignore list hides a real finding, a deleted `schedule` stops catching
rule-catalog changes that need no commit of ours, a drifted runner label quietly
moves the job off the runners the rest of this repo's CI dogfoods, a second scanner
call over the tree spends the day's allowance on nothing. In every one of those
cases the check still runs and still reports green — or red for the wrong reason.

These tests pin the properties the gate's value depends on. They are pure YAML and
text assertions — no network, no scanner, no token — so they run in the same
offline `pytest -v` as everything else. They cannot prove the scanner still
detects anything; that is `.github/scripts/registry_scan_redprove.py`, which runs
inside the workflow itself on every run.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "registry-scan.yml"
_CI_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"
_REDPROVE = _REPO / ".github" / "scripts" / "registry_scan_redprove.py"
_REPORT = _REPO / ".github" / "scripts" / "registry_scan_report.py"

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


def _invokes_scanner(step: dict) -> bool:
    """True for a step that actually shells out to the scanner's `scan` subcommand.

    Matched on the command line rather than on the word "scan" appearing anywhere in
    the step, so an echo, a heredoc, or a comment mentioning the scanner cannot stand
    in for a step that runs it.
    """
    return bool(re.search(r"uvx\s+snyk-agent-scan\S*\s+scan\b", step.get("run", "")))


def _gate_step(workflow: dict) -> dict:
    """The authoritative step: the one that runs the offline gate over the scan's JSON.

    Every property below is asserted against THIS step specifically. Asserting them
    against the job's concatenated step text is what let an earlier version of this
    file stay green while the gating flag moved onto the advisory pass.
    """
    steps = [
        step for step in _scan_job(workflow)["steps"]
        if "registry_scan_gate.py" in step.get("run", "")
    ]
    assert len(steps) == 1, (
        f"expected exactly one step running registry_scan_gate.py; found {len(steps)}"
    )
    return steps[0]


def _visibility_step(workflow: dict) -> dict:
    """The unfiltered pass: the ONE scanner invocation over SCAN_PATH.

    One, not two. Every invocation is a network request against the scanner's public
    tier, whose daily allowance is undocumented and, since 2026-09-14, smaller than a
    day of pushes. The verdict is derived offline from this pass's JSON; a second
    `--ci` call over the same tree bought nothing the document did not already say,
    and cost the call that pushed the day over the cap.
    """
    steps = [step for step in _scan_job(workflow)["steps"] if _invokes_scanner(step)]
    assert len(steps) == 1, (
        f"expected exactly one scanner invocation over SCAN_PATH; found {len(steps)}. "
        f"The verdict is derived from this pass's JSON by registry_scan_gate.py, so a "
        f"second call is a second charge against the daily cap for nothing."
    )
    assert "--ignore-risks" not in steps[0]["run"], (
        "the unfiltered pass carries an ignore list; the scanner strips ignored findings "
        "from its printed report as well as its exit status, so an accepted finding "
        "would be invisible everywhere"
    )
    return steps[0]


def _step_index(job: dict, predicate) -> int:
    for i, step in enumerate(job["steps"]):
        if predicate(step):
            return i
    raise AssertionError("no step matched")


def _assert_unconditional(step: dict, what: str) -> None:
    """A step with an `if:` or `continue-on-error:` is a step that can stop mattering.

    These are the two cheapest ways to neuter a gate step while leaving it visible in
    the file, so they are asserted rather than assumed.
    """
    assert "if" not in step, f"{what} is conditional; it must run on every run"
    assert not step.get("continue-on-error"), (
        f"{what} is continue-on-error; its failure would no longer fail the build"
    )


def test_workflow_exists_and_parses(workflow: dict):
    assert workflow["jobs"], "registry-scan.yml has no jobs"


def test_runs_on_pull_requests_and_pushes(triggers: dict):
    """PRs are path-filtered; everything else is not.

    A PR that touches nothing the scan reads gets no scan and no check — a deliberate
    trade against the scanner's daily allowance, which a day of unfiltered PR runs now
    exhausts. The push to main still scans every merge, the schedule still notices a
    rule-catalog change that needed no commit, and dispatch is there for a maintainer.
    Filtering any of those three would turn the trade into a hole.
    """
    assert "pull_request" in triggers, "the gate must run on pull requests"
    pr = triggers["pull_request"]
    assert isinstance(pr, dict) and pr.get("paths"), (
        "the pull_request trigger has no `paths:` filter, so every PR spends the day's "
        "scanner allowance whether or not it touches a skill")
    assert "push" in triggers, "the gate must run on pushes to main"
    push = triggers["push"]
    assert push.get("branches") == ["main"], "the push trigger must be scoped to main"
    assert "paths" not in push and "paths-ignore" not in push, (
        "the push trigger is path-filtered; a merge to main must always be scanned, "
        "because it is the run that certifies what ships")
    assert not isinstance(triggers.get("schedule"), dict) or "paths" not in triggers["schedule"], (
        "the schedule must not be path-filtered")
    dispatch = triggers.get("workflow_dispatch")
    assert dispatch is None or not (isinstance(dispatch, dict) and "paths" in dispatch), (
        "workflow_dispatch must not be path-filtered")


def _matches_path_filter(pattern: str, path: str) -> bool:
    """GitHub's `paths:` glob, reduced to what these patterns use: `**` crosses
    directory separators, `*` does not."""
    regex = "".join(
        ".*" if token == "**" else "[^/]*" if token == "*" else re.escape(token)
        for token in re.findall(r"\*\*|\*|[^*]+", pattern))
    return re.fullmatch(regex, path) is not None


def test_the_pull_request_path_filter_names_every_file_the_scan_depends_on(workflow: dict, triggers: dict):
    """Derived from the workflow, not hand-listed, so a new script cannot be forgotten.

    Everything the scan's behaviour depends on: the tree it scans, the workflow
    itself, every local script a `run:` line invokes (and every sibling
    `registry_scan_*.py` on disk, since the scripts import each other), and the test
    file that pins the workflow's shape. A change to any of these on a PR must run
    the scan; a filter that misses one lets that change merge on a green PR that
    never scanned it.
    """
    patterns = triggers["pull_request"]["paths"]
    scan_path = workflow["env"]["SCAN_PATH"]
    depends_on = {
        f"{scan_path}/some-skill/SKILL.md",
        f"{scan_path}/some-skill/scripts/helper.py",
        _WORKFLOW.relative_to(_REPO).as_posix(),
        Path(__file__).resolve().relative_to(_REPO).as_posix(),
    }
    for step in _scan_job(workflow)["steps"]:
        depends_on.update(re.findall(r"\.github/scripts/[\w./-]+\.py", step.get("run") or ""))
    depends_on.update(
        p.relative_to(_REPO).as_posix()
        for p in (_REPO / ".github" / "scripts").glob("registry_scan_*.py"))
    assert len(depends_on) >= 8, f"derivation found too little: {sorted(depends_on)}"

    unmatched = sorted(
        path for path in depends_on
        if not any(_matches_path_filter(pattern, path) for pattern in patterns))
    assert not unmatched, (
        f"the pull_request path filter {patterns} does not cover: {unmatched}. A PR "
        f"changing one of these would merge without the scan running.")
    # And it must not be so wide that the trade buys nothing.
    assert not any(_matches_path_filter(p, "README.md") for p in patterns), (
        "the path filter matches README.md; a docs-only PR would still spend the allowance")


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
    assert _scan_job(workflow)["runs-on"] == ci["jobs"]["test-self"]["runs-on"]


def test_fork_prs_never_reach_the_self_hosted_runner(workflow: dict):
    """Fork code must not execute on self-hosted runners (the ci.yml contract)."""
    guard = _scan_job(workflow)["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in guard


def test_the_gate_is_offline_and_unconditional(workflow: dict):
    """The gate reads the JSON; it does not ask the scanner a second time.

    Asserted against the gating step itself. A scanner invocation in the gate step is
    the second network call this workflow stopped spending; `--ci` on the unfiltered
    pass would make that pass exit 1 on any risk while printing nothing (JSON mode
    prints no exit line), and `--ignore-risks` anywhere in the job would null exempt
    findings out of the one document that is supposed to show them.
    """
    gate = _gate_step(workflow)
    assert not _invokes_scanner(gate), (
        "the gate step invokes the scanner; the verdict is supposed to come from the "
        "unfiltered pass's JSON, not from a second network call"
    )
    _assert_unconditional(gate, "the gate")
    visibility = _visibility_step(workflow)
    assert "--json" in visibility["run"], (
        "the unfiltered pass no longer emits --json, which is the document the gate reads"
    )
    assert "--ci" not in visibility["run"], (
        "the unfiltered pass carries --ci: in JSON mode that exits 1 on any risk without "
        "printing why, and the exit is then read as 'could not start'"
    )
    assert "--ignore-risks" not in _steps_text(_scan_job(workflow)), (
        "an ignore list is passed to the scanner somewhere in the job; the exemption is "
        "applied offline by the gate so the one document shows every finding"
    )


def test_the_gate_reads_the_document_the_unfiltered_pass_wrote(workflow: dict):
    """One file name, used by the pass that writes it and every step that reads it.

    The gate classifies on that document, so a renamed redirect target would leave
    the gate reading a file nobody wrote — which it reports as DID NOT RUN, honestly,
    on every run.
    """
    visibility = _visibility_step(workflow)
    targets = set(re.findall(r">\s*(\S+\.json)", visibility["run"]))
    assert len(targets) == 1, f"expected one JSON redirect target in the unfiltered pass, found {targets}"
    document = targets.pop()
    assert document in _gate_step(workflow)["run"], "the gate does not read the document the pass wrote"
    report = next(s for s in _scan_job(workflow)["steps"] if "registry_scan_report.py" in s.get("run", ""))
    assert document in report["run"], "the reporter does not read the document the pass wrote"
    upload = next(s for s in _scan_job(workflow)["steps"] if "upload-artifact" in str(s.get("uses", "")))
    assert upload["with"]["path"] == document, "the artifact is not the document the pass wrote"


def test_warnings_are_surfaced_on_all_three_channels(workflow: dict):
    """Warnings do not block, so they have to be impossible to miss instead.

    Each channel serves a different consumer and none substitutes for the others: the
    annotations are what an automated review agent queries through the checks API, the job
    summary is what a human glances at, and the artifact is the deterministic full set that
    tooling can fetch without scraping a log. If "does not block" quietly became "does not
    appear", the gate would be back to the failure it was built to prevent.
    """
    job = _scan_job(workflow)
    steps_text = _steps_text(job)

    assert _REPORT.exists(), "the finding-surfacing script is missing"
    assert "registry_scan_report.py" in steps_text, (
        "nothing surfaces the findings; warnings would be invisible as well as non-blocking"
    )

    report_step = next(s for s in job["steps"] if "registry_scan_report.py" in s.get("run", ""))
    _assert_unconditional(report_step, "the finding-surfacing step")

    uploads = [
        step for step in job["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert uploads, "the finding set is never uploaded as an artifact"
    assert uploads[0]["with"]["name"] == "registry-scan-findings", (
        "the artifact name is the documented handle tooling fetches by; changing it breaks "
        "every consumer that reads it"
    )

    source = _REPORT.read_text(encoding="utf-8")
    assert "::warning" in source, "no warning annotations are emitted"
    assert "GITHUB_STEP_SUMMARY" in source, "nothing is written to the job summary"


def test_surfacing_findings_never_fails_the_build(workflow: dict):
    """Warnings must not block — including through the back door of the reporter.

    The reporter's only non-zero exit is unparseable scanner output, which is a broken
    pipeline rather than a finding. Any other non-zero path would turn a warning into a
    red build and quietly reverse the gating rule.
    """
    source = _REPORT.read_text(encoding="utf-8")
    returns = set(re.findall(r"^\s+return (\d+)$", source, re.M))
    assert returns <= {"0", "1", "2"}, f"unexpected exit codes in the reporter: {returns}"
    assert "unparseable" in source.lower() or "UNREADABLE" in source, (
        "the reporter's failure path must be about unreadable output, not about findings"
    )


def test_full_findings_are_printed_unfiltered(workflow: dict):
    """The scanner strips ignored findings from its printed report as well as from
    its exit status, so the one pass over SCAN_PATH is unfiltered and the exemption
    is applied afterwards, offline. If that pass ever grows an ignore list, accepted
    findings stop appearing anywhere."""
    visibility = _visibility_step(workflow)
    gate = _gate_step(workflow)
    assert visibility is not gate, (
        "the unfiltered pass and the gate are the same step; the gate must be the "
        "offline reader, not a scanner invocation"
    )
    # The visibility pass is advisory on purpose — the gate below is what fails the build —
    # but only the visibility pass may be advisory.
    assert visibility.get("continue-on-error") is True, (
        "the unfiltered pass must be advisory; its job is to write the document, not to gate"
    )


def test_scan_path_points_at_a_tree_that_actually_holds_skills(workflow: dict):
    """A gate that scans nothing passes.

    The scanner reads a directory of `<name>/SKILL.md` subdirectories. Point it at a
    renamed, moved, or restructured tree and it finds zero skills, reports nothing, and
    exits 0 — a green check over an empty scan. The red-proof cannot catch this: it scans
    its own temp fixture and never touches SCAN_PATH. So the path is pinned here, and the
    workflow re-checks it at runtime against the tree it actually checked out.
    """
    scan_path = _REPO / workflow["env"]["SCAN_PATH"]
    assert scan_path.is_dir(), f"SCAN_PATH {scan_path} is not a directory in this repo"
    skills = sorted(p.parent.name for p in scan_path.glob("*/SKILL.md"))
    assert skills, (
        f"SCAN_PATH {workflow['env']['SCAN_PATH']!r} contains no '<name>/SKILL.md' — the "
        f"scanner would find nothing to scan and the gate would pass over an empty tree"
    )

    text = _steps_text(_scan_job(workflow))
    assert "SKILL.md" in text and "HAS NOTHING TO SCAN" in text, (
        "the workflow must fail at runtime if SCAN_PATH holds no skills; this test only "
        "pins the tree as it stands in the repo, not as it is checked out in CI"
    )


def _red_proof_step(workflow: dict) -> dict:
    steps = [
        step for step in _scan_job(workflow)["steps"]
        if "registry_scan_redprove.py" in step.get("run", "")
    ]
    assert len(steps) == 1, "expected exactly one red-proof step"
    return steps[0]


def _enforcement_step(workflow: dict) -> dict:
    """The step that turns a failed control into a failed build.

    The control is `continue-on-error` so the scan behind it still runs and still
    reports (see `test_findings_are_surfaced_even_when_the_control_fails`). That
    makes this step the thing standing between "the gate cannot be proven able to
    fail" and a green check, so it is asserted by name.
    """
    red_proof_id = _red_proof_step(workflow).get("id")
    assert red_proof_id, (
        "the red-proof step needs an `id:` for a later step to read its outcome"
    )
    steps = [
        step for step in _scan_job(workflow)["steps"]
        if f"steps.{red_proof_id}.outcome" in step.get("run", "")
    ]
    assert len(steps) == 1, (
        f"expected exactly one step reading `steps.{red_proof_id}.outcome` and "
        f"failing the build on it; found {len(steps)}"
    )
    return steps[0]


def test_red_proof_runs_on_every_run(workflow: dict):
    """A check that cannot fail is not a check.

    An `if:` on this step disables the one guarantee the whole gate rests on while
    leaving it plainly visible in the file, so it is asserted — and it must run
    before the gating scan, so a broken anchor is reported as such rather than as
    a scan result.

    `continue-on-error` is now REQUIRED here rather than banned, and the reason is
    the 2026-08-18 outage: with a hard failure the job stopped at this step, so a
    scanner that had gone blind cost us not only the gate but every finding it
    might still have reported — the unfiltered pass never ran and the artifact
    uploaded nothing. The guarantee moved rather than weakened: the build still
    fails, from the enforcement step below.
    """
    assert _REDPROVE.exists(), "the red-proof script is missing"
    job = _scan_job(workflow)
    red_proof = _red_proof_step(workflow)
    assert "if" not in red_proof, "the red-proof step is conditional; it must always run"

    gate_index = _step_index(job, lambda s: s is _gate_step(workflow))
    redprove_index = _step_index(
        job, lambda s: "registry_scan_redprove.py" in s.get("run", "")
    )
    assert redprove_index < gate_index, (
        "the red-proof must run before the gating scan, so 'the gate cannot fail' is "
        "reported as its own failure rather than hidden behind a scan result"
    )


def test_a_failed_control_still_fails_the_build(workflow: dict):
    """The property `continue-on-error` would otherwise destroy.

    "The gate cannot be proven able to fail" must never show a green check. The
    enforcement step is what preserves that, so its existence, its
    unconditionality and its non-zero exit are all asserted.
    """
    step = _enforcement_step(workflow)
    condition = str(step.get("if") or "")
    assert condition == "" or "cancelled()" in condition or "always()" in condition, (
        "the enforcement step must run whatever else failed — a plain success "
        "condition would skip it in exactly the case it exists for"
    )
    assert not step.get("continue-on-error"), (
        "the enforcement step is continue-on-error, so a failed control would show green"
    )
    run = step["run"]
    assert re.search(r"\bexit\s+1\b", run), (
        "the enforcement step never exits non-zero, so it cannot fail the build"
    )
    # Searching the whole block is not enough. The step branches, and only the
    # LAST branch is the coverage gap itself; an edit that turned just that
    # branch into `exit 0` would leave the earlier branches' `exit 1` in place
    # and keep a whole-block search green — a green check over a scan that
    # verified nothing, which is the exact regression this test exists to catch.
    assert run.strip().endswith("exit 1"), (
        "the enforcement step's final branch — the coverage gap — does not exit "
        "non-zero, so an unverified scan would show a green check"
    )


def test_findings_are_surfaced_even_when_the_control_fails(workflow: dict):
    """The 2026-08-18 lesson, pinned.

    When the control failed hard, the job stopped there: the unfiltered scan never
    ran, no annotation was written, and the artifact step uploaded nothing —
    "No files were found with the provided path". A blind scanner cost us the
    report as well as the gate, and the report is the half that would show
    detection coming back.
    """
    job = _scan_job(workflow)
    red_proof = _red_proof_step(workflow)
    assert red_proof.get("continue-on-error") is True, (
        "the control must be continue-on-error, or the steps after it never run "
        "and a blind scanner also costs us every finding it might still report"
    )
    for step in (_visibility_step(workflow), _gate_step(workflow)):
        index = _step_index(job, lambda s, t=step: s is t)
        assert index > _step_index(
            job, lambda s: "registry_scan_redprove.py" in s.get("run", "")
        ), "the scan steps must come after the control, not replace it"


def test_the_coverage_gap_is_labelled_distinctly_from_a_finding(workflow: dict):
    """A red check that means "we could not look" and a red check that means "we
    found something" are opposite situations, and for one day in August 2026 they
    wore the same badge. The annotation title is what a human — or the checks API
    a review agent reads — uses to tell them apart, so the enforcement step must
    carry one and must say it is not a finding."""
    run = _enforcement_step(workflow)["run"]
    assert "::error title=" in run, (
        "the coverage gap must be annotated, not just printed into the log"
    )
    lowered = run.lower()
    assert "coverage" in lowered and "not a finding" in lowered, (
        "the annotation must say this is a coverage gap and NOT a security finding"
    )


def test_a_real_finding_is_never_labelled_not_a_finding(workflow: dict):
    """The scenario the split itself can get wrong.

    The control failing and the gate finding a real critical are not exclusive —
    a blind scanner is exactly when a malicious change is most likely to be
    sitting in the tree, and the unfiltered pass behind the control can still
    surface one. If the enforcement step reads only the control's outcome, that
    run goes red carrying `NOT A FINDING` in capitals over a run that found
    something. The one case where a human most needs to look would wear the
    label that most discourages looking.

    So the enforcement step must consult the GATE's outcome too, and the gate
    needs an `id:` for it to be readable.
    """
    gate_id = _gate_step(workflow).get("id")
    assert gate_id, (
        "the gating scan step needs an `id:` so the enforcement step can tell "
        "'we could not look' apart from 'we looked and found something'"
    )
    run = _enforcement_step(workflow)["run"]
    assert f"steps.{gate_id}.outcome" in run, (
        "the enforcement step never reads the gate's outcome, so a run where the "
        "control failed AND a critical finding was reported is annotated "
        "'COVERAGE GAP — NOT A FINDING' over a real finding"
    )


def test_the_coverage_gap_is_not_claimed_when_the_gate_never_ran(workflow: dict):
    """A skipped gate is not a clean gate.

    The steps between the control and the gate are not all `continue-on-error`;
    if one of them fails, the gate is SKIPPED and its outcome is neither
    `success` nor `failure`. Falling through to the coverage-gap text there
    would publish a verified-cause narrative over a run whose gate never
    executed. The control's outcome already gets this guard; the gate's needs
    the same one.
    """
    gate_id = _gate_step(workflow).get("id")
    run = _enforcement_step(workflow)["run"]
    assert re.search(
        rf'steps\.{re.escape(gate_id)}\.outcome\s*\}}\}}"\s*!=\s*"success"', run
    ), (
        "the enforcement step has no branch for the gate not having run, so a "
        "skipped gate is reported as a plain coverage gap"
    )


def test_the_coverage_gap_is_not_claimed_on_a_cancelled_run(workflow: dict):
    """`always()` also means "and when someone cancelled the job".

    A cancelled run has verified nothing, but it has also not established that
    the scanner is blind — and the coverage-gap text asserts a specific verified
    cause. Claiming it on a cancellation invents a diagnosis.
    """
    condition = str(_enforcement_step(workflow).get("if") or "")
    assert "always()" not in condition, (
        "the enforcement step runs under always(), so a cancelled job emits the "
        "coverage-gap annotation and its verified-cause narrative"
    )
    assert "cancelled()" in condition, (
        "the enforcement step must still run after the gate fails, so it needs "
        "`!cancelled()` rather than a bare success condition"
    )


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


def test_fork_report_is_confined_to_pull_requests(workflow: dict):
    """`github.event.pull_request` is null on pushes, schedules, and dispatches, so a
    bare `head.repo.full_name != github.repository` is TRUE on every one of them. Without
    the event-name clause this job posts a green 'fork PR — no scan coverage' check
    beside a scan that actually ran — the misreading its name exists to prevent."""
    guard = workflow["jobs"]["fork-not-scanned"]["if"]
    assert "github.event_name == 'pull_request'" in guard, (
        "the fork coverage-gap report must be gated on the pull_request event, or it "
        "fires on pushes and scheduled runs too"
    )
    assert "github.event.pull_request.head.repo.full_name != github.repository" in guard


def test_scanner_coverage_limits_are_documented(workflow_text: str):
    """Registries run more than one scanner; a green check here is not a clean bill
    of health from all of them."""
    for scanner in ("Gen Agent Trust Hub", "Socket"):
        assert scanner in workflow_text, (
            f"the workflow must say that {scanner} is not covered by this gate"
        )


def _load_report_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("registry_scan_report", _REPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Scanner 0.6.0's real shape, taken from a live artifact: one top-level key whose value
# is a LIST of scanned paths, each carrying skills, each carrying risks keyed by name.
# A risk that did not fire is ABSENT: the scanner serialises with `exclude_none=True`.
# `ci-secure` below carries no `malicious_code` key at all, which is the real shape;
# `ci-speedup` carries an explicit null, which the reader tolerates but never sees live.
_SAMPLE_SCAN = {
    "scan_path_responses": [
        {
            "path": "skills",
            "skill_risks": [
                {
                    "name": "ci-speedup",
                    "risk_indexes": {
                        "third_party_content_exposure": {
                            "score": 300,
                            "evidence": "Exposure to untrusted third-party content",
                        },
                        "malicious_code": None,
                    },
                },
                {
                    "name": "ci-secure",
                    "risk_indexes": {
                        "suspicious_download_url": {
                            "score": 900,
                            "evidence": "Suspicious download URL in skill",
                        }
                    },
                },
            ],
        }
    ]
}


def test_reporter_annotates_warnings_and_leaves_criticals_to_the_gate(tmp_path, capsys):
    """Warnings become annotations; criticals do not.

    A critical already fails the build loudly in the gating step. Annotating it here too
    would double-report it, and an `::error::` annotation from a step that does not gate
    is how a reader learns to distrust the annotations.
    """
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(_SAMPLE_SCAN), encoding="utf-8")

    assert module.main(["report", str(findings)]) == 0
    out = capsys.readouterr().out

    assert "::warning title=third_party_content_exposure (warning) in ci-speedup::" in out
    assert "::warning title=suspicious_download_url" not in out, (
        "a blocking risk must not be reported as a warning")
    assert "::error" not in out, "the reporter does not gate, so it must not emit errors"

    # Both findings still appear in the human table — "does not annotate" is not "does not show".
    assert "third_party_content_exposure" in out and "suspicious_download_url" in out
    assert "ci-secure" in out


def test_reporter_states_the_rule_in_the_job_summary(tmp_path, monkeypatch, capsys):
    """The summary has to say what a green check means, or a reader infers 'clean'."""
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(_SAMPLE_SCAN), encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert module.main(["report", str(findings)]) == 0
    capsys.readouterr()

    body = summary.read_text(encoding="utf-8")
    assert "Non-blocking risks do not fail the build" in body
    assert "registry-scan-findings" in body, "the summary must point at the artifact"
    assert "`third_party_content_exposure`" in body and "`suspicious_download_url`" in body


def test_reporter_fails_loudly_on_unreadable_scan_output(tmp_path, capsys):
    """A reporter that silently produced nothing is indistinguishable from a clean scan."""
    module = _load_report_module()
    broken = tmp_path / "findings.json"
    broken.write_text("the scanner crashed before writing anything\n", encoding="utf-8")

    assert module.main(["report", str(broken)]) == 1
    assert "UNREADABLE" in capsys.readouterr().err


def test_reporter_survives_a_banner_before_the_json(tmp_path, capsys):
    """The scanner prints a version line before the document in some versions."""
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(
        "Snyk Agent Scan v0.5.16\n" + json.dumps(_SAMPLE_SCAN), encoding="utf-8"
    )

    assert module.main(["report", str(findings)]) == 0
    assert "::warning title=third_party_content_exposure" in capsys.readouterr().out


def test_reporter_reports_nothing_as_nothing(tmp_path, capsys):
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"scan_path_responses": []}), encoding="utf-8")

    assert module.main(["report", str(findings)]) == 0
    assert "No findings." in capsys.readouterr().out


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

    # Parse the host out rather than substring-matching the URL: the fixture must
    # stay under example.com, reserved by RFC 2606 and not resolvable, so the
    # red-proof can never point a reader (or an agent) at a live host.
    from urllib.parse import urlsplit

    urls = re.findall(r"https?://\S+", body)
    assert urls, "fixture no longer contains a download URL"
    for url in urls:
        host = urlsplit(url).hostname or ""
        assert host == "example.com" or host.endswith(".example.com"), (
            f"fixture URL host {host!r} is outside the reserved example.com domain"
        )

    # The assembled string must not appear verbatim in the script that assembles it —
    # asserted with the SHIPPED guard's own patterns rather than a hand-built substring,
    # which would silently stop matching if the fixture's flags ever changed.
    ioc_spec = importlib.util.spec_from_file_location(
        "test_no_ioc_shaped_literals", _REPO / "tests" / "test_no_ioc_shaped_literals.py")
    ioc = importlib.util.module_from_spec(ioc_spec)
    ioc_spec.loader.exec_module(ioc)
    source = _REDPROVE.read_text(encoding="utf-8")
    for name in ("_FETCH_AND_EXECUTE", "_SCRIPT_URL", "_URL"):
        assert not re.search(getattr(ioc, name), source), (
            f"the red-proof script now contains a literal matching the shipped IOC guard's "
            f"{name}; it is supposed to construct that string at runtime, never carry it"
        )


def test_a_scan_that_could_not_run_is_never_reported_as_a_finding(workflow: dict):
    """A crashed scanner and a real finding must not read the same.

    The gate uses `--ci`, where exit 1 means "a finding is present" and exit 2 means
    "I could not start" — a renamed flag, a missing token, an unparseable argument.
    While both surfaced as a bare non-zero, the verdict step read either as a finding,
    and this build spent five days announcing `The gate reported a critical finding in
    skills` over a scan that never happened. That is worse than a plain failure: it
    sends every reviewer hunting for a security issue that does not exist, and it hides
    the real news, which is that the repo has no working registry scanning at all.

    So the scanner's exit code must be captured by the pass that runs it, handed to
    the gate, and republished for the verdict step to branch on.
    """
    visibility = _visibility_step(workflow)
    run = visibility["run"]
    assert "scan_exit=" in run and "GITHUB_OUTPUT" in run, (
        "the unfiltered pass no longer publishes the scanner's exit code, so the gate "
        "cannot tell a scanner that could not start from one that wrote a clean document")
    scan_id = visibility.get("id")
    assert scan_id, "the unfiltered pass needs an `id:` so the gate can read its exit code"
    gate = _gate_step(workflow)
    assert f"steps.{scan_id}.outputs.scan_exit" in str(gate.get("env", {})), (
        "the gate is not handed the scanner's exit code (expected it in the gate step's "
        "`env:`, as SCANNER_EXIT)")
    assert "SCANNER_EXIT" in gate.get("env", {}), "the gate reads SCANNER_EXIT; the step does not set it"

    verdict = next(s for s in workflow["jobs"]["scan"]["steps"]
                   if s.get("name") == "Report the coverage gap")
    vrun = verdict["run"]
    assert "steps.gate.outputs.scan_exit" in vrun, (
        "the verdict step no longer reads the gate's exit code, so any non-zero exit "
        "is reported as a critical finding again")
    assert "DID NOT RUN — NOT A FINDING" in vrun, (
        "the verdict step lost the branch that names a failed-to-start scan as a "
        "coverage gap rather than a finding")


def test_the_scanner_version_is_pinned(workflow: dict):
    """`@latest` is why a vendor rename broke a green build with no commit of ours.

    The weekly cron exists to catch RULE-catalog drift — a rule renamed or added
    upstream that turns a shipped skill red on its own. It is not there to absorb
    breaking CLI changes, and it cannot: a usage error is not a finding, so the cron
    just goes red and stays red. Pinning makes the next scanner upgrade a deliberate
    commit that can be reviewed and reverted.
    """
    steps = [s for s in workflow["jobs"]["scan"]["steps"] if _invokes_scanner(s)]
    assert steps, "no step invokes the scanner"
    for step in steps:
        run = step["run"]
        assert "snyk-agent-scan@latest" not in run, (
            "the scanner is back on @latest — an upstream rename will redden this "
            "build again with no commit of ours")
        invocations = re.findall(r"uvx\s+snyk-agent-scan(\S*)\s+scan\b", run)
        assert invocations, "no scanner invocation found in a step that runs one"
        for spec in invocations:
            assert spec.startswith("=="), (
                f"the scanner invocation is not version-pinned on the command line "
                f"(found `uvx snyk-agent-scan{spec} scan`). A pin written in a comment "
                f"above an unpinned invocation is not a pin.")


def test_a_runtime_failure_is_not_reported_as_a_finding(workflow: dict):
    """Exit 1 is two different events, and only one of them is a finding.

    `--ci` returns 1 for a blocking risk AND for the scanner's own runtime failure,
    which this gate deliberately does not ignore (a scan that broke is not a scan that
    passed). A classifier that maps every exit 1 to "finding" leaves the false verdict
    reachable through a narrower door.

    The gate now reads the scanner's JSON, where a finding is a risk key and a runtime
    failure is an error object with `is_failure` — structure, not prose. The first
    version of the in-workflow classifier grepped the log for `E[0-9]{3}`, which
    0.6.0 never emits, so every real finding fell through to the coverage-gap branch
    and was announced as NOT A FINDING: a missed finding traded for a false alarm,
    the worse direction. A later one grepped for `risks found`, and scanned prose
    satisfied it. The executed tests further down pin each class; this pins that the
    classifier is structural.
    """
    source = _GATE.read_text(encoding="utf-8")
    code_lines = "\n".join(
        ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))
    assert "E[0-9]{3}" not in code_lines, (
        "the gate is back to grepping for E-codes, which scanner 0.6.0 never emits")
    for prose_signal in ("'risks found'", "'runtime failure codes", "\"risks found\""):
        assert prose_signal not in code_lines, (
            f"the gate keys on the scanner's printed prose ({prose_signal}); it must "
            f"classify on the JSON's structure")
    assert '"finding"' in code_lines and '"operational"' in code_lines

    verdict = next(s for s in workflow["jobs"]["scan"]["steps"]
                   if s.get("name") == "Report the coverage gap")
    assert "steps.gate.outputs.scan_class" in verdict["run"], (
        "the verdict step branches on the raw exit code again, so a runtime failure is "
        "reported as a security finding")


def test_an_unclassifiable_result_is_never_called_a_finding():
    """When the document reads clean but the scanner did not exit 0, the honest
    answer is 'unclassified'.

    Guessing 'critical finding' on a result this workflow cannot explain is exactly
    the failure it spent five days committing. An unclassified result still fails
    the build — it is not a pass — it just does not claim to be a security result.
    """
    source = _GATE.read_text(encoding="utf-8")
    assert '"indeterminate"' in source, (
        "the gate lost its unclassified branch, so a clean document from a scanner "
        "that did not exit 0 is read as clean")
    assert "UNCLASSIFIED" in source


def test_the_redprove_anchor_is_falsifiable_and_is_what_the_fixture_contains():
    """The control's anchor must be a string only a working scanner can produce.

    It was the vendor code `E005` until scanner 0.6.0 replaced issue codes with named
    risks, at which point the control reported a blind scanner while the scanner was
    demonstrably seeing — it returned `2 risks` on the same fixture. Anchoring on
    vendor vocabulary means a rename reads as a detection failure.

    The anchor is now the fixture's own malicious host, produced by the SAME function
    that builds the fixture, so the two cannot drift apart and no installer-host
    literal lands on disk (which this repo's IOC guard forbids, and which the
    red-proof module goes out of its way to avoid).

    Two ways this could rot into a meaningless green, both pinned: a trivial anchor
    that any output satisfies, and an anchor the fixture does not actually contain.
    """
    src = (_REPO / ".github" / "scripts" / "registry_scan_redprove.py").read_text()

    spec = importlib.util.spec_from_file_location(
        "_redprove", _REPO / ".github" / "scripts" / "registry_scan_redprove.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    anchor = mod.expected_evidence()

    assert len(anchor) >= 12, (
        f"the anchor is {anchor!r} — too short to be evidence of anything. A trivial "
        "anchor is satisfied by a scanner that says nothing, which is exactly the "
        "blind-scanner case this control exists to catch")

    with tempfile.TemporaryDirectory() as tmp:
        mod.build_violating_skill(pathlib.Path(tmp))
        fixture = (pathlib.Path(tmp) / "redprove-fixture" / "SKILL.md").read_text()
    assert anchor in fixture, (
        f"the anchor is {anchor!r}, which the fixture this script writes does not "
        "contain — so the scanner cannot echo it back and the control can never pass, "
        "however well the scanner is working")

    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code_lines if "E005" in ln], (
        "the red-proof still USES the retired E005 code, not just mentions it")


def _run_scan_step(workflow: dict, tmp_path, *, scanner_stdout: str, scanner_exit: int):
    """Execute the unfiltered pass's real shell against a stub scanner.

    The in-workflow gate shell used to have this harness; the gate is a Python script
    now, executed directly further down. What is left in shell is the pass that writes
    the document and publishes the scanner's exit code, and it has the same failure
    mode the old one did: the words can stay in the file while the behaviour goes.
    """
    run = _visibility_step(workflow)["run"]
    stub = tmp_path / "uvx"
    stub.write_text(
        f"#!/bin/sh\ncat <<'SCANOUT'\n{scanner_stdout}\nSCANOUT\nexit {scanner_exit}\n",
        encoding="utf-8")
    stub.chmod(0o755)
    out_file = tmp_path / "gh_output"
    out_file.touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", run],
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(out_file),
             "SCAN_PATH": "skills", "HOME": str(tmp_path)},
    )
    outputs = dict(
        line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_the_unfiltered_pass_publishes_the_exit_even_when_the_scanner_fails(workflow, tmp_path):
    """Under `set -e`, a failing scanner would end the step before the exit code was
    written, and the gate would read an empty SCANNER_EXIT as 0 — a crashed scanner
    handed to the gate as one that exited cleanly. Executed, with a stub that exits 2."""
    proc, outputs = _run_scan_step(workflow, tmp_path, scanner_stdout="usage error", scanner_exit=2)
    assert outputs.get("scan_exit") == "2", (proc.stdout, proc.stderr, outputs)
    assert proc.returncode == 2, "the step must still fail when the scanner did, so the log shows it"
    assert (tmp_path / "registry-scan-findings.json").read_text() == "usage error\n"


def test_the_unfiltered_pass_writes_the_document_and_exits_zero_on_success(workflow, tmp_path):
    proc, outputs = _run_scan_step(
        workflow, tmp_path, scanner_stdout='{"scan_path_responses": []}', scanner_exit=0)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("scan_exit") == "0"
    assert json.loads((tmp_path / "registry-scan-findings.json").read_text()) == {"scan_path_responses": []}


# ---------------------------------------------------------------------------
# Round-2 review: the properties that were still only grepped, or not checked at
# all. Each behavioural test below was written against the unfixed code and
# watched fail; each guard was proven by mutating the code it protects.
# ---------------------------------------------------------------------------


def _load_contract_module():
    spec = importlib.util.spec_from_file_location(
        "registry_scan_contract", _REPO / ".github" / "scripts" / "registry_scan_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_verdict_shell(workflow: dict, tmp_path, *, red_proof: str, gate: str,
                       scan_class: str = "", scan_exit: str = "", control_class: str = ""):
    """Execute the verdict step's real shell with the step-context values substituted.

    The gate step got an execution harness this round; this step — the one that
    actually prints `DID NOT RUN — NOT A FINDING` — did not, so every one of its five
    branches was pinned only by substring greps. Inverting its finding test to put the
    NOT A FINDING banner over a real critical left all tests green.
    """
    run = _enforcement_step(workflow)["run"]
    for expr, value in (
        ("steps.red_proof.outcome", red_proof),
        ("steps.gate.outcome", gate),
        ("steps.gate.outputs.scan_class", scan_class),
        ("steps.gate.outputs.scan_exit", scan_exit),
        ("steps.red_proof.outputs.control_class", control_class),
    ):
        run = re.sub(r"\$\{\{\s*" + re.escape(expr) + r"\s*\}\}", value, run)
    assert "${{" not in run, f"unsubstituted step expression left in the verdict shell: {run}"
    summary = tmp_path / "step_summary"
    summary.touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", run],
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "SCAN_PATH": "skills",
             "GITHUB_STEP_SUMMARY": str(summary), "HOME": str(tmp_path)},
    )
    return proc, summary.read_text(encoding="utf-8")


def test_the_verdict_shell_never_labels_a_real_finding_not_a_finding(workflow, tmp_path):
    """Executed, not grepped. This is the bug the whole PR exists to remove."""
    proc, summary = _run_verdict_shell(
        workflow, tmp_path, red_proof="failure", gate="failure", scan_class="finding", scan_exit="1")
    assert "NOT A FINDING" not in proc.stdout + summary, (
        "a run whose gate reported a blocking risk was announced as NOT A FINDING")
    assert proc.returncode == 1


def test_the_verdict_shell_reports_a_gate_that_never_ran(workflow, tmp_path):
    """A passing control must not certify a run whose gate was skipped.

    `Surface findings` is not continue-on-error, so unreadable scan output fails it and
    leaves the gate unrun. The verdict step's own comment describes exactly this case —
    and its early `exit 0` on a passing control made that branch unreachable, so the run
    was announced "meaningful" with nothing gated.
    """
    proc, _ = _run_verdict_shell(workflow, tmp_path, red_proof="success", gate="skipped")
    assert proc.returncode == 1, (
        f"a skipped gate was certified as a meaningful run: {proc.stdout!r}")
    assert "DID NOT RUN" in proc.stdout


def test_the_verdict_shell_certifies_a_healthy_run(workflow, tmp_path):
    proc, _ = _run_verdict_shell(
        workflow, tmp_path, red_proof="success", gate="success", scan_class="clean", scan_exit="0")
    assert proc.returncode == 0


def test_the_verdict_shell_reports_the_coverage_gap_on_a_blind_scanner(workflow, tmp_path):
    proc, summary = _run_verdict_shell(
        workflow, tmp_path, red_proof="failure", gate="success", scan_class="clean", scan_exit="0")
    assert proc.returncode == 1
    assert "COVERAGE GAP" in proc.stdout and "NOT A FINDING" in proc.stdout
    # The summary used to assert the 2026-08-19 diagnosis ("not the free-tier daily
    # cap") as a present-tense fact. The cap is its own class now, so the blind-scanner
    # text must say the cap was NOT reported on this run, not that it cannot be the cause.
    assert "quota" in summary.lower(), (
        "the blind-scanner summary does not say how it differs from the quota case")
    assert "(no 429" not in summary, (
        "the summary still asserts a 2026-08-19 measurement as a fact about this run")


def test_the_verdict_shell_names_the_quota_when_the_scan_hit_the_cap(workflow, tmp_path):
    """A control that failed on the cap is not a blind scanner, and the scan behind it
    verified nothing for the same reason. The verdict must say QUOTA — not COVERAGE
    GAP with the blind-scanner narrative, not DID NOT RUN, and never FINDING."""
    proc, summary = _run_verdict_shell(
        workflow, tmp_path, red_proof="failure", gate="failure", scan_class="quota",
        scan_exit="0", control_class="quota")
    assert proc.returncode == 1
    text = proc.stdout + summary
    assert "QUOTA EXHAUSTED" in text and "NOT A FINDING" in text
    assert "REGISTRY SCAN FINDING" not in text
    assert "came back clean" not in text, "the blind-scanner narrative was printed over a quota failure"
    assert "reset" in text.lower(), "the quota verdict must say the cap resets"


def test_the_verdict_shell_names_the_quota_when_only_the_control_hit_the_cap(workflow, tmp_path):
    """The control runs first; if the cap fell between it and the scan, the scan's
    clean document is unverified — but the reason is the cap, not blindness."""
    proc, _ = _run_verdict_shell(
        workflow, tmp_path, red_proof="failure", gate="success", scan_class="clean",
        scan_exit="0", control_class="quota")
    assert proc.returncode == 1
    assert "QUOTA EXHAUSTED" in proc.stdout
    assert "COVERAGE GAP — NOT A FINDING" not in proc.stdout


def test_the_gate_fails_when_the_contract_cannot_be_imported(tmp_path):
    """The old shell built the exemption list with `--ignore-risks "$(python ...)"`,
    which swallowed the shim's failure: a traceback became an empty argument, the
    scanner exempted nothing, and the owner's ruling was silently off. The gate now
    imports the contract; if that import fails it must fail the step with no class
    written — never fall through to a verdict under an unknown policy."""
    orphan = tmp_path / "registry_scan_gate.py"
    orphan.write_text(_GATE.read_text(encoding="utf-8"), encoding="utf-8")
    findings = tmp_path / "registry-scan-findings.json"
    findings.write_text(json.dumps({"scan_path_responses": []}), encoding="utf-8")
    out_file = tmp_path / "gh_output"
    out_file.touch()
    proc = subprocess.run(
        [sys.executable, str(orphan), str(findings)], cwd=tmp_path, capture_output=True,
        text=True, env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out_file),
                        "HOME": str(tmp_path)})
    assert proc.returncode != 0, "the gate ran without its contract instead of failing"
    assert "scan_class=clean" not in out_file.read_text()


def test_no_blocking_risk_is_ever_exempt():
    """A blocking risk reaching the exemption list is a disarmed gate reporting green.

    The runtime red-proof only anchors on the fixture's own risk, so widening the list to
    swallow malicious code or secret detection is invisible to it. This is the offline
    half, and it is the half that runs in the required `test` check.
    """
    contract = _load_contract_module()
    assert contract.NON_BLOCKING_RISKS == ("third_party_content_exposure",), (
        "the non-blocking list changed; exactly one risk is exempt by owner ruling "
        "(2026-08-10, restated for 0.6.0 2026-08-26)")
    for risk in ("suspicious_download_url", "malicious_code", "secret_detection",
                 "prompt_injection_skill_instructions", "insecure_credential_handling",
                 "unverifiable_dependencies", "direct_money_access",
                 "modifying_system_services", "missing_skill_md"):
        assert risk in contract.BLOCKING_RISKS, f"{risk} is no longer a blocking risk"


def test_the_risk_vocabulary_matches_the_scanners_own_model():
    """Pinned so a catalog change arrives as a conscious edit, not a silent exemption.

    Verbatim from `SkillRiskIndexes` / `McpServerRiskIndexes` in the scanner's
    `agent_scan/models/api/v20260710.py` at the pinned version.
    """
    contract = _load_contract_module()
    assert contract.SKILL_RISKS == (
        "prompt_injection_skill_instructions", "suspicious_download_url", "malicious_code",
        "insecure_credential_handling", "secret_detection", "direct_money_access",
        "third_party_content_exposure", "unverifiable_dependencies",
        "modifying_system_services", "missing_skill_md")
    assert contract.SERVER_RISKS == (
        "dangerous_words", "prompt_injection_tool_desc", "untrusted_content",
        "private_data", "destructive_capabilities")


def test_the_exemption_list_reaches_the_scanner_nowhere_but_the_red_proof():
    """The policy has exactly one home, and after the gate went offline exactly one
    consumer that turns it into a scanner argument: the red-proof, which must run the
    gate's real list so an exemption grown to swallow the anchor fails there. A shim
    that printed the list for the workflow used to exist; nothing reads it now, and a
    second copy of the list is a second place for it to drift."""
    assert not (_REPO / ".github" / "scripts" / "registry_scan_ignored.py").exists(), (
        "registry_scan_ignored.py is back; nothing in the workflow consumes it")
    assert "registry_scan_ignored" not in _WORKFLOW.read_text(encoding="utf-8")


def test_an_unrecognised_payload_shape_is_never_reported_as_clean(tmp_path, capsys):
    """The reporter returning `[]` on a renamed shape is bug #3, left undefended.

    0.5.x's shape produced exactly this: valid JSON, an empty finding list, "No
    findings." on every run, and the UNREADABLE guard never firing. The next rename must
    be loud.
    """
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"results": {"skills": {"issues": []}}}), encoding="utf-8")
    assert module.main(["report", str(findings)]) == 1
    assert "UNREADABLE" in capsys.readouterr().err


def test_a_server_risk_is_surfaced_like_a_skill_risk(tmp_path, capsys):
    """The scanner's `--ci` exit weighs `server_risks` as well as `skill_risks`.

    A server risk therefore fails the gate while the reporter printed "No findings." —
    the same silent-surfacing failure this gate exists to remove, from the other side.
    """
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"scan_path_responses": [{
        "path": "skills",
        "server_risks": [{"name": "some-mcp", "risk_indexes": {
            "untrusted_content": {"score": 400, "evidence": "Reads untrusted content"}}}],
        "skill_risks": [],
    }]}), encoding="utf-8")
    assert module.main(["report", str(findings)]) == 0
    out = capsys.readouterr().out
    assert "untrusted_content" in out and "No findings." not in out


def test_a_scan_error_is_surfaced_rather_than_read_as_clean(tmp_path, capsys):
    """A skill the scanner could not analyse is not a skill that came back clean."""
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"scan_path_responses": [{
        "path": "skills",
        "skill_risks": [{"name": "ci-secure", "risk_indexes": {},
                         "error": {"code": "X002", "message": "skill scan failed"}}],
    }]}), encoding="utf-8")
    assert module.main(["report", str(findings)]) == 0
    out = capsys.readouterr().out
    assert "No findings." not in out, "a skill that failed to scan was reported as clean"
    assert "ci-secure" in out


def test_a_risk_the_contract_does_not_know_is_announced(tmp_path, capsys):
    """The contract's comment promised this and nothing implemented it.

    A risk name the vocabulary does not carry still blocks (it is not in the exemption
    list), but nobody was told the catalog had moved — so the ten-name list, checked by
    hand against the scanner's model, had no effect on anything.
    """
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"scan_path_responses": [{
        "path": "skills",
        "skill_risks": [{"name": "ci-secure", "risk_indexes": {
            "brand_new_risk_2027": {"score": 700, "evidence": "something new"}}}],
    }]}), encoding="utf-8")
    assert module.main(["report", str(findings)]) == 0
    out = capsys.readouterr().out
    assert "brand_new_risk_2027" in out
    assert "UNKNOWN" in out.upper(), (
        "a risk name outside the pinned vocabulary was surfaced as if it were known")


def _run_redprove(tmp_path, *, scanner_output: str, scanner_exit: int):
    """Drive the red-proof offline with a stub scanner on PATH.

    `main()` was executed by nothing, so both of its failure conditions could be disabled
    with the suite green — including the blind-scanner anchor its own comment says must
    never be weakened.
    """
    stub = tmp_path / "uvx"
    stub.write_text(f"#!/bin/sh\ncat <<'OUT'\n{scanner_output}\nOUT\nexit {scanner_exit}\n",
                    encoding="utf-8")
    stub.chmod(0o755)
    return subprocess.run(
        [sys.executable, str(_REDPROVE)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SNYK_TOKEN": "stub"})


def test_the_red_proof_fails_when_the_scanner_says_nothing(tmp_path):
    proc = _run_redprove(tmp_path, scanner_output="Scan complete. No risks.", scanner_exit=0)
    assert proc.returncode == 1, "a blind scanner passed the control"
    assert "NOT PROVEN" in proc.stdout + proc.stderr


def test_the_red_proof_fails_when_the_scanner_exits_zero_on_the_fixture(tmp_path):
    """Isolated from the anchor check: the scanner SAW the fixture and still exited 0.

    A control that only asserts the anchor would pass this, and a scanner that reports a
    risk without failing on it is a gate that cannot go red.
    """
    spec = importlib.util.spec_from_file_location("registry_scan_redprove", _REDPROVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proc = _run_redprove(
        tmp_path,
        scanner_output=f"1 risk: Unverifiable URLs: {module.expected_evidence()}/install.sh",
        scanner_exit=0)
    assert proc.returncode == 1, "the scanner exited 0 on the violating fixture and the control passed"
    assert "exited 0" in proc.stdout + proc.stderr


def test_the_red_proof_fails_when_the_anchor_host_is_absent(tmp_path):
    """Non-zero exit alone is not proof the scanner saw OUR fixture."""
    proc = _run_redprove(
        tmp_path, scanner_output="CI (--ci): exiting with code 1 (risks found).", scanner_exit=1)
    assert proc.returncode == 1, "the control passed without the scanner echoing the fixture host"
    assert "NOT PROVEN" in proc.stdout + proc.stderr


def test_the_red_proof_passes_only_on_a_scanner_that_saw_the_fixture(tmp_path):
    spec = importlib.util.spec_from_file_location("registry_scan_redprove", _REDPROVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proc = _run_redprove(
        tmp_path,
        scanner_output=f"1 risk: Unverifiable URLs: {module.expected_evidence()}/install.sh\n"
                       "CI (--ci): exiting with code 1 (risks found).",
        scanner_exit=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_red_proof_runs_the_gates_own_exemption_list(tmp_path):
    """Documented as load-bearing and guarded by nothing: deleting the two lines that
    append `--ignore-risks` left every test green, and with them gone an exemption grown
    to swallow the anchor would no longer fail here."""
    proc = _run_redprove(tmp_path, scanner_output="Scan complete.", scanner_exit=0)
    contract = _load_contract_module()
    assert f"--ignore-risks {','.join(contract.NON_BLOCKING_RISKS)}" in proc.stdout, (
        "the red-proof no longer runs the gate's real exemption list")


def test_every_coverage_gap_names_the_scanner_pin(workflow: dict):
    """A red that is not a finding should point at the pin before the tree.

    The 2026-08-19 outage cost a week because the failure said "critical finding in
    skills" and nobody thought to check whether the vendor had moved. Every message
    that means "this check did not verify anything" now ends by naming the pinned
    version and suggesting it as the first suspect — so the next person does not need
    to have read this history.
    """
    steps = workflow["jobs"]["scan"]["steps"]
    gap_markers = ("DID NOT RUN", "DID NOT COMPLETE", "EXEMPTION IS STALE", "UNCLASSIFIED")
    # SNYK_TOKEN and HAS NOTHING TO SCAN are excluded deliberately: neither can be
    # caused by the scanner version, and a hint that fires on every gap regardless of
    # cause is noise that trains people to skip it. The daily cap (QUOTA EXHAUSTED) is
    # excluded the same way: its cause is known and named in the message.
    not_version_related = ("SNYK_TOKEN is not set", "HAS NOTHING TO SCAN")
    gap_lines = [
        line
        for step in steps
        for line in (step.get("run") or "").splitlines()
        if "::error title=" in line
        and any(m in line for m in gap_markers)
        and not any(x in line for x in not_version_related)
    ]
    # The gate's own messages moved into registry_scan_gate.py; the executed test
    # below covers those. What is left in shell is the verdict step's three.
    assert len(gap_lines) >= 3, f"expected several coverage-gap messages, found {len(gap_lines)}"
    missing = [ln for ln in gap_lines if "STALE_PIN_HINT" not in ln]
    assert not missing, (
        "coverage-gap message(s) do not name the scanner pin as a suspect:\n  "
        + "\n  ".join(m.strip()[:120] for m in missing))


def test_every_gate_coverage_gap_names_the_scanner_pin_and_the_quota_does_not(tmp_path):
    """Same property, for the classes the gate script decides — executed, since the
    messages are assembled at runtime. The quota message deliberately does NOT carry
    the hint: its cause is known, and a hint that fires regardless of cause is noise."""
    pin = _load_contract_module().PINNED_SCANNER
    version_related = {
        "did-not-run": {"missing": True, "scanner_exit": "2"},
        "operational": {"payload": _payload(path_error=_analysis_error("Unauthorized."))},
        "indeterminate": {"payload": _payload([_skill("ci-secure")]), "scanner_exit": "1"},
    }
    for expected, kwargs in version_related.items():
        proc, outputs = _run_gate(tmp_path, **kwargs)
        assert outputs.get("scan_class") == expected, (expected, proc.stdout)
        assert f"snyk-agent-scan=={pin}" in proc.stdout, f"{expected} does not name the pin"
    proc, outputs = _run_gate(tmp_path, _payload(path_error=_analysis_error(_QUOTA_MESSAGE)))
    assert outputs.get("scan_class") == "quota"
    assert f"snyk-agent-scan=={pin}" not in proc.stdout, (
        "the quota message names the pin as a suspect; the cap is not a version problem")


def test_the_pin_is_stated_once_and_matches_everywhere(workflow: dict):
    """Two places invoke the scanner; a half-bumped pin is two contracts at once.

    The workflow's unfiltered pass and the red-proof must run the same version, and
    the contract's PINNED_SCANNER — which the gate's messages name — must agree,
    otherwise the hint names a version the scan is not running, which is worse than
    no hint.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_contract", _REPO / ".github" / "scripts" / "registry_scan_contract.py")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    pin = contract.PINNED_SCANNER

    text = (_REPO / ".github" / "workflows" / "registry-scan.yml").read_text()
    text += (_REPO / ".github" / "scripts" / "registry_scan_redprove.py").read_text()
    found = set(re.findall(r"snyk-agent-scan==([0-9][0-9.]*)", text))
    assert found == {pin}, (
        f"scanner is invoked at {sorted(found)} but the contract pins {pin!r} — a "
        "half-bumped pin runs two different contracts in one job")
    assert pin in contract.STALE_PIN_HINT, "the hint no longer names the pinned version"


# ---------------------------------------------------------------------------
# The offline gate. One scanner call per run: the unfiltered `--json` pass is the
# only network call over `skills/`, and `registry_scan_gate.py` derives the verdict
# from that document instead of asking the scanner a second time under `--ci`.
# Every branch below is executed against a fixture payload, not grepped.
# ---------------------------------------------------------------------------

_GATE = _REPO / ".github" / "scripts" / "registry_scan_gate.py"

# The scanner's real 429 text, verbatim from `agent_scan/verify_api.py` at the pin.
_QUOTA_MESSAGE = (
    "Daily usage limit reached for the public version of Agent-Scan. Unlock higher "
    "limits and enterprise features by contacting us at https://evo.ai.snyk.io/#contact-us."
)


def _payload(skills=None, *, path_error=None):
    """A 0.6.0 `--json` document. Errors serialise with `category`, never a code."""
    response = {"path": "skills", "skill_risks": skills or []}
    if path_error is not None:
        response["error"] = path_error
    return {"scan_path_responses": [response]}


def _skill(name, **risks):
    return {"name": name, "risk_indexes": {
        risk: {"score": 700, "evidence": f"{risk} evidence"} for risk in risks}}


def _analysis_error(message):
    """What `_analysis_error_response` writes for every path when the endpoint fails."""
    return {"message": message, "exception": f"429, message='{message}'",
            "is_failure": True, "category": "analysis_error"}


def _run_gate(tmp_path, payload=None, *, raw=None, scanner_exit="0", missing=False):
    """Execute the gate script the way the workflow does, and read back its outputs."""
    findings = tmp_path / "registry-scan-findings.json"
    if not missing:
        findings.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    out_file = tmp_path / "gh_output"
    out_file.touch()
    proc = subprocess.run(
        [sys.executable, str(_GATE), str(findings)],
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out_file),
             "SCAN_PATH": "skills", "SCANNER_EXIT": scanner_exit, "HOME": str(tmp_path)},
    )
    outputs = dict(
        line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_the_gate_classifies_a_blocking_risk_as_a_finding(tmp_path):
    """The whole point, executed: a non-exempt risk in the JSON fails the build as a
    FINDING, with a titled annotation naming the risk, and never says NOT A FINDING."""
    proc, outputs = _run_gate(tmp_path, _payload([_skill("ci-secure", suspicious_download_url=1)]))
    assert outputs.get("scan_class") == "finding", (proc.stdout, proc.stderr)
    assert proc.returncode == 1
    assert "::error title=REGISTRY SCAN FINDING" in proc.stdout
    assert "suspicious_download_url" in proc.stdout and "ci-secure" in proc.stdout
    assert "NOT A FINDING" not in proc.stdout + proc.stderr


def test_the_gate_passes_a_scan_carrying_only_exempt_risks(tmp_path):
    """The owner's ruling, applied offline: the one exempt risk never blocks."""
    proc, outputs = _run_gate(
        tmp_path, _payload([_skill("ci-speedup", third_party_content_exposure=1)]))
    assert outputs.get("scan_class") == "clean", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


def test_the_gate_passes_an_empty_scan(tmp_path):
    proc, outputs = _run_gate(tmp_path, _payload([_skill("ci-secure")]))
    assert outputs.get("scan_class") == "clean", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


def test_the_gate_names_the_daily_cap_as_quota_and_still_fails(tmp_path):
    """The 2026-09-14 state: HTTP 429 from the analysis endpoint, serialised as an
    `analysis_error` (X007) whose message is the scanner's daily-cap text.

    It is its own class, with its own title, because it is neither a finding nor a
    broken scanner — and it still fails the job, because nothing was verified.
    """
    proc, outputs = _run_gate(tmp_path, _payload(path_error=_analysis_error(_QUOTA_MESSAGE)))
    assert outputs.get("scan_class") == "quota", (proc.stdout, proc.stderr)
    assert proc.returncode == 1, "a scan the cap prevented must not be green"
    assert "::error title=REGISTRY SCAN QUOTA EXHAUSTED — NOT A FINDING::" in proc.stdout
    lowered = proc.stdout.lower()
    assert "daily" in lowered and "reset" in lowered, "the annotation must say the cap resets daily"
    assert "DID NOT COMPLETE" not in proc.stdout, "the cap must not be reported as a broken scanner"


def test_an_analysis_error_that_is_not_the_cap_is_operational(tmp_path):
    """X007 is `analysis_error`, which the scanner also uses for 401, 413, 5xx and
    timeouts. Only the 429 text means quota; the rest is DID NOT COMPLETE."""
    proc, outputs = _run_gate(tmp_path, _payload(path_error=_analysis_error(
        "Unauthorized. Please check your SNYK_TOKEN environment variable or your push key.")))
    assert outputs.get("scan_class") == "operational", (proc.stdout, proc.stderr)
    assert proc.returncode == 1
    assert "::error title=REGISTRY SCAN DID NOT COMPLETE::" in proc.stdout
    assert "X007" in proc.stdout, "the scanner's code must be named, as its own exit line would"
    assert "QUOTA" not in proc.stdout


def test_a_skill_scan_error_is_operational_and_carries_its_code(tmp_path):
    """A serialised error has a `category`, not a code; the gate mints the code the
    scanner's own `--ci` exit line would have printed (`skill_scan_error` -> X002)."""
    proc, outputs = _run_gate(tmp_path, _payload([{
        "name": "ci-secure", "risk_indexes": {},
        "error": {"message": "skill scan failed", "is_failure": True,
                  "category": "skill_scan_error"}}]))
    assert outputs.get("scan_class") == "operational", (proc.stdout, proc.stderr)
    assert "X002" in proc.stdout
    assert proc.returncode == 1


def test_an_informational_error_does_not_fail_the_gate(tmp_path):
    """The scanner's `--ci` weighs only errors with `is_failure`; a `file_not_found`
    is informational there, so it is informational here — the gate must agree with
    the one it replaced, in both directions."""
    proc, outputs = _run_gate(tmp_path, _payload(path_error={
        "message": "no config here", "is_failure": False, "category": "file_not_found"}))
    assert outputs.get("scan_class") == "clean", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


def test_the_gate_reports_a_missing_document_as_did_not_run(tmp_path):
    proc, outputs = _run_gate(tmp_path, missing=True, scanner_exit="2")
    assert outputs.get("scan_class") == "did-not-run", (proc.stdout, proc.stderr)
    assert proc.returncode == 1
    assert "::error title=REGISTRY SCAN DID NOT RUN::" in proc.stdout
    assert "exited 2" in proc.stdout, "a scanner that could not start must be reported with its exit"


def test_the_gate_reports_unparseable_output_as_did_not_run(tmp_path):
    proc, outputs = _run_gate(tmp_path, raw="the scanner crashed before writing anything\n")
    assert outputs.get("scan_class") == "did-not-run", (proc.stdout, proc.stderr)
    assert proc.returncode == 1


def test_the_gate_never_reads_an_unrecognised_shape_as_clean(tmp_path):
    """Valid JSON in a shape the contract cannot read is the 0.5.x failure: it parsed,
    yielded nothing, and was read as clean for a week."""
    proc, outputs = _run_gate(tmp_path, {"results": {"skills": {"issues": []}}})
    assert outputs.get("scan_class") == "did-not-run", (proc.stdout, proc.stderr)
    assert proc.returncode == 1


def test_an_unknown_risk_name_is_a_finding(tmp_path):
    """A risk outside the pinned vocabulary is not exempt, so it blocks — and the
    annotation says the catalog moved, so the red is read as both."""
    proc, outputs = _run_gate(tmp_path, _payload([_skill("ci-secure", brand_new_risk_2027=1)]))
    assert outputs.get("scan_class") == "finding", (proc.stdout, proc.stderr)
    assert proc.returncode == 1
    assert "brand_new_risk_2027" in proc.stdout
    assert "vocabulary" in proc.stdout.lower() or "catalog" in proc.stdout.lower()


def test_a_real_finding_beside_a_quota_error_is_still_a_finding(tmp_path):
    """The two are not exclusive in the document, and the finding must win: putting
    NOT A FINDING over a run that found something is the label that most
    discourages the one look a human most needs to take."""
    proc, outputs = _run_gate(tmp_path, _payload(
        [_skill("ci-secure", malicious_code=1)], path_error=_analysis_error(_QUOTA_MESSAGE)))
    assert outputs.get("scan_class") == "finding", (proc.stdout, proc.stderr)
    assert "NOT A FINDING" not in proc.stdout + proc.stderr


def test_the_gate_classifies_on_structure_not_on_prose(tmp_path):
    """The evidence text of a scanned skill is untrusted prose. A CI-security skill's
    own evidence can say `risks found`, `X007` or the cap message; none of it is a
    signal. The old shell grepped the log and once let scanned prose classify a run."""
    prose = f"Reports risks found; mentions X007 and says: {_QUOTA_MESSAGE}"
    proc, outputs = _run_gate(tmp_path, _payload([{
        "name": "ci-secure", "risk_indexes": {
            "third_party_content_exposure": {"score": 300, "evidence": prose}}}]))
    assert outputs.get("scan_class") == "clean", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


def test_a_clean_document_from_a_scanner_that_did_not_exit_zero_is_unclassified(tmp_path):
    """A scan that broke is not a scan that passed. If the document reads clean but
    the scanner did not exit 0, the honest answer is 'cannot say' — it fails the
    build without claiming to be a security result."""
    proc, outputs = _run_gate(tmp_path, _payload([_skill("ci-secure")]), scanner_exit="1")
    assert outputs.get("scan_class") == "indeterminate", (proc.stdout, proc.stderr)
    assert proc.returncode == 1
    assert "::error title=REGISTRY SCAN RESULT UNCLASSIFIED::" in proc.stdout


def test_the_gate_publishes_the_scanner_exit_for_the_verdict_step(tmp_path):
    """The verdict annotation renders `exit <n>, class <c>`; both keys must be written
    on every path, including the ones that never read the document."""
    for kwargs, exit_code in (
        ({"payload": _payload([_skill("ci-secure")])}, "0"),
        ({"missing": True}, "2"),
    ):
        _, outputs = _run_gate(tmp_path, scanner_exit=exit_code, **kwargs)
        assert outputs.get("scan_exit") == exit_code, outputs
        assert outputs.get("scan_class"), outputs


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("registry_scan_gate", _GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_exemption_the_vocabulary_does_not_carry_is_stale(tmp_path, monkeypatch, capsys):
    """The scanner used to drop a retired exemption name with a yellow warning and
    exit 0 — the silent way the 2026-08 outage hid. Offline, the same drift is a
    NON_BLOCKING name outside the pinned vocabulary, and the gate refuses to run
    under it rather than scanning under a policy nobody chose."""
    module = _load_gate_module()
    monkeypatch.setattr(module, "NON_BLOCKING_RISKS", ("third_party_content_exposure_v2",))
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(_payload([_skill("ci-secure")])), encoding="utf-8")
    out_file = tmp_path / "gh_output"
    out_file.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    assert module.main(["gate", str(findings)]) == 1
    assert "scan_class=exemption-stale" in out_file.read_text()
    assert "EXEMPTION IS STALE" in capsys.readouterr().out


def test_the_gate_reads_its_exemption_list_from_the_shared_contract():
    """The gate must not restate the ruling inline. An inline list would exempt the
    very risk the red-proof anchors on while the red-proof — which reads the
    contract — stayed green. Drift between the two is the single thing
    `registry_scan_contract.py` exists to prevent."""
    source = _GATE.read_text(encoding="utf-8")
    assert re.search(r"from registry_scan_contract import \(?[^)]*\bNON_BLOCKING_RISKS\b", source), (
        "the gate does not import NON_BLOCKING_RISKS from the shared contract")
    contract = _load_contract_module()
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    for risk in contract.SKILL_RISKS + contract.SERVER_RISKS:
        assert not any(risk in ln for ln in code_lines), (
            f"the gate names the risk {risk!r} inline; the ruling has exactly one home, "
            f"in registry_scan_contract.py")


def test_a_scan_error_row_carries_the_code_the_scanner_would_print():
    """`ScanError` serialises a `category`, never a code. The contract must mint the
    same X-code the CLI's exit line would, or the reporter's table and the gate's
    annotation name `scan_error:unknown` on every real payload."""
    contract = _load_contract_module()
    rows = contract.iter_findings(_payload(path_error=_analysis_error(_QUOTA_MESSAGE)))
    assert [r["risk"] for r in rows] == ["scan_error:X007"], rows
    assert rows[0]["quota"] is True
    assert contract.FAILURE_CATEGORY_TO_CODE["skill_scan_error"] == "X002"
