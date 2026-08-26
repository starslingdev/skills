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
    """The authoritative step: the scanner invocation carrying the ignore list.

    Every property below is asserted against THIS step specifically. Asserting them
    against the job's concatenated step text is what let the earlier version of this
    file stay green while `--ci` moved onto the advisory pass.
    """
    steps = [
        step for step in _scan_job(workflow)["steps"]
        if _invokes_scanner(step) and "--ignore-risks" in step["run"]
    ]
    assert len(steps) == 1, (
        f"expected exactly one gating scanner invocation (the one passing the ignore "
        f"list); found {len(steps)}"
    )
    return steps[0]


def _visibility_step(workflow: dict) -> dict:
    """The unfiltered pass: a scanner invocation with no ignore list, distinct from the gate."""
    steps = [
        step for step in _scan_job(workflow)["steps"]
        if _invokes_scanner(step) and "--ignore-risks" not in step["run"]
    ]
    assert len(steps) == 1, (
        f"expected exactly one unfiltered scanner invocation; found {len(steps)}. The "
        f"scanner strips ignored findings from its printed report as well as its exit "
        f"status, so without a separate unfiltered pass an accepted finding is invisible."
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
    assert _scan_job(workflow)["runs-on"] == ci["jobs"]["test-self"]["runs-on"]


def test_fork_prs_never_reach_the_self_hosted_runner(workflow: dict):
    """Fork code must not execute on self-hosted runners (the ci.yml contract)."""
    guard = _scan_job(workflow)["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in guard


def test_gate_uses_ci_flag(workflow: dict):
    """Without --ci a finding is printed and the build stays green.

    Asserted against the gating step itself. Checking the job's concatenated step text
    would be satisfied by `--ci` appearing in an echo, or by it moving onto the advisory
    visibility pass — both of which make every finding non-blocking.
    """
    gate = _gate_step(workflow)
    assert "--ci" in gate["run"], "the gating scan lost --ci; findings are now advisory"
    assert "--dangerously-run-mcp-servers" in gate["run"], (
        "the scanner refuses --ci without --dangerously-run-mcp-servers (exit 2)"
    )
    _assert_unconditional(gate, "the gating scan")


def test_the_gating_pass_logs_what_it_did(workflow: dict):
    """`--verbose` on the gating pass, so gate-output.txt records the run.

    Not for the reason an earlier revision of this test gave: in 0.6.0 `--verbose` only
    raises the logging level (`setup_logging`), and the printer neither strips codes nor
    mutates the response. It is pinned because the gate CLASSIFIES on that captured
    output, and a silent run gives the classifier nothing to read.
    """
    assert "--verbose" in _gate_step(workflow)["run"], (
        "the gating pass lost `--verbose`, so the output the classifier greps is thinner "
        "than the one those branches were written against"
    )


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
    its exit status, so the gate runs an unfiltered pass first. If that pass ever
    grows an ignore list, accepted findings stop appearing in the log entirely."""
    visibility = _visibility_step(workflow)
    gate = _gate_step(workflow)
    assert visibility is not gate, (
        "the unfiltered pass and the gating pass are the same step; accepted findings "
        "would never be printed anywhere"
    )
    # The visibility pass is advisory on purpose — the gate below is what fails the build —
    # but only the visibility pass may be advisory.
    assert visibility.get("continue-on-error") is True, (
        "the unfiltered pass must be advisory; its job is to print, not to gate"
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

    So the gate must capture the exit code and the verdict must branch on it.
    """
    gate = _gate_step(workflow)
    run = gate["run"]
    assert "scan_exit=" in run and "GITHUB_OUTPUT" in run, (
        "the gate no longer publishes the scanner's exit code, so the verdict step "
        "cannot tell a crash from a finding")
    assert 'code}" -eq 1 ' in run or "code}\" -eq 1" in run, (
        "the gate no longer treats exit 1 specifically as the finding case")

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

    It must key on what 0.6.0 actually prints — `risks found` versus `runtime failure
    codes:` — not on E-codes. The first version of this fix grepped for `E[0-9]{3}`,
    which 0.6.0 never emits, so every real finding fell through to the coverage-gap
    branch and was announced as NOT A FINDING: a missed finding traded for a false
    alarm, the worse direction.
    """
    run = _gate_step(workflow)["run"]
    assert "risks found" in run, (
        "the gate no longer recognises 0.6.0's finding signal, so a real finding is "
        "labelled a coverage gap")
    assert "runtime failure codes" in run, (
        "the gate no longer recognises 0.6.0's runtime-failure signal")
    assert "E[0-9]{3}" not in run, (
        "the gate is back to grepping for E-codes, which scanner 0.6.0 never emits")
    assert "scan_class=finding" in run and "scan_class=operational" in run

    verdict = next(s for s in workflow["jobs"]["scan"]["steps"]
                   if s.get("name") == "Report the coverage gap")
    assert "steps.gate.outputs.scan_class" in verdict["run"], (
        "the verdict step branches on the raw exit code again, so a runtime failure is "
        "reported as a security finding")


def test_an_unclassifiable_exit_one_is_never_called_a_finding(workflow: dict):
    """When the scanner names neither class, the honest answer is 'unclassified'.

    Guessing 'critical finding' on an exit this workflow cannot explain is exactly
    the failure it spent five days committing. An unclassified exit still fails the
    build — it is not a pass — it just does not claim to be a security result.
    """
    run = _gate_step(workflow)["run"]
    assert "scan_class=indeterminate" in run, (
        "the gate lost its unclassified branch, so an exit 1 naming no code falls "
        "through to whichever label happens to be last")
    assert "UNCLASSIFIED" in run


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


def _run_gate_classifier(workflow: dict, scanner_output: str, exit_code: int, tmp_path,
                         *, ignored_stdout: str = "third_party_content_exposure",
                         ignored_exit: int = 0):
    """Execute the gate step's real shell against a fake scanner, and report its verdict.

    The other guards in this file are substring greps over the workflow YAML. Review of
    PR #78 showed what that misses: delete the whole classifier but leave the required
    words in shell COMMENTS and every one of them still passes. Nothing here executed
    the ~35 lines of gate shell, so the logic those tests are named for was unverified.

    This runs it. The scanner is replaced by a stub that prints the given text and exits
    the given code, and `$GITHUB_OUTPUT` is a temp file we read back — so the assertion
    is on what the step DOES, not on which words appear near it.
    """
    run = _gate_step(workflow)["run"]
    stub = tmp_path / "uvx"
    stub.write_text(
        "#!/bin/sh\n"
        f"cat <<'SCANOUT'\n{scanner_output}\nSCANOUT\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    # The step calls `python .github/scripts/registry_scan_ignored.py`; stub that too so
    # the shell runs without the repo layout.
    py = tmp_path / "python"
    py.write_text(f"#!/bin/sh\necho '{ignored_stdout}'\nexit {ignored_exit}\n", encoding="utf-8")
    py.chmod(0o755)

    out_file = tmp_path / "gh_output"
    out_file.touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", run],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(out_file),
            "SCAN_PATH": "skills",
            "HOME": str(tmp_path),
        },
    )
    outputs = dict(
        line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line
    )
    return proc, outputs


def test_the_gate_shell_classifies_a_real_finding_as_a_finding(tmp_path):
    """The whole point, executed rather than grepped."""
    workflow = yaml.safe_load((_REPO / ".github" / "workflows" / "registry-scan.yml").read_text())
    proc, outputs = _run_gate_classifier(
        workflow, "└── ci-secure 1 risk\nCI (--ci): exiting with code 1 (risks found).", 1, tmp_path)
    assert outputs.get("scan_class") == "finding", (
        f"a real finding was classified {outputs.get('scan_class')!r} — it would be "
        f"announced as a coverage gap. stderr: {proc.stderr[:400]}")
    assert proc.returncode == 1


def test_the_gate_shell_classifies_a_runtime_failure_as_a_coverage_gap(tmp_path):
    workflow = yaml.safe_load((_REPO / ".github" / "workflows" / "registry-scan.yml").read_text())
    proc, outputs = _run_gate_classifier(
        workflow, "CI (--ci): exiting with code 1 (runtime failure codes: X003).", 1, tmp_path)
    assert outputs.get("scan_class") == "operational", (
        f"a scanner runtime failure was classified {outputs.get('scan_class')!r}")
    assert "NOT A FINDING" in proc.stdout or "DID NOT COMPLETE" in proc.stdout
    assert proc.returncode == 1


def test_the_gate_shell_passes_a_clean_scan(tmp_path):
    workflow = yaml.safe_load((_REPO / ".github" / "workflows" / "registry-scan.yml").read_text())
    proc, outputs = _run_gate_classifier(workflow, "No risks found.", 0, tmp_path)
    assert outputs.get("scan_class") == "clean"
    assert proc.returncode == 0


def test_the_gate_shell_refuses_to_guess_on_an_unnamed_exit(tmp_path):
    """Exit 1 naming neither signal must be UNCLASSIFIED, never a finding.

    Guessing "critical finding" on an exit the workflow cannot explain is the mistake
    that ran for a week. An honest "cannot say" still fails the build.
    """
    workflow = yaml.safe_load((_REPO / ".github" / "workflows" / "registry-scan.yml").read_text())
    proc, outputs = _run_gate_classifier(workflow, "something unexpected happened", 1, tmp_path)
    assert outputs.get("scan_class") == "indeterminate"
    assert proc.returncode == 1


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
                       scan_class: str = "", scan_exit: str = ""):
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
    proc, _ = _run_verdict_shell(
        workflow, tmp_path, red_proof="failure", gate="success", scan_class="clean", scan_exit="0")
    assert proc.returncode == 1
    assert "COVERAGE GAP" in proc.stdout and "NOT A FINDING" in proc.stdout


def test_the_gate_shell_does_not_mistake_scanned_prose_for_a_finding(tmp_path):
    """`risks found` must be read off the scanner's verdict line, not the whole log.

    The gate captures the scanner's `--verbose` output, which echoes the description and
    evidence text of the skills being scanned. These are CI-security skills, so the
    phrase is ordinary prose for them. An unanchored grep let that prose classify a
    crashed scan as a security finding — the same false verdict, sourced from the tree
    instead of from the scanner.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    proc, outputs = _run_gate_classifier(
        workflow,
        "Scanning skills\n"
        "└── ci-secure 0 risks\n"
        "    description: Reports the risks found in a repository's workflows.\n"
        "CI (--ci): exiting with code 1 (runtime failure codes: X003).",
        1, tmp_path)
    assert outputs.get("scan_class") == "operational", (
        f"scanned skill prose classified a crashed scan as {outputs.get('scan_class')!r}")


def test_a_blocking_finding_is_annotated_on_the_checks_tab(tmp_path):
    """The finding branch was the only failure state emitting no titled annotation.

    The reporter deliberately leaves blocking risks to the gate, and the gate emitted a
    plain `echo` — so the one run a review agent most needs to read through the checks
    API had an empty annotation set.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    proc, outputs = _run_gate_classifier(
        workflow, "CI (--ci): exiting with code 1 (risks found).", 1, tmp_path)
    assert outputs.get("scan_class") == "finding"
    assert "::error title=" in proc.stdout, "a blocking risk produced no checks-tab annotation"


def test_the_exit_two_path_records_a_class(tmp_path):
    """The verdict annotation renders `class <value>`; a blank one reads as a bug."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    proc, outputs = _run_gate_classifier(workflow, "error: unrecognized arguments", 2, tmp_path)
    assert outputs.get("scan_class"), "the exit-2 path left scan_class unset"
    assert outputs["scan_class"] != "finding"
    assert proc.returncode == 1


def test_the_gate_fails_when_its_exemption_list_cannot_be_built(tmp_path):
    """`--ignore-risks "$(python ...)"` swallowed the shim's failure.

    A traceback from the shim became an empty argument, the scanner exempted nothing,
    and the owner's ruling was silently off — on a tree with no risks, a green build.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    proc, outputs = _run_gate_classifier(
        workflow, "no risks", 0, tmp_path, ignored_stdout="", ignored_exit=1)
    assert proc.returncode != 0, "the gate ran with an empty exemption list instead of failing"
    assert outputs.get("scan_class") != "clean"


def test_a_retired_exemption_name_fails_the_gate(tmp_path):
    """The scanner drops an unknown risk name with a yellow warning and exits 0.

    That is exactly how the previous outage hid: fifteen `unknown failure code` lines on
    a green build. If Snyk renames the one exempt risk, the exemption goes inert the same
    silent way unless this build reads the warning.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    proc, _ = _run_gate_classifier(
        workflow, "Warning: unknown risk name: third_party_content_exposure\nScan complete.",
        0, tmp_path)
    assert proc.returncode != 0, (
        "the gate passed with an exemption name the scanner does not recognise")


def test_the_gate_reads_its_exemption_list_from_the_shared_contract(workflow: dict):
    """The gate must not restate the ruling inline.

    An inline `--ignore-risks "third_party_content_exposure,suspicious_download_url"`
    would exempt the very risk the red-proof anchors on, while the red-proof — which
    reads the contract — stayed green. Drift between the two is the single thing
    `registry_scan_contract.py` exists to prevent.
    """
    run = _gate_step(workflow)["run"]
    assert "registry_scan_ignored.py" in run, (
        "the gate no longer sources its exemption list from the shared contract shim")
    contract = _load_contract_module()
    for risk in contract.SKILL_RISKS + contract.SERVER_RISKS:
        assert risk not in run, (
            f"the gate names the risk {risk!r} inline; the ruling has exactly one home, "
            f"in registry_scan_contract.py")


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


def test_the_exemption_shim_prints_exactly_the_contract_list():
    """The one place a policy decision becomes a command-line argument, and it had no
    coverage at all: hardcoding blocking risks into it kept every test green."""
    proc = subprocess.run(
        [sys.executable, str(_REPO / ".github" / "scripts" / "registry_scan_ignored.py")],
        capture_output=True, text=True, check=True)
    contract = _load_contract_module()
    assert proc.stdout.strip() == ",".join(contract.NON_BLOCKING_RISKS)
    assert proc.stdout.strip() == "third_party_content_exposure"


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
    # cause is noise that trains people to skip it.
    not_version_related = ("SNYK_TOKEN is not set", "HAS NOTHING TO SCAN")
    gap_lines = [
        line
        for step in steps
        for line in (step.get("run") or "").splitlines()
        if "::error title=" in line
        and any(m in line for m in gap_markers)
        and not any(x in line for x in not_version_related)
    ]
    assert len(gap_lines) >= 4, f"expected several coverage-gap messages, found {len(gap_lines)}"
    missing = [ln for ln in gap_lines if "STALE_PIN_HINT" not in ln]
    assert not missing, (
        "coverage-gap message(s) do not name the scanner pin as a suspect:\n  "
        + "\n  ".join(m.strip()[:120] for m in missing))


def test_the_pin_is_stated_once_and_matches_everywhere(workflow: dict):
    """Three places invoke the scanner; a half-bumped pin is two contracts at once.

    The workflow's unfiltered pass, its gate, and the red-proof must all run the same
    version, and the contract's PINNED_SCANNER must agree — otherwise the hint above
    names a version the gate is not running, which is worse than no hint.
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
