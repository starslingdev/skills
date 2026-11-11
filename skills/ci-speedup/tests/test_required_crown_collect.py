from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_VR_SPEC = importlib.util.spec_from_file_location(
    "required_crown_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_CI = ".github/workflows/ci.yml"
_MYSTERY = ".github/workflows/mystery.yml"


def _iso(seconds: int) -> str:
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _job(name: str, seconds: int, run_id: int) -> dict:
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(0),
        "completed_at": _iso(seconds),
        "steps": [{"name": "work", "started_at": _iso(0),
                   "completed_at": _iso(seconds)}],
    }


class _RequiredCrownClient:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.shas = [f"s{i}" for i in range(1, 9)]

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)

    def available(self) -> bool:
        return True

    def _runs(self, workflow_id: int) -> list[dict]:
        wall = 310 if workflow_id == 1 else 510
        base = 100 if workflow_id == 1 else 200
        return [{
            "id": base + i,
            "event": "pull_request",
            "head_sha": sha,
            "status": "completed",
            "conclusion": "success",
            "created_at": _iso(0),
            "run_started_at": _iso(0),
            "updated_at": _iso(wall),
        } for i, sha in enumerate(self.shas)]

    def _jobs(self, run_id: int) -> list[dict]:
        if 100 <= run_id < 200:
            return [_job("unit", 300, run_id), _job("merge-gate", 5, run_id)]
        if 200 <= run_id < 300:
            return [_job("mystery matrix", 500, run_id)]
        return []

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _CI, "name": "ci"},
                {"id": 2, "path": _MYSTERY, "name": "mystery"},
            ]}
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(2)):
                return {"total_count": 30}
            return {"workflow_runs": self._runs(int(match.group(1)))}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"jobs": self._jobs(int(match.group(1)))}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [
                {"name": "unit", "started_at": _iso(0), "completed_at": _iso(300)},
                {"name": "merge-gate", "started_at": _iso(300),
                 "completed_at": _iso(305)},
                {"name": "mystery matrix", "started_at": _iso(0),
                 "completed_at": _iso(500)},
            ]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["merge-gate"]}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _RequiredExternalStatusClient(_RequiredCrownClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            result = super().json(endpoint, allow_missing)
            result["check_runs"].append({
                "name": "external required status",
                "started_at": _iso(0),
                "completed_at": _iso(400),
            })
            return result
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            self.queries += 1
            return {"contexts": ["merge-gate", "external required status"]}
        return super().json(endpoint, allow_missing)


class _AllChecksRequiredClient(_RequiredCrownClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            self.queries += 1
            return {"contexts": ["merge-gate", "mystery matrix"]}
        return super().json(endpoint, allow_missing)


_DOC = {
    "repo": "o/r",
    "workflow_job_graph": {
        _CI: {
            "merge-gate": {"name": "merge-gate", "needs": ["unit"]},
            "unit": {"name": "unit", "needs": []},
        },
        _MYSTERY: {
            "other": {"name": "different display", "needs": []},
        },
    },
    "findings": [
        {"id": "ci", "workflow_file": _CI},
        {"id": "mystery", "workflow_file": _MYSTERY},
    ],
}


def test_ranking_is_identical_without_an_unpinnable_check_or_readable_required_set():
    p50 = {"unit": 300.0, "merge-gate": 5.0}
    per_pr = [dict(p50) for _ in range(8)]
    legacy = cr._rank_spine_present_first(
        p50, per_pr, frozenset({"merge-gate"}), caps={})

    assert cr._rank_spine_present_first(
        p50, per_pr, frozenset({"merge-gate"}), caps={},
        confirmed_gate_checks=frozenset(p50)) == legacy
    assert cr._rank_spine_present_first(
        p50, per_pr, frozenset({"merge-gate"}), caps={},
        confirmed_gate_checks=None) == legacy


def test_confirmed_required_status_can_crown_without_a_workflow_file(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _RequiredExternalStatusClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)

    assert doc["pr_critical_path"]["critical_path_check"] == "external required status"


def test_collect_crowns_slowest_confirmed_gate_and_verifier_agrees(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _RequiredCrownClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)
    doc["skill_commit_sha"] = "installed:unversioned"
    cp = doc["pr_critical_path"]

    assert cp["critical_path_check"] == "unit"
    assert cp["chain_summary"]["makespan_p50_s"] >= 300.0
    assert cp["chain_summary"]["modal_chain"] == ["unit", "merge-gate"]
    poles = cp["poles"]
    pole_names = [pole["check"] for pole in poles]
    assert pole_names[0] == "unit"
    assert "merge-gate" not in pole_names
    assert "merge-gate" in cp["bookkeeping_checks"]
    assert pole_names.index("mystery matrix") > pole_names.index("unit")
    assert poles[0]["required_reachable"] is True
    assert next(p for p in poles if p["check"] == "mystery matrix")[
        "required_reachable"] is False

    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    assert "could not confirm this check blocks merging" in report.lower()
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok and "provenance" not in check.name]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_unconfirmed_check_is_uncredited_and_verifier_rejects_restamped_credit(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _RequiredCrownClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)
    doc["skill_commit_sha"] = "installed:unversioned"
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT75" and row.get("workflow_file") == _MYSTERY)
    reasons = [
        str(row.get("reason") or "")
        for row in finding.get("wall_clock_derivation") or []
    ]

    assert not (
        finding.get("realization") == "direct"
        and float(finding.get("wall_clock_p50_s") or 0) > 0
    ), (finding.get("pattern"), finding.get("wall_clock_p50_s"), reasons)

    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    assert "critical-path check `mystery matrix`" not in report
    assert "could not confirm this check blocks merging" in report.lower()
    pole = next(
        body for _workflow, check, body in vr._pole_header_sections(report)
        if check == "mystery matrix")
    assert "What a change here can buy (wall-clock)" not in pole
    assert "becomes the gate" not in pole
    assert "it gates until" not in pole
    assert "wall-clock figure applies" not in pole
    toc = report[report.index("## 📋 Contents"):report.index('<a id="pole-1"')]
    assert "the checks that gate your merge" not in toc
    assert "could not confirm it blocks merging" in toc.lower()
    assert "**🐌 Critical path**" not in toc
    assert "**🐌 Measured checks**" in toc

    findings_path = tmp_path / "unconfirmed-findings.json"
    report_path = tmp_path / "unconfirmed-report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    finding["wall_clock_p50_s"] = 195.0
    finding["realization"] = "direct"
    mutant_report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(mutant_report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    mutant_checks = vr.run_checks(
        mutant_report, report_path, findings_path, skill_repo=None)
    assert any(
        not check.skipped and not check.ok
        and "unconfirmed" in f"{check.name} {check.detail}".lower()
        for check in mutant_checks
    )

    finding["wall_clock_p50_s"] = 0.0
    finding["realization"] = "none"
    finding["evidence"] = str(finding.get("evidence") or "") + " critical-path check"
    framing_report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(framing_report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    framing_check = next(
        check for check in vr.run_checks(
            framing_report, report_path, findings_path, skill_repo=None)
        if check.name ==
        "unconfirmed checks carry no direct credit or critical-path framing")
    assert not framing_check.ok
    assert "critical-path wording" in framing_check.detail


def test_confirmed_check_keeps_structural_credit_and_framing(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _AllChecksRequiredClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)
    doc["skill_commit_sha"] = "installed:unversioned"
    finding = next(
        row for row in doc["findings"]
        if row.get("pattern") == "OPT75" and row.get("workflow_file") == _MYSTERY)

    assert finding.get("required_reachable") is not False
    assert finding.get("realization") == "direct"
    assert float(finding.get("wall_clock_p50_s") or 0) > 0
    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    assert "critical-path check `mystery matrix`" in report

    findings_path = tmp_path / "confirmed-findings.json"
    report_path = tmp_path / "confirmed-report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_unconfirmed_verifier_preserves_exact_distinct_check_credit(tmp_path):
    wf = ".github/workflows/ci.yml"
    doc = {
        "pr_critical_path": {"checks": [
            {"name": "test", "workflow_file": wf, "required_reachable": True},
            {"name": "test (self-hosted)", "workflow_file": wf,
             "required_reachable": False},
        ]},
        "findings": [{
            "id": "confirmed", "pattern": "OPT75", "workflow_file": wf,
            "affected_jobs": ["test"], "wall_clock_p50_s": 10.0,
            "realization": "direct", "tier": 1,
        }],
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report = """## 📋 Contents

**🐌 Measured checks** - unconfirmed blockers are labelled.

## Long poles

## Long pole: `ci.yml` ▸ `test (self-hosted)` - 1m

**Could not confirm this check blocks merging.**
"""

    check = vr.check_unconfirmed_checks_have_no_direct_credit_or_gate_framing(
        report, findings_path)
    assert check.ok, check.detail

    doc["findings"][0]["affected_jobs"] = ["test (self-hosted)"]
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    check = vr.check_unconfirmed_checks_have_no_direct_credit_or_gate_framing(
        report, findings_path)
    assert not check.ok
    assert "direct credit" in check.detail
