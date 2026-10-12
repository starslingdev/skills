"""Measured compute counts completed job runs (owner decision 2026-10-12, revised).

The cost spine's per-job "measured compute" is the total every runner-minute
saving is capped at (`collect_runs._cap_at_measured_compute`, the sizing door)
and verified against (`verify_report.check_saving_within_measured_compute`).

It counts every COMPLETED job run, success or failure: findings size their
savings over all runs (a cache fix speeds a failed run too), so the cap must
share that basis. It excludes only runs no speed fix can remove: conclusion
`timed_out` or `cancelled`, and any run whose span falls in the default-timeout
kill window OPT57 defines ([95% x 360 min, 360 min + 15 min grace]). One
six-hour hang among a dozen runs would otherwise inflate a job's total several
times over. The excluded runs are stamped (count, reason, sampled minutes,
monthly minutes) on each spine row and on the spine, and the cost-spine
footnote names them, so nothing is dropped silently. Findings that measure
failure waste on purpose (OPT35, OPT46, OPT48, OPT57, OPT64) keep their own
basis: every sampled run.

Run from the repo root:

    python3 -m pytest skills/ci-speedup/tests/test_measured_compute_completed_runs.py -q
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


cr = _load("ci_speedup_collect_runs_mc_completed", "scripts/collect_runs.py")
bp = _load("ci_speedup_blocking_path_mc_completed", "scripts/blocking_path.py")
vr = _load("ci_speedup_verify_report_mc_completed", "tests/verify_report.py")

_WF = ".github/workflows/ci.yml"
_VOLUME = 120          # workflow runs per month
_SAMPLED_RUNS = 12     # the owner's case: 12 sampled runs ...
_JOB_S = 600           # ... each `build` run takes 10 min and succeeds ...
_HANG_S = 21600        # ... and one earlier attempt hung 6 h and was killed.

_SPLIT_KEYS = ("completed_occurrence_count", "completed_sampled_billable_min",
               "completed_billable_equiv_min_per_month",
               "excluded_unfinished_occurrence_count",
               "excluded_unfinished_sampled_billable_min",
               "excluded_unfinished_billable_equiv_min_per_month",
               "excluded_unfinished_by_reason")


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


def _failed_runs_spine() -> dict:
    # 12 sampled runs of `build`: 10 succeed at 10 min, 2 fail at 8 min (ordinary
    # test failures, nowhere near the kill window).
    latest = ([[_job(_JOB_S, "success", i)] for i in range(10)]
              + [[_job(480, "failure", 10 + i)] for i in range(2)])
    spine = cr._build_runner_minute_spine(
        {_WF: {"pull_request": latest}}, {}, {_WF: _VOLUME}, "public",
        workflows_in_play={_WF})
    assert spine is not None and spine["render_ready"] is True
    return spine


# Owner case: 10 billable min x 120 runs/mo = 1200 min/mo of completed compute.
# The hang: 360 billable min, seen in 1 of 12 sampled runs -> 3600 min/mo.
_COMPLETED_MIN = 1200.0
_HANG_MONTHLY = 3600.0
# Failed-runs case: 116 sampled min over 12 runs, mean 9.667 (3 decimals) x 120 runs/mo
# = 1160.04 min/mo, all of it completed compute.
_WITH_FAILURES_MIN = 1160.04


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


def _render_and_check(doc: dict, tmp_path: Path):
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    path = _write(tmp_path, doc)
    return (vr.check_runner_minute_spine_contract(report, path),
            vr.check_saving_within_measured_compute(report, path))


# --- engine: the spine row splits completed runs from unfinished ones -------------

def test_spine_excludes_the_killed_attempt_from_measured_compute():
    spine = _owner_case_spine()
    rows = {r["attempt_filter"]: r for r in spine["rows"]}
    latest, prior = rows["latest"], rows["prior"]
    assert latest["completed_billable_equiv_min_per_month"] == _COMPLETED_MIN
    assert latest["excluded_unfinished_occurrence_count"] == 0
    # The killed attempt stays visible in the table's spend column ...
    assert prior["billable_equiv_min_per_month"] == _HANG_MONTHLY
    # ... but contributes nothing to measured compute, and is stamped with its reason.
    assert prior["completed_billable_equiv_min_per_month"] == 0.0
    assert prior["completed_occurrence_count"] == 0
    assert prior["excluded_unfinished_occurrence_count"] == 1
    assert prior["excluded_unfinished_sampled_billable_min"] == 360
    assert prior["excluded_unfinished_billable_equiv_min_per_month"] == _HANG_MONTHLY
    assert prior["excluded_unfinished_by_reason"] == {
        "timed_out": 0, "cancelled": 0, "kill_window": 1}
    assert spine["measured_compute_basis"] == "completed_job_runs_only"
    assert spine["excluded_unfinished"] == {
        "job_run_count": 1, "sampled_billable_min": 360,
        "billable_equiv_min_per_month": _HANG_MONTHLY,
        "by_reason": {"timed_out": 0, "cancelled": 0, "kill_window": 1}}
    assert spine["totals"]["completed_billable_equiv_min_per_month"] == _COMPLETED_MIN


def test_spine_counts_ordinary_failed_runs_as_completed():
    spine = _failed_runs_spine()
    row = spine["rows"][0]
    assert row["completed_occurrence_count"] == 12
    assert row["completed_billable_equiv_min_per_month"] == _WITH_FAILURES_MIN
    assert row["excluded_unfinished_occurrence_count"] == 0
    assert spine["excluded_unfinished"]["job_run_count"] == 0


def test_timed_out_and_cancelled_runs_are_excluded_with_their_reason():
    latest = ([[_job(_JOB_S, "success", i)] for i in range(10)]
              + [[_job(1200, "timed_out", 10)], [_job(300, "cancelled", 11)]])
    spine = cr._build_runner_minute_spine(
        {_WF: {"pull_request": latest}}, {}, {_WF: _VOLUME}, "public",
        workflows_in_play={_WF})
    row = spine["rows"][0]
    assert row["excluded_unfinished_by_reason"] == {
        "timed_out": 1, "cancelled": 1, "kill_window": 0}
    assert row["excluded_unfinished_sampled_billable_min"] == 25
    assert round(row["completed_billable_equiv_min_per_month"], 1) == 1000.0


def test_kill_window_edges_match_opt57():
    # OPT57's kill window holds only runs that FAILED there: GitHub kills a job at the
    # default timeout with a failure. A run that SUCCEEDED (or ended neutral) inside the
    # window finished on its own (an explicit timeout-minutes above 342, a long release
    # job), so it counts as completed compute and is never reported as "killed".
    assert cr._spine_unfinished_reason({"conclusion": "success"},
                                       cr._OPT57_NEAR_DEFAULT_TIMEOUT_S) is None
    assert cr._spine_unfinished_reason({"conclusion": "neutral"},
                                       cr._OPT57_NEAR_DEFAULT_TIMEOUT_S) is None
    assert cr._spine_unfinished_reason({"conclusion": "failure"},
                                       cr._OPT57_NEAR_DEFAULT_TIMEOUT_S) == "kill_window"
    assert cr._spine_unfinished_reason({"conclusion": "failure"},
                                       cr._OPT57_DEFAULT_KILL_CEILING_S) == "kill_window"
    assert cr._spine_unfinished_reason({"conclusion": "failure"},
                                       cr._OPT57_NEAR_DEFAULT_TIMEOUT_S - 1) is None
    # Owner decision 2026-10-12: a failed run AT OR BEYOND the window's start is out, with
    # no upper bound. Past the 375-min ceiling the span is not compute (a 30-day deployment
    # approval wait reported as a failed job); OPT57 already ignores it.
    assert cr._spine_unfinished_reason({"conclusion": "failure"},
                                       cr._OPT57_DEFAULT_KILL_CEILING_S + 1) == "kill_window"
    assert cr._spine_unfinished_reason({"conclusion": "failure"}, 43200 * 60) == "kill_window"
    # A successful run of any length still counts.
    assert cr._spine_unfinished_reason({"conclusion": "success"}, 43200 * 60) is None


def test_a_failed_approval_wait_is_excluded_and_a_long_success_still_counts():
    runs = [[_job(_JOB_S, "success", i)] for i in range(10)]
    runs.append([_job(600 * 60, "success", 10)])          # a 10-hour build that succeeded
    runs.append([_job(43200 * 60, "failure", 11)])        # a 30-day approval wait, failed
    row = _spine_for(runs, volume=120)["rows"][0]
    assert row["completed_occurrence_count"] == 11
    assert row["completed_sampled_billable_min"] == 10 * 10 + 600
    assert row["excluded_unfinished_by_reason"] == {
        "timed_out": 0, "cancelled": 0, "kill_window": 1}
    assert row["excluded_unfinished_sampled_billable_min"] == 43200


# --- the cap and the gate ----------------------------------------------------------

def test_cap_excludes_the_killed_attempt(tmp_path):
    # The owner's regression: a modeled 3000 min/mo estimate on `build`. Against the
    # hang-inflated total (1200 + 3600 = 4800) it slipped through uncapped. Against
    # completed compute it is capped at 1200; the spine contract and bound both pass.
    spine = _owner_case_spine()
    f = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0, "sizing_basis": "modeled"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == _COMPLETED_MIN
    assert f["runner_min_basis"] == "measured_spine_clamped"
    contract, bound = _render_and_check(_doc(spine, [f]), tmp_path)
    assert contract.ok, contract.detail
    assert bound.ok and not bound.skipped, bound.detail


def test_cap_includes_ordinary_failed_runs(tmp_path):
    # A saving sized over all 12 runs (failures included: a cache fix speeds a
    # failing run too) must not be clipped to the successful runs' 1000 min/mo.
    spine = _failed_runs_spine()
    f = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 1100.0, "sizing_basis": "modeled"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == 1100.0
    assert f["runner_min_basis"] == "not_spine_derivable"
    contract, bound = _render_and_check(_doc(spine, [f]), tmp_path)
    assert contract.ok, contract.detail
    assert bound.ok and not bound.skipped, bound.detail


def test_whole_run_cancel_derives_from_completed_compute():
    # OPT45 derives hit_rate x measured compute, so it must use the completed set.
    spine = _failed_runs_spine()
    f = {"pattern": "OPT45", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 5000.0}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == round(0.2 * _WITH_FAILURES_MIN, 1)


def test_failure_waste_finding_keeps_its_own_basis(tmp_path):
    # OPT57 measures the killed attempt itself on purpose: it is bounded by every
    # sampled run (4800 here), not by completed compute, and the door does not cap it.
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


def test_verifier_bounds_a_saving_by_completed_compute(tmp_path):
    spine = _owner_case_spine()
    ok = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
          "runner_min_saving": 1100.0}
    over = dict(ok, runner_min_saving=3000.0)   # within 4800, above 1200
    assert _bound(_doc(spine, [ok]), tmp_path).ok
    res = _bound(_doc(spine, [over]), tmp_path)
    assert not res.ok, res.detail
    assert "completed job runs" in res.detail


def test_verifier_fails_an_old_style_spine_that_mixes_in_unfinished_runs(tmp_path):
    # Pre-split artifact: the same rows without the split. The prior-attempt row is
    # all-status, so its measured compute may include killed or cancelled runs; a
    # saving bounded by it cannot pass, whatever its size.
    spine = _owner_case_spine()
    old = copy.deepcopy(spine)
    for key in ("measured_compute_basis", "excluded_unfinished"):
        old.pop(key, None)
    old["totals"].pop("completed_billable_equiv_min_per_month", None)
    for row in old["rows"]:
        for key in _SPLIT_KEYS:
            row.pop(key, None)
    f = {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0}
    res = _bound(_doc(old, [f]), tmp_path)
    assert not res.ok, res.detail
    assert "timed-out, cancelled or killed" in res.detail


def test_verifier_keeps_legacy_success_only_rows_valid(tmp_path):
    rows = [{"workflow_file": _WF, "job_name": "build", "status_filter": "success",
             "attempt_filter": "latest", "billable_equiv_min_per_month": 1200.0}]
    doc = {"runner_minute_spine": {"render_ready": True, "rows": rows},
           "findings": [{"pattern": "OPT16", "workflow_file": _WF,
                         "affected_jobs": ["build"], "runner_min_saving": 1100.0}]}
    res = _bound(doc, tmp_path)
    assert res.ok and not res.skipped, res.detail


def test_contract_fails_a_completed_figure_that_includes_the_killed_run(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    forged = copy.deepcopy(doc)
    prior = next(r for r in forged["runner_minute_spine"]["rows"]
                 if r["attempt_filter"] == "prior")
    prior["completed_billable_equiv_min_per_month"] = prior["billable_equiv_min_per_month"]
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok
    assert "completed_billable_equiv_min_per_month" in res.detail


def test_contract_fails_an_excluded_summary_that_does_not_rederive(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    forged = copy.deepcopy(doc)
    forged["runner_minute_spine"]["excluded_unfinished"]["by_reason"]["kill_window"] = 0
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok
    assert "excluded_unfinished" in res.detail


def test_contract_accepts_a_large_bucket_whose_mean_rounding_hides_the_total(tmp_path):
    # 1500 runs totalling 15001 billable min: the row's mean rounds to 10.001, and
    # 10.001 x 1500 = 15001.5, so the sampled total cannot be recovered from the
    # mean. The split must re-derive from its own stamped whole minutes.
    jobs = [[_job(600, "success", i % 60)] for i in range(1499)] + [[_job(660, "success", 0)]]
    spine = cr._build_runner_minute_spine(
        {_WF: {"pull_request": jobs}}, {}, {_WF: 3000}, "public", workflows_in_play={_WF})
    doc = _doc(spine, [])
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, doc))
    assert res.ok, res.detail


# --- report: the footnote states the basis and names the excluded runs ------------

def test_spine_footnote_names_the_basis_and_the_excluded_runs(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    block = "\n".join(bp._runner_minute_spine_block(doc))
    assert ("Measured compute counts completed job runs: 1200.000 min/mo. 1 timed-out, "
            "cancelled or killed job run (1 failed at or past the 6-hour default timeout; "
            "360 sampled min, 3600.000 min/mo) is excluded because no speed fix removes "
            "it; the cost-spine rows and Total still count its minutes.") in block
    assert ("Failed, neutral and other finished runs count. Modeled savings are capped at "
            "measured compute and derived savings are computed from it; measured "
            "savings and failure-waste findings are checked against every sampled run "
            "instead. Savings on jobs whose every sampled run was excluded are shown "
            "unsized.") in block
    report ="# report\n\n" + block.replace("runs: 1200.000", "runs: 4800.000") + "\n"
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, doc))
    assert not res.ok
    assert "footnote" in res.detail


def test_footnote_mirror_matches_the_renderer():
    spine = _owner_case_spine()
    none = copy.deepcopy(spine)
    none["excluded_unfinished"] = {"job_run_count": 0, "sampled_billable_min": 0,
                                   "billable_equiv_min_per_month": 0.0,
                                   "by_reason": {"timed_out": 0, "cancelled": 0,
                                                 "kill_window": 0}}
    two = copy.deepcopy(spine)
    two["excluded_unfinished"]["job_run_count"] = 2
    legacy = copy.deepcopy(spine)
    legacy.pop("measured_compute_basis")
    for v in (spine, none, two, legacy):
        assert bp._measured_compute_footnote(v) == vr._measured_compute_footnote(v)
    assert ("No timed-out, cancelled or killed job run was found in the sample."
            in bp._measured_compute_footnote(none))
    assert ("2 timed-out, cancelled or killed job runs (1 failed at or past the 6-hour "
            "default timeout; 360 sampled min, 3600.000 min/mo) are excluded because "
            "no speed fix removes them; the cost-spine rows and Total still count their "
            "minutes.") in bp._measured_compute_footnote(two)
    assert bp._measured_compute_footnote(legacy) == ""


# --- review round: one basis rule, honest edge states, stricter contract ----------------

def _spine_for(jobs_by_run: list[list[dict]], volume: int = _VOLUME, **kw) -> dict:
    spine = cr._build_runner_minute_spine(
        {_WF: {"pull_request": jobs_by_run}}, {}, {_WF: volume}, "public",
        workflows_in_play={_WF}, **kw)
    assert spine is not None
    return spine


def _named(job: dict, name: str) -> dict:
    return dict(job, name=name)


def _block(doc: dict) -> str:
    return "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"


def test_basis_rule_matches_between_engine_and_verifier():
    cases = [
        {"pattern": "OPT16"},
        {"pattern": "OPT16", "sizing_basis": "modeled"},
        {"pattern": "OPT77", "sizing_basis": "measured"},
        {"pattern": "OPT65", "tier2_neutrality": {"kind": "x"}},
        {"pattern": "OPT65", "tier2_neutrality": {}},
        {"pattern": "OPT35", "sizing_basis": "modeled"},
        {"pattern": "OPT45"},
    ]
    got = [cr._measured_compute_basis_for(f) for f in cases]
    assert got == [vr._measured_compute_basis_for(f) for f in cases]
    assert got == ["completed", "completed", "all_runs", "all_runs", "completed",
                   "all_runs", "completed"]
    assert cr._SPINE_UNFINISHED_REASONS == vr._SPINE_UNFINISHED_REASONS
    for args in ((6999.0, 7, 0), (1250.04, 100, 25), (0.0, 0, 0), (3600.0, 0, 360)):
        assert cr._spine_split_monthly(*args) == vr._spine_split_monthly(*args)


def test_measured_saving_is_bounded_by_every_run_on_both_sides(tmp_path):
    # The door never caps a measured saving (the verifier re-derives it exactly), so the
    # bound must use the same basis: every sampled run. A job whose every run was cancelled
    # measures 0 completed min/mo; its measured 600 must not be refused against that 0.
    spine = _spine_for([[_named(_job(_JOB_S, "success", i), "build"),
                         _named(_job(_JOB_S, "cancelled", i), "flaky")]
                        for i in range(_SAMPLED_RUNS)])
    f = {"pattern": "OPT77", "workflow_file": _WF, "affected_jobs": ["flaky"],
         "runner_min_saving": 600.0, "sizing_basis": "measured"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == 600.0
    res = _bound(_doc(spine, [f]), tmp_path)
    assert res.ok and not res.skipped, res.detail


def test_measured_saving_on_a_mixed_job_uses_every_run_not_the_fallback(tmp_path):
    # The all-cancelled job above reaches the every-run bound through the all-excluded
    # fallback too, so it cannot tell the measured-basis rule apart. Here the job has
    # completed runs (10 x 10 min) AND cancelled ones (2 x 60 min): completed compute is
    # 1000 min/mo, every run 2200. A measured 1500 sits between them, so it passes only
    # because a measured saving is bounded by every run, on both sides.
    runs = [[_named(_job(_JOB_S, "success", i), "mixed")] for i in range(10)]
    runs += [[_named(_job(3600, "cancelled", 10 + i), "mixed")] for i in range(2)]
    spine = _spine_for(runs)
    row = spine["rows"][0]
    assert round(row["completed_billable_equiv_min_per_month"]) == 1000
    assert round(row["billable_equiv_min_per_month"]) == 2200
    f = {"pattern": "OPT77", "workflow_file": _WF, "affected_jobs": ["mixed"],
         "runner_min_saving": 1500.0, "sizing_basis": "measured"}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == 1500.0
    res = _bound(_doc(spine, [f]), tmp_path)
    assert res.ok and not res.skipped, res.detail
    modeled = dict(f, sizing_basis="modeled", runner_min_saving=1500.0)
    res = _bound(_doc(spine, [modeled]), tmp_path)
    assert not res.ok, res.detail


def test_modeled_failure_waste_finding_is_capped_at_every_run_compute():
    # OPT35 is a modeled failure-waste estimate: the door caps it, at every-run compute
    # (4800), never at completed compute (1200). This reaches the door's all-runs branch.
    spine = _owner_case_spine()
    within = {"pattern": "OPT35", "workflow_file": _WF, "affected_jobs": ["build"],
              "runner_min_saving": 3000.0, "sizing_basis": "modeled"}
    over = dict(within, runner_min_saving=9000.0)
    cr._reground_runner_minute_savings([within, over], spine)
    assert within["runner_min_saving"] == 3000.0
    assert over["runner_min_saving"] == _COMPLETED_MIN + _HANG_MONTHLY
    assert "of every sampled run" in over["size_note"]


def test_every_finished_conclusion_counts_and_only_unfinished_ones_are_excluded():
    runs = [[_job(_JOB_S, c, i)] for i, c in enumerate(
        ("success", "success", "failure", "neutral", "action_required",
         "cancelled", "timed_out"))]
    runs.append([_job(int(0.96 * 360 * 60), "success", 20)])     # finished on its own: counts
    runs.append([_job(int(0.96 * 360 * 60), "failure", 21)])     # killed in the window
    row = _spine_for(runs, volume=80)["rows"][0]
    assert row["completed_occurrence_count"] == 6
    assert row["excluded_unfinished_by_reason"] == {
        "timed_out": 1, "cancelled": 1, "kill_window": 1}
    assert row["completed_sampled_billable_min"] == 50 + 346


def test_a_tiny_completed_figure_is_a_completed_bound_on_both_sides(tmp_path):
    # 40 runs killed in the window plus one 1-minute success at 1 run/mo: completed compute
    # is about 0.02 min/mo, real but rounding to 0.0. The door treated it as "every run
    # excluded" (round(measured, 1) <= 0), kept a modeled 50 under the every-run cap with a
    # false note, while the verifier bounded the same finding by 0.02 and refused the
    # engine's own report. One unrounded test on both sides.
    runs = [[_job(350 * 60, "failure", i % 59)] for i in range(40)]
    runs.append([_job(60, "success", 59)])
    spine = _spine_for(runs, volume=1)
    row = spine["rows"][0]
    assert 0 < row["completed_billable_equiv_min_per_month"] < 0.05
    f = {"pattern": "OPT17", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 50.0, "sizing_basis": "modeled"}
    cr._reground_runner_minute_savings([f], spine)
    assert f.get("runner_min_door_note") != cr._ALL_RUNS_EXCLUDED_NOTE
    assert f["runner_min_saving"] == 0.0
    res = _bound(_doc(spine, [f]), tmp_path)
    assert res.ok, res.detail


def test_derived_saving_uses_completed_compute():
    # OPT45 / OPT29 derive hit_rate x measured compute: on the owner fixture that is now
    # 0.2 x 1200 = 240 (it was 0.2 x 4800 = 960 while the killed attempt counted).
    spine = _owner_case_spine()
    opt45 = {"pattern": "OPT45", "workflow_file": _WF, "affected_jobs": ["build"],
             "runner_min_saving": 5000.0}
    opt29 = {"pattern": "OPT29", "workflow_file": _WF, "affected_jobs": ["build"],
             "runner_min_saving": 5000.0}
    cr._reground_runner_minute_savings([opt45, opt29], spine)
    assert opt45["runner_min_saving"] == 240.0
    assert opt29["runner_min_saving"] == 120.0
    assert "of completed job runs" in opt45["size_note"]


def _all_killed_spine() -> dict:
    # `build` succeeds; `nightly` is killed at the 6-hour default timeout on every run.
    return _spine_for([[_named(_job(_JOB_S, "success", i), "build"),
                        _named(_job(_HANG_S, "failure", i), "nightly")]
                       for i in range(_SAMPLED_RUNS)])


def test_every_run_excluded_leaves_a_modeled_saving_unsized(tmp_path):
    # Owner decision 2026-10-12: every sampled run of `nightly` was killed, 0 completed
    # min/mo. Those minutes are not removable by a speed fix, so a modeled estimate gets
    # NO sized saving (it used to fall back to the 43,200 every-run total, crediting the
    # killed minutes): unsized, not derivable, with the reason, exactly like a derived
    # saving. The failure-waste findings keep their own every-run basis.
    spine = _all_killed_spine()
    small = {"pattern": "OPT17", "workflow_file": _WF, "affected_jobs": ["nightly"],
             "runner_min_saving": 50.0, "sizing_basis": "modeled"}
    big = dict(small, runner_min_saving=99999.0)
    opt57 = {"pattern": "OPT57", "workflow_file": _WF, "affected_jobs": ["nightly"],
             "runner_min_saving": 3000.0, "sizing_basis": "modeled"}
    cr._reground_runner_minute_savings([small, big, opt57], spine)
    for f in (small, big):
        assert f["runner_min_saving"] is None
        assert f["runner_min_basis"] == "not_spine_derivable"
        assert "every sampled run of the affected job(s) was excluded" in f["size_note"]
        assert "no billable compute" not in f["size_note"]
    assert opt57["runner_min_saving"] == 3000.0
    res = _bound(_doc(spine, [small, big, opt57]), tmp_path)
    assert res.ok, res.detail
    # The verifier agrees: a SIZED modeled saving on that job is refused.
    sized = {"pattern": "OPT17", "workflow_file": _WF, "affected_jobs": ["nightly"],
             "runner_min_saving": 50.0, "sizing_basis": "modeled"}
    res = _bound(_doc(spine, [sized]), tmp_path)
    assert not res.ok, res.detail
    assert "every sampled run" in res.detail and "excluded" in res.detail


def test_every_run_excluded_leaves_a_derived_saving_not_derivable():
    spine = _all_killed_spine()
    opt45 = {"pattern": "OPT45", "workflow_file": _WF, "affected_jobs": ["nightly"],
             "runner_min_saving": 500.0}
    opt73 = {"pattern": "OPT73", "workflow_file": _WF, "affected_jobs": ["nightly"],
             "runner_min_saving": 500.0}
    cr._reground_runner_minute_savings([opt45, opt73], spine)
    for f in (opt45, opt73):
        assert f["runner_min_saving"] is None
        assert f["runner_min_basis"] == "not_spine_derivable"
        assert "every sampled run of the affected job(s) was excluded" in f["size_note"]
        assert "no affected job resolved" not in f["size_note"]


def test_clamp_with_a_row_missing_its_split_is_refused(tmp_path):
    # A stamped spine where one row lost its split: the door falls back to that row's full
    # figure (4800, no clamp), and the verifier refuses both the bound and the contract.
    spine = _owner_case_spine()
    for row in spine["rows"]:
        if row["attempt_filter"] == "prior":
            for key in _SPLIT_KEYS:
                row.pop(key)
    f = {"pattern": "OPT73", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 3000.0}
    cr._reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] == 3000.0
    doc = _doc(spine, [f])
    contract, bound = _render_and_check(doc, tmp_path)
    assert not bound.ok and "may include timed-out" in bound.detail
    assert not contract.ok and "split missing" in contract.detail


def test_cross_workflow_fallback_bounds_by_completed_compute(tmp_path):
    # A reusable-workflow caller names a job that only has rows under another workflow
    # file: the fallback must use that row's completed figure, not its all-runs figure.
    spine = _owner_case_spine()
    ok = {"pattern": "OPT16", "workflow_file": ".github/workflows/caller.yml",
          "affected_jobs": ["build"], "runner_min_saving": 1100.0}
    over = dict(ok, runner_min_saving=3000.0)
    assert _bound(_doc(spine, [ok]), tmp_path).ok
    res = _bound(_doc(spine, [over]), tmp_path)
    assert not res.ok and "completed job runs" in res.detail


def test_legacy_success_row_of_a_continue_on_error_job_is_refused(tmp_path):
    rows = [{"workflow_file": _WF, "job_name": "experimental", "status_filter": "success",
             "attempt_filter": "latest", "billable_equiv_min_per_month": 1200.0}]
    doc = {"runner_minute_spine": {"render_ready": True, "rows": rows},
           "workflow_job_graph": {_WF: {"experimental": {"continue_on_error": True}}},
           "findings": [{"pattern": "OPT16", "workflow_file": _WF,
                         "affected_jobs": ["experimental"], "runner_min_saving": 100.0}]}
    res = _bound(doc, tmp_path)
    assert not res.ok and "continue-on-error" in res.detail
    doc["workflow_job_graph"][_WF]["experimental"]["continue_on_error"] = False
    assert _bound(doc, tmp_path).ok


def test_contract_refuses_a_split_without_its_basis_stamp(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = _block(doc)
    forged = copy.deepcopy(doc)
    forged["runner_minute_spine"].pop("measured_compute_basis")
    for row in forged["runner_minute_spine"]["rows"]:
        row["completed_billable_equiv_min_per_month"] = 99999.0
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok and "measured_compute_basis" in res.detail
    # ... and the bound ignores an unstamped split instead of trusting it.
    forged["findings"] = [{"pattern": "OPT16", "workflow_file": _WF,
                           "affected_jobs": ["build"], "runner_min_saving": 50000.0}]
    assert not _bound(forged, tmp_path).ok


def test_contract_refuses_each_forged_spine_level_split_fact(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    report = _block(doc)
    cases = {
        "totals.completed_billable_equiv_min_per_month":
            lambda s: s["totals"].__setitem__("completed_billable_equiv_min_per_month", 4800.0),
        "measured_compute_basis=":
            lambda s: s.__setitem__("measured_compute_basis", "all_job_runs"),
        "excluded_unfinished does not rederive":
            lambda s: s["excluded_unfinished"].__setitem__("sampled_billable_min", 0),
    }
    for i, (needle, forge) in enumerate(cases.items()):
        forged = copy.deepcopy(doc)
        forge(forged["runner_minute_spine"])
        res = vr.check_runner_minute_spine_contract(
            report, _write(tmp_path, forged, f"forged{i}.json"))
        assert not res.ok, (needle, res.detail)
        assert needle in res.detail, (needle, res.detail)


def test_contract_refuses_excluded_runs_that_bill_no_minutes(tmp_path):
    # Every sampled run has a positive span, so each bills at least one minute: a row that
    # claims 4 excluded runs worth 0 minutes is misclassified, even if its sums balance.
    runs = ([[_job(_JOB_S, "success", i)] for i in range(8)]
            + [[_job(_JOB_S, "cancelled", 8 + i)] for i in range(4)])
    doc = _doc(_spine_for(runs), [])
    report = _block(doc)
    forged = copy.deepcopy(doc)
    row = forged["runner_minute_spine"]["rows"][0]
    row["completed_sampled_billable_min"] += row["excluded_unfinished_sampled_billable_min"]
    row["excluded_unfinished_sampled_billable_min"] = 0
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, forged))
    assert not res.ok and "fewer minutes than it has job runs" in res.detail


def test_an_unsplit_bound_does_not_hide_a_later_overstated_saving(tmp_path):
    rows = [
        {"workflow_file": _WF, "job_name": "old", "status_filter": "all-status",
         "attempt_filter": "prior", "billable_equiv_min_per_month": 500.0},
        {"workflow_file": _WF, "job_name": "build", "status_filter": "success",
         "attempt_filter": "latest", "billable_equiv_min_per_month": 1200.0},
    ]
    doc = {"runner_minute_spine": {"render_ready": True, "rows": rows}, "findings": [
        {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["old"],
         "runner_min_saving": 10.0},
        {"pattern": "OPT16", "workflow_file": _WF, "affected_jobs": ["build"],
         "runner_min_saving": 5000.0}]}
    res = _bound(doc, tmp_path)
    assert not res.ok
    assert res.detail.startswith("2 refused"), res.detail
    assert "credits 5000" in res.detail, res.detail


def test_completed_compute_never_exceeds_the_rows_billable_figure(tmp_path):
    # The row extrapolates its 3-decimal mean (7/3 -> 2.333 x 3000 = 6999); the split must
    # share that rounding, never stamp 7000 under a 6999 row.
    spine = _spine_for([[_job(120, "success", 0)], [_job(120, "success", 1)],
                        [_job(180, "success", 2)]], volume=3000)
    (row,) = spine["rows"]
    assert row["billable_equiv_min_per_month"] == 6999.0
    assert row["completed_billable_equiv_min_per_month"] == 6999.0
    doc = _doc(spine, [])
    res = vr.check_runner_minute_spine_contract(_block(doc), _write(tmp_path, doc))
    assert res.ok, res.detail


def test_footnote_outside_the_cost_spine_section_is_refused(tmp_path):
    spine = _owner_case_spine()
    doc = _doc(spine, [])
    note = bp._measured_compute_footnote(spine)
    kept = [line for line in bp._runner_minute_spine_block(doc) if line != note]
    report = "# report\n\n" + "\n".join(kept) + "\n## Next section\n\n" + note + "\n"
    res = vr.check_runner_minute_spine_contract(report, _write(tmp_path, doc))
    assert not res.ok and "footnote" in res.detail


def test_footnote_names_failed_job_downloads():
    spine = _spine_for([[_job(_JOB_S, "success", i)] for i in range(3)],
                       coverage_fetch_failures=2)
    note = bp._measured_compute_footnote(spine)
    assert "No timed-out, cancelled or killed job run was found in the sample." in note
    assert "2 job downloads failed, so the runs in them were not checked." in note
    one = copy.deepcopy(spine)
    one["workflow_coverage"]["job_fetch_failures"] = 1
    for render in (bp._measured_compute_footnote, vr._measured_compute_footnote):
        assert "1 job download failed, so the runs in it were not checked." in render(one)
    assert note == vr._measured_compute_footnote(spine)
