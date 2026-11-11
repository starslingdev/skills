#!/usr/bin/env python3
"""Record a public repository for the offline ci-speedup golden set.

The recorder is resumable.  It stores raw responses through the engine's
record/replay seam, stores a normalized month-wide PR/job history for the
independent oracle, and copies the audited workflow snapshot.  It never writes
an expected file.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
_SCRIPTS = _REPO_ROOT / "skills" / "ci-speedup" / "scripts"
_SECRET = re.compile(
    r"(?i)(?:authorization:\s*(?:bearer|token)|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+)"
)
_GITHUB_REPOSITORY_REFERENCE = re.compile(
    r"(?i)(?:(?:https?://)?(?:www\.)?github\.com[/:]|"
    r"(?:https?://)?api\.github\.com/repos/)"
    r"(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"|(?<![A-Za-z0-9_.-])"
    r"(?P<numbered>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:#!|#|!)(?:pull/|issues/)?\d+\b"
)
_LONG_NUMBER = re.compile(r"(?<![0-9])[0-9]{9,}(?![0-9])")
_PRIVATE_SHAPED_PARTS = (
    "sess" + "ion_",
    "/" + "users/",
    "/" + "home/",
    "/private" + "/tmp",
    ".work" + "trees",
)
_PRIVATE_SHAPED_REPLACEMENTS = dict(zip(
    _PRIVATE_SHAPED_PARTS,
    ("run_state_", "/accounts/", "/workspace/", "/temporary", ".workspaces"),
    strict=True,
))
_PRIVATE_SHAPED_PUBLIC_LITERAL = re.compile(
    "(?i)" + "|".join(re.escape(value) for value in _PRIVATE_SHAPED_PARTS)
)


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


def _gh_json(endpoint: str) -> Any:
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(3):
        result = _run(["gh", "api", endpoint], timeout=120)
        if not result.returncode:
            return json.loads(result.stdout)
        if "HTTP 502" in result.stderr:
            raise RuntimeError("GitHub API read failed: " + result.stderr.splitlines()[0])
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    assert result is not None
    raise RuntimeError("GitHub API read failed: " + (result.stderr.splitlines() or ["unknown error"])[0])


def _pages(endpoint: str, key: str, *, per_page: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        try:
            payload = _gh_json(f"{endpoint}{separator}per_page={per_page}&page={page}")
        except RuntimeError as exc:
            if per_page == 100 and "HTTP 502" in str(exc):
                return _pages(endpoint, key, per_page=30)
            raise
        batch = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError(f"GitHub API page did not contain {key}")
        rows.extend(row for row in batch if isinstance(row, dict))
        if len(batch) < per_page:
            return rows
        page += 1


def _job_is_not_bookkeeping(job: dict[str, Any]) -> bool:
    try:
        start = datetime.fromisoformat(str(job.get("started_at") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(job.get("completed_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (end - start).total_seconds() >= 60


def _month_history(repository: str, captured_at: datetime, state_path: Path) -> dict[str, Any]:
    start = captured_at - timedelta(days=30)
    prior: dict[str, Any] = {}
    if state_path.exists():
        prior = json.loads(state_path.read_text(encoding="utf-8"))
    prior_by_sha: dict[str, dict[str, Any]] = {}
    for row in prior.get("pr_commits") or []:
        if isinstance(row, dict) and row.get("sha"):
            prior_by_sha[str(row["sha"])] = row

    if prior.get("run_inventory_complete"):
        by_sha = prior_by_sha
        run_payload_by_id = {
            int(run.get("id") or 0): {"id": int(run.get("id") or 0)}
            for commit in by_sha.values()
            for run in commit.get("runs") or []
        }
    else:
        # The Actions run-list endpoint caps a broad filtered query at 1,000
        # rows on busy repositories. Walk one-day slices so "30 days" means
        # the whole month rather than the newest 1,000 workflow runs.
        runs_by_id: dict[int, dict[str, Any]] = {}
        cursor = start
        slice_endpoints: list[str] = []
        while cursor < captured_at:
            right = min(cursor + timedelta(days=1), captured_at)
            created = (
                cursor.strftime("%Y-%m-%dT%H:%M:%SZ") + ".." +
                right.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            slice_endpoints.append(
                f"repos/{repository}/actions/runs?event=pull_request&created={created}"
            )
            cursor = right
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(_pages, endpoint, "workflow_runs") for endpoint in slice_endpoints]
            for future in as_completed(futures):
                for run in future.result():
                    run_id = int(run.get("id") or 0)
                    if run_id:
                        runs_by_id[run_id] = run
        runs = list(runs_by_id.values())
        workflow_names = {
            int(row.get("id") or 0): str(row.get("name") or row.get("path") or "workflow")
            for row in _pages(f"repos/{repository}/actions/workflows?", "workflows")
        }
        by_sha: dict[str, dict[str, Any]] = {}
        for run in runs:
            sha = str(run.get("head_sha") or "")
            if not sha:
                continue
            commit = by_sha.setdefault(sha, {
                "sha": sha,
                "created_at": run.get("created_at"),
                "completed": True,
                "runs": [],
                "jobs": [],
            })
            run_id = int(run.get("id") or 0)
            status = str(run.get("status") or "")
            commit["completed"] = bool(commit["completed"] and status == "completed")
            workflow = workflow_names.get(
                int(run.get("workflow_id") or 0), str(run.get("name") or "workflow")
            )
            prior_commit = prior_by_sha.get(sha) or {}
            prior_run = next(
                (row for row in prior_commit.get("runs") or []
                 if int(row.get("id") or 0) == run_id),
                {},
            )
            commit["runs"].append({
                "id": run_id,
                "workflow": workflow,
                "status": status,
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "jobs_recorded": bool(prior_run.get("jobs_recorded")),
            })
            if prior_run.get("jobs_recorded"):
                commit["jobs"].extend(
                    job for job in prior_commit.get("jobs") or []
                    if int(job.get("run_id") or 0) == run_id
                )
        run_payload_by_id = {int(run.get("id") or 0): run for run in runs}

    # The last eligible job on a commit must belong to its latest workflow run
    # that contains work above the bookkeeping cutoff. Start with the latest
    # run; only walk backward when that run proves to contain short bookkeeping
    # jobs alone.
    def pending_runs() -> list[tuple[dict[str, Any], dict[str, Any], str]]:
        pending: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for commit in by_sha.values():
            if not commit["completed"]:
                continue
            ordered = sorted(
                commit["runs"], key=lambda row: str(row.get("updated_at") or ""), reverse=True
            )
            for row in ordered:
                run_id = int(row.get("id") or 0)
                if row.get("jobs_recorded"):
                    recorded_jobs = [
                        job for job in commit["jobs"]
                        if int(job.get("run_id") or 0) == run_id
                    ]
                    if any(_job_is_not_bookkeeping(job) for job in recorded_jobs):
                        break
                    continue
                run = run_payload_by_id.get(run_id)
                if run is not None:
                    pending.append((commit, run, str(row.get("workflow") or "workflow")))
                break
        return pending

    def fetch_jobs(item: tuple[dict[str, Any], dict[str, Any], str]) -> tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]]:
        commit, run, workflow = item
        run_id = int(run.get("id") or 0)
        jobs = _pages(f"repos/{repository}/actions/runs/{run_id}/jobs?", "jobs")
        return commit, run, workflow, jobs

    def write_state() -> None:
        state = {
            "schema_version": 1,
            "repository": repository,
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "window": {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": captured_at.isoformat().replace("+00:00", "Z"),
            },
            "run_inventory_complete": True,
            "pr_commits": sorted(by_sha.values(), key=lambda row: str(row.get("created_at") or "")),
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(state_path)

    completed_fetches = 0
    while pending := pending_runs():
        for offset in range(0, len(pending), 200):
            batch = pending[offset:offset + 200]
            with ThreadPoolExecutor(max_workers=40) as pool:
                futures = [pool.submit(fetch_jobs, item) for item in batch]
                results = []
                failures = 0
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except RuntimeError:
                        failures += 1
            for commit, run, workflow, jobs in results:
                for job in jobs:
                    commit["jobs"].append({
                        "id": int(job.get("id") or 0),
                        "run_id": int(run.get("id") or 0),
                        "workflow": workflow,
                        "name": job.get("name"),
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion"),
                        "started_at": job.get("started_at"),
                        "completed_at": job.get("completed_at"),
                    })
                for row in commit["runs"]:
                    if int(row.get("id") or 0) == int(run.get("id") or 0):
                        row["jobs_recorded"] = True
                completed_fetches += 1
            write_state()
            if failures and not results:
                raise RuntimeError("every job-list request in a recording batch failed")
    write_state()
    return json.loads(state_path.read_text(encoding="utf-8"))


def _scan_and_cap_logs(recording_dir: Path, cap_bytes: int) -> list[str]:
    skipped: list[str] = []
    for path in recording_dir.rglob("*.txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SECRET.search(text):
            path.unlink()
            skipped.append(path.name)
            continue
        raw = text.encode("utf-8")
        if len(raw) > cap_bytes:
            path.write_bytes(raw[:cap_bytes].decode("utf-8", errors="ignore").encode("utf-8"))
    return skipped


def _compress_api_fixtures(api_dir: Path) -> None:
    for path in api_dir.iterdir():
        if not path.is_file() or path.suffix == ".gz":
            continue
        compressed = path.with_name(path.name + ".gz")
        with path.open("rb") as source, gzip.open(compressed, "wb", compresslevel=9) as target:
            shutil.copyfileobj(source, target)
        path.unlink()


def _sanitize_checkout_references(snapshot: Path, repository: str) -> None:
    """Redact links to repositories other than the recorded public source."""
    source = repository.casefold()

    def replace(match: re.Match[str]) -> str:
        referenced = (match.group("repository") or match.group("numbered") or "").casefold()
        if referenced.rstrip("/.,;:!?)").removesuffix(".git") == source:
            return match.group(0)
        return "<external-public-repository>"

    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        sanitized = _GITHUB_REPOSITORY_REFERENCE.sub(replace, text)
        sanitized = _LONG_NUMBER.sub(
            lambda match: ("a1" * ((len(match.group(0)) + 1) // 2))[:len(match.group(0))],
            sanitized,
        )
        sanitized = _PRIVATE_SHAPED_PUBLIC_LITERAL.sub(
            lambda match: _PRIVATE_SHAPED_REPLACEMENTS[match.group(0).casefold()],
            sanitized,
        )
        if sanitized != text:
            path.write_text(sanitized, encoding="utf-8")


def _write_compressed_json(path: Path, payload: dict[str, Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _sanitize_api_identifiers(api_dir: Path) -> dict[str, str]:
    """Replace public API IDs longer than fixture policy permits, consistently."""
    files = [path for path in api_dir.iterdir() if path.is_file()]
    tokens: set[str] = set()
    text_by_path: dict[Path, str] = {}
    for path in files:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
                text = stream.read()
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        text_by_path[path] = text
        tokens.update(_LONG_NUMBER.findall(path.name.removesuffix(".gz")))
        tokens.update(_LONG_NUMBER.findall(text))
    mapping = {token: str(70_000_001 + index) for index, token in enumerate(sorted(tokens))}

    def replace(value: str) -> str:
        return _LONG_NUMBER.sub(lambda match: mapping[match.group(0)], value)

    for path in files:
        plain_name = path.name.removesuffix(".gz")
        target = path.with_name(replace(plain_name))
        target.write_text(replace(text_by_path[path]), encoding="utf-8")
        if target != path:
            path.unlink()
    return mapping


def _public_history(history: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in history.items() if key != "pr_commits"}
    commits: list[dict[str, Any]] = []
    for index, row in enumerate(history.get("pr_commits") or [], start=1):
        if not isinstance(row, dict):
            continue
        commits.append({
            "sha": f"commit-{index:06d}",
            "created_at": row.get("created_at"),
            "completed": bool(row.get("completed")),
            "job_lists_recorded": sum(
                bool(run.get("jobs_recorded")) for run in row.get("runs") or []
            ),
            "runs": [
                {key: value for key, value in run.items()
                 if key not in {"id", "jobs_recorded"}}
                for run in row.get("runs") or [] if isinstance(run, dict)
            ],
            "jobs": [
                {key: value for key, value in job.items()
                 if key not in {"id", "run_id"}}
                for job in row.get("jobs") or [] if isinstance(job, dict)
            ],
        })
    public["pr_commits"] = commits
    return public


def _write_manifest(repository: str, checkout: Path, output: Path, captured_at: datetime,
                    log_cap_bytes: int, skipped: list[str], history: dict[str, Any],
                    *, commit_override: str | None = None) -> None:
    origin = _run(["git", "-C", str(checkout), "remote", "get-url", "origin"])
    commit = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if origin.returncode or commit.returncode:
        raise RuntimeError("checkout must be a git clone with an origin")
    prior: dict[str, Any] = {}
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
    manifest = {
        "schema_version": 1,
        "repository": repository,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "commit": commit_override or commit.stdout.strip(),
        "origin": origin.stdout.strip(),
        "log_cap_bytes": log_cap_bytes,
        "secret_scan": "passed",
        "identifiers": "stable eight-digit fixture ids",
        "skipped_log_files": skipped or list(prior.get("skipped_log_files") or []),
        "pole_logs_recorded": len(list((output / "logs").glob("*"))),
        "pole_log_note": (
            "recorded and secret-scanned"
            if any((output / "logs").glob("*"))
            else "the public API returned no pole logs for this recording"
        ),
        "api_fixture_count": len(list((output / "api").glob("*.gz"))),
        "month_wide_pr_commits": len(history.get("pr_commits") or []),
        "month_wide_completed_pr_commits": sum(
            bool(row.get("completed")) for row in history.get("pr_commits") or []
        ),
        "month_wide_job_lists_recorded": sum(
            int(row.get("job_lists_recorded") or 0)
            for row in history.get("pr_commits") or []
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(repository: str, checkout: Path, output: Path, *, captured_at: datetime,
           log_cap_bytes: int, month_only: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if month_only:
        state_path = output / ".recording-state.json"
        history = _month_history(repository, captured_at, state_path)
        public_history = _public_history(history)
        _write_compressed_json(output / "month-wide.json.gz", public_history)
        (output / "month-wide.json").unlink(missing_ok=True)
        _write_manifest(repository, checkout, output, captured_at, log_cap_bytes, [], public_history)
        state_path.unlink(missing_ok=True)
        return
    snapshot = output / "checkout"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    workflows = checkout / ".github" / "workflows"
    if not workflows.is_dir():
        raise RuntimeError("checkout has no .github/workflows directory")
    shutil.copytree(workflows, snapshot / ".github" / "workflows")
    for name in ("package.json", "pnpm-workspace.yaml", "pyproject.toml", "Cargo.toml", "go.mod"):
        source = checkout / name
        if source.is_file():
            (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, snapshot / name)
    _sanitize_checkout_references(snapshot, repository)
    origin = _run(["git", "-C", str(checkout), "remote", "get-url", "origin"])
    commit = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if origin.returncode or commit.returncode:
        raise RuntimeError("checkout must be a git clone with an origin")
    env = dict(os.environ)
    env["CI_SPEEDUP_GH_RECORD"] = str(output / "api")
    env.pop("CI_SPEEDUP_GH_FIXTURES", None)
    findings = output / "recorded-findings.json"
    run = _run([
        sys.executable, str(_SCRIPTS / "run.py"), "--root", str(checkout),
        "--out", str(findings), "--repo", repository, "--with-logs",
        "--commit-sha", commit.stdout.strip(),
    ], env=env)
    (output / "record.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (output / "record.stderr.txt").write_text(run.stderr, encoding="utf-8")
    if run.returncode:
        raise RuntimeError("ci-speedup recording run failed")
    skipped = _scan_and_cap_logs(output, log_cap_bytes)
    data_dir = output / "recorded-findings.data"
    logs_dir = output / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)
    if data_dir.exists():
        data_dir.rename(logs_dir)
    identifier_mapping = _sanitize_api_identifiers(output / "api")
    _compress_api_fixtures(output / "api")
    state_path = output / ".recording-state.json"
    history = _month_history(repository, captured_at, state_path)
    public_history = _public_history(history)
    _write_compressed_json(output / "month-wide.json.gz", public_history)
    public_commit = _LONG_NUMBER.sub(
        lambda match: identifier_mapping[match.group(0)], commit.stdout.strip()
    )
    _write_manifest(
        repository, checkout, output, captured_at, log_cap_bytes, skipped,
        public_history, commit_override=public_commit,
    )
    state_path.unlink(missing_ok=True)
    findings.unlink(missing_ok=True)
    (output / "record.stdout.txt").unlink(missing_ok=True)
    (output / "record.stderr.txt").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--log-cap-bytes", type=int, default=200_000)
    parser.add_argument("--month-only", action="store_true", help="Resume only the normalized month-wide job walk")
    args = parser.parse_args(argv)
    captured = datetime.fromisoformat(args.captured_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    record(args.repository, args.checkout.resolve(), args.output.resolve(), captured_at=captured,
           log_cap_bytes=args.log_cap_bytes, month_only=args.month_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
