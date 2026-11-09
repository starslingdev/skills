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


def advance_verification(
    client: Any,
    *,
    repo: str,
    branch: str,
    baseline_artifact: str,
    context_path: Any,
    changed_files: list[str],
    cache_sensitive: bool = False,
    environ: dict[str, str] | None = None,
    emit: Any = print,
) -> dict[str, Any]:
    """Advance a persisted post-fix verification session."""
    return {"status": "pending", "verdict": None}


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
    seen: set[tuple[str, int]] = set()
    for cohort in cohorts:
        identity = str(cohort.get("cohort_id") or "unknown")
        if cohort.get("conclusion") != "success":
            failed += 1
            excluded.append(identity)
            continue
        head_sha = str(cohort.get("head_sha") or "")
        attempt = int(cohort.get("attempt") or 1)
        sample_key = (identity, attempt)
        if not head_sha or sample_key in seen:
            excluded.append(identity)
            continue
        seen.add(sample_key)
        if bound_sha and head_sha != bound_sha:
            excluded.append(identity)
            continue
        workflows = {str(v) for v in cohort.get("workflows") or []}
        expected_raw = cohort.get("expected_workflows")
        expected = {str(v) for v in expected_raw or []}
        if not workflows or not expected_raw or workflows != expected:
            excluded.append(identity)
            continue
        jobs = [j for j in cohort.get("jobs") or [] if isinstance(j, dict)]
        if ({str(j.get("workflow") or "") for j in jobs} != workflows
                or any(not j.get("started_at")
                       or j.get("run_attempt") is None
                       or int(j["run_attempt"]) != attempt
                       or not j.get("head_sha")
                       or str(j["head_sha"]) != head_sha
                       for j in jobs)):
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


def _sample_work(sample: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted(
        (str(job.get("name") or ""),
         tuple(sorted(str(v) for v in job.get("tests") or [])))
        for job in sample.get("jobs") or []))


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


def _cached_count(samples: list[dict[str, Any]]) -> float | None:
    per_sample: list[int] = []
    for sample in samples:
        values: list[int] = []
        for text in sample.get("logs") or []:
            values.extend(int(m.group(1)) for m in _CACHED_COUNT.finditer(str(text)))
        if values:
            per_sample.append(sum(values))
    return _median(per_sample)


def _workload_state(before: list[dict[str, Any]],
                    after: list[dict[str, Any]]) -> tuple[str, str]:
    before_shapes = {_sample_work(sample) for sample in before}
    after_shapes = {_sample_work(sample) for sample in after}
    if len(before_shapes) != 1 or len(after_shapes) != 1:
        return "changed", "named work differed between sampled cohorts"
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
        if before_shapes == after_shapes:
            return "same", "named tests and jobs match"
    return "unknown", "workload identity was not established"


def _runner_set(samples: list[dict[str, Any]]) -> set[tuple[tuple[str, str, str, str], ...]]:
    return {tuple(sorted(
        (str(j.get("name") or ""), str(j.get("runner_label") or ""),
         str(j.get("runner_class") or ""), str(j.get("runner_image") or ""))
        for j in sample.get("jobs") or [])) for sample in samples}


def _workflow_set(samples: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    return {tuple(sorted(str(w) for w in sample.get("workflows") or []))
            for sample in samples}


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


def _compute_median(samples: list[dict[str, Any]]) -> float | None:
    return _median(sum(_job_duration(job) for job in sample.get("jobs") or [])
                   for sample in samples)


def _changed_step(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> tuple[str, float, float] | None:
    samples = before + after
    marked = {str(job.get("name") or "") for sample in samples
              for job in sample.get("jobs") or [] if job.get("changed_step") is True}
    per_sample_names = [
        {str(step.get("name") or "") for job in sample.get("jobs") or []
         for step in job.get("steps") or []}
        for sample in samples]
    common = set.intersection(*per_sample_names) if per_sample_names else set()
    candidates = marked & common if marked else common
    if len(candidates) != 1:
        return None
    name = next(iter(candidates))

    def durations(rows: list[dict[str, Any]]) -> list[float]:
        values: list[float] = []
        for sample in rows:
            matches = [step for job in sample.get("jobs") or []
                       for step in job.get("steps") or []
                       if str(step.get("name") or "") == name]
            if len(matches) != 1:
                return []
            values.append(_job_duration(matches[0]))
        return values

    before_values, after_values = durations(before), durations(after)
    if not before_values or not after_values:
        return None
    return name, float(_median(before_values)), float(_median(after_values))


def compare_push_cohorts(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    changed_files: list[str],
    ci_paths: tuple[str, ...] = (
        ".github/", "scripts/ci/", "Dockerfile", "docker-compose"),
) -> dict[str, Any]:
    """Compare fetched cohorts without inventing missing comparability facts."""
    after_heads = {str(row.get("head_sha") or "") for row in after
                   if row.get("conclusion") == "success"}
    bound_sha = next(iter(after_heads)) if len(after_heads) == 1 and "" not in after_heads else None
    before_samples, before_excluded, before_failed = _prepare(before)
    after_samples, after_excluded, after_failed = (
        _prepare(after, bound_sha) if bound_sha else ([], [str(
            row.get("cohort_id") or "unknown") for row in after],
            sum(row.get("conclusion") != "success" for row in after)))
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
                       and after_cached != before_cached)
    cache_regressed = cache_regressed or (
        all(s["cache_class"] == "warm"
            and s["cache_reason"] == "log cache hit" for s in before_samples)
        and any(s["cache_class"] == "cold" for s in after_samples))
    if cache_regressed:
        state, workload_evidence = "changed", "cache replay fell on compared work"

    ci_only = bool(changed_files) and all(
        any(path == prefix or path.startswith(prefix)
            or ("/" not in prefix and path.endswith("/" + prefix))
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
    before_total = _compute_median(before_samples)
    after_total = _compute_median(after_samples)
    step = _changed_step(before_samples, after_samples)
    runners_match = (len(before_runners) == len(after_runners) == 1
                     and before_runners == after_runners)
    workflows_match = (len(before_workflows) == len(after_workflows) == 1
                       and before_workflows == after_workflows)
    after_runner_rows = next(iter(after_runners)) if len(after_runners) == 1 else ()
    runner_evidence = bool(after_runner_rows) and all(
        label and kind for _job, label, kind, _image in after_runner_rows)
    lane_facts = {(str(sample.get("lane") or ""), sample.get("lane_runs_30d"))
                  for sample in after_samples}
    lane_evidence = (len(lane_facts) == 1
                     and all(value not in (None, "")
                             for value in next(iter(lane_facts))))
    comparable = (runners_match and workflows_match and runner_evidence
                  and lane_evidence)
    enough_samples = len(before_samples) >= 2 and len(after_samples) >= 2
    sampling_pending = need > len(after_samples)
    noisy_at_cap = spread_pct > 30.0 and len(after_samples) >= 4
    no_change = bool(
        set(s.get("head_sha") for s in before_samples)
        & set(s.get("head_sha") for s in after_samples)
        and any(int(s.get("attempt") or 1) > 1 for s in after_samples))
    clean = (comparable and ci_only and state == "same" and enough_samples
             and not sampling_pending and not noisy_at_cap and not no_change
             and step is not None)
    before_med = _median(float(s["wall_s"]) for s in before_samples)
    after_med = _median(float(s["wall_s"]) for s in headline)
    saving = round(float(before_med) - float(after_med), 3) if clean else None
    step_line = ("Changed step: unavailable (no uniquely matched step timing)."
                 if step is None else
                 f"Changed step: {step[0]}, {step[1]:.1f}s before and "
                 f"{step[2]:.1f}s after")
    queue_line = (
        "Queue/work delta: unavailable."
        if bq is None or aq is None or bw is None or aw is None
        else f"Queue delta: {aq - bq:+.1f}s; work delta: {aw - bw:+.1f}s.")
    runner_rows = after_runner_rows
    runner_line = "Runner class: " + ", ".join(
        f"{job}: {label or 'unknown'} ({kind or 'unknown'})"
        for job, label, kind, _image in runner_rows)
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
        (f"Total job duration: {float(before_total):.1f}s before, "
         f"{float(after_total):.1f}s after (median per cohort; compute disclosure only)."),
        runner_line,
        f"Lane: {lane}; {lane_runs if lane_runs is not None else 'unknown'} runs in 30 days.",
        f"Cold evidence: {cold_reasons}.",
        step_line,
    ])
    reason = ("comparable" if comparable else
              "triggered workflow set changed" if not workflows_match else
              "runner class changed" if not runners_match else
              "runner class unavailable" if not runner_evidence else
              "lane evidence unavailable")
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
