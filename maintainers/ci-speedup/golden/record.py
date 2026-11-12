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
import hashlib
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
# Dependency manifests copied into a checkout snapshot.  They are stored as
# ``<name>.golden`` so Dependabot never proposes bumping a frozen third-party
# snapshot; ``compare.replay`` restores the real name before the engine runs.
SNAPSHOT_MANIFESTS = ("package.json", "pnpm-workspace.yaml", "pyproject.toml", "Cargo.toml", "go.mod")
SNAPSHOT_SUFFIX = ".golden"
# A busy repository can have hundreds of bookkeeping-only workflow runs before
# five calendar days are represented.  Keep recording bounded; unrecorded job
# lists replay as failed reads, never as successful empty payloads.
MONTH_JOB_LIST_READ_CAP = 400
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

# Contributor identities.  Recorded gh payloads carry the names, logins and
# email addresses of third-party contributors (commit authors and committers,
# run actors, commit trailers).  The engine and the oracle read none of them,
# so the recorder replaces every one with a fixed placeholder.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CONTRIBUTOR_NAME = "contributor"
CONTRIBUTOR_EMAIL = "contributor@example.invalid"
CONTRIBUTOR_LOGIN = re.compile(r"contributor-\d{4}")
# Addresses that are not a person: git's SSH user, and role inboxes published
# in the third-party manifests and workflows the golden checkouts copy.
_ALLOWED_EMAILS = frozenset({
    "git@github.com",
    CONTRIBUTOR_EMAIL,
    "infra+ci@vercel.com",
    "hello@home-assistant.io",
})


def email_addresses(text: str) -> list[str]:
    """Email-shaped tokens in *text* that are a contributor's, not a role address.

    ``name@1.2.3.patch`` (pnpm patch file names) is not an address.
    """
    return [
        match for match in _EMAIL.findall(text)
        if match not in _ALLOWED_EMAILS and not match.endswith(".patch")
    ]


def _is_person(user: dict[str, Any]) -> bool:
    login = user.get("login")
    return (
        isinstance(login, str)
        and user.get("type") not in {"Bot", "Organization"}
        and not login.endswith("[bot]")
    )


def identity_leaks(payload: Any, path: str = "$", *, keep: frozenset[str] = frozenset()) -> list[str]:
    """Every contributor identity left in a decoded JSON payload.

    *keep* names logins that stay as recorded: the audited repository's owner.
    """
    leaks: list[str] = []
    if isinstance(payload, dict):
        login = payload.get("login")
        if _is_person(payload) and login not in keep and not CONTRIBUTOR_LOGIN.fullmatch(login):
            leaks.append(f"{path}.login")
        if "email" in payload and "name" in payload and payload.get("name") != CONTRIBUTOR_NAME:
            leaks.append(f"{path}.name")
        for key, value in payload.items():
            leaks.extend(identity_leaks(value, f"{path}.{key}", keep=keep))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            leaks.extend(identity_leaks(value, f"{path}[{index}]", keep=keep))
    elif isinstance(payload, str) and email_addresses(payload):
        leaks.append(f"{path} (email)")
    return leaks


_TRAILER = re.compile(
    r"(?im)^(?P<key>(?:co-authored|signed-off|reviewed|acked|tested|reported|suggested|helped)-by):[^\n]*$"
)
_URL_KEY = re.compile(r"(?:^|_)url$")
# Fields whose value embeds a login as a token: fork ``owner/name``, merge
# messages, and branch names (many repositories name branches ``login/topic``).
_LOGIN_BEARING_KEYS = frozenset({"full_name", "message", "head_branch", "ref", "headRefName", "summary"})


def _person_logins(payload: Any, found: set[str]) -> None:
    if isinstance(payload, dict):
        if _is_person(payload):
            found.add(payload["login"])
        for value in payload.values():
            _person_logins(value, found)
    elif isinstance(payload, list):
        for value in payload:
            _person_logins(value, found)


def _login_pattern(mapping: dict[str, str], *, mention: bool = False) -> re.Pattern[str] | None:
    """Whole-token logins; with *mention*, only ``@login`` mentions (any field)."""
    if not mapping:
        return None
    alternatives = "|".join(re.escape(login) for login in mapping)  # longest first
    if mention:
        return re.compile(rf"(?<![A-Za-z0-9_.-])@(?P<login>{alternatives})(?![A-Za-z0-9-])")
    return re.compile(rf"(?<![A-Za-z0-9-])(?P<login>{alternatives})(?![A-Za-z0-9-])")


def _replace_logins(text: str, mapping: dict[str, str], pattern: re.Pattern[str] | None) -> str:
    """Swap whole-token logins inside a URL, ``owner/name`` or commit message."""
    if pattern is None:
        return text
    return pattern.sub(
        lambda match: match.group(0).replace(match.group("login"), mapping[match.group("login")]),
        text,
    )


def _scrub_string(text: str) -> str:
    text = _TRAILER.sub(lambda m: f"{m.group('key')}: {CONTRIBUTOR_NAME} <{CONTRIBUTOR_EMAIL}>", text)
    return _EMAIL.sub(
        lambda m: m.group(0) if m.group(0) not in email_addresses(m.group(0)) else CONTRIBUTOR_EMAIL,
        text,
    )


def scrub_identities(payload: Any, mapping: dict[str, str], key: str = "",
                     pattern: re.Pattern[str] | None = None,
                     mentions: re.Pattern[str] | None = None) -> Any:
    """Return *payload* with every contributor identity replaced by a placeholder.

    People's logins map to stable ``contributor-NNNN`` aliases (so "same author"
    and "fork differs from the audited repository" still hold); commit
    author/committer names and emails, trailer lines, signature payloads and any
    other email address become fixed placeholders.  Bots and organisations keep
    their logins: they are not people, and the engine recognises some bots.
    """
    if isinstance(payload, dict):
        result = {
            name: scrub_identities(value, mapping, name, pattern, mentions)
            for name, value in payload.items()
        }
        if _is_person(payload):
            result["login"] = mapping.get(payload["login"], payload["login"])
        if "email" in payload and "name" in payload:
            result["name"] = CONTRIBUTOR_NAME
            result["email"] = CONTRIBUTOR_EMAIL
        if key == "verification":
            for field in ("payload", "signature"):
                if field in result:
                    result[field] = None
        return result
    if isinstance(payload, list):
        return [scrub_identities(value, mapping, key, pattern, mentions) for value in payload]
    if isinstance(payload, str):
        if _URL_KEY.search(key) or key in _LOGIN_BEARING_KEYS:
            payload = _replace_logins(payload, mapping, pattern)
        # ``@login`` mentions in titles, bodies and annotations, in any field.
        return _scrub_string(_replace_logins(payload, mapping, mentions))
    return payload


def _json_files(recording: Path) -> list[Path]:
    return sorted(
        path for path in recording.rglob("*")
        if path.is_file() and "checkout" not in path.relative_to(recording).parts
        and (path.suffix == ".json" or path.name.endswith(".json.gz"))
    )


def audited_owner(recording: Path) -> str | None:
    """The audited repository's owner, from the manifest's ``public-github:`` origin."""
    try:
        origin = json.loads((recording / "manifest.json").read_text(encoding="utf-8"))["origin"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    prefix = "public-github:"
    if not isinstance(origin, str) or not origin.startswith(prefix) or "/" not in origin:
        return None
    return origin.removeprefix(prefix).split("/", 1)[0]


def _scrub_text_files(recording: Path) -> int:
    """Replace contributor email addresses in copied checkout files and text logs."""
    rewritten = 0
    for folder in ("checkout", "logs"):
        for path in sorted((recording / folder).rglob("*")):
            if not path.is_file() or path.suffix == ".gz":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not email_addresses(text):
                continue
            path.write_text(
                _EMAIL.sub(
                    lambda m: CONTRIBUTOR_EMAIL if email_addresses(m.group(0)) else m.group(0), text
                ),
                encoding="utf-8",
            )
            rewritten += 1
    return rewritten


def scrub_recording(recording: Path, owner: str | None = None) -> int:
    """Rewrite every recorded payload under *recording* without contributor identities.

    *owner* (default: read from the manifest) is the audited repository's owner.
    Its login stays as recorded even when it is a person, so the engine's
    "head repository differs from the audited one" fork test still holds.
    Returns the number of files rewritten.  Files whose content does not change
    are left byte-for-byte alone.
    """
    owner = owner or audited_owner(recording)
    decoded: dict[Path, Any] = {}
    logins: set[str] = set()
    for path in _json_files(recording):
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        try:
            decoded[path] = json.loads(raw)
        except ValueError:
            continue
        _person_logins(decoded[path], logins)
    # Already-scrubbed aliases stay as they are, so a second pass is a no-op.
    logins.discard(owner or "")
    taken = {login for login in logins if CONTRIBUTOR_LOGIN.fullmatch(login)}
    fresh = sorted(logins - taken, key=lambda value: (-len(value), value))
    numbers = (number for number in range(1, 10_000) if f"contributor-{number:04d}" not in taken)
    # Longest first, so a login that contains another is replaced whole.
    mapping = {login: f"contributor-{next(numbers):04d}" for login in fresh}
    pattern = _login_pattern(mapping)
    mentions = _login_pattern(mapping, mention=True)
    rewritten = _scrub_text_files(recording)
    for path, payload in decoded.items():
        scrubbed = scrub_identities(payload, mapping, pattern=pattern, mentions=mentions)
        if scrubbed == payload:
            continue
        text = json.dumps(scrubbed, indent=2 if path.name == "manifest.json" else None, sort_keys=False)
        data = (text + "\n").encode("utf-8")
        if path.suffix == ".gz":
            path.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
        else:
            path.write_bytes(data)
        rewritten += 1
    return rewritten


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

    attempted_fetches = 0
    while attempted_fetches < MONTH_JOB_LIST_READ_CAP and (pending := pending_runs()):
        pending = pending[:MONTH_JOB_LIST_READ_CAP - attempted_fetches]
        for offset in range(0, len(pending), 200):
            batch = pending[offset:offset + 200]
            attempted_fetches += len(batch)
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
    """Redact repository links while retaining the source slug separately."""
    del repository

    def replace(match: re.Match[str]) -> str:
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


def _reject_external_symlinks(checkout: Path, sources: list[Path]) -> None:
    checkout_root = checkout.resolve(strict=True)
    candidates: list[Path] = []
    for source in sources:
        candidates.append(source)
        if source.is_dir() and not source.is_symlink():
            candidates.extend(source.rglob("*"))
    for path in candidates:
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"unreadable checkout symlink: {path.relative_to(checkout)}") from error
        if not resolved.is_relative_to(checkout_root):
            raise ValueError(f"checkout symlink resolves outside checkout: {path.relative_to(checkout)}")


def _write_compressed_json(path: Path, payload: dict[str, Any]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    value = digest.hexdigest()
    return "-".join(value[offset:offset + 8] for offset in range(0, len(value), 8))


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
        run_keys = {
            int(run.get("id") or 0): f"run-{run_index:04d}"
            for run_index, run in enumerate(row.get("runs") or [], start=1)
            if isinstance(run, dict)
        }
        commits.append({
            "sha": f"commit-{index:06d}",
            "created_at": row.get("created_at"),
            "completed": bool(row.get("completed")),
            "job_lists_recorded": sum(
                bool(run.get("jobs_recorded")) for run in row.get("runs") or []
            ),
            "runs": [
                {**{key: value for key, value in run.items() if key != "id"},
                 "run_key": run_keys.get(int(run.get("id") or 0))}
                for run in row.get("runs") or [] if isinstance(run, dict)
            ],
            "jobs": [
                {**{key: value for key, value in job.items()
                    if key not in {"id", "run_id"}},
                 "run_key": run_keys.get(int(job.get("run_id") or 0))}
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
        "origin": f"public-github:{repository}",
        "log_cap_bytes": log_cap_bytes,
        "secret_scan": "passed",
        "identifiers": "stable eight-digit fixture ids",
        "checkout_sanitization": {
            "repository_links": "repository URL targets and numbered references use a neutral placeholder",
            "long_decimal_runs": "long decimal runs use same-length shape-preserving pseudonyms",
            "environment_paths": "absolute runner and account paths use neutral fixture paths",
            "snapshot_sha256": _tree_sha256(output / "checkout"),
        },
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
        scrub_recording(output, repository.split("/", 1)[0])
        _write_manifest(repository, checkout, output, captured_at, log_cap_bytes, [], public_history)
        state_path.unlink(missing_ok=True)
        return
    snapshot = output / "checkout"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    workflows = checkout / ".github" / "workflows"
    if not workflows.is_dir():
        raise RuntimeError("checkout has no .github/workflows directory")
    snapshot_sources = [workflows]
    snapshot_sources.extend(
        checkout / name
        for name in SNAPSHOT_MANIFESTS
        if (checkout / name).exists() or (checkout / name).is_symlink()
    )
    _reject_external_symlinks(checkout, snapshot_sources)
    shutil.copytree(workflows, snapshot / ".github" / "workflows")
    for name in SNAPSHOT_MANIFESTS:
        source = checkout / name
        if source.is_file():
            # Stored under a name Dependabot does not scan; replay restores it.
            target = snapshot / (name + SNAPSHOT_SUFFIX)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
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
    scrub_recording(output, repository.split("/", 1)[0])
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
