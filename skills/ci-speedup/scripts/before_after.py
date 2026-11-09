"""Conservative push-cohort comparison for post-fix CI verification."""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

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
    """Advance a persisted post-fix verification session.

    The client owns GitHub transport. Its methods are intentionally small so
    acceptance tests can prove the write boundary without network access.
    Repository writes are limited to context_path beside scratch artifacts.
    """
    env = os.environ if environ is None else environ
    try:
        target_runs = max(0, min(4, int(env.get("CI_SPEEDUP_VERIFY_RUNS", "2"))))
    except ValueError:
        target_runs = 2
    if target_runs == 0:
        return {"status": "disabled", "verdict": None}

    context_file = Path(context_path)
    head = client.remote_head(repo, branch)
    if not head:
        return {"status": "ineligible", "verdict": None}
    initial = client.completed_cohort(repo, head)
    if initial is None:
        return {"status": "waiting-for-first-cohort", "head_sha": head,
                "verdict": None}

    saved: dict[str, Any] = {}
    if context_file.exists():
        try:
            loaded = json.loads(context_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = loaded
        except (OSError, json.JSONDecodeError):
            saved = {}
    moved = bool(saved.get("head_sha") and saved.get("head_sha") != head)
    same_binding = (
        saved.get("repository") == repo
        and saved.get("baseline_artifact") == baseline_artifact
        and saved.get("head_sha") == head
    )
    if same_binding and saved.get("verdict") is not None:
        return {"status": "complete", "head_sha": head,
                "sample_count": len(saved.get("samples") or []),
                "verdict": saved["verdict"], "reused": True,
                "discarded_moved_head": False}

    samples = list(saved.get("samples") or []) if same_binding else [initial]
    workflow_ids = list(initial.get("expected_workflows") or
                        initial.get("workflows") or [])
    context: dict[str, Any] = {
        "repository": repo,
        "baseline_artifact": baseline_artifact,
        "head_sha": head,
        "workflow_identities": workflow_ids,
        "samples": samples,
        "run_attempt_ids": [
            item for sample in samples
            for item in sample.get("run_attempt_ids") or []
        ],
        "verdict": None,
    }

    def persist() -> None:
        context_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = context_file.with_name(context_file.name + ".partial")
        temporary.write_text(
            json.dumps(context, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        temporary.replace(context_file)

    if cache_sensitive:
        pushes = client.default_branch_cohorts(repo, after_head=head, limit=3)
        distinct: list[dict[str, Any]] = []
        seen_heads: set[str] = set()
        for cohort in pushes:
            push_head = str(cohort.get("head_sha") or "")
            if push_head and push_head not in seen_heads:
                seen_heads.add(push_head)
                distinct.append(cohort)
        context["samples"] = distinct
        context["run_attempt_ids"] = [
            item for sample in distinct
            for item in sample.get("run_attempt_ids") or []
        ]
        if len(distinct) < 3:
            persist()
            emit("Cache-sensitive verification needs 3 distinct consecutive "
                 "default-branch pushes; reruns are not valid samples.")
            return {"status": "waiting-for-default-branch-pushes",
                    "head_sha": head, "sample_count": len(distinct),
                    "needed": 3, "verdict": None,
                    "discarded_moved_head": moved}
        samples = distinct
    else:
        wanted = max(1, target_runs)
        if len(samples) < wanted:
            jobs = initial.get("jobs") or []
            wall_s = observed_commit_wall_s(jobs) or 0.0
            runner_s = sum(
                max(0.0, _seconds(str(job["completed_at"]))
                    - _seconds(str(job["started_at"])))
                for job in jobs
                if job.get("started_at") and job.get("completed_at"))
            remaining = wanted - len(samples)
            emit("Measuring the speedup — rerunning the complete CI cohort "
                 f"{remaining}× (~{remaining * wall_s / 60:.1f} more minutes "
                 f"of wall-clock, ~{remaining * runner_s / 60:.1f} "
                 "runner-minutes).")
        while len(samples) < wanted:
            cohort = client.rerun_cohort(repo, initial)
            if cohort is None:
                context["samples"] = samples
                persist()
                return {"status": "waiting-for-rerun", "head_sha": head,
                        "sample_count": len(samples), "verdict": None,
                        "discarded_moved_head": moved}
            if str(cohort.get("head_sha") or "") != head:
                context["samples"] = samples
                persist()
                return {"status": "moved-head", "head_sha": head,
                        "sample_count": len(samples), "verdict": None,
                        "discarded_moved_head": True}
            samples.append(cohort)
            context["samples"] = samples
            context["run_attempt_ids"] = [
                item for sample in samples
                for item in sample.get("run_attempt_ids") or []
            ]
            persist()

    if cache_sensitive:
        verdict: dict[str, Any] = {
            "status": "measured",
            "head_sha": head,
            "sample_count": len(samples),
            "sample_heads": [str(sample.get("head_sha") or "")
                             for sample in samples],
            "changed_files": list(changed_files),
            "cache_sensitive": True,
        }
    else:
        try:
            baseline_doc = json.loads(
                Path(baseline_artifact).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            baseline_doc = {}
        before = (baseline_doc.get("verification_baseline")
                  if isinstance(baseline_doc, dict) else None)
        if (not isinstance(before, list) or not before) and isinstance(
                baseline_doc, dict):
            before = _baseline_from_findings(baseline_doc)
        if not isinstance(before, list) or not before:
            context["samples"] = samples
            persist()
            return {"status": "baseline-unavailable", "head_sha": head,
                    "sample_count": len(samples), "verdict": None,
                    "discarded_moved_head": moved}
        verdict = compare_push_cohorts(
            before, samples, changed_files=list(changed_files))
        while (int((verdict.get("sampling") or {}).get("need") or 0)
               > len(samples) and len(samples) < 4):
            jobs = initial.get("jobs") or []
            wall_s = observed_commit_wall_s(jobs) or 0.0
            runner_s = sum(
                max(0.0, _seconds(str(job["completed_at"]))
                    - _seconds(str(job["started_at"])))
                for job in jobs
                if job.get("started_at") and job.get("completed_at"))
            emit("Measured variance needs one more complete cohort "
                 f"(~{wall_s / 60:.1f} more minutes of wall-clock, "
                 f"~{runner_s / 60:.1f} runner-minutes).")
            cohort = client.rerun_cohort(repo, initial)
            if cohort is None:
                context["samples"] = samples
                persist()
                return {"status": "waiting-for-rerun", "head_sha": head,
                        "sample_count": len(samples), "verdict": None,
                        "discarded_moved_head": moved}
            samples.append(cohort)
            context["samples"] = samples
            context["run_attempt_ids"] = [
                item for sample in samples
                for item in sample.get("run_attempt_ids") or []
            ]
            persist()
            verdict = compare_push_cohorts(
                before, samples, changed_files=list(changed_files))
    context["samples"] = samples
    context["verdict"] = verdict
    persist()
    return {"status": "complete", "head_sha": head,
            "sample_count": len(samples), "verdict": verdict,
            "reused": False, "discarded_moved_head": moved}


class GhVerificationClient:
    """Small authenticated-gh adapter for the post-fix write boundary."""

    def __init__(self, *, poll_s: float = 10.0, timeout_s: float = 3600.0):
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    @staticmethod
    def _api(endpoint: str, *, method: str = "GET") -> Any:
        cmd = ["gh", "api"]
        if method != "GET":
            cmd += ["--method", method]
        cmd.append(endpoint)
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=90)
        if result.returncode != 0:
            return None
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def remote_head(self, repo: str, branch: str) -> str | None:
        payload = self._api(f"repos/{repo}/branches/{quote(branch, safe='')}")
        if not isinstance(payload, dict):
            return None
        commit = payload.get("commit")
        return str(commit.get("sha") or "") if isinstance(commit, dict) else None

    @staticmethod
    def _run_rows(payload: Any, head: str) -> list[dict[str, Any]]:
        rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
        return [row for row in rows or []
                if isinstance(row, dict)
                and str(row.get("head_sha") or "") == head]

    def _jobs(self, repo: str, run_id: int, attempt: int) -> list[dict[str, Any]] | None:
        endpoint = (f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                    "?per_page=100")
        payload = self._api(endpoint)
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)] if rows is not None else None

    def _cohort(self, repo: str, head: str, runs: list[dict[str, Any]],
                *, identity: str) -> dict[str, Any] | None:
        if not runs or any(row.get("status") != "completed"
                           or row.get("conclusion") != "success" for row in runs):
            return None
        workflows = [str(row.get("name") or row.get("workflow_id") or "")
                     for row in runs]
        if any(not value for value in workflows) or len(set(workflows)) != len(workflows):
            return None
        jobs: list[dict[str, Any]] = []
        run_attempt_ids: list[dict[str, int]] = []
        for run, workflow in zip(runs, workflows):
            run_id = int(run.get("id") or 0)
            attempt = int(run.get("run_attempt") or 1)
            if not run_id:
                return None
            fetched = self._jobs(repo, run_id, attempt)
            if not fetched:
                return None
            run_attempt_ids.append({"run_id": run_id, "attempt": attempt})
            for job in fetched:
                if (not job.get("created_at") or not job.get("started_at")
                        or not job.get("completed_at")
                        or job.get("conclusion") != "success"):
                    return None
                labels = job.get("labels") or []
                steps = [dict(step) for step in job.get("steps") or []
                         if isinstance(step, dict)]
                jobs.append({
                    "name": str(job.get("name") or ""),
                    "workflow": workflow,
                    "head_sha": head,
                    "run_attempt": attempt,
                    "created_at": job["created_at"],
                    "started_at": job["started_at"],
                    "completed_at": job["completed_at"],
                    "runner_label": str(labels[0] if labels else
                                        job.get("runner_name") or ""),
                    "runner_class": ("self-hosted" if "self-hosted" in labels
                                     else "github-hosted"),
                    "runner_image": "",
                    "steps": steps,
                })
        return {
            "head_sha": head,
            "cohort_id": identity,
            "attempt": max(item["attempt"] for item in run_attempt_ids),
            "position": max(item["attempt"] for item in run_attempt_ids),
            "conclusion": "success",
            "workflows": workflows,
            "expected_workflows": workflows,
            "jobs": jobs,
            "logs": [],
            "run_attempt_ids": run_attempt_ids,
            "lane": str(runs[0].get("event") or ""),
            "lane_runs_30d": None,
        }

    def completed_cohort(self, repo: str, head: str) -> dict[str, Any] | None:
        payload = self._api(
            f"repos/{repo}/actions/runs?head_sha={quote(head)}&per_page=100")
        rows = [row for row in self._run_rows(payload, head)
                if int(row.get("run_attempt") or 1) == 1]
        if not rows:
            return None
        latest_created = max(str(row.get("created_at") or "") for row in rows)
        latest_event = next(str(row.get("event") or "") for row in rows
                            if str(row.get("created_at") or "") == latest_created)
        event_rows = [row for row in rows
                      if str(row.get("event") or "") == latest_event]
        return self._cohort(repo, head, event_rows,
                            identity=f"{head}:{latest_event}:initial")

    def rerun_cohort(self, repo: str,
                     cohort: dict[str, Any]) -> dict[str, Any] | None:
        members = cohort.get("run_attempt_ids") or []
        if not members:
            return None
        for member in members:
            if self._api(
                    f"repos/{repo}/actions/runs/{int(member['run_id'])}/rerun",
                    method="POST") is None:
                return None
        deadline = time.monotonic() + self.timeout_s
        runs: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            runs = []
            settled = True
            for member in members:
                run = self._api(
                    f"repos/{repo}/actions/runs/{int(member['run_id'])}")
                if not isinstance(run, dict):
                    return None
                if int(run.get("run_attempt") or 1) <= int(member["attempt"]):
                    settled = False
                if run.get("status") != "completed":
                    settled = False
                runs.append(run)
            if settled:
                break
            time.sleep(self.poll_s)
        else:
            return None
        head = str(cohort.get("head_sha") or "")
        attempt = max(int(row.get("run_attempt") or 1) for row in runs)
        return self._cohort(repo, head, runs,
                            identity=f"{head}:rerun:{attempt}")

    def default_branch_cohorts(self, repo: str, after_head: str,
                               limit: int) -> list[dict[str, Any]]:
        info = self._api(f"repos/{repo}")
        branch = str(info.get("default_branch") or "") if isinstance(info, dict) else ""
        if not branch:
            return []
        payload = self._api(
            f"repos/{repo}/actions/runs?event=push&branch={quote(branch)}&per_page=100")
        rows = payload.get("workflow_runs") if isinstance(payload, dict) else []
        by_head: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for row in rows or []:
            if not isinstance(row, dict) or row.get("event") != "push":
                continue
            head = str(row.get("head_sha") or "")
            if head not in by_head:
                by_head[head] = []
                order.append(head)
            by_head[head].append(row)
        if after_head not in order:
            return []
        # GitHub returns newest first. Keep the named first push and pushes that
        # followed it, then restore chronological order for position semantics.
        candidate_heads = list(reversed(order[:order.index(after_head) + 1]))
        cohorts: list[dict[str, Any]] = []
        for head in candidate_heads:
            cohort = self._cohort(repo, head, by_head[head],
                                  identity=f"{head}:push")
            if cohort is not None:
                cohorts.append(cohort)
            if len(cohorts) >= limit:
                break
        return cohorts


def _seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _median(values: Iterable[float]) -> float | None:
    rows = list(values)
    return round(float(statistics.median(rows)), 3) if rows else None


def _baseline_from_findings(doc: dict[str, Any]) -> list[dict[str, Any]]:
    clock = ((doc.get("pr_critical_path") or {}).get("commit_wall_clock") or {})
    cohorts: list[dict[str, Any]] = []
    for fact in clock.get("facts") or []:
        if not isinstance(fact, dict) or fact.get("partial") is True:
            continue
        sha = str(fact.get("sha") or "")
        raw_jobs = [row for row in fact.get("jobs") or []
                    if isinstance(row, dict)]
        workflows = sorted({str(row.get("workflow_file") or "")
                            for row in raw_jobs if row.get("workflow_file")})
        if not sha or not raw_jobs or not workflows:
            continue
        jobs = [{
            "name": str(row.get("name") or ""),
            "workflow": str(row.get("workflow_file") or ""),
            "head_sha": sha,
            "run_attempt": 1,
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "runner_label": str(row.get("runner") or ""),
            "runner_class": "",
            "runner_image": "",
            "steps": [],
        } for row in raw_jobs]
        cohorts.append({
            "head_sha": sha,
            "cohort_id": f"baseline:{sha}",
            "attempt": 1,
            "position": len(cohorts) + 1,
            "conclusion": "success",
            "workflows": workflows,
            "expected_workflows": workflows,
            "jobs": jobs,
            "logs": [],
            "lane": str(fact.get("event") or "push"),
            "lane_runs_30d": None,
        })
    return cohorts


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
