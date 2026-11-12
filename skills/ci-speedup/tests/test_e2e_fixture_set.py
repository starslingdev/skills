"""Small whole-pipeline fixtures for report-level interaction coverage."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402
import summary  # noqa: E402


HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE / "fixtures" / "e2e"
CATALOG = HERE.parent / "references" / "patterns"

_VR_SPEC = importlib.util.spec_from_file_location(
    "e2e_fixture_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


class FixtureClient:
    """Small recorded-response client; unknown optional reads stay unavailable."""

    gave_up = False

    def __init__(self, recorded: dict) -> None:
        self.recorded = recorded
        self.queries = 0
        self.errors = 0
        self.job_reads: dict[str, int] = {}
        self.log_reads: dict[str, int] = {}

    def available(self) -> bool:
        return not self.gave_up

    def _bump(self, *, error: bool = False) -> None:
        if error:
            self.errors += 1

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        expire_after = self.recorded.get("expire_after")
        if isinstance(expire_after, int) and self.queries > expire_after:
            self.errors += 1
            self.gave_up = True
            return None
        repo = self.recorded["repo"]
        if endpoint == f"repos/{repo}":
            return {"default_branch": self.recorded.get("default_branch", "main")}
        if endpoint == f"repos/{repo}/actions/cache/usage":
            value = self.recorded.get("cache_usage_bytes")
            return ({"active_caches_size_in_bytes": value}
                    if isinstance(value, int) else None)
        match = re.match(
            rf"repos/{re.escape(repo)}/pulls/(\d+)/files\?", endpoint)
        if match:
            rows = self.recorded.get("changed_files", {}).get(match.group(1))
            return ([{"filename": name} for name in rows]
                    if isinstance(rows, list) else None)
        if endpoint.startswith(f"repos/{repo}/actions/workflows?"):
            return {"workflows": [
                {"id": row["id"], "path": row["path"],
                 "name": row.get("name", Path(row["path"]).stem)}
                for row in self.recorded["workflows"]
            ]}
        match = re.match(
            rf"repos/{re.escape(repo)}/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            workflow = next(
                row for row in self.recorded["workflows"]
                if row["id"] == int(match.group(1)))
            query = parse_qs(match.group(2), keep_blank_values=True)
            pages = workflow.get("run_pages")
            page = int((query.get("page") or ["1"])[0])
            if (not query.get("status")
                    and page in set(workflow.get("failed_all_status_pages") or [])):
                self._bump(error=True)
                return None
            if isinstance(pages, list):
                runs = list(pages[page - 1]) if 0 < page <= len(pages) else []
            else:
                runs = list(workflow.get("runs", []))
            for key, field in (("event", "event"), ("branch", "head_branch")):
                if query.get(key):
                    runs = [
                        row for row in runs
                        if str(row.get(field) or "") == query[key][0]
                    ]
            if query.get("status"):
                wanted = query["status"][0]
                runs = [
                    row for row in runs
                    if wanted in {str(row.get("status") or ""),
                                  str(row.get("conclusion") or "")}
                ]
            if query.get("created"):
                created = query["created"][0]
                if created.startswith(">="):
                    runs = [row for row in runs
                            if str(row.get("created_at") or "") >= created[2:]]
                elif created.startswith("<="):
                    runs = [row for row in runs
                            if str(row.get("created_at") or "") <= created[2:]]
            if query.get("per_page") == ["1"]:
                return {"total_count": len(runs)}
            return {"workflow_runs": runs}
        match = re.match(
            rf"repos/{re.escape(repo)}/actions/runs/(\d+)/jobs", endpoint)
        if match:
            run_id = match.group(1)
            self.job_reads[run_id] = self.job_reads.get(run_id, 0) + 1
            return {"jobs": self.recorded.get("jobs", {}).get(run_id, [])}
        match = re.match(
            rf"repos/{re.escape(repo)}/commits/([^/]+)/check-runs", endpoint)
        if match:
            return {"check_runs": self.recorded.get("checks", {}).get(
                match.group(1), [])}
        if endpoint == f"repos/{repo}/rulesets?includes_parents=true":
            return []
        if endpoint == (
                f"repos/{repo}/branches/{self.recorded.get('default_branch', 'main')}"
                "/protection/required_status_checks"):
            return {"contexts": self.recorded.get("required_checks", ["gate"])}
        if "/commits?path=" in endpoint:
            return self.recorded.get("commits_by_path", [])
        match = re.match(
            rf"repos/{re.escape(repo)}/contents/(.+)\?ref=(.+)", endpoint)
        if match:
            return self.recorded.get("workflow_blobs", {}).get(match.group(2))
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        match = re.search(r"/actions/jobs/(\d+)/logs$", endpoint)
        if match:
            job_id = match.group(1)
            self.log_reads[job_id] = self.log_reads.get(job_id, 0) + 1
            return self.recorded.get("logs", {}).get(job_id, "")
        return ""


def run_fixture(path: Path, monkeypatch, tmp_path: Path, *,
                max_runs: int = 20) -> tuple[dict, str, list]:
    recorded = json.loads((path / "responses.json").read_text(encoding="utf-8"))
    repo_root = tmp_path / f"{path.name}-repo"
    repo_root.mkdir()
    for entry in path.iterdir():
        if entry.name in {"analyses.json", "expected.json", "responses.json", "workflows"}:
            continue
        if entry.is_dir():
            shutil.copytree(entry, repo_root / entry.name)
        else:
            shutil.copy2(entry, repo_root / entry.name)
    workflow_root = repo_root / ".github/workflows"
    workflow_root.mkdir(parents=True)
    for workflow in (path / "workflows").glob("*.yml"):
        shutil.copy2(workflow, workflow_root / workflow.name)
    client = FixtureClient(recorded)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(cr, "_root_is_clone_of", lambda *args, **kwargs: True)
    monkeypatch.setattr(cr, "_workflows_are_dirty", lambda *args, **kwargs: False)
    monkeypatch.setattr(cr, "_root_branch_skew", lambda *args, **kwargs: None)
    scanned = scan.scan(
        repo_root, CATALOG, default_branch=recorded.get("default_branch", "main"))
    scanned["skill_commit_sha"] = "installed:unversioned"
    doc = cr.collect(
        scanned, recorded["repo"], max_runs=max_runs,
        shallow_runs=recorded.get("shallow_runs", 20),
        with_logs=True, data_dir=tmp_path / f"{path.name}-data",
        created_before=recorded["captured_at"], root=repo_root)
    logs: dict[str, str] = {}
    steps: dict[str, dict] = {}
    mags: dict[str, dict] = {}
    log_runs: dict[str, str] = {}
    bundle = doc.get("data_bundle") or {}
    entries = bundle.get("logs") or []
    logs_dir = Path(bundle.get("logs_dir") or tmp_path)
    for key, entry in zip(summary._render_keys(entries), entries):
        if "=" in key:
            continue
        if entry.get("file"):
            logs[key] = (logs_dir / entry["file"]).read_text()
        if entry.get("steps_file"):
            steps[key] = json.loads((logs_dir / entry["steps_file"]).read_text())
        if entry.get("mag_file"):
            mags[key] = json.loads((logs_dir / entry["mag_file"]).read_text())
        if entry.get("html_url"):
            log_runs[key] = entry["html_url"]
    analyses_path = path / "analyses.json"
    analyses = (json.loads(analyses_path.read_text(encoding="utf-8"))
                if analyses_path.exists() else None)
    report = bp.render(
        doc, logs, steps=steps, mags=mags, log_runs=log_runs,
        captured_at=recorded["captured_at"], analyses=analyses)
    out = tmp_path / path.name
    out.mkdir()
    findings_path = out / "findings.json"
    report_path = out / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    doc["_fixture_job_reads"] = dict(client.job_reads)
    doc["_fixture_log_reads"] = dict(client.log_reads)
    return doc, report, checks


def fixture_facts(doc: dict, checks: list) -> dict:
    cp = doc.get("pr_critical_path") or {}
    clock = cp.get("commit_wall_clock") or {}
    complete_clock_facts = [
        row for row in clock.get("facts") or []
        if row.get("partial") is not True
        and isinstance(row.get("wall_s"), (int, float))
    ]
    clock_p50 = clock.get("p50_s")
    representative = (
        min(complete_clock_facts,
            key=lambda row: abs(float(row["wall_s"]) - float(clock_p50)))
        if complete_clock_facts and isinstance(clock_p50, (int, float)) else {})
    lane = doc.get("default_branch_push_lane") or {}
    graph = doc.get("workflow_job_graph") or {}
    bundle = doc.get("data_bundle") or {}
    logs_dir = Path(bundle.get("logs_dir") or ".")
    leaves = []
    for entry in bundle.get("logs") or []:
        if not entry.get("file"):
            continue
        leaf = bp._parse_log(
            (logs_dir / entry["file"]).read_text(),
            doc.get("test_runner_isolation"),
        )
        if leaf and leaf.get("fix_key"):
            leaves.append(str(leaf["fix_key"]))
    leaves.sort()
    credited = [
        {"pattern": row.get("pattern"),
         "wall_clock_s": row.get("wall_clock_p50_s"),
         "runner_min": row.get("runner_min_saving")}
        for row in doc.get("findings", [])
        if ((isinstance(row.get("wall_clock_p50_s"), (int, float))
             and row.get("wall_clock_p50_s") > 0)
            or (isinstance(row.get("runner_min_saving"), (int, float))
                and row.get("runner_min_saving") > 0))
    ]
    return {
        "headline": {
            "check": cp.get("critical_path_check"),
            "seconds": cp.get("critical_path_s"),
        },
        "gate_kind": cp.get("gate_kind"),
        "provenance": cp.get("provenance"),
        "commit_wall_clock": {
            "p50_s": clock.get("p50_s"), "p90_s": clock.get("p90_s"),
            "queue_p50_s": clock.get("queue_p50_s"),
            "queue_p90_s": clock.get("queue_p90_s"),
            "work_p50_s": clock.get("work_p50_s"),
            "work_p90_s": clock.get("work_p90_s"),
        },
        "representative_jobs": {
            "slowest": (representative.get("slowest_job") or {}).get("name"),
            "next": (representative.get("next_longest_job") or {}).get("name"),
        },
        "detected_patterns": sorted({
            str(row.get("pattern")) for row in doc.get("findings", [])
            if row.get("pattern")
        }),
        "config_eras": [
            {key: row.get(key) for key in (
                "workflow_file", "kept_era", "rule", "pre_count", "post_count")}
            for row in cp.get("config_eras") or []
        ],
        "minority_tails": sorted(
            row.get("name") for row in cp.get("checks") or []
            if row.get("minority_tail")),
        "workflow_shapes": {
            "matrix_jobs": sorted(
                f"{workflow}:{name}" for workflow, jobs in graph.items()
                for name, spec in jobs.items() if spec.get("matrix")),
            "reusable_jobs": sorted(
                f"{workflow}:{name}" for workflow, jobs in graph.items()
                for name, spec in jobs.items() if spec.get("reusable")),
        },
        "credited_findings": credited,
        "lane": {
            "sampled_pushes": lane.get("sampled_push_count"),
            "job_fetches": lane.get("job_fetch_count"),
            "p50_s": lane.get("p50_s"),
        },
        "leaves": leaves,
        "verifier": {
            "fail": sorted(check.name for check in checks if not check.skipped and not check.ok),
            "skip": sorted(check.name for check in checks if check.skipped),
        },
    }


def _first_fact_difference(expected, actual, path: str = "") -> tuple[str, object, object]:
    """Return the first compact-fact path that moved."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else str(key)
            if key not in expected or key not in actual:
                return child, expected.get(key), actual.get(key)
            if expected[key] != actual[key]:
                return _first_fact_difference(expected[key], actual[key], child)
    if isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                return _first_fact_difference(left, right, f"{path}[{index}]")
        if len(expected) != len(actual):
            return f"{path}.length", len(expected), len(actual)
    return path, expected, actual


def assert_fixture_facts(fixture: Path, actual: dict) -> None:
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    if actual != expected:
        path, before, after = _first_fact_difference(expected, actual)
        raise AssertionError(
            f"{fixture.name}: {path} changed from {before!r} to {after!r}")


def test_fixture_corpus_has_broad_interaction_coverage():
    fixtures = sorted(
        path for path in (FIXTURE_ROOT.iterdir() if FIXTURE_ROOT.exists() else ())
        if path.is_dir())

    assert len(fixtures) >= 10, (
        f"expected at least 10 whole-pipeline fixtures, found {len(fixtures)}")


def test_compact_facts_pin_collected_provenance_and_representative_job(
        monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    facts = fixture_facts(doc, checks)

    assert facts["provenance"] == "required_scoped"
    assert facts["representative_jobs"]["slowest"] == "test"


def test_serial_chain_sizes_removed_edge_and_failure_path_compute(
        monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    doc, report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    finding = next(row for row in doc["findings"]
                   if row.get("pattern") == "OPT21"
                   and row.get("affected_jobs") == ["test"])
    stamp = finding["opt21_edge_sizing"]
    assert stamp["chain_before_s"] == 1320.0
    assert stamp["chain_after_s"] == 1200.0
    assert finding["wall_clock_p50_s"] == 120.0
    assert stamp["dependency_failure_share"] == 0.25
    assert stamp["extra_runner_min_per_run"] == 5.0
    assert "can add up to 5.00 extra runner-minutes" in finding["evidence"]
    assert "can add up to" in report
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_serial_chain_commit_wall_facts_retain_job_ids_for_verification_logs(
        monkeypatch, tmp_path):
    doc, _report, checks = run_fixture(
        FIXTURE_ROOT / "serial-chain", monkeypatch, tmp_path)
    facts = doc["pr_critical_path"]["commit_wall_clock"]["facts"]
    jobs = [job for fact in facts for job in fact.get("jobs", [])]

    assert jobs
    assert all(isinstance(job.get("id"), int) for job in jobs)
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_serial_chain_keeps_a_go_script_lint_as_a_gate(monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)

    assert not [row for row in doc["findings"]
                if row.get("pattern") == "OPT21"
                and row.get("affected_jobs") == ["checked-test"]]
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_opt21_edge_sizing_certificate_rejects_a_tampered_path_delta(
        monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    doc, report, _checks = run_fixture(fixture, monkeypatch, tmp_path)
    finding = next(row for row in doc["findings"]
                   if row.get("opt21_edge_sizing"))
    finding["opt21_edge_sizing"]["chain_before_s"] += 100.0
    out = tmp_path / fixture.name
    findings_path = out / "findings.json"
    report_path = out / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")

    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    assert any(
        check.name == "OPT21 edge sizing re-derives from the workflow graph"
        and not check.ok
        for check in checks
    )


def test_opt21_edge_delta_is_not_capped_to_the_short_dependent_job(
        monkeypatch, tmp_path):
    fixture = tmp_path / "edge-cap-fixture"
    shutil.copytree(FIXTURE_ROOT / "serial-chain", fixture)
    responses_path = fixture / "responses.json"
    recorded = json.loads(responses_path.read_text(encoding="utf-8"))
    for run in recorded["workflows"][0]["runs"]:
        if run.get("conclusion") == "success":
            day = str(run["created_at"])[:10]
            run["updated_at"] = f"{day}T00:20:10Z"
    for jobs in recorded["jobs"].values():
        if len(jobs) != 3:
            continue
        day = str(jobs[0]["started_at"])[:10]
        spans = ((0, 600), (600, 610), (610, 1210))
        for job, (start_s, end_s) in zip(jobs, spans):
            job["started_at"] = f"{day}T00:{start_s // 60:02d}:{start_s % 60:02d}Z"
            job["completed_at"] = f"{day}T00:{end_s // 60:02d}:{end_s % 60:02d}Z"
            job["steps"][0]["started_at"] = job["started_at"]
            job["steps"][0]["completed_at"] = job["completed_at"]
    for checks in recorded["checks"].values():
        day = str(checks[0]["started_at"])[:10]
        spans = ((0, 600), (600, 610), (610, 1210))
        for check, (start_s, end_s) in zip(checks, spans):
            check["started_at"] = f"{day}T00:{start_s // 60:02d}:{start_s % 60:02d}Z"
            check["completed_at"] = f"{day}T00:{end_s // 60:02d}:{end_s % 60:02d}Z"
    responses_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    finding = next(row for row in doc["findings"]
                   if row.get("pattern") == "OPT21"
                   and row.get("affected_jobs") == ["test"])
    assert finding["opt21_edge_sizing"]["chain_before_s"] == 1210.0
    assert finding["opt21_edge_sizing"]["chain_after_s"] == 610.0
    assert finding["wall_clock_p50_s"] == 600.0
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_opt21_failure_share_uses_one_bounded_recent_run_cohort(
        monkeypatch, tmp_path):
    doc, _report, checks = run_fixture(
        FIXTURE_ROOT / "serial-chain", monkeypatch, tmp_path, max_runs=2)
    finding = next(row for row in doc["findings"]
                   if row.get("pattern") == "OPT21"
                   and row.get("affected_jobs") == ["test"])
    stamp = finding["opt21_edge_sizing"]
    assert stamp["dependency_sampled_runs"] == 2
    assert stamp["dependency_failed_runs"] == 0
    assert stamp["dependency_failure_share"] == 0.0
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_fixture_client_applies_run_query_filters():
    recorded = json.loads(
        (FIXTURE_ROOT / "push-job-list" / "responses.json").read_text())
    client = FixtureClient(recorded)
    repo = recorded["repo"]
    workflow_id = recorded["workflows"][0]["id"]

    prs = client.json(
        f"repos/{repo}/actions/workflows/{workflow_id}/runs?"
        "per_page=100&event=pull_request&status=success")
    pushes = client.json(
        f"repos/{repo}/actions/workflows/{workflow_id}/runs?"
        "per_page=1&event=push&branch=main&status=success")

    assert {row["event"] for row in prs["workflow_runs"]} == {"pull_request"}
    assert pushes["total_count"] == 1


def test_fixture_materialization_excludes_recording_and_expectation_files(
        monkeypatch, tmp_path):
    real_scan = scan.scan

    def guarded_scan(root, *args, **kwargs):
        assert not (root / "responses.json").exists()
        assert not (root / "expected.json").exists()
        return real_scan(root, *args, **kwargs)

    monkeypatch.setattr(scan, "scan", guarded_scan)
    run_fixture(FIXTURE_ROOT / "serial-chain", monkeypatch, tmp_path)


def test_named_fixtures_exercise_their_defining_interactions(
        monkeypatch, tmp_path):
    docs = {}
    reports = {}
    for name in (
            "cache-lints", "config-era-change", "duplicate-gate-job", "emulated-container",
            "external-required-gate", "heavy-runner-queue", "minority-tail",
            "multiarch-image", "path-partitioned-monorepo", "pr-light-matrix", "vendored-libs",
            "vitest-isolated-serial", "cargo-double-compile", "per-test-startup",
            "jest-make-hop", "sharded-floor", "replayed-test-cache",
            "path-filter-unmeasured", "lighthouse-passes"):
        docs[name], reports[name], _checks = run_fixture(
            FIXTURE_ROOT / name, monkeypatch, tmp_path)

    assert docs["config-era-change"]["pr_critical_path"]["config_eras"]
    assert (docs["config-era-change"]["pr_critical_path"]["sample_stop_reason"]
            == "era_door")
    assert docs["config-era-change"]["pr_critical_path"]["sample_complete"] is False
    cache_rows = [row for row in docs["cache-lints"]["findings"]
                  if row.get("pattern") in {"OPT4", "OPT61", "OPT62"}]
    assert {row["pattern"] for row in cache_rows} == {"OPT4", "OPT61", "OPT62"}
    assert all(row.get("wall_clock_p50_s") in {None, 0.0} for row in cache_rows)
    assert not any(
        row.get("pattern") == "OPT61"
        and "restore-family" in (row.get("affected_jobs") or [])
        for row in cache_rows)
    assert not any(
        row.get("pattern") == "OPT61"
        and "env-ccache" in (row.get("affected_jobs") or [])
        for row in cache_rows)
    assert any(
        row.get("pattern") == "OPT4"
        and "export-only" in (row.get("affected_jobs") or [])
        for row in cache_rows)
    assert not any(
        row.get("pattern") == "OPT4"
        and "cache-dance" in (row.get("affected_jobs") or [])
        for row in cache_rows)
    static_cache_rows = [row for row in cache_rows if row.get("static_cache_lint")]
    assert static_cache_rows
    assert all(row.get("cache_family") for row in static_cache_rows)
    assert all(not (row.get("cache_log_confirmation") or {}).get("misses")
               for row in static_cache_rows)
    assert all(
        not float(row.get("wall_clock_p50_s") or 0)
        and not float(row.get("runner_min_saving") or 0)
        for row in static_cache_rows)
    external = docs["external-required-gate"]["pr_critical_path"]
    assert external["gate_kind"] == "pr_floor_fallback"
    assert external["provenance"] == "pr_floor_fallback"
    assert (docs["heavy-runner-queue"]["pr_critical_path"]
            ["commit_wall_clock"]["queue_p50_s"]) > 0
    assert any(
        row.get("minority_tail")
        for row in docs["minority-tail"]["pr_critical_path"]["checks"])
    assert "Across commits, the median projection is" in reports["minority-tail"]
    assert "`ci.yml` holds its slowest job" not in reports["minority-tail"]
    assert "OPT25" not in {
        row.get("pattern") for row in docs["pr-light-matrix"]["findings"]}
    unmeasured = next(
        row for row in docs["path-filter-unmeasured"]["findings"]
        if row.get("pattern") == "OPT32")
    assert unmeasured["path_filter_measurement"]["status"] == "unmeasured"
    assert "hit rate not measured" in reports["path-filter-unmeasured"]
    duplicate = next(
        row for row in docs["duplicate-gate-job"]["findings"]
        if row.get("pattern") == "OPT12"
        and row.get("opt12_duplicate_test_work"))
    assert duplicate["wall_clock_p50_s"] == 0.0
    assert duplicate["runner_min_saving"] == 6.0
    assert duplicate["opt12_duplicate_test_work"]["duplicate_p50_s"] == 90.0
    assert duplicate["opt12_duplicate_test_work"]["workflow_run_wall_p50_s"] == 0.0
    assert "OPT102" in {
        row.get("pattern") for row in docs["multiarch-image"]["findings"]}
    assert "OPT76" not in {
        row.get("pattern") for row in docs["multiarch-image"]["findings"]}
    assert "OPT89" in {
        row.get("pattern") for row in docs["emulated-container"]["findings"]}
    assert any(
        row.get("pattern") == "OPT89"
        and "make all" in row.get("evidence_snippet", "")
        for row in docs["emulated-container"]["findings"])
    emulated_workflow = (
        FIXTURE_ROOT / "emulated-container" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    assert "run: make all $EXTRA" in emulated_workflow
    assert "./configure\n            make -j2" in emulated_workflow
    image_doc = docs["multiarch-image"]
    image_opt72 = [
        row for row in image_doc["findings"]
        if row.get("pattern") == "OPT72" and "image" in (row.get("affected_jobs") or [])
    ]
    assert not any(float(row.get("wall_clock_p50_s") or 0.0) > 0 for row in image_opt72)
    assert "prefer warming the cache" not in reports["multiarch-image"].lower()
    assert "cache path: measure warm-vs-cold" not in reports["multiarch-image"].lower()
    assert "warm-cache claim" not in reports["multiarch-image"].lower()
    assert any(
        pole.get("job") == "image"
        for pole in image_doc["pr_critical_path"]["poles"])
    assert "emulated-image-compile" in fixture_facts(image_doc, [])["leaves"]
    assert "vendored-lib-build" in fixture_facts(
        docs["vendored-libs"], [])["leaves"]
    assert "vendored libraries built from source" in reports["vendored-libs"]
    lighthouse = docs["lighthouse-passes"]
    assert "lighthouse-serial-audits" in fixture_facts(lighthouse, [])["leaves"]
    lighthouse_pole = next(
        pole for pole in lighthouse["pr_critical_path"]["poles"]
        if pole.get("job") == "gate")
    assert lighthouse_pole["dominant_category"] == "scan"
    assert ("<!-- ci-speedup:leaf-crown fix_key=lighthouse-serial-audits -->"
            in reports["lighthouse-passes"])
    assert "Lighthouse CI runs 40 full page audits" in reports["lighthouse-passes"]
    # The two passes share one report folder, so the side-by-side lever is held back.
    assert "OPT83" not in {
        row.get("pattern") for row in lighthouse["findings"]}
    assert [row["gate"] for row in lighthouse["opt83_withheld_candidates"]] == [
        "siblings_share_tool_state"]
    # OPT103 credits no saving, so the generic OPT75 route on the pole it owns does not
    # credit one either (the OPT102 precedent for OPT72).
    opt75 = [row for row in lighthouse["findings"] if row.get("pattern") == "OPT75"
             and "gate" in (row.get("affected_jobs") or [])]
    assert opt75 and all(row["wall_clock_p50_s"] == 0.0 for row in opt75), opt75
    assert "test-isolated-serial" in fixture_facts(
        docs["vitest-isolated-serial"], [])["leaves"]
    assert "describe.concurrent" in reports["vitest-isolated-serial"]
    assert "cargo-double-compile" in fixture_facts(
        docs["cargo-double-compile"], [])["leaves"]
    assert "Compiling goport_core" in reports["cargo-double-compile"]
    assert "combine their `-p`/`--package`" in reports["cargo-double-compile"]
    assert "OPT9" not in {
        row.get("pattern") for row in docs["jest-make-hop"]["findings"]}
    jest = next(row for row in docs["jest-make-hop"]["findings"]
                if row.get("pattern") == "OPT88")
    assert "maxWorkers" in jest["evidence"] and "OPT88" in jest["evidence"]
    assert "no provider-switch saving is credited" in reports["jest-make-hop"]
    assert "per-invocation-startup" in fixture_facts(
        docs["per-test-startup"], [])["leaves"]
    assert "state-leak canary" in reports["per-test-startup"]
    replayed_pole = next(
        pole for pole in docs["replayed-test-cache"]["pr_critical_path"]["poles"]
        if pole.get("job") == "build")
    assert replayed_pole["replayed_test_results"]["cached_lines"] == 144
    assert replayed_pole["replayed_test_results"]["test_result_lines"] == 145
    assert "Replayed test results" in reports["replayed-test-cache"]
    assert any(
        row.get("cache_net_negative", {}).get("kind") == "opt79_buildkit_export"
        for row in image_doc["findings"])
    assert any(
        spec.get("matrix")
        for jobs in docs["path-partitioned-monorepo"]["workflow_job_graph"].values()
        for spec in jobs.values())
    restored_workflow = (FIXTURE_ROOT / "path-partitioned-monorepo" / "workflows"
                         / "reusable-tests.yml").read_text()
    assert "package: [api, web]" in restored_workflow
    assert "runtime: [20, 22]" in restored_workflow
    assert "shard:" not in restored_workflow
    assert any(
        row.get("name", "").startswith("tests / test (")
        for row in docs["path-partitioned-monorepo"]["pr_critical_path"]["checks"])
    path_facts = docs["path-partitioned-monorepo"]["pr_critical_path"][
        "commit_wall_clock"]["facts"]
    assert all(
        any(row.get("node") == "gate" for row in fact.get("job_projections") or [])
        for fact in path_facts if fact.get("partial") is not True)


def test_path_filter_fixture_without_pr_numbers_stays_unmeasured(
        monkeypatch, tmp_path):
    doc, report, checks = run_fixture(
        FIXTURE_ROOT / "path-filter-unmeasured", monkeypatch, tmp_path)
    measurement = next((
        row.get("path_filter_measurement")
        for row in doc["findings"] if row.get("pattern") == "OPT32"
    ), None)

    assert measurement is not None
    assert measurement["status"] == "unmeasured"
    assert measurement["reason"] == "no PR number on sampled runs"
    assert "hit rate not measured (no PR number on sampled runs)" in report
    assert not [check for check in checks if not check.skipped and not check.ok]


@pytest.mark.parametrize(("family", "pattern", "miss", "hit", "credited"), [
    ("ccache", "OPT61", "Cache not found for input keys: ccache-linux-old",
     "Cache restored from key: ccache-linux-stable", True),
    ("ccache", "OPT61", "Cache not found for input keys: pnpm-linux-old",
     "Cache restored from key: pnpm-linux-stable", False),
    ("buildkit", "OPT4", "ERROR importing cache manifest from gha: not found",
     "#5 CACHED", True),
    ("buildkit", "OPT4", "Cache not found for input keys: pnpm-linux-old",
     "Cache restored from key: pnpm-linux-stable", False),
    ("go", "OPT62", "Go build cache miss",
     "Go cache restored from key: go-build-stable", True),
    ("go", "OPT62", "Cache not found for input keys: cargo-linux-old",
     "Cache restored from key: cargo-linux-stable", False),
])
def test_static_cache_lint_with_measured_miss_can_be_credited(
        monkeypatch, tmp_path, family, pattern, miss, hit, credited):
    fixture = tmp_path / "inputs" / "cache-lint-confirmed"
    fixture.parent.mkdir()
    shutil.copytree(FIXTURE_ROOT / "cache-lints", fixture)
    responses = fixture / "responses.json"
    recorded = json.loads(responses.read_text(encoding="utf-8"))
    recorded["repo"] = "fixture/cache-lint-confirmed"
    recorded["required_checks"] = ["build"]
    recorded["cache_usage_bytes"] = 0
    recorded["changed_files"] = {
        "1": ["src/a.cc"], "2": ["src/b.cc"], "3": ["src/c.cc"]}
    for number, workflow_run in enumerate(recorded["workflows"][0]["runs"], 1):
        workflow_run["pull_requests"] = [{"number": number}]
        sha = workflow_run["head_sha"]
        recorded["checks"][sha] = [{
            "name": "build",
            "started_at": workflow_run["run_started_at"],
            "completed_at": workflow_run["updated_at"],
        }]
        jobs = recorded["jobs"][str(workflow_run["id"])]
        build = next(job for job in jobs if job["name"] == "build")
        recorded["jobs"][str(workflow_run["id"])] = [build]
        day = build["started_at"][:10]
        install_end = "01:02" if number == 1 else "00:12"
        build["steps"] = [
            {"name": "Restore compiler cache",
             "started_at": f"{day}T00:00:00Z", "completed_at": f"{day}T00:00:02Z"},
            {"name": "Install dependencies",
             "started_at": f"{day}T00:00:02Z", "completed_at": f"{day}T00:{install_end}Z"},
            {"name": "Build",
             "started_at": f"{day}T00:{install_end}Z", "completed_at": f"{day}T00:01:58Z"},
            {"name": "Post Restore compiler cache",
             "started_at": f"{day}T00:01:58Z", "completed_at": f"{day}T00:02:00Z"},
        ]
    miss = (f"{miss}\nCache size: 50 MB\n"
            "Cache restored in 2s\nCache saved in 2s")
    hit = (f"{hit}\nCache size: 50 MB\n"
           "Cache restored in 2s\nCache saved in 2s")
    recorded["logs"] = {"1101": miss, "1201": miss, "1301": hit}
    responses.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    family_steps = {
        "ccache": """
    env:
      CCACHE_MAXSIZE: 50M
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-${{ hashFiles('compiler.lock') }}
      - run: cmake --build build
""",
        "buildkit": """
    steps:
      - run: >-
          docker buildx build --cache-to type=gha,scope=image-${{ github.sha }} .
""",
        "go": """
    steps:
      - run: go clean -i -cache
""",
    }
    workflow = fixture / "workflows" / "ci.yml"
    workflow.write_text(f"""name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest{family_steps[family]}""", encoding="utf-8")
    (fixture / "expected.json").write_text("{}\n", encoding="utf-8")

    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    confirmed = [row for row in doc["findings"]
                 if row.get("static_cache_lint")
                 and row.get("pattern") == pattern
                 and family in " ".join(str(row.get(key) or "")
                                        for key in ("evidence", "subject")).lower()
                 and "build" in (row.get("affected_jobs") or [])
                 and row.get("cache_log_confirmation", {}).get("misses")]
    if credited:
        assert confirmed
        assert all(isinstance(row.get("cache_sizing_guard"), dict)
                   for row in confirmed), json.dumps(confirmed, indent=2)
        if family == "ccache":
            assert all(row["cache_sizing_guard"]["status"] == "passed"
                       for row in confirmed), json.dumps(confirmed, indent=2)
            assert any(
                float(row.get("wall_clock_p50_s") or 0) > 0
                or float(row.get("runner_min_saving") or 0) > 0
                for row in confirmed), json.dumps(confirmed, indent=2)
        else:
            assert all(
                row["cache_sizing_guard"]["status"] != "passed"
                and not float(row.get("wall_clock_p50_s") or 0)
                and not float(row.get("runner_min_saving") or 0)
                for row in confirmed), json.dumps(confirmed, indent=2)
    else:
        assert not confirmed

    red = json.loads(
        (FIXTURE_ROOT / "red-default-branch" / "responses.json").read_text())
    assert any(
        run.get("event") == "push" and run.get("conclusion") == "failure"
        for workflow in red["workflows"] for run in workflow["runs"])


@pytest.mark.parametrize("statuses", [
    ("hit", "hit", "hit", "hit"),
    ("miss", "hit", "hit", "hit"),
])
def test_collect_withholds_cache_credit_without_a_majority_of_misses(
        monkeypatch, tmp_path, statuses):
    fixture = tmp_path / "inputs" / "cache-hit-distribution"
    fixture.parent.mkdir()
    shutil.copytree(FIXTURE_ROOT / "cache-lints", fixture)
    responses = fixture / "responses.json"
    recorded = json.loads(responses.read_text(encoding="utf-8"))
    recorded["repo"] = "fixture/cache-hit-distribution"
    recorded["required_checks"] = ["build"]
    recorded["cache_usage_bytes"] = 0

    extra_run = json.loads(json.dumps(recorded["workflows"][0]["runs"][-1]))
    extra_run["id"] = 1004
    extra_run["head_sha"] = "sha-d"
    extra_run["html_url"] = str(extra_run.get("html_url") or "").replace(
        "/1003", "/1004")
    recorded["workflows"][0]["runs"].append(extra_run)
    extra_jobs = json.loads(json.dumps(recorded["jobs"]["1003"]))
    for job in extra_jobs:
        old_id = str(job.get("id") or "")
        if old_id:
            job["id"] = int(old_id) + 100
            job["html_url"] = str(job.get("html_url") or "").replace(
                f"/job/{old_id}", f"/job/{job['id']}")
    recorded["jobs"]["1004"] = extra_jobs
    recorded["changed_files"] = {
        str(number): [f"src/{letter}.cc"]
        for number, letter in enumerate("abcd", 1)
    }
    log_by_status = {
        "miss": ("Cache not found for input keys: pnpm-linux-old\n"
                 "pnpm install\nCache size: 50 MB\n"
                 "Cache restored in 2s\nCache saved in 2s"),
        "hit": ("Cache restored from key: pnpm-linux-stable\n"
                "pnpm install\nCache size: 50 MB\n"
                "Cache restored in 2s\nCache saved in 2s"),
    }
    recorded["logs"] = {
        job_id: log_by_status[status]
        for job_id, status in zip(("1101", "1201", "1301", "1401"), statuses)
    }
    for number, workflow_run in enumerate(recorded["workflows"][0]["runs"], 1):
        workflow_run["pull_requests"] = [{"number": number}]
        recorded["checks"][workflow_run["head_sha"]] = [{
            "name": "build",
            "started_at": workflow_run["run_started_at"],
            "completed_at": workflow_run["updated_at"],
        }]
    responses.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    (fixture / "workflows" / "ci.yml").write_text("""name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
      - run: pnpm install
""", encoding="utf-8")
    (fixture / "expected.json").write_text("{}\n", encoding="utf-8")

    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    finding = next(row for row in doc["findings"]
                   if row.get("pattern") == "OPT5")
    assert finding.get("wall_clock_p50_s") == 0
    assert not float(finding.get("runner_min_saving") or 0)


def _pr_commit_tail_fixture(root: Path) -> Path:
    """Ten PR commits; four also carry an untouched 100-minute workflow."""
    fixture = root / "pr-commit-tail"
    workflows = fixture / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [pull_request]\njobs:\n  gate:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo test\n",
        encoding="utf-8")
    (workflows / "slow-tail.yml").write_text(
        "name: Slow tail\non: [pull_request]\njobs:\n  archive:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo archive\n",
        encoding="utf-8")

    def at(commit: int, seconds: int) -> str:
        return datetime.fromtimestamp(
            1_767_225_600 + commit * 10_000 + seconds,
            tz=timezone.utc).isoformat().replace("+00:00", "Z")

    recorded = {
        "captured_at": "2026-01-31T00:00:00Z",
        "default_branch": "main",
        "repo": "fixture/pr-commit-tail",
        "required_checks": ["gate"],
        "workflows": [
            {"id": 601, "name": "CI", "path": ".github/workflows/ci.yml",
             "runs": []},
            {"id": 602, "name": "Slow tail",
             "path": ".github/workflows/slow-tail.yml", "runs": []},
        ],
        "jobs": {},
        "checks": {},
    }
    for commit in range(10):
        sha = f"commit-tail-{commit}"
        tail_duration = 6000 if commit < 4 else (600 if commit < 6 else 10)
        for workflow_index, duration in ((0, 600), (1, tail_duration)):
            run_id = 60_000 + workflow_index * 100 + commit
            name = "gate" if workflow_index == 0 else "archive"
            recorded["workflows"][workflow_index]["runs"].append({
                "id": run_id,
                "event": "pull_request",
                "head_branch": f"feature-{commit}",
                "head_sha": sha,
                "pull_requests": [{"number": commit + 1}],
                "status": "completed",
                "conclusion": "success",
                "created_at": at(commit, 0),
                "run_started_at": at(commit, 0),
                "updated_at": at(commit, duration),
            })
            recorded["jobs"][str(run_id)] = [{
                "id": run_id * 10,
                "name": name,
                "labels": ["ubuntu-latest"],
                "started_at": at(commit, 0),
                "completed_at": at(commit, duration),
                "steps": [{
                    "name": "work",
                    "started_at": at(commit, 0),
                    "completed_at": at(commit, duration),
                }],
            }]
        recorded["checks"][sha] = [{
            "name": "gate",
            "started_at": at(commit, 0),
            "completed_at": at(commit, 600),
        }]
    (fixture / "responses.json").write_text(
        json.dumps(recorded), encoding="utf-8")
    return fixture


def test_pr_commit_projection_discloses_the_population_and_slow_tail(
        monkeypatch, tmp_path):
    fixture = _pr_commit_tail_fixture(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    clock = doc["pr_critical_path"]["commit_wall_clock"]
    assert clock["median_projection_multiple"] == 1.0
    assert clock["worst_commit"]["workflow_file"].endswith("slow-tail.yml")
    assert "Across commits, the median projection is **1.00x**" in report
    assert "`slow-tail.yml` holds its slowest job" in report
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    doctored = json.loads(json.dumps(doc))
    doctored["pr_critical_path"]["commit_wall_clock"][
        "median_projection_multiple"] = 2.0
    findings_path = tmp_path / "doctored-findings.json"
    findings_path.write_text(json.dumps(doctored), encoding="utf-8")
    check = vr.check_commit_wall_clock_projection(report, findings_path)
    assert not check.ok
    assert "median per-commit projection multiple does not re-derive" in check.detail


def test_sharded_floor_runs_through_collection_and_full_verifier(
        monkeypatch, tmp_path):
    doc, report, checks = run_fixture(
        FIXTURE_ROOT / "sharded-floor", monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    pole = next(
        row for row in doc["pr_critical_path"]["poles"]
        if row.get("shard_floor"))
    stamp = pole["shard_floor"]
    assert stamp["floor_s"] == 60.0
    assert stamp["divided_work_s"] == 90.0
    assert stamp["in_step_floor_s"] == 125.0
    assert stamp["n_shards"] == 4
    # One bounded representative-log probe is part of this qualifying path. Pin
    # the complete offline call budget so it cannot silently fan out per shard.
    assert doc["data_sources"]["gh_query_count"] == 18
    sentence = "Observed on the sampled runs, not a shard-count recommendation"
    assert report.count(sentence) == 2
    pole_section = report.split("## 🟠 Long pole 2", 1)[1].split("\n## ", 1)[0]
    assert "#### 🤖 Prompt for your coding agent" in pole_section
    assert sentence in pole_section.split("#### 🤖 Prompt for your coding agent", 1)[1]
    assert sentence not in report.split("## 📋 Contents", 1)[0]


def test_queue_by_runner_label_runs_through_collection_and_full_verifier(
        monkeypatch, tmp_path):
    doc, report, checks = run_fixture(
        FIXTURE_ROOT / "heavy-runner-queue", monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT43" and row.get("affected_jobs") == ["build"])
    labels = {
        row["runner_label"]: row
        for row in finding["queue_by_runner_label"]
    }
    slow = labels["self-hosted slow-linux"]
    fast = labels["fast-linux self-hosted"]
    cancelled = labels["cancelled-linux self-hosted"]
    assert slow == {
        "runner_label": "self-hosted slow-linux",
        "n": 11,
        "queue_p50_s": 20.0,
        "queue_p95_s": 125.0,
        "work_n": 11,
        "work_p50_s": 180.0,
    }
    assert fast == {
        "runner_label": "fast-linux self-hosted",
        "n": 11,
        "queue_p50_s": 5.0,
        "queue_p95_s": 5.0,
        "work_n": 11,
        "work_p50_s": 60.0,
    }
    assert cancelled == {
        "runner_label": "cancelled-linux self-hosted",
        "n": 0,
        "queue_p50_s": None,
        "queue_p95_s": None,
        "work_n": 0,
        "work_p50_s": None,
        "censored_n": 1,
        "censored_queue_lower_bound_max_s": 30.0,
    }
    assert finding["queue_label_comparison"] == {
        "slow_label": "self-hosted slow-linux",
        "slow_queue_p50_s": 20.0,
        "lower_label": "fast-linux self-hosted",
        "lower_queue_p50_s": 5.0,
    }
    assert "self-hosted slow-linux" in report
    assert "fast-linux self-hosted" in report
    assert "20s" in report and "5s" in report and "2m 05s" in report
    assert "one maximum per workflow run and runner label" in report
    assert "runnable-to-cancelled time is a censored lower bound" in report
    assert "**Runner-label comparison for `build`:** `self-hosted slow-linux` queued at p50 20s" in report
    assert "reduce parallelism" not in report.lower()

    table_mutant = report.replace(
        "| `self-hosted slow-linux` | 11 | 20s | 2m 05s | 11 | 3m 00s | 0 |",
        "| `self-hosted slow-linux` | 11 | 5s | 2m 05s | 11 | 3m 00s | 0 |",
    )
    mutant_checks = vr.run_checks(
        table_mutant, tmp_path / "heavy-runner-queue" / "report.md",
        tmp_path / "heavy-runner-queue" / "findings.json", skill_repo=None)
    assert any(not check.skipped and not check.ok for check in mutant_checks)

    comparison_mutant = report.replace(
        "**Runner-label comparison for `build`:** `self-hosted slow-linux` queued at p50 20s",
        "**Runner-label comparison for `build`:** `self-hosted slow-linux` queued at p50 5s",
    )
    mutant_checks = vr.run_checks(
        comparison_mutant, tmp_path / "heavy-runner-queue" / "report.md",
        tmp_path / "heavy-runner-queue" / "findings.json", skill_repo=None)
    assert any(not check.skipped and not check.ok for check in mutant_checks)


def test_queue_single_label_says_comparison_unavailable(
        monkeypatch, tmp_path):
    fixture = tmp_path / "one-queue-label"
    shutil.copytree(FIXTURE_ROOT / "heavy-runner-queue", fixture)
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    for run_jobs in responses["jobs"].values():
        for job in run_jobs:
            job["labels"] = ["self-hosted", "only-pool"]
    responses_path.write_text(json.dumps(responses), encoding="utf-8")
    workflow_path = fixture / "workflows" / "ci.yml"
    workflow_path.write_text(
        re.sub(r"\[self-hosted, [^\]]+\]", "[self-hosted, only-pool]",
               workflow_path.read_text(encoding="utf-8")),
        encoding="utf-8")

    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    finding = next(row for row in doc["findings"]
                   if row.get("pattern") == "OPT43")
    assert finding["queue_label_comparison"] == {"unavailable": True}
    assert "comparison unavailable" in report
    assert "reduce parallelism" not in report.lower()


def test_duplicate_gate_sizing_is_independently_verified(monkeypatch, tmp_path):
    doc, report, checks = run_fixture(
        FIXTURE_ROOT / "duplicate-gate-job", monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    finding = next(
        row for row in doc["findings"]
        if row.get("opt12_duplicate_test_work"))
    finding["runner_min_saving"] = 5.0
    finding["opt12_duplicate_test_work"]["runner_min_saving"] = 5.0
    findings_path = tmp_path / "duplicate-gate-job" / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    assert not vr.check_default_branch_push_lane_isolated(
        report, findings_path).ok


def test_duplicate_gate_positive_push_wall_stays_out_of_merge_credit(
        monkeypatch, tmp_path):
    fixture = tmp_path / "positive-duplicate-gate"
    shutil.copytree(FIXTURE_ROOT / "duplicate-gate-job", fixture)
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    for workflow in responses["workflows"]:
        if workflow["path"].endswith("publish.yml"):
            for run in workflow["runs"]:
                run["updated_at"] = run["created_at"].replace("00:00Z", "03:30Z")
    for run_id in ("3001", "3002", "3003"):
        test_job, publish_job = responses["jobs"][run_id]
        test_job["completed_at"] = test_job["started_at"].replace("00:00Z", "03:00Z")
        test_job["steps"][0]["completed_at"] = test_job["completed_at"]
        publish_job["started_at"] = test_job["completed_at"]
        publish_job["completed_at"] = publish_job["started_at"].replace("03:00Z", "03:30Z")
        publish_job["steps"][0]["started_at"] = publish_job["started_at"]
        publish_job["steps"][0]["completed_at"] = publish_job["completed_at"]
    responses_path.write_text(json.dumps(responses), encoding="utf-8")

    run_root = tmp_path / "positive-run"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    finding = next(
        row for row in doc["findings"]
        if row.get("opt12_duplicate_test_work"))
    stamp = finding["opt12_duplicate_test_work"]
    assert stamp["workflow_run_wall_p50_s"] == 40.0
    assert finding["wall_clock_p50_s"] == 0.0
    assert finding["wall_clock_uncapped_p50_s"] > 0
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
def _run_multiarch_log_variant(
        name: str, monkeypatch, tmp_path, transform, *, preserve_no_inspect=False):
    fixture = tmp_path / "fixture-variants" / name
    fixture.parent.mkdir(exist_ok=True)
    shutil.copytree(FIXTURE_ROOT / "multiarch-image", fixture)
    responses_path = fixture / "responses.json"
    recorded = json.loads(responses_path.read_text(encoding="utf-8"))
    inspected_prefix = next(
        log[:log.index("#10 [linux/amd64 build 2/3]")]
        for log in recorded["logs"].values() if "Nodes:" in log)

    def variant_input(log: str) -> str:
        if preserve_no_inspect or "Nodes:" in log:
            return log
        return inspected_prefix + log[log.index("#10 [linux/amd64 build 2/3]"):]

    recorded["logs"] = {
        job_id: transform(variant_input(log))
        for job_id, log in recorded["logs"].items()
    }
    responses_path.write_text(json.dumps(recorded), encoding="utf-8")
    doc, report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    facts = fixture_facts(doc, checks)
    bundle = doc.get("data_bundle") or {}
    logs_dir = Path(bundle.get("logs_dir") or tmp_path)
    facts["leaf_details"] = [
        leaf for entry in bundle.get("logs") or []
        if entry.get("file")
        for leaf in [bp._parse_log((logs_dir / entry["file"]).read_text())]
        if leaf is not None
    ]
    return doc, report, facts


def test_builder_topology_and_foreign_only_shapes_run_through_collection(
        monkeypatch, tmp_path):
    second_node = (
        "Name:                  native-arm0\n"
        "Endpoint:              tcp://native-arm.example:1234\n"
        "Status:                running\n"
        "BuildKit version:      v0.24.0\n"
        "Platforms:             linux/arm64, linux/arm/v7\n")

    _doc, _report, multi_node = _run_multiarch_log_variant(
        "multiarch-two-node", monkeypatch, tmp_path,
        lambda log: log.replace(
            "#10 [linux/amd64 build 2/3]", second_node + "#10 [linux/amd64 build 2/3]"))
    assert "emulated-image-compile" not in multi_node["leaves"]

    docker_info = (
        "##[group]Docker info\n"
        "Server:\n"
        " Name: runner-engine\n"
        " Server Version: 27.5.1\n"
        "##[endgroup]\n")
    _doc, _report, after_info = _run_multiarch_log_variant(
        "multiarch-docker-info", monkeypatch, tmp_path,
        lambda log: log.replace(
            "#10 [linux/amd64 build 2/3]", docker_info + "#10 [linux/amd64 build 2/3]"))
    emulated = next(
        leaf for leaf in after_info["leaf_details"]
        if leaf.get("fix_key") == "emulated-image-compile")
    assert emulated["magnitude"] == {
        "label": "emulated compile/install seconds", "unit": "s", "value": 120.0}
    assert "builder_topology_unconfirmed" not in emulated

    def foreign_only(log: str) -> str:
        start = log.index("#10 [linux/amd64 build 2/3]")
        end = log.index("Cache not found", start)
        return (log[:start]
                + "#15 [linux/arm/v7 build 2/3] RUN make -j2\n"
                  "#15 DONE 100.0s\n"
                + log[end:])

    _doc, report, armv7 = _run_multiarch_log_variant(
        "multiarch-foreign-only-armv7", monkeypatch, tmp_path, foreign_only)
    assert "emulated-image-compile" in armv7["leaves"]
    assert "foreign-architecture compile" in report
    assert "100s" in report

    def native_arm(log: str) -> str:
        return foreign_only(log).replace(
            "linux/amd64, linux/amd64/v2, linux/arm64, linux/arm/v7",
            "linux/arm64, linux/arm/v7")

    _doc, _report, native = _run_multiarch_log_variant(
        "multiarch-native-arm", monkeypatch, tmp_path, native_arm)
    assert "emulated-image-compile" not in native["leaves"]


def test_no_inspect_foreign_compile_runs_through_collection(
        monkeypatch, tmp_path):
    def remove_builder_inspect(log: str) -> str:
        first_layer = log.index("#10 [linux/amd64 build 2/3]")
        return ("docker buildx build --platform linux/amd64,linux/arm64 .\n"
                + log[first_layer:])

    doc, report, facts = _run_multiarch_log_variant(
        "multiarch-no-inspect", monkeypatch, tmp_path, remove_builder_inspect,
        preserve_no_inspect=True)
    leaf = next(
        row for row in facts["leaf_details"]
        if row.get("fix_key") == "emulated-image-compile")

    assert leaf["magnitude"] is None
    assert leaf["builder_topology_unconfirmed"] is True
    opt72 = [row for row in doc["findings"] if row.get("pattern") == "OPT72"]
    assert not any(float(row.get("wall_clock_p50_s") or 0.0) > 0 for row in opt72)
    assert "prefer warming the cache" not in report.lower()
    assert "cache path: measure warm-vs-cold" not in report.lower()
    assert "warm-cache claim" not in report.lower()
    assert "docker buildx inspect --bootstrap" in report
    assert "does not establish whether that work ran under emulation" in report
    assert "building a foreign architecture under emulation instead" not in report


def test_inactive_builder_runs_through_collection_as_an_uncredited_check(
        monkeypatch, tmp_path):
    def inactive_builder(log: str) -> str:
        return re.sub(r"(?m)^Platforms:.*$", "Platforms:", log)

    _doc, report, facts = _run_multiarch_log_variant(
        "multiarch-inactive-builder", monkeypatch, tmp_path, inactive_builder)
    leaf = next(
        row for row in facts["leaf_details"]
        if row.get("fix_key") == "emulated-image-compile")

    assert leaf["magnitude"] is None
    assert leaf["builder_topology_unconfirmed"] is True
    assert "#15 [linux/arm64 build 2/3] RUN go build ./..." in report
    assert "Do not size or claim a saving" in report
    assert "docker buildx rebuilds 2 layers from scratch every run" not in report
    assert "builder topology was not printed" not in report
    assert "native platform for every builder node" in report


def test_builder_detected_platform_and_dominance_guards_run_through_collection(
        monkeypatch, tmp_path):
    def configured_first(log: str) -> str:
        return log.replace(
            "linux/amd64, linux/amd64/v2, linux/arm64, linux/arm/v7",
            "linux/arm64*, linux/amd64, linux/amd64/v2, linux/arm64, linux/arm/v7")

    _doc, report, configured = _run_multiarch_log_variant(
        "multiarch-configured-platform-first", monkeypatch, tmp_path,
        configured_first)
    assert "emulated-image-compile" in configured["leaves"]
    emulated = next(
        leaf for leaf in configured["leaf_details"]
        if leaf.get("fix_key") == "emulated-image-compile")
    assert emulated["deeper"][0]["rows"][0][0] == (
        "native linux/amd64 compile/install")
    assert any("Platforms:" in line and "linux/arm64*" in line
               for line in emulated["evidence"])
    assert "native linux/amd64" in report
    assert ("Platforms:             linux/arm64*, linux/amd64" in report)

    def cold_layer_dominates(log: str) -> str:
        return log.replace(
            "#15 DONE 120.0s\n",
            "#15 DONE 120.0s\n"
            "#16 [assets 1/1] RUN download-release-assets\n"
            "#16 DONE 400.0s\n")

    _doc, _report, cold = _run_multiarch_log_variant(
        "multiarch-cold-layer-dominates", monkeypatch, tmp_path,
        cold_layer_dominates)
    assert "emulated-image-compile" not in cold["leaves"]

    def near_native_speed(log: str) -> str:
        return (log.replace("#10 DONE 20.0s", "#10 DONE 300.0s")
                .replace("#15 DONE 120.0s", "#15 DONE 320.0s"))

    _doc, _report, near_native = _run_multiarch_log_variant(
        "multiarch-near-native-speed", monkeypatch, tmp_path, near_native_speed)
    assert "emulated-image-compile" not in near_native["leaves"]

    def equal_extra_targets(log: str) -> str:
        return (log.replace("#10 DONE 20.0s", "#10 DONE 100.0s")
                .replace(
                    "#15 DONE 120.0s\n",
                    "#15 DONE 100.0s\n"
                    "#16 [linux/arm/v7 build 3/3] RUN go build ./...\n"
                    "#16 DONE 100.0s\n"))

    _doc, _report, extra_targets = _run_multiarch_log_variant(
        "multiarch-equal-extra-targets", monkeypatch, tmp_path,
        equal_extra_targets)
    assert "emulated-image-compile" not in extra_targets["leaves"]


def test_push_fixture_fetches_the_recorded_run_jobs_once(monkeypatch, tmp_path):
    doc, _report, _checks = run_fixture(
        FIXTURE_ROOT / "push-job-list", monkeypatch, tmp_path)

    assert doc["_fixture_job_reads"]["9001"] == 1


def test_default_branch_image_is_drilled_and_kept_out_of_merge_claims(
        monkeypatch, tmp_path):
    doc, report, checks = run_fixture(
        FIXTURE_ROOT / "default-branch-image", monkeypatch, tmp_path)
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT102" and row.get("lane") == "push")
    projection = finding["push_lane_projection"]

    assert finding["wall_clock_p50_s"] == 0.0
    assert projection["projected_s"] == 120.0
    assert projection["credited_s"] == 0.0  # three pushes in 30 days is rare
    assert doc["default_branch_push_lane"]["drill"]["dominant_step"] == (
        "Build multi-platform image")
    assert doc["_fixture_log_reads"]["1101"] == 1
    assert not ({"1102", "1201", "1202", "1301", "1302"}
                & set(doc["_fixture_log_reads"]))
    assert "changes the wait after merge, not the merge gate" in finding["agent_prompt"]
    assert "Build platform-independent output once" in finding["agent_prompt"]
    assert "OPT102" in report
    bottom = next(line for line in report.splitlines() if "**Bottom line.**" in line)
    assert "after merge" not in bottom.lower()
    assert "OPT102" not in bottom
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    mutant = report.replace(
        "## 📋 Contents", "## Tier 1\n\nOPT102 push-lane finding\n\n## 📋 Contents", 1)
    assert not vr.check_default_branch_push_lane_isolated(
        mutant, tmp_path / "default-branch-image" / "findings.json").ok
    bottom_mutant = report.replace(
        "**Bottom line.**", "**Bottom line.** OPT102 push-lane finding;", 1)
    assert not vr.check_default_branch_push_lane_isolated(
        bottom_mutant, tmp_path / "default-branch-image" / "findings.json").ok


def test_unconfirmed_push_image_stays_uncredited_on_a_frequent_lane(
        monkeypatch, tmp_path):
    fixture = tmp_path / "frequent-no-inspect-image"
    shutil.copytree(FIXTURE_ROOT / "default-branch-image", fixture)
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    for job_id, log in responses["logs"].items():
        first_layer = log.index("#9 [linux/amd64")
        responses["logs"][job_id] = (
            "docker buildx build --platform linux/amd64,linux/arm64 .\n"
            + log[first_layer:])
    push_workflow = next(
        row for row in responses["workflows"] if row["path"].endswith("ci.yml"))
    template_run = push_workflow["runs"][0]
    template_jobs = responses["jobs"][str(template_run["id"])]
    for offset in range(5):
        run_id = 1004 + offset
        day = 25 - offset
        run = dict(template_run)
        run.update({
            "id": run_id,
            "head_sha": f"sha-extra-{offset}",
            "created_at": f"2026-01-{day:02d}T00:00:00Z",
            "run_started_at": f"2026-01-{day:02d}T00:00:00Z",
            "updated_at": f"2026-01-{day:02d}T00:05:00Z",
        })
        push_workflow["runs"].append(run)
        jobs = json.loads(json.dumps(template_jobs))
        for job_index, job in enumerate(jobs, start=1):
            job["id"] = run_id * 10 + job_index
            for key in ("started_at", "completed_at"):
                job[key] = job[key].replace("2026-01-28", f"2026-01-{day:02d}")
            for step in job.get("steps") or []:
                for key in ("started_at", "completed_at"):
                    step[key] = step[key].replace(
                        "2026-01-28", f"2026-01-{day:02d}")
        responses["jobs"][str(run_id)] = jobs
    push_workflow["runs_30d"] = len(push_workflow["runs"])
    responses_path.write_text(json.dumps(responses), encoding="utf-8")

    run_root = tmp_path / "frequent-run"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT102" and row.get("lane") == "push")
    leaf = doc["default_branch_push_lane"]["drill"]["leaf"]

    assert leaf["builder_topology_unconfirmed"] is True
    assert leaf["uncredited"] is True
    assert finding["push_lane_projection"]["cause_ceiling_s"] == 0.0
    assert finding["push_lane_projection"]["credited_s"] == 0.0
    assert "docker buildx inspect --bootstrap" in finding["agent_prompt"]
    assert "Do not claim emulation or a saving" in finding["agent_prompt"]
    assert "Build platform-independent output once" not in finding["agent_prompt"]
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_isolates_every_match_without_erasing_a_pr_match(
        monkeypatch, tmp_path):
    source = FIXTURE_ROOT / "default-branch-image"
    fixture = tmp_path / "mixed-lane-image"
    shutil.copytree(source, fixture)
    workflow = fixture / "workflows" / "ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "  build-image:\n    runs-on:",
            "  build-image:\n    name: Publish image\n    runs-on:").replace(
            "on:\n  push:", "on:\n  pull_request:\n  push:"),
        encoding="utf-8")
    with workflow.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n# A second public build site in the same measured job.\n"
            "# The scanner must keep both lane-specific findings isolated.\n")
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "      - uses: docker/build-push-action@v6\n"
            "        with:\n"
            "          platforms: linux/arm64\n",
            "      - uses: docker/build-push-action@v6\n"
            "        with:\n"
            "          platforms: linux/arm64\n"
            "      - uses: docker/build-push-action@v6\n"
            "        with:\n"
            "          platforms: linux/arm64\n"
            "          file: Dockerfile.second\n"),
        encoding="utf-8")
    shutil.copy2(fixture / "Dockerfile", fixture / "Dockerfile.second")
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    for jobs in responses["jobs"].values():
        for job in jobs:
            if job.get("name") == "build-image":
                job["name"] = "Publish image"
    responses_path.write_text(json.dumps(responses), encoding="utf-8")

    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    opt102 = [row for row in doc["findings"] if row.get("pattern") == "OPT102"]
    merge_findings = [row for row in opt102 if row.get("lane") != "push"]
    push_findings = [row for row in opt102 if row.get("lane") == "push"]

    assert len(merge_findings) == 2
    assert len(push_findings) == 2
    assert all(row["wall_clock_p50_s"] == 0.0 for row in push_findings)
    assert all(row.get("non_pr_lane") is not True for row in merge_findings)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_clones_an_unscoped_branch_push_finding(
        monkeypatch, tmp_path):
    fixture = tmp_path / "unscoped-push-image"
    shutil.copytree(FIXTURE_ROOT / "default-branch-image", fixture)
    workflow = fixture / "workflows" / "ci.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "  push:\n    branches: [main]", "  push:"),
        encoding="utf-8")

    run_root = tmp_path / "run-unscoped-push"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    opt102 = [row for row in doc["findings"] if row.get("pattern") == "OPT102"]
    ordinary = [row for row in opt102 if row.get("lane") != "push"]
    push = [row for row in opt102 if row.get("lane") == "push"]

    assert len(ordinary) == 1
    assert ordinary[0]["workflow_push_scopes"] == ["unscoped"]
    assert len(push) == 1
    assert push[0]["id"] != ordinary[0]["id"]
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_credits_a_non_rare_lane_through_the_full_pipeline(
        monkeypatch, tmp_path):
    fixture = tmp_path / "non-rare-default-branch-image"
    shutil.copytree(FIXTURE_ROOT / "default-branch-image", fixture)
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    template_run = responses["workflows"][0]["runs"][0]
    template_jobs = responses["jobs"][str(template_run["id"])]
    template_log = responses["logs"][str(template_jobs[0]["id"])]
    runs = []
    for index in range(12):
        run = dict(template_run, id=3000 + index, head_sha=f"push-{index}")
        jobs = json.loads(json.dumps(template_jobs))
        for offset, job in enumerate(jobs):
            job["id"] = 4000 + index * 10 + offset
        runs.append(run)
        responses["jobs"][str(run["id"])] = jobs
        responses["logs"][str(jobs[0]["id"])] = template_log
    responses["workflows"][0]["runs"] = runs
    responses_path.write_text(json.dumps(responses), encoding="utf-8")

    run_root = tmp_path / "run-non-rare"
    run_root.mkdir()
    doc, _report, checks = run_fixture(
        fixture, monkeypatch, run_root)
    drill = doc["default_branch_push_lane"]["drill"]
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT102" and row.get("lane") == "push")

    assert doc["default_branch_push_lane"]["rare"] is False
    assert drill["catalog_route"] == "OPT102"
    assert drill["finding_id"] == finding["id"]
    assert finding["push_lane_projection"]["credited_s"] > 0
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_keeps_log_only_and_non_opt102_routes_valid(
        monkeypatch, tmp_path):
    source = FIXTURE_ROOT / "default-branch-image"
    fixture = tmp_path / "log-only-image"
    shutil.copytree(source, fixture)
    (fixture / "Dockerfile").write_text(
        "FROM alpine:3.22\nRUN echo prebuilt\n", encoding="utf-8")
    real_parse = bp._parse_log

    def non_opt102_route(text, *args, **kwargs):
        if "linux/arm64" in text:
            return {"fix_key": "eslint-no-cache"}
        return real_parse(text, *args, **kwargs)

    monkeypatch.setattr(bp, "_parse_log", non_opt102_route)
    run_root = tmp_path / "run-log-only"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)
    drill = doc["default_branch_push_lane"]["drill"]

    assert drill["catalog_status"] == "matched"
    assert drill["catalog_route"] == "OPT9"
    assert drill["finding_ids"] == []
    assert "**Catalog route:** OPT9." in report
    assert "Enable `--cache` with a stable `--cache-location`" in drill["agent_prompt"]
    assert "no pattern matched" not in report.lower()
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_attaches_only_the_matching_opt9_tool(
        monkeypatch, tmp_path):
    fixture = tmp_path / "eslint-push"
    shutil.copytree(FIXTURE_ROOT / "default-branch-image", fixture)
    (fixture / "Dockerfile").unlink()
    (fixture / "package.json").write_text("{}\n", encoding="utf-8")
    (fixture / "workflows" / "ci.yml").write_text(
        """name: Check
on:
  push:
    branches: [main]
    paths: ["**"]
concurrency: push-check
jobs:
  build-image:
    runs-on: ubuntu-latest
    steps:
      - run: npx eslint .
      - run: npx tsc --noEmit
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: make verify
""", encoding="utf-8")
    real_parse = bp._parse_log

    def eslint_route(text, *args, **kwargs):
        if "linux/arm64" in text:
            return {"fix_key": "eslint-no-cache"}
        return real_parse(text, *args, **kwargs)

    monkeypatch.setattr(bp, "_parse_log", eslint_route)
    run_root = tmp_path / "run-eslint"
    run_root.mkdir()
    doc, _report, checks = run_fixture(fixture, monkeypatch, run_root)
    drill = doc["default_branch_push_lane"]["drill"]
    attached = [
        row for row in doc["findings"]
        if str(row.get("id") or "") in set(drill["finding_ids"])
    ]

    assert [row.get("opt9_tool") for row in attached] == ["eslint"]
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_push_drill_attaches_dependency_cache_recipe_and_push_finding(
        monkeypatch, tmp_path):
    fixture = tmp_path / "dependency-cache-push"
    shutil.copytree(FIXTURE_ROOT / "default-branch-image", fixture)
    (fixture / "Dockerfile").unlink()
    (fixture / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (fixture / "workflows" / "ci.yml").write_text(
        """name: Build
on: [pull_request, push]
jobs:
  build-image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        if: github.event_name == 'pull_request'
        with:
          path: ~/.cache/pip
          key: pip-${{ hashFiles('requirements.txt') }}
      - run: pip install -r requirements.txt
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: make verify
""", encoding="utf-8")
    responses_path = fixture / "responses.json"
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    cache_log = "Cache not found for input keys: pip-deadbeef\n"
    responses["logs"] = {key: cache_log for key in ("1101", "1201", "1301")}
    responses_path.write_text(json.dumps(responses), encoding="utf-8")

    run_root = tmp_path / "run-dependency-cache"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)
    drill = doc["default_branch_push_lane"]["drill"]
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT96" and row.get("lane") == "push")

    assert drill["catalog_route"] == "OPT96"
    assert drill["finding_id"] == finding["id"]
    assert "Seed the same cache" in drill["agent_prompt"]
    assert "**Catalog route:** OPT96." in report
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


@pytest.mark.parametrize(
    "fixture", sorted(path for path in FIXTURE_ROOT.glob("*") if path.is_dir()),
    ids=lambda path: path.name)
def test_fixture_runs_through_collect_render_and_full_verifier(
        fixture: Path, monkeypatch, tmp_path: Path):
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [
        (check.name, check.detail) for check in failures]

    assert_fixture_facts(fixture, fixture_facts(doc, checks))


def _pr_sample_fixture(tmp_path: Path, *, second_page: bool,
                       third_page: bool = False,
                       fail_first_page: bool = False,
                       fail_second_page: bool = False,
                       zero_first_matches: bool = False,
                       matches_on_third: bool = False,
                       era_boundary: bool = False,
                       wide_short_window: bool = False) -> Path:
    """A full collect/render/verify fixture with a sparse first candidate page."""
    fixture = tmp_path / ("pr-sample-paged" if second_page else "pr-sample-exhausted")
    workflows = fixture / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [pull_request]\njobs:\n  gate:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
        encoding="utf-8")
    if wide_short_window:
        for number in range(1, 9):
            (workflows / f"other-{number}.yml").write_text(
                f"name: Other {number}\non: [pull_request]\njobs:\n  other:\n"
                "    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
                encoding="utf-8")

    def run(i: int) -> dict:
        day = 28 - (i // 24)
        hour = i % 24
        ts = f"2026-01-{day:02d}T{hour:02d}:00:00Z"
        end = f"2026-01-{day:02d}T{hour:02d}:05:00Z"
        return {
            "id": 10_000 + i, "event": "pull_request",
            "head_branch": f"feature-{i}", "head_sha": f"sha-{i}",
            "status": "completed", "conclusion": "success",
            "created_at": ts, "run_started_at": ts, "updated_at": end,
        }

    first = [run(i) for i in range(
        85 if wide_short_window else (100 if second_page else 9))]
    other_pages: list[list[dict]] = []
    if wide_short_window:
        for number in range(1, 9):
            page: list[dict] = []
            for i in range(85):
                row = run(i)
                row["id"] += number * 1_000
                row["head_branch"] = f"other-{number}-{i}"
                row["head_sha"] = f"other-{number}-sha-{i}"
                page.append(row)
            other_pages.append(page)
    other = [row for page in other_pages for row in page]
    second_count = (100 if third_page
                    else (20 if second_page and zero_first_matches
                          else (12 if second_page else 0)))
    second = [run(100 + i) for i in range(second_count)]
    third = [run(200 + i) for i in range(100)] if third_page else []
    qualifying = (set() if zero_first_matches
                  else {f"sha-{i}" for i in range(9)})
    if matches_on_third:
        qualifying.update(f"sha-{200 + i}" for i in range(20))
    elif (second_page and not third_page and not fail_second_page
          and not era_boundary):
        qualifying.update(
            f"sha-{100 + i}" for i in range(20 if zero_first_matches else 12))
    checks = {
        row["head_sha"]: [{
            "name": "gate" if row["head_sha"] in qualifying else "optional",
            "started_at": row["created_at"], "completed_at": row["updated_at"],
        }]
        for row in first + second + third + other
    }
    jobs = {
        str(row["id"]): [{
            "id": row["id"] + 100_000,
            "name": "gate",
            "labels": ["ubuntu-latest"],
            "created_at": row["created_at"],
            "started_at": row["created_at"],
            "completed_at": row["updated_at"],
            "conclusion": "success",
            "steps": [{
                "name": "Run tests",
                "started_at": row["created_at"],
                "completed_at": row["updated_at"],
            }],
        }]
        for row in first + second + third + other
    }
    run_pages = [first]
    if second_page:
        run_pages.append(second)
    if third_page:
        run_pages.append(third)
    recorded = {
        "repo": f"fixture/{fixture.name}",
        "captured_at": "2026-01-31T00:00:00Z",
        "default_branch": "main", "required_checks": ["gate"],
        "workflows": [{
            "id": 210, "path": ".github/workflows/ci.yml", "name": "CI",
            "runs_30d": len(first) + len(second) + len(third),
            "run_pages": run_pages,
            "failed_all_status_pages": (
                ([1] if fail_first_page else [])
                + ([2] if fail_second_page else [])),
        }] + ([{
            "id": 210 + number,
            "path": f".github/workflows/other-{number}.yml",
            "name": f"Other {number}",
            "runs_30d": len(page), "run_pages": [page],
        } for number, page in enumerate(other_pages, 1)]),
        "jobs": jobs, "checks": checks,
    }
    if era_boundary:
        recorded["commits_by_path"] = [{
            "sha": "current-config",
            "commit": {"committer": {"date": "2026-01-23T00:00:00Z"}},
        }]
    (fixture / "responses.json").write_text(
        json.dumps(recorded), encoding="utf-8")
    (fixture / "expected.json").write_text("{}\n", encoding="utf-8")
    return fixture


def test_pr_sample_pages_until_target_through_collect_and_full_verifier(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(tmp_path, second_page=True)
    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 20
    assert cp["sample_stop_reason"] == "target_met"
    assert cp["commit_wall_clock"]["n"] == 20
    assert sum(
        fact.get("partial") is not True
        for fact in cp["commit_wall_clock"]["facts"]) == 20
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "20 of 20 PRs sampled: target met" in report


def test_pr_sample_does_not_treat_fallback_as_target_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=True, zero_first_matches=True)
    run_root = tmp_path / "run-zero-strict-first-page"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 20
    assert cp["sample_stop_reason"] == "target_met"
    assert cp["required_suite_unsatisfiable"] is False
    assert cp["sample_candidate_pages_fetched"] == 2
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "20 of 20 PRs sampled: target met" in report


def test_pr_sample_evaluates_third_page_candidates_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=True, third_page=True,
        zero_first_matches=True, matches_on_third=True)
    run_root = tmp_path / "run-third-page-target"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 20
    assert cp["sample_stop_reason"] == "target_met"
    assert cp["sample_candidate_pages_fetched"] == 3
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "20 of 20 PRs sampled: target met" in report


def test_short_pr_sample_names_exhausted_candidates_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(tmp_path, second_page=False)
    run_root = tmp_path / "run"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 9
    assert cp["sample_stop_reason"] == "candidates_exhausted"
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "9 of 20 PRs sampled: candidates exhausted" in report


def test_pr_sample_three_page_cap_is_pinned_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=True, third_page=True)
    run_root = tmp_path / "run-page-cap"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 9
    assert cp["sample_stop_reason"] == "page_cap"
    assert cp["sample_candidate_pages_fetched"] == 3
    assert cp["sample_candidate_page_cap"] == cr._PR_CANDIDATE_PAGE_CAP == 3
    # Pin the bounded walk's offline API budget: three run-list pages, candidate
    # checks, and jobs only for the nine selected commits.
    assert doc["data_sources"]["gh_query_count"] == 254
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "9 of 20 PRs sampled: page cap" in report


def test_pr_sample_selector_limit_is_not_called_exhaustion(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=False, wide_short_window=True)
    run_root = tmp_path / "run-selector-cap"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert 0 < cp["sampled_pr_count"] < 20
    assert cp["sample_candidate_selection_limited"] is True, {
        key: cp.get(key) for key in (
            "sample_fetched", "sampled_pr_count", "sample_stop_reason",
            "sample_candidate_pages_exhausted")}
    assert cp["sample_stop_reason"] == "page_cap"
    assert cp["sample_candidate_pages_exhausted"] is True
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "of 20 PRs sampled: page cap" in report


def test_pr_sample_page_failure_is_named_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=True, fail_second_page=True)
    run_root = tmp_path / "run-fetch-failure"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 9
    assert cp["sample_stop_reason"] == "fetch_failures"
    assert cp["sample_candidate_page_fetch_failures"] == 1
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "9 of 20 PRs sampled: fetch failures" in report


def test_pr_sample_initial_page_failure_is_named_through_full_pipeline(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=False, fail_first_page=True)
    run_root = tmp_path / "run-initial-fetch-failure"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sample_stop_reason"] == "fetch_failures"
    assert cp["sample_candidate_page_fetch_failures"] == 1
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "PRs sampled: fetch failures" in report


def test_pr_sample_stops_at_era_boundary_discovered_on_later_page(
        monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(
        tmp_path, second_page=True, third_page=True, era_boundary=True)
    run_root = tmp_path / "run-later-era-door"
    run_root.mkdir()
    doc, report, checks = run_fixture(fixture, monkeypatch, run_root)

    cp = doc["pr_critical_path"]
    assert cp["sampled_pr_count"] == 9
    assert cp["sample_stop_reason"] == "era_door"
    assert cp["sample_candidate_era_door"] is True
    assert cp["sample_complete"] is False
    assert [c for c in checks if not c.skipped and not c.ok] == []
    assert "9 of 20 PRs sampled: era door" in report


def test_pr_sample_verifier_rejects_doctored_stop_reason(monkeypatch, tmp_path):
    fixture = _pr_sample_fixture(tmp_path, second_page=False)
    run_root = tmp_path / "run-doctored-stop-reason"
    run_root.mkdir()
    doc, report, _checks = run_fixture(fixture, monkeypatch, run_root)
    findings_path = run_root / "findings.json"
    doc["pr_critical_path"]["sample_stop_reason"] = "target_met"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")

    check = vr.check_pr_sample_stop_reason(report, findings_path)
    assert not check.ok
    assert "expected 'candidates_exhausted'" in check.detail


def test_fixture_set_catches_required_crown_regression(monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "unpinnable-slow-check"
    monkeypatch.setattr(
        cr, "_pole_is_required_reachable", lambda *args, **kwargs: True)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    facts = fixture_facts(doc, checks)
    assert facts["headline"]["check"] != "unit"


def test_fixture_set_catches_leaf_precedence_regression(monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "serial-chain"
    real_parse = bp._parse_log

    def reverted_parse(text: str, *args, **kwargs):
        if "Cache not found for input keys:" in text and "./run-test.sh" in text:
            text = "\n".join(
                line for line in text.splitlines()
                if "Cache not found for input keys:" not in line)
        return real_parse(text, *args, **kwargs)

    monkeypatch.setattr(bp, "_parse_log", reverted_parse)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    with pytest.raises(
            AssertionError, match=r"serial-chain: leaves\[0\] changed"):
        assert_fixture_facts(fixture, fixture_facts(doc, checks))


def test_fixture_set_catches_unconfirmed_gate_credit_regression(
        monkeypatch, tmp_path):
    fixture = FIXTURE_ROOT / "unpinnable-slow-check"
    monkeypatch.setattr(cr, "_pole_is_required_reachable", lambda *args, **kwargs: True)
    doc, _report, checks = run_fixture(fixture, monkeypatch, tmp_path)
    with pytest.raises(
            AssertionError,
            match=r"unpinnable-slow-check: credited_findings\.length changed"):
        assert_fixture_facts(fixture, fixture_facts(doc, checks))
