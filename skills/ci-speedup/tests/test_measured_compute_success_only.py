"""Measured compute counts successful job runs only (owner decision 2026-10-12).

The cost spine's per-job "measured compute" is the total every runner-minute
saving is capped at (`collect_runs._cap_at_measured_compute`, the sizing door)
and verified against (`verify_report.check_saving_within_measured_compute`).
It used to sum every sampled job run, including prior attempts that failed,
were cancelled or timed out. One six-hour hang among a dozen runs then
inflated the job's total several times over, so an overstated saving fitted
under it.

Now the measured compute sums only job runs whose conclusion is `success`.
The excluded runs are stamped (count, sampled minutes, monthly minutes) on
each spine row and on the spine, and the report's cost-spine footnote names
them, so nothing is dropped silently. Findings that measure failure waste on
purpose (OPT35, OPT46, OPT48, OPT57, OPT64) keep their own basis: they are
still bounded by every sampled run.

Run from the repo root:

    python3 -m pytest skills/ci-speedup/tests/test_measured_compute_success_only.py -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _SKILL / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cr = _load("ci_speedup_collect_runs_mc_success", "scripts/collect_runs.py")
bp = _load("ci_speedup_blocking_path_mc_success", "scripts/blocking_path.py")
vr = _load("ci_speedup_verify_report_mc_success", "tests/verify_report.py")

_WF = ".github/workflows/ci.yml"
_VOLUME = 120          # workflow runs per month
_SAMPLED_RUNS = 12     # the owner's case: 12 sampled runs ...
_JOB_S = 600           # ... each `build` run takes 10 min and succeeds ...
_HANG_S = 21600        # ... and one earlier attempt hung for 6 h and failed.


def _job(seconds: int, conclusion: str, idx: int) -> dict:
    start_h, start_m = divmod(idx, 60)
    started = f"2026-10-01T{start_h:02d}:{start_m:02d}:00Z"
    end_total = start_h * 3600 + start_m * 60 + seconds
    eh, rem = divmod(end_total, 3600)
    em, es = divmod(rem, 60)
    day = 1 + eh // 24
    completed = f"2026-10-{day:02d}T{eh % 24:02d}:{em:02d}:{es:02d}Z"
    return {"name": "build", "started_at": started, "completed_at": completed,
            "conclusion": conclusion, "labels": ["ubuntu-latest"],
            "_run_created_at": started}


def _owner_case_spine() -> dict:
    latest = [[_job(_JOB_S, "success", i)] for i in range(_SAMPLED_RUNS)]
    prior = {"event_scope": "all-events", "sampled_workflow_run_count": _SAMPLED_RUNS,
             "runs": [[_job(_HANG_S, "failure", 30)]]}
    spine = cr._build_runner_minute_spine(
        {_WF: {"pull_request": latest}}, {}, {_WF: _VOLUME}, "public",
        prior_attempt_jobs_by_wf={_WF: prior}, workflows_in_play={_WF})
    assert spine is not None and spine["render_ready"] is True
    return spine


# Successful compute: 10 billable min x 120 runs/mo = 1200 min/mo.
# The hang: 360 billable min, seen in 1 of 12 sampled runs -> 3600 min/mo.
_SUCCESS_MIN = 1200.0
_HANG_MONTHLY = 3600.0


def _doc(spine: dict, findings: list[dict]) -> dict:
    return {"repo": "example/repo", "repo_visibility": "public",
            "per_workflow_monthly_volume": {_WF: _VOLUME},
            "data_sources": {"cost_spine_job_fetch_failures": 0},
            "runner_minute_spine": spine, "findings": findings}


def _write(tmp_path: Path, doc: dict, name: str = "findings.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _bound(doc: dict, tmp_path: Path):
    return vr.check_saving_within_measured_compute("# report\n", _write(tmp_path, doc))


# --- engine: the spine row splits successful from excluded job runs ------------------

def test_spine_measured_compute_counts_successful_job_runs_only():
    spine = _owner_case_spine()
    rows = {r["attempt_filter"]: r for r in spine["rows"]}
    latest, prior = rows["latest"], rows["prior"]
    assert latest["success_billable_equiv_min_per_month"] == _SUCCESS_MIN
    assert latest["excluded_non_success_occurrence_count"] == 0
    # The hung attempt stays visible in the table's spend column ...
    assert prior["billable_equiv_min_per_month"] == _HANG_MONTHLY
    # ... but contributes nothing to measured compute, and is stamped as excluded.
    assert prior["success_billable_equiv_min_per_month"] == 0.0
    assert prior["success_occurrence_count"] == 0
    assert prior["excluded_non_success_occurrence_count"] == 1
    assert prior["excluded_non_success_sampled_billable_min"] == 360
    assert prior["excluded_non_success_billable_equiv_min_per_month"] == _HANG_MONTHLY
    assert spine["measured_compute_basis"] == "successful_job_runs_only"
    assert spine["excluded_non_success"] == {
        "job_run_count": 1, "sampled_billable_min": 360,
        "billable_equiv_min_per_month": _HANG_MONTHLY}
    assert spine["totals"]["success_billable_equiv_min_per_month"] == _SUCCESS_MIN


def test_cap_uses_successful_compute_not_the_hang_inflated_total(tmp_path):
    # The owner's regression: a modeled 3000 min/mo estimate on `build`. Against the
    # hang-inflated total (1200 + 3600 = 4800) it slipped through uncapped, crediting
    # 2.5x what the job's successful runs consume. Against successful compute it is
    # capped at 1200, the report's spine contract and saving bound both pass.
    spine = _owner_case_spine()
    f = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0, "sizing_basis": "modeled"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == _SUCCESS_MIN
    assert f["runner_min_basis"] == "measured_spine_clamped"
    doc = _doc(spine, [f])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    path = _write(tmp_path, doc)
    contract = vr.check_runner_minute_spine_contract(report, path)
    assert contract.ok, contract.detail
    bound = vr.check_saving_within_measured_compute(report, path)
    assert bound.ok and not bound.skipped, bound.detail


def test_failure_waste_finding_keeps_its_own_basis(tmp_path):
    # OPT57 measures the hang itself on purpose: it is bounded by every sampled run
    # (4800 here), not by successful compute, and the door does not cap it.
    spine = _owner_case_spine()
    f = {"pattern": "OPT57", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0, "sizing_basis": "measured"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == 3000.0
    res = _bound(_doc(spine, [f]), tmp_path)
    assert res.ok and not res.skipped, res.detail


def test_failure_waste_pattern_set_matches_between_engine_and_verifier():
    assert cr._FAILURE_WASTE_PATTERNS == vr._FAILURE_WASTE_PATTERNS == frozenset(
        {"OPT35", "OPT46", "OPT48", "OPT57", "OPT64"})


# --- verifier: the bound is successful compute; an old-style artifact cannot pass ---

def test_verifier_bounds_a_saving_by_successful_compute(tmp_path):
    spine = _owner_case_spine()
    ok = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
          "runner_min_saving": 1100.0}
    over = dict(ok, runner_min_saving=3000.0)   # within 4800, above 1200
    assert _bound(_doc(spine, [ok]), tmp_path).ok
    res = _bound(_doc(spine, [over]), tmp_path)
    assert not res.ok, res.detail
    assert "successful" in res.detail


def test_verifier_fails_an_old_style_spine_that_counts_failed_runs(tmp_path):
    # Pre-fix artifact: the same rows without the success split. The prior-attempt
    # row is all-status, so its measured compute includes failed runs; a saving
    # bounded by it cannot pass, whatever its size.
    spine = _owner_case_spine()
    old = copy.deepcopy(spine)
    for key in ("measured_compute_basis", "excluded_non_success"):
        old.pop(key, None)
    old["totals"].pop("success_billable_equiv_min_per_month", None)
    for row in old["rows"]:
        for key in ("success_occurrence_count", "success_billable_equiv_min_per_month",
                    "excluded_non_success_occurrence_count",
                    "excluded_non_success_sampled_billable_min",
                    "excluded_non_success_billable_equiv_min_per_month"):
            row.pop(key, None)
    f = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0}
    res = _bound(_doc(old, [f]), tmp_path)
    assert not res.ok, res.detail
    assert "failed, cancelled or timed-out" in res.detail


def test_verifier_keeps_legacy_success_only_rows_valid(tmp_path):
    # A pre-fix spine with only latest rows sampled from successful runs never mixed
    # in failed attempts: it still bounds normally (the shape of most old artifacts).
    rows = [{"workflow_file": _WF, "job_name": "build", "status_filter": "success",
             "attempt_filter": "latest", "billable_equiv_min_per_month": 1200.0}]
    doc = {"runner_minute_spine": {"render_ready": True, "rows": rows},
           "findings": [{"pattern": "OPT16", "workflow_file": _WF,
                         "affected_jobs": ["build"], "runner_min_saving": 1100.0}]}
    res = _bound(doc, tmp_path)
    assert res.ok and not res.skipped, res.detail


def test_contract_fails_a_success_figure_that_includes_the_failed_run(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    forged = copy.deepcopy(doc)
    prior = next(r for r in forged["runner_minute_spine"]["rows"]
                 if r["attempt_filter"] == "prior")
    prior["success_billable_equiv_min_per_month"] = prior["billable_equiv_min_per_month"]
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok
    assert "success_billable_equiv_min_per_month" in res.detail


def test_contract_fails_an_excluded_summary_that_does_not_rederive(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    forged = copy.deepcopy(doc)
    forged["runner_minute_spine"]["excluded_non_success"]["job_run_count"] = 0
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok
    assert "excluded_non_success" in res.detail


# --- report: the footnote states the basis and names the excluded runs -------------

def test_spine_footnote_names_the_basis_and_the_excluded_runs(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    block = "\n".join(bp._runner_minute_spine_block(doc))
    assert ("Measured compute counts successful job runs only: 1200.000 billable "
            "min/mo.") in block
    assert ("1 failed, cancelled or timed-out job run (360 sampled billable min, "
            "3600.000 min/mo) excluded") in block
    # A footnote that disagrees with the stamped facts fails the contract.
    report = "# report\n\n" + block.replace("1200.000 billable", "4800.000 billable") + "\n"
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, doc))
    assert not res.ok
    assert "footnote" in res.detail


def test_footnote_mirror_matches_the_renderer():
    spine = _owner_case_spine()
    none = copy.deepcopy(spine)
    none["excluded_non_success"] = {"job_run_count": 0, "sampled_billable_min": 0,
                                    "billable_equiv_min_per_month": 0.0}
    two = copy.deepcopy(spine)
    two["excluded_non_success"]["job_run_count"] = 2
    legacy = copy.deepcopy(spine)
    legacy.pop("measured_compute_basis")
    for v in (spine, none, two, legacy):
        assert bp._measured_compute_footnote(v) == vr._measured_compute_footnote(v)
    assert "no failed, cancelled or timed-out job run was sampled" in (
        bp._measured_compute_footnote(none))
    assert "2 failed, cancelled or timed-out job runs " in bp._measured_compute_footnote(two)
    assert bp._measured_compute_footnote(legacy) == ""
