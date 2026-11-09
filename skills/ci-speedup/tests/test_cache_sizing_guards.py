from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import blocking_path as bp
import collect_runs as cr


def _fakes():
    path = Path(__file__).with_name("test_disclosure_reaches_the_artifact.py")
    spec = importlib.util.spec_from_file_location("cache_guard_collect_fakes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _verify(doc: dict, report: str, tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = Path(__file__).with_name("verify_report.py")
    spec = importlib.util.spec_from_file_location("cache_guard_verify", path)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    assert spec.loader is not None
    spec.loader.exec_module(vr)
    findings = tmp_path / "findings.json"
    rendered = tmp_path / "report.md"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    rendered.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    rendered.with_name(rendered.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failed = [c for c in vr.run_checks(report, rendered, findings, skill_repo=None)
              if not c.skipped and not c.ok]
    assert failed == [], "\n".join(f"{c.name}: {c.detail}" for c in failed)


def test_collect_applies_budget_invalidation_and_transfer_guards(monkeypatch, tmp_path):
    fakes = _fakes()

    class Client(fakes._TwoWorkflowClient):
        usage_bytes = 0
        changed_lockfiles = 0

        def __init__(self):
            super().__init__()
            self._shas = [f"s{i}" for i in range(1, 21)]

        def _runs(self, wf_id):
            rows = super()._runs(wf_id)
            for number, row in enumerate(rows, 1):
                row["pull_requests"] = [{"number": number}]
                row["head_branch"] = f"feature-{number}"
            return rows

        def json(self, endpoint, allow_missing=False):
            if endpoint == "repos/o/r/actions/cache/usage":
                self.queries += 1
                return {"active_caches_size_in_bytes": self.usage_bytes}
            if "/pulls/" in endpoint and "/files?" in endpoint:
                self.queries += 1
                number = int(endpoint.split("/pulls/")[1].split("/")[0])
                return [{"filename": "lockfile" if number <= self.changed_lockfiles
                         else f"src/file-{number}.py"}]
            return super().json(endpoint, allow_missing)

    cases = [
        (4 * 1024**3, 7.0, 1, 0, 0.75, 1, 1,
         "would exceed the repository's cache budget"),
        (int(10.4 * 1024**3), 0.5, 1, 0, 0.75, 1, 1,
         "at least 90% of the default 10 GB"),
        (200 * 1024**3, 1.0, 1, 0, 0.75, 1, 1,
         "limit unknown: not measured"),
        (0, 5.1, 2, 0, 0.75, 1, 1,
         "would exceed the repository's cache budget"),
        (0, 1.0, 1, 15, 0.25, 1, 1,
         "invalidated on 75% of sampled PRs"),
        (0, 1.0, 1, 0, 0.50, 1, 230,
         "restore and save cost exceeds the expected saving"),
        (0, 1.0, 1, 0, 0.75, 1, 1, "all three cache sizing checks passed"),
    ]

    for index, (usage, size_gb, copies, changed, hit_rate, restore_s, save_s,
                phrase) in enumerate(cases):
        Client.usage_bytes = usage
        Client.changed_lockfiles = changed
        monkeypatch.setattr(cr, "GhClient", Client)

        def persist(client, repo, poles, *args, **kwargs):
            for pole in poles:
                if pole.get("job") == "Test suite":
                    values = [{"fork": False, "cache_state": {
                        "miss_pct": (1 - hit_rate) * 100,
                        "cache_size_gb": size_gb,
                        "restore_s": restore_s,
                        "save_s": save_s,
                    }} for _ in range(8)]
                    pole["cache_dist"] = {
                        "fix_key": "dependency-cache",
                            "confirmed": True,
                            "recommendation": "seed the default-branch cache",
                        "pr": {"values": values},
                        "tail": {"prevalence_max": 0.0},
                        "floor_pct": 50.0,
                        "tail_min_frac": cr._CACHE_TAIL_MIN_FRAC,
                        "verdict": cr._cache_verdict(
                            values, {"prevalence_max": 0.0}, floor_pct=50.0,
                            tail_min_frac=cr._CACHE_TAIL_MIN_FRAC,
                            cold_pct=cr._CACHE_COLD_MISS_PCT),
                        "sizing": {"credited": True, "saving_s": 120.0},
                    }
                    return [{"job": "Test suite", "check": pole.get("check"),
                             "job_id": 101, "file": "test.log",
                             "steps_file": "test.steps.json", "mag_file": "test.mag.json",
                             "html_url": "https://github.com/o/r/actions/runs/101/job/101",
                             "workflow_file": fakes._GATE, "duration_s": 1400,
                             "selected": "nearest-p50", "sample": []}]
            return []

        monkeypatch.setattr(cr, "_persist_pole_logs", persist)
        finding = {
            "id": "f9", "pattern": "OPT96",
            "title": "Dependency cache is not seeded on the default branch",
            "workflow_file": fakes._GATE,
            "affected_jobs": ["Test suite"],
            "evidence": "the dependency install has no persisted cache",
            "cache_key_globs": ["lockfile"],
            "cache_copies": copies,
        }
        head = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        doc = cr.collect({"repo": "o/r", "skill_commit_sha": head,
                          "findings": [finding]}, "o/r",
                         max_runs=20, shallow_runs=20, with_logs=True,
                         data_dir=tmp_path / f"bundle-{index}")
        [result] = [f for f in doc["findings"] if f.get("id") == "f9"]
        assert "cache_sizing_guard" in result, result
        import summary
        key = summary._render_keys(doc["data_bundle"]["logs"])[0]
        timeline = {"job_dur_s": 1400.0,
                    "run_url": "https://github.com/o/r/actions/runs/101",
                    "steps": [{"name": "Install dependencies", "dur_s": 120.0,
                               "start_s": 10.0},
                              {"name": "Test", "dur_s": 1200.0, "start_s": 140.0}]}
        magnitude = {"label": "the `Test` step (wall)", "unit": "s", "kind": "step-wall",
                     "this_run": 1200.0, "values": [
                         {"run_url": "https://github.com/o/r/actions/runs/101",
                          "value": 1200.0, "drilled": True},
                         {"run_url": "https://github.com/o/r/actions/runs/102",
                          "value": 1180.0}]}
        analysis = {"cause": "The test step accounts for most of this job's measured wall time.",
                    "breakdown": [["Test", "20m"], ["Install dependencies", "2m"]],
                    "evidence": ["the representative step timeline measures both steps"],
                    "prompt": "REPO: o/r\nTHE GATE: Test suite in test.yml.\nMeasure the "
                              "test runner's internal phases before changing it."}
        report = bp.render(doc, {key: "Cache restored from key: deps"}, {},
                           {key: "https://github.com/o/r/actions/runs/101"},
                           "2026-11-07T00:00:00Z", steps={key: timeline},
                           mags={key: magnitude}, analyses={key: analysis})
        assert phrase in report
        if phrase == "all three cache sizing checks passed":
            assert result["cache_sizing_guard"]["status"] == "passed"
            assert result["wall_clock_p50_s"] is not None
        else:
            assert result["cache_sizing_guard"]["status"] != "passed"
            assert result["wall_clock_p50_s"] is None
            assert result["runner_min_saving"] is None
        _verify(doc, report, tmp_path / f"verify-{index}")


def test_verifier_rejects_cache_credit_after_a_failed_guard(tmp_path):
    path = Path(__file__).with_name("verify_report.py")
    spec = importlib.util.spec_from_file_location("cache_guard_negative_verify", path)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    assert spec.loader is not None
    spec.loader.exec_module(vr)
    doc = {"findings": [{
        "id": "f1", "wall_clock_p50_s": 30.0, "runner_min_saving": 4.0,
        "cache_sizing_guard": {"status": "failed", "checks": {
            "budget": {"status": "failed", "detail": "the proposed copies would exceed the repository's cache budget",
                       "usage_bytes": 4 * 1024**3, "proposed_bytes": 7 * 1024**3},
            "invalidation": {"status": "passed", "detail": "cache key invalidated on 0% of sampled PRs",
                             "sampled_prs": 20, "invalidation_rate": 0.0},
            "economics": {"status": "passed", "detail": "expected cache saving is positive",
                          "hit_rate": .75, "saving_s": 120, "restore_s": 1,
                          "save_s": 1, "expected_s": 88.75},
        }},
    }]}
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    check = vr.check_cache_sizing_guards(
        "the proposed copies would exceed the repository's cache budget", findings)
    assert not check.ok
    assert "remains credited" in check.detail


def test_verifier_requires_guard_and_raw_terms_for_credited_cache(tmp_path):
    path = Path(__file__).with_name("verify_report.py")
    spec = importlib.util.spec_from_file_location("cache_guard_terms_verify", path)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    assert spec.loader is not None
    spec.loader.exec_module(vr)

    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"findings": [{
        "id": "f1", "pattern": "OPT6", "wall_clock_p50_s": 30.0,
    }]}), encoding="utf-8")
    missing = vr.check_cache_sizing_guards("", findings)
    assert not missing.ok
    assert "lack a sizing guard" in missing.detail

    findings.write_text(json.dumps({"findings": [{
        "id": "f1", "pattern": "OPT6", "wall_clock_p50_s": 30.0,
        "cache_sizing_guard": {"status": "passed", "checks": {
            "budget": {"status": "passed"},
            "invalidation": {"status": "passed"},
            "economics": {"status": "passed"},
        }},
    }]}), encoding="utf-8")
    empty = vr.check_cache_sizing_guards("all three cache sizing checks passed", findings)
    assert not empty.ok
    assert "raw terms" in empty.detail or "stored bytes" in empty.detail


def test_cache_transfer_parser_reads_actions_cache_and_buildkit_timings():
    actions = bp._cache_state_of_log(
        "Cache restored from key: deps\nCache Size: 1.5 GB\n"
        "Cache restored in 4.5 seconds\nCache saved in 7 seconds", "dependency-cache")
    assert actions["cache_size_gb"] == 1.5
    assert actions["restore_s"] == 4.5
    assert actions["save_s"] == 7

    buildkit = bp._cache_state_of_log(
        "#1 [stage 1/1] RUN make\n#2 importing cache manifest from gha\n#2 DONE 3.5s\n"
        "#3 exporting cache to GitHub Actions Cache\n#3 DONE 9.0s", "buildx-no-cache")
    assert buildkit["restore_s"] == 3.5
    assert buildkit["save_s"] == 9.0

    multiple = bp._cache_state_of_log(
        "Cache restored from key: one\nCache Size: 1 GB\nCache restored in 2 seconds\n"
        "Cache Size: 3 GB\nCache restored in 4 seconds\nCache saved in 5 seconds",
        "dependency-cache")
    assert multiple["cache_size_gb"] == 4.0
    assert multiple["restore_s"] == 6.0


def test_rolling_key_printed_miss_rate_can_fail_invalidation_guard():
    values = [{"fork": False, "cache_state": {
        "miss_pct": 75.0, "cache_size_gb": 1.0, "restore_s": 1.0, "save_s": 1.0,
    }} for _ in range(4)]
    finding = {
        "id": "f1", "pattern": "OPT6", "cache_key_globs": ["github.sha"],
        "cache_copies": 1, "wall_clock_p50_s": 120.0,
        "cache_dist": {"pr": {"values": values}, "sizing": {"saving_s": 120.0}},
    }

    cr._apply_cache_sizing_guard(
        finding, usage_bytes=0, changed={}, sampled_runs=[], wf_docs={})

    invalidation = finding["cache_sizing_guard"]["checks"]["invalidation"]
    assert invalidation["status"] == "failed"
    assert invalidation["invalidation_rate"] == 0.75
    assert finding["cache_sizing_guard"]["status"] == "failed"
    assert finding["wall_clock_p50_s"] is None


def test_matrix_include_leg_counts_as_a_separate_cache_copy():
    finding = {"workflow_file": "w.yml", "affected_jobs": ["build"]}
    workflows = {"w.yml": {"jobs": {"build": {"strategy": {"matrix": {
        "os": ["ubuntu", "windows"],
        "include": [{"os": "macos", "experimental": True}],
    }}}}}}
    assert cr._cache_copy_count(finding, [{"head_branch": "feature"}], workflows) == 3

    include_only = {"w.yml": {"jobs": {"build": {"strategy": {"matrix": {
        "include": [{"os": "ubuntu"}, {"os": "windows"}, {"os": "macos"}],
    }}}}}}
    assert cr._cache_copy_count(finding, [{"head_branch": "feature"}], include_only) == 3


def test_hashfiles_arguments_and_character_classes_measure_invalidation():
    finding = {
        "workflow_file": "w.yml", "affected_jobs": ["build"],
        "cache_dist": {"pr": {"values": []}},
    }
    workflows = {"w.yml": {"jobs": {"build": {"steps": [{
        "uses": "actions/cache@v4",
        "with": {"key": "src-${{ hashFiles('**/*.ts', '**/*.[jt]sx') }}"},
    }]}}}}
    _text, globs = cr._cache_key_text_and_globs(finding, workflows)
    assert globs == ["**/*.ts", "**/*.[jt]sx"]
    assert cr._opt32_glob_matches("packages/app/main.ts", globs[0])
    assert cr._opt32_glob_matches("packages/app/main.jsx", globs[1])


def test_cache_evidence_joins_named_job_without_touching_unrelated_finding():
    dist = {"fix_key": "dependency-cache", "pr": {"values": []}}
    poles = [{"workflow_file": "w.yml", "job": "Test suite", "cache_dist": dist}]
    workflows = {"w.yml": {"jobs": {"test": {"name": "Test suite"}}}}

    cache_finding = {
        "pattern": "OPT4", "workflow_file": "w.yml", "affected_jobs": ["test"]}
    unrelated = {
        "pattern": "OPT1", "workflow_file": "w.yml", "affected_jobs": ["test"]}

    assert cr._cache_dist_for_finding(cache_finding, poles, workflows) is dist
    assert cr._cache_dist_for_finding(unrelated, poles, workflows) is None


def test_jobs_api_step_times_fill_standard_cache_log_timing_gap():
    job = {"steps": [
        {"name": "Cache dependencies", "started_at": "2026-01-01T00:00:00Z",
         "completed_at": "2026-01-01T00:00:04Z"},
        {"name": "Post Cache dependencies", "started_at": "2026-01-01T00:01:00Z",
         "completed_at": "2026-01-01T00:01:07Z"},
    ]}
    assert cr._cache_step_transfer_times(job) == (4.0, 7.0)


def test_dependency_cache_economics_use_unweighted_avoided_work():
    values = [{"fork": False, "cache_state": {
        "miss_pct": 75.0, "cache_size_gb": 1.0, "restore_s": 20.0, "save_s": 1.0,
    }} for _ in range(4)]
    finding = {
        "id": "f1", "pattern": "OPT96", "cache_key_globs": ["lockfile"],
        "cache_copies": 1, "wall_clock_p50_s": 75.0,
        "cache_dist": {"pr": {"values": values}, "sizing": {
            "saving_s": 75.0, "miss_time_s": 100.0, "hit_time_s": 0.0}},
    }
    sampled = [{"pull_requests": [{"number": n}]} for n in range(1, 5)]
    changed = {n: [f"src/{n}.py"] for n in range(1, 5)}

    cr._apply_cache_sizing_guard(
        finding, usage_bytes=0, changed=changed,
        sampled_runs=sampled, wf_docs={})

    economics = finding["cache_sizing_guard"]["checks"]["economics"]
    assert economics["status"] == "passed"
    assert economics["saving_s"] == 100.0
    assert economics["expected_s"] == 4.25
    assert finding["cache_sizing_guard"]["status"] == "passed"


def test_cache_economics_excludes_no_change_reruns() -> None:
    def entry(sha: str, miss: float) -> dict:
        return {"head_sha": sha, "fork": False, "cache_state": {
            "miss_pct": miss, "cache_size_gb": 0.1,
            "restore_s": 2.0, "save_s": 2.0,
        }}

    finding = {
        "id": "f1", "pattern": "OPT96", "cache_key_globs": ["lockfile"],
        "cache_copies": 1, "wall_clock_p50_s": 100.0,
        "cache_dist": {"pr": {"values": [
            entry("same-head", 100.0), entry("same-head", 0.0),
            entry("edited-head", 0.0),
        ]}, "sizing": {"saving_s": 100.0}},
    }
    sampled = [
        {"head_sha": "same-head", "pull_requests": [{"number": 1}]},
        {"head_sha": "same-head", "pull_requests": [{"number": 1}]},
        {"head_sha": "edited-head", "pull_requests": [{"number": 2}]},
    ]

    cr._apply_cache_sizing_guard(
        finding, usage_bytes=0, changed={1: [], 2: ["src/app.py"]},
        sampled_runs=sampled, wf_docs={},
    )

    economics = finding["cache_sizing_guard"]["checks"]["economics"]
    assert economics["sampled_real_edits"] == 1
    assert economics["hit_rate"] == 1.0


def test_cache_economics_keeps_original_run_when_same_head_is_sampled_out_of_order() -> None:
    def entry(run_id: int, miss: float) -> dict:
        return {
            "run_url": f"https://github.com/o/r/actions/runs/{run_id}",
            "head_sha": "edited-head", "fork": False,
            "cache_state": {"miss_pct": miss, "cache_size_gb": 0.1,
                            "restore_s": 2.0, "save_s": 2.0},
        }

    finding = {
        "id": "f1", "pattern": "OPT96", "cache_key_globs": ["lockfile"],
        "cache_copies": 1, "wall_clock_p50_s": 100.0,
        # The representative warm rerun arrives before the original cold run.
        "cache_dist": {"pr": {"values": [entry(102, 0.0), entry(101, 100.0)]},
                       "sizing": {"saving_s": 100.0}},
    }
    sampled = [{"head_sha": "edited-head", "pull_requests": [{"number": 1}]}]

    cr._apply_cache_sizing_guard(
        finding, usage_bytes=0, changed={1: ["src/app.py"]},
        sampled_runs=sampled, wf_docs={},
    )

    economics = finding["cache_sizing_guard"]["checks"]["economics"]
    assert economics["hit_rate"] == 0.0


def test_unused_cache_path_counts_transfers_as_pure_cost() -> None:
    finding = {
        "id": "f1", "pattern": "OPT61", "cache_key_globs": ["lockfile"],
        "cache_copies": 1, "wall_clock_p50_s": 100.0,
        "cache_path_consumed": False,
        "cache_dist": {"pr": {"values": [{"fork": False, "cache_state": {
            "miss_pct": 0.0, "cache_size_gb": 0.1,
            "restore_s": 4.0, "save_s": 3.0,
        }}]}, "sizing": {"saving_s": 100.0}},
    }

    cr._apply_cache_sizing_guard(
        finding, usage_bytes=0, changed={}, sampled_runs=[], wf_docs={},
    )

    economics = finding["cache_sizing_guard"]["checks"]["economics"]
    assert economics["status"] == "failed"
    assert economics["expected_s"] == -4.0
    assert "never reads" in economics["detail"]


def test_verifier_rederives_unused_cache_path_as_pure_cost(tmp_path) -> None:
    path = Path(__file__).with_name("verify_report.py")
    spec = importlib.util.spec_from_file_location("cache_guard_unused_verify", path)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    assert spec.loader is not None
    spec.loader.exec_module(vr)
    doc = {"findings": [{
        "id": "f1", "pattern": "OPT61", "cache_path_consumed": False,
        "wall_clock_p50_s": None, "runner_min_saving": None,
        "cache_sizing_guard": {"status": "failed", "checks": {
            "budget": {"status": "passed", "detail": "fits",
                       "usage_bytes": 0, "proposed_bytes": 1024},
            "invalidation": {"status": "passed", "detail": "stable",
                             "invalidation_rate": 0.0, "sampled_prs": 1},
            "economics": {"status": "failed", "detail": "the job never reads the restored cache path",
                          "hit_rate": 1.0, "saving_s": 100.0,
                          "restore_s": 4.0, "save_s": 3.0, "expected_s": -4.0},
        }},
    }]}
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    check = vr.check_cache_sizing_guards(
        "the job never reads the restored cache path", findings)
    assert check.ok, check.detail


def test_renderer_does_not_label_zero_saving_static_lint_credited() -> None:
    finding = {
        "title": "Static cache lint", "static_cache_lint": True,
        "wall_clock_p50_s": None, "runner_min_saving": None,
        "cache_sizing_guard": {"status": "passed", "checks": {
            "budget": {"status": "passed"}, "invalidation": {"status": "passed"},
            "economics": {"status": "passed"},
        }},
    }
    rendered = "\n".join(bp._cache_sizing_guard_block({"findings": [finding]}))
    assert "uncredited" in rendered
    assert " - credited:" not in rendered


def test_ninety_percent_default_budget_names_the_admin_limit_uncertainty() -> None:
    finding = {
        "id": "f1", "pattern": "OPT61", "cache_key_globs": ["lockfile"],
        "cache_copies": 1, "wall_clock_p50_s": 10.0,
        "cache_dist": {"pr": {"values": []}},
    }
    cr._apply_cache_sizing_guard(
        finding, usage_bytes=9 * 1024 ** 3, changed={}, sampled_runs=[], wf_docs={},
    )
    detail = finding["cache_sizing_guard"]["checks"]["budget"]["detail"]
    assert "90% of the default 10 GB" in detail
    assert "owner-raised limit" in detail
