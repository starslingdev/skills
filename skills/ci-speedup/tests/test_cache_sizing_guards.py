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
         "already fills the default budget"),
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
