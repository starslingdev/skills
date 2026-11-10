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
L2_MIN_BASELINE = 8
L3_MIN_HISTORY = 15
STEP_PRE_MIN = 8
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


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("workflow_id", "")),
        " ".join(str(row.get("job", "")).split()),
        tuple(sorted({str(label) for label in row.get("runner_labels", [])})),
        tuple(str(step) for step in row.get("step_signature", [])),
    )


def _rename_key(row: dict[str, Any]) -> tuple[Any, ...]:
    identity = _key(row)
    return identity[0], identity[2], identity[3]


def _stats(values: Iterable[float]) -> dict[str, float]:
    sample = [float(value) for value in values]
    median = float(statistics.median(sample))
    mad = float(statistics.median(abs(value - median) for value in sample))
    sigma = 1.4826 * mad
    noise = max(2.0, 3.0 * sigma)
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
               baseline_runs: int = BASELINE_RUNS, min_history: int = L2_MIN_BASELINE
               ) -> dict[str, Any]:
    """Compare this PR's successful jobs with recent successful main jobs."""
    old = [row for row in history if _eligible(row)]
    new = [row for row in current if _eligible(row)]
    by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    current_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in old:
        by_key[_key(row)].append(row)
    for row in new:
        current_by_key[_key(row)].append(row)
    current_names: dict[str, set[str]] = defaultdict(set)
    old_names: dict[str, set[str]] = defaultdict(set)
    for identity in current_by_key:
        current_names[identity[0]].add(identity[1])
    for identity in by_key:
        old_names[identity[0]].add(identity[1])

    observations = [_observation("excluded", row,
        excluded_conclusion=str(row.get("conclusion") or "unknown"))
        for row in current if row.get("status") == "completed" and not _eligible(row)]
    jobs = [{"workflow": str(row.get("workflow", "")),
             "workflow_id": str(row.get("workflow_id", "")),
             "job": str(row.get("job", "")), "evidence": "excluded",
             "excluded_conclusion": str(row.get("conclusion") or "unknown")}
            for row in current if row.get("status") == "completed" and not _eligible(row)]
    for key in sorted(current_by_key):
        workflow_id, job, runner_labels, step_signature = key
        rows = current_by_key[key]
        workflow = str(rows[-1].get("workflow", workflow_id))
        duration = float(statistics.median(row["duration_s"] for row in rows))
        baseline = by_key.get(key, [])[-baseline_runs:]
        renamed_from = None
        if not baseline:
            disappeared = sorted(old_names[workflow_id] - current_names[workflow_id])
            plausible = []
            for previous in disappeared:
                previous_rows = [row for row in old
                    if _rename_key(row) == (workflow_id, runner_labels, step_signature)
                    and _key(row)[1] == previous][-baseline_runs:]
                if len(previous_rows) < min_history:
                    continue
                summary = _stats(row["duration_s"] for row in previous_rows)
                plausible.append((previous, summary, previous_rows))
            if len(plausible) == 1:
                previous, summary, previous_rows = plausible[0]
                observations.append(_observation("renamed_job", rows[-1],
                    previous_job=previous, baseline_runs=len(previous_rows),
                    baseline_median_s=summary["median_s"]))
                renamed_from = previous
                baseline = previous_rows
            elif not plausible:
                observations.append(_observation("new_job", rows[-1]))
                evidence, count = "new_job", 0
            else:
                observations.append(_observation("ambiguous_rename", rows[-1],
                    candidates=sorted(previous for previous, _, _ in plausible)))
                evidence, count = "ambiguous_rename", 0
            if not baseline:
                jobs.append({"workflow": workflow, "workflow_id": workflow_id, "job": job,
                             "runner_labels": list(runner_labels),
                             "step_signature": list(step_signature), "baseline_runs": count,
                             "current_s": round(duration, 6), "evidence": evidence})
                continue
        if len(baseline) < min_history:
            observations.append(_observation("no_baseline", rows[-1],
                available_runs=len(baseline), required_runs=min_history))
            jobs.append({"workflow": workflow, "workflow_id": workflow_id, "job": job,
                         "baseline_runs": len(baseline), "current_s": round(duration, 6),
                         "evidence": "no_baseline"})
            continue
        summary = _stats(row["duration_s"] for row in baseline)
        delta = duration - summary["median_s"]
        slower = duration > summary["relative_threshold_s"] and delta > summary["noise_band_s"]
        evidence = "slowdown" if slower else "threshold_not_exceeded"
        jobs.append({"workflow": workflow, "workflow_id": workflow_id, "job": job,
            "runner_labels": list(runner_labels), "step_signature": list(step_signature),
            "baseline_runs": len(baseline),
            "baseline_median_s": summary["median_s"], "noise_band_s": summary["noise_band_s"],
            "current_s": round(duration, 6),
            "change_percent": round(100 * delta / summary["median_s"], 3)
            if summary["median_s"] else None, "evidence": evidence,
            "renamed_from": renamed_from})
        if slower:
            observations.append(_observation("slowdown", rows[-1],
                baseline_runs=len(baseline), baseline_median_s=summary["median_s"],
                noise_band_s=summary["noise_band_s"], current_s=round(duration, 6),
                change_percent=round(100 * delta / summary["median_s"], 3),
                renamed_from=renamed_from))
    return {"layer": "L2",
            "promise": "job-history evidence; silence does not prove CI stayed fast",
            "job_conclusion": "success", "observations": observations, "jobs": jobs,
            "excluded_runs": len(history) + len(current) - len(old) - len(new)}


def _attribution(row: dict[str, Any]) -> dict[str, Any]:
    return {"pr_number": row.get("pr_number"), "sha": row.get("sha")}


def _step(rows: list[dict[str, Any]], baseline_runs: int, min_history: int):
    for index in range(min_history, len(rows) - 3):
        before = rows[max(0, index - baseline_runs):index]
        if len(before) < min_history:
            continue
        summary = _stats(row["duration_s"] for row in before)
        post = rows[index:]
        boundary = summary["median_s"] + max(
            RELATIVE_THRESHOLD * summary["median_s"], summary["noise_band_s"])
        after = float(statistics.median(row["duration_s"] for row in post))
        first_four = [float(row["duration_s"]) for row in post[:4]]
        newest_three = [float(row["duration_s"]) for row in post[-3:]]
        held = (after > summary["relative_threshold_s"]
                and after - summary["median_s"] > summary["noise_band_s"]
                and sum(value > boundary for value in first_four) >= 3
                and all(value > boundary for value in newest_three))
        if held:
            first_slow = next(row for row in post[:4]
                              if float(row["duration_s"]) > boundary)
            return {"row": first_slow, "baseline": summary, "after_s": round(after, 6),
                    "change": round(100 * (after - summary["median_s"])
                                    / summary["median_s"], 3)}
    return None


def _creep(rows: list[dict[str, Any]], min_history: int):
    if len(rows) < max(CREEP_MIN_RUNS, min_history):
        return None
    for start in range(0, len(rows) - CREEP_MIN_RUNS + 1):
        window = rows[start:start + CREEP_MIN_RUNS]
        values = [float(row["duration_s"]) for row in window]
        first = _stats(values[:CREEP_WINDOW])
        last_median = float(statistics.median(values[-CREEP_WINDOW:]))
        rise = last_median - first["median_s"]
        if not (last_median > first["relative_threshold_s"]
                and rise > first["noise_band_s"]):
            continue
        slopes = [(values[j] - values[i]) / (j - i)
                  for i in range(len(values)) for j in range(i + 1, len(values))]
        adjacent = [values[i] - values[i - 1] for i in range(1, len(values))]
        if statistics.median(slopes) <= 0 or sum(delta >= 0 for delta in adjacent) < 10:
            continue
        if max(adjacent) >= 0.5 * rise:
            continue
        return {"row": window[CREEP_WINDOW], "baseline": first,
                "recent_s": round(last_median, 6),
                "change": round(100 * rise / first["median_s"], 3),
                "window_start": start}
    return None


def analyze_l3(history: list[dict[str, Any]], *, baseline_runs: int = BASELINE_RUNS,
               min_history: int = L3_MIN_HISTORY) -> dict[str, Any]:
    """Find sustained abrupt shifts and gradual creep in successful main jobs."""
    eligible = [row for row in history if _eligible(row)]
    by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_key[_key(row)].append(row)
    observations, jobs = [], []
    if not eligible:
        observations.append({"kind": "no_baseline", "workflow": "", "job": "",
            "conclusion": "neutral", "available_runs": 0, "required_runs": min_history})
        jobs.append({"workflow": "", "job": "", "history_runs": 0,
                     "evidence": "no_baseline"})
    for identity, rows in sorted(by_key.items()):
        workflow_id, job, runner_labels, step_signature = identity
        workflow = str(rows[-1].get("workflow", workflow_id))
        if len(rows) < min_history:
            observations.append(_observation("no_baseline", rows[-1],
                available_runs=len(rows), required_runs=min_history))
            jobs.append({"workflow": workflow, "workflow_id": workflow_id, "job": job,
                         "runner_labels": list(runner_labels),
                         "step_signature": list(step_signature), "history_runs": len(rows),
                         "evidence": "no_baseline"})
            continue
        step = _step(rows, baseline_runs, STEP_PRE_MIN)
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
        jobs.append({"workflow": workflow, "workflow_id": workflow_id, "job": job,
                     "runner_labels": list(runner_labels),
                     "step_signature": list(step_signature), "history_runs": len(rows),
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
    def __init__(self, budget: int, timeout: int, deadline_seconds: int = 120):
        self.budget, self.timeout, self.calls = budget, timeout, 0
        self.deadline = time.monotonic() + deadline_seconds

    def get(self, endpoint: str) -> Any:
        if self.calls >= self.budget:
            raise CollectionError(f"GitHub API budget exhausted at {self.budget} calls")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("total collection deadline exceeded")
        self.calls += 1
        try:
            proc = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                text=True, capture_output=True, timeout=min(self.timeout, remaining), check=False)
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


def _paginate(client: Any, endpoint: str, key: str, *, max_pages: int = 2,
              per_page: int = 100) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    total = None
    for page in range(1, max_pages + 1):
        separator = "&" if "?" in endpoint else "?"
        doc = client.get(f"{endpoint}{separator}per_page={per_page}&page={page}")
        if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
            raise CollectionError(f"{key} response is incomplete")
        rows = [row for row in doc[key] if isinstance(row, dict)]
        if isinstance(doc.get("total_count"), int):
            total = doc["total_count"]
        items.extend(rows)
        if len(rows) < per_page or (total is not None and len(items) >= total):
            return items, False
    return items, total is None or len(items) < total


def _jobs_for_run(repo: str, run_id: Any, client: Any) -> tuple[list[dict[str, Any]], bool]:
    return _paginate(client, f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest",
                     "jobs", max_pages=2)


def _job_records(run: dict[str, Any], jobs: Any) -> list[dict[str, Any]]:
    if not isinstance(jobs, list):
        raise CollectionError("jobs response is incomplete")
    workflow_id = str(run.get("workflow_id") or "")
    workflow = str(run.get("path") or run.get("name") or workflow_id or "unknown")
    result = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        started, completed = _parse_time(job.get("started_at")), _parse_time(job.get("completed_at"))
        raw_steps = job.get("steps") if isinstance(job.get("steps"), list) else []
        step_signature = [" ".join(str(step.get("name", "")).split())
                          for step in raw_steps if isinstance(step, dict)
                          and not str(step.get("name", "")).casefold().startswith("post ")]
        labels = job.get("labels") if isinstance(job.get("labels"), list) else []
        result.append({"workflow": workflow, "workflow_id": workflow_id,
            "job": str(job.get("name") or "unknown"),
            "runner_labels": sorted({str(label) for label in labels}),
            "step_signature": step_signature,
            "duration_s": (completed - started).total_seconds() if started and completed else None,
            "status": job.get("status"), "conclusion": job.get("conclusion"),
            "sha": run.get("head_sha"), "run_id": run.get("id"),
            "created_at": run.get("created_at"), "event": run.get("event"),
            "branch": run.get("head_branch"), "pr_number": None})
    return result


def _successful_push_runs(rows: list[dict[str, Any]], branch: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") == "completed"
            and row.get("conclusion") == "success" and row.get("event") == "push"
            and row.get("head_branch") == branch]


def collect_history(repo: str, branch: str, *, pr_number: int | None,
                    max_runs: int, client: GhClient, max_workflows: int = 5):
    """Collect candidate-first L2 history or bounded per-workflow L3 history."""
    coverage: dict[str, Any] = {"partial_workflows": {}, "omitted_workflows": [],
                                "budget_limited_workflows": {}}
    main_records: list[dict[str, Any]] = []
    pr_records: list[dict[str, Any]] = []
    workflow_ids: list[str] = []
    candidate_keys: set[tuple[Any, ...]] = set()

    if pr_number is not None:
        pr = client.get(f"repos/{repo}/pulls/{pr_number}")
        head = pr.get("head", {}).get("sha") if isinstance(pr, dict) else None
        if not isinstance(head, str) or not head:
            raise CollectionError("pull request head SHA is unavailable")
        pr_runs, pr_truncated = _paginate(client,
            f"repos/{repo}/actions/runs?head_sha={quote(head, safe='')}&status=completed",
            "workflow_runs", max_pages=2)
        if pr_truncated:
            coverage["candidate_partial"] = "pull-request run list reached page cap"
        for run in pr_runs:
            if run.get("status") != "completed":
                continue
            jobs, jobs_truncated = _jobs_for_run(repo, run.get("id"), client)
            records = _job_records(run, jobs)
            pr_records.extend(records)
            workflow_id = str(run.get("workflow_id") or "")
            if workflow_id and workflow_id not in workflow_ids:
                workflow_ids.append(workflow_id)
            if jobs_truncated:
                coverage["partial_workflows"][workflow_id] = "candidate jobs reached page cap"
        candidate_keys = {_key(row) for row in pr_records if _eligible(row)}
        eligible_workflows = {key[0] for key in candidate_keys}
        workflow_ids = [workflow_id for workflow_id in workflow_ids
                        if workflow_id in eligible_workflows]
    else:
        workflows, workflows_truncated = _paginate(
            client, f"repos/{repo}/actions/workflows?", "workflows", max_pages=2)
        active = [row for row in workflows if row.get("state") == "active" and row.get("id")]
        active.sort(key=lambda row: (str(row.get("path", "")), int(row.get("id", 0))))
        workflow_ids = [str(row["id"]) for row in active[:max_workflows]]
        coverage["omitted_workflows"] = [str(row["id"]) for row in active[max_workflows:]]
        if workflows_truncated:
            coverage["workflow_list_partial"] = "workflow list reached page cap"

    for workflow_index, workflow_id in enumerate(workflow_ids):
        try:
            runs, runs_truncated = _paginate(client,
                f"repos/{repo}/actions/workflows/{workflow_id}/runs?"
                f"branch={quote(branch, safe='')}&event=push&status=completed",
                "workflow_runs", max_pages=2)
            selected = _successful_push_runs(runs, branch)
            counts: dict[tuple[Any, ...], int] = defaultdict(int)
            fetched = 0
            relevant: set[tuple[Any, ...]] = {
                key for key in candidate_keys if key[0] == workflow_id
            }
            for run in selected:
                jobs, jobs_truncated = _jobs_for_run(repo, run.get("id"), client)
                records = _job_records(run, jobs)
                main_records.extend(records)
                fetched += 1
                for record in records:
                    if _eligible(record):
                        counts[_key(record)] += 1
                if pr_number is None and not relevant:
                    # L3 follows the identities in the newest successful run.
                    # This avoids spending the budget on obsolete job eras while
                    # still collecting enough comparable observations per identity.
                    relevant = {_key(record) for record in records if _eligible(record)}
                if jobs_truncated:
                    coverage["partial_workflows"][workflow_id] = "jobs reached page cap"
                if relevant and all(counts[key] >= max_runs for key in relevant):
                    break
            unfilled = sorted(key[1] for key in relevant if counts[key] < max_runs)
            if not relevant and runs_truncated:
                coverage["partial_workflows"].setdefault(
                    workflow_id, "no eligible identity found before the run-list page cap")
            elif unfilled:
                reason = (f"comparable history below {max_runs} observations for: "
                          + ", ".join(unfilled[:10]))
                if runs_truncated:
                    reason += "; run list reached page cap"
                coverage["partial_workflows"].setdefault(workflow_id, reason)
            elif len(selected) > fetched:
                coverage["budget_limited_workflows"][workflow_id] = (
                    f"stopped after all selected identities reached {max_runs} observations")
        except CollectionError as exc:
            coverage["partial_workflows"][workflow_id] = str(exc)
            if getattr(client, "calls", 0) >= getattr(client, "budget", math.inf):
                for omitted_id in workflow_ids[workflow_index + 1:]:
                    coverage["partial_workflows"].setdefault(
                        omitted_id, "not collected because the API budget was exhausted")
                break
    return list(reversed(main_records)), pr_records, coverage


def _apply_coverage(report: dict[str, Any], coverage: dict[str, Any], *,
                    candidate: bool = False) -> None:
    partial_ids = set(coverage.get("partial_workflows", {}))
    if candidate and coverage.get("candidate_partial"):
        partial_ids.update(str(job.get("workflow_id", "")) for job in report.get("jobs", []))
    affected = []
    for job in report.get("jobs", []):
        if str(job.get("workflow_id", "")) in partial_ids:
            job["evidence"] = "partial"
            affected.append((str(job.get("workflow", "")), str(job.get("job", ""))))
    if not affected:
        return
    affected_set = set(affected)
    report["observations"] = [item for item in report.get("observations", [])
        if (str(item.get("workflow", "")), str(item.get("job", ""))) not in affected_set
        or item.get("kind") not in {"slowdown", "step_change", "creep"}]
    existing = {(item.get("workflow"), item.get("job"), item.get("kind"))
                for item in report["observations"]}
    for workflow, job in sorted(affected_set):
        if (workflow, job, "partial") not in existing:
            report["observations"].append({"kind": "partial", "workflow": workflow,
                "job": job, "conclusion": "neutral",
                "reason": "history collection was incomplete"})


def _fill_attribution(repo: str, report: dict[str, Any], client: GhClient,
                      max_calls: int = 5) -> None:
    cache = {}
    calls = 0
    for item in report.get("observations", []):
        attribution = item.get("attribution")
        if not isinstance(attribution, dict) or attribution.get("pr_number") is not None:
            continue
        sha = attribution.get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        if sha not in cache:
            if calls >= max_calls:
                attribution.update({"status": "partial", "pr_number": None,
                                    "candidates": [], "reason": "attribution call cap reached"})
                continue
            calls += 1
            try:
                doc = client.get(f"repos/{repo}/commits/{sha}/pulls?per_page=10")
                numbers = sorted({row.get("number") for row in doc
                                  if isinstance(row, dict) and row.get("merged_at")
                                  and isinstance(row.get("number"), int)}) \
                    if isinstance(doc, list) else []
                cache[sha] = numbers
            except CollectionError as exc:
                attribution.update({"status": "partial", "pr_number": None,
                                    "candidates": [], "reason": str(exc)})
                continue
        candidates = cache[sha]
        attribution["candidates"] = candidates
        if len(candidates) == 1:
            attribution.update({"status": "unique", "pr_number": candidates[0]})
        elif candidates:
            attribution.update({"status": "ambiguous", "pr_number": None})
        else:
            attribution.update({"status": "unavailable", "pr_number": None})


def _md(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|").replace("`", "'")[:300]


def render_markdown(result: dict[str, Any], *, max_bytes: int = 65536) -> str:
    lines = ["## CI speedup shift-left", "",
             "Advisory timing evidence only. Silence does not establish that CI stayed fast.", ""]
    if result.get("status") == "coverage_gap":
        lines.extend(["**Coverage gap:** " + _md(result.get("reason", "unknown")), ""])
    for layer_name in ("l2", "l3"):
        report = result.get(layer_name)
        if not isinstance(report, dict):
            continue
        lines.extend([f"### {layer_name.upper()}", ""])
        observations = report.get("observations", [])
        if not observations:
            lines.append("- Thresholds were not crossed in the collected comparable evidence.")
        for item in observations:
            line = f"- **{_md(item.get('kind', 'unknown'))}:** "
            line += f"{_md(item.get('workflow', ''))} / {_md(item.get('job', ''))}"
            if item.get("change_percent") is not None:
                line += f" ({_md(item['change_percent'])}% versus baseline)"
            if isinstance(item.get("attribution"), dict):
                line += f"; attribution={_md(item['attribution'].get('status', 'unresolved'))}"
            lines.append(line)
        lines.append("")
    coverage = result.get("coverage")
    if isinstance(coverage, dict) and any(coverage.values()):
        lines.extend(["### Coverage", "", "- " + _md(json.dumps(coverage, sort_keys=True)), ""])
    rendered = "\n".join(lines).rstrip() + "\n"
    if len(rendered.encode("utf-8")) > max_bytes:
        suffix = "\n- Summary truncated at the configured output limit.\n"
        budget = max_bytes - len(suffix.encode("utf-8"))
        rendered = rendered.encode("utf-8")[:max(0, budget)].decode("utf-8", "ignore") + suffix
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--max-runs", type=int, default=20)
    parser.add_argument("--api-budget", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--deadline-seconds", type=int, default=120)
    parser.add_argument("--output-byte-limit", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not L2_MIN_BASELINE <= args.max_runs <= 50:
        parser.error(f"--max-runs must be between {L2_MIN_BASELINE} and 50")
    if not 1 <= args.api_budget <= 110:
        parser.error("--api-budget must be between 1 and 110")
    if not 10 <= args.deadline_seconds <= 300:
        parser.error("--deadline-seconds must be between 10 and 300")
    if not 4096 <= args.output_byte_limit <= 4 * 1024 * 1024:
        parser.error("--output-byte-limit must be between 4096 and 4194304")
    started = time.monotonic()
    client = GhClient(args.api_budget, args.timeout_seconds, args.deadline_seconds)
    try:
        main_records, pr_records, coverage = collect_history(
            args.repo, args.branch, pr_number=args.pr, max_runs=args.max_runs, client=client)
        l3 = analyze_l3(main_records)
        _apply_coverage(l3, coverage)
        _fill_attribution(args.repo, l3, client)
        partial = bool(coverage.get("partial_workflows") or coverage.get("candidate_partial")
                       or coverage.get("workflow_list_partial") or coverage.get("omitted_workflows"))
        result: dict[str, Any] = {"status": "partial" if partial else "measured",
            "job_conclusion": "success", "repository": args.repo, "branch": args.branch,
            "coverage": coverage, "l3": l3}
        if args.pr is not None:
            l2 = analyze_l2(main_records, pr_records)
            _apply_coverage(l2, coverage, candidate=True)
            result.update({"pull_request": args.pr, "l2": l2})
    except CollectionError as exc:
        result = {"status": "coverage_gap", "job_conclusion": "success",
            "repository": args.repo, "branch": args.branch, "reason": str(exc),
            "promise": "no performance conclusion was produced"}
    result["provenance"] = {"api_calls": client.calls, "api_budget": client.budget,
        "elapsed_s": round(time.monotonic() - started, 3),
        "completed_successful_jobs_only": True}
    summary_limit = min(65536, max(1024, args.output_byte_limit // 4))
    result["summary_markdown"] = render_markdown(result, max_bytes=summary_limit)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > args.output_byte_limit:
        result = {"status": "coverage_gap", "job_conclusion": "success",
            "repository": args.repo, "branch": args.branch,
            "reason": "serialized output exceeded configured byte limit",
            "provenance": result["provenance"]}
        result["summary_markdown"] = render_markdown(result, max_bytes=summary_limit)
        text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
