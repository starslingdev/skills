#!/usr/bin/env python3
"""Compare a replayed ci-speedup result with independent golden facts."""

from __future__ import annotations

import json
import gzip
import hashlib
import importlib.util
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
    checks = [
        row for row in critical.get("checks") or []
        if isinstance(row, dict) and row.get("required_reachable") is not False]
    checks.sort(key=lambda row: (-int(row.get("pole_n") or 0), -float(row.get("p50_s") or 0), str(row.get("name") or "")))
    wall = critical.get("commit_wall_clock") or {}
    # The chain summary covers the representative PR population.  The direct
    # wall sample may contain only a handful of commits whose check-run API
    # rows happened to be complete, so it is a fallback rather than the primary
    # comparison surface.
    typical = (critical.get("chain_summary") or {}).get("makespan_p50_s")
    if typical is None:
        typical = wall.get("p50_s")
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
    expected_depth = expected_name.count(" / ")
    candidates = [
        name for name in actual_gates
        if (expected_name.endswith(" / " + name)
            and name.count(" / ") >= max(0, expected_depth - 1))
        or name.endswith(" / " + expected_name)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _bottom_line(report: str) -> str:
    for line in report.splitlines():
        if line.lstrip().startswith("> **Bottom line.**"):
            return line
    return ""


def compare(findings: dict[str, Any], expected: dict[str, Any], report: str = "",
            *, recording: Path | None = None) -> list[str]:
    oracle = (_oracle_for_engine_span(recording, findings)
              if recording is not None else expected.get("oracle") or {})
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
    oracle_rows = [row for row in oracle.get("gates") or [] if isinstance(row, dict)]
    # A bounded sample cannot support an exact ordering among low-frequency
    # gates whose shares fit inside the comparison's uncertainty band.
    ordered_names = {
        str(row.get("check") or "") for row in oracle_rows
        if float(row.get("share") or 0.0) >= 0.15
    }
    expected_order = [
        name for name in oracle.get("gate_order") or []
        if not oracle_rows or str(name) in ordered_names]
    comparable = [
        match for name in expected_order
        if (match := _matching_gate(str(name), actual["gates"])) is not None
    ]
    actual_order = [name for name in actual["gate_order"] if name in set(comparable)]
    if len(comparable) >= 2 and actual_order != comparable:
        differences.append(
            "gate order differs: engine=" + repr(actual_order) + ", oracle=" + repr(comparable)
        )
    expected_wait = float(oracle.get("typical_pr_wait_s") or 0.0)
    actual_wait = float(actual["typical_pr_wait_s"] or 0.0)
    if expected_wait and abs(actual_wait - expected_wait) > max(60.0, expected_wait * 0.35):
        differences.append(
            f"typical PR wait differs: engine={actual_wait:.1f}s, oracle={expected_wait:.1f}s"
        )
    expected_gates = oracle_rows
    if expected_gates:
        lead_name = str(expected_gates[0].get("check") or "")
        if lead_name and _matching_gate(lead_name, actual["gates"]) is None:
            differences.append(f"independent leading gate absent: {lead_name!r}")
    for row in expected_gates:
        expected_name = str(row.get("check") or "")
        actual_name = _matching_gate(expected_name, actual["gates"])
        if actual_name is None:
            if float(row.get("share") or 0.0) >= 0.05 and expected_name != lead_name:
                differences.append(f"frequent gate absent: {expected_name!r}")
            continue
        actual_gate = actual["gates"][actual_name]
        oracle_share = float(row.get("share") or 0.0)
        # The oracle population excludes commits whose independent job evidence
        # is unavailable.  Compare counts over that same eligible denominator;
        # the report still discloses the broader sampled-pr count separately.
        actual_share = (float(actual_gate["last_on"])
                        / max(1, expected_completed))
        if abs(actual_share - oracle_share) > 0.15:
            differences.append(
                f"gate share differs for {expected_name!r}: "
                f"engine={actual_share:.3f}, oracle={oracle_share:.3f}"
            )
        oracle_p50 = float(row.get("p50_s") or 0.0)
        actual_p50 = float(actual_gate["p50_s"] or 0.0)
        if oracle_p50 and abs(actual_p50 - oracle_p50) > max(240.0, oracle_p50 * 0.35):
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
    if bottom_line and bottom_line not in _bottom_line(report):
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


def _oracle_for_engine_span(recording: Path, findings: dict[str, Any]) -> dict[str, Any]:
    """Recompute independent facts over the same public sample definition.

    The month recording is broader than the engine's bounded candidate walk.
    Comparing a five-day engine sample with a 30-day oracle, or comparing
    required/reachable checks with every terminal bookkeeping job, creates a
    definition mismatch rather than an implementation check.
    """
    with gzip.open(recording / "month-wide.json.gz", "rt", encoding="utf-8") as stream:
        month = json.load(stream)
    critical = findings.get("pr_critical_path") or {}
    first = str(critical.get("sample_span_first") or "")
    last = str(critical.get("sample_span_last") or "")
    engine_names = {
        str(row.get("name") or "")
        for row in critical.get("checks") or []
        if isinstance(row, dict) and row.get("required_reachable") is not False
    }
    selected_shas = {
        str(row.get("sha") or "")
        for row in critical.get("chain_facts") or [] if isinstance(row, dict)
    }
    gate_by_sha = {
        str(row.get("sha") or ""): str((row.get("chain") or [""])[-1])
        for row in critical.get("chain_facts") or []
        if isinstance(row, dict) and row.get("sha") and row.get("chain")
    }
    month_shas = {
        str(row.get("sha") or "")
        for row in month.get("pr_commits") or [] if isinstance(row, dict)
    }
    exact_selection = selected_shas & month_shas

    def included_job(job: dict[str, Any]) -> bool:
        workflow = str(job.get("workflow") or "").strip()
        name = str(job.get("name") or "").strip()
        qualified = f"{workflow} / {name}" if workflow and name else name or workflow
        return any(
            candidate == qualified or candidate == name
            or qualified.endswith(" / " + candidate)
            or candidate.endswith(" / " + name)
            for candidate in engine_names
        )

    commits = []
    for source in month.get("pr_commits") or []:
        if not isinstance(source, dict):
            continue
        created = str(source.get("created_at") or "")
        if exact_selection:
            if str(source.get("sha") or "") not in exact_selection:
                continue
        elif (first and created < first) or (last and created > last):
            continue
        row = dict(source)
        included_jobs = [
            job for job in source.get("jobs") or []
            if isinstance(job, dict) and included_job(job)]
        row["jobs"] = included_jobs
        included_run_keys = {
            str(job.get("run_key")) for job in included_jobs if job.get("run_key")}
        included_run_ids = {
            int(job.get("run_id") or 0) for job in included_jobs if job.get("run_id")}
        included_workflows = {
            str(job.get("workflow") or "") for job in included_jobs
            if job.get("workflow")}
        row["runs"] = [
            run for run in source.get("runs") or []
            if isinstance(run, dict) and (
                (run.get("run_key") and str(run.get("run_key")) in included_run_keys)
                or (run.get("id") and int(run.get("id") or 0) in included_run_ids)
                or (not run.get("run_key") and not run.get("id")
                    and str(run.get("workflow") or "") in included_workflows)
            )
        ]
        commits.append(row)
    scoped = dict(month)
    scoped["pr_commits"] = commits

    oracle_path = Path(__file__).with_name("oracle.py")
    spec = importlib.util.spec_from_file_location("golden_span_oracle", oracle_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The engine's gate population has no duration-only bookkeeping cutoff.
    # Eligibility comes from required/reachable checks above.
    return module.compute(
        scoped, bookkeeping_cutoff_s=0, gate_by_sha=gate_by_sha)


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

    # The sanitized month and copied API pages used to name the same commit in
    # two different ways (``commit-NNNNNN`` versus a hexadecimal SHA).  Normalize
    # copied page rows to the month identity by workflow + run timestamp before
    # deduplication, and alias the corresponding check-run fixture.
    commits_by_workflow_time: dict[tuple[str, str], set[str]] = {}
    for commit in month.get("pr_commits") or []:
        if not isinstance(commit, dict):
            continue
        for run in commit.get("runs") or []:
            if isinstance(run, dict) and run.get("workflow") and run.get("created_at"):
                commits_by_workflow_time.setdefault(
                    (str(run["workflow"]), str(run["created_at"])), set()
                ).add(str(commit.get("sha") or ""))

    existing_heads: dict[int, set[str]] = {}
    existing_oldest: dict[int, str] = {}
    existing_times: dict[int, set[str]] = {}
    existing_pages: dict[int, dict[str, Any]] = {}
    for workflow in workflows.values():
        workflow_id = int(workflow.get("id") or 0)
        endpoint = f"repos/{repository}/actions/workflows/{workflow_id}/runs?per_page=100"
        fixture = destination / _fixture_name(endpoint)
        if fixture.is_file():
            payload = load(fixture)
            for run in payload.get("workflow_runs") or []:
                if not isinstance(run, dict):
                    continue
                replacements = commits_by_workflow_time.get((
                    str(workflow.get("name") or workflow.get("path") or ""),
                    str(run.get("created_at") or ""),
                ), set())
                replacement = next(iter(replacements)) if len(replacements) == 1 else ""
                old_sha = str(run.get("head_sha") or "")
                if replacement:
                    run["head_sha"] = replacement
                    if isinstance(run.get("head_commit"), dict):
                        run["head_commit"]["id"] = replacement
                    old_token = _fixture_name(
                        f"repos/{repository}/commits/{old_sha}/check-runs?per_page=100")
                    new_token = _fixture_name(
                        f"repos/{repository}/commits/{replacement}/check-runs?per_page=100")
                    old_check = destination / old_token
                    new_check = destination / new_token
                    if old_check.is_file() and not new_check.exists():
                        shutil.copy2(old_check, new_check)
            fixture.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
            existing_pages[workflow_id] = payload
            existing_heads[workflow_id] = {
                str(run.get("head_sha") or "")
                for run in payload.get("workflow_runs") or [] if isinstance(run, dict)
            }
            existing_times[workflow_id] = {
                str(run.get("created_at") or "")
                for run in payload.get("workflow_runs") or []
                if isinstance(run, dict) and run.get("created_at")
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
        source_runs = [
            run for run in commit.get("runs") or [] if isinstance(run, dict)]
        source_jobs = [
            job for job in commit.get("jobs") or [] if isinstance(job, dict)]
        if not source_runs:
            # Schema-v1 fixtures originally stored only workflow-tagged jobs.
            # Preserve replay compatibility by creating one run per workflow;
            # new recordings use explicit run_key rows and never take this path.
            for workflow_name in sorted({
                    str(job.get("workflow") or "") for job in source_jobs}):
                matching = [job for job in source_jobs
                            if str(job.get("workflow") or "") == workflow_name]
                source_runs.append({
                    "workflow": workflow_name, "status": "completed",
                    "created_at": commit.get("created_at"),
                    "updated_at": max((str(job.get("completed_at") or "")
                                       for job in matching), default=commit.get("created_at")),
                    "jobs_recorded": True,
                })
        jobs_by_run: dict[int, list[dict[str, Any]]] = {
            index: [] for index in range(len(source_runs))}

        # New recordings carry an explicit run_key.  Older public recordings
        # predate that field, so bind their jobs to the same-workflow run whose
        # recorded time window is closest.  Never pool every job with the same
        # workflow name into every run: matrix/retry runs are distinct API rows.
        run_key_to_index = {
            str(run.get("run_key")): index
            for index, run in enumerate(source_runs) if run.get("run_key")}
        for job in source_jobs:
            tagged = run_key_to_index.get(str(job.get("run_key") or ""))
            candidates = [
                index for index, run in enumerate(source_runs)
                if str(run.get("workflow") or "")
                == str(job.get("workflow") or "")]
            if tagged is not None:
                chosen = tagged
            elif candidates:
                job_end = str(job.get("completed_at") or job.get("started_at") or "")
                chosen = min(
                    candidates,
                    key=lambda index: (
                        abs((datetime.fromisoformat(
                            str(source_runs[index].get("updated_at")
                                or source_runs[index].get("created_at"))
                            .replace("Z", "+00:00"))
                             - datetime.fromisoformat(job_end.replace("Z", "+00:00")))
                            .total_seconds())
                        if job_end and (source_runs[index].get("updated_at")
                                        or source_runs[index].get("created_at"))
                        else float("inf"),
                        index,
                    ),
                )
            else:
                continue
            jobs_by_run[chosen].append(job)

        for run_index, source_run in enumerate(source_runs):
            workflow_name = str(source_run.get("workflow") or "")
            jobs = jobs_by_run.get(run_index) or []
            workflow = workflows.get(workflow_name)
            if not workflow:
                continue
            workflow_id = int(workflow.get("id") or 0)
            head_sha = str(commit.get("sha") or "")
            created_at = str(source_run.get("created_at")
                             or commit.get("created_at") or "")
            if (not workflow_id or not head_sha
                    or created_at in existing_times.get(workflow_id, set())
                    or (existing_oldest.get(workflow_id)
                        and created_at >= existing_oldest[workflow_id])):
                continue
            run_serial += 1
            run_id = 71_000_000 + run_serial
            starts = sorted(str(job.get("started_at") or "") for job in jobs
                            if job.get("started_at"))
            ends = sorted(str(job.get("completed_at") or "") for job in jobs
                          if job.get("completed_at"))
            completed = (bool(commit.get("completed"))
                         and str(source_run.get("status") or "completed") == "completed")
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
                "created_at": created_at,
                "updated_at": source_run.get("updated_at") or (
                    ends[-1] if ends else created_at),
                "run_started_at": starts[0] if starts else created_at,
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
            jobs_recorded = (bool(source_run.get("jobs_recorded"))
                             if "jobs_recorded" in source_run else bool(jobs))
            if not jobs_recorded:
                # No fixture means the replay client observes a failed read.
                # An empty successful payload would turn missing evidence into
                # a clean run and shrink the gate sample silently.
                continue
            for offset in range(0, max(1, len(recorded_jobs)), 100):
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
        runs.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        total = len(runs) + len(existing_heads.get(workflow_id, set()))
        first_payload = dict(existing_pages.get(workflow_id) or {})
        first_rows = list(first_payload.get("workflow_runs") or [])
        first_count = min(len(runs), max(0, 100 - len(first_rows)))
        if first_count:
            first_payload["total_count"] = total
            first_payload["workflow_runs"] = first_rows + runs[:first_count]
            for suffix in ("", "&event=pull_request"):
                _write_fixture(
                    destination,
                    f"repos/{repository}/actions/workflows/{workflow_id}/runs"
                    f"?per_page=100{suffix}",
                    first_payload,
                )
        remaining = runs[first_count:]
        for offset in range(0, len(remaining), 100):
            page = 2 + offset // 100
            payload = {"total_count": total,
                       "workflow_runs": remaining[offset:offset + 100]}
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


def restore_snapshot_names(checkout: Path) -> None:
    """Give ``<name>.golden`` snapshot manifests their real names back.

    record.py stores them renamed so Dependabot never proposes bumping a frozen
    third-party snapshot; the engine needs ``package.json`` and friends.
    """
    for stored in sorted(checkout.rglob("*.golden")):
        stored.rename(stored.with_name(stored.name.removesuffix(".golden")))


def replay(recording: Path, work: Path, *, repo_root: Path) -> tuple[dict[str, Any], str]:
    """Run scan, collect, render and the full verifier on one recording."""
    manifest = load(recording / "manifest.json")
    checkout = work / "checkout"
    shutil.copytree(recording / "checkout", checkout)
    restore_snapshot_names(checkout)
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
