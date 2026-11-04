"""OPT64 (repeated workflow attempts) is ranked only on a real sample.

Dogfood bug, 2026-11-03: the only runner-minute reduction in a report rested on
ONE sampled run with a retried attempt (2.0 failed minutes), extrapolated to
~8 min/mo and presented as a ranked, "measured, merge-safe" saving. Its
dominant failing job was an eval job that then passed on the retry, which is a
flaky-test signal. SKILL.md admission rule 3 routes such signals to advisory,
as OPT48 is. The formula text read "153/30d ÷ 100 sampled all-status run(s)".

Pinned here:
- engine: fewer than the minimum retried runs, or any retry where the dominant
  job then passed on the same commit, emits an advisory finding with no credit;
- engine: the scaling formula reads in words;
- render: a legacy findings.json (older engine, no stamp) is held to the same
  minimum, so re-rendering saved data never ranks a one-run OPT64;
- render: the Source block says why billable and credited minutes differ;
- verify: a report that ranks OPT64 below the minimum (or on a flaky retry) FAILS.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import test_tier2_wave1_detectors as t2  # noqa: E402  (fixture helpers only)
import test_verify_report_self as vs  # noqa: E402  (fixture helpers only)

_WF = ".github/workflows/ci.yml"


def _retried_run(run_id: str, latest_conclusion: str = "failure") -> tuple:
    """One run retried once after `test` failed. `latest_conclusion` is what
    `test` concluded on the retry (same run, so same commit)."""
    run = t2._run("feature/retry", "abc", "pull_request", run_id=run_id) | {"run_attempt": 2}
    prior = [
        t2._attempt_job("setup", 1, 0, 5, "success", f"{run_id}-setup-1"),
        t2._attempt_job("test", 1, 0, 20, "failure", f"{run_id}-test-1"),
    ]
    latest = [
        t2._attempt_job("setup", 2, 0, 5, "success", f"{run_id}-setup-2"),
        t2._attempt_job("test", 2, 0, 18, latest_conclusion, f"{run_id}-test-2"),
    ]
    return (run, [*prior, *latest], latest)


def _detect(samples, monthly=153, denominator=100):
    return cr._detect_opt64_rerun_attempt_waste(_WF, samples, monthly, denominator, 0)


# ------------------------------- engine --------------------------------------

def test_minimum_is_one_named_constant_shared_by_engine_renderer_and_verifier():
    vr = vs._load_verify_report()
    assert cr._OPT64_MIN_RETRIED_RUNS == 3
    assert bp._OPT64_MIN_RETRIED_RUNS == cr._OPT64_MIN_RETRIED_RUNS
    assert vr._VR_OPT64_MIN_RETRIED_RUNS == cr._OPT64_MIN_RETRIED_RUNS


def test_one_retried_run_is_advisory_with_no_credit():
    out = _detect([_retried_run("r1")])
    assert len(out) == 1
    f = out[0]
    assert f["advisory"] is True
    assert f["runner_min_saving"] is None
    assert "tier2_neutrality" not in f
    assert "rests on 1 retried run" in f["evidence"]
    assert f["rerun_sample"]["retried_runs"] == 1
    assert f["rerun_sample"]["min_retried_runs"] == 3
    assert not bp._is_tier2_finding(f)


def test_two_retried_runs_are_still_below_the_minimum():
    f = _detect([_retried_run("r1"), _retried_run("r2")])[0]
    assert f["advisory"] is True and f["runner_min_saving"] is None
    assert "rests on 2 retried runs" in f["evidence"]


def test_three_deterministic_retries_rank_with_a_readable_formula():
    f = _detect([_retried_run(f"r{i}") for i in range(3)])[0]
    assert not f.get("advisory")
    assert (f["runner_min_saving"] or 0) > 0
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"
    assert f["rerun_sample"] == {
        "retried_runs": 3, "passed_on_retry": 0, "unsettled_on_retry": 0, "min_retried_runs": 3,
        "runs_30d": 153, "sampled_runs": 100}
    text = f["measured_evidence"]["summary"]
    assert "153 runs in 30 days; 100 sampled (any status), of which 3 were retried" in text
    assert "/30d" not in text and "÷" not in text


def test_a_retry_that_then_passed_on_the_same_commit_is_a_flaky_signal():
    # Admission rule 3: even above the minimum, a failure that passed on the
    # retry (same run, so same commit) is a flaky-test signal, not a ranked saving.
    samples = [_retried_run("r1", "success"), _retried_run("r2"), _retried_run("r3")]
    f = _detect(samples)[0]
    assert f["advisory"] is True
    assert f["runner_min_saving"] is None
    assert f["rerun_sample"]["passed_on_retry"] == 1
    assert "1 of 3 retried runs" in f["evidence"]
    assert "passed on the retry" in f["evidence"]


# ------------------------------- render --------------------------------------

def _legacy_opt64(rows: int) -> dict:
    """An OPT64 finding as an older engine wrote it: no `rerun_sample` stamp,
    one evidence-table row per retried run."""
    f = vs._tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    f.pop("rerun_sample", None)
    row = ["`run-1`", "2", "`test`", "25.0"]
    f["measured_evidence"]["table"]["rows"] = [
        [f"`run-{i}`", *row[1:]] for i in range(1, rows + 1)]
    return f


def test_legacy_one_run_opt64_is_not_ranked_and_carries_no_credit(tmp_path: Path):
    doc = vs._tier2_doc_for_verify()
    doc["findings"] = [_legacy_opt64(1)]
    report, _rp, _fp = vs._tier2_artifacts(tmp_path, doc)
    assert "pattern=OPT64 -->" not in report
    assert "100 min/mo" not in report


def test_legacy_three_run_opt64_has_unknown_retry_outcomes(tmp_path: Path):
    doc = vs._tier2_doc_for_verify()
    doc["findings"] = [_legacy_opt64(3)]
    report, _rp, _fp = vs._tier2_artifacts(tmp_path, doc)
    assert "pattern=OPT64 -->" not in report


def test_engine_formula_and_billable_gap_render_readably(tmp_path: Path):
    f = _detect([_retried_run(f"r{i}") for i in range(3)])[0]
    f["id"] = "f-opt64"
    doc = vs._tier2_doc_for_verify()
    doc["findings"] = [f]
    report, _rp, findings_path = vs._tier2_artifacts(tmp_path, doc)
    measured = next(line for line in report.splitlines()
                    if line.startswith("- **What ci-speedup measured:**"))
    assert "153 runs in 30 days; 100 sampled (any status), of which 3 were retried" in measured
    source = next(line for line in report.splitlines()
                  if line.lstrip("- ").startswith("**Source block:**"))
    assert "credited in raw minutes" in source
    assert "the rounded-up figure rounds each job up to a whole minute" in source
    vr = vs._load_verify_report()
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


# ------------------------------- verify --------------------------------------

_CHECK = "OPT64 is ranked only on enough retried runs"


def _check_named(vr, report, report_path, findings_path):
    """The named check out of the verifier's FULL run (run_checks), so the test
    also proves the check is wired in, not merely defined."""
    found = [c for c in vr.run_checks(report, report_path, findings_path, None)
             if c.name == _CHECK]
    assert len(found) == 1, f"verify_report runs no check named {_CHECK!r}"
    return found[0]


def _ranked_then_tampered(tmp_path: Path, mutate):
    """Render a report that legitimately ranks a three-run OPT64, then rewrite
    the findings.json under it. Returns the check (before, after)."""
    vr = vs._load_verify_report()
    doc = vs._tier2_doc_for_verify()
    doc["findings"] = [vs._tier2_promoted_finding(
        "f-opt64", 100.0, proof="post_completion_waste_opt64")]
    report, report_path, findings_path = vs._tier2_artifacts(tmp_path, doc)
    assert "pattern=OPT64 -->" in report
    before = _check_named(vr, report, report_path, findings_path)
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    mutate(data["findings"][0])
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    after = _check_named(vr, report, report_path, findings_path)
    return before, after


def test_verify_fails_a_report_ranking_opt64_on_one_retried_run(tmp_path: Path):
    def one_run(f):
        f["rerun_sample"]["retried_runs"] = 1
        f["measured_evidence"]["table"]["rows"] = f["measured_evidence"]["table"]["rows"][:1]
    before, after = _ranked_then_tampered(tmp_path, one_run)
    assert before.ok and not before.skipped, before
    assert not after.ok and not after.skipped, after
    assert "f-opt64" in after.detail and "1 retried run" in after.detail


def test_verify_fails_a_report_ranking_opt64_on_a_flaky_retry(tmp_path: Path):
    def flaky(f):
        f["rerun_sample"] = {"retried_runs": 3, "passed_on_retry": 1, "unsettled_on_retry": 0, "min_retried_runs": 3}
    before, after = _ranked_then_tampered(tmp_path, flaky)
    assert before.ok and not before.skipped, before
    assert not after.ok and not after.skipped, after
    assert "passed on the retry" in after.detail


def test_verify_check_reads_the_stamp_before_the_table(tmp_path: Path):
    # The table is capped at six rows; the stamp is the exact count.
    vr = vs._load_verify_report()
    f = copy.deepcopy(_legacy_opt64(3))
    f["rerun_sample"] = {"retried_runs": 9, "passed_on_retry": 0, "unsettled_on_retry": 0, "min_retried_runs": 3}
    assert vr._vr_opt64_hold_reason(f) is None
    f["rerun_sample"]["retried_runs"] = 2
    assert "2 retried run" in (vr._vr_opt64_hold_reason(f) or "")


# --------------------------- review hardening --------------------------------

def test_a_retry_still_running_is_not_counted_as_failing_again():
    # Two retries that failed again plus one whose `test` has no conclusion yet
    # (the collector samples running retries): the third may still pass, so
    # nothing proves the job failed on every retry. Advisory, no credit.
    samples = [_retried_run("r1"), _retried_run("r2"), _retried_run("r3", None)]
    f = _detect(samples)[0]
    assert f["advisory"] is True
    assert f["runner_min_saving"] is None
    assert f["rerun_sample"]["unsettled_on_retry"] == 1
    assert "had not finished" in f["evidence"]


def test_verify_group_cover_ignores_a_held_sibling(tmp_path: Path):
    # Synthetic siblings on one workflow: an eligible 80-min row and a one-retry
    # 40-min row against 100 prior-attempt source minutes. The renderer holds
    # the 40-min sibling and promotes the 80; the verifier must agree.
    vr = vs._load_verify_report()
    doc = vs._tier2_doc_for_verify()
    ok = vs._opt64_finding("f-opt64a", 80.0, "test", line=31)
    held = vs._opt64_finding("f-opt64b", 40.0, "test", line=32)
    held["rerun_sample"]["retried_runs"] = 1
    held["measured_evidence"]["table"]["rows"] = held["measured_evidence"]["table"]["rows"][:1]
    doc["findings"] = [ok, held]
    vs._set_tier2_spine_rows(doc, [vs._tier2_spine_row(
        doc, job="test", status="all-status", attempt="prior", raw=100.0, billable=120.0)])
    report, _rp, findings_path = vs._tier2_artifacts(tmp_path, doc)
    assert "id=f-opt64a pattern=OPT64 -->" in report
    assert "id=f-opt64b" not in report
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_unknown_retry_outcomes_never_rank(tmp_path):
    vr = vs._load_verify_report()
    variants = [None, {'retried_runs': 3},
                {'retried_runs': 3, 'passed_on_retry': 0},
                {'retried_runs': 3, 'passed_on_retry': -1, 'unsettled_on_retry': 0},
                {'retried_runs': 3, 'passed_on_retry': 0, 'unsettled_on_retry': 'unknown'}]
    for stamp in variants:
        f = _legacy_opt64(3)
        if stamp is not None:
            f['rerun_sample'] = stamp
        assert bp._opt64_held(f) is not None, stamp
        assert vr._vr_opt64_hold_reason(f) is not None, stamp
        doc = vs._tier2_doc_for_verify()
        doc['findings'] = [f]
        assert 'pattern=OPT64 -->' not in bp.render(doc)


def test_carried_forward_failure_does_not_prove_a_failed_retry():
    samples = [_retried_run(f'r{i}') for i in range(3)]
    for run, all_jobs, latest in samples:
        prior_test = next(j for j in all_jobs if j['name'] == 'test' and j['run_attempt'] == 1)
        latest[:] = [j for j in latest if j['name'] != 'test'] + [prior_test]
        all_jobs[:] = [j for j in all_jobs if not (j['name'] == 'test' and j['run_attempt'] == 2)]
    f = _detect(samples)[0]
    assert f.get('advisory') is True
    assert f['runner_min_saving'] is None


def test_carried_forward_successful_jobs_do_not_inflate_retry_savings():
    samples = [_retried_run(f'r{i}') for i in range(3)]
    expected_prior_s = 0.0
    for run, all_jobs, latest in samples:
        prior_setup = next(j for j in all_jobs if j['name'] == 'setup' and j['run_attempt'] == 1)
        latest[:] = [j for j in latest if j['name'] != 'setup'] + [prior_setup]
        all_jobs[:] = [j for j in all_jobs if not (j['name'] == 'setup' and j['run_attempt'] == 2)]
        expected_prior_s += sum(cr._job_compute_s(j) for j in all_jobs
                                if j['name'] == 'test' and j['run_attempt'] == 1)
    f = _detect(samples, monthly=100, denominator=100)[0]
    assert not f.get('advisory')
    assert f['runner_min_saving'] == round(expected_prior_s / 60.0, 1)
