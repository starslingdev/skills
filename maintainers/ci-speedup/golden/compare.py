#!/usr/bin/env python3
"""Compare a replayed ci-speedup result with independent golden facts."""

from __future__ import annotations

import json
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def engine_facts(findings: dict[str, Any]) -> dict[str, Any]:
    critical = findings.get("pr_critical_path") or {}
    checks = [row for row in critical.get("checks") or [] if isinstance(row, dict)]
    checks.sort(key=lambda row: (-int(row.get("pole_n") or 0), -float(row.get("p50_s") or 0), str(row.get("name") or "")))
    wall = critical.get("commit_wall_clock") or {}
    typical = wall.get("p50_s")
    if typical is None:
        typical = (critical.get("chain_summary") or {}).get("makespan_p50_s")
    return {
        "sampled_pr_count": int(critical.get("sampled_pr_count") or 0),
        "typical_pr_wait_s": float(typical or 0.0),
        "gate_order": [str(row.get("name") or "") for row in checks if int(row.get("pole_n") or 0) > 0],
        "gates": {
            str(row.get("name") or ""): {
                "last_on": int(row.get("pole_n") or 0),
                "p50_s": float(row.get("p50_s") or 0.0),
            }
            for row in checks if int(row.get("pole_n") or 0) > 0
        },
    }


def _matching_gate(expected_name: str, actual_gates: dict[str, Any]) -> str | None:
    if expected_name in actual_gates:
        return expected_name
    candidates = [
        name for name in actual_gates
        if expected_name.endswith(" / " + name) or name.endswith(" / " + expected_name)
    ]
    return candidates[0] if len(candidates) == 1 else None


def compare(findings: dict[str, Any], expected: dict[str, Any], report: str = "") -> list[str]:
    oracle = expected.get("oracle") or {}
    actual = engine_facts(findings)
    differences: list[str] = []
    verifier_failures = findings.get("_golden_verifier_failures") or []
    if not verifier_failures and findings.get("_golden_verifier_failure"):
        verifier_failures = [findings["_golden_verifier_failure"]]
    for verifier_failure in verifier_failures:
        differences.append("full report verifier failed: " + str(verifier_failure))
    expected_completed = int((oracle.get("sample") or {}).get("completed_with_eligible_jobs") or 0)
    if actual["sampled_pr_count"] < min(100, expected_completed):
        differences.append(
            f"sampled {actual['sampled_pr_count']} PR commits; independent recording has {expected_completed} completed commits"
        )
    expected_order = list(oracle.get("gate_order") or [])
    comparable = [
        match for name in expected_order
        if (match := _matching_gate(str(name), actual["gates"])) is not None
    ]
    actual_order = [name for name in actual["gate_order"] if name in set(comparable)]
    if len(comparable) >= 2 and actual_order != comparable:
        differences.append(
            "gate order differs: engine=" + repr(actual_order) + ", oracle=" + repr(comparable)
        )
    sample_target = min(100, expected_completed)
    if sample_target and actual["sampled_pr_count"] >= sample_target:
        expected_wait = float(oracle.get("typical_pr_wait_s") or 0.0)
        actual_wait = float(actual["typical_pr_wait_s"] or 0.0)
        if expected_wait and abs(actual_wait - expected_wait) > max(60.0, expected_wait * 0.35):
            differences.append(
                f"typical PR wait differs: engine={actual_wait:.1f}s, oracle={expected_wait:.1f}s"
            )
        expected_gates = [row for row in oracle.get("gates") or [] if isinstance(row, dict)]
        if expected_gates:
            lead_name = str(expected_gates[0].get("check") or "")
            if lead_name and _matching_gate(lead_name, actual["gates"]) is None:
                differences.append(f"independent leading gate absent: {lead_name!r}")
        for row in expected_gates:
            expected_name = str(row.get("check") or "")
            actual_name = _matching_gate(expected_name, actual["gates"])
            if actual_name is None:
                continue
            actual_gate = actual["gates"][actual_name]
            oracle_share = float(row.get("share") or 0.0)
            actual_share = float(actual_gate["last_on"]) / actual["sampled_pr_count"]
            if abs(actual_share - oracle_share) > 0.15:
                differences.append(
                    f"gate share differs for {expected_name!r}: "
                    f"engine={actual_share:.3f}, oracle={oracle_share:.3f}"
                )
            oracle_p50 = float(row.get("p50_s") or 0.0)
            actual_p50 = float(actual_gate["p50_s"] or 0.0)
            if oracle_p50 and abs(actual_p50 - oracle_p50) > max(120.0, oracle_p50 * 0.35):
                differences.append(
                    f"gate p50 differs for {expected_name!r}: "
                    f"engine={actual_p50:.1f}s, oracle={oracle_p50:.1f}s"
                )
    required = set((expected.get("human_review") or {}).get("findings_must_appear") or [])
    present = {str(row.get("pattern") or "") for row in findings.get("findings") or [] if isinstance(row, dict)}
    for pattern in sorted(required - present):
        differences.append(f"required finding absent: {pattern}")
    forbidden = set((expected.get("human_review") or {}).get("findings_must_not_appear") or [])
    for pattern in sorted(forbidden & present):
        differences.append(f"forbidden finding present: {pattern}")
    review = expected.get("human_review") or {}
    bottom_line = str(review.get("bottom_line_must_contain") or "")
    if bottom_line and bottom_line not in report:
        differences.append(f"Bottom line does not contain reviewed text: {bottom_line!r}")
    for text in review.get("report_must_contain") or []:
        if str(text) not in report:
            differences.append(f"reviewed report text absent: {text!r}")
    for text in review.get("report_must_not_contain") or []:
        if str(text) in report:
            differences.append(f"forbidden report text present: {text!r}")
    return differences


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not an object")
    return value


def _fixture_name(endpoint: str) -> str:
    endpoint_safe = endpoint
    for operator, word in ((">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt")):
        endpoint_safe = endpoint_safe.replace(operator, word)
    safe = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in endpoint_safe
    )
    if len(safe) > 200:
        safe = safe[:200] + hashlib.sha256(endpoint.encode()).hexdigest()[:8]
    return safe + ".json"


def _write_fixture(destination: Path, endpoint: str, payload: dict[str, Any]) -> None:
    (destination / _fixture_name(endpoint)).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _synthesize_month_replay(recording: Path, destination: Path) -> None:
    """Expose normalized month-wide facts through ordinary replay endpoints."""
    month_path = recording / "month-wide.json.gz"
    if not month_path.is_file():
        return
    with gzip.open(month_path, "rt", encoding="utf-8") as stream:
        month = json.load(stream)
    manifest = load(recording / "manifest.json")
    repository = str(manifest["repository"])
    workflow_endpoint = f"repos/{repository}/actions/workflows?per_page=100"
    workflow_fixture = destination / _fixture_name(workflow_endpoint)
    if not workflow_fixture.is_file():
        return
    workflow_payload = load(workflow_fixture)
    workflows = {
        str(row.get("name") or row.get("path") or ""): row
        for row in workflow_payload.get("workflows") or [] if isinstance(row, dict)
    }

    existing_heads: dict[int, set[str]] = {}
    existing_oldest: dict[int, str] = {}
    for workflow in workflows.values():
        workflow_id = int(workflow.get("id") or 0)
        endpoint = f"repos/{repository}/actions/workflows/{workflow_id}/runs?per_page=100"
        fixture = destination / _fixture_name(endpoint)
        if fixture.is_file():
            payload = load(fixture)
            existing_heads[workflow_id] = {
                str(run.get("head_sha") or "")
                for run in payload.get("workflow_runs") or [] if isinstance(run, dict)
            }
            created = sorted(
                str(run.get("created_at") or "")
                for run in payload.get("workflow_runs") or []
                if isinstance(run, dict) and run.get("created_at")
            )
            if created:
                existing_oldest[workflow_id] = created[0]

    runs_by_workflow: dict[int, list[dict[str, Any]]] = {}
    run_serial = 0
    job_serial = 0
    commits = sorted(
        (row for row in month.get("pr_commits") or [] if isinstance(row, dict)),
        key=lambda row: str(row.get("created_at") or ""), reverse=True,
    )
    for commit in commits:
        jobs_by_workflow: dict[str, list[dict[str, Any]]] = {}
        for job in commit.get("jobs") or []:
            if isinstance(job, dict):
                jobs_by_workflow.setdefault(str(job.get("workflow") or ""), []).append(job)
        for workflow_name, jobs in sorted(jobs_by_workflow.items()):
            workflow = workflows.get(workflow_name)
            if not workflow:
                continue
            workflow_id = int(workflow.get("id") or 0)
            head_sha = str(commit.get("sha") or "")
            created_at = str(commit.get("created_at") or "")
            if (not workflow_id or not head_sha
                    or head_sha in existing_heads.get(workflow_id, set())
                    or (existing_oldest.get(workflow_id)
                        and created_at >= existing_oldest[workflow_id])):
                continue
            run_serial += 1
            run_id = 71_000_000 + run_serial
            starts = sorted(str(job.get("started_at") or "") for job in jobs if job.get("started_at"))
            ends = sorted(str(job.get("completed_at") or "") for job in jobs if job.get("completed_at"))
            completed = bool(commit.get("completed"))
            failed = any(job.get("conclusion") not in (None, "success", "skipped") for job in jobs)
            runs_by_workflow.setdefault(workflow_id, []).append({
                "id": run_id,
                "name": workflow_name,
                "head_branch": "golden-recording",
                "head_sha": head_sha,
                "path": workflow.get("path"),
                "display_title": "Recorded pull request commit",
                "run_number": run_serial,
                "event": "pull_request",
                "status": "completed" if completed else "in_progress",
                "conclusion": ("failure" if failed else "success") if completed else None,
                "workflow_id": workflow_id,
                "created_at": commit.get("created_at"),
                "updated_at": ends[-1] if ends else commit.get("created_at"),
                "run_started_at": starts[0] if starts else commit.get("created_at"),
                "run_attempt": 1,
                "head_commit": {"id": head_sha, "timestamp": commit.get("created_at")},
                "pull_requests": [],
            })
            recorded_jobs: list[dict[str, Any]] = []
            for source_job in jobs:
                job_serial += 1
                recorded_jobs.append({
                    "id": 72_000_000 + job_serial,
                    "run_id": run_id,
                    "name": source_job.get("name"),
                    "status": source_job.get("status"),
                    "conclusion": source_job.get("conclusion"),
                    "started_at": source_job.get("started_at"),
                    "completed_at": source_job.get("completed_at"),
                    "runner_name": "golden-recording",
                    "steps": [],
                })
            for offset in range(0, len(recorded_jobs), 100):
                page = 1 + offset // 100
                page_suffix = "" if page == 1 else f"&page={page}"
                payload = {
                    "total_count": len(recorded_jobs),
                    "jobs": recorded_jobs[offset:offset + 100],
                }
                for filter_suffix in ("", "&filter=all"):
                    _write_fixture(
                        destination,
                        f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
                        f"{filter_suffix}{page_suffix}",
                        payload,
                    )

    for workflow_id, runs in runs_by_workflow.items():
        total = len(runs) + len(existing_heads.get(workflow_id, set()))
        for offset in range(0, len(runs), 100):
            page = 2 + offset // 100
            payload = {"total_count": total, "workflow_runs": runs[offset:offset + 100]}
            for suffix in ("", "&event=pull_request"):
                _write_fixture(
                    destination,
                    f"repos/{repository}/actions/workflows/{workflow_id}/runs"
                    f"?per_page=100{suffix}&page={page}",
                    payload,
                )


def _dynamic_replay_copy(recording: Path, destination: Path) -> None:
    """Copy fixtures and alias the moving 30-day query to today's spelling."""
    source = recording / "api"
    destination.mkdir(parents=True)
    for path in source.iterdir():
        if path.suffix == ".gz":
            target = destination / path.name[:-3]
            with gzip.open(path, "rb") as compressed, target.open("wb") as plain:
                shutil.copyfileobj(compressed, plain)
        elif path.is_file():
            shutil.copy2(path, destination / path.name)
    stamp = re.compile(r"created_gte\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2}Z")
    targets = [
        (datetime.now(timezone.utc) - timedelta(days=30) + timedelta(seconds=offset))
        .strftime("created_gte%Y-%m-%dT%H_%M_%SZ")
        for offset in range(-4, 5)
    ]
    for path in list(destination.iterdir()):
        if not path.is_file() or not stamp.search(path.name):
            continue
        for target in targets:
            alias = destination / stamp.sub(target, path.name)
            if not alias.exists():
                shutil.copy2(path, alias)
    _synthesize_month_replay(recording, destination)


def replay(recording: Path, work: Path, *, repo_root: Path) -> tuple[dict[str, Any], str]:
    """Run scan, collect, render and the full verifier on one recording."""
    manifest = load(recording / "manifest.json")
    checkout = work / "checkout"
    shutil.copytree(recording / "checkout", checkout)
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "golden-test",
        "GIT_AUTHOR_EMAIL": "golden-test@example.invalid",
        "GIT_COMMITTER_NAME": "golden-test",
        "GIT_COMMITTER_EMAIL": "golden-test@example.invalid",
    })
    commands = [
        ["git", "init", "-q"],
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        ["git", "remote", "add", "origin", str(manifest["origin"])],
        ["git", "add", ".github"],
        ["git", "commit", "-q", "-m", "golden snapshot"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=checkout, env=env, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"{' '.join(command[:2])} failed: {result.stderr}")
    fixtures = work / "api"
    _dynamic_replay_copy(recording, fixtures)
    env["CI_SPEEDUP_GH_FIXTURES"] = str(fixtures)
    env.pop("CI_SPEEDUP_GH_RECORD", None)
    skill = repo_root / "skills" / "ci-speedup"
    scripts = skill / "scripts"
    skill_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    findings_path = work / "findings.json"
    report_path = work / "report.md"
    run = subprocess.run([
        sys.executable, str(scripts / "run.py"), "--root", str(checkout),
        "--out", str(findings_path), "--repo", str(manifest["repository"]),
        "--commit-sha", str(manifest["commit"]), "--skill-commit-sha", skill_commit,
    ], env=env, text=True, capture_output=True, timeout=300)
    if run.returncode:
        raise RuntimeError(f"run.py failed:\n{run.stdout}\n{run.stderr}")
    findings = load(findings_path)
    render = subprocess.run([
        sys.executable, str(scripts / "blocking_path.py"),
        "--in", str(findings_path), "--out", str(report_path),
    ], env=env, text=True, capture_output=True, timeout=120)
    if render.returncode:
        raise RuntimeError(f"render failed:\n{render.stdout}\n{render.stderr}")
    verify = subprocess.run([
        sys.executable, str(skill / "tests" / "verify_report.py"),
        "--report", str(report_path), "--findings", str(findings_path),
    ], env=env, text=True, capture_output=True, timeout=120)
    if verify.returncode:
        failed = [
            line.removeprefix("FAIL  ").strip()
            for line in verify.stdout.splitlines()
            if line.startswith("FAIL  ")
        ]
        findings["_golden_verifier_failures"] = failed or ["unknown verifier failure"]
    return findings, report_path.read_text(encoding="utf-8")
