"""Conservative push-cohort comparison for post-fix CI verification."""
from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any, Iterable

from wall_clock import observed_commit_wall_s

_COLD = re.compile(r"(?i)cache (?:key )?miss|not found in cache|cache.*evict|branch.scope")
_WARM = re.compile(r"(?i)cache hit|from-cache|cache hit, replaying|\(cached\)|\bcached\b")
_CACHED_COUNT = re.compile(
    r"(?i)\b(\d+)\s+(?:(?:packages?|tests?|steps?|lines?).{0,20}\(cached\)|"
    r"(?:go\s+)?cached(?:\s+lines?)?)")


def _seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _median(values: Iterable[float]) -> float | None:
    rows = list(values)
    return round(float(statistics.median(rows)), 3) if rows else None


def _summary(samples: list[dict[str, Any]], *, values: bool = False) -> dict[str, Any]:
    wall_values = [float(s["wall_s"]) for s in samples]
    if not wall_values:
        out: dict[str, Any] = {"n": 0, "median_s": None, "range_s": []}
    else:
        out = {"n": len(wall_values), "median_s": _median(wall_values),
               "range_s": [min(wall_values), max(wall_values)]}
    if values:
        out["values_s"] = wall_values
    return out


def _cache_class(cohort: dict[str, Any]) -> tuple[str, str]:
    log = "\n".join(str(v) for v in cohort.get("logs") or [])
    cold = _COLD.search(log)
    if cold:
        return "cold", cold.group(0)
    if _WARM.search(log):
        return "warm", "log cache hit"
    return (("cold", "first run") if int(cohort.get("position") or 0) == 1
            else ("warm", "later run"))


def _prepare(cohorts: list[dict[str, Any]], bound_sha: str | None = None
             ) -> tuple[list[dict[str, Any]], list[str], int]:
    samples: list[dict[str, Any]] = []
    excluded: list[str] = []
    failed = 0
    for cohort in cohorts:
        identity = str(cohort.get("cohort_id") or "unknown")
        if cohort.get("conclusion") != "success":
            failed += 1
            excluded.append(identity)
            continue
        if bound_sha and cohort.get("head_sha") != bound_sha:
            excluded.append(identity)
            continue
        workflows = {str(v) for v in cohort.get("workflows") or []}
        expected = {str(v) for v in cohort.get("expected_workflows") or workflows}
        if workflows != expected:
            excluded.append(identity)
            continue
        jobs = [j for j in cohort.get("jobs") or [] if isinstance(j, dict)]
        attempt = int(cohort.get("attempt") or 1)
        if any((j.get("run_attempt") is not None
                and int(j["run_attempt"]) != attempt)
               or (j.get("head_sha") is not None
                   and str(j["head_sha"]) != str(cohort.get("head_sha") or ""))
               for j in jobs):
            excluded.append(identity)
            continue
        wall = observed_commit_wall_s(jobs)
        if wall is None or not cohort.get("workflows"):
            excluded.append(identity)
            continue
        row = dict(cohort)
        row["wall_s"] = wall
        row["cache_class"], row["cache_reason"] = _cache_class(cohort)
        samples.append(row)
    return samples, excluded, failed


def _job_identity(samples: list[dict[str, Any]]) -> set[str]:
    return {str(j.get("name") or "") for s in samples for j in s.get("jobs") or []}


def _tests(samples: list[dict[str, Any]]) -> tuple[set[str], bool]:
    found: set[str] = set()
    has_names = False
    for sample in samples:
        for job in sample.get("jobs") or []:
            if job.get("tests"):
                has_names = True
                found.update(str(v) for v in job["tests"])
    return found, has_names


def _has_shards(samples: list[dict[str, Any]]) -> bool:
    return any(j.get("shard") for s in samples for j in s.get("jobs") or [])


def _cached_count(samples: list[dict[str, Any]]) -> int | None:
    values: list[int] = []
    for sample in samples:
        for text in sample.get("logs") or []:
            values.extend(int(m.group(1)) for m in _CACHED_COUNT.finditer(str(text)))
    return sum(values) if values else None


def _workload_state(before: list[dict[str, Any]],
                    after: list[dict[str, Any]]) -> tuple[str, str]:
    before_tests, before_named = _tests(before)
    after_tests, after_named = _tests(after)
    if _has_shards(before) != _has_shards(after):
        return "changed", "test work was re-sharded"
    if before_named and after_named:
        removed = before_tests - after_tests
        added = after_tests - before_tests
        if removed and added:
            return "changed", "named test work both appeared and disappeared"
        if removed:
            return "reduced", "missing tests: " + ", ".join(sorted(removed))
        if added:
            return "increased", "added tests: " + ", ".join(sorted(added))
        if _job_identity(before) == _job_identity(after):
            return "same", "named tests and jobs match"
    return "unknown", "workload identity was not established"


def _runner_set(samples: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(j.get("runner_label") or ""), str(j.get("runner_class") or ""))
            for s in samples for j in s.get("jobs") or []}


def _workflow_set(samples: list[dict[str, Any]]) -> set[str]:
    return {str(w) for s in samples for w in s.get("workflows") or []}


def _queue_work(samples: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    queues: list[float] = []
    work: list[float] = []
    for sample in samples:
        jobs = sample.get("jobs") or []
        starts = [_seconds(str(j["started_at"])) for j in jobs if j.get("started_at")]
        created = [_seconds(str(j["created_at"])) for j in jobs if j.get("created_at")]
        if starts and created:
            queue = max(0.0, min(starts) - min(created))
            queues.append(queue)
            work.append(max(0.0, float(sample["wall_s"]) - queue))
    return _median(queues), _median(work)


def _job_duration(job: dict[str, Any]) -> float:
    return max(0.0, _seconds(str(job["completed_at"])) -
               _seconds(str(job["started_at"])))


def compare_push_cohorts(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    changed_files: list[str],
    ci_paths: tuple[str, ...] = (
        ".github/", "scripts/ci/", "Dockerfile", "docker-compose"),
) -> dict[str, Any]:
    """Compare fetched cohorts without inventing missing comparability facts."""
    bound_sha = str(after[0].get("head_sha") or "") if after else None
    before_samples, before_excluded, before_failed = _prepare(before)
    after_samples, after_excluded, after_failed = _prepare(after, bound_sha)
    base: dict[str, Any] = {
        "excluded": before_excluded + after_excluded,
        "failed_count": before_failed + after_failed,
        "lower_bound": False,
    }
    if not before_samples or not after_samples:
        return {**base, "available": False,
                "reason": "no complete successful exact-head cohort"}

    before_runners, after_runners = _runner_set(before_samples), _runner_set(after_samples)
    before_workflows, after_workflows = _workflow_set(before_samples), _workflow_set(after_samples)
    state, workload_evidence = _workload_state(before_samples, after_samples)
    before_cached, after_cached = _cached_count(before_samples), _cached_count(after_samples)
    cache_regressed = (before_cached is not None and after_cached is not None
                       and after_cached < before_cached)
    if cache_regressed:
        state, workload_evidence = "changed", "cache replay fell on compared work"

    ci_only = all(any(path == prefix or path.startswith(prefix)
                      for prefix in ci_paths) for path in changed_files)
    warm = [s for s in after_samples if s["cache_class"] == "warm"]
    cold = [s for s in after_samples if s["cache_class"] == "cold"]
    headline = warm or after_samples
    values = [float(s["wall_s"]) for s in after_samples]
    median = float(statistics.median(values))
    spread_pct = ((max(values) - min(values)) / median * 100.0
                  if len(values) > 1 and median else 0.0)
    need = min(4, len(values) + 1) if len(values) < 2 or spread_pct > 30.0 else len(values)
    sampling = {
        "need": need, "cap": 4,
        "reason": (f"samples differ by {spread_pct:.1f}%" if spread_pct > 30.0
                   else "minimum met" if len(values) >= 2
                   else "one sample is indicative"),
    }

    bq, bw = _queue_work(before_samples)
    aq, aw = _queue_work(after_samples)
    before_total = sum(_job_duration(j) for s in before_samples for j in s.get("jobs") or [])
    after_total = sum(_job_duration(j) for s in after_samples for j in s.get("jobs") or [])
    comparable = before_runners == after_runners and before_workflows == after_workflows
    clean = comparable and ci_only and state == "same"
    before_med = _median(float(s["wall_s"]) for s in before_samples)
    after_med = _median(float(s["wall_s"]) for s in headline)
    saving = round(float(before_med) - float(after_med), 3) if clean else None
    no_change = bool(
        set(s.get("head_sha") for s in before_samples)
        & set(s.get("head_sha") for s in after_samples)
        and any(int(s.get("attempt") or 1) > 1 for s in after_samples))

    first_before = (before_samples[0].get("jobs") or [{}])[0]
    first_after = (after_samples[0].get("jobs") or [{}])[0]
    step_line = (
        f"Changed step: {first_after.get('name') or 'unknown'}, "
        f"{_job_duration(first_before):.1f}s before and "
        f"{_job_duration(first_after):.1f}s after")
    queue_line = (
        "Queue/work delta: unavailable."
        if bq is None or aq is None or bw is None or aw is None
        else f"Queue delta: {aq - bq:+.1f}s; work delta: {aw - bw:+.1f}s.")
    runner_line = "Runner class: " + ", ".join(
        f"{label or 'unknown'} ({kind or 'unknown'})"
        for label, kind in sorted(after_runners))
    lane = str(after_samples[0].get("lane") or "unknown")
    lane_runs = after_samples[0].get("lane_runs_30d")
    cold_reasons = ", ".join(str(s["cache_reason"]) for s in cold) or "none observed"
    report = "\n".join([
        f"Sample size: {len(after_samples)} fresh complete runs.",
        f"Asymmetry: {len(before_samples)} historical push cohorts versus one exact-head branch cohort.",
        f"CI-only scope: {'passed' if ci_only else 'product files also changed'}; workload is {state} ({workload_evidence}).",
        f"Variance: {min(values):.1f}s-{max(values):.1f}s; {sampling['reason']}.",
        f"Cache state: {len(cold)} cold and {len(warm)} warm; warm reruns are optimistic.",
        "Environment: CI wall-clock depends on runner tier, load, queue and concurrency.",
        queue_line,
        (f"Total job duration: {before_total:.1f}s before, "
         f"{after_total:.1f}s after (compute disclosure only)."),
        runner_line,
        f"Lane: {lane}; {lane_runs if lane_runs is not None else 'unknown'} runs in 30 days.",
        f"Cold evidence: {cold_reasons}.",
        step_line,
    ])
    reason = ("comparable" if comparable else
              "runner class changed" if before_runners != after_runners else
              "triggered workflow set changed")
    return {
        **base, "available": comparable, "reason": reason,
        "before": _summary(before_samples, values=True),
        "after": {"warm": _summary(warm), "cold": _summary(cold, values=True),
                  "all_complete": _summary(after_samples, values=True),
                  "headline": _summary(headline)},
        "sampling": sampling, "workload_state": state,
        "workload_evidence": workload_evidence, "ci_only": ci_only,
        "clean_attribution": clean, "verified_saving": saving,
        "no_change_rerun": no_change,
        "cache": {"before_cached": before_cached, "after_cached": after_cached,
                  "regressed": cache_regressed},
        "queue_delta_s": None if bq is None or aq is None else round(aq - bq, 3),
        "work_delta_s": None if bw is None or aw is None else round(aw - bw, 3),
        "total_job_duration": {"before_s": round(before_total, 3),
                               "after_s": round(after_total, 3)},
        "report": report,
    }
