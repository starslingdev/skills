from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "shard_compile_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_WORKFLOW = ".github/workflows/tests.yml"
_SHA = "sample-sha"


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def _job(name: str, run_id: int, duration_s: int, ordinal: int) -> dict:
    job_id = run_id * 10 + ordinal
    return {
        "id": job_id,
        "run_id": run_id,
        "name": name,
        "conclusion": "success",
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(0),
        "completed_at": _iso(duration_s),
        "steps": [{
            "name": "Run tests",
            "conclusion": "success",
            "started_at": _iso(0),
            "completed_at": _iso(duration_s),
        }],
    }


class _Client:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{
                "id": 1, "path": _WORKFLOW, "name": "tests", "state": "active",
            }]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 12}
            return {"workflow_runs": [
                {
                    "id": run_id,
                    "event": "pull_request",
                    "head_sha": f"{_SHA}-{run_id}",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": _iso(0),
                    "run_started_at": _iso(0),
                    "updated_at": _iso(3000),
                }
                for run_id in (101, 102)
            ]}
        match = re.match(r"repos/o/r/actions/runs/(101|102)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            return {"jobs": [
                _job("cargo tests", run_id, 3000, 1),
                _job("go tests", run_id, 2900, 2),
                _job("jest tests", run_id, 2800, 3),
            ]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [
                {"name": "cargo tests", "started_at": _iso(0), "completed_at": _iso(3000)},
                {"name": "go tests", "started_at": _iso(0), "completed_at": _iso(2900)},
                {"name": "jest tests", "started_at": _iso(0), "completed_at": _iso(2800)},
            ]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["cargo tests"]}
        if endpoint == f"repos/o/r/contents/{_WORKFLOW}":
            workflow = """\
on: pull_request
jobs:
  cargo-tests:
    name: cargo tests
    runs-on: ubuntu-latest
    steps:
      - run: cargo test --locked
  go-tests:
    name: go tests
    needs: cargo-tests
    runs-on: ubuntu-latest
    steps:
      - run: go test ./...
  jest-tests:
    name: jest tests
    needs: go-tests
    runs-on: ubuntu-latest
    steps:
      - run: npx jest
"""
            return {"content": base64.b64encode(workflow.encode()).decode()}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        job_id = int(endpoint.split("/")[-2])
        ordinal = job_id % 10
        if ordinal == 1:
            return "\n".join([
                "$ cargo test --locked",
                "Finished `test` profile [unoptimized + debuginfo] target(s) in 49m 50s",
                "test result: ok. 42 passed; 0 failed; finished in 7.05s",
            ])
        if ordinal == 2:
            return "$ go test ./...\nok example/project 2900.0s"
        return "$ npx jest\nTests: 42 passed, 42 total"


def _finding(doc: dict, job: str) -> dict:
    return next(
        finding for finding in doc["findings"]
        if finding.get("pattern") == "OPT24" and job in finding.get("affected_jobs", [])
    )


def test_collect_excludes_repeated_compile_from_sharding_credit_and_report_verifies(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cr, "GhClient", _Client)
    doc = cr.collect({
        "repo": "o/r",
        "skill_commit_sha": "installed:unversioned",
        "workflow_job_graph": {
            _WORKFLOW: {
                "cargo-tests": {"name": "cargo tests", "needs": []},
                "go-tests": {"name": "go tests", "needs": ["cargo-tests"]},
                "jest-tests": {"name": "jest tests", "needs": ["go-tests"]},
            },
        },
        "findings": [],
    }, "o/r", max_runs=2, shallow_runs=2)

    cargo = _finding(doc, "cargo tests")
    assert cargo["wall_clock_p50_s"] <= 5.0, (
        "the 49m50s compile repeats on every shard, so it is not shardable payload")
    assert "49m 50s" in cargo["measured_evidence"]["note"]

    go = _finding(doc, "go tests")
    assert go["wall_clock_p50_s"] == 0.0
    assert "measure" in go["size_note"].lower()

    jest = _finding(doc, "jest tests")
    assert jest["wall_clock_p50_s"] == 1400.0

    report = bp.render(doc, captured_at="2026-11-07T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_shard_guidance_names_the_repeated_runner_minute_floor() -> None:
    assert "add roughly" in bp._SHARD_RUNNER_MINUTE_LINE
    assert "pre-test" in bp._SHARD_RUNNER_MINUTE_LINE


def test_cargo_log_leaf_passes_the_compile_floor_to_the_agent_prompt() -> None:
    lines = [
        "$ cargo test --locked",
        "Finished `test` profile [unoptimized + debuginfo] target(s) in 49m 50s",
    ]
    for index, seconds in enumerate((100, 80, 60, 40), 1):
        lines += [
            f"Running tests/case_{index}.rs (target/debug/deps/case_{index}-abcd)",
            "test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; "
            f"0 filtered out; finished in {seconds}.00s",
        ]
    leaf = bp._parse_log("\n".join(lines))
    assert leaf is not None and leaf["fix_key"] == "cargo-test-shard"
    assert leaf["pre_test_floor_s"] == 2990.0

    prompt = bp._build_agent_prompt(
        leaf,
        {
            "workflow_file": _WORKFLOW,
            "check": "cargo tests",
            "job": "cargo tests",
            "p50_s": 3000.0,
        },
        [], None, "o/r", None, 2, 2,
    )
    assert "Measured pre-test compile floor: 49m 50s" in prompt
    assert "Every added shard repeats that floor" in prompt

