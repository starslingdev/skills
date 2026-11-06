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
    "caller_gate_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_CALLER = ".github/workflows/caller.yml"
_SUITE = ".github/workflows/suite.yml"


def _iso(seconds: int) -> str:
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _job(name: str, started: int, completed: int, run_id: int) -> dict:
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(started),
        "completed_at": _iso(completed),
        "steps": [{"name": "work", "started_at": _iso(started),
                   "completed_at": _iso(completed)}],
    }


class _CallerGateClient:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.shas = [f"s{i}" for i in range(1, 9)]

    def available(self) -> bool:
        return True

    def _runs(self, workflow_id: int) -> list[dict]:
        base = 100 if workflow_id == 1 else 200
        return [{
            "id": base + i,
            "event": "pull_request",
            "head_sha": sha,
            "status": "completed",
            "conclusion": "success",
            "created_at": _iso(0),
            "run_started_at": _iso(0),
            "updated_at": _iso(305 if workflow_id == 1 else 300),
        } for i, sha in enumerate(self.shas)]

    def _jobs(self, run_id: int) -> list[dict]:
        if 100 <= run_id < 200:
            return [
                _job("tests / unit (1)", 0, 300, run_id),
                _job("merge-gate", 300, 305, run_id),
            ]
        if 200 <= run_id < 300:
            return [_job("unit (1)", 0, 300, run_id)]
        return []

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _CALLER, "name": "caller"},
                {"id": 2, "path": _SUITE, "name": "suite"},
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
                {"name": "tests / unit (1)", "started_at": _iso(0),
                 "completed_at": _iso(300)},
                {"name": "merge-gate", "started_at": _iso(300),
                 "completed_at": _iso(305)},
                {"name": "unit (1)", "started_at": _iso(0),
                 "completed_at": _iso(300)},
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


_GRAPH = {
    _CALLER: {
        "merge-gate": {"name": "merge-gate", "needs": ["tests"]},
        "tests": {"name": "tests", "needs": [], "reusable": True,
                  "uses": "./.github/workflows/suite.yml"},
    },
    _SUITE: {
        "unit": {"name": "unit (${{ matrix.shard }})", "needs": [],
                 "matrix": True, "reusable": False},
    },
}

_DOC = {
    "repo": "o/r",
    "workflow_job_graph": _GRAPH,
    "findings": [
        {"id": "caller", "workflow_file": _CALLER},
        {"id": "suite", "workflow_file": _SUITE},
    ],
}


def test_nested_caller_inherits_gate_but_standalone_run_does_not():
    required = frozenset({"merge-gate"})
    crit = {
        _CALLER: {"job_p50": {"tests / unit (1)": 300.0,
                              "merge-gate": 5.0}},
        _SUITE: {"job_p50": {"unit (1)": 300.0}},
    }

    assert cr._pole_is_required_reachable(
        "tests / unit (1)", required, _GRAPH, crit) is True
    assert cr._pole_is_required_reachable(
        "unit (1)", required, _GRAPH, crit) is False
    assert cr._check_to_job_node_scanned(
        "tests / unit (1)", _GRAPH) == (_SUITE, "unit")


def test_collect_keeps_only_the_caller_path_in_the_required_spine(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _CallerGateClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)
    cp = doc["pr_critical_path"]

    pole_names = [pole["check"] for pole in cp["poles"]]
    assert "tests / unit (1)" in pole_names
    assert "unit (1)" not in pole_names
    assert "unit (1)" in cp["dropped_non_required_checks"]

    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
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
