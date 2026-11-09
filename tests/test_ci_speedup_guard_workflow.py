"""Contract tests for the repository-local ci-speedup-guard workflow."""
from __future__ import annotations

import itertools
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parents[1]
PATH = REPO / ".github" / "workflows" / "ci-speedup-guard.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


def test_pull_request_trigger_is_unfiltered_and_includes_edited(workflow):
    trigger = workflow[True]["pull_request"]
    assert trigger == {"types": ["opened", "synchronize", "reopened", "edited"]}


def test_scan_jobs_are_exact_internal_fork_complements(workflow):
    internal, fork = workflow["jobs"]["scan"], workflow["jobs"]["scan-fork"]
    predicate = "github.event.pull_request.head.repo.full_name == github.repository"
    assert " ".join(internal["if"].split()) == predicate
    assert " ".join(fork["if"].split()) == predicate.replace(" == ", " != ")
    assert internal["runs-on"].startswith("starsling-")
    assert fork["runs-on"] == "ubuntu-latest"
    assert "secrets." not in yaml.safe_dump(fork)
    assert "TOKEN" not in yaml.safe_dump(fork).upper()


def test_checkouts_are_depth_two_credentialless_merge_checkouts(workflow):
    for job_id in ("scan", "scan-fork"):
        checkout = workflow["jobs"][job_id]["steps"][0]
        assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
        assert checkout["with"] == {"path": "merge", "ref": "${{ github.sha }}",
                                    "fetch-depth": 2, "persist-credentials": False}
        materialize = workflow["jobs"][job_id]["steps"][3]["run"]
        assert "worktree add --detach ../base HEAD^1" in materialize
        assert "fetch" not in materialize


def test_actions_and_runtime_dependency_are_pinned(workflow):
    for job_id in ("scan", "scan-fork"):
        steps = workflow["jobs"][job_id]["steps"]
        assert re.fullmatch(r"actions/setup-python@[0-9a-f]{40}", steps[1]["uses"])
        assert steps[2]["run"] == "python3 -m pip install PyYAML==6.0.3"
    assert workflow["permissions"] == {"contents": "read"}


def test_only_always_running_hosted_verdict_owns_required_name(workflow):
    jobs = workflow["jobs"]
    named = [job for job in jobs.values() if job.get("name") == "ci-speedup-guard"]
    assert len(named) == 1
    verdict = named[0]
    assert set(verdict["needs"]) == {"scan", "scan-fork"}
    assert verdict["if"] == "always()"
    assert verdict["runs-on"] == "ubuntu-latest"
    assert not any("uses" in step for step in verdict["steps"])


def test_verdict_accepts_only_one_success_and_one_skip(workflow):
    script = workflow["jobs"]["verdict"]["steps"][0]["run"]
    values = ("success", "failure", "cancelled", "skipped", "")
    for internal, fork in itertools.product(values, repeat=2):
        proc = subprocess.run(["bash", "-c", script], env={"SELF_HOSTED": internal,
                              "FORK": fork}, capture_output=True, text=True)
        expected = (internal, fork) in {("success", "skipped"), ("skipped", "success")}
        assert (proc.returncode == 0) is expected, (internal, fork, proc.stdout, proc.stderr)


def test_no_step_can_swallow_a_guard_failure(workflow):
    text = PATH.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "set +e" not in text
    for job_id in ("scan", "scan-fork"):
        guard_step = workflow["jobs"][job_id]["steps"][-1]
        assert "if" not in guard_step
        assert "--base base --merge merge" in guard_step["run"]


def test_scan_timeout_contains_two_engine_timeouts(workflow):
    source = (REPO / ".github" / "scripts" / "ci_speedup_guard.py").read_text()
    assert "SCAN_TIMEOUT_SECONDS = 60" in source
    assert workflow["jobs"]["scan"]["timeout-minutes"] == 10


def test_repository_policy_has_a_real_blocking_rule():
    guard_path = REPO / ".github" / "scripts" / "ci_speedup_guard.py"
    spec = importlib.util.spec_from_file_location("guard_policy_probe", guard_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.BLOCKING_ALLOWLIST == frozenset({"OPT23"})


def test_codeowners_routes_the_workflow():
    text = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "/.github/" in text


def test_the_guard_workflow_does_not_create_candidate_debt():
    scan_path = REPO / "skills" / "ci-speedup" / "scripts" / "scan.py"
    spec = importlib.util.spec_from_file_location("guard_workflow_scan", scan_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    catalog = REPO / "skills" / "ci-speedup" / "references" / "patterns"
    findings = [f for f in module.scan(REPO, catalog, "main")["findings"]
                if f["workflow_file"] == ".github/workflows/ci-speedup-guard.yml"]
    assert {f["pattern"] for f in findings} == {"OPT32", "OPT45"}
    candidates = {"OPT23", "OPT27", "OPT41", "OPT55", "OPT62", "OPT63", "OPT17"}
    assert not candidates & {f["pattern"] for f in findings}
