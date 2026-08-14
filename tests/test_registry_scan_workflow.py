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

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "registry-scan.yml"
_CI_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"
_REDPROVE = _REPO / ".github" / "scripts" / "registry_scan_redprove.py"
_REPORT = _REPO / ".github" / "scripts" / "registry_scan_report.py"

# THE GATING RULE (owner ruling, 2026-08-10): the build fails on the critical class only —
# Snyk Agent Scan's E-codes. Warning-class findings (W-codes) are surfaced on every run but
# never block. The scanner has no severity threshold, so the rule is expressed by enumerating
# the W class into --ignore-issues-codes.
#
# This is every W-code published in the scanner's catalog:
# https://github.com/snyk/agent-scan/blob/main/docs/issue-codes.md
# Pinned so that a newly published W-code arrives as a conscious update here rather than as an
# unexplained red build — and, far more importantly, so that no E-code can ever be slipped in.
_PUBLISHED_WARNING_CODES = {
    "W001", "W007", "W008", "W009", "W011", "W012", "W013", "W014",
    "W015", "W016", "W017", "W018", "W019", "W020", "W021",
}


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
        if _invokes_scanner(step) and "--ignore-issues-codes" in step["run"]
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
        if _invokes_scanner(step) and "--ignore-issues-codes" not in step["run"]
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


def test_gate_spells_the_ignore_flag_the_way_the_scanner_does(workflow: dict):
    """The flag is `--ignore-issues-codes` — plural "issues", singular "codes".

    It reads like a typo and has been "corrected" to `--ignore-issue-codes` in review.
    It is not a typo: the scanner's own argument parser declares
    `parser.add_argument("--ignore-issues-codes", ...)`, and the singular spelling is
    rejected with `error: unrecognized arguments`. Getting it wrong does not degrade the
    gate quietly — it exits on a usage error the first time it runs with a token, which
    is a path no offline test can reach. Hence this assertion.
    """
    assert "--ignore-issues-codes" in _gate_step(workflow)["run"]


def test_unfiltered_pass_sees_what_the_printer_would_hide(workflow: dict):
    """The visibility pass emits JSON, which is strictly more complete than the report.

    The scanner's printer strips the codes W003-W006 from the result as it prints — and
    mutates the result in place doing it. JSON output skips the printer entirely, so the
    one pass that is supposed to see everything actually does. (The gating pass keeps
    `--verbose`, which is the same defect's off-switch on the report path.)
    """
    assert "--json" in _visibility_step(workflow)["run"], (
        "the unfiltered pass must emit JSON: it is the input to the annotation, summary, "
        "and artifact surfaces, and it is the only output the printer cannot silently trim"
    )
    assert "--verbose" in _gate_step(workflow)["run"], (
        "the gating pass lost --verbose, so the scanner drops W003-W006 from the result "
        "the --ci exit check reads"
    )


def _declared_ignored_codes(workflow: dict) -> set:
    return {
        code.strip()
        for code in workflow["env"]["IGNORED_ISSUE_CODES"].split(",")
        if code.strip()
    }


def test_no_critical_code_is_ever_ignored(workflow: dict):
    """The load-bearing direction of the gating rule.

    Warnings not blocking is a policy choice. A critical code reaching the ignore list is
    a disarmed gate that still reports green — the precise failure this whole workflow
    exists to prevent — so it is asserted separately and in the strongest form: nothing
    outside the W class may be suppressed, whatever the reason given.
    """
    for code in _declared_ignored_codes(workflow):
        assert code.startswith("W"), (
            f"{code!r} is in the gate's ignore list but is not a warning-class code. Only "
            f"W-codes may be ignored; an E-code here silently disarms the gate, and the "
            f"X-codes report that the scan itself failed."
        )


def test_ignore_list_is_exactly_the_published_warning_class(workflow: dict):
    """The rule is class-based, so the list must be the class — no more, no less.

    Short of the published set, a warning fails the build and someone 'fixes' it by
    guessing. Beyond it, a code nobody has read is being suppressed.
    """
    assert _declared_ignored_codes(workflow) == _PUBLISHED_WARNING_CODES, (
        "the gate's ignore list no longer matches the published W-class. If the scanner "
        "published a new warning code, add it here and to the workflow in the same change; "
        "if a code was removed, drop it. Never add a code that is not a W-code."
    )

    # The env var is only the declared list; what the gate actually suppresses is what it
    # passes on the command line. An inline `--ignore-issues-codes "${IGNORED_ISSUE_CODES},E005"`
    # would suppress the very rule the red-proof is anchored to while the check above stays
    # green, so pin that the flag's argument is the variable and nothing else.
    argument = re.search(
        r'--ignore-issues-codes\s+"?([^"\n\\]*)"?', _gate_step(workflow)["run"]
    )
    assert argument, "the gating scan no longer passes an ignore list"
    assert argument.group(1).strip() == "${IGNORED_ISSUE_CODES}", (
        "the gating scan suppresses codes inline rather than through IGNORED_ISSUE_CODES. "
        "Every exclusion must go through the reviewed list above, where its reason is "
        f"written beside it; found {argument.group(1).strip()!r}."
    )


def test_gating_rule_is_written_down_where_it_is_configured(workflow_text: str):
    """A rule nobody can see is how a real finding gets hidden later.

    The ignore list is now fifteen codes long; without the rule stated beside it, the next
    reader sees a pile of suppressions rather than one deliberate class decision.
    """
    assert "issue-codes.md" in workflow_text, (
        "the workflow must cite the published catalog the W class is enumerated from"
    )
    assert "untrusted third-party content" in workflow_text, (
        "the workflow must say what W011 — the code that actually fires on these skills "
        "— is, so the list is not fifteen opaque strings"
    )
    lowered = workflow_text.lower()
    assert "never fail" in lowered or "never block" in lowered, (
        "the workflow must state the gating rule in words, not only in a variable"
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


def test_red_proof_runs_on_every_run(workflow: dict):
    """A check that cannot fail is not a check.

    Adding `if:` or `continue-on-error:` to this step disables the one guarantee the whole
    gate rests on while leaving it plainly visible in the file, so both are asserted — and
    it must run before the gating scan, so a broken anchor is reported as such rather than
    as a scan result.
    """
    assert _REDPROVE.exists(), "the red-proof script is missing"
    job = _scan_job(workflow)
    redprove = [
        step for step in job["steps"]
        if "registry_scan_redprove.py" in step.get("run", "")
    ]
    assert len(redprove) == 1, "expected exactly one red-proof step"
    _assert_unconditional(redprove[0], "the red-proof step")

    gate_index = _step_index(job, lambda s: s is _gate_step(workflow))
    redprove_index = _step_index(
        job, lambda s: "registry_scan_redprove.py" in s.get("run", "")
    )
    assert redprove_index < gate_index, (
        "the red-proof must run before the gating scan, so 'the gate cannot fail' is "
        "reported as its own failure rather than hidden behind a scan result"
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


_SAMPLE_SCAN = {
    "skills": {
        "path": "skills",
        "servers": [{"name": "ci-speedup"}, {"name": "ci-secure"}],
        "issues": [
            {
                "code": "W011",
                "message": "Exposure to untrusted third-party content",
                "reference": [0, None],
                "extra_data": {"severity": "medium"},
            },
            {
                "code": "E005",
                "message": "Suspicious download URL in skill",
                "reference": [1, None],
                "extra_data": {"severity": "critical"},
            },
        ],
    }
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

    assert "::warning title=W011 (medium) in ci-speedup::" in out
    assert "::warning title=E005" not in out, "a critical must not be reported as a warning"
    assert "::error" not in out, "the reporter does not gate, so it must not emit errors"

    # Both findings still appear in the human table — "does not annotate" is not "does not show".
    assert "W011" in out and "E005" in out
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
    assert "Warnings do not block" in body
    assert "registry-scan-findings" in body, "the summary must point at the artifact"
    assert "`W011`" in body and "`E005`" in body


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
    assert "::warning title=W011" in capsys.readouterr().out


def test_reporter_reports_nothing_as_nothing(tmp_path, capsys):
    module = _load_report_module()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"skills": {"path": "skills", "issues": []}}), encoding="utf-8")

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

    # The assembled string must not appear verbatim in the script that assembles it.
    marker = "curl -sSL http" + "s://get.redprove-fixture.example.com"
    assert marker not in _REDPROVE.read_text(encoding="utf-8"), (
        "the red-proof script now contains the literal it is supposed to construct"
    )
