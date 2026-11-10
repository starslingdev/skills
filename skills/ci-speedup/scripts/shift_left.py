#!/usr/bin/env python3
"""Report-only PR and main-branch CI duration drift detection."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

BASELINE_RUNS = 20
MIN_BASELINE = 5
RELATIVE_THRESHOLD = 0.10
STEP_HOLD = 3
CREEP_WINDOW = 5
CREEP_MIN_RUNS = 15


class CollectionError(RuntimeError):
    pass


def _eligible(row: dict[str, Any]) -> bool:
    value = row.get("duration_s")
    return (row.get("status") == "completed" and row.get("conclusion") == "success"
            and isinstance(value, (int, float)) and math.isfinite(float(value))
            and float(value) >= 0)


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("workflow", "")), str(row.get("job", ""))


def _stats(values: Iterable[float]) -> dict[str, float]:
    sample = [float(value) for value in values]
    median = float(statistics.median(sample))
    mad = float(statistics.median(abs(value - median) for value in sample))
    sigma = 1.4826 * mad
    noise = max(1.0, 0.05 * median, 3.0 * sigma)
    return {"median_s": round(median, 6), "mad_s": round(mad, 6),
            "robust_sigma_s": round(sigma, 6), "noise_band_s": round(noise, 6),
            "relative_threshold_s": round(median * 1.10, 6)}


def _observation(kind: str, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {"kind": kind, "workflow": str(row.get("workflow", "")),
              "job": str(row.get("job", "")),
              "conclusion": "warning" if kind in {"slowdown", "step_change", "creep"}
              else "neutral"}
    result.update(extra)
    return result


def analyze_l2(history: list[dict[str, Any]], current: list[dict[str, Any]], *,
               baseline_runs: int = BASELINE_RUNS, min_history: int = MIN_BASELINE
               ) -> dict[str, Any]:
    """Compare this PR's successful jobs with recent successful main jobs."""
    old = [row for row in history if _eligible(row)]
    new = [row for row in current if _eligible(row)]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    current_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in old:
        by_key[_key(row)].append(row)
    for row in new:
        current_by_key[_key(row)].append(row)
    current_names: dict[str, set[str]] = defaultdict(set)
    old_names: dict[str, set[str]] = defaultdict(set)
    for workflow, job in current_by_key:
        current_names[workflow].add(job)
    for workflow, job in by_key:
        old_names[workflow].add(job)

    observations, jobs = [], []
    for key in sorted(current_by_key):
        workflow, job = key
        rows = current_by_key[key]
        duration = float(statistics.median(row["duration_s"] for row in rows))
        baseline = by_key.get(key, [])[-baseline_runs:]
        if not baseline:
            disappeared = sorted(old_names[workflow] - current_names[workflow])
            plausible = []
            for previous in disappeared:
                previous_rows = by_key[(workflow, previous)][-baseline_runs:]
                if len(previous_rows) < min_history:
                    continue
                summary = _stats(row["duration_s"] for row in previous_rows)
                if abs(duration - summary["median_s"]) <= max(
                        summary["noise_band_s"], summary["median_s"] * 0.20):
                    plausible.append((previous, summary, len(previous_rows)))
            if len(plausible) == 1:
                previous, summary, count = plausible[0]
                observations.append(_observation("renamed_job", rows[-1],
                    previous_job=previous, baseline_runs=count,
                    baseline_median_s=summary["median_s"]))
                evidence, count = "renamed_job", count
            else:
                observations.append(_observation("new_job", rows[-1]))
                evidence, count = "new_job", 0
            jobs.append({"workflow": workflow, "job": job, "baseline_runs": count,
                         "current_s": round(duration, 6), "evidence": evidence})
            continue
        if len(baseline) < min_history:
            observations.append(_observation("no_baseline", rows[-1],
                available_runs=len(baseline), required_runs=min_history))
            jobs.append({"workflow": workflow, "job": job,
                         "baseline_runs": len(baseline), "current_s": round(duration, 6),
                         "evidence": "no_baseline"})
            continue
        summary = _stats(row["duration_s"] for row in baseline)
        delta = duration - summary["median_s"]
        slower = duration > summary["relative_threshold_s"] and delta > summary["noise_band_s"]
        evidence = "slowdown" if slower else "threshold_not_exceeded"
        jobs.append({"workflow": workflow, "job": job, "baseline_runs": len(baseline),
            "baseline_median_s": summary["median_s"], "noise_band_s": summary["noise_band_s"],
            "current_s": round(duration, 6),
            "change_percent": round(100 * delta / summary["median_s"], 3)
            if summary["median_s"] else None, "evidence": evidence})
        if slower:
            observations.append(_observation("slowdown", rows[-1],
                baseline_runs=len(baseline), baseline_median_s=summary["median_s"],
                noise_band_s=summary["noise_band_s"], current_s=round(duration, 6),
                change_percent=round(100 * delta / summary["median_s"], 3)))
    return {"layer": "L2",
            "promise": "job-history evidence; silence does not prove CI stayed fast",
            "job_conclusion": "success", "observations": observations, "jobs": jobs,
            "excluded_runs": len(history) + len(current) - len(old) - len(new)}


def _attribution(row: dict[str, Any]) -> dict[str, Any]:
    return {"pr_number": row.get("pr_number"), "sha": row.get("sha")}


def _step(rows: list[dict[str, Any]], baseline_runs: int, min_history: int):
    for index in range(min_history, len(rows) - STEP_HOLD + 1):
        before = rows[max(0, index - baseline_runs):index]
        if len(before) < min_history:
            continue
        summary = _stats(row["duration_s"] for row in before)
        candidates = rows[index:index + STEP_HOLD]
        previous = float(rows[index - 1]["duration_s"])
        onset = float(candidates[0]["duration_s"]) - previous
        abrupt = onset > max(summary["noise_band_s"], previous * 0.05)
        held = all(float(row["duration_s"]) > summary["relative_threshold_s"]
                   and float(row["duration_s"]) - summary["median_s"] > summary["noise_band_s"]
                   for row in candidates)
        if abrupt and held:
            after = float(statistics.median(row["duration_s"] for row in candidates))
            return {"row": rows[index], "baseline": summary, "after_s": round(after, 6),
                    "change": round(100 * (after - summary["median_s"])
                                    / summary["median_s"], 3)}
    return None


def _creep(rows: list[dict[str, Any]], min_history: int):
    if len(rows) < max(CREEP_MIN_RUNS, min_history + CREEP_WINDOW):
        return None
    anchor = _stats(row["duration_s"] for row in rows[:min_history])
    qualifying = []
    for end in range(min_history + CREEP_WINDOW - 1, len(rows)):
        rolling = float(statistics.median(
            row["duration_s"] for row in rows[end - CREEP_WINDOW + 1:end + 1]))
        if (rolling > anchor["relative_threshold_s"]
                and rolling - anchor["median_s"] > anchor["noise_band_s"]):
            qualifying.append(end)
    if not qualifying or qualifying != list(range(qualifying[0], len(rows))):
        return None
    changes = [float(rows[i]["duration_s"]) - float(rows[i - 1]["duration_s"])
               for i in range(1, len(rows))]
    if statistics.median(changes) <= 0:
        return None
    recent = float(statistics.median(row["duration_s"] for row in rows[-CREEP_WINDOW:]))
    return {"row": rows[qualifying[0]], "baseline": anchor, "recent_s": round(recent, 6),
            "change": round(100 * (recent - anchor["median_s"])
                            / anchor["median_s"], 3)}


def analyze_l3(history: list[dict[str, Any]], *, baseline_runs: int = BASELINE_RUNS,
               min_history: int = MIN_BASELINE) -> dict[str, Any]:
    """Find sustained abrupt shifts and gradual creep in successful main jobs."""
    eligible = [row for row in history if _eligible(row)]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_key[_key(row)].append(row)
    observations, jobs = [], []
    for (workflow, job), rows in sorted(by_key.items()):
        if len(rows) < min_history:
            observations.append(_observation("no_baseline", rows[-1],
                available_runs=len(rows), required_runs=min_history))
            jobs.append({"workflow": workflow, "job": job, "history_runs": len(rows),
                         "evidence": "no_baseline"})
            continue
        step = _step(rows, baseline_runs, min_history)
        if step:
            observations.append(_observation("step_change", step["row"],
                attribution=_attribution(step["row"]),
                baseline_median_s=step["baseline"]["median_s"],
                noise_band_s=step["baseline"]["noise_band_s"],
                after_median_s=step["after_s"], change_percent=step["change"],
                held_runs=STEP_HOLD))
            evidence = "step_change"
        else:
            creep = _creep(rows, min_history)
            if creep:
                observations.append(_observation("creep", creep["row"],
                    attribution=_attribution(creep["row"]),
                    baseline_median_s=creep["baseline"]["median_s"],
                    noise_band_s=creep["baseline"]["noise_band_s"],
                    recent_median_s=creep["recent_s"], change_percent=creep["change"],
                    window_runs=CREEP_WINDOW))
                evidence = "creep"
            else:
                evidence = "no_sustained_shift_established"
        jobs.append({"workflow": workflow, "job": job, "history_runs": len(rows),
                     "evidence": evidence})
    return {"layer": "L3",
            "promise": "sustained history evidence; silence does not prove main stayed fast",
            "job_conclusion": "success", "observations": observations, "jobs": jobs,
            "excluded_runs": len(history) - len(eligible)}


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GhClient:
    def __init__(self, budget: int, timeout: int):
        self.budget, self.timeout, self.calls = budget, timeout, 0

    def get(self, endpoint: str) -> Any:
        if self.calls >= self.budget:
            raise CollectionError(f"GitHub API budget exhausted at {self.budget} calls")
        self.calls += 1
        try:
            proc = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                text=True, capture_output=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectionError(f"GitHub API call did not complete: {type(exc).__name__}") from exc
        if proc.returncode:
            raise CollectionError(f"GitHub API returned a non-zero status on call {self.calls}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CollectionError("GitHub API returned malformed JSON") from exc


def _runs(doc: Any) -> list[dict[str, Any]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("workflow_runs"), list):
        raise CollectionError("workflow-runs response is incomplete")
    return [row for row in doc["workflow_runs"] if isinstance(row, dict)]


def _job_records(run: dict[str, Any], jobs: Any) -> list[dict[str, Any]]:
    if not isinstance(jobs, list):
        raise CollectionError("jobs response is incomplete")
    workflow = str(run.get("path") or run.get("workflow_id") or run.get("name") or "unknown")
    result = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        started, completed = _parse_time(job.get("started_at")), _parse_time(job.get("completed_at"))
        result.append({"workflow": workflow, "job": str(job.get("name") or "unknown"),
            "duration_s": (completed - started).total_seconds() if started and completed else None,
            "status": job.get("status"), "conclusion": job.get("conclusion"),
            "sha": run.get("head_sha"), "run_id": run.get("id"),
            "created_at": run.get("created_at"), "pr_number": None})
    return result


def collect_history(repo: str, branch: str, *, pr_number: int | None,
                    max_runs: int, client: GhClient):
    main_doc = client.get(
        f"repos/{repo}/actions/runs?branch={quote(branch, safe='')}&status=completed&per_page=100")
    main_runs = [row for row in _runs(main_doc)
                 if row.get("status") == "completed" and row.get("conclusion") == "success"][:max_runs]
    main_records = []
    for run in reversed(main_runs):
        doc = client.get(f"repos/{repo}/actions/runs/{run.get('id')}/jobs?filter=latest&per_page=100")
        main_records.extend(_job_records(run, doc.get("jobs") if isinstance(doc, dict) else None))
    pr_records = []
    if pr_number is not None:
        pr = client.get(f"repos/{repo}/pulls/{pr_number}")
        head = pr.get("head", {}).get("sha") if isinstance(pr, dict) else None
        if not isinstance(head, str) or not head:
            raise CollectionError("pull request head SHA is unavailable")
        pr_doc = client.get(
            f"repos/{repo}/actions/runs?head_sha={quote(head, safe='')}&status=completed&per_page=100")
        for run in _runs(pr_doc):
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            doc = client.get(f"repos/{repo}/actions/runs/{run.get('id')}/jobs?filter=latest&per_page=100")
            pr_records.extend(_job_records(run, doc.get("jobs") if isinstance(doc, dict) else None))
    return main_records, pr_records


def _fill_attribution(repo: str, report: dict[str, Any], client: GhClient) -> None:
    cache = {}
    for item in report.get("observations", []):
        attribution = item.get("attribution")
        if not isinstance(attribution, dict) or attribution.get("pr_number") is not None:
            continue
        sha = attribution.get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        if sha not in cache:
            doc = client.get(f"repos/{repo}/commits/{sha}/pulls?per_page=10")
            numbers = [row.get("number") for row in doc if isinstance(row, dict)] if isinstance(doc, list) else []
            cache[sha] = next((number for number in numbers if isinstance(number, int)), None)
        attribution["pr_number"] = cache[sha]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--max-runs", type=int, default=20)
    parser.add_argument("--api-budget", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not MIN_BASELINE <= args.max_runs <= 50:
        parser.error(f"--max-runs must be between {MIN_BASELINE} and 50")
    if not 1 <= args.api_budget <= 100:
        parser.error("--api-budget must be between 1 and 100")
    started = time.monotonic()
    client = GhClient(args.api_budget, args.timeout_seconds)
    try:
        main_records, pr_records = collect_history(
            args.repo, args.branch, pr_number=args.pr, max_runs=args.max_runs, client=client)
        l3 = analyze_l3(main_records)
        _fill_attribution(args.repo, l3, client)
        result: dict[str, Any] = {"status": "measured", "job_conclusion": "success",
            "repository": args.repo, "branch": args.branch, "l3": l3}
        if args.pr is not None:
            result.update({"pull_request": args.pr, "l2": analyze_l2(main_records, pr_records)})
    except CollectionError as exc:
        result = {"status": "coverage_gap", "job_conclusion": "success",
            "repository": args.repo, "branch": args.branch, "reason": str(exc),
            "promise": "no performance conclusion was produced"}
    result["provenance"] = {"api_calls": client.calls, "api_budget": client.budget,
        "elapsed_s": round(time.monotonic() - started, 3),
        "completed_successful_jobs_only": True}
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
