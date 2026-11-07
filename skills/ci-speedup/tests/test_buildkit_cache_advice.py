from __future__ import annotations

import sys
import base64
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "buildkit_advice_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)

_WORKFLOW = ".github/workflows/image.yml"


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


class _Client:
    gave_up = False
    cached_compile = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{
                "id": 1, "path": _WORKFLOW, "name": "image", "state": "active",
            }]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 2}
            return {"workflow_runs": [
                {
                    "id": run_id,
                    "event": "push",
                    "head_branch": "release-test",
                    "head_sha": f"sha-{run_id}",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": _iso(0),
                    "run_started_at": _iso(0),
                    "updated_at": _iso(600),
                }
                for run_id in (101, 102)
            ]}
        match = re.match(r"repos/o/r/actions/runs/(101|102)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            return {"jobs": [{
                "id": run_id * 10 + 1,
                "run_id": run_id,
                "name": "image",
                "conclusion": "success",
                "labels": ["ubuntu-latest"],
                "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/1",
                "started_at": _iso(0),
                "completed_at": _iso(600),
                "steps": [{
                    "name": "Build image", "conclusion": "success",
                    "started_at": _iso(0), "completed_at": _iso(600),
                }],
            }]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [{
                "name": "image", "started_at": _iso(0), "completed_at": _iso(600),
            }]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": []}
        if endpoint == f"repos/o/r/contents/{_WORKFLOW}":
            workflow = """\
on:
  push:
    tags: ['v*']
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          cache-from: type=gha
          cache-to: type=gha,mode=max
"""
            return {"content": base64.b64encode(workflow.encode()).decode()}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return "\n".join([
            f"{_iso(10)} #10 [build 2/3] RUN cargo build --release",
            f"{_iso(310)} " + ("#10 CACHED" if self.cached_compile else "#10 DONE 300.0s"),
            f"{_iso(311)} #20 exporting to GitHub Actions Cache",
            f"{_iso(531)} #20 DONE 220.0s",
        ])


class _CachedClient(_Client):
    cached_compile = True


class _NoCacheClient(_Client):
    def text(self, endpoint: str, allow_missing: bool = False):
        return (f"{_iso(0)} docker buildx build --no-cache --cache-to type=gha .\n" +
                super().text(endpoint, allow_missing))


class _UnrelatedCachedCompileClient(_Client):
    def text(self, endpoint: str, allow_missing: bool = False):
        if endpoint.endswith("/1011/logs"):
            return "\n".join([
                f"{_iso(10)} #9 [build 1/3] RUN cargo build --bin helper",
                f"{_iso(11)} #9 CACHED",
                f"{_iso(12)} #10 [build 2/3] RUN cargo build --release",
                f"{_iso(312)} #10 DONE 300.0s",
                f"{_iso(313)} #20 exporting to GitHub Actions Cache",
                f"{_iso(533)} #20 DONE 220.0s",
            ])
        return super().text(endpoint, allow_missing)


def _multi_platform_log(*, no_cache: bool = False) -> str:
    flag = " --no-cache" if no_cache else ""
    return "\n".join([
        f"docker buildx build{flag} --platform linux/amd64,linux/arm64 .",
        "#2 [linux/amd64 1/3] FROM docker.io/library/golang:1.23",
        "#2 DONE 3.0s",
        "#10 [linux/amd64 2/3] RUN go build -o /out/server ./cmd/server",
        "#10 DONE 69.0s",
        "#12 [linux/arm64 1/3] FROM docker.io/library/golang:1.23",
        "#12 DONE 3.0s",
        "#15 [linux/arm64 2/3] RUN go build -o /out/server ./cmd/server",
        "#15 DONE 573.0s",
    ])


def test_emulated_compile_precedes_missing_layer_cache_advice() -> None:
    leaf = bp._parse_log(_multi_platform_log())
    assert leaf is not None
    assert leaf["fix_key"] == "emulated-image-compile"


def test_explicit_no_cache_still_routes_to_emulated_compile_without_cache_advice() -> None:
    leaf = bp._parse_log(_multi_platform_log(no_cache=True))
    assert leaf is not None
    assert leaf["fix_key"] == "emulated-image-compile"
    assert "--no-cache" in leaf.get("config_fact", "")
    assert "cache-to" not in leaf.get("config_fact", "").lower()


def test_single_platform_cold_build_keeps_missing_cache_leaf() -> None:
    log = "\n".join([
        "docker buildx build --platform linux/amd64 .",
        "#2 [linux/amd64 1/3] FROM docker.io/library/golang:1.23",
        "#2 DONE 3.0s",
        "#10 [linux/amd64 2/3] RUN go build -o /out/server ./cmd/server",
        "#10 DONE 120.0s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None
    assert leaf["fix_key"] == "buildx-no-cache"


def _prompt(log: str) -> str:
    leaf = bp._parse_log(log)
    assert leaf is not None
    return bp._build_agent_prompt(
        leaf,
        {
            "workflow_file": ".github/workflows/image.yml",
            "check": "image",
            "job": "image",
            "p50_s": 900.0,
        },
        [], None, "o/r", None, 2, 2,
    )


def test_whole_context_copy_before_compile_blocks_cache_export_advice() -> None:
    prompt = _prompt("\n".join([
        "docker buildx build --platform linux/amd64 .",
        "#3 [build 1/3] COPY . .",
        "#3 DONE 5.0s",
        "#4 [build 2/3] RUN cargo build --release",
        "#4 DONE 300.0s",
    ]))
    assert "--cache-to" not in prompt
    assert "copy dependency manifests first" in prompt.lower()
    assert "--mount=type=cache" in prompt


def test_manifest_first_build_keeps_budgeted_cache_advice() -> None:
    prompt = _prompt("\n".join([
        "docker buildx build --platform linux/amd64 .",
        "#3 [build 1/3] COPY Cargo.toml Cargo.lock ./",
        "#3 DONE 5.0s",
        "#4 [build 2/3] RUN cargo build --release",
        "#4 DONE 300.0s",
    ]))
    assert "--cache-to" in prompt
    assert "type=gha" in prompt
    assert "mode=min" in prompt
    assert "mode=max" in prompt
    assert "measure" in prompt.lower()


def test_whole_context_copy_in_another_stage_does_not_replace_cache_advice() -> None:
    prompt = _prompt("\n".join([
        "docker buildx build --platform linux/amd64 .",
        "#2 [assets 1/1] COPY . .",
        "#2 DONE 5.0s",
        "#3 [build 1/3] COPY Cargo.toml Cargo.lock ./",
        "#3 DONE 5.0s",
        "#4 [build 2/3] RUN cargo build --release",
        "#4 DONE 300.0s",
    ]))
    assert "--cache-to" in prompt


def test_opt4_contract_does_not_recommend_mode_max_unconditionally() -> None:
    pattern = (Path(__file__).resolve().parents[1] / "references" / "patterns" /
               "opt4-docker-layer-cache-missing.md").read_text(encoding="utf-8")
    assert "mode=min" in pattern
    assert "`mode=max` only" in pattern.lower()
    assert "no later tag can restore this" in pattern


def _collect(monkeypatch, tmp_path: Path, client_type: type[_Client]) -> tuple[dict, str]:
    monkeypatch.setattr(cr, "GhClient", client_type)
    doc = cr.collect({
        "repo": "o/r",
        "skill_commit_sha": "installed:unversioned",
        "workflow_job_graph": {_WORKFLOW: {"image": {"name": "image", "needs": []}}},
        "findings": [],
    }, "o/r", max_runs=2, shallow_runs=2)
    report = bp.render(doc, captured_at="2026-11-07T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    return doc, report


def test_collect_finds_repeated_expensive_buildkit_export_and_discloses_tag_scope(
        monkeypatch, tmp_path: Path) -> None:
    doc, report = _collect(monkeypatch, tmp_path, _Client)
    finding = next(f for f in doc["findings"]
                   if f.get("cache_net_negative", {}).get("kind") ==
                   "opt79_buildkit_export")
    assert finding["cache_net_negative"]["export_p50_s"] == 220.0
    assert len(finding["cache_net_negative"]["runs"]) == 2
    assert "no later tag can restore this" in report


def test_collect_stays_silent_when_the_compile_layer_hits_cache(
        monkeypatch, tmp_path: Path) -> None:
    doc, _report = _collect(monkeypatch, tmp_path, _CachedClient)
    assert not any(f.get("cache_net_negative", {}).get("kind") ==
                   "opt79_buildkit_export" for f in doc["findings"])


def test_unrelated_cached_compile_does_not_hide_the_cold_slowest_layer(
        monkeypatch, tmp_path: Path) -> None:
    doc, _report = _collect(monkeypatch, tmp_path, _UnrelatedCachedCompileClient)
    findings = [f for f in doc["findings"]
                if f.get("cache_net_negative", {}).get("kind") ==
                "opt79_buildkit_export"]
    assert len(findings) == 1
    assert findings[0]["sizing_basis"] == "measured"


def test_collect_stays_silent_for_an_explicit_no_cache_build(
        monkeypatch, tmp_path: Path) -> None:
    doc, _report = _collect(monkeypatch, tmp_path, _NoCacheClient)
    assert not any(f.get("cache_net_negative", {}).get("kind") ==
                   "opt79_buildkit_export" for f in doc["findings"])


def test_buildkit_candidates_share_the_existing_per_workflow_probe_limit() -> None:
    wf_doc = {"jobs": {
        name: {"name": name, "steps": [{
            "uses": "docker/build-push-action@v6",
            "with": {"cache-to": "type=gha"},
        }]}
        for name in ("small", "medium", "large")
    }}
    jobs = [{
        "id": index, "name": name, "conclusion": "success",
        "started_at": _iso(0), "completed_at": _iso(duration),
    } for index, (name, duration) in enumerate(
        (("small", 100), ("medium", 200), ("large", 300)), 1)]
    plan = cr._opt79_log_plan("wf.yml", [jobs], {}, wf_doc)
    selected = {job["name"] for _p50, _wf, job in plan}
    assert selected == {"medium", "large"}


def test_verifier_rederives_buildkit_monthly_sizing(
        monkeypatch, tmp_path: Path) -> None:
    doc, report = _collect(monkeypatch, tmp_path, _Client)
    finding = next(f for f in doc["findings"]
                   if f.get("cache_net_negative", {}).get("kind") ==
                   "opt79_buildkit_export")
    report_path = tmp_path / "report.md"
    findings_path = tmp_path / "findings.json"
    for key, value in (("effective_monthly_runs", 999.0),
                       ("sampled_runs", 999), ("monthly_volume", 999)):
        mutant = json.loads(json.dumps(doc))
        stamped = next(f for f in mutant["findings"] if f["id"] == finding["id"])
        stamped["cache_net_negative"][key] = value
        findings_path.write_text(json.dumps(mutant), encoding="utf-8")
        failures = [check for check in vr.run_checks(
            report, report_path, findings_path, skill_repo=None)
            if not check.skipped and not check.ok]
        assert any("OPT79 findings re-derive" in check.name for check in failures)
    mutant = json.loads(json.dumps(doc))
    stamped = next(f for f in mutant["findings"] if f["id"] == finding["id"])
    stamped["runner_min_saving"] = 999.0
    findings_path.write_text(json.dumps(mutant), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert any("OPT79 findings re-derive" in check.name for check in failures)
