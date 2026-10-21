"""The skill-evals gate cannot go green over a session that did not happen.

This workflow runs two skills' behavioural cases against a live agent: ci-secure's
and ci-speedup's, each as its own job reporting under its own verdict check. Its
entire value is that a green `<skill> skill evals` check means real sessions ran
and behaved. Every way that decays is invisible in the check list: a fork PR that
reports green having reached no API key, a verdict job that exits 0 on a skip, a
harness that could not start and is read as a behaviour change, a floating CLI
version that turns an upstream release into a "skill regression", or one skill's
pull request paying for (or hiding behind) the other skill's suite.

A text reading of the YAML cannot see an added permissive clause, so the verdict
and classifier scripts are EXECUTED here over every input they can be handed — the
same technique tests/test_ci_required_check_verdict.py uses on ci.yml. No network,
no agent, no token: this is the offline guard on the gate that costs money to run.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "skill-evals.yml"

# (eval job id, verdict job id, verdict check name, classifier output, skill dir, harness)
_SUITES = [
    ("evals", "verdict", "ci-secure skill evals", "secure",
     _REPO / "skills" / "ci-secure", "maintainers/ci-secure/scripts/run_skill_evals.py"),
    ("speedup-evals", "speedup-verdict", "ci-speedup skill evals", "speedup",
     _REPO / "skills" / "ci-speedup", "maintainers/ci-speedup/scripts/run_skill_evals.py"),
]
_SUITE_IDS = [s[0] for s in _SUITES]
_FORK_CLAUSE = ("github.event_name == 'pull_request' && "
                "github.event.pull_request.head.repo.full_name != github.repository")


def _doc() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job(job_id: str) -> dict:
    jobs = _doc()["jobs"]
    assert job_id in jobs, f"job {job_id!r} is gone; found {sorted(jobs)}"
    return jobs[job_id]


def _squash(expr: object) -> str:
    return re.sub(r"\s+", " ", str(expr)).strip()


def _agent_step(job_id: str) -> dict:
    return next(s for s in _job(job_id)["steps"] if s.get("name") == "Run the eval cases")


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_verdict_never_reports_on_a_fork_pull_request(suite):
    """The bug this pins: the verdict used to `exit 0` on a fork PR, so an
    external contribution got a green eval check with no session run at all —
    a `::warning::` annotation is not a check status."""
    cond = _squash(_job(suite[1]).get("if", ""))
    assert "always()" in cond, "a verdict that can be skipped can be bypassed"
    assert "head.repo.full_name != github.repository" in cond, (
        "the verdict must exclude fork PRs; a green eval check on a fork is "
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


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_fork_and_verdict_conditions_are_exact_complements(suite):
    """On a fork PR only the NOT RUN notice reports; off one, the verdict
    reports unless the classifier said, EXPLICITLY, that this suite does not
    apply. Overlap prints a green verdict next to the NOT RUN notice; a gap
    prints neither. `!= 'false'` rather than `== 'true'`: a classifier that
    failed leaves the output empty, and the verdict must then still report."""
    assert _squash(_job("fork-did-not-run")["if"]) == _FORK_CLAUSE
    assert _squash(_job(suite[1])["if"]) == (
        f"always() && !({_FORK_CLAUSE}) && needs.changes.outputs.{suite[3]} != 'false'")


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_a_suite_runs_only_when_its_skill_changed_and_never_for_a_fork(suite):
    """Each suite costs real sessions, so it runs for its own skill's contract
    and nothing else, and never for a fork, which cannot reach the API key."""
    cond = _squash(_job(suite[0])["if"])
    assert cond.startswith(f"needs.changes.outputs.{suite[3]} == 'true' && ("), cond
    assert "github.event_name == 'workflow_dispatch'" in cond
    assert "head.repo.full_name == github.repository" in cond
    assert "changes" in _job(suite[0])["needs"]
    assert set(_job(suite[1])["needs"]) == {"changes", suite[0]}


def test_every_verdict_check_name_is_unique_and_names_its_skill():
    """Two suites under one check name would let a red in one be read as the
    other's, and a green in one hide the other's absence."""
    names = [str(j.get("name", jid)) for jid, j in _doc()["jobs"].items()]
    for _, verdict, check, *_ in _SUITES:
        assert str(_job(verdict).get("name")) == check
        assert names.count(check) == 1, f"{check!r} is reported by more than one job"
    assert "skill evals" not in names, (
        "the bare name 'skill evals' no longer says which skill was verified")


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
@pytest.mark.parametrize("result,expected", [
    ("success", 0),
    ("failure", 1),
    ("cancelled", 1),
    ("skipped", 1),
    ("", 1),
    ("neutral", 1),
])
def test_the_verdict_script_passes_only_on_a_real_success(suite, result, expected):
    """Executed, not read. `skipped` reads as green to branch protection, so the
    verdict must turn every non-success into a non-zero exit itself — and it
    must read ITS OWN suite's result, not the other skill's."""
    step = _job(suite[1])["steps"][0]
    assert _squash(step["env"]["RESULT"]) == f"${{{{ needs.{suite[0]}.result }}}}"
    proc = subprocess.run(
        ["bash", "-c", step["run"]], env={"RESULT": result, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode == expected, (
        f"RESULT={result!r} exited {proc.returncode}, expected {expected}: "
        f"{proc.stdout}{proc.stderr}")


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_agent_step_declares_the_sandbox_the_cli_requires_under_root(suite):
    """The first CI run failed every grader in 1.6 seconds: the runner is root,
    and `claude` refuses `--permission-mode bypassPermissions` under uid 0
    unless a deliberate sandbox is declared. Dropping this env var silently
    reverts the workflow to grading sessions that never started."""
    assert _agent_step(suite[0])["env"].get("IS_SANDBOX") == "1"


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_agent_step_runs_its_own_skills_harness(suite):
    run = _agent_step(suite[0])["run"]
    assert suite[5] in run
    assert not [s[5] for s in _SUITES if s is not suite and s[5] in run]


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_agent_step_preserves_the_could_not_run_exit_code(suite):
    """Exit 2 is the harness saying "nothing was verified". Collapsing it into
    the same red as exit 1 tells the reader the agent's behaviour changed when
    the truth is that no agent ran."""
    step = _agent_step(suite[0])
    assert "-eq 2" in step["run"], "the step must branch on the harness's exit 2"
    assert "DID NOT RUN" in step["run"]


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_claude_cli_is_pinned(suite):
    """A suite whose failure message asserts "the agent's behaviour changed"
    must hold the runtime still, or an upstream release reads as a regression."""
    step = next(s for s in _job(suite[0])["steps"]
                if s.get("name") == "Install Claude Code")
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", step["run"]), (
        f"pin the CLI version; got {step['run'].strip()!r}")


def test_both_suites_pin_the_same_claude_cli():
    """One runtime for both suites, so a bump re-baselines both at once and a
    difference between them is never a CLI difference."""
    pins = {next(s for s in _job(suite[0])["steps"]
                 if s.get("name") == "Install Claude Code")["run"].strip()
            for suite in _SUITES}
    assert len(pins) == 1, pins


@pytest.mark.parametrize("suite", _SUITES, ids=_SUITE_IDS)
def test_the_job_budget_exceeds_the_harness_own_worst_case(suite):
    """A slow-but-healthy suite killed by the job timeout is reported as a
    behaviour change."""
    worst_seconds = 0
    for case in sorted((suite[4] / "evals").glob("*/case.yaml")):
        spec = yaml.safe_load(case.read_text(encoding="utf-8"))
        worst_seconds += (spec.get("execution") or {}).get("timeout_seconds", 900)
    assert worst_seconds, f"no cases found for {suite[0]}"
    assert _job(suite[0])["timeout-minutes"] >= worst_seconds / 60, (
        f"{worst_seconds / 60:.0f} minutes of per-run budget under a "
        f"{_job(suite[0])['timeout-minutes']}-minute job timeout")


def test_the_ci_speedup_session_gets_a_token_for_the_gh_gate_and_nothing_more():
    """ci-speedup's SKILL.md phase 1 stops the audit unless `gh auth status`
    passes, so its session needs a token; the workflow's own read-only one is
    enough, because every run-history read is replayed from the committed
    corpus. ci-secure's suite never had one, and adding it would change what
    that suite tests."""
    assert _agent_step("speedup-evals")["env"].get("GH_TOKEN") == "${{ github.token }}"
    assert "permissions" not in _job("speedup-evals"), (
        "the job must not widen the workflow's read-only token")
    assert "GH_TOKEN" not in _agent_step("evals")["env"]


def test_every_third_party_action_is_pinned_to_a_sha():
    for jid, job in _doc()["jobs"].items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), (
                    f"{jid}: {uses} is not pinned to a full commit SHA")


def test_the_workflow_asks_for_no_more_than_read_access():
    assert _doc()["permissions"] == {"contents": "read"}
    for jid, job in _doc()["jobs"].items():
        assert "permissions" not in job, f"{jid} widens the workflow's token"


# --- the classifier: which suites an event pays for --------------------------

def _classify(tmp_path: Path, event: str, changed: list[str], skill: str = "") -> dict:
    step = next(s for s in _job("changes")["steps"] if s.get("id") == "classify")
    listing = tmp_path / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed), encoding="utf-8")
    out = tmp_path / "out.txt"
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"EVENT": event, "SKILL": skill, "CHANGED": str(listing),
             "GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


@pytest.mark.parametrize("changed,secure,speedup", [
    (["skills/ci-speedup/scripts/collect_runs.py"], "false", "true"),
    (["skills/ci-speedup/SKILL.md", "README.md"], "false", "true"),
    (["skills/ci-speedup/tests/fixtures/gh_replay/repos_synthetic_repo.json"], "false", "true"),
    (["skills/ci-speedup/tests/verify_report.py"], "false", "true"),
    (["maintainers/ci-speedup/scripts/eval_gh_shim.py"], "false", "true"),
    (["skills/ci-secure/references/x.md"], "true", "false"),
    (["maintainers/ci-secure/scripts/run_skill_evals.py"], "true", "false"),
    (["skills/ci-secure/SKILL.md", "skills/ci-speedup/evals/README.md"], "true", "true"),
    ([".github/workflows/skill-evals.yml"], "true", "true"),
    # Not contract files: an oracle test or a doc never pays for a session.
    (["skills/ci-speedup/tests/test_blocking_path.py", "docs/methodology.md"], "false", "false"),
])
def test_a_pull_request_pays_only_for_the_suites_it_touched(tmp_path, changed, secure, speedup):
    got = _classify(tmp_path, "pull_request", changed)
    assert (got.get("secure"), got.get("speedup")) == (secure, speedup), got


@pytest.mark.parametrize("skill,secure,speedup", [
    ("both", "true", "true"), ("", "true", "true"),
    ("ci-secure", "true", "false"), ("ci-speedup", "false", "true"),
])
def test_a_manual_run_runs_the_suites_it_names(tmp_path, skill, secure, speedup):
    got = _classify(tmp_path, "workflow_dispatch", [], skill)
    assert (got.get("secure"), got.get("speedup")) == (secure, speedup), got


def test_every_triggering_path_is_classified_into_a_suite(tmp_path):
    """The trigger's path filter and the classifier are two lists of the same
    thing. A path that triggers the workflow but no suite starts a run that
    verifies nothing; the day the lists drift, this says which entry."""
    doc = _doc()
    on = doc.get("on") or doc.get(True)
    for pattern in on["pull_request"]["paths"]:
        sample = pattern.replace("**", "x/y.md")
        got = _classify(tmp_path, "pull_request", [sample])
        assert "true" in got.values(), f"{pattern} triggers the workflow but runs no suite"
