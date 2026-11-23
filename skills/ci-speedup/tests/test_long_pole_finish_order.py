"""The long pole is the check each PR actually waits on: the one that FINISHES LAST.

Owner decision (2026-11-23, issues #363/#364/#365): every merge-gating check competes on each
sampled PR, displayed or not; each run counts at its measured job time (start to finish, no
queue) plus its start offset; only true outliers are clipped (a run above its check's own P95,
and a re-run that started after every first-attempt check had finished). No p50 cap.

The regression fixture below reproduces the shape found on a public repository: a short
collecting job `needs:` a long one and finishes last, while an independent job has the longest
span. The PR waits on the long job the collector waited on, not on the longest span.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_VR_SPEC = importlib.util.spec_from_file_location(
    "long_pole_finish_order_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_CI = ".github/workflows/ci.yml"
_LINT = ".github/workflows/lint.yml"
# The made-up shape: `setup` (4 min) -> `unit` (9 min) -> `merge-reports` (20 s, also needs
# `lint`), with an independent `lint` (11 min) in another workflow starting at once. `lint` has
# the longest span, but `unit` ends at 13 min and `merge-reports` (which waits on it) at 13m20s.
_TIMELINE = {
    "setup": (0, 240),
    "unit": (240, 780),
    "merge-reports": (780, 800),
    "lint": (0, 660),
}


def _iso(seconds: int) -> str:
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _job(name: str, run_id: int) -> dict:
    start, end = _TIMELINE[name]
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "html_url": f"https://example.invalid/runs/{run_id}",
        "started_at": _iso(start),
        "completed_at": _iso(end),
        "steps": [{"name": "work", "started_at": _iso(start), "completed_at": _iso(end)}],
    }


class _CollectingSinkClient:
    """Eight PRs of the fixture shape, served through the ordinary gh seams."""

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
        wall = 800 if workflow_id == 1 else 660
        base = 100 if workflow_id == 1 else 200
        return [{
            "id": base + i, "event": "pull_request", "head_sha": sha,
            "status": "completed", "conclusion": "success",
            "created_at": _iso(0), "run_started_at": _iso(0), "updated_at": _iso(wall),
        } for i, sha in enumerate(self.shas)]

    def _jobs(self, run_id: int) -> list[dict]:
        if 100 <= run_id < 200:
            return [_job(name, run_id) for name in ("setup", "unit", "merge-reports")]
        if 200 <= run_id < 300:
            return [_job("lint", run_id)]
        return []

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _CI, "name": "ci"},
                                  {"id": 2, "path": _LINT, "name": "lint"}]}
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
                {"name": name, "started_at": _iso(start), "completed_at": _iso(end)}
                for name, (start, end) in _TIMELINE.items()]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": []}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


_DOC = {
    "repo": "o/r",
    "workflow_job_graph": {
        _CI: {
            "setup": {"name": "setup", "needs": []},
            "unit": {"name": "unit", "needs": ["setup"]},
            "merge-reports": {"name": "merge-reports", "needs": ["unit", "setup"]},
        },
        _LINT: {"lint": {"name": "lint", "needs": []}},
    },
    "findings": [{"id": "ci", "workflow_file": _CI}, {"id": "lint", "workflow_file": _LINT}],
}


def _collect(monkeypatch) -> dict:
    monkeypatch.setattr(cr, "GhClient", _CollectingSinkClient)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=8, shallow_runs=8)
    doc["skill_commit_sha"] = "installed:unversioned"
    return doc


def test_a_short_collecting_job_credits_the_long_job_it_waited_on(monkeypatch, tmp_path):
    """The regression: the PR waits on `unit` (it finishes after `lint`, and `merge-reports`
    only starts once it ends), so `unit` finishes last on every PR and leads the long poles.
    Ranking by the longest span crowned `lint`, which finishes 2 minutes earlier."""
    doc = _collect(monkeypatch)
    cp = doc["pr_critical_path"]
    pole_n = {row["name"]: row["pole_n"] for row in cp["checks"]}

    assert cp["critical_path_check"] == "unit", (cp["critical_path_check"], pole_n)
    assert pole_n["unit"] == 8 and pole_n["lint"] == 0, pole_n
    # The 20 s collector waits on `unit`; it is kept off the basis rather than credited.
    assert "merge-reports" not in pole_n

    report = bp.render(doc, captured_at="2026-11-23T00:00:00Z")
    assert re.search(r"\[unit\]\(#pole-1\)[^\n]*finishes last on 8/8 PRs", report), report[:3000]
    assert re.search(r"\[lint\]\(#pole-\d\)[^\n]*finishes last on 0/8 PRs", report)

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


def test_the_verifier_rejects_a_pole_count_the_basis_does_not_give(monkeypatch, tmp_path):
    """A `pole_n` restamped onto the longest span (the retired rule) no longer re-derives."""
    doc = _collect(monkeypatch)
    for row in doc["pr_critical_path"]["checks"]:
        row["pole_n"] = 8 if row["name"] == "lint" else 0
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = vr.check_pole_counts_match_the_pole_basis("", path)
    assert not res.ok and "`lint` pole_n=8 but the pole basis gives 0" in res.detail, res.detail


def _row(spans: dict, offsets: dict, retried: dict | None = None) -> dict:
    row = {"spans_s": dict(spans), "offsets_s": dict(offsets)}
    if retried:
        row["retried"] = dict(retried)
    return row


def test_measured_time_is_not_capped_at_a_p50():
    """Each run competes at its measured time: a check that took 900 s on every PR finishes last
    on every PR even when its job p50 elsewhere was 200 s (the retired cap made it lose)."""
    rows = [_row({"light": 900.0, "heavy": 300.0}, {"light": 0.0, "heavy": 0.0})] * 6
    assert cr._pole_frequencies([], {"light", "heavy"}, pole_basis=rows) == {
        "light": 6, "heavy": 0}


def test_only_a_run_above_its_checks_p95_is_clipped():
    """`a` ran 100 s on 19 PRs and 5000 s once. Its P95 over the 20 runs is 345 s, so on that
    PR it counts at 345 s and the steady 400 s `b` finishes last; the clip touches the outlier."""
    rows = ([_row({"a": 100.0, "b": 400.0}, {"a": 0.0, "b": 0.0})] * 19
            + [_row({"a": 5000.0, "b": 400.0}, {"a": 0.0, "b": 0.0})])
    assert cr._pole_basis_winners(rows) == [["b"]] * 20
    # A check with a real slow mode keeps it: 6 of 20 runs at 900 s sit below its P95.
    slow = ([_row({"a": 900.0, "b": 400.0}, {})] * 6 + [_row({"a": 100.0, "b": 400.0}, {})] * 14)
    assert cr._pole_basis_winners(slow)[:6] == [["a"]] * 6


def test_start_offsets_decide_who_finishes_last():
    """A job that `needs:` another starts when it ends, so the shorter dependant can finish last."""
    rows = [_row({"unit": 540.0, "lint": 660.0}, {"unit": 240.0, "lint": 0.0})] * 6
    assert cr._pole_frequencies([], {"unit", "lint"}, pole_basis=rows) == {"unit": 6, "lint": 0}
    # Ties on the 0.1 s-rounded finish credit every co-last check.
    tied = [_row({"unit": 420.0, "lint": 660.0}, {"unit": 240.0, "lint": 0.0})] * 6
    assert cr._pole_frequencies([], {"unit", "lint"}, pole_basis=tied) == {"unit": 6, "lint": 6}


def test_a_late_re_run_sits_out_but_a_retry_that_ran_alongside_competes():
    spans = {"unit": 300.0, "lint": 120.0}
    # `lint` was re-run (attempt 2) an hour after `unit` finished: its offset measures the re-run.
    late = _row(spans, {"unit": 0.0, "lint": 3600.0}, {"lint": 2})
    assert cr._late_reruns(late) == {"lint"}
    assert cr._pole_basis_winners([late]) == [["unit"]]
    # A retry that started while `unit` was still running ran with the PR's other checks.
    overlap = _row(spans, {"unit": 0.0, "lint": 250.0}, {"lint": 2})
    assert cr._late_reruns(overlap) == set()
    assert cr._pole_basis_winners([overlap]) == [["lint"]]
    # When the whole PR re-ran there is no first attempt to compare with: nothing sits out.
    whole = _row(spans, {"unit": 0.0, "lint": 0.0}, {"unit": 2, "lint": 2})
    assert cr._late_reruns(whole) == set()


def test_pole_basis_rows_read_measured_intervals_and_attempts():
    per_sha = [{"unit": 999.0, "lint": 120.0, "other": 50.0}]
    intervals = [{"unit": (_iso(60), _iso(360)), "lint": (_iso(0), _iso(120))}]
    attempts = [{"unit": 1, "lint": 3}]
    rows = cr._pole_basis_rows(per_sha, {"unit", "lint"}, intervals, attempts)
    # `unit` is measured from its own interval (300 s), not the 999 s map value; `other` is not
    # a candidate; offsets are rebased on the earliest start; `lint` came from attempt 3.
    assert rows == [{"spans_s": {"unit": 300.0, "lint": 120.0},
                     "offsets_s": {"unit": 60.0, "lint": 0.0}, "retried": {"lint": 3}}]


def test_an_unconfirmed_check_keeps_its_own_wins():
    """A check the spine cannot confirm as merge-blocking still competes: the PRs it finishes last
    on are its own, never handed to the next confirmed check (the crown still goes to a
    confirmed check; `test_required_crown_collect` covers that end to end)."""
    p50 = {"unit": 300.0, "mystery": 500.0}
    per_sha = [dict(p50) for _ in range(8)]
    _order, _present, _n, pole_freq = cr._rank_spine_present_first(
        p50, per_sha, frozenset(), confirmed_gate_checks=frozenset({"unit"}))
    assert pole_freq == {"mystery": 8, "unit": 0}


def test_engine_renderer_and_verifier_copies_stay_identical():
    """The collector, the renderer and the verifier each carry the finish-order rule; pin them."""
    rng = random.Random(23)
    assert cr._POLE_BASIS_RULE == bp._POLE_BASIS_RULE == vr._VR_POLE_BASIS_RULE
    for _ in range(300):
        names = [f"c{i}" for i in range(rng.randint(1, 5))]
        spans = {nm: float(rng.choice([0, 3, 20.5, 60, 120, 720])) for nm in names}
        offs = {nm: float(rng.choice([0, 10, 700.1, 720])) for nm in names if rng.random() < 0.7}
        assert (cr._finish_times(spans, offs) == bp._finish_times(spans, offs)
                == vr._vr_finish_times(spans, offs))
        vals = [float(rng.choice([1, 5, 60, 61.5, 900])) for _ in range(rng.randint(0, 25))]
        pct = rng.choice([50, 90, 95, 99])
        assert (cr._percentile(vals, pct) == bp._pole_percentile(vals, pct)
                == vr._vr_pole_percentile(vals, pct))
        rows = []
        for _ in range(rng.randint(1, 12)):
            row_spans = {nm: float(rng.choice([1, 30, 120, 600, 5000])) for nm in names
                         if rng.random() < 0.85}
            if not row_spans:
                continue
            rows.append(_row(
                row_spans,
                {nm: float(rng.choice([0, 0, 60, 700, 4000])) for nm in row_spans
                 if rng.random() < 0.8},
                {nm: 2 for nm in row_spans if rng.random() < 0.2}))
        assert (cr._pole_basis_winners(rows) == bp._pole_basis_winners(rows)
                == vr._vr_pole_basis_winners(rows))
        for row in rows:
            assert cr._late_reruns(row) == bp._late_reruns(row) == vr._vr_late_reruns(row)


def test_rendered_counts_and_their_verifier_read_the_stamped_basis():
    """The displayed "finishes last on N/M" and its verifier re-derivation read the stamped basis
    (a tie credits one check there, so workflow sums never double-count a PR)."""
    rows = ([_row({"unit": 540.0, "lint": 660.0}, {"unit": 240.0, "lint": 0.0})] * 5
            + [_row({"unit": 420.0, "lint": 660.0}, {"unit": 240.0, "lint": 0.0})])
    cp = {"pole_basis": {"rule": cr._POLE_BASIS_RULE, "per_pr": rows},
          "checks": [{"name": "unit", "required_reachable": False}, {"name": "lint"}],
          "populations": [[1.0, [["lint", 660.0], ["unit", 540.0]]]]}
    pole_count, present, n = bp._gate_counts(cp)
    assert (pole_count, present, n) == ({"unit": 6}, {"unit": 6, "lint": 6}, 6)
    assert vr._populations_pole_counts(cp) == ({"unit": 6}, 6, 0)
    # Findings without the stamped basis keep the populations rule.
    legacy = {k: v for k, v in cp.items() if k != "pole_basis"}
    assert bp._gate_counts(legacy)[0] == {"lint": 1}
    assert vr._populations_pole_counts(legacy)[0] == {"lint": 1}


def test_a_mid_name_matrix_leg_floor_is_a_sibling_not_a_silent_drop(tmp_path):
    """Binding-floor disclosure: `test prod (6/10) / build` beside the drilled pole
    `test prod (8/10) / build` in the same workflow is a matrix sibling leg (the renderer folds
    them with `_same_matrix`), so the check counts it as a sibling-leg floor, not an undisclosed
    heavy check."""
    wf = ".github/workflows/build.yml"
    pole, leg = "test prod (8/10) / build", "test prod (6/10) / build"
    n = 8
    findings = {"pr_critical_path": {
        "checks": [{"name": leg, "p50_s": 823.0, "workflow_file": wf},
                   {"name": pole, "p50_s": 737.0, "workflow_file": wf}],
        "populations": [[round(1 / n, 4), [[pole, 900.0], [leg, 800.0]]]] * n,
    }}
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(findings), encoding="utf-8")
    report = (f"## 🔴 Long pole 1: `build.yml` ▸ `{pole}` - 12m 17s\n\nbody\n")
    res = vr.check_spine_heavy_check_disclosed(report, path)
    assert res.ok, res.detail
    assert "1 matrix sibling-leg floor" in res.detail, res.detail
