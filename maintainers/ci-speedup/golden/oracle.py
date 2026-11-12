#!/usr/bin/env python3
"""Independent reference facts for recorded GitHub Actions histories.

This module deliberately consumes a small, normalized recording format.  It
does not call or import the ci-speedup engine.  That separation lets its facts
disagree with the implementation under test.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_BOOKKEEPING_CUTOFF_S = 60


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds(start: object, end: object) -> float | None:
    left, right = _instant(start), _instant(end)
    if left is None or right is None or right < left:
        return None
    return (right - left).total_seconds()


def _percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _check_name(job: dict[str, Any]) -> str:
    workflow = str(job.get("workflow") or "").strip()
    name = str(job.get("name") or "").strip()
    return f"{workflow} / {name}" if workflow and name else name or workflow or "unknown"


def _eligible_jobs(commit: dict[str, Any], cutoff_s: int) -> tuple[list[dict[str, Any]], list[str]]:
    jobs: list[dict[str, Any]] = []
    excluded: list[str] = []
    for row in commit.get("jobs") or []:
        if not isinstance(row, dict) or row.get("status") not in (None, "completed"):
            continue
        duration = _seconds(row.get("started_at"), row.get("completed_at"))
        if duration is None:
            continue
        if duration < cutoff_s:
            excluded.append(_check_name(row))
            continue
        jobs.append(row)
    return jobs, excluded


def compute(recording: dict[str, Any], *,
            bookkeeping_cutoff_s: int = DEFAULT_BOOKKEEPING_CUTOFF_S,
            gate_by_sha: dict[str, str] | None = None) -> dict[str, Any]:
    """Compute month-wide gate shares and wait facts from normalized data."""
    if recording.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported golden recording schema")
    commits = [row for row in recording.get("pr_commits") or [] if isinstance(row, dict)]
    completed = [row for row in commits if bool(row.get("completed"))]
    complete_times = [
        instant for row in completed
        if (instant := _instant(row.get("created_at"))) is not None
    ]
    gate_counts: Counter[str] = Counter()
    gate_durations: dict[str, list[float]] = defaultdict(list)
    wait_s: list[float] = []
    bookkeeping: Counter[str] = Counter()
    commits_with_jobs = 0
    for commit in completed:
        jobs, excluded = _eligible_jobs(commit, bookkeeping_cutoff_s)
        bookkeeping.update(excluded)
        if not jobs:
            continue
        commits_with_jobs += 1
        earliest_job = min(
            instant for job in jobs
            if (instant := _instant(job.get("started_at"))) is not None
        )
        run_starts = [
            instant for run in commit.get("runs") or [] if isinstance(run, dict)
            if (instant := _instant(run.get("created_at"))) is not None
        ]
        earliest = min([earliest_job, *run_starts])
        latest = max(
            instant for job in jobs
            if (instant := _instant(job.get("completed_at"))) is not None
        )
        wait_s.append((latest - earliest).total_seconds())
        for job in jobs:
            name = _check_name(job)
            duration = _seconds(job.get("started_at"), job.get("completed_at"))
            if duration is not None:
                # The engine reports each check's ordinary P50, independent of
                # whether that occurrence happened to finish last.  Gate share
                # remains conditional below; timing does not.
                gate_durations[name].append(duration)
        forced_gate = (gate_by_sha or {}).get(str(commit.get("sha") or ""))
        longest = max(
            _seconds(job.get("started_at"), job.get("completed_at")) or 0.0
            for job in jobs)
        for job in jobs:
            job_name = _check_name(job)
            leaf_name = str(job.get("name") or "")
            if forced_gate:
                is_gate = forced_gate in {job_name, leaf_name}
            else:
                is_gate = (
                    _seconds(job.get("started_at"), job.get("completed_at"))
                    == longest)
            if not is_gate:
                continue
            name = job_name
            gate_counts[name] += 1

    denominator = commits_with_jobs
    gates = [
        {
            "check": name,
            "last_on": count,
            "out_of": denominator,
            "share": round(count / denominator, 6) if denominator else 0.0,
            "p50_s": round(statistics.median(gate_durations[name]), 1),
        }
        for name, count in gate_counts.items()
    ]
    gates.sort(key=lambda row: (-row["last_on"], -row["p50_s"], row["check"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": str(recording.get("repository") or ""),
        "window": recording.get("window") or {},
        "sample": {
            "total_pr_commits": len(commits),
            "completed_pr_commits": len(completed),
            "inflight_pr_commits": len(commits) - len(completed),
            "completed_with_eligible_jobs": denominator,
            "distinct_days": len({value.date().isoformat() for value in complete_times}),
            "first_completed_at": min(complete_times).isoformat().replace("+00:00", "Z") if complete_times else None,
            "last_completed_at": max(complete_times).isoformat().replace("+00:00", "Z") if complete_times else None,
        },
        "typical_pr_wait_s": round(_percentile(wait_s, 0.5) or 0.0, 1),
        "gate_order": [row["check"] for row in gates],
        "gates": gates,
        "bookkeeping_cutoff_s": int(bookkeeping_cutoff_s),
        "bookkeeping_jobs": [
            {"check": name, "excluded_on": count}
            for name, count in sorted(bookkeeping.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--bookkeeping-cutoff-s", type=int, default=DEFAULT_BOOKKEEPING_CUTOFF_S)
    args = parser.parse_args(argv)
    if args.recording.suffix == ".gz":
        with gzip.open(args.recording, "rt", encoding="utf-8") as stream:
            source = json.load(stream)
    else:
        source = json.loads(args.recording.read_text(encoding="utf-8"))
    result = compute(source, bookkeeping_cutoff_s=args.bookkeeping_cutoff_s)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
