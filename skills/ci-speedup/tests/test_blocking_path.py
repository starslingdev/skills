"""Tests for `blocking_path.py` - the data-first ASCII blocking-path renderer.

The leaf detectors in `_parse_log` are pure regex-over-tool-output functions:
the single most likely thing to break silently when a tool tweaks its log format,
and (before this) the least covered. Each test feeds a representative log snippet
and locks the classification + the rendered shape, so a format drift fails loudly
here instead of silently dropping a root cause from the report.

Run: pytest -v skills/ci-speedup/tests/test_blocking_path.py
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402  (uniquely-named module; no cross-skill clash)


def _tier2_doc() -> dict:
    doc = {
        "repo": "demo/repo",
        "repo_visibility": "private",
        "scanned_at": "2026-07-04T00:00:00Z",
        "skill_commit_sha": "7039302",
        "commit_sha": "abcdef1234567890",
        "pr_critical_path": {
            "critical_path_check": "build",
            "sampled_pr_count": 4,
            "sample_target": 4,
            "check_present_n_pr": 4,
            "checks": [{"name": "build", "workflow_file": ".github/workflows/ci.yml",
                        "p50_s": 300.0, "present_on": 4, "pole_n": 4}],
            "poles": [{"check": "build", "job": "build",
                       "workflow_file": ".github/workflows/ci.yml", "p50_s": 300.0}],
        },
        "findings": [
            {
                "id": "f-promoted",
                "pattern": "OPT46",
                "severity": "MEDIUM",
                "title": "Superseded runs never cancelled",
                "workflow_file": ".github/workflows/ci.yml",
                "line": 12,
                "affected_jobs": ["cleanup"],
                "evidence": "3 overlapping runs in the sampled branch window",
                "runner_min_saving": 120.0,
                "runner_min_range_s": [100.0, 150.0],
                "wall_clock_p50_s": 0.0,
                "realization": "none",
                "sizing_basis": "measured",
                "measured_signal": ("remainder-weighted superseded runs x mean job-minutes "
                                    "(3 confirmed, 90% mean remainder; naive 5; 4 timed run(s); scale 1)"),
                "tier2_neutrality": {
                    "proof": "post_completion_waste",
                    "margin_s": None,
                    "ref": "superseded runs finish after a newer run starts",
                },
                "measured_evidence": {
                    "summary": "3 overlapping runs confirmed by timestamp overlap; credited on the remainder basis",
                    "table": {
                        "headers": ["Workflow", "Overlapping runs", "Mean compute/run"],
                        "rows": [["`ci.yml`", "3 confirmed", "40.0 job-min"]],
                    },
                    "note": "Cancellation cause is inference; verify this is not a deploy.",
                },
                "fix_recipe_anchor": "opt46--superseded-runs",
            },
            {
                "id": "f-residual",
                "pattern": "OPT46",
                "severity": "LOW",
                "title": "Superseded runs never cancelled",
                "workflow_file": ".github/workflows/slow.yml",
                "line": 9,
                "affected_jobs": [],
                "evidence": "modeled legacy occurrence",
                "runner_min_saving": 8.0,
                "wall_clock_p50_s": 0.0,
                "sizing_basis": "modeled",
                "fix_recipe_anchor": "opt46--superseded-runs",
            },
        ],
    }
    wf = ".github/workflows/ci.yml"
    jobs = [[_spine_job("cleanup", 75, "2026-06-01T00:00:00Z")]]
    doc["data_sources"] = {"cost_spine_job_fetch_failures": 0}
    doc["per_workflow_monthly_volume"] = {wf: 100}
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": jobs}}, {}, {wf: 100}, "private",
        workflows_in_play={wf})
    assert spine is not None
    assert spine["render_ready"] is True
    doc["runner_minute_spine"] = spine
    return doc


def _spine_job(name: str, seconds: int, run_created: str) -> dict:
    start = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(seconds=seconds)
    return {
        "name": name,
        "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "conclusion": "success",
        "labels": ["ubuntu-latest"],
        "_run_created_at": run_created,
    }


def _render_ready_spine_doc(*, row_count: int = 2) -> dict:
    doc = _doc_one_pole()
    doc["repo"] = "demo/repo"
    doc["repo_visibility"] = "private"
    doc["commit_sha"] = "abcdef1234567890"
    doc["skill_commit_sha"] = "7039302"
    doc["data_sources"]["cost_spine_job_fetch_failures"] = 0
    wf = ".github/workflows/pipeline.yml"
    jobs = [
        _spine_job(f"job-{i:02d}", 60 + i, "2026-06-01T00:00:00Z")
        for i in range(row_count)
    ]
    doc["per_workflow_monthly_volume"] = {wf: 10}
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": [jobs]}}, {}, {wf: 10}, "private",
        workflows_in_play={wf})
    assert spine is not None
    assert spine["render_ready"] is True
    doc["runner_minute_spine"] = spine
    return doc


def test_tier2_section_promotes_measured_certified_findings_before_appendix():
    report = bp.render(_tier2_doc())
    assert "## Runner-minute reductions (wall-clock-neutral)" in report
    assert report.index("## Runner-minute reductions") < report.index("## 🧹 Also noticed")
    assert "<!-- ci-speedup:tier2-finding id=f-promoted pattern=OPT46 -->" in report
    # Long-pole format parity (owner directive, 2026-07-09): `##` header with the
    # 💸 marker, workflow + magnitude in the pole shape, a bold role lead, an OPEN
    # body (no <details> wrapper), and the pattern name as a bold body line.
    assert re.search(r"^## 🟢 Runner saving 1: `ci\.yml:12` \(cleanup\) - "
                     r"120 min/mo$", report, re.MULTILINE)
    assert "**The largest merge-safe runner-minute saving measured on this repo.**" in report
    # Contents row: 🟢 in the pole rows' severity-dot slot, short label, link to #r-1.
    assert re.search(r"^1\. 🟢 \[Superseded runs never cancelled\]\(#r-1\) - 120 min/mo",
                     report, re.MULTILINE)
    assert "**💸 Bill root-cause - OPT46 · Superseded runs never cancelled**" in report
    tier2_region = report.split("<!-- ci-speedup:tier2-finding", 1)[1]
    tier2_region = tier2_region.split("## 🧹 Also noticed", 1)[0]
    assert "<details>" not in tier2_region
    assert "machine-derived proof: `post_completion_waste`" in report
    assert ("- **Source block:** `runner_minute_spine` matched 1 row for "
            "`.github/workflows/ci.yml`") in report
    appendix = report.split("## 🧹 Also noticed", 1)[1]
    assert "Tier-2 note:" in appendix
    assert "measured wall-clock-neutral instances of this same pattern are promoted above" in appendix
    assert "f-promoted" not in appendix


def test_tier2_short_title_strips_parenthetical_and_caps():
    # The requests-report case: trailing parenthetical qualifier stripped whole.
    assert bp._tier2_short_title({"title": (
        "Superseded Runs Not Cancelled (Missing Concurrency or "
        "`cancel-in-progress: false`)")}) == "Superseded Runs Not Cancelled"
    # Under the cap and paren-free: unchanged.
    assert bp._tier2_short_title({"title": "Cron Schedule Too Frequent"}) == (
        "Cron Schedule Too Frequent")
    # Over the cap: word-boundary cut + ellipsis, never a mid-word chop.
    long_title = "Flaky Test Retry Waste Across Cross SKU Reruns Of The Whole Matrix"
    short = bp._tier2_short_title({"title": long_title})
    assert short.endswith("…") and len(short) <= bp._TIER2_SHORT_TITLE_MAX + 1
    stem = short[:-1]
    assert long_title.startswith(stem)                 # a true prefix ...
    assert long_title[len(stem)] == " "                # ... cut exactly at a space
    # All-parenthetical degenerate title: falls back to the original, not "".
    assert bp._tier2_short_title({"title": "(only a qualifier)"}) == "(only a qualifier)"
    # No title: falls back to the pattern id.
    assert bp._tier2_short_title({"pattern": "OPT46"}) == "OPT46"


def test_tier2_source_unbacked_candidates_fall_back_to_appendix():
    doc = _tier2_doc()
    doc["runner_minute_spine"]["rows"][0]["job_name"] = "other"

    report = bp.render(doc)

    assert "<!-- ci-speedup:tier2-finding id=f-promoted pattern=OPT46 -->" not in report
    appendix = report.split("## 🧹 Also noticed", 1)[1]
    # PR-Z: appendix labels use the Tier-2 positive-saving convention.
    assert "128 min/mo" in appendix
    assert "-128 min/mo" not in appendix
    assert "did not have matching render-ready `runner_minute_spine` source rows" in appendix


def test_tier2_claims_manifest_records_headline_lead_and_cert():
    report = bp.render(_tier2_doc())
    manifest = bp._LAST_CLAIMS.to_json()
    kinds = [c["kind"] for c in manifest["claims"]]
    assert "runner_minutes" in manifest["families_migrated"]
    assert "tier2_headline" in kinds
    assert "tier2_section_lead" in kinds
    assert "tier2_neutrality_line" in kinds
    assert any(c["kind"] == "tier2_headline" and c["subject"] == "f-promoted"
               for c in manifest["claims"])
    assert "wall-clock-neutral runner spend" in report


def test_runner_minute_spine_renderer_round_trips_against_verifier(tmp_path: Path):
    doc = _render_ready_spine_doc(row_count=2)
    report = bp.render(doc)

    assert "## Runner-minute cost spine" in report
    assert "**Runner-minute cost spine**" in report
    assert "### Cost spine: where runner minutes go" in report
    assert "<!-- ci-speedup:runner-minute-spine -->" in report
    # The single minutes-only cost-spine shape (pricing excised 2026-07-20): no
    # SKU/Billing/Weighted/USD columns, just the runner-minute surface.
    assert ("| Workflow | Job | Runner | Event | Status | Attempt | Volume "
            "| Raw min/mo | Billable min/mo | Share |") in report
    assert "| Total |" in report
    # The one-sentence pricing story leads the table; no dollar column anywhere.
    assert ("All figures are runner-minutes; multiply by your runner's per-minute "
            "rate to get dollars.") in report
    assert "USD/mo" not in report

    report_path = tmp_path / "blocking-path-speed.md"
    findings_path = tmp_path / "findings.json"
    report_path.write_text(report, encoding="utf-8")
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    vr = _load_verify_report()
    chk = vr.check_runner_minute_spine_contract(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_renderer_discloses_hidden_rows(tmp_path: Path):
    doc = _render_ready_spine_doc(row_count=13)
    report = bp.render(doc)

    assert "+1 more runner-minute row hidden" in report

    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    vr = _load_verify_report()
    chk = vr.check_runner_minute_spine_contract(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_renderer_skips_source_block_until_render_ready():
    doc = _render_ready_spine_doc(row_count=1)
    spine = doc["runner_minute_spine"]
    spine["render_ready"] = False
    spine["render_blocker"] = "coverage still incomplete"

    report = bp.render(doc)

    assert "<!-- ci-speedup:runner-minute-spine -->" not in report
    assert "### Cost spine: where runner minutes go" not in report


# --------------------------------------------------------------------------- #
# Class A #4 — the addressable ceiling floors against the pole's OWN gating-PR
# co-occurrence, not global presence (so a co-occurring 2nd-slowest sibling that
# is globally slightly rarer can't be demoted, overstating the ceiling ~2×).
# --------------------------------------------------------------------------- #
def test_binding_floor_uses_pole_gating_cooccurrence_not_global_presence():
    # `slow` (800s) co-occurs with the pole on 13/13 of the pole's gating PRs but is globally
    # present on 15/20; the trivial `fast` (100s) is on all 20. The LEGACY global test
    # (0.8 × 20 = 16) demotes `slow` (15 < 16) and wrongly names `fast` the floor; per-pole
    # co-occurrence keeps `slow` (13/13 > 50%) — the real floor.
    candidates = [
        {"name": "pole", "p50_s": 1000.0, "workflow_file": "a.yml", "present_on": 15},
        {"name": "slow", "p50_s": 800.0, "workflow_file": "b.yml", "present_on": 15},
        {"name": "fast", "p50_s": 100.0, "workflow_file": "c.yml", "present_on": 20},
    ]
    others = [c for c in candidates if c["name"] != "pole"]
    cooccur, gating_n = {"slow": 13, "fast": 13}, 13
    # With co-occurrence: the floor is the slower co-occurring sibling.
    assert bp._binding_floor(others, cooccur, gating_n)["name"] == "slow"
    assert bp._floor_candidate(others, cooccur, gating_n)["name"] == "slow"
    pole = {"check": "pole", "p50_s": 1000.0, "_cooccur": cooccur, "_gating_n": gating_n}
    assert abs(bp._pole_addressable(pole, candidates) - 200.0) < 1.0   # 1000 - 800
    # Legacy fallback (no co-occurrence data): the global 0.8-presence cutoff demotes `slow`
    # (15/20) and crowns `fast` — the exact overstatement the fix removes. Pins the regression.
    assert bp._binding_floor(others)["name"] == "fast"
    pole_legacy = {"check": "pole", "p50_s": 1000.0}
    assert abs(bp._pole_addressable(pole_legacy, candidates) - 900.0) < 1.0   # 1000 - 100 (overstated)


def test_pole_cooccurrence_counts_only_the_poles_gating_prs():
    # Re-derives the pole's gating PRs (where it is the per-PR slowest) and per-check co-occurrence
    # from `populations`. `B` is slowest on 2 PRs (its own gate), `A` on 3; on A's 3 gating PRs the
    # sibling `S` co-occurs twice. So for pole A: gating_n == 3 and S's count == 2.
    cp = {"populations": [
        [0.1, [["A", 100.0], ["S", 60.0]]],
        [0.1, [["A", 100.0], ["S", 60.0]]],
        [0.1, [["A", 100.0], ["X", 10.0]]],          # A gates, S absent
        [0.1, [["B", 90.0], ["A", 50.0]]],           # B gates, not A
        [0.1, [["B", 90.0], ["A", 50.0]]],
    ]}
    cooccur, gating_n = bp._pole_cooccurrence(cp, "A")
    assert gating_n == 3
    assert cooccur.get("s") == 2
    assert bp._pole_cooccurrence(cp, "nope") == ({}, 0)   # never a winner → empty, callers fall back


def test_floor_candidate_raw_fallback_when_checks_lack_workflow_file():
    # The better-auth shape: populations-rich (so cooccur/gating_n are non-empty) but the `checks`
    # carry NO `workflow_file`. `_floor_candidate` must still NAME a floor via the raw fallback —
    # co-occurrence is not file metadata and must NOT disable that fallback — so `_pole_addressable`
    # doesn't zero the win. Pins the `has_meta` rework (regression: the win silently floored to 0).
    candidates = [{"name": "pole", "p50_s": 600.0}, {"name": "floor", "p50_s": 400.0}]
    others = [c for c in candidates if c["name"] != "pole"]
    cooccur, gating_n = {"floor": 18}, 18
    fc = bp._floor_candidate(others, cooccur, gating_n)
    assert fc is not None and fc["name"] == "floor"     # raw fallback still names a floor
    pole = {"check": "pole", "p50_s": 600.0, "_cooccur": cooccur, "_gating_n": gating_n}
    assert abs(bp._pole_addressable(pole, candidates) - 200.0) < 1.0   # 600 - 400, not zeroed


def test_floor_qualifies_strict_majority_boundary():
    # The strict-majority crux at the engine: a sibling co-occurring on EXACTLY half the gating PRs
    # does NOT qualify; one more does. (Mirrors the verifier's `count <= majority` skip.)
    c = {"name": "x", "p50_s": 1.0}
    assert bp._floor_qualifies(c, {"x": 3}, 6, denom=0) is False   # 3/6 — not a majority
    assert bp._floor_qualifies(c, {"x": 4}, 6, denom=0) is True    # 4/6 — a majority


def test_render_toc_reframes_when_also_noticed_has_on_path_lever():
    # Class A #7: when the "Also noticed" appendix holds a credited wall-clock lever that sits ON the
    # critical path, the TOC pointer must reframe — not blanket-label the section "below the critical
    # path / ~0 wall-clock". OPT24 (120s wall-clock, on a NON-pole job `slow-test`) lands in the
    # appendix flagged on-path; the pointer must acknowledge it.
    checks = [
        {"name": "build", "p50_s": 1000.0, "present_on": 10, "workflow_file": "ci.yml"},
        {"name": "slow-test", "p50_s": 500.0, "present_on": 10, "workflow_file": "other.yml"},
    ]
    pops = [[0.1, [["build", 1000.0], ["slow-test", 500.0]]] for _ in range(10)]
    doc = {"pr_critical_path": {
        "critical_path_check": "build", "critical_path_s": 1000.0,
        "checks": checks, "check_present_n_pr": 10, "populations": pops,
        "poles": [{"check": "build", "p50_s": 1000.0, "workflow_file": "ci.yml", "job": "build"}],
    }, "findings": [
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job",
         "workflow_file": "other.yml", "affected_jobs": ["slow-test"],
         "runner_min_saving": 0, "wall_clock_p50_s": 120.0},
    ]}
    out = bp.render(doc)
    assert "sits ON the merge-gating critical path" in out, "the credited wall-clock lever should flag on-path"
    m = re.search(r"\*\*🧹 Also noticed\*\* -.*", out)
    assert m, "TOC 'Also noticed' pointer should render"
    assert "below the critical path" not in m.group(0), "pointer must not blanket-label off-path"
    assert "DO sit on the critical path" in m.group(0), "pointer must acknowledge the on-path lever"


def test_render_does_not_frame_rare_demoted_job_on_path_in_appendix():
    # Regression (paradedb `Test pg_search`): a heavy job the spine DEMOTES as opt-in/rare
    # (present on a MINORITY of PRs — 8/20 here) keeps a large `wall_clock_p50_s` (the cascade
    # bounds only against CONCURRENT checks, not the presence demotion) and is NOT off_spine
    # (it IS PR-gating, just rare). Its OPT24 must NOT be framed "sits ON the merge-gating
    # critical path" in "Also noticed" while the level-1 footnote demotes the SAME job as
    # opt-in. `_saves_wall_clock` must decline it via the `spine_rare` stamp.
    checks = [
        {"name": "build", "p50_s": 1000.0, "present_on": 20, "workflow_file": "ci.yml"},
        {"name": "Test pg_search on PostgreSQL 18 (pgrx - arm64)", "p50_s": 1351.0,
         "present_on": 8, "workflow_file": "test-pg_search.yml"},
    ]
    # 12 PRs run only `build`; 8 run build + pg_search (pg_search slowest on those). No pole_n
    # stamped, so the legacy presence rule fires: pg_search present 8/20 <= 0.5 → rare/opt-in.
    pops = ([[0.05, [["build", 1000.0]]] for _ in range(12)]
            + [[0.05, [["build", 1000.0],
                       ["Test pg_search on PostgreSQL 18 (pgrx - arm64)", 1351.0]]]
               for _ in range(8)])
    doc = {"pr_critical_path": {
        "critical_path_check": "build", "critical_path_s": 1000.0,
        "checks": checks, "check_present_n_pr": 20, "populations": pops,
        "poles": [{"check": "build", "p50_s": 1000.0, "workflow_file": "ci.yml", "job": "build"}],
    }, "findings": [
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH",
         "title": "Long Test Job Without Sharding", "line": 12,
         "workflow_file": "test-pg_search.yml",
         "affected_jobs": ["Test pg_search on PostgreSQL 18 (pgrx - arm64)"],
         "runner_min_saving": 0, "wall_clock_p50_s": 100.4},
    ]}
    out = bp.render(doc)
    # The spine footnote demotes it as opt-in (present on a minority)...
    assert "opt-in" in out or "a typical PR doesn't wait" in out, \
        "the rare job should be demoted as opt-in in the spine footnote"
    # ...so the appendix must NOT double-frame the SAME job as on the critical path.
    assert "sits ON the merge-gating critical path" not in out, \
        "a presence-demoted (opt-in/rare) job must not be framed on the critical path"
    # And the finding is stamped `spine_rare` (the mechanism). The stamp is DECOUPLED from
    # coverage: `_saves_wall_clock` (credited-magnitude / catalog-coverage gate) stays True — a
    # rare job with a real OPT24 is still catalog-covered — while `_frames_on_path` (the appendix
    # on-path gate) declines it, so the appendix drops only the "sits ON the critical path" claim.
    f = doc["findings"][0]
    assert f.get("spine_rare") is True
    assert bp._saves_wall_clock(f) is True, "coverage/magnitude gate must IGNORE spine_rare"
    assert bp._frames_on_path(f) is False, "appendix on-path framing gate must honor spine_rare"


def test_spine_rare_pole_stays_catalog_covered_no_false_gap():
    # Finding 1 regression guard (the paradedb shape the earlier test MISSED): the rare-demoted
    # job is ITSELF the drilled pole, its captured log matches NO `_parse_log` detector, and it
    # carries an OPT24 data-driven finding. `spine_rare` must suppress ONLY the appendix on-path
    # framing — it must NOT flip catalog-coverage. If it did (the reviewed regression), the pole
    # would falsely read as a coverage gap: gap-fill fires, the gap is captured to
    # `.ci-speedup-gaps/`, and a phase-4c detector-draft subagent is launched for a job OPT24
    # ALREADY covers — the exact anti-pattern `_gap_poles` guards against.
    pg = "Test pg_search on PostgreSQL 18 (pgrx - arm64)"
    checks = [
        {"name": "build", "p50_s": 1000.0, "present_on": 20, "workflow_file": "ci.yml"},
        {"name": pg, "p50_s": 1351.0, "present_on": 8, "workflow_file": "test-pg_search.yml"},
    ]
    pops = ([[0.05, [["build", 1000.0]]] for _ in range(12)]
            + [[0.05, [["build", 1000.0], [pg, 1351.0]]] for _ in range(8)])
    doc = {"pr_critical_path": {
        "critical_path_check": "build", "critical_path_s": 1000.0,
        "checks": checks, "check_present_n_pr": 20, "populations": pops,
        "poles": [
            {"check": "build", "p50_s": 1000.0, "workflow_file": "ci.yml", "job": "build"},
            # The rare job IS a drilled pole here (unlike the earlier test, where it lived only
            # in checks/populations) — this is what surfaces the coverage-gap regression.
            {"check": pg, "p50_s": 1351.0, "workflow_file": "test-pg_search.yml", "job": pg},
        ],
    }, "findings": [
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH",
         "title": "Long Test Job Without Sharding", "line": 12,
         "workflow_file": "test-pg_search.yml", "affected_jobs": [pg],
         "runner_min_saving": 0, "wall_clock_p50_s": 1351.0},
    ]}
    # A captured log for the pg_search pole that matches no `_parse_log` detector.
    logs = {pg: "compiling crate pgrx\nrunning suite\nfinished in 22m 31s\n"}
    assert bp._parse_log(logs[pg]) is None, "fixture log must match no detector (be a true gap-shape)"

    out = bp.render(doc, logs)
    f = doc["findings"][0]
    # The finding IS stamped spine_rare (the regression scenario is real)...
    assert f.get("spine_rare") is True
    pole = doc["pr_critical_path"]["poles"][1]
    # ...yet it stays catalog-covered: _data_driven_for_pole still joins the OPT24 to the pole.
    assert bp._data_driven_for_pole(pole, doc["findings"]), \
        "spine_rare must NOT strip catalog coverage (the HIGH regression)"
    # So the gap machinery does NOT capture it (no wasted gap-fill / detector-draft subagent).
    assert bp._gap_poles(doc, logs) == [], \
        "a rare pole an OPT24 covers must NOT be captured as a catalog gap"
    # And the rendered pole waterfall does NOT read as a coverage gap...
    assert "matched no known root-cause pattern" not in out
    assert "coverage gap, not a clean job" not in out
    # ...it points at the catalog match, framed opt-in/rare (NOT "sits ON the critical path").
    assert "a measured **catalog pattern** matched this pole" in out
    assert "flagged as opt-in / rare" in out
    assert "sits ON the merge-gating critical path" not in out


def _nx_offcategory_doc() -> tuple[dict, dict]:
    # The nrwl/nx shape (issue #16): a single combined `Run Checks/Lint/Test/Build` step (one
    # `nx affected` that lints + tests + builds) bins as `test` (payload), so the pole's measured
    # dominant category is `test`. Its captured log ALSO carries an uncached `eslint` invocation, so
    # the whole-log `eslint-no-cache` (`scan`) leaf fires — the hijack the fix must demote.
    pole = {
        "check": "Run Checks", "p50_s": 308.0, "workflow_file": "ci.yml", "job": "Run Checks",
        "dominant_category": "test", "dominant_step": "Run Checks/Lint/Test/Build",
        "dominant_p50_s": 300.0, "dominant_share": 0.974, "job_p50_s": 308.0,
        "steps": [{"step": "Run Checks/Lint/Test/Build", "category": "test", "p50_s": 300.0},
                  {"step": "Set up job", "category": "setup", "p50_s": 8.0}],
    }
    doc = {"pr_critical_path": {
        "critical_path_check": "Run Checks", "critical_path_s": 308.0,
        "checks": [{"name": "Run Checks", "p50_s": 308.0, "present_on": 6, "pole_n": 6,
                    "workflow_file": "ci.yml"}],
        "check_present_n_pr": 6,
        "populations": [[0.05, [["Run Checks", 308.0]]] for _ in range(6)],
        "poles": [pole],
    }, "findings": []}
    logs = {"Run Checks": (
        "> nx run-many --target=lint\n"
        "$ eslint . --ext .ts,.tsx,.js\n"
        "> nx affected --target=test\n"
        "PASS  src/foo.spec.ts (312 tests) 41200ms\n")}
    return doc, logs


def test_offcategory_eslint_leaf_does_not_hijack_a_test_dominant_pole():
    # Issue #16 (nrwl/nx, HIGH): the `eslint-no-cache` whole-log leaf must NOT crown the MEASURED
    # CAUSE of a TEST-dominant pole nor pin its full ceiling on a lint-cache fix. The leaf's category
    # (`scan`) contradicts the pole's measured `dominant_category` (`test`), so it demotes to a
    # secondary observation and the pole falls back to its generic dominant-step hand-off.
    doc, logs = _nx_offcategory_doc()
    # Fixture sanity: the log IS a true hijack shape (the eslint detector fires on the joined log)...
    assert bp._parse_log(logs["Run Checks"]) is not None
    assert bp._parse_log(logs["Run Checks"])["fix_key"] == "eslint-no-cache"
    # ...and the demotion helper agrees this leaf is off-category for a test-dominant pole.
    kept, demoted = bp._demote_offcategory_leaf(bp._parse_log(logs["Run Checks"]),
                                                doc["pr_critical_path"]["poles"][0])
    assert kept is None and demoted is not None, "an off-category leaf must be demoted, not crowned"

    out = bp.render(doc, logs)
    # The eslint leaf is NOT crowned: no crown marker for it, and its FIX_META cause prose (the
    # matched-cause prompt) never renders — the pole's ceiling is not pinned on the lint fix.
    assert "ci-speedup:leaf-crown fix_key=eslint-no-cache" not in out, \
        "an off-category eslint leaf must not crown the pole's MEASURED CAUSE"
    assert "re-analyses every file every run" not in out, \
        "the eslint FIX_META cause (matched-cause prompt) must not render for a demoted leaf"
    # It IS kept as a labelled secondary observation (never a silent drop), with its evidence.
    assert "ci-speedup:offcategory-leaf" in out
    assert "Secondary observation" in out
    assert "eslint" in out
    # The pole falls back to the generic dominant-step hand-off, pointing at the MEASURED dominant
    # step (the combined step), not the lint cache.
    assert "investigate the dominant step" in out
    assert "Run Checks/Lint/Test/Build" in out


def test_offcategory_guard_keeps_a_legitimate_same_category_leaf():
    # Discriminator: the guard must NOT demote a leaf whose category AGREES with the pole's dominant
    # category. A genuinely lint-dominant pole (dominant step `Lint`, category `scan`) keeps the
    # eslint crown — proving the fix demotes on the measured contradiction, not on eslint per se.
    doc, logs = _nx_offcategory_doc()
    pole = doc["pr_critical_path"]["poles"][0]
    pole["dominant_category"] = "scan"
    pole["dominant_step"] = "Lint"
    pole["steps"] = [{"step": "Lint", "category": "scan", "p50_s": 300.0},
                     {"step": "Set up job", "category": "setup", "p50_s": 8.0}]
    kept, demoted = bp._demote_offcategory_leaf(bp._parse_log(logs["Run Checks"]), pole)
    assert kept is not None and demoted is None, "a same-category (lint-dominant) leaf must be kept"
    out = bp.render(doc, logs)
    assert "ci-speedup:leaf-crown fix_key=eslint-no-cache" in out, \
        "a legitimately lint-dominant pole must keep the eslint crown"
    assert "ci-speedup:offcategory-leaf" not in out


def test_offcategory_sibling_token_demotes_a_typecheck_dominant_scan_pole():
    # Issue #16 (sveltejs/svelte, HIGH) — the SIBLING-TOKEN half of the rule. Here the pole's
    # measured dominant category IS `scan` (so a coarse-category check alone would keep the eslint
    # crown), but the dominant STEP is a type-check (`svelte-check`), NOT the lint the eslint fix
    # addresses. `_LEAF_DOMINANT_STEP_TOKEN` (eslint -> /lint/) must fire the demotion on the
    # dominant-step token mismatch. This is the case the plain category check CANNOT catch — without
    # the token branch this pole would wrongly crown a lint-cache fix on a type-check-dominated pole.
    doc, logs = _nx_offcategory_doc()
    pole = doc["pr_critical_path"]["poles"][0]
    pole["dominant_category"] = "scan"          # scan IS dominant — category agreement alone passes
    pole["dominant_step"] = "svelte-check"      # ...but the dominant STEP is a type-check, not lint
    pole["steps"] = [{"step": "svelte-check", "category": "scan", "p50_s": 300.0},
                     {"step": "Set up job", "category": "setup", "p50_s": 8.0}]
    leaf = bp._parse_log(logs["Run Checks"])
    assert leaf and leaf["fix_key"] == "eslint-no-cache"          # fixture sanity: eslint fires
    # The token branch — NOT the category branch — must be what demotes here: category agrees (scan).
    assert bp._offcategory_leaf(leaf, pole) is True, \
        "a scan-dominant pole whose dominant step is a type-check must demote the eslint leaf"
    kept, demoted = bp._demote_offcategory_leaf(leaf, pole)
    assert kept is None and demoted is not None
    out = bp.render(doc, logs)
    assert "ci-speedup:leaf-crown fix_key=eslint-no-cache" not in out, \
        "the eslint leaf must not crown a type-check-dominant pole (sibling-token demotion)"
    assert "ci-speedup:offcategory-leaf" in out
    assert "investigate the dominant step" in out
    assert "svelte-check" in out


def test_demoted_offcategory_pole_does_not_pull_an_llm_gap_fill():
    # A demoted pole is NOT a coverage gap: a detector DID match its log, so it must fall back to the
    # generic dominant-step hand-off, NOT the LLM gap-fill (which is only for poles no detector
    # recognized). The `offcat_leaf is None` term in render's `analysis` guard is load-bearing here.
    doc, logs = _nx_offcategory_doc()                            # test-dominant pole, eslint leaf -> demoted
    analysis = {"cause": "SENTINEL-LLM-CAUSE the runner re-clones the world each run.",
                "breakdown": [["clone", "~30s"]], "evidence": ["fetching 200000 objects"],
                "prompt": "SENTINEL-LLM-PROMPT investigate the clone."}
    out = bp.render(doc, logs, analyses={"Run Checks": analysis})
    assert "ci-speedup:offcategory-leaf" in out                 # the leaf demoted, as designed
    assert "🤖 LLM root-cause analysis" not in out, \
        "a demoted pole matched a detector — it must not pull an LLM gap-fill"
    assert "SENTINEL-LLM-CAUSE" not in out and "SENTINEL-LLM-PROMPT" not in out
    # Positive control: the SAME pole with a log that matches NO detector (no eslint) is a true
    # coverage gap, so the gap-fill DOES render — proving the suppression above is caused by the
    # demoted leaf, not by the analysis simply never rendering in this fixture.
    out2 = bp.render(doc, {"Run Checks": "> nx affected --target=test\nPASS 41200ms\n"},
                     analyses={"Run Checks": analysis})
    assert "ci-speedup:offcategory-leaf" not in out2
    assert "🤖 LLM root-cause analysis" in out2 and "SENTINEL-LLM-CAUSE" in out2


def test_offcategory_leaf_fails_open_without_a_measured_contradiction():
    # `_offcategory_leaf` must NEVER demote without a measured contradiction to point at — it fails
    # OPEN (keep the crown) on a missing/unknown category or a pole with no decomposition. The
    # missing-`dominant_category` case is the load-bearing one: a pole from a legacy findings doc that
    # predates step decomposition must keep its crown, not be silently demoted on absence.
    doc, logs = _nx_offcategory_doc()
    leaf = bp._parse_log(logs["Run Checks"])
    pole = doc["pr_critical_path"]["poles"][0]
    p_nodom = {k: v for k, v in pole.items() if k != "dominant_category"}
    assert bp._offcategory_leaf(leaf, p_nodom) is False, \
        "a pole with no measured dominant_category must keep its crown (fail open)"
    kept, demoted = bp._demote_offcategory_leaf(leaf, p_nodom)
    assert kept is not None and demoted is None
    assert bp._offcategory_leaf({"fix_key": "not-a-real-detector"}, pole) is False, \
        "an unmapped fix_key has no known category to contradict — keep the crown"
    assert bp._offcategory_leaf(None, pole) is False             # no leaf -> nothing to demote


def test_spine_rare_presence_clause_guards_always_present_frequency_demoted_leg():
    # Finding 2 guard: the presence-MINORITY clause in `_opt_in_rare_check` is load-bearing on the
    # modern `pole_n` path. A matrix leg that is FREQUENCY-demoted (`pole_n == 0` — rarely the single
    # slowest) but present on EVERY PR is genuinely on the critical path each PR (a sibling gates),
    # so its real wall-clock lever must STAY on-path — it must NOT be stamped `spine_rare`. Only a
    # check present on a MINORITY of PRs is opt-in/rare. Deleting the presence clause (reducing the
    # test to `not _typical_check`) would wrongly stamp the always-present leg — this test FAILS then.
    always_leg = "build (3.10, windows-latest)"   # pole_n 0 but present 11/11 — NOT opt-in
    minority = "Heavy conditional suite"           # pole_n 0 AND present 4/11 — opt-in/rare
    checks = [
        {"name": "lint", "p50_s": 100.0, "present_on": 11, "pole_n": 11, "workflow_file": "ci.yml"},
        {"name": always_leg, "p50_s": 900.0, "present_on": 11, "pole_n": 0, "workflow_file": "ci.yml"},
        {"name": minority, "p50_s": 1300.0, "present_on": 4, "pole_n": 0,
         "workflow_file": "heavy.yml"},
    ]
    # 7 PRs run lint + build leg; 4 also run the minority suite. npop=11, present: lint 11,
    # build-leg 11, minority 4. All checks carry `pole_n` → the modern pole-frequency path is active.
    pops = ([[0.05, [["lint", 100.0], [always_leg, 900.0]]] for _ in range(7)]
            + [[0.05, [["lint", 100.0], [always_leg, 900.0], [minority, 1300.0]]] for _ in range(4)])
    doc = {"pr_critical_path": {
        "critical_path_check": "lint", "critical_path_s": 100.0,
        "checks": checks, "check_present_n_pr": 11, "populations": pops,
        "poles": [{"check": "lint", "p50_s": 100.0, "workflow_file": "ci.yml", "job": "lint"}],
    }, "findings": [
        {"id": "leg", "pattern": "OPT73", "severity": "MEDIUM", "title": "Redundant Build Setup",
         "line": 5, "workflow_file": "ci.yml", "affected_jobs": [always_leg],
         "runner_min_saving": 0, "wall_clock_p50_s": 300.0},
        {"id": "min", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job Without Sharding",
         "line": 8, "workflow_file": "heavy.yml", "affected_jobs": [minority],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
    ]}
    bp.render(doc)
    leg_f = next(f for f in doc["findings"] if f["id"] == "leg")
    min_f = next(f for f in doc["findings"] if f["id"] == "min")
    # The always-present frequency-demoted leg must NOT be stamped (presence clause keeps it on-path).
    assert not leg_f.get("spine_rare"), \
        "an always-present (frequency-demoted) leg must NOT be demoted opt-in — its lever stays on-path"
    # The true minority-present check MUST be stamped opt-in/rare.
    assert min_f.get("spine_rare") is True, "a minority-present check must be stamped spine_rare"


def test_spine_rare_kept_guard_does_not_demote_a_job_also_on_the_typical_path():
    # KEPT-GUARD: a finding whose job maps to BOTH a typical check AND a rare check is genuinely on
    # the typical path (a sibling matrix leg gates every PR), so its wall-clock lever legitimately
    # "sits ON the critical path" — stamping `spine_rare` would wrongly strip that on-path framing.
    # `_stamp_spine_rare` stamps only when ALL matched checks are opt-in/rare. Even after Finding 1's
    # decoupling (spine_rare no longer touches coverage) this still guards the APPENDIX framing.
    typ = "test (ubuntu-latest)"   # present on every PR — typical
    rare = "test (windows-latest)"  # minority-present — opt-in/rare
    checks = [
        {"name": "lint", "p50_s": 100.0, "present_on": 11, "workflow_file": "ci.yml"},
        {"name": typ, "p50_s": 800.0, "present_on": 11, "workflow_file": "ci.yml"},
        {"name": rare, "p50_s": 900.0, "present_on": 4, "workflow_file": "ci.yml"},
    ]
    pops = ([[0.05, [["lint", 100.0], [typ, 800.0]]] for _ in range(7)]
            + [[0.05, [["lint", 100.0], [typ, 800.0], [rare, 900.0]]] for _ in range(4)])
    doc = {"pr_critical_path": {
        "critical_path_check": "lint", "critical_path_s": 100.0,
        "checks": checks, "check_present_n_pr": 11, "populations": pops,
        "poles": [{"check": "lint", "p50_s": 100.0, "workflow_file": "ci.yml", "job": "lint"}],
    }, "findings": [
        # Routed to the UNEXPANDED matrix base `test` → matches BOTH legs (one typical, one rare).
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job",
         "line": 5, "workflow_file": "ci.yml", "affected_jobs": ["test"],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
    ]}
    bp.render(doc)
    assert not doc["findings"][0].get("spine_rare"), \
        "a job that also maps to a typical check must NOT be demoted opt-in (kept-guard)"


def test_spine_rare_demotes_a_cross_workflow_name_collision_with_a_rare_check():
    # NAME-level (formerly `_wf_conflict`-guarded to the OPPOSITE): GitHub gives same-named jobs in
    # different workflows IDENTICAL check-run names (`Python 3.13` in two workflows). The spine
    # footnote demotes the NAME `Python 3.13` as opt-in/rare, and the reader can't tell which
    # workflow's `Python 3.13` a later "sits ON the merge-gating critical path" note is about — so a
    # finding on that job in ANY workflow must be stamped `spine_rare`, or the appendix double-frames
    # the same NAME the footnote calls opt-in on-path (the tauri `test (macos-latest)` class, the
    # cross-workflow arm). The stamp join is NAME-level — it does NOT skip `_wf_conflict` — so this
    # matches what `check_dropped_check_not_framed_on_path` enforces. Over-stamping here only makes
    # the appendix MORE conservative (drop an on-path claim for a name the footnote already demotes);
    # it can never manufacture the opposite contradiction, and it never touches catalog coverage.
    checks = [
        {"name": "lint", "p50_s": 100.0, "present_on": 11, "workflow_file": "a.yml"},
        # A rare check in a DIFFERENT workflow that shares the finding's job name.
        {"name": "Python 3.13", "p50_s": 900.0, "present_on": 4, "workflow_file": "b.yml"},
    ]
    pops = ([[0.05, [["lint", 100.0]]] for _ in range(7)]
            + [[0.05, [["lint", 100.0], ["Python 3.13", 900.0]]] for _ in range(4)])
    doc = {"pr_critical_path": {
        "critical_path_check": "lint", "critical_path_s": 100.0,
        "checks": checks, "check_present_n_pr": 11, "populations": pops,
        "poles": [{"check": "lint", "p50_s": 100.0, "workflow_file": "a.yml", "job": "lint"}],
    }, "findings": [
        # Finding lives in a.yml; its job name collides with b.yml's rare check. The footnote
        # demotes the NAME, so the on-path framing must be suppressed regardless of workflow file.
        {"id": "f1", "pattern": "OPT73", "severity": "MEDIUM", "title": "Redundant Setup",
         "line": 5, "workflow_file": "a.yml", "affected_jobs": ["Python 3.13"],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
    ]}
    bp.render(doc)
    assert doc["findings"][0].get("spine_rare") is True, \
        "a finding on a NAME the footnote demotes as opt-in/rare must be spine_rare even across workflows"
    assert bp._frames_on_path(doc["findings"][0]) is False, \
        "the appendix must not frame a footnote-demoted NAME on-path"


# --- tauri regression: `test (macos-latest)` framed on the merge-gating path while demoted -------
# The dogfood `check_dropped_check_not_framed_on_path` FAIL on tauri-apps/tauri had TWO renderer
# gaps, both leaving the RARE leg `test (macos-latest)` framed on-path while the footnote demotes
# it as opt-in: (1) `_stamp_spine_rare` folded a DISTINCT typical sibling leg (`test
# (windows-latest)`) into a single-leg rare finding's match set, so the KEPT-GUARD wrongly declined
# the stamp; and (2) an on-path cluster (OPT73) finding's `**Where:**` LED with the rare sibling
# leg (`affected_jobs[0]`) even though its on-path claim rests on the typical leg. Both are fixed to
# be NAME-level and on-path-leg-first respectively.
def _tauri_shape_checks() -> list[dict]:
    # windows: typical (pole_n 5); macos + ubuntu: minority-present, never the single slowest
    # (pole_n 0) → opt-in/rare. lint: the always-present gate.
    return [
        {"name": "lint", "p50_s": 100.0, "present_on": 20, "pole_n": 20, "workflow_file": "ci.yml"},
        {"name": "test (windows-latest)", "p50_s": 900.0, "present_on": 8, "pole_n": 5,
         "workflow_file": "test.yml"},
        {"name": "test (macos-latest)", "p50_s": 800.0, "present_on": 5, "pole_n": 0,
         "workflow_file": "test.yml"},
        {"name": "test (ubuntu-latest)", "p50_s": 700.0, "present_on": 5, "pole_n": 0,
         "workflow_file": "test.yml"},
    ]


def _tauri_shape_pops() -> list:
    # npop 20: 12 lint-only; 3 lint+windows; 5 lint+windows+macos+ubuntu (windows is the pole
    # whenever the test cluster runs). windows present 8, macos/ubuntu present 5.
    W, M, U, L = "test (windows-latest)", "test (macos-latest)", "test (ubuntu-latest)", "lint"
    return ([[0.05, [[L, 100.0]]] for _ in range(12)]
            + [[0.05, [[L, 100.0], [W, 900.0]]] for _ in range(3)]
            + [[0.05, [[L, 100.0], [W, 900.0], [M, 800.0], [U, 700.0]]] for _ in range(5)])


def _tauri_shape_doc(findings: list[dict]) -> dict:
    return {"pr_critical_path": {
        "critical_path_check": "lint", "critical_path_s": 100.0,
        "checks": _tauri_shape_checks(), "check_present_n_pr": 20, "populations": _tauri_shape_pops(),
        "poles": [{"check": "lint", "p50_s": 100.0, "workflow_file": "ci.yml", "job": "lint"}],
    }, "findings": findings}


def test_spine_rare_stamps_exact_rare_leg_despite_typical_sibling_leg():
    # ARM 1 (the `_same_matrix` fold): a finding on the EXACT rare leg `test (macos-latest)` — with
    # a TYPICAL sibling leg `test (windows-latest)` in the same matrix/workflow — must be stamped
    # spine_rare. The old `_hits` matched the sibling via `_same_matrix` and the KEPT-GUARD then
    # declined the stamp, leaving the rare leg framed "sits ON the critical path" (the contradiction).
    doc = _tauri_shape_doc([
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job Without Sharding",
         "line": 5, "workflow_file": "test.yml", "affected_jobs": ["test (macos-latest)"],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
    ])
    out = bp.render(doc)
    assert doc["findings"][0].get("spine_rare") is True, \
        "the exact rare leg must be demoted even when a typical sibling leg shares its matrix base"
    assert bp._frames_on_path(doc["findings"][0]) is False
    assert "sits ON the merge-gating critical path" not in out


def test_on_path_cluster_where_leads_with_on_path_leg_not_rare_sibling():
    # ARM 2 (the `**Where:**` lead): an OPT73 cluster finding is genuinely on-path via its TYPICAL
    # leg `test (windows-latest)`, so it is NOT stamped spine_rare and IS framed on-path — correct.
    # But its `affected_jobs` LEADS with the rare `test (macos-latest)`; the on-path `**Where:**`
    # must display the typical (on-path) leg, never the demoted sibling (which the footnote calls
    # opt-in) — else the reader sees the demoted NAME framed on the critical path.
    doc = _tauri_shape_doc([
        {"id": "c1", "pattern": "OPT73", "severity": "HIGH",
         "title": "Shared step recurs across the cluster",
         "line": 5, "workflow_file": "test.yml",
         "affected_jobs": ["test (macos-latest)", "test (windows-latest)"],
         "evidence": "the `test` step is 90% of the slowest cluster job `test (windows-latest)`",
         "runner_min_saving": 100, "wall_clock_p50_s": 400.0},
    ])
    out = bp.render(doc)
    f = doc["findings"][0]
    assert not f.get("spine_rare"), "a cluster on-path via a typical leg must NOT be demoted"
    assert bp._frames_on_path(f) is True, "it legitimately sits on the critical path (typical leg)"
    # The on-path appendix `**Where:**` line names the typical leg, NOT the demoted rare sibling.
    where = next(ln for ln in out.splitlines()
                 if ln.startswith("**Where:**") and "test.yml" in ln)
    assert "test (windows-latest)" in where, "the on-path Where must lead with the on-path leg"
    assert "test (macos-latest)" not in where, \
        "the on-path Where must NOT name the footnote-demoted rare sibling leg"


def _spine_dropped_check_line(report: str, findings_path: Path) -> str:
    """Run verify_report.py's CLI and return the `no spine-dropped check …` check line (tag +
    message) — the actual invariant that failed on tauri, tied to the SAME rendered bytes."""
    import subprocess
    verify = Path(__file__).resolve().parent / "verify_report.py"
    rp = findings_path.parent / "report-2026-05-29.md"
    rp.write_text(report, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(verify), "--report", str(rp), "--findings", str(findings_path)],
        capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if "no spine-dropped check is also framed on the merge-gating critical path" in ln:
            return ln
    raise AssertionError(f"no spine-dropped check line in verify_report output:\n{out}")


def test_rendered_tauri_shape_passes_the_spine_dropped_verify_gate_end_to_end(tmp_path: Path):
    # End-to-end coupling: render BOTH arms through bp.render() and feed the real bytes to the real
    # `check_dropped_check_not_framed_on_path` gate — the invariant that FAILED on tauri. On the
    # pre-fix renderer this render frames `test (macos-latest)` on-path (both a single rare leg and
    # a cluster Where lead) and the gate FAILs; after the fix it PASSes.
    doc = _tauri_shape_doc([
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job Without Sharding",
         "line": 5, "workflow_file": "test.yml", "affected_jobs": ["test (macos-latest)"],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
        {"id": "x1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job Without Sharding",
         "line": 7, "workflow_file": "other.yml", "affected_jobs": ["test (macos-latest)"],
         "runner_min_saving": 0, "wall_clock_p50_s": 400.0},
        {"id": "c1", "pattern": "OPT73", "severity": "HIGH",
         "title": "Shared step recurs across the cluster",
         "line": 5, "workflow_file": "test.yml",
         "affected_jobs": ["test (macos-latest)", "test (windows-latest)"],
         "evidence": "the `test` step is 90% of the slowest cluster job `test (windows-latest)`",
         "runner_min_saving": 100, "wall_clock_p50_s": 400.0},
    ])
    out = bp.render(doc)
    fp = tmp_path / "findings.json"
    fp.write_text(json.dumps(doc), encoding="utf-8")
    line = _spine_dropped_check_line(out, fp)
    assert line.startswith("PASS"), \
        f"a footnote-demoted leg must not be framed on the merge-gating path:\n{line}"


def test_render_excludes_pole_job_from_also_noticed_appendix():
    # Class A #5: a catalog finding on a job that is ALSO drilled as a long pole must NOT appear in
    # the "Also noticed" off-path appendix (the pole already headlines it as the biggest lever).
    # OPT24 on `main.yml` (pytest-torch) sits on the pole job; an OPT45 on a different job stays.
    checks = [
        {"name": "pytest-torch (ubuntu, 3.10)", "p50_s": 1500.0, "present_on": 10, "workflow_file": "main.yml"},
        {"name": "lint", "p50_s": 50.0, "present_on": 10, "workflow_file": "main.yml"},
    ]
    pops = [[0.1, [["pytest-torch (ubuntu, 3.10)", 1500.0], ["lint", 50.0]]] for _ in range(10)]
    doc = {"pr_critical_path": {
        "critical_path_check": "pytest-torch (ubuntu, 3.10)", "critical_path_s": 1500.0,
        "checks": checks, "check_present_n_pr": 10, "populations": pops,
        "poles": [{"check": "pytest-torch (ubuntu, 3.10)", "p50_s": 1500.0,
                   "workflow_file": "main.yml", "job": "pytest-torch (ubuntu, 3.10)"}],
    }, "findings": [
        {"id": "f1", "pattern": "OPT24", "severity": "HIGH", "title": "Long Test Job Without Sharding",
         "workflow_file": "main.yml", "affected_jobs": ["pytest-torch"], "runner_min_saving": 0},
        {"id": "f2", "pattern": "OPT45", "severity": "LOW", "title": "Missing Concurrency Groups",
         "workflow_file": "main.yml", "affected_jobs": ["lint"], "runner_min_saving": 120},
    ]}
    out = bp.render(doc)
    assert "OPT24" not in out, "a finding on the drilled-pole job must be excluded from 'Also noticed'"
    assert "OPT45" in out, "a finding on a non-pole job must still appear in 'Also noticed'"


def test_render_workflow_gate_frequency_sums_all_legs():
    # Class A #2: a workflow's "gates N/M PRs" = how often ANY of its checks is the per-PR slowest,
    # summed over ALL its legs — not just the representative pole. `validate.yml` holds the pole via
    # leg `val (a)` on 3 PRs and sibling leg `val (b)` on 2 → 5/10, even though only one leg is the
    # rendered representative. Pre-#2 (sum over representative poles) this rendered 3/10.
    checks = [
        {"name": "val (a)", "p50_s": 500.0, "present_on": 5, "workflow_file": "validate.yml"},
        {"name": "val (b)", "p50_s": 480.0, "present_on": 5, "workflow_file": "validate.yml"},
        {"name": "other", "p50_s": 50.0, "present_on": 10, "workflow_file": "other.yml"},
    ]
    pops = []
    for _ in range(3):
        pops.append([0.1, [["val (a)", 500.0], ["val (b)", 100.0], ["other", 50.0]]])   # (a) slowest
    for _ in range(2):
        pops.append([0.1, [["val (b)", 480.0], ["val (a)", 100.0], ["other", 50.0]]])   # (b) slowest
    for _ in range(5):
        pops.append([0.1, [["other", 50.0]]])                                            # neither leg
    doc = {"pr_critical_path": {
        "critical_path_check": "val (a)", "critical_path_s": 500.0,
        "checks": checks, "check_present_n_pr": 10, "populations": pops,
        "poles": [{"check": "val (a)", "p50_s": 500.0, "workflow_file": "validate.yml", "job": "validate"}],
    }}
    out = bp.render(doc)
    assert re.search(r"`validate\.yml` gates 5/10 PRs", out), \
        "per-workflow gate frequency must sum both legs (5/10), not just the representative (3/10)"


def test_render_floors_against_heavy_minority_check_not_just_typical():
    # Class A #6: the floor pool is the FULL concurrent set, not the typical-PR chart. A pole's floor
    # must be a heavy check that co-occurs on a majority of its gating PRs even if that check runs on
    # a MINORITY of all PRs (so it's not "typical"). `deploy` (on every PR) is gated by `heavy` (on
    # half the PRs, slower than every typical check); the floor note must name `heavy`, not `lint`.
    # `render()` derives `typical` internally from `checks` + `present_on` — `heavy`'s present_on=5
    # fails the strict-majority test (5 > 10×0.5 is False), so PRE-#6 (floor pool = `src` = typical)
    # `heavy` was excluded from the floor pool entirely; the split floor_pool (all checks) restores it.
    checks = [
        {"name": "deploy", "p50_s": 1000.0, "present_on": 10, "workflow_file": "ci.yml"},
        {"name": "heavy", "p50_s": 780.0, "present_on": 5, "workflow_file": "ci.yml"},   # minority, the real floor
        {"name": "lint", "p50_s": 200.0, "present_on": 10, "workflow_file": "ci.yml"},
    ]
    pops = []
    for i in range(10):                     # 10 PRs; deploy slowest on all; heavy co-occurs on 6 (a majority)
        row = [["deploy", 1000.0], ["lint", 200.0]]
        if i < 6:
            row.insert(1, ["heavy", 780.0])
        pops.append([0.1, row])
    doc = {"pr_critical_path": {
        "critical_path_check": "deploy", "critical_path_s": 1000.0,
        "checks": checks, "check_present_n_pr": 10, "populations": pops,
        "poles": [{"check": "deploy", "p50_s": 1000.0, "workflow_file": "ci.yml", "job": "deploy"}],
    }}
    out = bp.render(doc)
    assert "`heavy`" in out, "the heavy minority check must be named as the floor"
    assert re.search(r"biggest single measured win is \*\*~3m 40s\*\*", out), \
        "addressable win must be pole(1000s) - heavy(780s) = 3m 40s, not pole - lint"


# --------------------------------------------------------------------------- #
# _parse_log — one assertion per leaf detector + the negative case
# --------------------------------------------------------------------------- #

def test_parse_log_detects_prisma_per_file_migrations():
    # Realistic structure: each file prints its per-file migration total then its ✓
    # summary, files one after another.
    log = "\n".join([
        "   Total Migration Time: 303057.44ms",
        " ✓ prisma.mysql.test.ts (446 tests | 9 skipped) 579553ms",
        "   Total Migration Time: 150000.00ms",
        " ✓ prisma.pg.test.ts (446 tests) 300000ms",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "prisma-migrate-once"
    # Two deeper levels: the parallel test files, then migrations-vs-tests.
    assert len(leaf["deeper"]) == 2
    mig = next(r for r in leaf["deeper"][-1]["rows"] if "migration" in r[0].lower())
    # The SLOWEST file's OWN migration total (303s of its 580s), not an average.
    assert abs(mig[1] - 303.057) < 1.0
    assert leaf["magnitude"]["value"] == round(100 * 303.05744 / 579.553, 2)  # ~52%
    assert leaf["evidence"]  # verbatim proof present


def test_prisma_migration_uses_slowest_files_own_total_not_cross_file_average():
    # Regression: the slowest file (mysql, 400s) has a SMALL migration (80s); the
    # other engines have huge ones. Averaging across files would wrongly inflate the
    # share (~220s / 55%); it must use mysql's OWN 80s (20%).
    log = "\n".join([
        "   Total Migration Time: 300000.00ms",          # sqlite
        " ✓ prisma.sqlite.test.ts (446 tests) 350000ms",
        "   Total Migration Time: 280000.00ms",          # pg
        " ✓ prisma.pg.test.ts (446 tests) 360000ms",
        "   Total Migration Time: 80000.00ms",           # mysql (the slowest file)
        " ✓ prisma.mysql.test.ts (446 tests) 400000ms",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "prisma-migrate-once"
    mig = next(r for r in leaf["deeper"][-1]["rows"] if "migration" in r[0].lower())
    assert abs(mig[1] - 80.0) < 1.0          # mysql's own 80s, NOT the ~220s average
    assert leaf["magnitude"]["value"] == 20.0  # 80 / 400, NOT ~55%


def test_parse_log_detects_vitest_istanbul_coverage():
    log = "\n".join([
        "$ vitest run --coverage --coverage.provider=istanbul",
        " RUN  v4.1.5 /repo/packages/core",
        "      Coverage enabled with istanbul",
        " Duration  12.18s (transform 12.24s, setup 0ms, import 19.07s, tests 4.13s, environment 7ms)",
        " RUN  v4.1.5 /repo/packages/web",
        " Duration  19.30s (transform 7.03s, setup 0ms, import 14.25s, tests 32.56s, environment 0ms)",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "vitest-v8-coverage"
    # Compile/load vs tests split is the root-cause level.
    last = leaf["deeper"][-1]
    assert any("transform" in r[0] for r in last["rows"])


def test_parse_log_detects_vitest_import_bound_without_coverage():
    # No coverage, and import dominates tests -> the import-bound leaf, NOT istanbul.
    log = "\n".join([
        " RUN  v4.1.4 /repo/web",
        " Test Files  149 passed (149)",
        " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, tests 214.54s, environment 8ms)",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "vitest-isolate-pool"
    rows = leaf["deeper"][-1]["rows"]
    assert rows[0][0].startswith("import")  # import is the blocker (first) row


def test_parse_log_detects_turbo_cold_cache():
    log = "\n".join([
        "   • Remote caching disabled",
        "cache miss, executing aaa",
        "cache miss, executing bbb",
        " Tasks:    149 successful, 149 total",
        "Cached:    0 cached, 149 total",
        "  Time:    9m48.772s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "turbo-remote-cache"
    rows = leaf["deeper"][-1]["rows"]
    assert rows[0][0].startswith("rebuilt")  # 149 rebuilt, 0 restored
    assert "Remote caching disabled" in "\n".join(leaf["evidence"])


def test_parse_log_detects_turbo_cold_cache_with_remote_enabled():
    # Second trigger: remote caching is ON (no "disabled" line) but the LOCAL cache
    # is fully cold (0 cached) with >5 misses -> still a cold-cache root cause, and
    # the note must NOT claim remote caching is disabled.
    log = "\n".join(
        [f"cache miss, executing pkg{i}" for i in range(6)]
        + [" Tasks:    80 successful, 80 total",
           "Cached:    0 cached, 80 total",
           "  Time:    6m12.000s"])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "turbo-remote-cache"
    note = leaf["deeper"][-1]["blocker_note"]
    assert "0/80 cached" in note
    assert "Remote caching DISABLED" not in note     # remote was not disabled here


def test_parse_log_turbo_cold_cache_respects_the_miss_boundary():
    # 5 misses (not >5) with remote caching enabled is below the threshold -> no leaf.
    log = "\n".join(
        [f"cache miss, executing pkg{i}" for i in range(5)]
        + ["Cached:    0 cached, 80 total"])
    assert bp._parse_log(log) is None


def test_parse_log_detects_turbo_partial_cache_high_miss():
    # Caching is ON and some packages hit, but most rebuild every run (62% miss here):
    # a high partial-miss rate is cache-key churn, not normal per-PR change, so it
    # drills as `turbo-partial-cache` (distinct from the fully-cold `turbo-remote-cache`).
    log = "\n".join(
        [f"cache miss, executing pkg{i}" for i in range(93)]
        + [" Tasks:    150 successful, 150 total",
           "Cached:    57 cached, 150 total",
           "  Time:    7m11.858s"])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "turbo-partial-cache"
    assert leaf["magnitude"]["value"] == 62.0 and leaf["magnitude"]["unit"] == "%"
    note = leaf["deeper"][-1]["blocker_note"]
    assert "57/150 cached" in note and "62% rebuilt" in note


def test_parse_log_turbo_cold_cache_summary_driven_multi_invocation_and_bypass():
    # The langfuse variant the LLM gap-fill first surfaced, now catalogued: a job runs
    # TWO turbo invocations (db:migrate then build), the build prints NO per-task
    # "cache miss" line (only "cache bypass, force executing") - so detection must (a)
    # pick the summary of the SLOWEST invocation (build 0/6 @ 1m20s, not migrate 0/1 @
    # 3s), and (b) treat the run-summary + "Remote caching disabled" as authoritative
    # without needing >5 explicit miss lines.
    log = "\n".join([
        "$ turbo run db:migrate",
        "   • Remote caching disabled",
        " Tasks:    1 successful, 1 total",
        "Cached:    0 cached, 1 total",
        "  Time:    3.013s",
        "$ turbo run build",
        "   • Remote caching disabled",
        "cache bypass, force executing 7ec18f29742f9e0f",
        " Tasks:    6 successful, 6 total",
        "Cached:    0 cached, 6 total",
        "  Time:    1m20.171s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "turbo-remote-cache"
    # The BUILD summary (6 rebuilt) drives it, not the 1-task migrate.
    assert leaf["magnitude"]["value"] == 100.0          # 6/6 rebuilt
    note = leaf["deeper"][-1]["blocker_note"]
    assert "0/6 cached" in note and "Remote caching DISABLED" in note
    assert any("cache bypass, force executing" in e for e in leaf["evidence"])


def test_parse_log_turbo_picks_the_slowest_invocation_not_the_most_rebuilt():
    # expo/expo regression: a job runs THREE turbo invocations - a fast `prepare`
    # (104/128), the DOMINANT `check` (127/356, 9m48.756s wall), and a separate `lint`
    # (0/260, 4m40s wall). The check step is the one the drill renders under, but it is
    # NOT the most-rebuilt summary - lint rebuilds all 260. Keying off most-rebuilt
    # crowned the lint summary (0/260 = 100% miss, turbo-remote-cache), overstating the
    # miss rate and contradicting the evidence block, which quotes the slow check build.
    # The magnitude must come from the SLOWEST (gating) invocation, the 9m48s check.
    log = "\n".join([
        "$ turbo run prepare",
        " Tasks:    128 successful, 128 total",
        "Cached:    104 cached, 128 total",
        "  Time:    45.0s",
        "$ turbo run check",
        *[f"cache miss, executing check-pkg{i}" for i in range(12)],
        " Tasks:    356 successful, 356 total",
        "Cached:    127 cached, 356 total",
        "  Time:    9m48.756s",
        "$ turbo run lint",
        *[f"cache miss, executing lint-pkg{i}" for i in range(12)],
        " Tasks:    260 successful, 260 total",
        "Cached:    0 cached, 260 total",
        "  Time:    4m40.000s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None
    # The slow `check` block (127 cached of 356 → 229 rebuilt = 64% miss) drives it, NOT
    # the cold `lint` (0 cached of 260 → 100% miss).
    assert leaf["fix_key"] == "turbo-partial-cache"
    assert leaf["magnitude"]["value"] == round(100 * 229 / 356, 2)   # 64.33%, not 100%
    rebuilt_row, restored_row = leaf["deeper"][-1]["rows"]
    assert rebuilt_row[1] == 229 and restored_row[1] == 127
    # Evidence must quote the SAME (gating) summary the magnitude came from, never the
    # cold lint block - the two halves of the finding agree.
    joined_ev = "\n".join(leaf["evidence"])
    assert "127 cached, 356 total" in joined_ev
    assert "0 cached, 260 total" not in joined_ev


def test_parse_log_turbo_partial_cache_ignores_a_normal_low_miss_rate():
    # A few misses (the packages a PR actually changed) is normal, NOT churn: 10/150
    # = 6.7% miss is below the 40% floor -> no leaf (don't cry wolf on healthy caching).
    log = "\n".join(
        [f"cache miss, executing pkg{i}" for i in range(10)]
        + [" Tasks:    150 successful, 150 total",
           "Cached:    140 cached, 150 total",
           "  Time:    1m30.000s"])
    assert bp._parse_log(log) is None


def test_parse_log_turbo_does_not_fire_on_full_local_cache_hit():
    # "Remote caching disabled" but the LOCAL cache fully hit (0 misses) is benign -
    # must NOT headline a cold cache. (Guards the remote_off & miss>0 fix.)
    log = "\n".join([
        "   • Remote caching disabled",
        " Tasks:    149 successful, 149 total",
        "Cached:    149 cached, 149 total",
        "  Time:    8.2s",
    ])
    assert bp._parse_log(log) is None


def test_parse_log_detects_buildx_no_persistent_cache():
    # A `docker buildx build` with NO `--cache-from`/`--cache-to` backend: BuildKit
    # prints `#NN [stage] RUN|FROM …` headers + matching `#NN DONE <secs>s`, every
    # step cold (zero `CACHED` lines). The slowest cold layer's share of the build
    # wall is the load-bearing magnitude. `cache-binary` is the `docker/setup-buildx-action`
    # default (caches the binary, not the layers) and must NOT count as a cache backend.
    log = "\n".join([
        "  cache-binary: true",
        "[command]/usr/bin/docker buildx build --output type=local,dest=/tmp/out "
        "--platform linux/amd64 --target publish-server .",
        "#6 [base 1/2] FROM docker.io/example/base:1.0@sha256:abc",
        "#6 DONE 80.0s",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN --mount=type=cache,target=/root/.ccache cmake --build .",
        "#14 DONE 600.0s",
        "#15 [publish 1/1] COPY --from=server dist /out",
        "#15 DONE 0.5s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    # cold-layer wall = 80 + 180 + 600 + 0.5 = 860.5s. RUN/FROM/COPY/ADD all build
    # CACHEABLE layers (a warm `--cache-from` skips them as `CACHED`), so COPY #15 counts
    # toward the wall too. Slowest cold layer #14 = 600s. Magnitude divides by the SAME
    # cold wall the `pct_of: "sum"` drill bars do, so the headline % and bars agree.
    assert leaf["magnitude"]["unit"] == "%"
    assert leaf["magnitude"]["value"] == round(100 * 600.0 / 860.5, 2)  # ~69.73%
    rows = leaf["deeper"][-1]["rows"]
    assert rows[0][1] == 600.0          # slowest cold layer leads the drill
    assert any("copy" in r[0] for r in rows)   # the COPY layer is counted, not dropped
    assert "BIGGEST LEVER" in leaf["deeper"][-1]["blocker_note"]
    assert leaf["evidence"]             # verbatim proof present


def test_parse_log_buildx_evidence_quotes_the_slowest_cold_layer():
    # Faithfulness: the verbatim-evidence excerpt must quote the header AND the matching
    # `DONE` line of the SLOWEST cold layer - the single layer the BIGGEST-LEVER magnitude
    # is computed from. A blind "first-2 headers + last-3 DONE" window can structurally
    # exclude that layer (its header isn't in the first two, its DONE isn't in the last
    # three) and juxtapose an unrelated header against an unrelated DONE, inviting a false
    # reading (e.g. apt-get's header next to pip's DONE). The quoted evidence must
    # substantiate the headline number, not contradict it.
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#5 [1/4] FROM docker.io/example/base:1.0",
        "#5 DONE 12.0s",
        "#6 [2/4] RUN apt-get install -y build-essential",
        "#6 DONE 26.4s",
        "#7 [3/4] RUN make -j4 all",       # the slowest cold layer - the BIGGEST LEVER
        "#7 DONE 185.9s",
        "#8 [4/4] RUN pip install -r requirements.txt",
        "#8 DONE 94.8s",
        "#9 [5/5] COPY dist /out",
        "#9 DONE 20.4s",
        "#10 [6/6] COPY meta /meta",
        "#10 DONE 0.0s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    # #7 is the slowest cold layer (185.9s); the drill leads with it, so the magnitude
    # is computed from #7's DONE line.
    assert leaf["deeper"][-1]["rows"][0][1] == 185.9
    ev_text = "\n".join(leaf["evidence"])
    assert "#7 [3/4] RUN make -j4 all" in ev_text   # the slowest layer's header is quoted
    assert "#7 DONE 185.9s" in ev_text              # ...and its DONE line (the magnitude source)


def test_parse_log_buildx_tail_row_keeps_drill_shares_summing_to_the_cold_wall():
    # Regression: with MORE than 6 cold layers the drill truncates to the top 6 + a
    # rollup row. That rollup must carry the SUMMED seconds of the hidden layers (not
    # None), or `pct_of: "sum"` would divide every shown bar by a partial denominator and
    # inflate the shares past the headline magnitude. 8 cold layers -> 6 shown + 1 tail.
    log = "\n".join(
        ["[command]/usr/bin/docker buildx build --output type=local ."]
        + [f"#{i} [s{i} 1/1] RUN step-{i}\n#{i} DONE {10 * i}.0s" for i in range(1, 9)]
    )
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    rows = leaf["deeper"][-1]["rows"]
    assert len(rows) == 7                      # 6 layers + the rollup row
    assert "more cold layers" in rows[-1][0]
    assert rows[-1][1] == 10.0 + 20.0          # two hidden layers (#1=10s, #2=20s) folded
    cold_wall = sum(10 * i for i in range(1, 9))           # 360.0s
    assert sum(s for _l, s, _d in rows) == cold_wall       # bars total the cold wall
    # headline magnitude divides by that same cold wall (slowest layer #8 = 80s).
    assert leaf["magnitude"]["value"] == round(100 * 80.0 / cold_wall, 2)


def test_parse_log_buildx_fires_on_export_only_cache_with_no_restore():
    # False-negative guard: a config that EXPORTS cache (`--cache-to`) but never IMPORTS
    # it (`--cache-from`) writes a cache it never reads, so every run still starts cold.
    # Keying suppression on `--cache-to` would miss this; we key on the import path, so it
    # must still fire - and the label must call out the export-only nature.
    export_only = "\n".join([
        "[command]/usr/bin/docker buildx build --cache-to type=gha,mode=max "
        "--output type=local .",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    leaf = bp._parse_log(export_only)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    assert "never imported" in leaf["unit_label"]


def test_parse_log_buildx_fires_when_an_expensive_copy_layer_dominates():
    # False-negative guard: a build whose RUN/FROM layers are cheap but a `COPY` of a big
    # context / `COPY --from` artifact is expensive. COPY/ADD are cacheable layers, so the
    # cold wall must include them - excluding COPY would drop cold_wall under the floor and
    # silently miss a build that rebuilds a costly cacheable layer every run.
    copy_heavy = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#6 [base 1/2] FROM docker.io/example/base:1.0",
        "#6 DONE 3.0s",
        "#9 [stage-1 2/3] ADD https://example.com/big.tar /vendor",
        "#9 DONE 5.0s",
        "#12 [stage-1 3/3] COPY --from=builder /app/dist /out",
        "#12 DONE 120.0s",
    ])
    leaf = bp._parse_log(copy_heavy)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    # cold wall = 3 + 5 + 120 = 128s (above the 60s floor only because COPY/ADD count);
    # the COPY layer is the biggest lever.
    rows = leaf["deeper"][-1]["rows"]
    assert rows[0][1] == 120.0
    assert leaf["magnitude"]["value"] == round(100 * 120.0 / 128.0, 2)


def test_parse_log_buildx_does_not_fire_on_a_warm_cache_hit_build():
    # False-fire boundary: a healthy build that restores layers from cache prints
    # `#NN CACHED` (and/or wires a `--cache-from` backend). Either signal must keep
    # the detector silent - a warm build is not a cold-cache finding.
    cached_build = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#6 [base 1/2] FROM docker.io/example/base:1.0",
        "#6 CACHED",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 CACHED",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 12.0s",
    ])
    assert bp._parse_log(cached_build) is None
    # A build that DOES wire a cache backend is configured correctly -> no finding,
    # even with cold steps (the cache simply missed this run).
    with_backend = "\n".join([
        "[command]/usr/bin/docker buildx build --cache-from type=gha "
        "--cache-to type=gha,mode=max .",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    assert bp._parse_log(with_backend) is None
    # A trivial buildx build below the wall floor (one quick layer) is not worth a
    # cache backend -> no finding (guards the _BUILDX_COLD_WALL_FLOOR_S floor).
    tiny = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#6 [base 1/2] FROM docker.io/example/base:1.0",
        "#6 DONE 4.0s",
        "#7 [base 2/2] RUN echo hi",
        "#7 DONE 1.0s",
    ])
    assert bp._parse_log(tiny) is None


def test_parse_log_buildx_no_export_label_names_the_no_cache_case():
    # Pin the OTHER arm of the unit_label ternary: with no `--cache-to` at all the label
    # must read "(no persistent cache - all cold)", not the export-only wording.
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    assert "no persistent cache - all cold" in leaf["unit_label"]
    assert "never imported" not in leaf["unit_label"]


def test_parse_log_buildx_fires_on_build_push_action_with_yaml_cache_to_only():
    # The `docker/build-push-action` form (no raw `docker buildx build` line) is the
    # dominant real-world trigger, and it expresses cache as YAML action INPUTS, not CLI
    # flags. A `cache-to:` import-less config must still fire with the export-only label;
    # the evidence must remain usable even without a `docker buildx build` command line.
    log = "\n".join([
        "Run docker/build-push-action@v5",
        "  with:",
        "    push: true",
        "    cache-to: type=gha,mode=max",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    assert "never imported" in leaf["unit_label"]   # export-only via the YAML cache-to
    assert any(e for e in leaf["evidence"])          # evidence is non-empty/usable


def test_parse_log_buildx_does_not_fire_with_yaml_cache_from_import():
    # The correctly-configured build-push-action form: a `cache-from:` YAML input IS a
    # cache import, so a warm restore is wired - the detector must stay silent even with
    # cold layers this run. Guards the `re.MULTILINE` anchor on the YAML input regex.
    log = "\n".join([
        "Run docker/build-push-action@v5",
        "  with:",
        "    cache-from: type=gha",
        "    cache-to: type=gha,mode=max",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    assert bp._parse_log(log) is None


def test_parse_log_buildx_requires_at_least_two_work_steps():
    # Guards `_BUILDX_MIN_WORK_STEPS`: a single heavy cold layer clears the 60s wall floor
    # but is ONE step - a single-layer image isn't a layer-cache story, so no finding.
    one_step = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 300.0s",
    ])
    assert bp._parse_log(one_step) is None


def test_parse_log_buildx_ignores_stray_cached_token_in_log_chatter():
    # Silent-failure regression: the cached-layer count is anchored to the `#NN CACHED`
    # layer grammar, NOT a bare `" CACHED"` substring. A provably-cold build whose log
    # merely MENTIONS the word CACHED (a RUN echo, a test print, a filename) must still
    # fire - a stray token must not zero the count and suppress the finding as "clean".
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#10 [deps 1/1] RUN echo 'nothing was CACHED this run' && pkg install",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
        "Status: build finished, 0 layers CACHED",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"


def test_parse_log_buildx_survives_a_malformed_done_line():
    # A corrupt/interleaved `#NN DONE 1.2.3s` must not crash rendering (float() would
    # raise on the multi-dot capture). The bad layer is skipped as un-timed; the rest of
    # the build still produces a finding off the well-formed layers, and the malformed
    # layer surfaces as an UNPAIRED-layer caveat rather than silently shrinking the wall.
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 1.2.3s",                       # malformed - unparseable seconds
        "#12 [build 1/2] RUN make deps",
        "#12 DONE 90.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    leaf = bp._parse_log(log)                     # must not raise
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    # cold wall = 90 + 600 = 690 (the 1.2.3s layer dropped); two well-formed layers clear
    # the 2-step floor, and the undercount is disclosed in the evidence.
    assert leaf["magnitude"]["value"] == round(100 * 600.0 / 690.0, 2)
    assert any("no DONE line" in e for e in leaf["evidence"])


def test_parse_log_buildx_excludes_internal_load_from_the_cold_wall():
    # `[internal] load …` steps (context transfer, metadata) are BuildKit OVERHEAD, not
    # cacheable layers, so they must NOT inflate the cold wall / magnitude denominator -
    # only RUN/FROM/COPY/ADD layers count. A big `[internal] load` must not change the math.
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#1 [internal] load build context",
        "#1 DONE 200.0s",                        # huge overhead - must be ignored
        "#10 [deps 1/1] RUN pkg install -y heavy-toolkit",
        "#10 DONE 180.0s",
        "#14 [server 4/4] RUN cmake --build .",
        "#14 DONE 600.0s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    # cold wall = 180 + 600 = 780 (the 200s [internal] load excluded), so magnitude is
    # 600/780, NOT 600/980 - the overhead step does not dilute the share.
    assert leaf["magnitude"]["value"] == round(100 * 600.0 / 780.0, 2)
    rows = leaf["deeper"][-1]["rows"]
    assert sum(s for _l, s, _d in rows) == 780.0     # bars total the cacheable-layer wall only


# A real serial-pytest job log (trimmed from Opentrons/opentrons robot-server
# integration testing): xdist-2.5.0 in the plugin banner, no `-n`, all 195 tests
# on one worker. The pattern that hit a catalog GAP on the live run.
_PYTEST_SERIAL_LOG = "\n".join([
    "uv run --python 3.12 pytest tests/integration --cov=robot_server "
    "--cov-report term-missing:skip-covered --cov-report xml:coverage.xml",
    "plugins: forked-1.6.0, hypothesis-6.146.0, xdist-2.5.0, decoy-2.2.0, "
    "cov-4.1.0, asyncio-1.3.0, tavern-3.0.2",
    "collected 195 items",
    "tests/integration/http_api/persistence/test_compatibility.py .......  [ 13%]",
    "tests/integration/http_api/runs/test_persistence.py .......           [ 50%]",
    "================== 195 passed, 1 warning in 555.79s (0:09:15) ==================",
])


def test_parse_log_detects_pytest_xdist_installed_but_unused():
    # The deterministic detector for the gap the live Opentrons run hit: xdist is in the
    # plugin banner but the invocation passes no `-n`, so 195 independent tests ran
    # one-by-one on a single worker. Flag-style (no scalar magnitude / drill) like
    # playwright-parallel; the evidence carries the banner + collected + summary lines.
    leaf = bp._parse_log(_PYTEST_SERIAL_LOG)
    assert leaf is not None and leaf["fix_key"] == "pytest-no-xdist"
    assert leaf["magnitude"] is None and leaf["deeper"] == []
    assert any("xdist-2.5.0" in e for e in leaf["evidence"])
    assert any("195 passed" in e for e in leaf["evidence"])


def test_parse_log_pytest_no_xdist_silent_when_workers_are_active():
    # False-fire boundary: the SAME suite actually running under xdist prints per-test
    # worker tags (`[gw0]`) and a `N workers` startup line. With workers active it is
    # already parallel - no finding.
    active = _PYTEST_SERIAL_LOG.replace(
        "collected 195 items",
        "collected 195 items\n8 workers [195 items]") + \
        "\n[gw0] [ 13%] PASSED tests/integration/http_api/x_test.py::test_a"
    assert bp._parse_log(active) is None
    # An explicit `-n auto` ON the pytest command line is also active.
    n_auto = _PYTEST_SERIAL_LOG.replace("pytest tests/integration",
                                        "pytest -n auto tests/integration")
    assert bp._parse_log(n_auto) is None


def test_parse_log_pytest_no_xdist_silent_without_xdist_installed():
    # If xdist isn't in the banner at all, "add `-n auto`" is the wrong (it's not a dep)
    # advice - this detector stays silent and the pole falls to the LLM gap-fill instead.
    no_xdist = _PYTEST_SERIAL_LOG.replace("xdist-2.5.0, ", "")
    assert bp._parse_log(no_xdist) is None


def test_parse_log_pytest_no_xdist_respects_wall_and_item_floors():
    # A fast suite (below the wall floor) isn't worth a worker pool's overhead.
    quick = _PYTEST_SERIAL_LOG.replace("in 555.79s (0:09:15)", "in 9.20s")
    assert bp._parse_log(quick) is None
    # A handful of tests (below the item floor) is a matrix-leg problem, not an xdist one.
    few = _PYTEST_SERIAL_LOG.replace("collected 195 items", "collected 3 items")
    assert bp._parse_log(few) is None


def test_parse_log_pytest_no_xdist_ignores_a_stray_dash_n_elsewhere():
    # Silent-drop guard: a stray `-n` in unrelated log chatter (e.g. `echo -n`, `uname
    # -n`) must NOT be misread as "xdist active" and suppress a real serial finding. The
    # active-check only treats `-n` as workers when it's on a `pytest` command line.
    with_chatter = _PYTEST_SERIAL_LOG.replace(
        "collected 195 items",
        "echo -n 'starting'\nuname -n\ncollected 195 items")
    leaf = bp._parse_log(with_chatter)
    assert leaf is not None and leaf["fix_key"] == "pytest-no-xdist"


def test_parse_log_detects_sequential_playwright():
    log = "\n".join([
        "$ pnpm exec playwright test tests/smoke.spec.ts --project=desktop",
        "$ pnpm exec playwright test tests/navigation.spec.ts",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "playwright-parallel"
    assert leaf["deeper"] == []  # step-level root cause; no further drill


def test_parse_log_detects_sequential_playwright_with_tsx_specs():
    # `.spec.tsx` / `.spec.jsx` are valid Playwright specs (TS/React repos) and must
    # be detected too, not just `.spec.ts`/`.spec.js`.
    log = "\n".join([
        "$ pnpm exec playwright test tests/home.spec.tsx",
        "$ pnpm exec playwright test tests/checkout.spec.jsx",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "playwright-parallel"


def test_playwright_cause_asserts_no_unrendered_timeline_or_worker_pool_shape():
    """Regression (nrwl/nx): detector C fires on >=2 `playwright test <spec>` hits
    ANYWHERE in the joined log with NO step-structure or sequencing check, yet the
    canned MEASURED CAUSE asserted the invocations are 'visible as sequential steps in
    the timeline above' AND 'so they don't share a worker pool'. Neither is derivable
    from the log the detector reads: the invocations commonly live in ONE step (nx: a
    single `Run Checks/Lint/Test/Build` step — the rendered timeline shows one bar, not
    sequential steps), and in an nx monorepo they are separate `nx e2e` targets nx may
    run CONCURRENTLY. The cause must state only what the log establishes and hand the
    scheduling/worker-pool question to the agent — never assert timeline shape the
    report's own timeline can contradict (the R2 rule, applied to the catalog cause)."""
    leaf = {"fix_key": "playwright-parallel", "unit_label": "", "deeper": [],
            "magnitude": None,
            "evidence": ["> playwright test apps/foo-e2e/src/foo.spec.ts",
                         "> playwright test apps/bar-e2e/src/bar.spec.ts"],
            "search": ["playwright test"]}
    # A single-step pole with NO captured timeline — the nrwl/nx shape.
    pole = {"check": "Run Checks/Lint/Test/Build",
            "workflow_file": ".github/workflows/ci.yml", "p50_s": 600.0,
            "dominant_step": "Run Checks/Lint/Test/Build",
            "dominant_p50_s": 560.0, "dominant_share": 0.93,
            "steps": [{"step": "Run Checks/Lint/Test/Build", "category": "test",
                       "p50_s": 560.0}]}
    prompt = bp._build_agent_prompt(
        leaf, pole, [], "https://github.com/nrwl/nx/actions/runs/123",
        "nrwl/nx", "abc1234", 5, 20, timeline=None)
    cause = prompt.split("THE MEASURED CAUSE", 1)[1].split("WHERE TO LOOK", 1)[0]
    lc = cause.lower()
    assert "in the timeline above" not in lc, (
        "MEASURED CAUSE points at a timeline the pole doesn't render (R2 violation)")
    assert "sequential steps in the timeline" not in lc
    # The over-claim was that the invocations definitively DON'T share a worker pool.
    # Referencing the worker pool as an open question the agent must confirm is fine;
    # ASSERTING they don't share one is not.
    assert "don't share a worker pool" not in lc
    assert "do not share a worker pool" not in lc
    # The `constraints`/`deliver` blocks were reworded too (they no longer presuppose serial
    # execution) - assert the over-claims are gone from the WHOLE prompt, not just the cause
    # slice, so a regression that moved the phrasing into another block is still caught.
    lp = prompt.lower()
    assert "in the timeline above" not in lp
    assert "don't share a worker pool" not in lp
    assert "do not share a worker pool" not in lp


# A real Lint pole shape (generalized from a captured gap): a `bun run lint` step that
# shells to a bare `$ eslint` with no `--cache`, followed by a whole-graph `knip` pass in
# the same job. eslint prints no duration, so the finding is the missing-flag, not a wall.
_ESLINT_NO_CACHE_LOG = "\n".join([
    "##[group]Run bun run lint",
    "bun run lint",
    "$ eslint",
    "##[group]Run bun run lint:unused",
    "$ knip --include files,dependencies,unlisted",
])


def test_parse_log_detects_eslint_without_cache():
    # The deterministic detector for the Lint gap: an `eslint` invocation with no
    # `--cache`, so it re-lints the whole tree every run. Flag-style (no scalar magnitude
    # / drill) like playwright-parallel; the evidence carries the invocation line.
    leaf = bp._parse_log(_ESLINT_NO_CACHE_LOG)
    assert leaf is not None and leaf["fix_key"] == "eslint-no-cache"
    assert leaf["magnitude"] is None and leaf["deeper"] == []
    assert any("eslint" in e for e in leaf["evidence"])


def test_parse_log_eslint_silent_when_cache_flag_is_present():
    # False-fire boundary: the SAME step already passing `--cache` (with a
    # `--cache-location`) is persisting its cache — already warm, no finding.
    cached = _ESLINT_NO_CACHE_LOG.replace(
        "$ eslint", "$ eslint --cache --cache-location .eslintcache")
    assert bp._parse_log(cached) is None


def test_parse_log_eslint_silent_on_dependency_lines_only():
    # An install log that merely NAMES eslint as a dependency (`+ eslint@…`,
    # `eslint-plugin-…`, `typescript-eslint@…`) must NOT be misread as an invocation —
    # there is no eslint COMMAND here, so the detector stays silent (no false fire).
    install_only = "\n".join([
        "544 packages installed",
        "+ eslint@10.0.3",
        "+ eslint-plugin-simple-import-sort@12.1.1",
        "+ typescript-eslint@8.57.0",
    ])
    assert bp._parse_log(install_only) is None


def test_parse_log_eslint_fires_via_a_js_runner_invocation():
    # The command can come through a JS runner (`npx`/`pnpm exec`/`bunx`) rather than a
    # bare script echo; a runner-prefixed eslint with no `--cache` still fires.
    runner = "\n".join([
        "##[group]Run npm run lint",
        "npx eslint . --max-warnings 0",
    ])
    leaf = bp._parse_log(runner)
    assert leaf is not None and leaf["fix_key"] == "eslint-no-cache"


def test_parse_log_eslint_fires_via_gha_command_line():
    # The third invocation position: the GHA `[command]…/eslint` echo (e.g. a `run:` step
    # that shells straight to the binary). With no `--cache` it still fires.
    gha = "\n".join([
        "##[group]Run eslint .",
        "[command]/home/runner/work/repo/node_modules/.bin/eslint . --format stylish",
    ])
    leaf = bp._parse_log(gha)
    assert leaf is not None and leaf["fix_key"] == "eslint-no-cache"


def test_parse_log_eslint_fires_when_only_cache_location_present():
    # False-NEGATIVE guard: `--cache-location` ALONE does not enable ESLint caching (the
    # boolean `--cache` must be present), so a tail carrying only `--cache-location` is still
    # a cache MISS and must fire — `--cache(?![\w-])` must not be satisfied by the prefix of
    # `--cache-location`.
    loc_only = _ESLINT_NO_CACHE_LOG.replace(
        "$ eslint", "$ eslint --cache-location .eslintcache")
    leaf = bp._parse_log(loc_only)
    assert leaf is not None and leaf["fix_key"] == "eslint-no-cache"


def test_parse_log_eslint_silent_on_prose_and_output_lines():
    # False-FIRE guard (PR-review): the detector requires a LINE-ANCHORED command marker, so
    # prose / eslint's own output that merely mentions or starts with `eslint` is NOT read as
    # an invocation. Each of these once fired a bogus `eslint-no-cache` finding (and would
    # have stolen the LLM gap-fill, since branch G is the last gate before it).
    for line in (
        "Note: we use bun run eslint to lint the code base.",   # `bun run eslint` mid-prose
        "eslint reported 2 warnings",                           # eslint's own output
        "Running eslint via wrapper...",                        # a banner line
        "/usr/local/bin/eslint",                                # a `which eslint` path line
        "Loaded eslint.config.js from cwd",                     # a config-file mention
    ):
        assert bp._parse_log(line) is None, line


def test_parse_log_eslint_silent_on_script_name_with_colon():
    # `bun run eslint:fix` / `yarn eslint:fix` are SCRIPT names (the real flags live in the
    # hidden script body), not a bare `eslint` invocation — the `(?=\s|$)` boundary after the
    # token rejects the `:` so a misleadingly-named script can't false-fire.
    assert bp._parse_log("yarn eslint:fix") is None
    assert bp._parse_log("bun run eslint:lint") is None


def test_parse_log_eslint_fires_per_invocation_not_collapsed():
    # False-NEGATIVE guard (PR-review): a job that caches ONE eslint run but not another still
    # has a real miss. The detector decides per-invocation and fires on the uncached one — a
    # cached sibling must NOT silence it — and the evidence quotes the UNCACHED line, not the
    # cached one (which would read as proof of a no-cache finding).
    mixed = "\n".join(["$ eslint --cache --cache-location .x packages/a",
                       "$ eslint packages/b"])
    leaf = bp._parse_log(mixed)
    assert leaf is not None and leaf["fix_key"] == "eslint-no-cache"
    assert leaf["evidence"] == ["$ eslint packages/b"]         # the uncached line only
    # Both invocations cached -> no miss -> silent.
    assert bp._parse_log("$ eslint --cache a\n$ eslint --cache b") is None


def test_parse_log_eslint_silent_on_line_continuation():
    # False-POSITIVE guard: a backslash-continued command (`$ eslint \` then `--cache` on the
    # next line) has its `--cache` on a line the single-line match can't see, so the cache
    # state is UNKNOWN — fall through to the gap-fill rather than assert a (wrong) miss.
    wrapped = "\n".join(["$ eslint \\", "    --cache \\", "    --cache-location .x \\", "    ."])
    assert bp._parse_log(wrapped) is None


# A serial `cargo test` (libtest) suite shape: each binary prints a `Running <name>
# (target/…/deps/<name>-<hash>)` header then a `test result: ok. N passed; … finished
# in <secs>s`, binaries one after another. Generalized from a captured gap (no repo
# text): two heavy binaries dominate, a long tail is sub-second, plus the lib + an empty
# `0 passed … 0.00s` binary that must NOT count as work.
_CARGO_SERIAL_LOG = "\n".join([
    "     Running unittests src/lib.rs (target/debug/deps/lib-aaaa)",
    "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s",
    "     Running tests/heavy_one.rs (target/debug/deps/heavy_one-bbbb)",
    "test result: ok. 23 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 228.00s",
    "     Running tests/heavy_two.rs (target/debug/deps/heavy_two-cccc)",
    "test result: ok. 9 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 78.00s",
    "     Running tests/mid.rs (target/debug/deps/mid-dddd)",
    "test result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 36.00s",
    "     Running tests/small.rs (target/debug/deps/small-eeee)",
    "test result: ok. 14 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.40s",
])


def test_parse_log_detects_serial_cargo_test_suite():
    # The deterministic detector for the long-serial-cargo-test gap: many libtest binaries
    # run one after another in a single job, a couple of them dominate. Magnitude is the
    # slowest binary's share of the summed serial wall.
    leaf = bp._parse_log(_CARGO_SERIAL_LOG)
    assert leaf is not None and leaf["fix_key"] == "cargo-test-shard"
    # 4 real binaries (the `0.00s` empty one is excluded as non-work).
    assert "4 `cargo test` binaries" in leaf["unit_label"]
    total = 228.0 + 78.0 + 36.0 + 0.40
    assert leaf["magnitude"]["value"] == round(100 * 228.0 / total, 2)  # ~66%
    assert leaf["magnitude"]["unit"] == "%"
    # One drill level; the slowest binary is the first row and carries the BIGGEST LEVER.
    assert len(leaf["deeper"]) == 1
    rows = leaf["deeper"][0]["rows"]
    assert "heavy_one" in rows[0][0] and abs(rows[0][1] - 228.0) < 0.01
    assert leaf["evidence"]


def test_parse_log_cargo_test_tail_row_keeps_drill_shares_summing_to_the_wall():
    # >6 binaries fold into one VALUED tail row so the `pct_of: "sum"` bars still total
    # the serial wall (a None tail would drop out of the denominator and inflate shares).
    many = _CARGO_SERIAL_LOG + "\n" + "\n".join(
        f"     Running tests/x{i}.rs (target/debug/deps/x{i}-{i:04d})\n"
        f"test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
        f"finished in {2.0 + i}s" for i in range(4))
    leaf = bp._parse_log(many)
    assert leaf is not None and leaf["fix_key"] == "cargo-test-shard"
    rows = leaf["deeper"][0]["rows"]
    assert rows[-1][0].startswith("…") and "more test binaries" in rows[-1][0]
    valued = sum(r[1] for r in rows if isinstance(r[1], (int, float)))
    total = 228.0 + 78.0 + 36.0 + 0.40 + sum(2.0 + i for i in range(4))
    assert abs(valued - total) < 0.01  # bars total the serial wall


def test_parse_log_cargo_test_silent_on_a_small_or_quick_suite():
    # False-fire boundary: a handful of binaries (below the binary floor) is a matrix-leg
    # problem, not a sharding one.
    few = "\n".join([
        "     Running tests/a.rs (target/debug/deps/a-1111)",
        "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 90.00s",
        "     Running tests/b.rs (target/debug/deps/b-2222)",
        "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 60.00s",
    ])
    assert bp._parse_log(few) is None
    # Enough binaries but a quick suite (below the wall floor) isn't worth sharding.
    quick = "\n".join(
        f"     Running tests/q{i}.rs (target/debug/deps/q{i}-{i:04d})\n"
        f"test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
        f"finished in 1.00s" for i in range(6))
    assert bp._parse_log(quick) is None


def test_parse_log_detects_benchmark_serial_reruns():
    # The deterministic detector for the repeated-iteration benchmark gap: a benchmark
    # harness invoked with `--runs N` reruns its whole op suite N times in sequence.
    # Magnitude is N (the serial-rerun multiplier), unit "x".
    log = "\n".join([
        "   Running `target/release/benchmarks benchmark --url postgresql://x "
        "--dataset stuff --runs 10 --output json`",
        "Results: [cold: 91.0 ] [22.1, 22.2, 22.1, 22.0, 22.3, 22.2, 22.1, 22.0, 22.2, 22.1]",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "benchmark-serial-reruns"
    assert leaf["magnitude"]["value"] == 10 and leaf["magnitude"]["unit"] == "x"
    assert leaf["deeper"] == []
    assert any("--runs" in e for e in leaf["evidence"])


def test_parse_log_benchmark_reruns_accepts_synonym_flags_and_takes_the_max():
    # `--iterations`/`--samples`/`--repeat` are accepted synonyms; the magnitude is the
    # largest configured rerun count across the invocations in the step.
    log = "\n".join([
        "$ ./bench run --iterations 5 suite-a",
        "$ ./bench run --iterations 12 suite-b",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "benchmark-serial-reruns"
    assert leaf["magnitude"]["value"] == 12


def test_parse_log_benchmark_reruns_respects_the_floor_and_requires_a_bench_indicator():
    # A low rerun count (below the floor) is a smoke benchmark that needs its repetition.
    assert bp._parse_log("   Running `target/release/benchmarks benchmark --runs 2`") is None
    # `--runs` on a NON-benchmark command line must not fire (the `\bbench` anchor is
    # required on the same line) - this is the false-fire guard against a stray flag.
    assert bp._parse_log("$ ./migrate apply --runs 50 --verbose") is None


# A multi-module Android instrumentation suite (`./gradlew connectedCheck`) run serially on
# one emulator: Gradle prints a per-module `> Task :<module>:connected[<Variant>]AndroidTest`
# (plus the module's own `connectedAndroidTest`/`connectedCheck` aggregates), then a
# `BUILD SUCCESSFUL in <wall>` summary. Generalized from a captured gap with synthetic
# generic module names (no repo text); GHA timestamps + an ANSI-coloured command echo are
# included so the timestamp/ANSI stripping is exercised.
_ANDROID_CONNECTED_LOG = "\n".join([
    "2026-01-01T00:00:00.0000000Z \x1b[36;1m./gradlew connectedCheck --stacktrace\x1b[0m",
    "2026-01-01T00:00:10.0000000Z > Task :app:connectedDebugAndroidTest",
    "2026-01-01T00:00:10.1000000Z > Task :app:connectedAndroidTest",
    "2026-01-01T00:00:10.2000000Z > Task :app:connectedCheck",
    "2026-01-01T00:01:30.0000000Z > Task :core:connectedDebugAndroidTest",
    "2026-01-01T00:01:30.1000000Z > Task :core:connectedAndroidTest",
    "2026-01-01T00:01:30.2000000Z > Task :core:connectedCheck",
    "2026-01-01T00:02:40.0000000Z > Task :network:connectedDebugAndroidTest",
    "2026-01-01T00:02:40.1000000Z > Task :network:connectedAndroidTest",
    "2026-01-01T00:02:40.2000000Z > Task :network:connectedCheck",
    "2026-01-01T00:03:55.0000000Z Starting 6 tests on emulator-5554",
    "2026-01-01T00:04:02.0000000Z Finished 6 tests on emulator-5554",
    "2026-01-01T00:05:35.0000000Z BUILD SUCCESSFUL in 5m 35s",
    "2026-01-01T00:05:35.0000001Z 1576 actionable tasks: 1051 executed, 525 from cache",
])


def test_parse_log_detects_serial_android_emulator_suite():
    # The deterministic detector for the connectedCheck-on-one-emulator gap: each module's
    # androidTest runs serially on a single device. Flag-style like playwright-parallel - the
    # cost is the serialization, not a single percentage - and the module count is surfaced
    # in the unit label (3 distinct modules; the per-module `connectedAndroidTest`/
    # `connectedCheck` aggregates do NOT inflate the count).
    leaf = bp._parse_log(_ANDROID_CONNECTED_LOG)
    assert leaf is not None and leaf["fix_key"] == "android-emulator-shard"
    assert "3 modules" in leaf["unit_label"]
    assert leaf["magnitude"] is None and leaf["deeper"] == []
    assert leaf["fix_key"] in bp._FIX_META
    assert leaf["evidence"]


def test_parse_log_android_emulator_respects_module_and_wall_floors():
    # False-fire boundary 1: a single-module connected run is a matrix-leg problem, not a
    # device-parallelism one - below the module floor, no finding.
    one_module = "\n".join([
        "> Task :app:connectedDebugAndroidTest",
        "> Task :app:connectedAndroidTest",
        "> Task :app:connectedCheck",
        "BUILD SUCCESSFUL in 4m 00s",
    ])
    assert bp._parse_log(one_module) is None
    # False-fire boundary 2: enough modules but a quick build (below the wall floor) isn't
    # worth the per-shard AVD-boot overhead.
    quick = "\n".join([
        "> Task :app:connectedAndroidTest",
        "> Task :core:connectedAndroidTest",
        "> Task :network:connectedAndroidTest",
        "BUILD SUCCESSFUL in 45s",
    ])
    assert bp._parse_log(quick) is None


# A single un-sharded JVM Gradle `:test` task that gates a long build: one
# `> Task :<module>:test` line, then a `BUILD SUCCESSFUL in <wall>` over the floor.
# Synthetic generic module name (no repo text).
_GRADLE_TEST_LOG = "\n".join([
    "2026-01-01T00:00:00.0000000Z \x1b[36;1m./gradlew :integration-tests:test --stacktrace\x1b[0m",
    "2026-01-01T00:00:02.0000000Z Calculating task graph as no cached configuration is available",
    "2026-01-01T00:01:00.0000000Z > Task :integration-tests:testClasses",
    "2026-01-01T00:01:20.0000000Z > Task :integration-tests:test",
    "2026-01-01T00:17:06.0000000Z BUILD SUCCESSFUL in 17m 6s",
    "2026-01-01T00:17:06.0000001Z 1026 actionable tasks: 637 executed, 389 from cache",
])


def test_parse_log_detects_single_serial_gradle_test_task():
    # The deterministic detector for the un-sharded `:test`-task gap: one serial Gradle test
    # task gates a long build. Flag-style; `:testClasses` must NOT be miscounted as a `:test`
    # task, and the instrumentation detector (J) must NOT fire (no connectedAndroidTest).
    leaf = bp._parse_log(_GRADLE_TEST_LOG)
    assert leaf is not None and leaf["fix_key"] == "gradle-test-parallelism"
    assert leaf["magnitude"] is None and leaf["deeper"] == []
    assert leaf["fix_key"] in bp._FIX_META
    assert leaf["evidence"]


def test_parse_log_gradle_test_also_fires_on_android_unit_test_task():
    # The Android JVM unit-test task name (`:test<Variant>UnitTest`) is recognized too.
    log = "\n".join([
        "> Task :core:testDebugUnitTest",
        "BUILD SUCCESSFUL in 8m 30s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "gradle-test-parallelism"


def test_parse_log_detectors_handle_nested_gradle_module_paths():
    # NESTED module paths (`:feature:login:…`, `:libraries:network:test`) are common in the
    # large multi-module Android/Gradle builds these detectors target — the capture must
    # span multiple `:`-segments, not just one, or the modules go uncounted (false negative).
    android = "\n".join([
        "./gradlew connectedCheck",
        "> Task :feature:login:connectedDebugAndroidTest",
        "> Task :feature:profile:connectedDebugAndroidTest",
        "> Task :libraries:network:connectedDebugAndroidTest",
        "BUILD SUCCESSFUL in 4m 00s",
    ])
    leaf = bp._parse_log(android)
    assert leaf is not None and leaf["fix_key"] == "android-emulator-shard"
    assert "3 modules" in leaf["unit_label"]   # all three nested modules counted

    jvm = "\n".join([
        "./gradlew :libraries:network:test",
        "> Task :libraries:network:test",
        "> Task :libraries:network:testClasses",   # must NOT be miscounted as a :test task
        "BUILD SUCCESSFUL in 6m 00s",
    ])
    leaf = bp._parse_log(jvm)
    assert leaf is not None and leaf["fix_key"] == "gradle-test-parallelism"


def test_parse_log_gradle_test_respects_the_wall_floor():
    # False-fire boundary: a quick JVM test build (below the wall floor) isn't worth forking.
    quick = "\n".join([
        "> Task :app:test",
        "BUILD SUCCESSFUL in 40s",
    ])
    assert bp._parse_log(quick) is None


def test_parse_log_gradle_detectors_are_mutually_exclusive():
    # An instrumentation suite (connectedAndroidTest tasks present) is classified by the
    # emulator detector (J), never by the JVM `:test` detector (K) - even though a long
    # connectedCheck build also clears K's wall floor.
    leaf = bp._parse_log(_ANDROID_CONNECTED_LOG)
    assert leaf is not None and leaf["fix_key"] == "android-emulator-shard"
    # And a JVM `:test` build has no connectedAndroidTest tasks, so J stays silent and K owns it.
    leaf2 = bp._parse_log(_GRADLE_TEST_LOG)
    assert leaf2 is not None and leaf2["fix_key"] == "gradle-test-parallelism"


def test_gradle_build_secs_parses_every_unit():
    assert bp._gradle_build_secs("BUILD SUCCESSFUL in 17m 6s") == 17 * 60 + 6
    assert bp._gradle_build_secs("BUILD SUCCESSFUL in 5m 35s") == 5 * 60 + 35
    assert bp._gradle_build_secs("BUILD SUCCESSFUL in 45s") == 45
    assert bp._gradle_build_secs("BUILD SUCCESSFUL in 1h 2m 3s") == 3600 + 120 + 3
    assert bp._gradle_build_secs("BUILD SUCCESSFUL in 800ms") == 0.8  # ms, not minutes
    assert bp._gradle_build_secs("no build summary here") is None


def test_parse_log_returns_none_for_unrecognized_log():
    assert bp._parse_log("just some unrelated build output\nDone in 3s") is None
    assert bp._parse_log("") is None
    # A vitest run where import does NOT dominate and there's no coverage -> no leaf.
    weak = " Duration  10s (transform 1s, setup 0ms, import 2s, tests 40s, environment 0ms)"
    assert bp._parse_log(weak) is None


# --------------------------------------------------------------------------- #
# _floor_note — the "what a change here can buy (wall-clock)" figure under each
# pole. The render smoke tests use a single-pole doc (no concurrent checks), so
# these exercise the number-producing branches directly.
# --------------------------------------------------------------------------- #

def test_floor_note_non_matrix_buys_down_to_next_concurrent_check():
    pole = {"check": "build", "p50_s": 300.0}
    candidates = [{"name": "build", "p50_s": 300.0},
                  {"name": "lint", "p50_s": 120.0}]
    note = "\n".join(bp._floor_note(pole, candidates))
    # Cutting the gate helps 1:1 only down to the next concurrent check (120s),
    # so the payoff is 300 - 120 = 180s.
    assert "up to **~3m 00s**" in note
    assert "`lint` (2m 00s)" in note
    assert "the gate moves" in note


def test_floor_note_matrix_leg_distinguishes_one_leg_from_the_whole_matrix():
    pole = {"check": "tests-web (pg12)", "p50_s": 300.0}
    candidates = [{"name": "tests-web (pg12)", "p50_s": 300.0},
                  {"name": "tests-web (pg15)", "p50_s": 290.0},  # sibling leg
                  {"name": "lint", "p50_s": 100.0}]              # next non-leg
    note = "\n".join(bp._floor_note(pole, candidates))
    # Speeding ONE leg only saves down to the next leg (300 - 290 = 10s)...
    assert "this one leg** saves only ~10s" in note
    assert "`tests-web (pg15)`" in note
    # ...but a shared-config fix drops the whole matrix toward the next non-leg
    # check `lint` (100s), for up to 300 - 100 = 200s.
    assert "drops the whole matrix" in note
    assert "`lint` (1m 40s)" in note
    assert "**~3m 20s**" in note


def test_same_matrix_prefix_suffix_and_length_guard():
    # Real legs (share a long prefix/suffix, near-equal length) -> same matrix.
    assert bp._same_matrix("tests-web (node24, pg12, mode)", "tests-web (node24, pg15, mode)")
    assert bp._same_matrix("prisma-adapter Integration Test", "drizzle-adapter Integration Test")
    # A leg whose dimension adds ONE token is still a leg (kysely-prisma vs prisma).
    assert bp._same_matrix("@better-auth-test/prisma-adapter Integration Test",
                           "@better-auth-test/kysely-prisma-adapter Integration Test")
    # Length guard: a distinct, much shorter check that only shares a generic tail
    # ("integration test", 2 tokens vs 4) is NOT a matrix leg - otherwise it would be
    # wrongly dropped from the wall-clock floor if it sat between the gate and the true
    # floor. (A one-token difference like kysely-prisma vs prisma is indistinguishable
    # from a real leg by name alone and is intentionally still grouped, above.)
    assert not bp._same_matrix("prisma-adapter Integration Test", "Integration test")
    # Same length, exactly one differing token (>=3 tokens) is a single-dimension
    # matrix - catches the node-version legs the prefix/suffix rule misses.
    assert bp._same_matrix("test (22.x)", "test (24.x)")
    assert not bp._same_matrix("lint", "test")          # too short / fully different


def test_same_matrix_cross_workflow_name_collision_is_not_one_matrix():
    # GitHub gives same-named jobs in DIFFERENT workflows identical check-run names: a
    # `name: Python ${{ matrix.python }}` job in both datasets-test.yml and
    # framework-test.yml surfaces as `Python 3.13`. Name similarity alone would fold them
    # into one matrix; with the producing workflow files known they are NOT the same matrix.
    assert not bp._same_matrix("Python 3.13", "Python 3.11",
                               ".github/workflows/framework-test.yml",
                               ".github/workflows/datasets-test.yml")
    # Even an IDENTICAL name across two workflows is a collision, not a shared matrix.
    assert not bp._same_matrix("Python 3.13", "Python 3.13",
                               ".github/workflows/framework-test.yml",
                               ".github/workflows/datasets-test.yml")
    # Same workflow file (path or bare base) -> still a matrix, as before.
    assert bp._same_matrix("Python 3.13", "Python 3.11",
                           ".github/workflows/datasets-test.yml",
                           "datasets-test.yml")
    # An unknown side falls back to name-only matching (existing call sites unchanged).
    assert bp._same_matrix("Python 3.13", "Python 3.11", "", "")
    assert bp._same_matrix("Python 3.13", "Python 3.11")


def test_floor_note_does_not_fold_two_workflows_into_one_matrix():
    # flwrlabs/flower: datasets-test.yml ('Datasets') and framework-test.yml ('Framework
    # Python') BOTH define a python matrix named `Python ${{ matrix.python }}`. The
    # 623.5s `Python 3.13` pole is framework-test's job — NOT a leg of datasets-test's
    # `Python 3.11`/`3.12`. The floor note must not claim "the legs share one job config"
    # by mixing the two workflows' jobs into one matrix.
    pole = {"check": "Python 3.13", "p50_s": 623.5,
            "workflow_file": ".github/workflows/framework-test.yml"}
    candidates = [
        {"name": "Python 3.13", "p50_s": 623.5,
         "workflow_file": ".github/workflows/framework-test.yml"},
        {"name": "Python 3.11", "p50_s": 500.0,
         "workflow_file": ".github/workflows/datasets-test.yml"},
        {"name": "Python 3.12", "p50_s": 480.0,
         "workflow_file": ".github/workflows/datasets-test.yml"},
        {"name": "Lint", "p50_s": 100.0,
         "workflow_file": ".github/workflows/lint.yml"},
    ]
    note = "\n".join(bp._floor_note(pole, candidates))
    # framework-test's Python 3.13 has no sibling leg in its OWN workflow, so it must use
    # the plain single-check floor framing, never the multi-leg "share one job config" one.
    assert "share one job config" not in note
    assert "matrix legs run" not in note
    # The honest plain framing floors against the next concurrent check directly.
    assert "it gates until it drops to the next concurrent check" in note


def test_floor_note_excludes_a_distinct_shorter_check_sharing_a_generic_tail():
    # `Integration test` (a standalone job) shares the "integration test" tail with the
    # gating matrix pole but is NOT a leg - it must count as the floor, not be grouped
    # away as a sibling leg (the _same_matrix length-guard regression).
    pole = {"check": "prisma-adapter Integration Test", "p50_s": 500.0}
    candidates = [{"name": "prisma-adapter Integration Test", "p50_s": 500.0},
                  {"name": "drizzle-adapter Integration Test", "p50_s": 480.0},  # real leg
                  {"name": "Integration test", "p50_s": 300.0}]                  # distinct
    note = "\n".join(bp._floor_note(pole, candidates))
    # The whole-matrix drop floors against `Integration test` (the next NON-leg), 500-300.
    assert "`Integration test` (5m 00s)" in note
    assert "**~3m 20s**" in note


def test_floor_note_empty_for_a_non_gating_pole():
    # A pole that runs BEHIND a slower concurrent check isn't the gate, so cutting it
    # buys ~0 merge wait until that check is cut - emit NO "what a change can buy"
    # note (the old code printed incoherent prose naming the slower check as a floor,
    # and falsely matrix-grouped suffix-colliding names like `Smoke test (22.x)`).
    pole = {"check": "test (22.x)", "p50_s": 268.0}
    candidates = [{"name": "test (22.x)", "p50_s": 268.0},
                  {"name": "Smoke test (22.x)", "p50_s": 120.0},
                  {"name": "prisma-adapter Integration Test", "p50_s": 425.0}]
    assert bp._floor_note(pole, candidates) == []


def test_floor_note_empty_when_nothing_concurrent_to_floor_against():
    # A lone pole with no other concurrent check has no wall-clock floor to quote.
    assert bp._floor_note({"check": "build", "p50_s": 300.0},
                          [{"name": "build", "p50_s": 300.0}]) == []


def test_floor_note_floors_against_universal_bimodal_high_not_minority_blended():
    # kernel/kernel-images class. The `test` pole (682s) runs concurrently with:
    #   - `build-headful`: present on EVERY sampled PR (20/20), blended p50 only 150s but
    #     flagged BIMODAL with a 356.5s HIGH mode (~47% of PRs) - so on the slow-mode PRs IT
    #     is the real concurrent floor, at 356.5s.
    #   - `Cursor Bugbot`: a managed/external check (no workflow_file, suppressed from the
    #     long-pole list) present on only 13/20 PRs, blended 198s.
    # The bug: the floor ranked siblings by BARE p50, so it floored against Cursor Bugbot
    # (198s > build-headful's blended 150s) and over-promised 682-198 = ~8m 04s - using a
    # minority-presence MANAGED check the report won't even render as a pole. The faithful
    # floor is the universal build-headful at its EFFECTIVE (bimodal-high) 356.5s, so the
    # honest ceiling is 682-356.5 = ~5m 26s.
    pole = {"check": "test", "p50_s": 682.0,
            "workflow_file": ".github/workflows/ci.yml"}
    candidates = [
        {"name": "test", "p50_s": 682.0, "present_on": 20,
         "workflow_file": ".github/workflows/ci.yml"},
        {"name": "build-headful", "p50_s": 150.0, "present_on": 20,
         "workflow_file": ".github/workflows/ci.yml",
         "bimodal": {"high_p50_s": 356.5, "low_p50_s": 90.0, "slow_frac": 0.47}},
        {"name": "Cursor Bugbot", "p50_s": 198.0, "present_on": 13},  # managed, no wf file
    ]
    note = "\n".join(bp._floor_note(pole, candidates))
    # Floors against the universal bimodal-high sibling, NOT the minority managed check.
    assert "`build-headful` (5m 56s)" in note
    assert "up to **~5m 26s**" in note
    assert "Cursor Bugbot" not in note
    # The executive-summary win must agree with the per-pole floor (no internal contradiction).
    assert bp._pole_addressable(pole, candidates) == 682.0 - 356.5


# --------------------------------------------------------------------------- #
# _floor_note / _pole_addressable chain branch (review V2 / OD-F2): a modal-chain
# MEMBER pole (render() stashes `_chain_member`) renders the chain-stage story with
# the win capped at the stamped chain headroom — never the concurrent "next leg"
# arithmetic that overstated deepgram's poles (~28s rendered vs the 5.0s stamp).
# --------------------------------------------------------------------------- #

_DEEPGRAM_LABEL = "`compile (3.10)` → `test (3.13)`"


def _chain_member_pole(p50=66.0, win=5.0, stage=2):
    return {"check": "test (3.13)", "p50_s": p50,
            "workflow_file": ".github/workflows/ci.yml",
            "_chain_member": {"win_s": win, "stage": stage, "len": 2,
                              "label": _DEEPGRAM_LABEL}}


_CHAIN_CANDIDATES = [
    {"name": "test (3.13)", "p50_s": 66.0,
     "workflow_file": ".github/workflows/ci.yml"},
    {"name": "test (3.10)", "p50_s": 54.0,          # sibling matrix leg
     "workflow_file": ".github/workflows/ci.yml"},
    {"name": "Title Check", "p50_s": 14.0,
     "workflow_file": ".github/workflows/title.yml"},
]


def test_floor_note_chain_member_caps_win_at_stamped_chain_headroom():
    # The deepgram pole-1 shape (plan cell 2): the old note said "~28s"; the
    # stamped chain headroom is 5.0s. The chain-stage story replaces the
    # matrix/next-leg framing entirely.
    pole = _chain_member_pole()
    note = "\n".join(bp._floor_note(pole, _CHAIN_CANDIDATES))
    assert "up to **~5s**" in note
    assert "stage 2/2" in note and "gate chain" in note
    assert "next leg" not in note and "matrix" not in note
    # Never-disagree: the executive-summary win quotes the same capped figure.
    assert bp._pole_addressable(pole, _CHAIN_CANDIDATES) == 5.0


def test_floor_note_chain_predecessor_gets_its_stage_framing():
    # Plan cell 3: a chain PREDECESSOR (the compile shape) is never framed as a
    # concurrent sibling — it gets its own stage position.
    pole = {"check": "compile (3.10)", "p50_s": 38.0,
            "workflow_file": ".github/workflows/ci.yml",
            "_chain_member": {"win_s": 5.0, "stage": 1, "len": 2,
                              "label": _DEEPGRAM_LABEL}}
    candidates = [{"name": "compile (3.10)", "p50_s": 38.0,
                   "workflow_file": ".github/workflows/ci.yml"},
                  {"name": "Title Check", "p50_s": 14.0,
                   "workflow_file": ".github/workflows/title.yml"}]
    note = "\n".join(bp._floor_note(pole, candidates))
    assert "stage 1/2" in note and "up to **~5s**" in note
    assert "next leg" not in note
    assert bp._pole_addressable(pole, candidates) == 5.0


def test_chain_member_gate_prompt_line_uses_chain_framing_not_slowest_typical():
    # Issue #112 (secondary): a modal-CHAIN member's agent-prompt "THE GATE" line must NOT read
    # "Slowest check a typical PR waits on: P50 37s" — a mid-chain serial `needs:` stage is not the
    # slowest single check a typical PR waits on; the CHAIN is. It frames the stage against the chain
    # total (`merge_dur`) instead. The pole carries render()'s `_chain_member` stash incl. `merge_dur`.
    pole = {"check": "Prepare dependencies (3.14.5)", "p50_s": 37.0,
            "workflow_file": ".github/workflows/ci.yaml",
            "_chain_member": {"win_s": 5.0, "stage": 2, "len": 3,
                              "label": "the chain", "merge_dur": "1m 56s"}}
    line = bp._pole_gate_prompt_claim(
        "Prepare dependencies (3.14.5)", "ci.yaml", "37s",
        gate_count=18, npop=18, pole=pole, cs=None)
    assert "Stage 2/3 of the `needs:` gate chain (chain P50 1m 56s)" in line
    assert "this stage: P50 37s" in line
    assert "Slowest check a typical PR waits on" not in line
    # A non-chain pole is unaffected — it keeps the typical-gate line.
    plain = bp._pole_gate_prompt_claim(
        "lint", "ci.yaml", "37s", gate_count=18, npop=18, pole={"check": "lint"}, cs=None)
    assert "Slowest check a typical PR waits on: P50 37s" in plain


def test_floor_note_non_member_pole_unchanged_on_chain_bearing_repo():
    # Plan cell 4: a pole that is NOT a chain member renders today's note
    # byte-identically even when the repo has a chain (no `_chain_member` stash).
    pole = {"check": "docs", "p50_s": 300.0,
            "workflow_file": ".github/workflows/docs.yml"}
    candidates = [{"name": "docs", "p50_s": 300.0,
                   "workflow_file": ".github/workflows/docs.yml"},
                  {"name": "lint", "p50_s": 120.0,
                   "workflow_file": ".github/workflows/lint.yml"}]
    note = "\n".join(bp._floor_note(pole, candidates))
    assert "up to **~3m 00s**" in note and "the gate moves" in note
    assert "gate chain" not in note


def test_floor_note_chain_member_zero_headroom_says_so():
    # Plan cell 6: co-longest chains net the per-PR headroom to 0 (the runner-up
    # re-walk counts the tied path at full span — the both-counted rule). The
    # note must not quote a positive win.
    pole = _chain_member_pole(win=0.0)
    note = "\n".join(bp._floor_note(pole, _CHAIN_CANDIDATES))
    assert "~0s" in note and "competing path of comparable length" in note
    assert "up to **~" not in note
    assert bp._pole_addressable(pole, _CHAIN_CANDIDATES) == 0.0


def test_floor_note_chain_member_win_capped_by_own_span():
    # A stage can't give back more time than it runs: headroom 100s over a 66s
    # member caps at the member's own span (the wall_clock member-span bound).
    pole = _chain_member_pole(win=100.0)
    note = "\n".join(bp._floor_note(pole, _CHAIN_CANDIDATES))
    assert "up to **~1m 06s**" in note and "own span" in note
    assert bp._pole_addressable(pole, _CHAIN_CANDIDATES) == 66.0


def test_floor_note_chain_member_suppressed_pole_stays_suppressed():
    # Plan cell 7: the suppression guards run BEFORE the chain branch — a pole
    # whose note is suppressed today (a slower universal concurrent blocker)
    # stays suppressed, chain member or not.
    pole = _chain_member_pole()
    candidates = _CHAIN_CANDIDATES + [
        {"name": "Enterprise Gate", "p50_s": 400.0,
         "workflow_file": ".github/workflows/gate.yml"}]
    assert bp._floor_note(pole, candidates) == []
    assert bp._pole_addressable(pole, candidates) == 0.0


def test_floor_and_addressable_count_a_universal_managed_blocker():
    # OneSignal-Android-SDK class — the OVER-statement guard. The `build` pole (619s, file-backed)
    # runs concurrently with a UNIVERSAL managed check `Claude Code Review` (no workflow_file,
    # present on EVERY PR, 944s > the pole) plus a smaller file-backed sibling (300s). Cutting
    # `build` buys ~0 merge wait — the universal managed check already gates the merge at 944s.
    # The regression to guard: flooring only against FILE-BACKED siblings floored at the 300s
    # sibling and over-promised 619−300 = 319s of a win that does not exist. A universal
    # managed/external blocker must CAP the win (and trip the non-gate guard) even though it is
    # never NAMED as the floor (the reader can't tune it).
    pole = {"check": "build", "p50_s": 619.0, "workflow_file": ".github/workflows/ci.yml"}
    candidates = [
        {"name": "build", "p50_s": 619.0, "present_on": 20, "workflow_file": ".github/workflows/ci.yml"},
        {"name": "Claude Code Review", "p50_s": 944.0, "present_on": 20},   # managed, no wf, universal, slower
        {"name": "lint", "p50_s": 300.0, "present_on": 20, "workflow_file": ".github/workflows/ci.yml"},
    ]
    assert bp._floor_note(pole, candidates) == [], "pole behind a universal managed blocker → no over-stated win"
    assert bp._pole_addressable(pole, candidates) == 0.0, "headline credits 0 — the managed check gates the merge"
    # When the managed check is BELOW the pole but ABOVE the file sibling, the win is capped at
    # pole − managed (119s), NOT pole − file sibling (319s).
    cand2 = [dict(c) for c in candidates]
    cand2[1]["p50_s"] = 500.0
    assert bp._pole_addressable(pole, cand2) == 619.0 - 500.0, "win capped by the universal managed check, not the file sibling"
    # Prose coherence: when the managed check caps the win ABOVE the named file floor, the note
    # names the managed check (the real next gate), NOT the faster file sibling — else the quoted
    # number (619−500) wouldn't reconcile with a "drops to lint (300s)" clause.
    note2 = "\n".join(bp._floor_note(pole, cand2))
    assert "Claude Code Review" in note2 and "`lint`" not in note2, "names the managed cap, not the faster file floor"
    assert "up to **~1m 59s**" in note2   # 619 − 500 = 119s, capped by the managed check


# --------------------------------------------------------------------------- #
# render() smoke test — the whole pipeline wired together
# --------------------------------------------------------------------------- #

def _doc_one_pole() -> dict:
    return {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "tests-web", "p50_s": 255.0,
                "workflow_file": ".github/workflows/pipeline.yml", "job": "tests-web",
                "dominant_step": "run tests", "dominant_p50_s": 91.0,
                "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0},
                          {"step": "Build", "category": "build", "p50_s": 60.0}],
            }]},
    }


_IMPORT_BOUND_LOG = "\n".join([
    " RUN  v4.1.4 /repo/web",
    " Test Files  149 passed (149)",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, tests 214.54s, environment 8ms)",
])


def test_render_smoke_builds_pole_section_prompt_and_provenance():
    md = bp.render(
        _doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {},
        {"pipeline": "https://github.com/o/r/actions/runs/123"}, "2026-06-08")
    assert "# o/r - why is the merge slow?" in md
    assert "Long pole 1: `pipeline.yml` ▸ `tests-web`" in md
    assert "## 🟠 Long pole 1:" in md   # severity dot (255s gate -> 🟠 medium)
    assert "Where this data comes from" in md          # provenance block
    assert "scanned **2026-06-08**" in md
    assert "one representative run" in md               # drill-down source line
    assert "[run 123]" in md                            # the run is linked
    # RCA-only: a prompt that points the agent at the docs, NOT a prescribed fix.
    assert "Prompt for your coding agent" in md
    assert "does NOT prescribe the fix" in md           # plain text, copy-friendly
    assert "```text" in md                              # the prompt is in a code block
    assert "vitest.dev/guide/improving-performance" in md   # the doc pointer
    assert "```diff" not in md                          # no prescribed code change


def test_render_pr_floor_fallback_demotes_spine_and_does_not_dead_end():
    # External-gate repo: the required suite is all external/managed, so collect_runs
    # synthesized a PR-FLOOR fallback spine (gate_kind=pr_floor_fallback) rather than
    # leaving poles empty. The renderer must drill it (NOT return the dead-end line)
    # AND demote it unmistakably: a demotion banner + a PR-scoped title that never
    # calls the floor "the merge gate".
    doc = _doc_one_pole()
    doc["required_checks"] = ["Enterprise CI/tests", "cla/mattermost", "merge/blocked"]
    cp = doc["pr_critical_path"]
    cp["gate_kind"] = "pr_floor_fallback"
    # The sampler proved the external suite ran on no PR (promoted a recency sample).
    cp["required_suite_unsatisfiable"] = True
    cp["required_suite_scoped"] = False
    cp["poles"][0]["pr_floor_fallback"] = True
    cp["checks"] = [{"name": "tests-web", "p50_s": 255.0}]
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"}, "2026-06-08")
    assert "_No measured critical path" not in md           # did NOT dead-end
    assert "why is CI slow on a PR?" in md                  # PR-scoped title, not "merge"
    assert "why is the merge slow?" not in md
    assert "[!IMPORTANT]" in md and "measured PR-floor" in md   # demotion banner present
    assert "not** the branch-protection gate" in md        # banner disclaims the real gate
    assert "Enterprise CI/tests" in md                      # names the external required checks
    assert "ran on the sampled PRs" in md                   # banner says the suite was absent, not unread
    assert "Long pole 1: `pipeline.yml` ▸ `tests-web`" in md  # still drills the floor pole
    assert "gate your merge" not in md                      # TOC framing demoted too, no contradiction
    assert "set your PR-floor" in md
    # P2 regression: an external-but-READ suite must NOT be reported as unreadable.
    assert "Required checks were unreadable" not in md
    assert "No file-backed required gate" in md             # the correct provenance line


def test_render_pr_floor_banner_absent_on_a_normal_gate_scoped_report():
    # Guard: a normal report (a real file-backed gate, no gate_kind) must NOT show the
    # PR-floor demotion banner or the PR-scoped title.
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {}, "2026-06-08")
    assert "why is the merge slow?" in md
    assert "measured PR-floor" not in md
    assert "PR-floor" not in md


def test_all_managed_gate_bottom_line_does_not_promise_missing_drilldowns():
    # Regression: when every gating check is managed/external (nwf==0, no file-backed
    # pole), the lead correctly says "there is no workflow to drill or diff" and the
    # report contains ZERO per-pole "## Long pole" sections. The Bottom line must agree
    # — it must NOT direct the reader to "the per-pole drill-downs below" that don't exist.
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "required_checks": ["Enterprise CI/tests", "cla/bot"],
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "gate_kind": "pr_floor_fallback",
            "poles": [{"check": "Enterprise CI/tests", "p50_s": 600.0,
                       "workflow_file": "", "job": ""}],
        },
    }
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    # Precondition: this really is the all-managed / no-drill-down case.
    assert "there is no workflow to drill or diff" in md
    # Emoji-agnostic: the renderer emits 🔴/🟠/🟡 severity dots, so match ANY pole heading
    # (mirrors verify_report._pole_sections) rather than enumerating two of the three glyphs.
    assert not re.search(r"^##\s+.*Long pole \d+:", md, re.MULTILINE)
    # The bug: the Bottom line promised drill-downs that the report does not contain.
    bottom = next(ln for ln in md.splitlines() if "**Bottom line.**" in ln)
    assert "per-pole drill-downs below" not in bottom
    assert "drill-down" not in bottom


def _prov_doc(**cp_overrides) -> dict:
    """Minimal doc for exercising `_provenance_block` directly."""
    cp = {"sampled_pr_count": 20, "sample_target": 20, "sample_complete": True}
    cp.update(cp_overrides)
    return {
        "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": cp,
    }


def test_provenance_surfaces_the_gh_call_cost():
    # The data-collection cost (gh call count + time) must be visible in the report
    # so "why did this take a while" is answerable from the artifact, not a guess.
    doc = _prov_doc()
    doc["data_sources"]["gh_query_count"] = 837
    doc["data_sources"]["gh_error_count"] = 0
    doc["timings"] = {"scripted_total_s": 191.0}
    block = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "837 gh API call(s)" in block
    assert "3m 11s" in block          # _clock(191.0)
    assert "failed" not in block      # no error clause when gh_error_count == 0


def test_provenance_cost_line_omitted_when_no_call_count():
    # An older/static-only doc with no gh_query_count must not render a bare/empty
    # cost line.
    block = "\n".join(bp._provenance_block(_prov_doc(), "o/r", "2026-06-08"))
    assert "Data-collection cost" not in block


def test_provenance_cost_line_has_four_honest_states():
    # The cost line must distinguish: (a) adaptive-with-deepening, (b) a genuine full
    # pass, (c) candidates that already fit within the shallow depth (exact, nothing to
    # deepen) while others stayed shallow, and (d) no PR-gating pole at all. It must
    # never launder reduced coverage as full (c/d vs b) nor call an exact gate shallow.
    doc = _prov_doc()
    # (a) adaptive with deepening
    doc["data_sources"].update({"gh_query_count": 560, "shallow_runs": 10, "max_runs": 20,
                                "shallow_capped": True, "pole_candidates": 30,
                                "deepened_workflows": 5, "deepen_converged": True})
    a = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "560 gh API call(s)" in a
    assert "10-run shallow pass" in a and "5 of 30 PR-gating pole candidate(s) deepened to 20" in a
    doc["data_sources"]["deepen_converged"] = False
    assert "did not fully converge" in "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    # (b) genuine full pass (nothing capped) → per-run wording
    doc["data_sources"].update({"shallow_capped": False, "deepened_workflows": 0,
                                "deepen_converged": True})
    assert "one jobs fetch per sampled run" in "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    # (c) capped, candidates exist but were already within shallow depth (deep==0,
    #     cand>0) → "already fit within N runs", NOT "none ranked as a pole".
    doc["data_sources"].update({"shallow_capped": True, "deepened_workflows": 0,
                                "pole_candidates": 3})
    c = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "rendered PR-gating poles fit within 10 runs" in c
    assert "none" not in c.lower() and "one jobs fetch per sampled run" not in c
    # (c2) cost-only deepening: no PR-gating workflow needed more samples, but
    # selected bill-pole workflows did. This must not render as "nothing deepened".
    doc["data_sources"].update({"shallow_capped": True, "deepened_workflows": 0,
                                "pole_candidates": 0,
                                "cost_deepened_workflow_count": 2})
    c2 = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "2 bill-pole workflow candidate(s) deepened to 20 runs for the runner-minute source block" in c2
    assert "nothing was deepened" not in c2
    assert "gate, drill-set, floor, and selected bill-pole workflows are full-depth" not in c2
    # (d) capped, NO pole candidates → loud reduced-coverage wording
    doc["data_sources"]["cost_deepened_workflow_count"] = 0
    doc["data_sources"]["pole_candidates"] = 0
    d = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "none" in d.lower() and "rests on the shallow sample" in d


def test_provenance_flags_a_deep_scan_for_a_complete_sample():
    # A complete sample that needed a deep scan (>=3x kept) to find PRs running the
    # full suite is flagged, so it reads differently from a shallow one.
    block = "\n".join(bp._provenance_block(
        _prov_doc(sampled_pr_count=20, sample_fetched=60), "o/r", "2026-06-08"))
    assert "scanned **60 recent PRs**" in block
    assert "20 that ran the full required suite" in block


def test_provenance_omits_deep_scan_note_for_a_shallow_walk():
    # 50 fetched for 20 kept is past the OLD 2x trigger but below the 3x threshold —
    # a routine ~half-partial-suite rate must NOT trip the note (guards the raise).
    block = "\n".join(bp._provenance_block(
        _prov_doc(sampled_pr_count=20, sample_fetched=50), "o/r", "2026-06-08"))
    assert "recent PRs**" not in block
    assert "20/20 sampled PRs" in block          # the plain honest baseline


def test_provenance_unsatisfiable_suite_is_not_reported_as_unreadable():
    # P2 regression: an external required suite that was READ but ran on no sampled PR
    # sets required_suite_scoped=False AND required_suite_unsatisfiable=True. The
    # provenance must report the PR-floor cause, NOT the "unreadable (no admin / 404)"
    # cause — the two share the scoped=False flag but are different failures.
    block = "\n".join(bp._provenance_block(
        _prov_doc(required_suite_scoped=False, required_suite_unsatisfiable=True),
        "o/r", "2026-06-08"))
    assert "No file-backed required gate" in block
    assert "ran on **none** of the sampled PRs" in block
    assert "Required checks were unreadable" not in block      # the wrong cause


def test_provenance_genuinely_unreadable_suite_still_says_unreadable():
    # The other branch is preserved: scoped=False with NO unsatisfiable flag means the
    # required set truly couldn't be read (no admin / 404).
    block = "\n".join(bp._provenance_block(
        _prov_doc(required_suite_scoped=False), "o/r", "2026-06-08"))
    assert "Required checks were unreadable" in block
    assert "No file-backed required gate" not in block


def test_provenance_surfaces_required_scope_narrowing_never_silent():
    # The required-scope filter can drop the slowest check (a non-required, non-gating
    # one), so the narrowing must be VISIBLE in the report — same "never a silent drop"
    # bar as the push/schedule exclusion. The footnote names what was set aside and why.
    block = "\n".join(bp._provenance_block(
        _prov_doc(dropped_non_required_checks=[
            "changed-tests", "Validate build outputs", "CodeQL", "Socket Security",
            "labeler"]),
        "o/r", "2026-06-08"))
    assert "Narrowed to merge-blocking checks:" in block
    assert "5 non-required check(s)" in block
    assert "`changed-tests`" in block
    assert "+1 more" in block                       # only the first 4 are named inline
    # No drop list -> no footnote (it's not an unconditional line).
    empty = "\n".join(bp._provenance_block(_prov_doc(), "o/r", "2026-06-08"))
    assert "Narrowed to merge-blocking checks" not in empty


def test_pr_floor_banner_clauses_branch_on_unsatisfiable():
    # The categorical "external/managed … none ran on the sampled PRs" claim only
    # appears when the sampler PROVED the external suite absent (unsatisfiable) ON A
    # COMPLETE, fetch-clean sample. For an all-fileless gate that DID run (no
    # unsatisfiable flag), the banner must not claim it didn't run.
    doc = {"required_checks": ["Enterprise CI/tests", "cla/mattermost"]}
    unsat = "\n".join(bp._pr_floor_fallback_banner(
        doc, {"gate_kind": "pr_floor_fallback", "required_suite_unsatisfiable": True,
              "sample_complete": True}))
    assert "ran on the sampled PRs" in unsat
    assert "external/managed" in unsat
    fileless = "\n".join(bp._pr_floor_fallback_banner(
        doc, {"gate_kind": "pr_floor_fallback"}))
    assert "ran on the sampled PRs" not in fileless
    assert "external/managed" in fileless                       # still demoted, just accurate
    # A normal report draws no banner at all.
    assert bp._pr_floor_fallback_banner(doc, {}) == []


def test_pr_floor_banner_softens_on_a_degraded_sample():
    # "No PR carried the required suite" is only as strong as the sample. On a SHORT
    # sample or one with fetch failures, the absence may be a COVERAGE gap, not proof
    # the gate is external — the banner must NOT assert "external/managed" categorically,
    # and must point the reader at re-running on a fuller window.
    doc = {"required_checks": ["Enterprise CI/tests"]}
    short = "\n".join(bp._pr_floor_fallback_banner(
        doc, {"gate_kind": "pr_floor_fallback", "required_suite_unsatisfiable": True,
              "sample_complete": False}))
    assert "may be a coverage gap" in short
    assert "are external/managed" not in short  # the categorical claim is withheld
    failures = "\n".join(bp._pr_floor_fallback_banner(
        doc, {"gate_kind": "pr_floor_fallback", "required_suite_unsatisfiable": True,
              "sample_complete": True, "sample_fetch_failures": 3}))
    assert "3 fetch failure(s)" in failures
    assert "coverage gap" in failures


def test_pr_floor_banner_handles_empty_required_checks():
    # The `else` arm: no required check could be resolved at all (empty list). The banner
    # must still demote to the PR-floor without naming a (nonexistent) gate check.
    banner = "\n".join(bp._pr_floor_fallback_banner(
        {"required_checks": []}, {"gate_kind": "pr_floor_fallback"}))
    assert "No file-backed required check could be resolved" in banner
    assert "measured PR-floor" in banner


def test_provenance_deep_scan_count_excludes_fetch_failures():
    # The deep-scan note counts only PRs we got a verdict on: 70 fetched − 12
    # failed = 58 evaluated, which is < 3×20, so the note must NOT fire on the raw
    # 70 (it would, pre-fix). The failures get their own coverage-gap bullet.
    block = "\n".join(bp._provenance_block(
        _prov_doc(sampled_pr_count=20, sample_fetched=70, sample_fetch_failures=12),
        "o/r", "2026-06-08"))
    assert "recent PRs**" not in block                  # 58 evaluated < 60, omitted
    assert "12 PR check-run fetch(es) failed" in block  # disclosed separately
    # With fewer failures the evaluated count clears 3× kept and the note fires,
    # reporting the evaluated span (66), not the raw fetched (70).
    block2 = "\n".join(bp._provenance_block(
        _prov_doc(sampled_pr_count=20, sample_fetched=70, sample_fetch_failures=4),
        "o/r", "2026-06-08"))
    assert "scanned **66 recent PRs**" in block2


def test_provenance_short_sample_wins_over_deep_scan_note():
    # An INCOMPLETE sample shows the short-sample floor caveat and never the
    # (reassuring) deep-scan note, even when the scan was deep — locks the `elif`.
    block = "\n".join(bp._provenance_block(
        _prov_doc(sample_complete=False, sampled_pr_count=16, sample_fetched=160),
        "o/r", "2026-06-08"))
    assert "short sample" in block
    assert "scanned **160 recent PRs**" not in block


def test_provenance_renders_fetch_failure_coverage_gap():
    # A non-zero fetch-failure count is surfaced as a coverage gap (it was computed
    # but never rendered before) — and suppressed when zero.
    shown = "\n".join(bp._provenance_block(
        _prov_doc(sample_fetch_failures=2), "o/r", "2026-06-08"))
    assert "2 PR check-run fetch(es) failed" in shown
    assert "not laundered into 'ran nothing / clean'" in shown
    clean = "\n".join(bp._provenance_block(
        _prov_doc(sample_fetch_failures=0), "o/r", "2026-06-08"))
    assert "fetch(es) failed" not in clean


def test_toc_attributes_gate_frequency_to_the_workflow_not_one_matrix_leg():
    # The gate count is a per-WORKFLOW sum over its matrix legs. The representative
    # check shown is a single leg that may be the literal pole on fewer PRs than its
    # workflow gates, so the label must attribute the count to the workflow file -
    # "<check> gates 13/20" would read as the leg's own count and mismatch a
    # populations recompute (a real defect caught by adversarial review).
    pole_wfs = [{"check": "tests-web (node24, pg15, mode)", "p50_s": 269.0,
                 "workflow_file": ".github/workflows/pipeline.yml"}]
    wf_gate = {".github/workflows/pipeline.yml": 13}
    toc = "\n".join(bp._toc_block(pole_wfs, wf_gate, 20, also_count=0))
    assert "`pipeline.yml` gates 13/20 PRs" in toc      # attributed to the workflow
    assert "mode)` — 4m 29s · gates 13/20" not in toc   # NOT to the bare leg name


def test_agent_prompt_attributes_gate_frequency_to_the_workflow():
    # Same fix at the per-pole agent prompt: "its workflow `<wf>` gates N/M", never
    # "the pole on N/M sampled PRs" attached to the single leg.
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "tests-web (node24, pg15, mode)"
    # A second pipeline.yml leg so the workflow gate count sums over both legs (11+2).
    doc["pr_critical_path"]["poles"].append({
        "check": "tests-web (node24, pg12, mode)", "p50_s": 258.0,
        "workflow_file": ".github/workflows/pipeline.yml",
        "job": "tests-web (node24, pg12, mode)", "dominant_step": "run tests",
        "dominant_p50_s": 88.0,
        "steps": [{"step": "run tests", "category": "test", "p50_s": 88.0}]})
    # 20 sampled PRs: pg15 is the pole on 11, pg12 on 2 (workflow gates 13), and 7 PRs
    # are gated by some other check -> npop=20, wf gate 13, single leg pg15 only 11.
    doc["pr_critical_path"]["populations"] = (
        [[0.05, [["tests-web (node24, pg15, mode)", 269.0],
                 ["tests-web (node24, pg12, mode)", 258.0]]]] * 11 +
        [[0.05, [["tests-web (node24, pg12, mode)", 268.0],
                 ["tests-web (node24, pg15, mode)", 255.0]]]] * 2 +
        [[0.05, [["some-other-check", 300.0]]]] * 7
    )
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"}, "2026-06-08")
    assert "its workflow `pipeline.yml` gates 13/20 sampled PRs" in md
    assert "the pole on 13/20 sampled PRs" not in md


def _doc_demoted_frequency_pole() -> dict:
    # caddy goreleaser-check regression: `test` gates every PR (typical). `goreleaser-check` is
    # present on a MAJORITY (13/20) but the actual slowest on 0/20 (pole_n 0) → frequency-demoted,
    # NOT opt-in/rare. Both jobs live in ci.yml, so the WORKFLOW gate-count is 20/20 (driven by
    # `test`) — the count the buggy render borrowed next to the demoted pole.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["check_present_n_pr"] = 20
    cp["poles"] = [
        {"check": "test", "p50_s": 400.0,
         "workflow_file": ".github/workflows/ci.yml", "job": "test",
         "dominant_step": "run tests", "dominant_p50_s": 200.0,
         "steps": [{"step": "run tests", "category": "test", "p50_s": 200.0}]},
        {"check": "goreleaser-check", "p50_s": 168.0,
         "workflow_file": ".github/workflows/ci.yml", "job": "goreleaser-check",
         "dominant_step": "run goreleaser", "dominant_p50_s": 120.0,
         "steps": [{"step": "run goreleaser", "category": "build", "p50_s": 120.0}]},
    ]
    cp["checks"] = [
        {"name": "test", "workflow_file": ".github/workflows/ci.yml",
         "p50_s": 400.0, "present_on": 20, "pole_n": 20},
        {"name": "goreleaser-check", "workflow_file": ".github/workflows/ci.yml",
         "p50_s": 168.0, "present_on": 13, "pole_n": 0},
    ]
    cp["populations"] = (
        [[0.05, [["test", 400.0], ["goreleaser-check", 168.0]]]] * 13 +
        [[0.05, [["test", 400.0]]]] * 7)
    return doc


def test_demoted_frequency_pole_prompt_and_toc_drop_typical_gate_framing():
    # A frequency-demoted pole (`goreleaser-check`, the actual slowest on 0/20) must NOT be handed
    # the typical-gate framing its own "Rarely the merge gate" header disowns: its agent prompt must
    # not say "Slowest check a typical PR waits on", and its Contents row must not borrow the
    # workflow's "gates 20/20 PRs" count. The TYPICAL `test` pole keeps both.
    doc = _doc_demoted_frequency_pole()
    md = bp.render(doc, {"ci": _IMPORT_BOUND_LOG}, {},
                   {"ci": "https://github.com/caddyserver/caddy/actions/runs/1"}, "2026-06-08")
    # Isolate the demoted pole's drill section (from its Long-pole header to end).
    demoted = md[md.index("Long pole 2: `ci.yml` ▸ `goreleaser-check`"):]
    assert "Rarely the merge gate" in demoted                      # header states the demotion
    assert "Slowest check a typical PR waits on" not in demoted    # the bug: gone from the prompt
    assert "Rarely the merge pole" in demoted                      # prompt reframed to match header
    # Contents rows: typical pole keeps the gate count, demoted pole does not. Slice from the
    # Contents header to the first pole drill header AFTER it (the bottom-line above also names
    # "Long pole 1", so anchor on the "## " drill header, not the bare phrase).
    _toc_start = md.index("📋 Contents")
    toc = md[_toc_start:md.index("\n## ", _toc_start)]
    # Plain-text anchor labels, no backticks (owner UX edit 2026-07-19).
    assert "[test](#pole-1)" in toc and "`ci.yml` gates 20/20 PRs" in toc
    assert "[goreleaser-check](#pole-2)" in toc
    assert "rarely the merge pole" in toc
    # The demoted row must not carry a "gates N/NN PRs" tail (the sibling `test`'s frequency).
    gore_row = next(l for l in toc.splitlines() if "goreleaser-check](#pole-2)" in l)
    assert "gates" not in gore_row


def test_bimodal_note_surfaces_a_fast_median_check_but_not_a_slow_median_one():
    # A check whose MEDIAN is the fast mode (bar under-states it) -> surfaced, so the
    # P50 ranking can't silently drop a frequent slow gate from the spine.
    checks = [
        {"name": "test (22.x)", "p50_s": 145.0,
         "bimodal": {"low_p50_s": 132.0, "high_p50_s": 353.0, "slow_frac": 0.48}},
        # Median already on the slow mode -> shown at its slow time, nothing hidden.
        {"name": "build", "p50_s": 500.0,
         "bimodal": {"low_p50_s": 300.0, "high_p50_s": 520.0, "slow_frac": 0.4}},
        # Fast-median bimodal BUT the slow mode (136s) isn't a material gate vs the
        # 549s headline gate -> filtered out (not surfaced as a "long gate").
        {"name": "typecheck", "p50_s": 91.0,
         "bimodal": {"low_p50_s": 85.0, "high_p50_s": 136.0, "slow_frac": 0.4}},
        {"name": "lint", "p50_s": 100.0},  # not bimodal
    ]
    note = "\n".join(bp._bimodal_note(checks, gate_p50=549.0))
    assert "Bimodal gates" in note
    assert "`test (22.x)`" in note and "~48% of PRs" in note and "5m 53s" in note
    assert "`build`" not in note       # slow-median check is not surfaced (not hidden)
    assert "`typecheck`" not in note   # slow mode below materiality threshold
    assert "`lint`" not in note
    # No bimodal checks at all -> no note.
    assert bp._bimodal_note([{"name": "a", "p50_s": 10.0}], gate_p50=549.0) == []


def test_pole_headline_shows_slow_mode_for_a_bimodal_pole():
    # A bimodal pole whose P50 sits on the FAST mode: the drill is captured from a
    # representative SLOW run, so the header + severity must use the slow-mode time -
    # not the median, which would read as a 2m header over a 6m drill.
    bimodal = {"check": "test", "p50_s": 145.0,
               "bimodal": {"low_p50_s": 132.0, "high_p50_s": 353.0, "slow_frac": 0.48}}
    head_s, caveat = bp._pole_headline(bimodal)
    assert head_s == 353.0                       # header/severity follow the slow mode
    assert bp._severity_dot(head_s) == "🔴"      # 353s -> 🔴, vs 🟠 at the 145s median
    assert bp._severity_dot(145.0) == "🟠"       # what the median alone would have shown
    assert "Bimodal" in caveat and "5m 53s" in caveat and "~48% of runs" in caveat
    assert "2m 25s" in caveat                    # the fast P50 is named as the fast mode

    # A pole whose median is already its slow mode (or isn't bimodal) -> no override,
    # no caveat: its header is already honest.
    slow_median = {"check": "build", "p50_s": 500.0,
                   "bimodal": {"low_p50_s": 300.0, "high_p50_s": 520.0, "slow_frac": 0.4}}
    head_s2, caveat2 = bp._pole_headline(slow_median)
    assert head_s2 == 500.0 and caveat2 == ""
    head_s3, caveat3 = bp._pole_headline({"check": "lint", "p50_s": 100.0})
    assert head_s3 == 100.0 and caveat3 == ""


def test_pole_headline_no_fast_mode_caveat_when_median_sits_in_the_slow_cluster():
    # CLASS invariant (nrwl/nx): the "The P50 ... sits on the fast mode" caveat is only
    # TRUE when the median actually sits on the fast cluster - `p50 <= (lo + hi) / 2`, the
    # SAME midpoint predicate `_bimodal_note` uses to decide a median is on the fast mode.
    # When a strict majority of runs are slow (nx: slow_frac 0.59) the median sits IN the
    # slow cluster by construction, so p50 (46m 33s) is 3.4x the fast mode and 86% of the
    # slow mode. `hi > p50 * 1.15` alone (3254 > 3211.95) used to fire the override, printing
    # a caveat that CONTRADICTS the split rendered in the same sentence. Re-derived from the
    # findings pole fields (p50_s + bimodal.{low,high}_p50_s) - no rendered-text proxy.
    nx = {"check": "main", "p50_s": 2793.0,
          "bimodal": {"low_p50_s": 821.0, "high_p50_s": 3254.0, "slow_frac": 0.59}}
    lo, hi = 821.0, 3254.0
    assert nx["p50_s"] > (lo + hi) / 2                    # median is in the slow cluster
    assert hi > nx["p50_s"] * 1.15                        # the OLD condition still fires...
    head_s, caveat = bp._pole_headline(nx)
    assert head_s == 2793.0                               # ...but no override: header is the honest median
    assert caveat == ""                                   # and NO false "sits on the fast mode" caveat
    assert "sits on the fast mode" not in caveat

    # Boundary, aligned with `_bimodal_note` (`p50 > (lo + hi) / 2` -> median NOT on the
    # fast mode): a median just ABOVE the midpoint is in the slow cluster -> suppressed;
    # one AT the midpoint (or below) is still fast-median -> the caveat fires.
    just_above = {"check": "b", "p50_s": 205.0,
                  "bimodal": {"low_p50_s": 100.0, "high_p50_s": 300.0, "slow_frac": 0.52}}
    assert bp._pole_headline(just_above) == (205.0, "")   # 205 > (100+300)/2=200 -> slow cluster
    at_mid = {"check": "c", "p50_s": 200.0,
              "bimodal": {"low_p50_s": 100.0, "high_p50_s": 300.0, "slow_frac": 0.5}}
    head_am, caveat_am = bp._pole_headline(at_mid)        # 200 == midpoint, 300 > 230 -> override fires
    assert head_am == 300.0 and "sits on the fast mode" in caveat_am


def test_render_includes_headline_toc_anchors_and_data_sources():
    # Phase-1 polish: an executive-summary "Bottom line", a Contents TOC linking each
    # pole, explicit `#pole-N` anchors the TOC targets, and a Data sources footer.
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"}, "2026-06-08")
    assert "**Bottom line.**" in md
    assert "waits **4m 15s**" in md                     # total merge wait (255s pole)
    assert "## 📋 Contents" in md
    # Plain-text anchor label, no backticks (owner UX edit 2026-07-19): the label used to
    # be a code span `[\`tests-web\`]` that rendered as a non-link-looking code chip.
    assert "[tests-web](#pole-1)" in md                 # TOC entry links the pole
    assert "[`tests-web`](#pole-1)" not in md           # the old code-chip label is gone
    # This single pole IS the slowest gate, so its Contents row carries the "(the gate)"
    # tag that the removed Level-1 chart's ◀ used to carry.
    assert "(the gate)" in md
    assert '<a id="pole-1"></a>' in md                  # the anchor the TOC targets
    # The prose provenance now leads the Data sources section (owner UX edit 2026-07-19).
    assert "## 🗄️ Data sources" in md                   # methodology footer table
    _ds = md.split("## 🗄️ Data sources", 1)[1]
    assert "Where this data comes from" in _ds          # provenance consolidated into Data sources
    assert md.count("Where this data comes from") == 1  # not left after the Contents too


def test_data_sources_footer_reports_logs_when_the_job_logs_tier_ran():
    # Regression: the tier is named "job-logs" (collect_runs), but the footer used to
    # test `"logs" in tiers` and so always rendered "job logs | not run" even when every
    # drill came from those logs. Now it accepts "job-logs" and shows the count - from
    # `logs_fetched`, or falling back to the persisted data_bundle manifest.
    doc = _doc_one_pole()
    doc["data_sources"] = {**doc["data_sources"], "tiers_run": ["gh-timing", "job-logs"],
                           "logs_fetched": 2}
    foot = "\n".join(bp._data_sources_footer(doc, "o/r"))
    assert "| job logs | 2 job log(s) sampled |" in foot
    assert "job logs | not run" not in foot
    # No explicit count, but a bundle manifest is present -> fall back to its length.
    doc2 = _doc_one_pole()
    doc2["data_sources"] = {**doc2["data_sources"], "tiers_run": ["gh-timing", "job-logs"]}
    doc2["data_bundle"] = {"logs": [{"file": "a.log"}, {"file": "b.log"}, {"file": "c.log"}]}
    foot2 = "\n".join(bp._data_sources_footer(doc2, "o/r"))
    assert "| job logs | 3 job log(s) sampled |" in foot2
    # Tier genuinely absent -> still "not run".
    doc3 = _doc_one_pole()
    doc3["data_sources"] = {**doc3["data_sources"], "tiers_run": ["gh-timing"]}
    assert "job logs | not run" in "\n".join(bp._data_sources_footer(doc3, "o/r"))


def test_second_pole_role_names_the_real_slowest_concurrent_check_above_it():
    # Regression (two-pole): pole 2's "becomes the gate once X drops" must name the
    # ACTUAL slowest concurrent check above it - which may be an intervening check that
    # is not itself a drilled pole (e.g. a matrix shard) - not just the previous pole.
    doc = _doc_one_pole()
    doc["pr_critical_path"]["checks"] = [
        {"name": "changed-tests", "p50_s": 776.5},
        {"name": "UNIT Test (Shard 4)", "p50_s": 504.5},   # intervening, NOT a pole
        {"name": "Validate build outputs", "p50_s": 491.5},
    ]
    doc["pr_critical_path"]["poles"] = [
        {"check": "changed-tests", "p50_s": 776.5,
         "workflow_file": ".github/workflows/changed-test-gate.yml", "job": "changed-tests",
         "steps": [{"step": "Build", "category": "build", "p50_s": 650.0}]},
        {"check": "Validate build outputs", "p50_s": 491.5,
         "workflow_file": ".github/workflows/lint.yml", "job": "Validate build outputs",
         "steps": [{"step": "Build", "category": "build", "p50_s": 432.0}]},
    ]
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    # The intervening Shard 4 (8m 24s) sits between the two poles, so pole 2's role must
    # NOT claim it gates the moment changed-tests alone drops below 8m 12s.
    assert "becomes the gate only once every slower concurrent check drops below" in md
    assert "Runs concurrently behind `changed-tests`" in md


def test_unmatched_pole_still_gets_crossrun_check_and_agent_prompt():
    # A pole whose log matches NO catalog detector must still be a complete finding:
    # a cross-run check on its dominant step + an agent prompt - not a bare timeline.
    timeline = {"job_dur_s": 535.0, "run_url": "https://github.com/o/r/actions/runs/9",
                "steps": [{"name": "Set up job", "dur_s": 5.0, "start_s": 0.0},
                          {"name": "Build", "dur_s": 432.0, "start_s": 90.0}]}
    # generic step-wall magnitude (what collect_runs._dominant_step_sample produces)
    mag = {"label": "the `Build` step (wall)", "unit": "s", "kind": "step-wall",
           "this_run": 432.0, "values": [
               {"run_url": "https://github.com/o/r/actions/runs/9", "value": 432.0, "drilled": True},
               {"run_url": "https://github.com/o/r/actions/runs/8", "value": 410.0},
               {"run_url": "https://github.com/o/r/actions/runs/7", "value": 448.0}]}
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "Validate build outputs"
    doc["pr_critical_path"]["poles"][0]["job"] = "Validate build outputs"
    md = bp.render(doc, {"pipeline": "log that matches nothing"},
                   {}, {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                   mags={"pipeline": mag}, steps={"pipeline": timeline})
    assert "**🔬 Cross-run check** - the `Build` step (wall)" in md   # em-dash stripped to ascii
    assert "dominant step's own wall time" in md          # step-wall phrasing, not "categorical cause"
    assert "Prompt for your coding agent" in md           # the hand-off renders
    assert "NO CATALOG PATTERN MATCHED" in md             # honest about the coverage gap
    assert "`Build` step" in md                           # prompt focuses the dominant step


def test_unmatched_pole_with_llm_analysis_renders_grounded_section_and_prompt():
    # The LLM gap-fill: for a pole with NO catalog match, a provided --analysis renders
    # a clearly-labelled, log-grounded root-cause section + a tailored prompt, and the
    # waterfall's coverage-gap note points at it instead of dead-ending.
    timeline = {"job_dur_s": 210.0, "run_url": "https://github.com/o/r/actions/runs/9",
                "steps": [{"name": "Run e2e tests", "dur_s": 41.0, "start_s": 120.0}]}
    analysis = {
        "cause": "The `Run e2e tests` step runs Playwright specs serially across 3 "
                 "projects; the log shows each project starting only after the prior "
                 "finishes.",
        "breakdown": [["chromium project", "~16s"], ["firefox project", "~14s"]],
        "evidence": ["Running 42 tests using 1 worker",
                     "[chromium] › auth.spec.ts ... ok"],
        "prompt": "REPO: o/r\nTHE GATE: e2e-tests in pipeline.yml.\nInvestigate the "
                  "serial Playwright projects and raise the worker count.",
    }
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "e2e-tests"
    doc["pr_critical_path"]["poles"][0]["job"] = "e2e-tests"
    md = bp.render(doc, {"pipeline": "log matching no detector"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                   steps={"pipeline": timeline}, analyses={"pipeline": analysis})
    assert "🤖 LLM root-cause analysis" in md
    assert "not** a measured catalog detector" in md          # honest provenance label
    assert "runs Playwright specs serially" in md             # the cause narrative
    assert "Running 42 tests using 1 worker" in md            # grounded evidence line
    assert "see the **LLM root-cause analysis** below" in md  # gap note points at it
    assert "no drill-down available" not in md                # not a dead-end anymore
    assert "raise the worker count" in md                     # the tailored prompt body

    # Back-compat: same pole, NO analysis provided -> still the honest coverage-gap note.
    md2 = bp.render(doc, {"pipeline": "log matching no detector"}, {},
                    {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                    steps={"pipeline": timeline})
    assert "no drill-down available" in md2
    assert "🤖 LLM root-cause analysis" not in md2


def _rca_line(report: str, tmp_path: Path) -> str:
    """Run ci-speedup's verify_report.py CLI on `report` (via subprocess — ci-secure
    ships a same-named verify_report, so importing it would collide on the shared
    pythonpath) and return the full RCA-hands-off check line (tag + message)."""
    import subprocess
    verify = Path(__file__).resolve().parent / "verify_report.py"
    rp = tmp_path / "report-2026-05-29.md"
    rp.write_text(report, encoding="utf-8")
    out = subprocess.run([sys.executable, str(verify), "--report", str(rp)],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if "RCA hands off via prompts" in ln:
            return ln
    raise AssertionError(f"no RCA check line in verify_report output:\n{out}")


def test_rendered_deadend_pole_fails_the_verify_report_gate_end_to_end(tmp_path: Path):
    # End-to-end coupling: render a REAL coverage-gap dead-end pole through bp.render()
    # and feed the actual bytes to the actual verify_report gate. This is what stops a
    # renderer wording change from silently slipping past the gate — the marker literal
    # is asserted in test_blocking_path (render side) and test_verify_report_self (gate
    # side) separately against a hardcoded string, but only this test ties the SAME
    # rendered output to the gate, so a drift that updated one literal but not the other
    # fails loudly here. (The gate checks dead-end markers FIRST, so the FAIL is for the
    # dead-end, not some other incompleteness of this minimal synthetic report.)
    timeline = {"job_dur_s": 210.0, "run_url": "https://github.com/o/r/actions/runs/9",
                "steps": [{"name": "Run e2e tests", "dur_s": 41.0, "start_s": 120.0}]}
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "e2e-tests"
    doc["pr_critical_path"]["poles"][0]["job"] = "e2e-tests"
    dead = bp.render(doc, {"pipeline": "log matching no detector"}, {},
                     {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                     steps={"pipeline": timeline})
    assert "no drill-down available; this is a coverage gap" in dead   # the real marker
    line = _rca_line(dead, tmp_path)
    assert line.split(None, 1)[0] == "FAIL"
    assert "coverage-gap" in line          # failed FOR the dead-end reason, not another
    # The filled counterpart PASSES the RCA gate end-to-end. The agent's `prompt` body
    # carries NO disclaimer (as it naturally won't) — the renderer prepends the standard
    # "does NOT prescribe the fix" disclaimer, so prompts==disclaimers and the gate passes.
    # (This is the teleport-dogfood bug: before the fix, a gap-fill report failed its own
    # verify gate for the missing disclaimer, forcing the agent to hack its output.)
    analysis = {"cause": "Playwright specs run serially across 3 projects.",
                "breakdown": [["chromium", "~16s"]],
                "evidence": ["Running 42 tests using 1 worker"],
                "prompt": "REPO: o/r\nInvestigate the serial projects and raise workers."}
    filled = bp.render(doc, {"pipeline": "log matching no detector"}, {},
                       {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                       steps={"pipeline": timeline}, analyses={"pipeline": analysis})
    assert "no drill-down available" not in filled
    assert "does NOT prescribe the fix" in filled              # renderer added the disclaimer
    assert _rca_line(filled, tmp_path).split(None, 1)[0] == "PASS"   # full gate PASS, not a hack


def test_singleton_mag_pole_prompt_does_not_cite_a_missing_cross_run_check(tmp_path: Path):
    # Class regression (goreleaser-check on caddyserver/caddy): an undetected pole with a
    # DRILLED timeline but a SINGLETON magnitude sample (only the drilled run) renders NO
    # "🔬 Cross-run check" section (`_mag_line` returns [] on <2 values). The generic prompt
    # must therefore NOT claim the dominant step's share is "validated across runs in the
    # cross-run check above" — that section isn't there. verify_report's RCA-hands-off gate
    # re-derives this per pole and FAILs a dangling reference.
    timeline = {"job_dur_s": 535.0, "run_url": "https://github.com/o/r/actions/runs/87229819454",
                "steps": [{"name": "Set up job", "dur_s": 5.0, "start_s": 0.0},
                          {"name": "GoReleaser", "dur_s": 432.0, "start_s": 90.0}]}
    # Singleton step-wall magnitude: only the drilled run itself, so no cross-run check renders.
    mag = {"label": "the `GoReleaser` step (wall)", "unit": "s", "kind": "step-wall",
           "this_run": 432.0, "values": [
               {"run_url": "https://github.com/o/r/actions/runs/87229819454",
                "value": 432.0, "drilled": True}]}
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "goreleaser-check"
    doc["pr_critical_path"]["poles"][0]["job"] = "goreleaser-check"
    md = bp.render(doc, {"pipeline": "log that matches nothing"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/87229819454"},
                   "2026-06-08", mags={"pipeline": mag}, steps={"pipeline": timeline})
    # Precondition: the section really is suppressed for a singleton sample.
    assert "🔬 Cross-run check" not in md
    assert "GoReleaser" in md                                     # the prompt still names the step
    # The bug: the generic prompt must not point at a cross-run check that isn't rendered.
    assert "validated across runs in the cross-run check above" not in md
    assert "measured in the drilled run" in md                    # honest replacement wording

    # The SAME suppression applies to the LLM-analysis block's provenance line. With an
    # analysis (not a dead-end), the report is otherwise gate-clean, so the RCA-hands-off
    # gate PASSes end-to-end only because the dangling cross-run citation is gone (before
    # the engine fix `_llm_analysis_block` cited "cross-run check above" unconditionally).
    analysis = {"cause": "GoReleaser builds all targets serially.",
                "breakdown": [["darwin/amd64", "~2m"]],
                "evidence": ["building goreleaser ..."],
                "prompt": "REPO: o/r\nInvestigate the serial GoReleaser targets."}
    md2 = bp.render(doc, {"pipeline": "log that matches nothing"}, {},
                    {"pipeline": "https://github.com/o/r/actions/runs/87229819454"},
                    "2026-06-08", mags={"pipeline": mag}, steps={"pipeline": timeline},
                    analyses={"pipeline": analysis})
    assert "🔬 Cross-run check" not in md2
    assert "cross-run check above are measured" not in md2        # no dangling provenance ref
    assert "the timeline above is measured" in md2                # honest replacement wording
    assert _rca_line(md2, tmp_path).split(None, 1)[0] == "PASS", _rca_line(md2, tmp_path)


def test_two_gap_fill_poles_keep_the_disclaimer_count_balanced_end_to_end(tmp_path: Path):
    # The literal teleport-dogfood shape: a report where BOTH poles hit coverage gaps, so
    # TWO gap-fill prompts render. verify_report's invariant is a COUNT
    # (disclaimers == prompts), not a boolean — the single-pole test above only ever proves
    # 1 == 1, so a regression that drops the disclaimer would still balance at 0 == 0 or
    # slip through. This pins the N>1 case the bug actually fired on: prompts == 2 and the
    # renderer must contribute exactly one disclaimer per prompt, so disclaimers == 2 and the
    # real RCA gate PASSes.
    timeline = {"job_dur_s": 210.0, "run_url": "https://github.com/o/r/actions/runs/9",
                "steps": [{"name": "Run e2e tests", "dur_s": 41.0, "start_s": 120.0}]}
    doc = _doc_one_pole()
    doc["pr_critical_path"]["poles"][0]["check"] = "e2e-tests"
    doc["pr_critical_path"]["poles"][0]["job"] = "e2e-tests"
    # A second, distinct-workflow pole that also hits a coverage gap (its own log matches no
    # detector). Distinct workflow stem (`api`) so the per-pole keys bind unambiguously.
    doc["pr_critical_path"]["poles"].append({
        "check": "api-tests", "p50_s": 200.0,
        "workflow_file": ".github/workflows/api.yml", "job": "api-tests",
        "dominant_step": "pytest", "dominant_p50_s": 88.0,
        "steps": [{"step": "pytest", "category": "test", "p50_s": 88.0}],
    })
    api_timeline = {"job_dur_s": 200.0, "run_url": "https://github.com/o/r/actions/runs/9",
                    "steps": [{"name": "pytest", "dur_s": 88.0, "start_s": 30.0}]}
    logs = {"pipeline": "log matching no detector", "api": "other log, no detector"}
    analyses = {
        "pipeline": {"cause": "Playwright specs run serially across 3 projects.",
                     "breakdown": [["chromium", "~16s"]],
                     "evidence": ["Running 42 tests using 1 worker"],
                     "prompt": "REPO: o/r\nInvestigate the serial projects and raise workers."},
        "api": {"cause": "pytest collects the whole suite in one process.",
                "breakdown": [["collect", "~12s"]],
                "evidence": ["collected 1200 items"],
                "prompt": "REPO: o/r\nInvestigate pytest-xdist for the api suite."},
    }
    filled = bp.render(doc, logs, {},
                       {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                       steps={"pipeline": timeline, "api": api_timeline}, analyses=analyses)
    assert "no drill-down available" not in filled            # both poles are gap-filled
    assert filled.count("🤖 Prompt for your coding agent") == 2
    assert filled.count("does NOT prescribe the fix") == 2    # exactly one per prompt
    assert _rca_line(filled, tmp_path).split(None, 1)[0] == "PASS"   # count-balanced gate PASS


def test_llm_agent_prompt_owns_the_no_prescription_disclaimer():
    # The teleport-dogfood bug: the gap-fill prompt wrapper rendered a "🤖 Prompt…" header
    # with no "does NOT prescribe the fix" disclaimer, so verify_report's
    # one-disclaimer-per-prompt count failed on every coverage-gap report. The renderer now
    # owns the disclaimer (the agent writes only the body), exactly like the catalog prompts.
    p = bp._llm_agent_prompt("The Benchmarks job spends 400s in one serial step. Shard it.")
    assert p.count("🤖 Prompt for your coding agent") == 1
    assert p.count("does NOT prescribe the fix") == 1
    # Idempotent: a body that ALREADY carries the disclaimer (a hand-written prompt that
    # included it) must not be doubled — the per-prompt count stays exactly 1.
    p2 = bp._llm_agent_prompt("x does NOT prescribe the fix y\n\nThe real body.")
    assert p2.count("does NOT prescribe the fix") == 1


def test_dominant_step_from_timeline_skips_setup_and_picks_longest():
    tl = {"job_dur_s": 600.0, "steps": [
        {"name": "Set up job", "dur_s": 5.0}, {"name": "Checkout repo", "dur_s": 8.0},
        {"name": "Build", "dur_s": 432.0}, {"name": "Test", "dur_s": 90.0},
        {"name": "Post Build", "dur_s": 3.0}]}
    name, dur, share = bp._dominant_step_from_timeline(tl)
    assert name == "Build" and dur == 432.0 and round(share, 2) == 0.72
    assert bp._dominant_step_from_timeline(None) is None
    assert bp._dominant_step_from_timeline({"job_dur_s": 0, "steps": []}) is None


def test_render_headline_quotes_the_biggest_win_when_a_floor_exists():
    # With a concurrent check below the gate, the headline names the addressable
    # wall-clock win (gate p50 - next check) and points at Long pole 1.
    doc = _doc_one_pole()
    doc["pr_critical_path"]["checks"] = [
        {"name": "tests-web", "p50_s": 255.0},
        {"name": "lint", "p50_s": 75.0},
    ]
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "biggest single measured win is **~3m 00s**" in md   # 255 - 75 = 180s
    assert "[Long pole 1](#pole-1)" in md


def _doc_with_findings() -> dict:
    doc = _doc_one_pole()
    doc["findings"] = [
        {"id": "f1", "pattern": "OPT28", "title": "Full Git History Checkout",
         "severity": "MEDIUM", "runner_min_saving": 859.0,
         "workflow_file": ".github/workflows/ci.yml", "line": 10},
        {"id": "f2", "pattern": "OPT28", "title": "Full Git History Checkout",
         "severity": "MEDIUM", "runner_min_saving": 100.0,
         "workflow_file": ".github/workflows/release.yml", "line": 5},
        {"id": "f3", "pattern": "OPT5", "title": "pnpm Store Not Cached",
         "severity": "MEDIUM", "runner_min_saving": 68.0,
         "workflow_file": ".github/workflows/ci.yml", "line": 20},
        # Advisory: surfaced as a signal, NOT in the hygiene table.
        {"id": "f4", "pattern": "OPT19", "title": "Test Source Sleep Dominance",
         "severity": "LOW", "advisory": True,
         "workflow_file": ".github/workflows/ci.yml", "line": 30},
        # Structural per-pole lever (OPT70/71/72/74/75): rendered AS the pole above,
        # carries NO runner-minute axis (only the cluster-floor lever OPT73 does), so
        # it's excluded from the bill-ranked appendix.
        {"id": "f5", "pattern": "OPT72", "title": "Prefer the safe cache path",
         "severity": "HIGH", "structural": True, "risk": "MEDIUM",
         "runner_min_saving": None, "wall_clock_p50_s": 120.0,
         "workflow_file": ".github/workflows/pipeline.yml", "line": 1},
    ]
    return doc


def test_also_noticed_lists_hygiene_ranked_by_bill_and_links_toc():
    md = bp.render(_doc_with_findings(), {}, {}, {}, "2026-06-08")
    assert "## 🧹 Also noticed - residual hygiene" in md
    assert '<a id="also-noticed"></a>' in md
    # OPT28 (859+100 runner-min) ranks above OPT5 (68); both grouped by pattern.
    i28, i5 = md.index("OPT28"), md.index("OPT5")
    assert i28 < i5
    assert "959 min/mo" in md                  # 859 + 100 summed across occurrences (PR-Z: positive label)
    assert "-959 min/mo" not in md
    assert "2 across 2 wf" in md               # OPT28's two occurrences
    # The TOC points at the appendix (2 hygiene patterns: OPT28, OPT5).
    assert "**🧹 Also noticed** - 2 additional hygiene finding" in md
    assert "[see below](#also-noticed)" in md


def test_also_noticed_excludes_advisory_and_structural():
    md = bp.render(_doc_with_findings(), {}, {}, {}, "2026-06-08")
    # Advisory + manual-review sections were removed entirely; advisory findings are
    # not rendered anywhere (they stay in the findings JSON only).
    assert "Considered & set aside" not in md
    assert "Manual review" not in md
    assert "OPT19" not in md          # advisory finding, no longer surfaced
    # A structural PER-POLE lever (OPT72) is rendered AS the pole and carries no
    # runner-minute axis, so it never appears as an off-path hygiene row.
    assert "OPT72" not in md.split("## 🧹 Also noticed")[1]


def test_also_noticed_includes_bill_only_cluster_floor_structural():
    # Regression: OPT73 (the cross-cluster shared-substep floor lever) is the ONE
    # structural pattern that carries a credited, bill-only runner-minute saving. It
    # is NOT rendered as a single pole (it spans the whole cluster), so excluding the
    # entire structural track as "already rendered as the poles" silently dropped the
    # single biggest bill saving in the audit. The savings methodology says the
    # runner-minute axis is shown in "Also noticed" — so a bill-only OPT73 must render
    # there, ranked by bill saving, above a smaller catalog hygiene finding.
    doc = _doc_one_pole()
    doc["findings"] = [
        {"id": "f1", "pattern": "OPT33",
         "title": "No Draft-PR Gating on an Expensive Job",
         "severity": "MEDIUM", "runner_min_saving": 479.0,
         "workflow_file": ".github/workflows/ci.yml", "line": 10},
        # Per-pole structural lever — rendered AS the pole, no runner-minute axis.
        {"id": "f2", "pattern": "OPT72", "title": "Prefer the safe cache path",
         "severity": "HIGH", "structural": True, "risk": "MEDIUM",
         "runner_min_saving": None, "wall_clock_p50_s": 120.0,
         "workflow_file": ".github/workflows/ci.yml", "line": 1},
        # Cross-cluster floor lever — bill-only (wall-clock floored to 0), the biggest
        # single runner-minute saving in the audit.
        {"id": "f3", "pattern": "OPT73", "structural": True, "risk": "LOW",
         "severity": "HIGH",
         "title": "Shared step recurs across the cluster - fix once, lower the floor",
         "runner_min_saving": 984.0, "wall_clock_p50_s": 0.0,
         "affected_jobs": ["app", "web"],
         "workflow_file": ".github/workflows/ci.yml", "line": 0},
    ]
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    appendix = md.split("## 🧹 Also noticed")[1]
    # The dropped lever now renders, with its bill saving shown.
    assert "OPT73" in appendix
    assert "984 min/mo" in appendix            # PR-Z: positive label
    assert "-984 min/mo" not in appendix
    # Residual rows still rank by bill saving: OPT73 (984) above the smaller OPT33 (479).
    assert appendix.index("OPT73") < appendix.index("OPT33")
    # The per-pole structural lever (no runner-minute axis) is still excluded.
    assert "OPT72" not in appendix
    # The TOC count stays honest: OPT73 + OPT33 = 2 residual hygiene findings.
    assert "**🧹 Also noticed** - 2 additional hygiene finding" in md


def test_metadata_table_up_top_has_commit_link_and_window():
    doc = _doc_with_findings()
    doc["commit_sha"] = "abcdef1234567890"
    doc["skill_commit_sha"] = "2b4b7360000"
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    # Sits above the spine.
    assert md.index("| **Audited commit** |") < md.index("Long pole 1:")
    # Repository is the header row - no empty `| | |` row starting the table.
    assert "| Repository | `o/r` |" in md
    assert "| | |" not in md
    # Commit hash links to the commit so file refs have a home.
    assert "[`abcdef1`](https://github.com/o/r/commit/abcdef1234567890)" in md
    assert "anchored to this tree" in md
    # The skill commit links to the skill repo too.
    assert "https://github.com/starslingdev/skills/commit/2b4b7360000" in md
    # 30-day window derived from the scan date (2026-06-08 -> 2026-05-09).
    assert "2026-05-09 → 2026-06-08 (30-day window)" in md
    assert "100 runs / 300 jobs" in md


def test_starsling_footer_is_present():
    md = bp.render(_doc_with_findings(), {}, {}, {}, "2026-06-08")
    assert "Generated by [StarSling](https://starsling.dev) 💫" in md
    # The TOC carries the per-pole severity dot too (255s gate -> 🟠). Plain-text anchor
    # label, no backticks (owner UX edit 2026-07-19).
    assert "🟠 [tests-web](#pole-1)" in md


def test_coverage_gap_and_dropped_unprovable_banners_render():
    doc = _doc_with_findings()
    doc["scan_incomplete"] = [{"path": ".github/workflows/broken.yml",
                               "reason": "YAML parse error"}]
    doc["pr_critical_path"]["dropped_unprovable"] = [
        {"id": "d9", "pattern": "OPT3", "affected_jobs": ["build"],
         "reason": "no cache line in logs"}]
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "Incomplete coverage" in md and "broken.yml" in md
    assert "dropped after log review" in md and "OPT3" in md
    # No silent drops: both must read as gaps, not as clean.
    # Placement: the incomplete-coverage WARNING is read-me-first (above the spine);
    # the dropped-finding NOTE is bottom matter (below the poles, by the footer).
    assert md.index("Incomplete coverage") < md.index("Long pole 1:")
    assert md.index("dropped after log review") > md.index("Long pole 1:")
    assert md.index("dropped after log review") > md.index("## 🧹 Also noticed")


def test_render_handles_pole_with_no_captured_log():
    # A pole with no --log still renders its steps + the "run with --log" hint,
    # never crashes.
    md = bp.render(_doc_one_pole(), {}, {}, {}, "")
    assert "Long pole 1:" in md
    assert "no captured log" in md


def test_render_no_poles_is_graceful():
    assert "No measured critical path" in bp.render({"pr_critical_path": {}})


def _load_verify_report():
    # `verify_report` is not a unique module name (ci-secure ships one too), so load
    # THIS skill's by path under a unique name to avoid a cross-skill import clash.
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tests" / "verify_report.py"
    name = "ci_speedup_verify_report_in_bp"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register first: its @dataclass resolves __module__ here
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _doc_no_runs_with_static_findings() -> dict:
    # An archived / brand-new / low-activity repo: collect_runs sampled 0 runs, so the
    # spine is empty (poles == []) and per_workflow_timing is all-zero — but the static
    # scan still found real workflow hygiene findings.
    return {
        "repo": "datafold/data-diff", "scanned_at": "2026-06-08T00:00:00Z",
        "commit_sha": "abcdef1234567890", "skill_commit_sha": "2b4b7360000",
        "data_sources": {"runs_sampled": 0, "jobs_sampled": 0, "workflows_analyzed": 3},
        "pr_critical_path": {"sampled_pr_count": 0, "sample_target": 20, "poles": []},
        "findings": [
            {"id": "f1", "pattern": "OPT26", "title": "Outdated Action Versions",
             "severity": "MEDIUM", "runner_min_saving": 50.0,
             "workflow_file": ".github/workflows/ci.yml", "line": 10},
            {"id": "f2", "pattern": "OPT45", "title": "Missing Concurrency Group",
             "severity": "MEDIUM", "runner_min_saving": 120.0,
             "workflow_file": ".github/workflows/ci.yml", "line": 1},
            {"id": "f3", "pattern": "OPT33",
             "title": "No Draft-PR Gating on an Expensive Job",
             "severity": "MEDIUM", "runner_min_saving": 30.0,
             "workflow_file": ".github/workflows/ci.yml", "line": 20},
        ],
    }


def test_render_no_poles_but_static_findings_does_not_drop_them(tmp_path):
    # Regression: a no-run-history repo (poles == []) with static hygiene findings must
    # NOT dead-end with the one-line "_No measured critical path_" note — that silently
    # drops every static finding and fails verify_report (a dead-end the skill must
    # never ship). It must render those findings honestly and pass verify_report.
    doc = _doc_no_runs_with_static_findings()
    md = bp.render(doc)
    assert "_No measured critical path" not in md           # did NOT dead-end
    assert "No run history to measure" in md                # honest no-timing banner
    for pat in ("OPT26", "OPT45", "OPT33"):                 # every static finding rendered
        assert pat in md
    assert "## 🗄️ Data sources" in md                       # provenance/data-basis present

    # The rendered report must satisfy the shipped artifact guard (verify_report).
    report = tmp_path / "blocking-path-speed.md"
    report.write_text(md, encoding="utf-8")
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    vr = _load_verify_report()
    failures = [f"{c.name}: {c.detail}"
                for c in vr.run_checks(md, report, findings, skill_repo=None)
                if not c.skipped and not c.ok]
    assert not failures, "verify_report invariant failures:\n" + "\n".join(failures)


def test_render_no_pr_gating_ci_gets_shape_banner_not_dormant_hedge(tmp_path):
    # Regression (pre-launch audit, live on simonw/sf-tree-history): a repo running CI
    # daily whose workflows never fire on a PR event (schedule/push-only git scraper)
    # rendered the dormant-repo banner ("archived, brand-new, or low-activity … aged
    # out" / "found no run timing") while pricing OPT36 off 20 timed schedule runs
    # three sections below — the report contradicted its own data. The shape branch
    # must say "no PR-gating CI", own the timed runs, and pass the new verify guard.
    doc = _doc_no_runs_with_static_findings()
    doc["workflow_triggers"] = {
        ".github/workflows/update.yml": ["push", "schedule", "workflow_dispatch"]}
    doc["data_sources"]["runs_sampled"] = 10
    md = bp.render(doc)
    assert "No PR-gating CI to measure" in md
    assert "10 timed run(s)" in md                          # owns the measured runs
    assert "archived, brand-new, or low-activity" not in md  # no dormant speculation
    assert "found no run timing" not in md                   # would contradict OPT36 pricing
    assert "No run history was available" not in md
    assert "until there is run timing to measure" not in md  # no false merge-wait promise

    report = tmp_path / "blocking-path-speed.md"
    report.write_text(md, encoding="utf-8")
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    vr = _load_verify_report()
    failures = [f"{c.name}: {c.detail}"
                for c in vr.run_checks(md, report, findings, skill_repo=None)
                if not c.skipped and not c.ok]
    assert not failures, "verify_report invariant failures:\n" + "\n".join(failures)

    # CLASS guard: the pre-fix dormant prose over this same findings JSON must FAIL
    # verify_report.check_static_only_banner_matches_ci_shape (goes RED on the old
    # renderer, GREEN on this one).
    bad = ("<!-- ci-speedup:static-only -->\n# r — why is the merge slow?\n"
           "> **No run history to measure.** ci-speedup sampled 0 of 20 PRs but found "
           "no run timing (an archived, brand-new, or low-activity repo whose GitHub "
           "Actions run history aged out).\n"
           "> **Bottom line.** No run history was available.\n")
    c = vr.check_static_only_banner_matches_ci_shape(bad, findings)
    assert not c.ok and "no-PR-gating" in c.detail


def test_render_pr_workflow_without_pr_runs_keeps_hedged_banner(tmp_path):
    # A PR-triggered workflow EXISTS but no PR run timing was sampled (e.g. every
    # recent PR run belongs to a since-deleted workflow): the hedged no-run-history
    # banner is still the honest one — but it must name the since-deleted-workflows
    # possibility and must never render the no-PR-gating shape banner.
    doc = _doc_no_runs_with_static_findings()
    doc["workflow_triggers"] = {
        ".github/workflows/ci.yml": ["pull_request", "push"],
        ".github/workflows/release.yml": ["push", "workflow_dispatch"]}
    md = bp.render(doc)
    assert "No run history to measure" in md
    assert "since-deleted workflows" in md
    assert "No PR-gating CI to measure" not in md
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    vr = _load_verify_report()
    c = vr.check_static_only_banner_matches_ci_shape(md, findings)
    assert c.ok and not c.skipped


def test_render_no_poles_no_findings_still_dead_ends_gracefully():
    # The truly-empty case (no poles AND no static findings) keeps the one-line note —
    # there is genuinely nothing to report.
    md = bp.render({"pr_critical_path": {"poles": []}, "findings": []})
    assert "No measured critical path" in md


def test_primary_section_invariant_flags_measured_pole_dropped_to_static(tmp_path):
    # CLASS invariant (verify_report.check_primary_section_present, strengthened): a
    # static-only "no run history" report is honest ONLY when the merge-path spine is
    # truly unmeasurable. A push-only repo whose per_workflow_timing carries a measured
    # merge-path long pole but whose poles is empty (the bug: the PR-floor fallback never
    # engaged because it was gated on PR-volume events, not on populated timing) must be
    # FLAGGED — the slowest measured job was buried. Re-derived from the findings JSON,
    # mirroring collect_runs._select_pr_floor_workflows. Goes RED on the pre-fix renderer
    # (which accepted any static-only report) and GREEN once the floor is synthesized.
    vr = _load_verify_report()
    static = ("<!-- ci-speedup:static-only -->\n# webflow/js-webflow-api - why is the "
              "merge slow?\n> **No run history to measure.**\n")

    def _findings(pwt: dict, poles: list) -> Path:
        p = tmp_path / f"f{len(list(tmp_path.iterdir()))}.json"
        p.write_text(json.dumps(
            {"per_workflow_timing": pwt, "pr_critical_path": {"poles": poles}}),
            encoding="utf-8")
        return p

    # Bug shape: a measured push (merge-path) long pole, but poles == [].
    bug = _findings({".github/workflows/ci.yml": {
        "long_pole_job": "test", "long_pole_p50": 51.5, "events": ["push"]}}, [])
    c = vr.check_primary_section_present(static, bug)
    assert not c.ok and "measured merge-path long pole" in c.detail

    # Cron-only repo: the only timed workflow ran on `schedule` (NOT a merge-path event),
    # so there is genuinely no merge spine to drill — static-only stays legitimate.
    cron = _findings({".github/workflows/nightly.yml": {
        "long_pole_job": "bench", "long_pole_p50": 600.0, "events": ["schedule"]}}, [])
    assert vr.check_primary_section_present(static, cron).ok

    # Comment-contaminated push workflow: its 30d volume is not a CI proxy, so the engine
    # excludes it from the floor — the invariant must mirror that and stay legitimate.
    noisy = _findings({".github/workflows/chatter.yml": {
        "long_pole_job": "triage", "long_pole_p50": 80.0,
        "events": ["push", "issue_comment"]}}, [])
    assert vr.check_primary_section_present(static, noisy).ok

    # Pre-`events`-stamp findings (older JSON): can't re-derive eligibility, so the
    # invariant declines to fire rather than false-flag — static-only stays legitimate.
    legacy = _findings({".github/workflows/ci.yml": {
        "long_pole_job": "test", "long_pole_p50": 51.5}}, [])
    assert vr.check_primary_section_present(static, legacy).ok

    # A real spine report passes regardless of findings.
    spine = "# r - why is the merge slow?\n## Long pole 1: `ci.yml` test\nbody"
    assert vr.check_primary_section_present(spine, bug).ok


def test_render_static_only_queue_only_does_not_claim_zero_hygiene():
    # A no-run-history repo whose ONLY static finding is a queue-wait (OPT43) finding:
    # `_also_noticed_block` excludes wait patterns, so the hygiene group count is 0, but
    # the Pre-start wait section DOES render. The "Bottom line" must not read
    # "0 static hygiene finding(s)" beside a populated queue section (greptile #86 P2).
    doc = {
        "repo": "x/y", "scanned_at": "2026-06-08T00:00:00Z",
        "commit_sha": "abcdef1234567890", "skill_commit_sha": "2b4b7360000",
        "data_sources": {"runs_sampled": 0, "workflows_analyzed": 1},
        "pr_critical_path": {"sampled_pr_count": 0, "sample_target": 20, "poles": []},
        "findings": [
            {"id": "q1", "pattern": "OPT43", "title": "Long Queue Wait",
             "severity": "MEDIUM", "wall_clock_p50_s": 120.0,
             "evidence": "P90 queue 295s",
             "workflow_file": ".github/workflows/ci.yml", "line": 1},
        ],
    }
    md = bp.render(doc)
    assert "_No measured critical path" not in md           # did NOT dead-end
    assert "0 static hygiene finding" not in md             # no false zero-count
    assert "Pre-start wait" in md                           # queue section rendered
    assert "static hygiene finding" not in md               # hygiene clause omitted entirely


def test_static_only_finding_sized_with_no_timing_does_not_claim_a_spine():
    # End-to-end regression: in a static-only report (poles == [], no run timing), a
    # `direct`-model hygiene finding (OPT21 — Unnecessary `needs:`) must NOT render as a
    # wall-clock lever that "sits ON the merge-gating critical path / See the spine above".
    # There is NO spine in a static-only report (the IMPORTANT banner says so), so that
    # framing is a self-contradiction. The root cause was collect_runs._size_finding's
    # no-timing fallback crediting the nominal estimate as positive wall-clock; this test
    # runs the real pipeline (size, then render) so it fails before that fix.
    from collect_runs import _size_finding  # noqa: E402 (scripts on path via line 18)

    doc = {
        "repo": "openphone/gha", "scanned_at": "2026-06-08T00:00:00Z",
        "commit_sha": "abcdef1234567890", "skill_commit_sha": "2b4b7360000",
        "data_sources": {"runs_sampled": 0, "jobs_sampled": 0, "workflows_analyzed": 3},
        "pr_critical_path": {"sampled_pr_count": 0, "sample_target": 20, "poles": []},
        "findings": [
            {"id": "f2", "pattern": "OPT21", "title": "Unnecessary needs: Dependencies",
             "severity": "MEDIUM", "affected_jobs": ["deploy-docs"],
             "workflow_file": ".github/workflows/docs.yml", "line": 5},
        ],
    }
    # Size exactly as the real pipeline does: no measured critical path (empty crit).
    crit = {"long_pole_job": "", "long_pole_p50": 0.0, "floor_p50": 0.0, "job_p50": {}}
    for f in doc["findings"]:
        _size_finding(f, crit, monthly_volume=None)

    md = bp.render(doc)
    assert "<!-- ci-speedup:static-only -->" in md            # rendered static-only
    assert "OPT21" in md                                      # the finding still renders
    # The contradiction the bug filed: on-critical-path / spine framing in a no-spine report.
    assert "See the spine above" not in md
    assert "sits ON the merge-gating critical path" not in md
    assert "long pole ON the merge-gating critical path" not in md


def test_render_no_runs_with_scan_incomplete_keeps_coverage_banner():
    # A no-run-history repo with an UNSCANNABLE workflow but zero findings must NOT fall
    # back to the bannerless one-line note — an unscanned file must never read as clean,
    # so the coverage-gap warning still renders (#86 follow-up).
    doc = {
        "repo": "x/y", "skill_commit_sha": "2b4b7360000",
        "data_sources": {"runs_sampled": 0, "workflows_analyzed": 1},
        "pr_critical_path": {"poles": []},
        "findings": [],
        "scan_incomplete": [{"path": ".github/workflows/ci.yml",
                             "reason": "unparseable YAML"}],
    }
    md = bp.render(doc)
    assert "_No measured critical path" not in md           # did NOT dead-end
    assert "Incomplete coverage" in md                      # coverage banner present
    assert "not** known to be clean" in md


def test_static_only_classified_by_marker_not_banner_prose():
    # `_is_static_only` must key off the invisible machine marker, NOT the human banner
    # phrase — a MEASURED report whose prose happens to quote "no run history to measure"
    # must not be misclassified static-only (which would skip the spine invariants).
    vr = _load_verify_report()
    static_md = bp.render(_doc_no_runs_with_static_findings())
    assert "<!-- ci-speedup:static-only -->" in static_md
    assert vr._is_static_only(static_md) is True
    # A measured report quoting the banner phrase in prose is NOT static-only.
    measured_quoting_phrase = (
        "# repo — why is the merge slow?\n\n"
        "## Long pole 1: test\n\nThe agent noted there was no run history to measure "
        "in the upstream mirror, but the gate here is real.\n")
    assert vr._is_static_only(measured_quoting_phrase) is False


def test_render_captured_but_unrecognized_log_reads_as_coverage_gap():
    # A log WAS captured but no detector matched it: this must surface as a coverage
    # gap, NOT as "no captured log" (which would read as clean / nothing to drill).
    # Regression guard for the "no silent drops" rule applied to log parsing.
    md = bp.render(_doc_one_pole(), {"pipeline": "unrelated build output\nDone in 3s"},
                   {}, {}, "2026-06-08")
    assert "matched no known root-cause pattern" in md
    assert "coverage gap, not a clean job" in md
    assert "no captured log" not in md           # NOT mislabelled as "no log present"


def test_render_unrecognized_log_on_timeline_path_still_flags_the_gap():
    # Same false-clean risk on the timeline path: steps captured + an unrecognized
    # log must still note the gap rather than silently drawing the timeline alone.
    md = bp.render(_doc_one_pole(), {"pipeline": "noise\nDone"}, {}, {},
                   "2026-06-08", {"pipeline": _TIMELINE})
    assert "one after another" in md             # the timeline still renders
    assert "matched no known root-cause pattern" in md
    assert "Level 3" not in md  # no drill is claimed
    # No leaf -> the report must NOT promise a "cause below" or "Evidence lines
    # below" that don't exist; it sends the reader to the step's own log instead.
    assert "cause** below" not in md
    assert "Evidence** lines below" not in md
    assert "no specific callout to search for" in md


def _doc_with_structural_pole() -> dict:
    # A pole whose log matches NO leaf detector but to which a STRUCTURAL finding
    # (OPT70–75) is routed via `affected_jobs` — the reflex-dev/reflex shape: the
    # structural track IS the pole, so the finding must render AS the pole, not be
    # dropped (excluded from "Also noticed") AND missing from the pole.
    doc = _doc_one_pole()
    job = "integration-app-harness (redis, 3.14, 1)"
    doc["pr_critical_path"]["poles"][0]["check"] = job
    doc["pr_critical_path"]["poles"][0]["job"] = job
    doc["findings"] = [
        {"id": "f103", "pattern": "OPT75", "pattern_class": "structural",
         "structural": True, "severity": "HIGH", "risk": "MEDIUM",
         "title": "Decompose the dominant addressable step",
         "workflow_file": ".github/workflows/pipeline.yml", "line": 0,
         "affected_jobs": [job], "fix_recipe_anchor": "opt75-anchor",
         "guardrail": "Keep a full-suite fallback before scoping.",
         "rollout": "Run the scoped and full suites in parallel first.",
         "failure_mode": "A scoped run can miss a cross-cutting regression.",
         "evidence": "dominant step Run app harness tests (test, 88%)"},
    ]
    return doc


def test_structural_finding_routed_to_a_pole_renders_as_the_pole_not_a_dead_end():
    # Regression for the dropped-structural-pole bug (reflex-dev/reflex): a structural
    # OPT75/OPT73 finding routed to a long pole via affected_jobs was excluded from
    # "Also noticed" (the pole "already represents" it) but NEVER rendered at the pole,
    # so the pole falsely read "no catalog pattern matched" and the finding's
    # risk/guardrail/rollout rendered nowhere. The structural track IS the pole.
    doc = _doc_with_structural_pole()
    md = bp.render(doc, {"pipeline": "log that matches no leaf detector at all"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08")
    # (a) the pole no longer claims it's a coverage gap / no catalog match.
    assert "no drill-down available; this is a coverage gap" not in md
    assert "matched no known root-cause pattern" not in md
    # (b) the OPT75 finding renders AT the pole with its mandatory risk axis.
    assert "OPT75" in md
    assert "Decompose the dominant addressable step" in md
    assert "risk **MEDIUM**" in md
    assert "Keep a full-suite fallback before scoping." in md          # guardrail
    assert "Run the scoped and full suites in parallel first." in md   # rollout
    assert "A scoped run can miss a cross-cutting regression." in md   # failure mode
    # (c) the phase-4a LLM gap-fill must NOT fire for a pole with a catalog match.
    assert "🤖 LLM root-cause analysis" not in md
    # the waterfall points at the structural root-cause, not a dead-end.
    assert "structural catalog pattern" in md


def test_structural_for_pole_excludes_bill_only_cross_cluster_lever():
    # Composition regression (#62 render-as-pole vs #63 appendix): a bill-only cross-cluster
    # OPT73 is OWNED by "Also noticed" (it spans the whole cluster; no single pole represents
    # it). Even when its `affected_jobs` name-match a rendered pole, it must NOT also render
    # AT the pole — otherwise the same bill saving renders twice. `_structural_for_pole` must
    # mirror `_also_noticed_block`'s predicate (`_is_pole_structural`).
    pole = {"check": "tests-web", "job": "tests-web"}
    opt73_bill_only = {"pattern": "OPT73", "structural": True,
                       "affected_jobs": ["tests-web", "web"],   # name-matches the pole
                       "wall_clock_p50_s": 0.0, "runner_min_saving": 984.0}
    opt75_per_pole = {"pattern": "OPT75", "structural": True,
                      "affected_jobs": ["tests-web"], "wall_clock_p50_s": 120.0}
    # bill-only OPT73 → appendix-owned → NOT rendered at the pole (the fix)
    assert bp._structural_for_pole(pole, [opt73_bill_only]) == []
    # an ON-SPINE OPT73 (wall_clock > 0) is STILL the cross-cluster lever — anchoring on the
    # pattern keeps it appendix-owned, so it can't be mis-classified as per-pole and vanish
    # when no pole name-matches its cluster (greptile review of #65).
    opt73_on_spine = {"pattern": "OPT73", "structural": True,
                      "affected_jobs": ["tests-web", "web"],
                      "wall_clock_p50_s": 90.0, "runner_min_saving": 984.0}
    assert bp._is_pole_structural(opt73_on_spine) is False        # appendix-owned, not per-pole
    assert bp._structural_for_pole(pole, [opt73_on_spine]) == []  # not rendered at the pole
    # a per-pole structural lever (OPT75, wall-clock, no bill axis) still renders AS the pole
    assert bp._structural_for_pole(pole, [opt75_per_pole]) == [opt75_per_pole]


def test_structural_for_pole_joins_unexpanded_matrix_base_not_a_sibling_leg():
    # FAITHFUL per-pole routing (lancedb/lancedb regression). The join is "exact, then
    # base↔leg expansion" — NOT a shared-matrix-base fold. A structural finding whose
    # `affected_jobs` names the unexpanded matrix BASE joins the rendered leg (the router
    # anchored to the base, the pole is one expanded leg). But a DISTINCT sibling leg is its
    # OWN check with its own bar/dominant step; folding it under this pole would falsely
    # render it with an "it IS this pole" claim.
    pole = {"check": "integration-app-harness (redis, 3.14, 1)",
            "job": "integration-app-harness (redis, 3.14, 1)"}
    opt75_base = {"pattern": "OPT75", "structural": True,
                  "affected_jobs": ["integration-app-harness"],
                  "wall_clock_p50_s": 120.0}
    assert bp._structural_for_pole(pole, [opt75_base]) == [opt75_base]
    # A DISTINCT sibling leg (different params) does NOT join — it is its own pole.
    opt75_sibling_leg = {"pattern": "OPT75", "structural": True,
                         "affected_jobs": ["integration-app-harness (postgres, 3.12, 2)"],
                         "wall_clock_p50_s": 120.0}
    assert bp._structural_for_pole(pole, [opt75_sibling_leg]) == []
    # A finding on a DIFFERENT matrix (different base) does NOT join either.
    other = {"pattern": "OPT75", "structural": True,
             "affected_jobs": ["unit-tests (redis, 3.14, 1)"], "wall_clock_p50_s": 120.0}
    assert bp._structural_for_pole(pole, [other]) == []


def test_structural_for_pole_does_not_fold_sibling_windows_target_triple():
    # Direct lancedb/lancedb shape: long pole 2 is `windows (aarch64-pc-windows-msvc)`
    # (its own OPT70 Build lever). A SEPARATE concurrent check `windows (x86_64-pc-windows-msvc)`
    # carries its own OPT75 (dominant `Run tests`, saving 0 wall-clock) — a different check,
    # not pole 2. Sharing the `windows` matrix base must NOT fold the x86_64 finding under the
    # aarch64 pole with the false "it IS this pole" framing.
    pole = {"check": "windows (aarch64-pc-windows-msvc)",
            "job": "windows (aarch64-pc-windows-msvc)"}
    opt70_pole = {"pattern": "OPT70", "structural": True,
                  "affected_jobs": ["windows (aarch64-pc-windows-msvc)"],
                  "wall_clock_p50_s": 300.0}
    opt75_sibling = {"pattern": "OPT75", "structural": True,
                     "affected_jobs": ["windows (x86_64-pc-windows-msvc)"],
                     "wall_clock_p50_s": 0.0}
    joined = bp._structural_for_pole(pole, [opt70_pole, opt75_sibling])
    assert joined == [opt70_pole]  # only the pole's own lever — the x86_64 sibling is excluded


def test_wf_conflict_only_fires_when_both_files_known_and_differ():
    # The guard that stops a finding from one workflow anchoring a pole in another when
    # their check names collide. Both sides known + different basename → conflict; an
    # unknown side (""/None) falls back to name-only matching (no conflict); same file
    # (path or bare base) → no conflict.
    assert bp._wf_conflict(".github/workflows/framework-test.yml",
                           ".github/workflows/datasets-test.yml") is True
    assert bp._wf_conflict(".github/workflows/ci.yml", "ci.yml") is False   # path vs base
    assert bp._wf_conflict("ci.yml", "ci.yml") is False
    assert bp._wf_conflict("", "ci.yml") is False                           # unknown side
    assert bp._wf_conflict("ci.yml", "") is False
    assert bp._wf_conflict("", "") is False


def test_structural_for_pole_excludes_cross_workflow_name_collision():
    # Residual half of the cross-workflow fold (the by_matrix fix stopped the POLES from
    # folding, but the per-pole JOIN still matched on the bare name). GitHub gives a
    # `name: Python ${{ matrix.python }}` job in BOTH datasets-test.yml and
    # framework-test.yml the identical check-run name `Python 3.13`. A structural finding
    # routed to datasets-test's `Python 3.13` must NOT join framework-test's `Python 3.13`
    # pole via the exact-name (`j == t`) term — that re-attaches the wrong workflow's
    # finding (its own file/evidence/line) under this pole, the very contradiction the PR
    # eliminates everywhere else.
    pole = {"check": "Python 3.13", "job": "Python 3.13",
            "workflow_file": ".github/workflows/framework-test.yml"}
    foreign = {"pattern": "OPT75", "structural": True,
               "affected_jobs": ["Python 3.13"], "wall_clock_p50_s": 120.0,
               "workflow_file": ".github/workflows/datasets-test.yml"}
    assert bp._structural_for_pole(pole, [foreign]) == []
    # The SAME-workflow finding still joins (no regression to the legitimate case).
    native = {**foreign, "workflow_file": ".github/workflows/framework-test.yml"}
    assert bp._structural_for_pole(pole, [native]) == [native]
    # An unknown finding file falls back to name-only matching (reusable-workflow caller).
    unknown = {**foreign, "workflow_file": ""}
    assert bp._structural_for_pole(pole, [unknown]) == [unknown]


def test_data_driven_for_pole_excludes_cross_workflow_name_collision():
    # Same residual fold on the data-driven join (`_job_targets_pole`, which DOES use the
    # exact-name `j == t` term). A credited (>= floor) OPT24 on datasets-test's `Python
    # 3.13` must not be acknowledged by framework-test's `Python 3.13` pole.
    pole = {"check": "Python 3.13", "job": "Python 3.13",
            "workflow_file": ".github/workflows/framework-test.yml"}
    foreign = {"pattern": "OPT24", "affected_jobs": ["Python 3.13"],
               "wall_clock_p50_s": 200.0, "runner_min_saving": 0.0,
               "workflow_file": ".github/workflows/datasets-test.yml"}
    assert bp._data_driven_for_pole(pole, [foreign]) == []
    native = {**foreign, "workflow_file": ".github/workflows/framework-test.yml"}
    assert bp._data_driven_for_pole(pole, [native]) == [native]


def test_collapsed_sibling_structural_guards_no_fire_and_over_fire():
    # `_collapsed_sibling_structural` is the last line of defense against the silent drop:
    # it must annotate a COLLAPSED-OUT sibling leg's lever, but its faithfulness guards must
    # not (a) re-annotate a finding already rendered at the rep, (b) pull in a same-named leg
    # from a DIFFERENT workflow, or (c) annotate a leg that survived as its own rendered pole.
    wf = ".github/workflows/ci.yml"
    rep = {"check": "build (x86_64)", "job": "build (x86_64)", "workflow_file": wf}
    collapsed = {"check": "build (aarch64)", "job": "build (aarch64)", "workflow_file": wf}
    sibling_lever = {"pattern": "OPT75", "structural": True, "wall_clock_p50_s": 90.0,
                     "affected_jobs": ["build (aarch64)"], "workflow_file": wf,
                     "evidence": "Build runs uncached"}

    # Positive: the collapsed-out sibling's lever IS surfaced for the representative pole.
    out = bp._collapsed_sibling_structural(rep, [rep], [rep, collapsed], [sibling_lever])
    assert len(out) == 1
    leg, f = out[0]
    assert f is sibling_lever and "aarch64" in leg

    # (a) A finding routed to the matrix BASE (`build`) already joins the rep via base↔leg
    # expansion (it renders there), so it must NOT be re-annotated as a sibling.
    base_lever = {**sibling_lever, "affected_jobs": ["build"]}
    assert bp._collapsed_sibling_structural(rep, [rep], [rep, collapsed], [base_lever]) == []

    # (b) A same-named leg in a DIFFERENT workflow is not a sibling of this matrix.
    foreign_leg = {**collapsed, "workflow_file": ".github/workflows/other.yml"}
    foreign_lever = {**sibling_lever, "workflow_file": ".github/workflows/other.yml"}
    assert bp._collapsed_sibling_structural(
        rep, [rep], [rep, foreign_leg], [foreign_lever]) == []

    # (c) A sibling leg that survived as its OWN rendered pole is shown there already.
    assert bp._collapsed_sibling_structural(
        rep, [rep, collapsed], [rep, collapsed], [sibling_lever]) == []


def test_structural_for_pole_skips_unrouted_finding_with_no_affected_jobs():
    # An unrouted structural finding (empty/absent `affected_jobs`) can't anchor to a
    # pole, so it must be skipped rather than joined to every pole.
    pole = {"check": "tests-web", "job": "tests-web"}
    no_jobs = {"pattern": "OPT75", "structural": True, "wall_clock_p50_s": 120.0}
    empty_jobs = {"pattern": "OPT75", "structural": True, "affected_jobs": [],
                  "wall_clock_p50_s": 120.0}
    assert bp._structural_for_pole(pole, [no_jobs, empty_jobs]) == []


def test_is_pole_structural_uncredited_opt73_renders_as_a_pole():
    # An UNCREDITED OPT73 (no runner-minute saving) is NOT appendix-owned: with nothing
    # to show in the bill-ranked appendix, it falls back to the per-pole treatment
    # (`_is_pole_structural` True → rendered AS the pole, excluded from "Also noticed").
    # Contrast the credited case asserted above (saving>0 → False → appendix-owned).
    for saving in (0.0, None):
        opt73_uncredited = {"pattern": "OPT73", "structural": True,
                            "affected_jobs": ["tests-web"], "runner_min_saving": saving}
        assert bp._is_pole_structural(opt73_uncredited) is True, saving
        pole = {"check": "tests-web", "job": "tests-web"}
        assert bp._structural_for_pole(pole, [opt73_uncredited]) == [opt73_uncredited]


def test_structural_pole_with_analysis_suppresses_the_llm_gap_fill():
    # Even when an --analysis is supplied (the gap-fill ran), a structural catalog match
    # means the pole is NOT a coverage gap, so the LLM gap-fill must be suppressed
    # (SKILL.md phase 4a fires only for poles that "matched no catalog detector").
    doc = _doc_with_structural_pole()
    analysis = {"cause": "serial test projects", "breakdown": [["a", "1s"]],
                "evidence": ["X"], "prompt": "REPO: o/r\ndo the thing"}
    md = bp.render(doc, {"pipeline": "log matching no leaf detector"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                   analyses={"pipeline": analysis})
    assert "🤖 LLM root-cause analysis" not in md
    assert "OPT75" in md and "risk **MEDIUM**" in md


def _doc_with_data_driven_pole() -> dict:
    # The mindee/doctr shape: the headline pole's log matches NO leaf detector, but a
    # DATA-DRIVEN catalog finding (OPT24 'Long Test Job Without Sharding', a deterministic
    # detector) fired squarely ON it with a CREDITED wall-clock saving (it sits on the
    # critical path). The finding's `affected_jobs` names the UNEXPANDED matrix job id
    # (`pytest-torch`) while the rendered pole is an expanded leg
    # (`pytest-torch (ubuntu-latest, 3.10)`). The pole must not falsely read as a coverage
    # gap, and the gap-capture loop must not be told to "draft a new detector" for it.
    doc = _doc_one_pole()
    job = "pytest-torch (ubuntu-latest, 3.10)"
    pole = doc["pr_critical_path"]["poles"][0]
    pole["check"] = job
    pole["job"] = job
    pole["workflow_file"] = ".github/workflows/main.yml"
    doc["findings"] = [
        {"id": "f1", "pattern": "OPT24", "pattern_class": "data-driven",
         "structural": None, "severity": "HIGH",
         "title": "Long Test Job Without Sharding",
         "workflow_file": ".github/workflows/main.yml", "line": 0,
         "affected_jobs": ["pytest-torch"], "fix_recipe_anchor": "opt24-anchor",
         "wall_clock_p50_s": 744.8, "runner_min_saving": 0.0,
         "pr_critical_path_check": job,
         "evidence": "single pytest job runs 12m24s with no shard split"},
    ]
    return doc


def test_data_driven_for_pole_joins_credited_wall_clock_finding():
    # The join must recognize a credited-wall-clock data-driven finding routed to the pole
    # even when its `affected_jobs` names the unexpanded matrix base of the pole's leg.
    pole = {"check": "pytest-torch (ubuntu-latest, 3.10)",
            "job": "pytest-torch (ubuntu-latest, 3.10)"}
    opt24 = {"pattern": "OPT24", "affected_jobs": ["pytest-torch"],
             "wall_clock_p50_s": 744.8, "runner_min_saving": 0.0}
    assert bp._data_driven_for_pole(pole, [opt24]) == [opt24]
    # A bill-only / off-path data-driven finding (wall-clock floored to 0) makes no "spine"
    # claim, so it must NOT be joined (it stays appendix-owned, no contradiction to fix).
    off_path = {"pattern": "OPT33", "affected_jobs": ["pytest-torch"],
                "wall_clock_p50_s": 0.0, "runner_min_saving": 900.0}
    assert bp._data_driven_for_pole(pole, [off_path]) == []
    # A structural finding is handled by `_structural_for_pole`, never double-joined here.
    opt75 = {"pattern": "OPT75", "structural": True, "affected_jobs": ["pytest-torch"],
             "wall_clock_p50_s": 744.8}
    assert bp._data_driven_for_pole(pole, [opt75]) == []
    # An unrouted finding (no affected_jobs) can't anchor to a pole.
    unrouted = {"pattern": "OPT24", "wall_clock_p50_s": 744.8}
    assert bp._data_driven_for_pole(pole, [unrouted]) == []


def test_pole_with_data_driven_catalog_match_is_not_a_coverage_gap():
    # Regression (mindee/doctr): a data-driven OPT24 finding fired ON the headline pole, yet
    # the spine rendered "(no catalog pattern matched this job's log...)" and routed the pole
    # into the phase-4a LLM gap-fill — self-contradicting the "Also noticed" OPT24 entry that
    # says it "sits ON the merge-gating critical path... See the spine above".
    doc = _doc_with_data_driven_pole()
    analysis = {"cause": "single unsharded pytest job", "breakdown": [["tests", "12m"]],
                "evidence": ["X"], "prompt": "REPO: o/r\ndo the thing"}
    md = bp.render(doc, {"main": "log that matches no leaf detector at all"}, {},
                   {"main": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                   analyses={"main": analysis})
    # (a) the pole no longer claims it's a coverage gap / no catalog match.
    assert "no catalog pattern matched this job's log" not in md
    assert "this is a coverage gap, not a clean job" not in md
    # (b) the phase-4a LLM gap-fill must NOT fire for a pole with a catalog match.
    assert "🤖 LLM root-cause analysis" not in md
    # (c) the spine points back at the matched catalog finding (resolving the contradiction).
    assert "catalog pattern" in md
    # (d) the SAME pole's agent prompt must not contradict the matched data-driven finding —
    # the structural fix threaded `structural` into the prompt builders but left this parallel
    # case asserting `NO CATALOG PATTERN MATCHED` (it keyed purely off `leaf is None`), the same
    # output contradiction this PR eliminates for the structural case.
    assert "NO CATALOG PATTERN MATCHED" not in md
    assert "found no known root-cause pattern" not in md
    assert "DATA-DRIVEN CATALOG PATTERN MATCHED" in md
    prompt_tail = md.split("🤖 Prompt for your coding agent", 1)[1]
    assert "OPT24" in prompt_tail
    # The renderer's one-disclaimer-per-prompt invariant still holds (exactly one).
    assert md.count("does NOT prescribe the fix") == md.count(
        "🤖 Prompt for your coding agent")


def test_gap_poles_excludes_pole_with_data_driven_catalog_match():
    # The downstream harm: blocking_path captured this pole to `.ci-speedup-gaps/` and told a
    # maintainer to "promote these gaps to deterministic catalog detectors" — for a pole OPT24
    # already covers. A pole with a credited-wall-clock data-driven finding is NOT a gap.
    doc = _doc_with_data_driven_pole()
    # No data_bundle -> the fallback `cp.poles` + `_sole_owner_pole` path. `main` uniquely
    # owns the single pole (its workflow basename is `main.yml`).
    gaps = bp._gap_poles(doc, {"main": _NO_MATCH_LOG})
    assert gaps == []


def test_structural_pole_agent_prompt_does_not_claim_no_catalog_match():
    # Regression (superfly/litefs): a pole with no log `leaf` but a routed STRUCTURAL
    # finding (OPT75/OPT73) renders the `📐 Structural root-cause` block AND the
    # waterfall note that a structural catalog pattern matched - yet the SAME pole's
    # generic agent prompt asserted `NO CATALOG PATTERN MATCHED` / `its detectors found
    # no known root-cause pattern` (it keyed purely off `leaf is None`). The prompt must
    # NOT contradict the structural finding rendered alongside it.
    doc = _doc_with_structural_pole()
    md = bp.render(doc, {"pipeline": "log that matches no leaf detector at all"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08")
    # The structural finding IS rendered at this pole...
    assert "📐 Structural root-cause" in md and "OPT75" in md
    # ...so the prompt must NOT claim there was no catalog match.
    assert "NO CATALOG PATTERN MATCHED" not in md
    assert "found no known root-cause pattern" not in md
    # Instead it names the matched structural pattern and points at its root-cause block.
    assert "STRUCTURAL CATALOG PATTERN MATCHED" in md
    prompt_tail = md.split("🤖 Prompt for your coding agent", 1)[1]
    assert "OPT75" in prompt_tail
    # The renderer's one-disclaimer-per-prompt invariant still holds (exactly one).
    assert md.count("does NOT prescribe the fix") == md.count(
        "🤖 Prompt for your coding agent")


def test_pole_matching_both_structural_and_data_driven_prefers_structural_in_prompt():
    # Precedence contract introduced by threading BOTH finding lists into the prompt: when a
    # pole matches a structural finding (OPT70–75, rendered AS the pole) AND a credited
    # data-driven finding (OPT24, rendered in 'Also noticed'), both joins are non-empty. The
    # prompt names the STRUCTURAL pattern (the louder pole-level lever) and must NOT also emit
    # the data-driven wording — exactly one catalog-match block, one disclaimer, no double note.
    doc = _doc_with_structural_pole()
    job = doc["pr_critical_path"]["poles"][0]["check"]
    # A credited-wall-clock OPT24 routed to the SAME job, so _data_driven_for_pole also fires.
    doc["findings"].append(
        {"id": "f200", "pattern": "OPT24", "pattern_class": "data-driven",
         "structural": None, "severity": "HIGH",
         "title": "Long Test Job Without Sharding",
         "workflow_file": ".github/workflows/pipeline.yml", "line": 0,
         "affected_jobs": [job], "fix_recipe_anchor": "opt24-anchor",
         "wall_clock_p50_s": 744.8, "runner_min_saving": 0.0,
         "pr_critical_path_check": job,
         "evidence": "single pytest job runs 12m24s with no shard split"})
    pole, findings = doc["pr_critical_path"]["poles"][0], doc["findings"]
    assert bp._structural_for_pole(pole, findings)      # both joins genuinely fire...
    assert bp._data_driven_for_pole(pole, findings)     # ...so precedence is actually exercised
    md = bp.render(doc, {"pipeline": "log that matches no leaf detector at all"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08")
    prompt_tail = md.split("🤖 Prompt for your coding agent", 1)[1]
    assert "STRUCTURAL CATALOG PATTERN MATCHED" in prompt_tail     # structural wins
    assert "DATA-DRIVEN CATALOG PATTERN MATCHED" not in prompt_tail  # data-driven suppressed here
    assert "NO CATALOG PATTERN MATCHED" not in md
    # Exactly one disclaimer per prompt — no double catalog-match note.
    assert md.count("does NOT prescribe the fix") == md.count(
        "🤖 Prompt for your coding agent")


# --------------------------------------------------------------------------- #
# Timeline (Gantt) path — steps run in succession, not concurrently
# --------------------------------------------------------------------------- #

_TIMELINE = {
    "job": "tests-web", "job_dur_s": 300.0,
    "run_url": "https://github.com/o/r/actions/runs/999",
    "steps": [
        {"name": "Set up job", "start_s": 0.0, "dur_s": 3.0},     # collapsed (<2% )
        {"name": "Build", "start_s": 3.0, "dur_s": 60.0},
        {"name": "run tests", "start_s": 63.0, "dur_s": 230.0},   # dominant
        {"name": "Complete job", "start_s": 293.0, "dur_s": 1.0},  # collapsed
    ],
}


def test_timeline_renders_offset_gantt_and_drills_from_the_slow_step():
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {"pipeline": _TIMELINE})
    # The step level is now a succession timeline, not "slowest first" P50 bars.
    assert "one after another" in md
    assert "this run" in md                                 # job wall-time footer
    # Each row's number is the step's DURATION + % of the job (what the █ bar holds),
    # NOT a timestamp; the dominant row carries the SAME connector wire (◀┐) the
    # deeper levels use - the arrow is drawn literally.
    assert "3m 50s" in md and "◀┐" in md                    # run tests = 230s of 300s
    assert "77%" in md                                       # 230 / 300 of the job
    assert "1:03→4:53" not in md                             # no start→end timestamp
    assert "job start" in md and "wall time on this run" in md  # total folded into intro
    # An offset bar carries leading elapsed shading (it does not start at column 0).
    assert "░█" in md
    # Tiny setup/cleanup steps are collapsed, not drawn as slivers, and the wire
    # continues past them to the connector. The collapse threshold is max(2s, 1.5% of
    # the job), so on this 300s job it's ~4s and the note names the real threshold
    # INCLUSIVELY ("4s or less") - a step AT the threshold is collapsed, so the old
    # "under Ns" wording mislabelled a hidden step that is exactly Ns.
    assert "setup/cleanup steps of 4s or less not shown" in md
    assert "under 4s" not in md and "sub-2s" not in md
    # Sequential-steps note: time cut from a step comes off wall-clock.
    assert "comes straight off the job's wall-clock" in md
    # The drill still hangs off the dominant step (numbered level header), and the
    # run is linked.
    assert "Level 3 - inside `run tests`" in md   # em-dash sanitized to hyphen
    assert "[run 999]" in md
    # No P50-doesn't-sum caveat on the timeline path (a single run sums exactly).
    assert "read the bars as proportions" not in md


def test_emit_level_scales_aggregate_durations_to_wall():
    # Regression: summed-across-workers values (e.g. vitest transform+import) must be
    # scaled to the step wall so a sub-part never reads longer than its parent.
    out: list[str] = []
    bp._emit_level(out, [("compile+instrument", 1320.0, None), ("tests", 880.0, None)],
                   header_below=None, pct_of="sum", scale_to=196.0)  # 196s = 3m 16s
    text = "\n".join(out)
    assert "1m 58s" in text and "60%" in text   # 60% of 196s, not the raw 22m
    assert "1m 18s" in text and "40%" in text
    assert "22m" not in text and "14m" not in text  # raw summed values never shown


def test_audit_links_deep_link_run_job_and_step():
    # The drill must be auditable: a run → job → dominant-step deep-link trail, with
    # the step anchored via #step:N from the timeline's job_url + step number.
    tl = {"job": "tests-web", "job_dur_s": 300.0,
          "run_url": "https://github.com/o/r/actions/runs/999",
          "job_url": "https://github.com/o/r/actions/runs/999/job/77",
          "job_id": 77,
          "steps": [{"name": "Set up job", "number": 1, "start_s": 0.0, "dur_s": 3.0},
                    {"name": "Build", "number": 2, "start_s": 3.0, "dur_s": 60.0},
                    {"name": "run tests", "number": 7, "start_s": 63.0, "dur_s": 230.0},
                    {"name": "Complete job", "number": 9, "start_s": 293.0, "dur_s": 1.0}]}
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/999"},
                   "2026-06-08", {"pipeline": tl})
    assert "**🔗 Audit:**" in md
    assert "[the `tests-web` job](https://github.com/o/r/actions/runs/999/job/77)" in md
    # dominant step "run tests" -> #step:7
    assert "https://github.com/o/r/actions/runs/999/job/77#step:7:1" in md


def test_timeline_absent_falls_back_to_p50_bars():
    # Without a timeline the POLE step level is the prior P50 view (still fine). Scoped to
    # the pole section: the Long pole map's own level-2 lead legitimately says "one after
    # another", so assert on the pole waterfall's leads specifically (the timeline path's
    # "inside that one job" is absent; the P50 path's "Where the job's" is present).
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {})
    pole = md.split("Long pole 1:", 1)[1]
    assert "inside that one job" not in pole
    assert "Where the job's" in pole


def test_p50_chart_marker_follows_the_dominant_category_lead_not_the_longest_step():
    # Faithfulness regression (dominant_step-disagreement class): when NO single step
    # dominates, the category-aware crown (root-cause + agent prompt) names the slowest
    # step of the largest CATEGORY, while the per-step chart's ◀ "addressable lever"
    # marker must agree with it — not blindly flag the single longest step, which lives
    # in a NON-dominant category when a multi-step phase out-aggregates it. Here the
    # `other` phase (5 steps, 170s) out-aggregates the lone 98s `Install dependencies`
    # (install), so the crown is `Run schema generation` (the `other` lead) and the
    # chart's ◀ must land on THAT row, not on `Install dependencies`.
    pole = {
        "check": "Build",
        "workflow_file": ".github/workflows/ci.yml",
        "job": "build",
        "p50_s": 290.0,
        "dominant_step": "Run schema generation + 4 more other steps",
        "dominant_category": "other",
        "dominant_p50_s": 170.0,
        "dominant_share": 0.59,
        "steps": [
            {"step": "Install dependencies", "category": "install", "p50_s": 98.0},
            {"step": "Run schema generation", "category": "other", "p50_s": 42.0},
            {"step": "Run codegen", "category": "other", "p50_s": 40.0},
            {"step": "Run validate", "category": "other", "p50_s": 38.0},
            {"step": "Run lint configs", "category": "other", "p50_s": 30.0},
            {"step": "Run reconcile", "category": "other", "p50_s": 20.0},
        ],
    }
    lines = bp._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    marked = [ln for ln in lines if "◀" in ln]
    assert len(marked) == 1, f"expected exactly one ◀ marker, got: {marked}"
    # The marker must be on the dominant-category lead, NOT the single longest step.
    assert "Run schema generation" in marked[0], marked
    assert "Install dependencies" not in marked[0], marked
    install_rows = [ln for ln in lines if "Install dependencies" in ln]
    assert install_rows and "◀" not in install_rows[0], install_rows


def test_p50_chart_marker_skips_boilerplate_in_the_dominant_category():
    # Faithfulness regression (boilerplate-collision case of the dominant_step-disagreement
    # class): the category-aware crown (`collect_runs._decompose_job_steps` /
    # `_dominant_category_lead`) picks the dominant-category lead over NON-boilerplate steps
    # only. The chart's ◀ marker (`_dom_lead_idx`) must apply the SAME `_NON_WORK_STEP_RE`
    # exclusion — otherwise a slow boilerplate step that shares the dominant category (here
    # "Set up job", category `setup`, the single longest step at 60s) gets the ◀ while the
    # root-cause/prompt crown names the real work lead ("Setup environment", the slowest
    # NON-boilerplate `setup` step). The marker must land on the work lead, not the
    # un-actionable runner-provisioning step.
    pole = {
        "check": "Build",
        "workflow_file": ".github/workflows/ci.yml",
        "job": "build",
        "p50_s": 155.0,
        "dominant_step": "Setup environment",
        "dominant_category": "setup",
        "dominant_p50_s": 45.0,
        "dominant_share": 0.29,
        "steps": [
            {"step": "Set up job", "category": "setup", "p50_s": 60.0},      # boilerplate, longest
            {"step": "Setup environment", "category": "setup", "p50_s": 45.0},  # real setup lead
            {"step": "Run tests", "category": "test", "p50_s": 40.0},
            {"step": "Checkout", "category": "checkout", "p50_s": 10.0},
        ],
    }
    lines = bp._pole_waterfall(pole, leaf=None, timeline=None, log_present=False)
    marked = [ln for ln in lines if "◀" in ln]
    assert len(marked) == 1, f"expected exactly one ◀ marker, got: {marked}"
    # The ◀ must mark the real (non-boilerplate) dominant-category lead, NOT "Set up job".
    assert "Setup environment" in marked[0], marked
    assert "Set up job" not in marked[0], marked
    setup_rows = [ln for ln in lines if "Set up job" in ln]
    assert setup_rows and "◀" not in setup_rows[0], setup_rows


def test_parse_log_buildx_evidence_anchors_on_layer_number_not_substring():
    # Faithfulness/safety: the slowest-layer evidence anchor must match the layer NUMBER
    # exactly, not as a substring — so when a decoy layer like `#70` is present, the
    # excerpt for slowest layer `#7` quotes `#7`'s header + DONE and never `#70`'s. (The
    # anchor uses a trailing space — `#7 ` — so `#7` cannot match `#70`.)
    log = "\n".join([
        "[command]/usr/bin/docker buildx build --output type=local .",
        "#5 [1/4] FROM docker.io/example/base:1.0",
        "#5 DONE 12.0s",
        "#6 [2/4] RUN apt-get install -y build-essential",
        "#6 DONE 26.4s",
        "#7 [3/4] RUN make -j4 all",       # the slowest cold layer - the BIGGEST LEVER
        "#7 DONE 185.9s",
        "#70 [4/4] RUN echo decoy",        # decoy whose number CONTAINS '#7'
        "#70 DONE 5.0s",
        "#8 [5/5] COPY dist /out",
        "#8 DONE 20.4s",
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "buildx-no-cache"
    ev = leaf["evidence"]
    # The slowest layer's header must be quoted and IMMEDIATELY followed by ITS OWN DONE
    # (adjacency = correct anchoring). A substring anchor would mis-pair `#7` with `#70`.
    i = next(k for k, l in enumerate(ev) if "#7 [3/4] RUN make -j4 all" in l)
    assert "#7 DONE 185.9s" in ev[i + 1], ev          # #7's DONE, not #70's
    # The decoy layer (#70) must never be quoted AS the slow layer — its header is out,
    # and #7's anchored DONE is the real 185.9s, not the decoy's 5.0s.
    assert not any("#70 [" in l for l in ev), ev      # decoy header never anchored in


# --------------------------------------------------------------------------- #
# Cross-run magnitude check — the load-bearing number isn't trusted from 1 run
# --------------------------------------------------------------------------- #

def test_leaf_detectors_carry_a_load_bearing_magnitude():
    # Each scalar finding exposes the one number the fix rests on; the sequencing
    # finding (playwright) has none (it's categorical).
    coverage = "\n".join([
        "$ vitest run --coverage --coverage.provider=istanbul",
        " RUN  v4.1.5 /repo/packages/core",
        " Duration  12.18s (transform 12.24s, setup 0ms, import 19.07s, tests 4.13s)",
    ])
    assert bp._parse_log(coverage)["magnitude"]["unit"] == "%"
    assert bp._parse_log(_IMPORT_BOUND_LOG)["magnitude"]["value"] > 0
    pw = bp._parse_log("$ pnpm exec playwright test a.spec.ts\n"
                       "$ pnpm exec playwright test b.spec.ts")
    assert pw["magnitude"] is None


def test_cross_run_check_small_sample_is_a_bracket_not_a_median():
    # A 3-run probe with a TIGHT spread: report the drilled value + bracket (no
    # "median" claim at n<5), verdict = stable, runs linked.
    mag = {"pipeline": {
        "label": "import share of the vitest run", "unit": "%", "this_run": 49.0,
        "escalated": False,
        "values": [{"run_url": ".../49", "value": 49.0, "drilled": True},
                   {"run_url": ".../54", "value": 54.0},
                   {"run_url": ".../51", "value": 51.0}]}}
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {}, mag)
    assert "**49%** in the drilled run" in md      # drilled value, NOT "median"
    assert "median **" not in md                   # no bolded median claim at n<5
    assert "3 runs sampled, range 49%-54%" in md
    assert "tight spread" in md
    assert "- drilled above" in md                 # the drilled run is marked


def test_cross_run_check_wide_escalated_uses_median_and_flags_genuine_spread():
    # A widened sample with a genuinely wide IQR (not just one outlier): real
    # "median", verdict = varies run to run.
    vals = [{"run_url": f".../{v}", "value": float(v)} for v in (30, 35, 45, 50, 55)]
    vals[0]["drilled"] = True
    mag = {"pipeline": {"label": "DB-migration share", "unit": "%", "this_run": 30.0,
                        "escalated": True, "values": vals}}
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {}, mag)
    assert "median **45%**" in md                  # median of 30/35/45/50/55
    assert "5 runs sampled, range 30%-55%" in md
    assert "genuinely wide spread" in md and "varies run to run" in md


def test_cross_run_check_widened_sample_reads_a_lone_outlier_as_stable():
    # The key payoff of escalation: a tight cluster + ONE stray run has a wide raw
    # RANGE but a tiny IQR - it must read as stable (outlier noted), not "varies".
    vals = [{"run_url": f".../{int(v*10)}", "value": v}
            for v in (53.0, 52.5, 53.2, 52.7, 54.0, 36.7, 52.6, 53.1)]
    vals[0]["drilled"] = True
    mag = {"pipeline": {"label": "DB-migration share of the slowest test file",
                        "unit": "%", "this_run": 53.0, "escalated": True, "values": vals}}
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {}, mag)
    # Median of the 8 (mean of the two middles) is 52.85 -> shown to one decimal so
    # it doesn't round to a whole percent that disagrees with the cluster.
    assert "median **52.9%**" in md
    assert "cluster near 52.9%" in md and "1 outlier run" in md
    assert "effectively stable" in md
    assert "varies run to run" not in md           # NOT mislabelled as wide


def test_cross_run_check_skipped_with_one_value():
    # A single value isn't a "range" - don't render a misleading cross-run line.
    mag = {"pipeline": {"label": "x", "unit": "%", "this_run": 52.0,
                        "values": [{"run_url": "u1", "value": 52.0}]}}
    md = bp.render(_doc_one_pole(), {"pipeline": _IMPORT_BOUND_LOG}, {}, {},
                   "2026-06-08", {}, mag)
    assert "runs sampled, range" not in md      # the cross-run line is suppressed


# --- gap → catalog loop, phase 4b/4c plumbing (deterministic capture + signal) ---

def _gap_doc():
    # Two drilled poles: one whose log matches a detector (pytest-no-xdist), one whose
    # log matches nothing (a genuine coverage gap).
    return {"repo": "Acme/widgets",
            "data_sources": {"skill_commit_sha": "abc1234"},
            "captured_at": "2026-06-11",
            "pr_critical_path": {"poles": [
                {"check": "integration", "workflow_file": "robot.yml", "job": "integration",
                 "dominant_step": "Test"},
                {"check": "weird", "workflow_file": "mystery.yml", "job": "weird",
                 "dominant_step": "Run"},
            ]}}


def test_gap_poles_finds_only_undetected_drilled_poles():
    logs = {"robot": _PYTEST_SERIAL_LOG,                 # matches pytest-no-xdist
            "mystery": "random job output\nnothing here matches\nfinished in 300s"}
    gaps = bp._gap_poles(_gap_doc(), logs)
    assert [p["job"] for p, _, _, _ in gaps] == ["weird"]   # only the undetected pole


_NO_MATCH_LOG = "random job output\nnothing here matches\nfinished in 300s"


def _superstring_gap_doc():
    """The vellum-assistant shape that exposed the binding bug: FIVE critical-path poles
    across three workflows, but only TWO drilled (a `data_bundle` with two log entries).
    Several pole names are superstrings of the drilled `Test` pole (`Unit Tests`, `… vet,
    test, build`, `… type check & test`), so a loose substring matcher mis-binds the one
    `Test` log/analysis onto all of them. Only `Test` and `Lint` should ever surface."""
    return {
        "repo": "vellum-ai/vellum-assistant",
        "data_sources": {"skill_commit_sha": "abc1234"},
        "scanned_at": "2026-06-12T20:04:59+00:00",
        "pr_critical_path": {"poles": [
            {"check": "Test", "workflow_file": ".github/workflows/pr-assistant.yaml",
             "job": "Test", "dominant_step": "Test"},
            {"check": "Lint", "workflow_file": ".github/workflows/pr-assistant.yaml",
             "job": "Lint", "dominant_step": "Lint"},
            {"check": "Unit Tests", "workflow_file": ".github/workflows/pr-electron.yaml",
             "job": "Unit Tests", "dominant_step": "Run tests"},
            {"check": "Go vellum-evals-runtime (gofmt, vet, test, build)",
             "workflow_file": ".github/workflows/ci-pr-evals.yaml",
             "job": "Go vellum-evals-runtime (gofmt, vet, test, build)", "dominant_step": "Vet"},
            {"check": "Evals lint, type check & test",
             "workflow_file": ".github/workflows/ci-pr-evals.yaml",
             "job": "Evals lint, type check & test", "dominant_step": "Checkout"},
        ]},
        "data_bundle": {"logs_dir": "x.data", "logs": [
            {"check": "Test", "job": "Test",
             "workflow_file": ".github/workflows/pr-assistant.yaml", "file": "Test.log",
             "html_url": "https://github.com/vellum-ai/vellum-assistant/actions/runs/1/job/2"},
            {"check": "Lint", "job": "Lint",
             "workflow_file": ".github/workflows/pr-assistant.yaml", "file": "Lint.log",
             "html_url": "https://github.com/vellum-ai/vellum-assistant/actions/runs/3/job/4"},
        ]},
    }


def test_gap_poles_drives_off_drilled_set_not_cp_poles():
    # The bug: _gap_poles looped cp.poles (5) and substring-borrowed the Test log onto
    # the 3 undrilled poles -> "5 drilled pole(s)" and 3 poisoned captures. Now it drives
    # off the drill bundle (the 2 entries with their own logs) and binds by exact owner key.
    keys = ["Test", "Lint"]  # what summary._render_keys emits for these 2 entries
    logs = {keys[0]: _NO_MATCH_LOG, keys[1]: _NO_MATCH_LOG}
    gaps = bp._gap_poles(_superstring_gap_doc(), logs)
    assert [p["job"] for p, _, _, _ in gaps] == ["Test", "Lint"]   # NOT the 3 phantoms
    # each pole is bound to ITS OWN key, and run_url is threaded from the entry html_url
    assert [k for _, _, _, k in gaps] == ["Test", "Lint"]
    assert all(p.get("run_url", "").startswith("https://") for p, _, _, _ in gaps)


def test_gap_poles_threads_run_url_from_drill_entry():
    logs = {"Test": _NO_MATCH_LOG, "Lint": _NO_MATCH_LOG}
    gaps = bp._gap_poles(_superstring_gap_doc(), logs)
    by_job = {p["job"]: p for p, _, _, _ in gaps}
    assert by_job["Test"]["run_url"].endswith("/job/2")


def _superstring_fallback_doc():
    """The fixed bug's FALLBACK twin: superstring poles (`Test` ⊂ `Unit Tests`) but NO
    `data_bundle`, so `_gap_poles` takes the `cp.poles` + `_sole_owner_pole` path. A bare
    `Test` key must own NEITHER pole (it's a substring of both); only a fully-qualifying
    key binds. Guards the same class of bug as the headline fix on the path its tests miss."""
    return {"repo": "x/y", "data_sources": {"skill_commit_sha": "abc1234"},
            "pr_critical_path": {"poles": [
                {"check": "Test", "workflow_file": "a.yml", "job": "Test"},
                {"check": "Unit Tests", "workflow_file": "b.yml", "job": "Unit Tests"}]}}


def test_gap_poles_fallback_refuses_ambiguous_substring_key():
    # No data_bundle -> fallback. A bare `Test` key is a substring of BOTH pole names, so
    # _sole_owner_pole refuses it (returns None) rather than borrowing onto the wrong pole.
    gaps = bp._gap_poles(_superstring_fallback_doc(), {"Test": _NO_MATCH_LOG})
    assert gaps == []                                   # refused, not mis-bound
    # A key that uniquely identifies one pole binds to exactly that pole.
    gaps = bp._gap_poles(_superstring_fallback_doc(), {"Unit Tests": _NO_MATCH_LOG})
    assert [p["job"] for p, _, _, _ in gaps] == ["Unit Tests"]


def test_gap_poles_surfaces_drilled_entry_with_no_matching_cp_pole():
    # A drilled data_bundle entry whose (check, workflow_file) isn't in cp.poles must still
    # surface as a gap (synthesized from the entry) — skipping it would silently drop a real
    # gap (a false "clean"). run_url still threads from the entry's html_url.
    import summary
    doc = {"repo": "x/y", "pr_critical_path": {"poles": []},   # no poles to match
           "data_bundle": {"logs": [
               {"check": "Build", "job": "Build", "workflow_file": "ci.yml",
                "file": "Build.log",
                "html_url": "https://github.com/x/y/actions/runs/7/job/8"}]}}
    key = summary._render_keys(doc["data_bundle"]["logs"])[0]   # the owner key the renderer uses
    gaps = bp._gap_poles(doc, {key: _NO_MATCH_LOG})
    assert [p["job"] for p, _, _, _ in gaps] == ["Build"]
    assert gaps[0][0]["run_url"].endswith("/job/8")


def test_gap_poles_warns_loudly_when_key_derivation_fails(monkeypatch, capsys):
    # The no-silent-drop rule: when render-key derivation fails, _gap_poles degrades to the
    # fallback matcher, but must SAY SO (a silent downgrade could under-report gaps on the
    # very pipeline this fix exists to make trustworthy). Doc HAS a data_bundle (to enter the
    # key-derivation path) with a cleanly-bindable pole so the fallback still surfaces it.
    import summary
    monkeypatch.setattr(summary, "_render_keys",
                        lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))
    doc = {"repo": "x/y", "pr_critical_path": {"poles": [
               {"check": "Build", "workflow_file": "ci.yml", "job": "Build"}]},
           "data_bundle": {"logs": [
               {"check": "Build", "job": "Build", "workflow_file": "ci.yml",
                "file": "Build.log"}]}}
    gaps = bp._gap_poles(doc, {"Build": _NO_MATCH_LOG})
    assert [p["job"] for p, _, _, _ in gaps] == ["Build"]   # fallback still surfaces it
    err = capsys.readouterr().err
    assert "gap-key derivation failed" in err and "UNDER-REPORTED" in err


def test_emit_gap_signal_refuses_ambiguously_keyed_analysis(tmp_path: Path, capsys):
    # The transcript footgun: an analysis attached under a LOOSE key that only fuzzy-matches
    # must NOT be stamped onto the pole. Key `tes` is a substring of `Test` but is not the
    # exact owner key -> skip + loud warning naming the correct key, capture nothing.
    logs = {"Test": _NO_MATCH_LOG, "Lint": _NO_MATCH_LOG}
    analyses = {"tes": {"cause": "bun 1448 files", "evidence": ["x"]}}
    bp._emit_gap_signal(_superstring_gap_doc(), logs, analyses, gaps_root=tmp_path)
    assert not any(tmp_path.iterdir())                       # nothing captured
    err = capsys.readouterr().err
    assert "skipped 1 gap" in err and "exact owner key" in err
    assert "--analysis Test=PATH" in err                     # the right key, surfaced


def test_emit_gap_signal_binds_analysis_by_exact_key_only(tmp_path: Path, capsys):
    # The correct exact keys capture exactly their own pole — and the one Test analysis
    # is NOT also stamped onto Lint (no cross-pole bleed).
    logs = {"Test": _NO_MATCH_LOG, "Lint": _NO_MATCH_LOG}
    analyses = {"Test": {"cause": "bun test", "evidence": ["a"]},
                "Lint": {"cause": "eslint no cache", "evidence": ["b"]}}
    bp._emit_gap_signal(_superstring_gap_doc(), logs, analyses, gaps_root=tmp_path)
    dirs = sorted(p.name for p in tmp_path.iterdir())
    assert dirs == ["vellum-ai-vellum-assistant__Lint", "vellum-ai-vellum-assistant__Test"]
    test_a = json.loads((tmp_path / "vellum-ai-vellum-assistant__Test"
                         / "analysis.json").read_text())
    lint_a = json.loads((tmp_path / "vellum-ai-vellum-assistant__Lint"
                         / "analysis.json").read_text())
    assert test_a["cause"] == "bun test" and lint_a["cause"] == "eslint no cache"
    # provenance threaded: scanned_at from top-level, run_url from the drill entry
    meta = json.loads((tmp_path / "vellum-ai-vellum-assistant__Test" / "meta.json").read_text())
    assert meta["scanned_at"] == "2026-06-12T20:04:59+00:00"
    assert meta["run_url"].endswith("/job/2")


def test_emit_gap_signal_first_render_prints_exact_keys(tmp_path: Path, capsys):
    # First render (no analysis): the signal must hand the agent the EXACT per-pole key to
    # use, so it can't guess a loose key that collides.
    logs = {"Test": _NO_MATCH_LOG, "Lint": _NO_MATCH_LOG}
    bp._emit_gap_signal(_superstring_gap_doc(), logs, {}, gaps_root=tmp_path)
    err = capsys.readouterr().err
    assert "2 drilled pole(s)" in err                        # not the inflated 5
    assert "--analysis Test=PATH" in err and "--analysis Lint=PATH" in err


def test_emit_gap_signal_warns_on_missing_provenance(tmp_path: Path, capsys):
    # A captured gap with no scanned_at/run_url is the feedstock's missing audit trail —
    # warn, don't write it silently (provenance self-check discipline).
    import summary
    doc = {"repo": "x/y",                                  # no scanned_at / captured_at
           "pr_critical_path": {"poles": [
               {"check": "Build", "workflow_file": "ci.yml", "job": "Build"}]},
           "data_bundle": {"logs": [                       # entry has NO html_url -> no run_url
               {"check": "Build", "job": "Build", "workflow_file": "ci.yml",
                "file": "Build.log"}]}}
    key = summary._render_keys(doc["data_bundle"]["logs"])[0]
    bp._emit_gap_signal(doc, {key: _NO_MATCH_LOG},
                        {key: {"cause": "x", "evidence": ["a"]}}, gaps_root=tmp_path)
    err = capsys.readouterr().err
    assert "captured 1 gap" in err
    assert "no scanned_at/run_url provenance for: Build" in err


def test_emit_gap_signal_maintainer_next_action_is_imperative(tmp_path: Path, capsys):
    # 4c never fired in the dogfood run despite all preconditions. After a successful
    # capture in maintainer source context, the signal must print an IMPERATIVE next
    # action naming the exact prepare command + the captured slugs — not a passive pointer.
    logs = {"Test": _NO_MATCH_LOG, "Lint": _NO_MATCH_LOG}
    analyses = {"Test": {"cause": "bun", "evidence": ["a"]},
                "Lint": {"cause": "eslint", "evidence": ["b"]}}
    bp._emit_gap_signal(_superstring_gap_doc(), logs, analyses, gaps_root=tmp_path)
    err = capsys.readouterr().err
    # _is_maintainer_source() is True in this tracked checkout (asserted elsewhere).
    assert "REQUIRED NEXT ACTION" in err and "phase 4c" in err
    assert "draft_detector.py prepare vellum-ai-vellum-assistant__Test" in err
    assert "vellum-ai-vellum-assistant__Lint" in err


def test_capture_gap_writes_the_full_bundle(tmp_path: Path):
    doc = _gap_doc()
    pole = doc["pr_critical_path"]["poles"][1]
    dest = bp._capture_gap("Acme/widgets", pole, "raw log text",
                           {"cause": "x", "evidence": ["l1"]}, doc, tmp_path)
    assert dest.name == "Acme-widgets__weird"
    assert (dest / "job.log").read_text() == "raw log text"
    analysis = json.loads((dest / "analysis.json").read_text())
    assert analysis["cause"] == "x"
    meta = json.loads((dest / "meta.json").read_text())
    assert meta["repo"] == "Acme/widgets" and meta["job"] == "weird"
    assert meta["workflow_file"] == "mystery.yml" and meta["dominant_step"] == "Run"
    assert meta["skill_commit_sha"] == "abc1234"


def test_emit_gap_signal_captures_when_analysis_attached(tmp_path: Path, capsys):
    # The gap-fill analysis is attached (the re-render): 4b capture fires automatically,
    # and the stderr signal names the captured gap. This is the path that used to be
    # skippable prose - now it rides on the render the agent always runs.
    logs = {"mystery": "random output, no detector match, ran for a while"}
    analyses = {"mystery": {"cause": "serial thing", "evidence": ["x"]}}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "weird", "workflow_file": "mystery.yml", "job": "weird"}]}}
    bp._emit_gap_signal(doc, logs, analyses, gaps_root=tmp_path)
    assert (tmp_path / "Acme-widgets__weird" / "analysis.json").exists()
    err = capsys.readouterr().err
    assert "CATALOG GAP" in err and "captured 1 gap" in err


def test_emit_gap_signal_warns_but_does_not_capture_without_analysis(tmp_path: Path, capsys):
    # First render (no analysis yet): the gap is still announced loudly so it can't be
    # missed, but nothing is captured (no analysis to persist) - the signal tells the
    # agent to attach one on re-render.
    logs = {"mystery": "random output, no detector match, ran for a while"}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "weird", "workflow_file": "mystery.yml", "job": "weird"}]}}
    bp._emit_gap_signal(doc, logs, {}, gaps_root=tmp_path)
    assert not any(tmp_path.iterdir())                  # nothing captured
    err = capsys.readouterr().err
    assert "CATALOG GAP" in err and "--analysis" in err


def test_emit_gap_signal_is_silent_with_no_gaps(tmp_path: Path, capsys):
    # Every drilled pole matched a detector: no gap, no signal, no capture.
    logs = {"robot": _PYTEST_SERIAL_LOG}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "integration", "workflow_file": "robot.yml", "job": "integration"}]}}
    bp._emit_gap_signal(doc, logs, {}, gaps_root=tmp_path)
    assert not any(tmp_path.iterdir())
    assert capsys.readouterr().err == ""


def test_is_maintainer_source_true_in_this_checkout():
    # These tests run from the tracked monorepo source, so the 4c maintainer probe
    # must report True (it's what lets the gap signal tell a maintainer to draft a
    # detector). A symlinked install resolves into the same git tree.
    assert bp._is_maintainer_source() is True


def test_is_maintainer_source_false_when_git_probe_fails(monkeypatch):
    # The False path gates whether installed/end-user runs STOP at capture vs launch
    # the 4c detector-draft pointer. A git-missing / timeout degrades to False (an
    # installed copy must never tell an end user to draft detectors), not a crash.
    def boom(*_a, **_k):
        raise FileNotFoundError("git not on PATH")
    monkeypatch.setattr(bp.subprocess, "run", boom)
    assert bp._is_maintainer_source() is False
    # A non-zero git exit (path not tracked = an installed/vendored copy) is also False.
    monkeypatch.setattr(bp.subprocess, "run",
                        lambda *_a, **_k: bp.subprocess.CompletedProcess([], 1))
    assert bp._is_maintainer_source() is False


def test_gaps_root_default_none_on_installed_copy(monkeypatch):
    # Installed/vendored copy: not maintainer source → None, WITHOUT consulting git. This is
    # the end-user leak-prevention path — None makes _emit_gap_signal skip capture, so the
    # capture dir never lands under the skill (which the installer would ship).
    monkeypatch.setattr(bp, "_is_maintainer_source", lambda: False)
    def _no_git(*_a, **_k):
        raise AssertionError("git must not be consulted once not-a-maintainer-source is known")
    monkeypatch.setattr(bp.subprocess, "run", _no_git)
    assert bp._gaps_root_default() is None


def test_gaps_root_default_roots_at_repo_top_outside_skill(monkeypatch):
    # Tracked-source checkout: roots at the git toplevel, OUTSIDE skills/<name>/.
    monkeypatch.setattr(bp, "_is_maintainer_source", lambda: True)
    monkeypatch.setattr(bp.subprocess, "run",
                        lambda *_a, **_k: bp.subprocess.CompletedProcess([], 0, "/repo/root\n"))
    root = bp._gaps_root_default()
    assert root == Path("/repo/root") / ".ci-speedup-gaps"
    assert root.name == ".ci-speedup-gaps"
    assert "skills/ci-speedup" not in root.as_posix()


def test_gaps_root_default_warns_and_returns_none_when_root_unresolved(monkeypatch, capsys):
    # Maintainer source but the repo root won't resolve (git race/timeout/non-zero/empty) →
    # None, but LOUDLY — distinguished from the silent installed-copy skip so a maintainer's
    # feedstock loss is visible, not silent.
    monkeypatch.setattr(bp, "_is_maintainer_source", lambda: True)
    def _boom(*_a, **_k):
        raise FileNotFoundError("git vanished mid-run")
    monkeypatch.setattr(bp.subprocess, "run", _boom)
    assert bp._gaps_root_default() is None
    assert "could not resolve the repo root" in capsys.readouterr().err
    # Non-zero rc and empty/whitespace stdout reach the same loud None (the `and r.stdout.strip()`
    # guard — a regression here would root captures at `Path("")/.ci-speedup-gaps`).
    monkeypatch.setattr(bp.subprocess, "run",
                        lambda *_a, **_k: bp.subprocess.CompletedProcess([], 1, ""))
    assert bp._gaps_root_default() is None
    monkeypatch.setattr(bp.subprocess, "run",
                        lambda *_a, **_k: bp.subprocess.CompletedProcess([], 0, "   \n"))
    assert bp._gaps_root_default() is None


def test_emit_gap_signal_skips_capture_on_installed_copy(tmp_path: Path, capsys, monkeypatch):
    # The leak-prevention contract end to end: on an installed copy the resolver returns None,
    # so _emit_gap_signal (called with gaps_root=None, as the sole production caller does)
    # captures NOTHING and prints NO banner — loop machinery never reaches an end user. Must
    # monkeypatch the resolver: in this tracked-source checkout the real one returns a live
    # <repo-root>/.ci-speedup-gaps (precious, gitignored), which a test must never write to.
    monkeypatch.setattr(bp, "_gaps_root_default", lambda: None)
    logs = {"mystery": "random output, no detector match, ran for a while"}
    analyses = {"mystery": {"cause": "serial thing", "evidence": ["x"]}}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "weird", "workflow_file": "mystery.yml", "job": "weird"}]}}
    bp._emit_gap_signal(doc, logs, analyses, gaps_root=None)
    assert capsys.readouterr().err == ""          # no CATALOG GAP banner on an installed copy
    assert not any(tmp_path.iterdir())            # nothing captured anywhere we provided


def test_emit_gap_signal_consults_default_root_when_none(tmp_path: Path, capsys, monkeypatch):
    # The complement (proves the wiring isn't dead): gaps_root=None must fall through to
    # _gaps_root_default(); point it at tmp_path and assert the capture lands there.
    monkeypatch.setattr(bp, "_gaps_root_default", lambda: tmp_path)
    logs = {"mystery": "random output, no detector match, ran for a while"}
    analyses = {"mystery": {"cause": "serial thing", "evidence": ["x"]}}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "weird", "workflow_file": "mystery.yml", "job": "weird"}]}}
    bp._emit_gap_signal(doc, logs, analyses, gaps_root=None)
    assert (tmp_path / "Acme-widgets__weird" / "analysis.json").exists()
    assert "CATALOG GAP" in capsys.readouterr().err


def test_emit_gap_signal_degrades_when_capture_raises(tmp_path: Path, capsys, monkeypatch):
    # The stated invariant: a capture I/O error degrades to a warning, never fails the
    # render (the report is already written when this runs). With an analysis attached,
    # force _capture_gap to raise OSError and assert (a) no exception propagates and
    # (b) the message names the I/O failure - NOT "attach --analysis" (which the
    # maintainer already did).
    monkeypatch.setattr(bp, "_capture_gap",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    logs = {"mystery": "random output, no detector match, ran for a while"}
    analyses = {"mystery": {"cause": "serial thing", "evidence": ["x"]}}
    doc = {"repo": "Acme/widgets", "pr_critical_path": {"poles": [
        {"check": "weird", "workflow_file": "mystery.yml", "job": "weird"}]}}
    bp._emit_gap_signal(doc, logs, analyses, gaps_root=tmp_path)   # must not raise
    err = capsys.readouterr().err
    assert "gap capture failed" in err and "disk full" in err
    assert "capture to `.ci-speedup-gaps/` failed" in err
    assert "attach the gap-fill analysis" not in err              # not the wrong fix


def test_also_noticed_reframes_wall_clock_lever_not_off_path():
    # Regression (Infisical/infisical): OPT24 ("Long Test Job Without Sharding") sets a
    # POSITIVE credited wall-clock saving (`wall_clock_p50_s` survives the cascade only when
    # the job is the slowest concurrent check — i.e. it IS on the critical path) and rm=0.
    # The appendix used to assert "off the merge-gating critical path, so ~0 developer
    # wall-clock" for it, contradicting the report's own spine: sharding the long pole cuts
    # wall-clock. A finding with credited wall-clock must NOT be framed as off-path/bill-only.
    opt24 = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "f1",
             "workflow_file": "ci.yml", "line": 5, "affected_jobs": ["Run integration test"],
             "wall_clock_p50_s": 880.0, "runner_min_saving": 0.0}
    lines, _, _ = bp._also_noticed_block([opt24], "http://cat")
    body = "\n".join(lines)
    # The false off-path / ~0 wall-clock framing must be gone for a wall-clock lever.
    assert "off the merge-gating critical path, so ~0 developer wall-clock" not in body
    # ...and replaced by an honest on-critical-path / wall-clock disclosure.
    assert "wall-clock" in body.lower()
    assert "critical path" in body.lower()
    # A genuinely bill-only hygiene finding still gets the off-path / ~0 wall-clock framing.
    opt33 = {"pattern": "OPT33", "title": "No Draft PR Gating", "id": "f2",
             "workflow_file": "y.yml", "runner_min_saving": 100}
    bill_body = "\n".join(bp._also_noticed_block([opt33], "http://cat")[0])
    assert "off the merge-gating critical path, so ~0 developer wall-clock" in bill_body


def test_also_noticed_wall_clock_lever_not_capped_out_by_bill_only_hygiene():
    # Regression (greptile P2 on the OPT24 reframe): a wall-clock lever carries rm=0, so the
    # bill-desc ranking sank it to the bottom — and a repo with >_ALSO_NOTICED_CAP bill-only
    # hygiene patterns pushed it past the "+N more not shown" cap, suppressing the very lever
    # the reframe exists to surface. Wall-clock-saving groups must sort first and always render.
    bill_only = [
        {"pattern": f"OPT{n}", "title": f"Hygiene {n}", "id": f"h{n}",
         "workflow_file": f"w{n}.yml", "runner_min_saving": 500 - n}
        for n in range(bp._ALSO_NOTICED_CAP + 4)  # comfortably more than the cap
    ]
    opt24 = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "f1",
             "workflow_file": "ci.yml", "line": 5, "affected_jobs": ["Run integration test"],
             "wall_clock_p50_s": 880.0, "runner_min_saving": 0.0}
    lines, _, _ = bp._also_noticed_block(bill_only + [opt24], "http://cat")
    body = "\n".join(lines)
    # The wall-clock lever must render in the body, NOT be hidden behind the "+N more" cap.
    assert "OPT24" in body
    assert "more hygiene pattern(s)" in body  # the cap still fired on the bill-only surplus
    # And it leads the section (sorts ahead of the highest-bill hygiene group, OPT0).
    assert body.index("OPT24") < body.index("OPT0")


def test_also_noticed_blurb_not_contradicted_by_wall_clock_lever():
    # Regression (greptile P2): the section blurb unconditionally asserted "These do not sit
    # on the merge-gating critical path", contradicting the per-row "sits ON the critical path"
    # note added for a wall-clock lever. When a lever is present the blurb must be qualified.
    opt24 = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "f1",
             "workflow_file": "ci.yml", "line": 5, "affected_jobs": ["Run integration test"],
             "wall_clock_p50_s": 880.0, "runner_min_saving": 0.0}
    opt33 = {"pattern": "OPT33", "title": "No Draft PR Gating", "id": "f2",
             "workflow_file": "y.yml", "runner_min_saving": 100}
    blurb = "\n".join(bp._also_noticed_block([opt24, opt33], "http://cat")[0]).split("</summary>")[0]
    # The blanket "These do not sit on the merge-gating critical path" must be softened, and
    # the blurb must point the reader at the inline exception so header and row don't conflict.
    assert "These do **not** sit on the merge-gating critical path" not in blurb
    assert "exception" in blurb.lower()
    # A section with NO wall-clock lever keeps the residual-section blurb, with no exception callout.
    plain = "\n".join(bp._also_noticed_block([opt33], "http://cat")[0])
    assert "stay outside the wall-clock-neutral runner-minute section" in plain
    assert "removes little or no developer wall-clock" in plain
    assert "exception" not in plain.lower()


def test_also_noticed_off_path_finding_with_uncapped_wall_clock_stays_bill_only():
    # Guard the INVERSE of the OPT24 reframe: a genuinely off-path finding can still carry a
    # positive `wall_clock_uncapped_p50_s` (the pre-cascade raw), but the cross-workflow
    # cascade floored its CREDITED `wall_clock_p50_s` to 0 precisely because it is off the
    # critical path. It must keep bill-only / "~0 wall-clock" framing. `_saves_wall_clock`
    # must read the capped field — a "simplify" to the uncapped field would mis-promote EVERY
    # off-path finding to a critical-path lever (the exact inverse of the bug this PR fixes).
    off_path = {"pattern": "OPT33", "title": "No Draft PR Gating", "id": "f1",
                "workflow_file": "y.yml", "runner_min_saving": 100,
                "wall_clock_p50_s": 0.0, "wall_clock_uncapped_p50_s": 500.0}
    assert bp._saves_wall_clock(off_path) is False
    body = "\n".join(bp._also_noticed_block([off_path], "http://cat")[0])
    assert "off the merge-gating critical path, so ~0 developer wall-clock" in body


def test_also_noticed_wall_clock_framing_scans_all_group_members():
    # `_group_saves_wall_clock` deliberately scans EVERY member (not just ms[0]) to match the
    # `max(... for m in ms)` summary metric. A group whose wall-clock-saving member is not
    # first must still be detected as a lever, framed by its wall-clock saving, and sorted
    # first. This fails if the check regresses to `_saves_wall_clock(ms[0])`.
    bill_member = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "a",
                   "workflow_file": "ci.yml", "line": 1, "affected_jobs": ["job a"],
                   "wall_clock_p50_s": 0.0, "runner_min_saving": 0.0}
    wc_member = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "b",
                 "workflow_file": "ci.yml", "line": 2, "affected_jobs": ["job b"],
                 "wall_clock_p50_s": 880.0, "runner_min_saving": 0.0}
    # Saver is the SECOND member; ms[0] alone would miss it.
    assert bp._group_saves_wall_clock([bill_member, wc_member]) is True
    opt33 = {"pattern": "OPT33", "title": "No Draft PR Gating", "id": "c",
             "workflow_file": "y.yml", "runner_min_saving": 100}
    body = "\n".join(bp._also_noticed_block([opt33, bill_member, wc_member], "http://cat")[0])
    # The OPT24 group renders as a wall-clock lever and leads the section despite OPT33's bill.
    assert body.index("OPT24") < body.index("OPT33")
    assert "sits ON the merge-gating critical path" in body


def test_saves_wall_clock_floors_subsecond_and_rare_check_savings():
    # Regression (Opentrons/opentrons): `_saves_wall_clock` used a bare `> 0` test with no
    # magnitude floor, so ANY positive `wall_clock_p50_s` was labeled as sitting "ON the
    # merge-gating critical path (a long pole)". The concurrency cascade floors an OFF-path
    # workflow to 0, but it bounds against concurrent checks only — NOT against the spine's
    # rare/conditional-presence demotion — so a rare/opt-in job (the slowest check only on the
    # minority of PRs it runs) kept a small positive saving and got the long-pole label,
    # contradicting the report's own spine, whose footnote demotes that job as opt-in.
    # A sub-second OPT24 (0.3s, renders "~0s wall-clock") and a 2.2s OPT28 on a demoted
    # conditional job must NOT be credited as long poles.
    subsecond = {"pattern": "OPT24", "wall_clock_p50_s": 0.3}   # renders "~0s wall-clock"
    rare_check = {"pattern": "OPT28", "wall_clock_p50_s": 2.2}  # demoted conditional job
    assert bp._saves_wall_clock(subsecond) is False
    assert bp._saves_wall_clock(rare_check) is False
    # A genuine sharding/structural long-pole saving (minutes) is still credited.
    real_pole = {"pattern": "OPT24", "wall_clock_p50_s": 880.0}
    assert bp._saves_wall_clock(real_pole) is True
    # The floor is the gate: exactly at the floor counts, just under it does not.
    assert bp._saves_wall_clock({"wall_clock_p50_s": bp._WALL_CLOCK_LONG_POLE_FLOOR_S}) is True
    assert bp._saves_wall_clock(
        {"wall_clock_p50_s": bp._WALL_CLOCK_LONG_POLE_FLOOR_S - 0.1}) is False
    # The group summary uses `max(... for m in ms)`; a single sub-floor occurrence must NOT
    # relabel a whole bill-only group as a wall-clock lever (the OPT28-group "~2s wall-clock"
    # mislabel). With every member sub-floor the group claims no spine.
    group = [{"pattern": "OPT28", "wall_clock_p50_s": 2.2, "runner_min_saving": 0.0},
             {"pattern": "OPT28", "wall_clock_p50_s": 0.3, "runner_min_saving": 0.0}]
    assert bp._group_saves_wall_clock(group) is False
    # And the rendered appendix must not claim the sub-floor group sits ON the critical path.
    opt28_grp = [{"pattern": "OPT28", "title": "Rare Conditional Check", "id": f"f{i}",
                  "workflow_file": "ci.yml", "affected_jobs": ["confirm-g-code"],
                  "wall_clock_p50_s": wc, "runner_min_saving": 5.0}
                 for i, wc in enumerate((2.2, 0.3))]
    body = "\n".join(bp._also_noticed_block(opt28_grp, "http://cat")[0])
    assert "sits ON the merge-gating critical path" not in body


def test_data_driven_for_pole_skips_subfloor_credited_finding():
    # The spine-acknowledgment join must not fire for a sub-floor credited finding: a 2.2s
    # OPT28 on a demoted conditional job is NOT a long pole, so it makes no "sits ON the
    # critical path ... See the spine above" claim the spine would have to acknowledge back.
    pole = {"check": "confirm-g-code", "job": "confirm-g-code"}
    subfloor = {"pattern": "OPT28", "affected_jobs": ["confirm-g-code"],
                "wall_clock_p50_s": 2.2, "runner_min_saving": 5.0}
    assert bp._data_driven_for_pole(pole, [subfloor]) == []
    # A genuine credited long-pole finding on the same job still joins.
    real = {"pattern": "OPT24", "affected_jobs": ["confirm-g-code"],
            "wall_clock_p50_s": 744.8, "runner_min_saving": 0.0}
    assert bp._data_driven_for_pole(pole, [real]) == [real]


def test_hygiene_prompt_subfloor_positive_wallclock_is_not_framed_off_path():
    # The 30s `_saves_wall_clock` floor declines to call a small positive wall-clock saving
    # a "long pole" — correct. But the bill-only prompt branch then asserted the OPPOSITE of
    # the measured value: "off the merge-gating critical path, so ~0 developer wall-clock (a
    # cloud-bill cut)". For a rm=0 finding carrying a genuine 20s on-path saving (a near-tie
    # with the next concurrent check) that line is wrong three ways (it IS on the path, has
    # NO bill cut, and does NOT have ~0 wall-clock). The prompt must instead state the
    # measured fact: a sub-threshold saving, not credited as a long pole.
    members = [{"pattern": "OPT24", "wall_clock_p50_s": 20.0, "runner_min_saving": 0.0,
                "affected_jobs": ["test"], "workflow_file": "ci.yml", "line": 5,
                "evidence": "the test job runs ~10m serially"}]
    prompt = "\n".join(bp._hygiene_prompt("OPT24", "Long Test Job", members, "http://cat"))
    # The contradictory off-path / cloud-bill framing must be gone for a positive saving...
    assert "off the merge-gating critical path" not in prompt
    assert "cloud-bill cut" not in prompt
    # ...replaced by the honest below-threshold framing that quotes the measured value.
    assert "below the long-pole threshold" in prompt
    assert "~20s" in prompt
    # A genuinely off-path finding (wall-clock == 0) KEEPS the off-path framing — the fix
    # must not blanket-rewrite every bill-only prompt.
    off_path = [{"pattern": "OPT5", "wall_clock_p50_s": 0.0, "runner_min_saving": 1200.0,
                 "affected_jobs": ["lint"], "workflow_file": "ci.yml", "line": 9,
                 "evidence": "lint reinstalls deps"}]
    off_prompt = "\n".join(bp._hygiene_prompt("OPT5", "Cache deps", off_path, "http://cat"))
    assert "off the merge-gating critical path" in off_prompt
    assert "below the long-pole threshold" not in off_prompt


def test_group_evidence_falls_back_when_driver_has_no_evidence():
    # `_group_evidence(prefer=driver)` quotes the driver's evidence when it has one, but the
    # driver (the max-wall-clock member) may carry no evidence string — then it must fall
    # back to the first member that DOES, so a driver-without-evidence still shows the
    # group's available evidence rather than nothing.
    driver = {"wall_clock_p50_s": 200.0}                       # no evidence
    other = {"wall_clock_p50_s": 0.0, "evidence": "the e2e suite runs serially"}
    assert bp._group_evidence([driver, other], prefer=driver) == "the e2e suite runs serially"
    # With no `prefer` it is the historical first-with-evidence behaviour.
    assert bp._group_evidence([driver, other]) == "the e2e suite runs serially"
    # When the driver HAS evidence, that wins over a later member's.
    driver_ev = {"wall_clock_p50_s": 200.0, "evidence": "the driver's own evidence"}
    assert bp._group_evidence([driver_ev, other], prefer=driver_ev) == "the driver's own evidence"


def test_turbo_time_secs_parses_unit_edges():
    # The unit alternation must try `ms` BEFORE bare `m`/`s` so a millisecond tail isn't
    # misread as minutes/seconds, and `h`/`m`/`s` compose. Reached only via `_parse_log`
    # with m/s values elsewhere, so lock the edges directly.
    assert bp._turbo_time_secs("9m48.756s") == 9 * 60 + 48.756
    assert bp._turbo_time_secs("1h2m3s") == 3600 + 120 + 3
    assert bp._turbo_time_secs("3.013s") == 3.013
    assert bp._turbo_time_secs("250ms") == 0.25            # ms, not 250 minutes
    assert bp._turbo_time_secs("1m20.171s") == 60 + 20.171
    assert bp._turbo_time_secs("no time here") is None     # nothing parseable → None


def test_parse_log_turbo_falls_back_to_most_rebuilt_when_no_time_line():
    # The slowest-invocation selection keys off each block's `Time:` line; when NO block has
    # a parseable `Time:` (an older turbo, or piped output that dropped the summary timing),
    # it must fall back to the most-rebuilt summary rather than crash or pick nothing.
    log = "\n".join([
        "$ turbo run prepare",
        "   • Remote caching disabled",
        *[f"cache miss, executing prep{i}" for i in range(6)],
        "Cached:    1 cached, 2 total",          # barely rebuilt
        "$ turbo run build",
        *[f"cache miss, executing build{i}" for i in range(6)],
        "Cached:    0 cached, 8 total",          # most rebuilt → must be chosen
    ])
    leaf = bp._parse_log(log)
    assert leaf is not None
    # 0/8 (the most-rebuilt block) drives it: 100% miss.
    assert leaf["magnitude"]["value"] == 100.0


def test_also_noticed_distinct_opt73_levers_render_as_separate_rows():
    # Regression (embrace-io/embrace-android-sdk): OPT73 is the cross-cluster floor lever,
    # and EACH finding is a DISTINCT lever — its own shared step, its own cluster of jobs,
    # its own evidence and magnitude — NOT a fungible occurrence of one fix recipe. The
    # appendix grouped every OPT73 finding into a single row, which (1) showed only the
    # first member's evidence, (2) sized the row at the MAX leg's wall-clock — hiding the
    # smaller leg's evidence and over-sizing it. Two genuinely distinct levers must render
    # as two separate rows, each carrying its own evidence and its own magnitude.
    f17 = {"pattern": "OPT73", "title": "Shared step recurs across the cluster", "id": "f17",
           "workflow_file": ".github/workflows/android-emulator-tests.yml", "line": 0,
           "affected_jobs": ["emulator (29, .)"], "wall_clock_p50_s": 143.1,
           "runner_min_saving": 4000.0, "severity": "HIGH",
           "evidence": "the `Run tests on android emulator` step is 88% of the slowest "
                       "cluster job `emulator (29, .)` — a cluster-floor lever"}
    f18 = {"pattern": "OPT73", "title": "Shared step recurs across the cluster", "id": "f18",
           "workflow_file": ".github/workflows/ci-gradle.yml", "line": 0,
           "affected_jobs": ["gradle (build)"], "wall_clock_p50_s": 43.0,
           "runner_min_saving": 300.0, "severity": "HIGH",
           "evidence": "the `actions/cache` step recurs across the gradle cluster "
                       "— a cluster-floor lever"}
    lines, n, _ = bp._also_noticed_block([f17, f18], "http://cat")
    body = "\n".join(lines)
    # Both levers' OWN evidence must render — not just the larger leg's.
    assert "Run tests on android emulator" in body          # f17's evidence
    assert "actions/cache" in body                          # f18's evidence (was hidden)
    # The smaller leg must be sized at its OWN magnitude (~43s), not the max leg's 2m 23s.
    assert "~43s" in body                                   # f18 summary, was oversized
    assert "~2m 23s" in body                                # f17 summary, unchanged
    # Two distinct levers ⇒ two grouped rows, not one folded row.
    assert n == 2
    assert body.count("<summary>") == 2


def test_also_noticed_bill_only_group_evidence_covers_all_listed_jobs():
    # Regression (OPT12-style bill-only aggregate): a bill-only "Also noticed" group folds
    # multiple FUNGIBLE occurrences of one fix recipe into ONE row whose displayed magnitude
    # is an aggregate over ALL members and whose "Where" lists EVERY member's job. The
    # Evidence line was sourced from only the FIRST member (`_group_evidence(ms)`), so a
    # second member's job sat in "Where" unexplained and that member's evidence was silently
    # dropped — locations and evidence disagreed. Unlike the wall-clock-lever branch (which
    # sources evidence from the single displayed driver), the bill-only branch must COMPOSE
    # evidence across the listed members so every job in "Where" is explained.
    f1 = {"pattern": "OPT12", "title": "Redundant setup preamble", "id": "f1",
          "workflow_file": ".github/workflows/ci.yaml", "line": 12,
          "affected_jobs": ["Build-Docs", "Deploy-Pages"],
          "wall_clock_p50_s": 0.0, "runner_min_saving": 800.0, "severity": "MEDIUM",
          "evidence": "2 jobs share an identical 4-step setup preamble (Build-Docs, "
                      "Deploy-Pages)"}
    f10 = {"pattern": "OPT12", "title": "Redundant setup preamble", "id": "f10",
           "workflow_file": ".github/workflows/integration-test.yml", "line": 7,
           "affected_jobs": ["spark-integration-test", "spark-connect-integration-test"],
           "wall_clock_p50_s": 0.0, "runner_min_saving": 200.0, "severity": "MEDIUM",
           "evidence": "2 jobs share an identical setup preamble (spark-integration-test, "
                       "spark-connect-integration-test)"}
    lines = bp._also_noticed_block([f1, f10], "http://cat")[0]
    body = "\n".join(lines)
    # The two fungible occurrences fold into ONE bill-only row (same pattern, both rm-only).
    assert body.count("<summary>") == 1
    # ...whose "Where" lists BOTH members' jobs.
    where = next(ln for ln in body.splitlines() if ln.startswith("**Where:**"))
    assert "Build-Docs" in where and "spark-integration-test" in where
    # The single Evidence line must explain BOTH listed members, not just the first —
    # f10's evidence (spark-connect-integration-test) was being dropped, leaving its job in
    # "Where" unexplained.
    ev_line = next(ln for ln in body.splitlines() if ln.startswith("**Evidence:**"))
    assert "Build-Docs" in ev_line                       # f1's evidence (kept)
    assert "spark-connect-integration-test" in ev_line   # f10's evidence (was dropped)
    # The embedded agent prompt's "What ci-speedup saw" shares the same evidence source and
    # must reconcile with "Where" too.
    saw = next(ln for ln in body.splitlines() if ln.startswith("What ci-speedup saw:"))
    assert "Build-Docs" in saw and "spark-connect-integration-test" in saw


def test_also_noticed_wall_clock_magnitude_and_evidence_describe_same_member():
    # Regression (flwrlabs/flower): a wall-clock-lever group's displayed magnitude is
    # `max(wall_clock_p50_s for m in ms)` (ONE member), but the evidence was sourced from the
    # FIRST member carrying an evidence string — a DIFFERENT member. So the headline (one
    # on-path job's ~3m09s) and the evidence (a different, 0s, off-path job) described two
    # jobs that don't reconcile. The magnitude and the evidence MUST come from the same
    # member (the one whose wall-clock drives the displayed figure). OPT24 groups by pattern
    # id, so both members land in ONE group and exercise the driver-vs-first-evidence path.
    off_path = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "1",
                "workflow_file": "datasets-e2e.yml", "line": 5, "affected_jobs": ["e2e"],
                "wall_clock_p50_s": 0.0, "runner_min_saving": 0.0,
                "evidence": "datasets-e2e job runs the e2e suite serially"}
    driver = {"pattern": "OPT24", "title": "Long Test Job Without Sharding", "id": "7",
              "workflow_file": "framework-test.yml", "line": 9, "affected_jobs": ["framework"],
              "wall_clock_p50_s": 189.4, "runner_min_saving": 0.0,
              "evidence": "framework-test: full suite runs ~3m09s serially"}
    # off_path is FIRST (so the old `next(... with evidence)` would pick it), driver is the
    # max-wall-clock member that sets the displayed `~3m 09s` magnitude.
    body = "\n".join(bp._also_noticed_block([off_path, driver], "http://cat")[0])
    assert "~3m 09s wall-clock" in body  # magnitude is the driver's 189.4s
    # The Evidence line beside that 3m09s magnitude must describe the SAME (driver) job,
    # not the off-path 0s member that merely happened to be listed first.
    ev_line = next(ln for ln in body.splitlines() if ln.startswith("**Evidence:**"))
    assert "framework-test" in ev_line
    assert "datasets-e2e" not in ev_line
    # The embedded agent prompt's "What ci-speedup saw" must reconcile with its own
    # "~3m 09s" saving line too (it shares `_hygiene_prompt`).
    saw = next(ln for ln in body.splitlines() if ln.startswith("What ci-speedup saw:"))
    assert "framework-test" in saw and "datasets-e2e" not in saw


def test_also_noticed_wall_clock_banner_does_not_prescribe_sharding_for_non_opt24():
    # Regression (lancedb/lancedb): the on-critical-path wall-clock banner (and its agent
    # prompt) hardcoded OPT24's sharding remedy — "its fix (sharding / parallelizing the
    # suite) cuts developer wall-clock" — for EVERY credited finding. That is wrong for a
    # credited non-OPT24 pattern (OPT28 'Full Git History Checkout' → reduce fetch-depth;
    # a credited on-spine OPT73 → extract/cache the shared step), AND it violates the
    # skill's "does NOT prescribe the fix" invariant. The banner must assert the
    # on-critical-path / wall-clock fact WITHOUT naming a specific remedy. (Magnitudes are
    # kept above `_WALL_CLOCK_LONG_POLE_FLOOR_S` so the saves_wc reframe fires.)
    for pat, title, extra in [
        ("OPT28", "Full Git History Checkout", {"runner_min_saving": 0.0}),
        ("OPT73", "Shared step recurs across the cluster",
         {"structural": True, "runner_min_saving": 984.0}),
    ]:
        f = {"pattern": pat, "title": title, "id": "f1", "workflow_file": "ci.yml",
             "line": 5, "affected_jobs": ["pydantic1x"], "wall_clock_p50_s": 60.0, **extra}
        body = "\n".join(bp._also_noticed_block([f], "http://cat")[0]).lower()
        # The honest on-critical-path / wall-clock framing is still present.
        assert "merge-gating critical path" in body
        assert "wall-clock" in body
        # ...but it must NOT prescribe sharding / parallelizing — that is OPT24's remedy,
        # not this pattern's, and prescribing any fix breaks the no-prescription invariant.
        assert "sharding" not in body, f"{pat} banner/prompt prescribes sharding"
        assert "parallelizing" not in body, f"{pat} banner/prompt prescribes parallelizing"


def test_also_noticed_flags_shallow_sampled_figures():
    # When adaptive sampling left off-path workflows shallow, the hygiene appendix must
    # carry a visible "approximate — re-run at full depth" flag (chosen design: keep the
    # speedup, flag the rough findings).
    findings = [{"pattern": "OPT33", "title": "No Draft PR Gating", "id": "f1",
                 "workflow_file": "x.yml", "runner_min_saving": 100}]
    lines, n, _ = bp._also_noticed_block(findings, "http://cat",
                                      shallow_note="Approximate: shallow 10-run sample; "
                                      "re-run with `--shallow-runs 20` to confirm.")
    body = "\n".join(lines)
    assert n >= 1
    assert "⚠️" in body and "Approximate" in body and "--shallow-runs 20" in body
    # No note passed → no flag (a full-depth run reads clean).
    clean, _, _ = bp._also_noticed_block(findings, "http://cat")
    assert "Approximate" not in "\n".join(clean)


def test_queue_wait_block_renders_opt43_as_wall_clock_not_bill():
    # OPT43 (pre-start wait) gets its OWN section framed as developer WALL-CLOCK wait,
    # ranked by savable wait — NOT the runner-minute hygiene appendix (which would tell the
    # reader it removes ~0 wall-clock). And it's excluded from _also_noticed_block.
    opt43 = {"pattern": "OPT43", "title": "Excessive Queue Time", "id": "f1",
             "workflow_file": "x.yml", "job": "e2e", "wall_clock_p50_s": 130.0,
             "runner_min_saving": None}
    hygiene = {"pattern": "OPT33", "title": "No Draft PR Gating", "id": "f2",
               "workflow_file": "y.yml", "runner_min_saving": 100}
    qlines = bp._queue_wait_block([opt43, hygiene], "http://cat")
    body = "\n".join(qlines)
    assert "Pre-start wait (queue time)" in body
    assert "OPT43" in body and "wait-to-start" in body
    assert "worst savable wait 2m 10s" in body   # _clock(130) ranked headline
    assert "OPT33" not in body                # only wait-family patterns
    # The embedded agent prompt must match the section, not contradict it: developer
    # WALL-CLOCK wait, NOT the off-path "~0 developer wall-clock" bill framing.
    assert "developer WALL-CLOCK wait before the job starts" in body
    assert "~0 developer wall-clock" not in body
    assert "cloud-bill cut" not in body
    # OPT43 must NOT appear in the runner-minute appendix.
    also, _, _ = bp._also_noticed_block([opt43, hygiene], "http://cat")
    assert "OPT43" not in "\n".join(also) and "OPT33" in "\n".join(also)
    # No wait finding → empty section.
    assert bp._queue_wait_block([hygiene], "http://cat") == []


def test_queue_wait_block_ranks_by_savable_groups_and_truncates():
    # Multiple OPT43 findings: ranked savable-desc (worst leads the headline), the occurrence
    # string counts distinct workflow files, and >8 occurrences truncate with "+N more".
    findings = [{"pattern": "OPT43", "title": "Excessive Queue Time", "id": f"f{i}",
                 "workflow_file": f"wf{i % 2}.yml", "line": i, "job": f"j{i}",
                 "wall_clock_p50_s": float(i * 10)}
                for i in range(1, 11)]              # i=10 (100s) is the worst
    body = "\n".join(bp._queue_wait_block(findings, "http://cat"))
    assert "worst savable wait 1m 40s" in body     # _clock(100) leads, not a smaller one
    assert "10 across 2 wf" in body                # 10 occurrences over wf0.yml + wf1.yml
    assert "+2 more" in body                       # only the first 8 locations listed
    # The advisory exclusion: an advisory OPT43 never renders a section.
    adv = [{"pattern": "OPT43", "title": "Excessive Queue Time", "id": "a1",
            "workflow_file": "x.yml", "advisory": True, "wall_clock_p50_s": 200.0}]
    assert bp._queue_wait_block(adv, "http://cat") == []


def test_is_wait_finding_excludes_zero_savable_wait():
    # The dogfood overstatement: 107 OPT43 findings emitted but only the few with a positive
    # floor-capped savable wait belong in the section/TOC count. A zero-savable queue finding
    # (a Slack-notify / scheduled job no one merge-waits on) is excluded from both.
    real = {"pattern": "OPT43", "wall_clock_p50_s": 64.0}
    zero = {"pattern": "OPT43", "wall_clock_p50_s": 0.0}
    assert bp._is_wait_finding(real) is True
    assert bp._is_wait_finding(zero) is False
    body = "\n".join(bp._queue_wait_block([real, zero, zero, zero], "http://cat"))
    assert "1 occurrence" in body or "1 across" in body   # only the 1 real one counted
    # ...and the 3 dropped zero-savable findings are DISCLOSED (no silent drop), like the
    # hygiene appendix's "+N more ... kept in the findings JSON".
    assert "3 more queue findings have no addressable wait" in body
    assert "kept in the findings JSON" in body
    # No disclosure line when nothing was dropped.
    assert "no addressable wait" not in "\n".join(bp._queue_wait_block([real], "http://cat"))


def test_queue_wait_block_surfaces_shallow_note():
    # A shallow-sample run flags the queue figures as approximate (its own code path,
    # separate from the also-noticed appendix's copy of the warning).
    opt43 = {"pattern": "OPT43", "title": "Excessive Queue Time", "id": "f1",
             "workflow_file": "x.yml", "job": "e2e", "wall_clock_p50_s": 130.0}
    body = "\n".join(bp._queue_wait_block([opt43], "http://cat",
                                          shallow_note="Approximate: shallow 10-run "
                                          "sample; re-run to confirm."))
    assert "⚠️" in body and "Approximate: shallow 10-run sample" in body
    # No note → no warning line.
    assert "⚠️" not in "\n".join(bp._queue_wait_block([opt43], "http://cat"))


def test_toc_block_renders_pre_start_wait_pointer_and_pluralizes():
    # The TOC gains a "⏳ Pre-start wait" pointer driven by queue_count, with grammatical
    # subject/verb agreement: "1 job waits" vs "2 jobs wait".
    pole_wfs = [{"check": "tests", "p50_s": 100.0,
                 "workflow_file": ".github/workflows/ci.yml"}]
    wf_gate = {".github/workflows/ci.yml": 5}
    one = "\n".join(bp._toc_block(pole_wfs, wf_gate, 20, queue_count=1))
    assert "**⏳ Pre-start wait** — 1 job waits in queue" in one
    assert "[see below](#pre-start-wait)" in one
    two = "\n".join(bp._toc_block(pole_wfs, wf_gate, 20, queue_count=2))
    assert "**⏳ Pre-start wait** — 2 jobs wait in queue" in two
    # queue_count=0 → no pointer at all.
    zero = "\n".join(bp._toc_block(pole_wfs, wf_gate, 20, queue_count=0))
    assert "Pre-start wait" not in zero


def test_render_wires_queue_section_above_also_noticed():
    # End-to-end through render(): an OPT43 finding produces the section, the TOC pointer,
    # the `---` separator, and sits ABOVE the "Also noticed" appendix. Guards the two
    # independent copies of the queue predicate (render's queue_count vs the block's own
    # filter) from drifting into a dead #pre-start-wait anchor.
    doc = _doc_with_findings()
    doc["findings"].append(
        {"id": "q1", "pattern": "OPT43", "title": "Excessive Queue Time",
         "severity": "MEDIUM", "runner_min_saving": None, "wall_clock_p50_s": 130.0,
         "workflow_file": ".github/workflows/ci.yml", "line": 40, "job": "e2e"})
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "## ⏳ Pre-start wait (queue time)" in md
    assert '<a id="pre-start-wait"></a>' in md
    # render() flattens em-dashes to ASCII hyphens at the boundary (_strip_emdashes).
    assert "**⏳ Pre-start wait** - 1 job waits in queue" in md   # TOC pointer rendered
    # The section sits above the runner-minute appendix (and the anchor it points at
    # actually exists in the body — no dead link).
    assert md.index("pre-start-wait") < md.index("## 🧹 Also noticed")
    assert md.index("## ⏳ Pre-start wait (queue time)") < md.index("## 🧹 Also noticed")


def _doc_rare_and_typical_poles() -> dict:
    # Two FILE poles: a slow, rarely-run benchmark (present on 1/20 PRs — label-gated/opt-in)
    # and a typical test gate (present on a majority). populations drive presence/npop.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["poles"] = [
        {"check": "Run Benchmark Jobs", "p50_s": 9000.0,
         "workflow_file": ".github/workflows/benchmark.yml", "job": "Run Benchmark Jobs",
         "dominant_step": "bench", "dominant_p50_s": 8000.0,
         "steps": [{"step": "bench", "category": "test", "p50_s": 8000.0}]},
        {"check": "Test suite", "p50_s": 1400.0,
         "workflow_file": ".github/workflows/test.yml", "job": "Test suite",
         "dominant_step": "run tests", "dominant_p50_s": 900.0,
         "steps": [{"step": "run tests", "category": "test", "p50_s": 900.0}]},
    ]
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0},
                    {"name": "Test suite", "p50_s": 1400.0},
                    {"name": "lint", "p50_s": 200.0}]
    cp["populations"] = (
        [[0.05, [["Run Benchmark Jobs", 9000.0], ["Test suite", 1400.0]]]] * 1 +
        [[0.05, [["Test suite", 1400.0]]]] * 14 +
        [[0.05, [["lint", 200.0]]]] * 5)
    return doc


def test_rare_slow_file_pole_does_not_headline_and_is_labeled_opt_in():
    # A label-gated/opt-in benchmark that ran on only 1/20 PRs must NOT headline "why is the
    # merge slow?" just because it's the slowest job. The typical gate (majority-present)
    # headlines; the rare giant is demoted and labeled opt-in / conditional with its presence
    # count — and is NEVER mislabeled as an external review check (it's a fixable workflow).
    doc = _doc_rare_and_typical_poles()
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head   # typical gate headlines
    assert "`Run Benchmark Jobs`" in head and "ran on only 1/20 sampled PRs" in head
    assert "opt-in / conditional" in head
    assert "external review check" not in md          # the bug: a rare FILE pole is NOT external
    # Demoted, but still surfaced as a drilled pole carrying the opt-in label (not the gate).
    assert "Long pole 1: `test.yml` ▸ `Test suite`" in md
    assert "Opt-in / rare - ran on only 1/20 sampled PRs" in md   # typographic dash normalized to '-'


def test_required_scoped_minority_pole_reads_as_required_path_conditional():
    # trigger.dev regression: when the spine was REQUIRED-SCOPED (spine_required_scoped True —
    # the data layer ACTUALLY narrowed it to required-reachable checks), a demoted minority
    # FILE pole is a *required* path-conditional gate — NOT an opt-in benchmark. It must be
    # reframed "required · path-conditional" (it gates the PRs that run it), never "opt-in /
    # throughput, not merge-wait" (which would tell the user a required gate is skippable). All
    # THREE label sites (headline, "Also slower" note, per-pole role) must be reframed.
    doc = _doc_rare_and_typical_poles()
    doc["pr_critical_path"]["spine_required_scoped"] = True
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head   # typical gate still headlines
    # Site 1 (headline): the demoted required leg is reframed, not called opt-in/throughput.
    assert "is a *required* gate" in head and "path-conditional" in head
    # Site 2 ("Also slower" note): reframed to *required* path-conditional, with the
    # required-gate tail — not the opt-in/throughput note.
    note = next(l for l in md.split("\n") if "Also slower on **some**" in l)
    assert "*required* check(s) that ran on a minority of sampled PRs" in note
    assert "gate the merge on the PRs that run them" in note
    assert "opt-in / conditional workflow check(s)" not in note
    assert "treat it as throughput/cost (an opt-in job)" not in note
    # Site 3 (per-pole role): reframed.
    assert "Required · path-conditional - ran on 1/20 sampled PRs" in md       # role label reframed
    assert "throughput/cost, not merge-wait" not in md                         # the bug framing is gone
    assert "opt-in / conditional (e.g. label-gated)" not in md


def test_sampling_scoped_but_spine_unscoped_keeps_opt_in_framing():
    # Correctness guard for the flag fix: `required_suite_scoped` (the SAMPLING flag) can be
    # True on a partial / anchorless required read where `_scope_spine_to_required` stayed
    # inert and the spine still holds NON-required checks. The relabel must key off
    # `spine_required_scoped` (the narrowing-fired flag), NOT `required_suite_scoped` — else a
    # genuinely non-required opt-in benchmark gets mislabeled a "required gate", the exact
    # inversion of the bug this guards. With the sampling flag set but the spine flag unset,
    # the demoted minority pole keeps the opt-in / throughput framing.
    doc = _doc_rare_and_typical_poles()
    doc["pr_critical_path"]["required_suite_scoped"] = True
    doc["pr_critical_path"]["spine_required_scoped"] = False
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    assert "Opt-in / rare - ran on only 1/20 sampled PRs" in md   # opt-in framing kept
    assert "is a *required* gate" not in md                       # NOT relabeled required
    assert "Required · path-conditional" not in md


def test_unobservable_required_check_is_disclosed_in_report():
    # No-silent-drop: a required check excluded from the suite test as status-only/external
    # (`required_checks_unobservable`) must be NAMED in the rendered report, not just the JSON
    # — the gate was measured on the observable subset, so the excluded check is unmeasurable,
    # never silently treated as satisfied.
    doc = _doc_one_pole()
    doc["pr_critical_path"]["required_checks_unobservable"] = ["Devin Review"]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1"}, "2026-06-08")
    assert "Status-only required check(s) excluded from the suite test:" in md
    assert "`Devin Review`" in md
    assert "observable" in md


def test_external_fileless_minority_check_still_reads_as_external():
    # No-regression: a genuinely external/managed check (an AI review bot with NO workflow
    # file) that's slow but ran on a minority of PRs keeps the "external review check" framing
    # — the file-vs-external split must not turn every minority check into "opt-in".
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["poles"] = [{
        "check": "Test suite", "p50_s": 1400.0,
        "workflow_file": ".github/workflows/test.yml", "job": "Test suite",
        "dominant_step": "run tests", "dominant_p50_s": 900.0,
        "steps": [{"step": "run tests", "category": "test", "p50_s": 900.0}]}]
    cp["checks"] = [{"name": "Claude Code Review", "p50_s": 5000.0},  # fileless, no pole
                    {"name": "Test suite", "p50_s": 1400.0}]
    cp["populations"] = (
        [[0.05, [["Claude Code Review", 5000.0], ["Test suite", 1400.0]]]] * 2 +
        [[0.05, [["Test suite", 1400.0]]]] * 18)
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head
    assert "`Claude Code Review`" in head and "external review check" in head   # kept
    assert "opt-in / conditional" not in head

def _doc_two_typical_poles_one_rare_gate() -> dict:
    # flwrlabs/flower regression: TWO file poles BOTH present on every sampled PR, so both
    # pass the presence-based typical/rare split. But one (`Python 3.11`) is slower by global
    # p50 (743s) while only being the ACTUAL per-PR gate on 3/20 PRs; the other (`Python 3.13`)
    # is the real gate on 17/20. The headline must crown the actual gate (frequency), not the
    # slowest-but-rarely-gating check, even though both are "typical" by presence.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["poles"] = [
        {"check": "Python 3.11", "p50_s": 743.0,
         "workflow_file": ".github/workflows/datasets-test.yml", "job": "Python 3.11",
         "dominant_step": "run tests", "dominant_p50_s": 600.0,
         "steps": [{"step": "run tests", "category": "test", "p50_s": 600.0}]},
        {"check": "Python 3.13", "p50_s": 620.0,
         "workflow_file": ".github/workflows/framework-test.yml", "job": "Python 3.13",
         "dominant_step": "run tests", "dominant_p50_s": 500.0,
         "steps": [{"step": "run tests", "category": "test", "p50_s": 500.0}]},
    ]
    cp["checks"] = [{"name": "Python 3.11", "p50_s": 743.0},
                    {"name": "Python 3.13", "p50_s": 620.0}]
    # 20 per-PR populations: both checks present on every PR (so both are "typical" by
    # presence), but `Python 3.11` is the slowest (the pole) on only 3 and `Python 3.13` on 17.
    cp["populations"] = (
        [[0.05, [["Python 3.11", 743.0], ["Python 3.13", 500.0]]]] * 3 +
        [[0.05, [["Python 3.11", 600.0], ["Python 3.13", 700.0]]]] * 17)
    return doc


def test_headline_crowns_actual_gate_not_slowest_when_both_typical():
    # The slowest typical pole (`Python 3.11`, 743s) gates only 3/20 PRs; the actual gate is
    # `Python 3.13` (17/20). With both present on every PR the old presence-only split kept
    # both typical and crowned `Python 3.11` by p50. The fix orders typical poles by how often
    # they ACTUALLY gate, so the headline + Long pole 1 name the real gate, and the slowest
    # check is reported separately (never crowned as the gate).
    doc = _doc_two_typical_poles_one_rare_gate()
    md = bp.render(
        doc, {"datasets-test": _IMPORT_BOUND_LOG, "framework-test": _IMPORT_BOUND_LOG}, {},
        {"datasets-test": "https://github.com/o/r/actions/runs/1",
         "framework-test": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    # The real gate (most PRs gate on it) is named as the gate; the slowest check is reported
    # truthfully as the floor, NOT crowned as "the slowest check a typical PR waits on".
    assert "`Python 3.13` is the check" in head and "most PRs gate on" in head
    assert "`Python 3.11`" not in head or "is the slowest check a typical PR waits on" not in head
    # Long pole 1 is the actual gate, not the slowest-but-rare pole.
    assert "Long pole 1: `framework-test.yml` ▸ `Python 3.13`" in md
    # The actual gate must NOT be mislabeled "the slowest check a typical PR waits on".
    gate_role_idx = md.find("Long pole 1: `framework-test.yml` ▸ `Python 3.13`")
    next_pole_idx = md.find("Long pole 2", gate_role_idx)
    gate_section = md[gate_role_idx:next_pole_idx if next_pole_idx != -1 else len(md)]
    assert "The slowest check a typical PR waits on" not in gate_section


def test_provenance_discloses_triaged_fast_workflows():
    # Run-list triage must be visible, never silent: the provenance block names the fast
    # workflows whose job fetch was skipped and flags their hygiene as run-list-only.
    doc = _prov_doc()
    doc["data_sources"].update({
        "gh_query_count": 150, "shallow_runs": 10, "max_runs": 20, "shallow_capped": True,
        "deepened_workflows": 3, "pole_candidates": 5, "deepen_converged": True,
        "triaged_fast_workflows": [".github/workflows/lint.yml",
                                    ".github/workflows/check-typo.yml"]})
    block = "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))
    assert "Fast workflows triaged (no job fetch):" in block
    assert "2 workflow(s)" in block
    assert "`lint.yml`" in block and "`check-typo.yml`" in block
    # No triaged workflows -> no line.
    doc["data_sources"]["triaged_fast_workflows"] = []
    assert "Fast workflows triaged" not in "\n".join(bp._provenance_block(doc, "o/r", "2026-06-08"))


def test_renderer_demotes_via_present_on_fallback_when_no_populations():
    # The common non-bimodal case: M2 emitted no `populations`, so the renderer must fall
    # back to the data layer's per-check `present_on` / `check_present_n_pr` to still demote a
    # rare pole. (Every other rare-pole test sets `populations`; this pins the fallback path.)
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 20
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0, "present_on": 1},
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 15},
                    {"name": "lint", "p50_s": 200.0, "present_on": 18}]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head   # fallback worked
    assert "ran on only 1/20 sampled PRs" in head
    assert "external review check" not in md


def test_renderer_no_demotion_when_presence_unknown_zero_denominator():
    # check_present_n_pr == 0 means presence is UNKNOWN (not "absent") — the renderer must
    # NOT demote every pole to rare. Guards the 0-denominator fallback regression.
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 0
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0, "present_on": 0},
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 0}]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    assert "Opt-in / rare" not in md          # nothing demoted when presence is unknown


def test_renderer_no_rare_label_on_small_sample_fallback():
    # Regression for the toolkit-review finding: with the present_on fallback AND a sample
    # below _RARE_PRESENCE_MIN_PR, the renderer must NOT demote OR label a check rare — the
    # typical/minority label split is guarded by the same threshold as the pole ordering, so
    # the prose can't contradict the (un-demoted) ranking on a tiny sample.
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 4          # < 6 -> inert
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0, "present_on": 1},
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 3}]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    assert "Opt-in / rare" not in md            # no role-label demotion on a tiny sample
    assert "opt-in / conditional" not in md     # no minority-footnote label either


def test_minority_file_check_below_poles_reads_as_opt_in_not_external():
    # greptile P2: a rare FILE check that never ranked as a drilled pole (e.g. a slow upload
    # job) must read as "opt-in / conditional", not "external review check" — recognized via
    # its own emitted `workflow_file`, independent of whether it was drilled.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["poles"] = [{"check": "Test suite", "p50_s": 1400.0,
                    "workflow_file": ".github/workflows/test.yml", "job": "Test suite",
                    "dominant_step": "run tests", "dominant_p50_s": 900.0,
                    "steps": [{"step": "run tests", "category": "test", "p50_s": 900.0}]}]
    cp["check_present_n_pr"] = 20
    cp["checks"] = [
        {"name": "Upload artifacts", "p50_s": 5000.0, "present_on": 1,
         "workflow_file": ".github/workflows/release.yml"},   # file-backed, NOT a drilled pole
        {"name": "Test suite", "p50_s": 1400.0, "present_on": 15,
         "workflow_file": ".github/workflows/test.yml"},
        {"name": "lint", "p50_s": 200.0, "present_on": 18,
         "workflow_file": ".github/workflows/lint.yml"}]
    cp["populations"] = ([[0.05, [["Upload artifacts", 5000.0], ["Test suite", 1400.0]]]]
                         + [[0.05, [["Test suite", 1400.0]]]] * 14
                         + [[0.05, [["lint", 200.0]]]] * 5)
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head
    assert "`Upload artifacts`" in head and "opt-in / conditional" in head
    assert "external review check" not in md       # the bug: a file-backed minority is NOT external


def test_renderer_required_check_on_minority_is_not_demoted():
    # The renderer's required-check exemption (`name in _required_names` in `_typical_check`)
    # mirrors the data layer's: a *required* check present on a MINORITY still gates and must
    # NOT be labeled "Opt-in / rare" or demoted below the typical gate — else the report would
    # contradict `critical_path_check`, which keeps a required check as the headline.
    doc = _doc_rare_and_typical_poles()
    doc["required_checks"] = ["Run Benchmark Jobs"]   # slow + minority (1/20) BUT required
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    # Exempt -> stays the headline (slowest required gate), never demoted/relabeled rare.
    assert "`Run Benchmark Jobs` is the slowest check a typical PR waits on" in head
    assert "Opt-in / rare" not in md
    assert "opt-in / conditional" not in head


def test_renderer_active_demotion_at_min_pr_boundary_fallback():
    # Renderer analog of the data-layer n==6 boundary test: with the `present_on` fallback and
    # check_present_n_pr == _RARE_PRESENCE_MIN_PR (6, the first ACTIVE size), the renderer MUST
    # demote the rare giant. Pins the renderer's `npop >= _RARE_PRESENCE_MIN_PR` against a flip
    # to `>` that would leave demotion silently off at exactly 6 PRs.
    assert bp._RARE_PRESENCE_MIN_PR == 6
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 6
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0, "present_on": 1},
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 5}]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head   # demoted at n=6
    assert "ran on only 1/6 sampled PRs" in head


def test_renderer_demotes_at_exactly_half_presence_fallback():
    # The renderer carries its OWN duplicated `present > npop*_RARE_PRESENCE_FRAC` predicate
    # (`_typical_check`). At EXACTLY 50% presence the check is rare (`>` is False, mirroring
    # the data layer's `<= 0.5`), so it demotes below a >50% typical check. Pins the renderer
    # operator so a flip to `>=` (which would keep the giant headlining and contradict the
    # data layer's `critical_path_check`) fails CI — the data-layer analog is
    # `test_rank_spine_present_first_boundary_exactly_half_is_rare`.
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 10
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0, "present_on": 5},  # 5/10 = 50%
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 8}]          # 8/10 > 50%
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    assert "`Test suite` is the slowest check a typical PR waits on" in head   # demoted at 50%
    assert "ran on only 5/10 sampled PRs" in head


def test_renderer_partial_present_on_does_not_demote():
    # If ANY check in the fallback lacks `present_on`, the presence map is incomplete and
    # unreliable — the renderer must treat it as UNKNOWN and demote nothing (same stance as the
    # 0-denominator guard), rather than reading the missing key as present_on==0 and silently
    # marking that check rare.
    doc = _doc_rare_and_typical_poles()
    cp = doc["pr_critical_path"]
    cp["populations"] = []
    cp["populations_n"] = 0
    cp["check_present_n_pr"] = 20
    cp["checks"] = [{"name": "Run Benchmark Jobs", "p50_s": 9000.0},   # MISSING present_on
                    {"name": "Test suite", "p50_s": 1400.0, "present_on": 15}]
    md = bp.render(doc, {"test": _IMPORT_BOUND_LOG, "benchmark": _IMPORT_BOUND_LOG}, {},
                   {"test": "https://github.com/o/r/actions/runs/1",
                    "benchmark": "https://github.com/o/r/actions/runs/2"}, "2026-06-08")
    assert "Opt-in / rare" not in md            # incomplete presence -> nothing demoted
    assert "opt-in / conditional" not in md


# --------------------------------------------------------------------------- #
# _mag_line — degenerate cross-run sample must not read as "stable/validated"
# --------------------------------------------------------------------------- #

def test_mag_line_flags_degenerate_sample_not_stable():
    # Defensive guard for the duplicate-named-step bug: if every sampled value
    # collapsed to 0 while the drilled value is large, the renderer must NOT claim the
    # magnitude is "stable across runs" — it must flag it as NOT cross-run validated.
    mag = {
        "label": "the `Run tests on android emulator` step (wall)",
        "unit": "s", "kind": "step-wall", "this_run": 358.0, "escalated": False,
        "values": [
            {"run_url": "https://example/runs/1", "value": 0.0, "drilled": True},
            {"run_url": "https://example/runs/2", "value": 0.0, "drilled": False},
            {"run_url": "https://example/runs/3", "value": 0.0, "drilled": False},
        ],
    }
    out = "\n".join(bp._mag_line(mag, None))
    assert "stable across runs" not in out
    assert "validated" in out and "NOT cross-run validated" in out


def test_mag_line_healthy_step_wall_still_reads_stable():
    # The consistent post-fix shape (values track this_run) keeps its normal verdict.
    mag = {
        "label": "the `Gradle :test` step (wall)",
        "unit": "s", "kind": "step-wall", "this_run": 1000.0, "escalated": False,
        "values": [
            {"run_url": "https://example/runs/1", "value": 1000.0, "drilled": True},
            {"run_url": "https://example/runs/2", "value": 1010.0, "drilled": False},
            {"run_url": "https://example/runs/3", "value": 990.0, "drilled": False},
        ],
    }
    out = "\n".join(bp._mag_line(mag, None))
    assert "stable across runs" in out
    assert "NOT cross-run validated" not in out


def test_mag_line_annotates_fork_run_in_the_common_case_loop():
    # PR #126 fresh-review (Angle A): the fork disclosure (" — fork PR (repo cache unavailable)")
    # was added to only the degenerate "NOT cross-run validated" per-run loop; the common-case
    # (n>=5) loop emitted fork runs unannotated, so a fork PR's cold-cache miss read as an ordinary
    # upstream point in the range. Both loops must annotate the fork run.
    vals = [{"run_url": f"https://x/runs/{i}", "value": 12.0} for i in range(1, 5)]
    vals.append({"run_url": "https://x/runs/9", "value": 95.0, "fork": True})  # cold fork run
    mag = {"unit": "%", "label": "packages rebuilt", "this_run": 12.0, "values": vals}
    out = "\n".join(bp._mag_line(mag, {"fix_key": "turbo-partial-cache"}))
    assert "[run 9](https://x/runs/9) — 95% — fork PR (repo cache unavailable)" in out


def test_headline_floor_is_slowest_check_not_the_frequency_gate():
    # langfuse class: the most-GATING check (`tests-web`, 255s — what most PRs wait on, and
    # the only drilled pole) is NOT the slowest concurrent check (`heavy-bot`, 900s, a spine
    # check that isn't a drilled pole). "X until all checks finish" must be the FLOOR (900s),
    # and the headline must NOT call the frequency-gate "the slowest" when a slower check exists.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["checks"] = [{"name": "tests-web", "p50_s": 255.0},
                    {"name": "heavy-bot", "p50_s": 900.0},
                    {"name": "lint", "p50_s": 100.0}]
    # 20 populations, all three present → both gate-candidates are "typical"; heavy-bot is the
    # slowest (sets the floor) but is not a pole, so the frequency gate stays tests-web.
    cp["populations"] = [[0.05, [["heavy-bot", 900.0], ["tests-web", 255.0],
                                 ["lint", 100.0]]]] * 20
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/1"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    floor, gate_dur = bp._clock(900.0), bp._clock(255.0)
    # the floor (slowest) is the headline number AND named the slowest; the gate is named apart
    assert floor in head
    assert "the slowest check a typical PR waits on is `heavy-bot`" in head
    assert "`tests-web` is the check most PRs gate on" in head
    # the OLD bug: the frequency gate must NOT be called the slowest, and its 255s must not be
    # presented as the "until all checks finish" wall-clock floor
    assert "`tests-web` is the slowest check" not in head
    assert gate_dur not in head
    # the Bottom line waits the FLOOR (900s), not the gate's 255s
    bl = next(l for l in md.split("\n") if "Bottom line" in l)
    assert floor in bl and gate_dur not in bl
    # The Level-1 ASCII chart was removed (owner UX edit 2026-07-19); its "top bar = the
    # gate" signal now lives on the Contents' first critical-path row as a " (the gate)"
    # tag, emitted ONLY when the frequency gate IS the slowest single check. Here gate ≠
    # slowest, so the first Contents row must carry NO "(the gate)" tag (the same
    # invariant the old chart caption "Top bar = the slowest:" verified).
    toc_rows = [l for l in md.split("\n") if re.match(r"^\d+\. ", l)]
    assert toc_rows, "no Contents critical-path rows rendered"
    assert "(the gate)" not in toc_rows[0], toc_rows[0]


def test_headline_floor_excludes_partial_presence_slowest_check():
    # OneSignal/OneSignal-Android-SDK class: the slowest TYPICAL check (`Claude Code Review`,
    # a managed app check at 944.5s) ran on only 12/20 PRs; `build` (619.5s) ran on 20/20.
    # The "X until all checks finish" headline is the wait a TYPICAL (median) PR sees, so a
    # check present on only 12/20 PRs must NOT set that universal floor at its full conditional
    # p50 — the 8 PRs that don't run it finish sooner. The faithful floor is the population-
    # weighted typical: the median of the per-PR critical-path maxima (~750s = 12m 30s), NOT
    # Claude's 944.5s (15m 44s). The slowest check's OWN time is still named truthfully.
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["provenance"] = "unresolved"
    # `build` is the file-backed pole; `Claude Code Review` is a managed/fileless check.
    cp["poles"] = [{
        "check": "build", "p50_s": 619.5,
        "workflow_file": ".github/workflows/build.yml", "job": "build",
        "dominant_step": "compile", "dominant_p50_s": 300.0,
        "steps": [{"step": "compile", "category": "build", "p50_s": 300.0}],
    }]
    cp["checks"] = [{"name": "Claude Code Review", "p50_s": 944.5, "present_on": 12},
                    {"name": "build", "p50_s": 619.5, "present_on": 20,
                     "workflow_file": ".github/workflows/build.yml"}]
    # 20 per-PR populations: 8 run `build` only (max 619.5); 12 also run Claude (its per-PR
    # max). Median of the 20 per-PR maxima = 750.0 (12m 30s), well below Claude's 944.5 p50.
    claude_maxima = [700.0, 750.0, 750.0, 800.0, 850.0, 900.0,
                     944.5, 944.5, 944.5, 1000.0, 1050.0, 1100.0]
    cp["populations"] = ([[0.05, [["build", 619.5]]]] * 8
                         + [[0.05, [["Claude Code Review", x], ["build", 619.5]]]
                            for x in claude_maxima])
    md = bp.render(doc, {"build": _IMPORT_BOUND_LOG}, {},
                   {"build": "https://github.com/o/r/actions/runs/1"}, "2026-06-08")
    head = next(l for l in md.split("\n") if "until all checks finish" in l)
    bottom = next(l for l in md.split("\n") if "Bottom line" in l)
    # The faithful population-weighted floor (12m 30s) headlines, NOT Claude's 944.5 p50.
    assert head.lstrip("> ").startswith(f"**{bp._clock(750.0)} until all checks finish**")
    assert f"waits **{bp._clock(750.0)}** for all checks to finish" in bottom
    # The bug: Claude's conditional 944.5 (15m 44s) must NOT be the "all checks finish" floor.
    assert f"**{bp._clock(944.5)} until all checks finish**" not in md
    assert f"waits **{bp._clock(944.5)}** for all checks" not in md
    # …but Claude's OWN slowest-check time is still named truthfully (the faithful split).
    assert "Claude Code Review" in head and f"~{bp._clock(944.5)}" in head
    # …AND the form-2 headline RECONCILES the two: a non-universal slowest check (12/20) must not be
    # labeled "a typical PR waits on" beside a strictly-lower 12m 30s floor without the presence
    # caveat (the tauri `test (windows-latest)` bug — Form 2 omitted the disclosure Form 1 carries).
    assert f"ran on only 12/20 sampled PRs, so a typical PR finishes in {bp._clock(750.0)}" in head, \
        "form-2 floor-lowered headline must disclose the slowest check's presence (N/npop)"


def test_dominant_step_from_timeline_uses_category_aggregate_with_map():
    # With the pole's name→category map, the prompt crowns the dominant CATEGORY lead (a
    # `test` step), agreeing with the decomposition + cross-run check — not the lone big
    # `Build` step. Without the map it falls back to the single longest (backward-compatible).
    timeline = {"job_dur_s": 180.0, "steps": [
        {"name": "Build", "dur_s": 60.0}, {"name": "test a", "dur_s": 40.0},
        {"name": "test b", "dur_s": 40.0}, {"name": "test c", "dur_s": 40.0}]}
    cat = {"Build": "build", "test a": "test", "test b": "test", "test c": "test"}
    name, d, _share = bp._dominant_step_from_timeline(timeline, cat)
    assert name == "test a" and d == 40.0          # dominant-category lead, not Build(60)
    name2, _d2, _s2 = bp._dominant_step_from_timeline(timeline)   # no map → legacy behaviour
    assert name2 == "Build"


def test_dom_index_excludes_boilerplate_in_longest_fallback():
    # An AGGREGATE dominant label matches no single step name, so _dom_index falls
    # back to the longest step. It must skip setup/cleanup boilerplate (`Post`,
    # `checkout`) even when that boilerplate is the longest, crowning the dominant
    # WORK step — matching the producer's category-aware decomposition.
    steps = [
        {"name": "Set up job", "dur_s": 5.0},
        {"name": "checkout", "dur_s": 200.0},      # longest, but boilerplate
        {"name": "Build", "dur_s": 120.0},          # the real work step
        {"name": "Post Run actions/checkout", "dur_s": 30.0},
    ]
    assert bp._dom_index(steps, "Build + 2 more") == 2   # Build, not checkout


def test_dom_index_all_boilerplate_falls_back_to_full_set():
    # When EVERY step is boilerplate, the fallback uses the full set (longest wins)
    # rather than returning nothing.
    steps = [
        {"name": "Set up job", "dur_s": 5.0},
        {"name": "Post checkout", "dur_s": 40.0},   # longest boilerplate
        {"name": "Complete job", "dur_s": 2.0},
    ]
    assert bp._dom_index(steps, "no-such-step") == 1


def test_dom_index_exact_name_still_wins_over_longest():
    # An exact name match is unchanged by the boilerplate fix.
    steps = [
        {"name": "checkout", "dur_s": 200.0},
        {"name": "Build", "dur_s": 120.0},
    ]
    assert bp._dom_index(steps, "Build") == 1


def test_pole_addressable_caps_at_concurrent_sibling_not_optimistic_matrix():
    # Finding 2 (pre-commit): the headline "biggest single measured win" must floor at the
    # actual 2nd-slowest CONCURRENT check — a sibling matrix leg included — not the optimistic
    # next-NON-leg. A 175s concurrent sibling beside a 517s gate caps the win at 517-175=342,
    # not 517-90=427 (which over-promised past the visible 175s bar).
    pole = {"check": "main / windows-latest / py310", "p50_s": 517.0}
    candidates = [
        {"name": "main / windows-latest / py310", "p50_s": 517.0},
        {"name": "main / ubuntu-latest / py310", "p50_s": 175.0},   # concurrent sibling
        {"name": "lint", "p50_s": 90.0},                            # a non-leg, faster
    ]
    assert bp._pole_addressable(pole, candidates) == 517.0 - 175.0   # 342, not 427
    # non-gate guard (G6): a pole that runs BEHIND a slower concurrent check buys ~0
    pole2 = {"check": "fast", "p50_s": 100.0}
    cands2 = [{"name": "fast", "p50_s": 100.0}, {"name": "slow", "p50_s": 300.0}]
    assert bp._pole_addressable(pole2, cands2) == 0.0


# --- install-lifecycle-build detector + _cache_state_of_log (PR #126 review coverage) ----
def _install_lifecycle_log(build_time="3m20s", cmd="pnpm install --frozen-lockfile",
                           marker=True, cached="72 cached, 128 total"):
    lines = [f"##[group]Run {cmd}", cmd, "##[endgroup]", "Lockfile is up to date"]
    if marker:
        lines.append(". prepare$ turbo build")
    lines += [f"Cached:    {cached}", f"  Time:    {build_time}",
              "##[group]Run pnpm -w check", "done"]
    return "\n".join("2024Z " + l for l in lines)


def test_parse_log_install_lifecycle_build_fires_inside_install_section():
    leaf = bp._parse_log(_install_lifecycle_log())
    assert leaf is not None and leaf["fix_key"] == "install-lifecycle-build"
    assert leaf["magnitude"]["unit"] == "s" and leaf["magnitude"]["value"] == 200.0


def test_parse_log_install_lifecycle_respects_floor():
    # A sub-floor (<30s) in-install build is not a material gate -> falls through (D2 here).
    leaf = bp._parse_log(_install_lifecycle_log(build_time="12s"))
    assert leaf is None or leaf["fix_key"] != "install-lifecycle-build"


def test_parse_log_install_lifecycle_requires_a_lifecycle_marker():
    # A bare install whose section has a turbo summary but NO lifecycle marker is not this
    # mechanism (the marker is what proves a `prepare`/`postinstall` script ran the build).
    leaf = bp._parse_log(_install_lifecycle_log(marker=False))
    assert leaf is None or leaf["fix_key"] != "install-lifecycle-build"


def test_parse_log_install_lifecycle_rejects_explicit_chained_build():
    # `pnpm install && pnpm build` is an EXPLICIT second command, not a lifecycle script — the
    # `--ignore-scripts` remedy wouldn't remove it, so it must not be mislabeled lifecycle.
    leaf = bp._parse_log(_install_lifecycle_log(cmd="pnpm install --frozen-lockfile && pnpm build"))
    assert leaf is None or leaf["fix_key"] != "install-lifecycle-build"


def test_parse_log_install_lifecycle_ignore_scripts_not_matched():
    leaf = bp._parse_log(_install_lifecycle_log(cmd="pnpm install --ignore-scripts"))
    assert leaf is None or leaf["fix_key"] != "install-lifecycle-build"


def test_parse_log_turbo_in_its_own_step_still_d2_not_lifecycle():
    # THE D2-unchanged regression: a turbo build in its OWN (non-install) `##[group]Run` step
    # must still classify as turbo-partial-cache, not the new lifecycle leaf.
    log = "\n".join("2024Z " + l for l in [
        "##[group]Run pnpm install --frozen-lockfile", "##[endgroup]", "done",
        "##[group]Run pnpm turbo build", "pnpm turbo build", "##[endgroup]",
        "Cached:    72 cached, 128 total", "  Time:    3m20s", "##[endgroup]"])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "turbo-partial-cache"


def test_cache_state_of_log_turbo_percent_and_cold():
    st = bp._cache_state_of_log(_install_lifecycle_log(cached="72 cached, 128 total"),
                                "turbo-partial-cache")
    assert st and round(st["miss_pct"]) == 44 and st["cold"] is False
    cold = bp._cache_state_of_log("\n".join(["   • Remote caching disabled",
                                             "Cached:    0 cached, 100 total", "  Time: 5m"]),
                                  "turbo-remote-cache")
    assert cold and cold["cold"] is True and cold["remote_off"] is True


def test_cache_state_of_log_no_summary_needs_activity_burst():
    # A LONE stray "cache miss" line must NOT stamp a run 100% cold (mirrors detector D's
    # `activity > 5` floor) — otherwise one miss drags the cross-run median toward cold/churn.
    assert bp._cache_state_of_log("cache miss, executing x", "turbo-partial-cache") is None
    burst = "\n".join(f"cache miss, executing p{i}" for i in range(6))
    st = bp._cache_state_of_log(burst, "turbo-partial-cache")
    assert st and st["miss_pct"] == 100.0 and st["cold"] is True


def test_cache_state_of_log_buildx_and_unrecognized():
    bx = "\n".join(["#5 [build 2/3] RUN make", "#5 DONE 40.0s",
                    "#6 [build 3/3] RUN test", "#6 DONE 10.0s"])
    st = bp._cache_state_of_log(bx, "buildx-no-cache")
    assert st and st["cold"] is True and st["miss_pct"] == 100.0
    assert bp._cache_state_of_log("nothing here", "turbo-partial-cache") is None
    assert bp._cache_state_of_log("", "turbo-partial-cache") is None


def test_cache_state_of_log_lifecycle_reads_install_section_not_later_build():
    # Greptile P2 (PR #126): a job with an in-install lifecycle build (low miss) AND a later
    # explicit `turbo build` step (high miss). `_cache_state_of_log` for the lifecycle leaf must
    # read the INSTALL section's miss, not the later build's — else a false "churn" verdict would
    # tag the structural finding "BIGGEST LEVER". The detector and the state must agree.
    # The later explicit build is deliberately SLOWER (5m) than the in-install build (2m), so a
    # regression to a whole-log max-by-Time scan would pick the later 61%-miss block and the assert
    # below (~6%) would catch it. (A fixture where the install block is the slowest can't tell the
    # section-scoped fix from the bug — the weakness the PR-review flagged.)
    log = "\n".join("2024Z " + l for l in [
        "##[group]Run pnpm install --frozen-lockfile", "pnpm install --frozen-lockfile",
        "##[endgroup]", ". prepare$ turbo build",
        "Cached:    120 cached, 128 total",   # install section: 8 rebuilt = ~6% miss
        "  Time:    2m00s",
        "##[group]Run pnpm turbo build", "pnpm turbo build", "##[endgroup]",
        "Cached:    50 cached, 128 total",    # later explicit build: 78 rebuilt = ~61% miss
        "  Time:    5m00s", "##[endgroup]"])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "install-lifecycle-build"
    st = bp._cache_state_of_log(log, "install-lifecycle-build")
    # ~6% (the install section), NOT ~61% (the later build) — they measure the SAME section.
    assert st is not None and round(st["miss_pct"]) == 6


# --- PR #126 2nd-review: detector precision + render-side behavior ------------------
def test_parse_log_multiline_run_block_explicit_build_not_lifecycle():
    # F1 (2nd adversarial review): a `run: |` block that installs THEN explicitly builds echoes
    # both commands under one `##[group]Run` header. The explicit `pnpm build` must be recognized
    # (scanning the echoed command block, not just the header line) so it's NOT mislabeled a
    # lifecycle build — `--ignore-scripts` wouldn't remove an explicit command.
    log = "\n".join("2024Z " + l for l in [
        "##[group]Run pnpm install --frozen-lockfile", "pnpm install --frozen-lockfile",
        "pnpm build", "shell: /usr/bin/bash", "##[endgroup]",
        "esbuild@0.1 postinstall$ node install.js",   # a dependency postinstall marker (a decoy)
        "Cached:    40 cached, 128 total", "  Time:    1m30s"])
    leaf = bp._parse_log(log)
    assert leaf is None or leaf["fix_key"] != "install-lifecycle-build"


def test_cache_state_of_log_warm_run_below_fire_floor_still_measured():
    # F2 (2nd adversarial review): a WARM run's miss must be readable even when the leaf wouldn't
    # re-fire on it — otherwise warm runs drop out and the distribution can't reach mostly-warm.
    # turbo: `_cache_state_of_log` reads the miss regardless of the 40% fire floor.
    warm_turbo = "Cached:    120 cached, 128 total\n  Time:    1m00s"
    assert bp._parse_log(warm_turbo) is None                      # D2 doesn't fire at ~6%
    st = bp._cache_state_of_log(warm_turbo, "turbo-partial-cache")
    assert st and round(st["miss_pct"]) == 6                      # ...but the state is still read
    # lifecycle: a sub-30s in-install build (warm) is still measured (min_secs=0 in the state read).
    warm_lc = "\n".join("2024Z " + l for l in [
        "##[group]Run pnpm install", "pnpm install", "##[endgroup]", ". prepare$ turbo build",
        "Cached:    124 cached, 128 total", "  Time:    8s", "##[group]Run next"])
    assert bp._parse_log(warm_lc) is None                         # 8s build < 30s floor -> no leaf
    st2 = bp._cache_state_of_log(warm_lc, "install-lifecycle-build")
    assert st2 and round(st2["miss_pct"]) == 3                    # ...but the miss is still measured


def _cd(verdict, med=9.0, rng=(2, 44), n=6, upstream_n=6, no_summary_n=0, fork_n=0,
        push=None, push_reason=None, prev=0.1):
    return {"fix_key": "turbo-partial-cache", "verdict": verdict,
            "pr": {"upstream_median": med, "upstream_range": list(rng), "n": n,
                   "upstream_n": upstream_n, "no_summary_n": no_summary_n, "fork_n": fork_n},
            "tail": {"prevalence_max": prev}, "push": push, "push_reason": push_reason}


def test_apply_cache_dist_reframes_by_verdict():
    # T2 (2nd review): the render-side reframe had no behavioral test.
    churn_note = "72/128 cached - 44% rebuilt despite caching ON (cache-key churn?) - BIGGEST LEVER"
    # mostly-warm: strip BIGGEST LEVER + churn hint, reframe around the warm median.
    leaf = {"fix_key": "turbo-partial-cache", "deeper": [{"blocker_note": churn_note}]}
    bp._apply_cache_dist(leaf, _cd("mostly-warm"))
    note = leaf["deeper"][0]["blocker_note"]
    assert "BIGGEST LEVER" not in note and "cache-key churn" not in note and "mostly HITS" in note
    # cold: untouched (a genuinely cold cache keeps its broad claim).
    leaf2 = {"fix_key": "turbo-remote-cache",
             "deeper": [{"blocker_note": "0/110 cached - BIGGEST LEVER"}]}
    bp._apply_cache_dist(leaf2, _cd("cold"))
    assert "BIGGEST LEVER" in leaf2["deeper"][0]["blocker_note"]
    # lifecycle cold/churn GAINS the lever (it hard-codes none).
    leaf3 = {"fix_key": "install-lifecycle-build",
             "deeper": [{"blocker_note": "51s of turbo build runs INSIDE install"}]}
    bp._apply_cache_dist(leaf3, _cd("churn"))
    assert "BIGGEST LEVER" in leaf3["deeper"][0]["blocker_note"]


def test_install_build_section_multiple_installs_detector_and_state_agree():
    # PR #126 3rd-review (adversarial): a job with TWO qualifying `<pm> install` lifecycle-build
    # steps where the EARLIER one is sub-floor (<30s) and misses MORE than the later >=30s one.
    # `_install_build_section` used to return the FIRST section clearing its floor, so the detector
    # (floor 30) fired on the later low-miss section while `_cache_state_of_log` (floor 0) returned
    # the earlier high-miss section — the same drilled run then reported a well-cached gating build
    # in its leaf yet contributed the earlier section's higher miss to the cross-run distribution,
    # flipping the verdict to churn and stamping "BIGGEST LEVER" on a well-cached build. The fix
    # picks the MAX-build section across all install steps, then applies the floor, so both callers
    # land on the SAME section. Assert they measure identical hit/total.
    log = "\n".join("2024Z " + l for l in [
        "##[group]Run pnpm install", "pnpm install", "##[endgroup]", ". prepare$ turbo build",
        "Cached:    5 cached, 10 total",     # section 1: 20s, 50% miss (sub-floor)
        "  Time:    20s",
        "##[group]Run pnpm install", "pnpm install", "##[endgroup]", ". prepare$ turbo build",
        "Cached:    9 cached, 10 total",     # section 2: 50s, 10% miss (the real gate)
        "  Time:    50s",
        "##[group]Run pnpm -w test", "done"])
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "install-lifecycle-build"
    # Detector fires on the 50s section: 9/10 cached, value 50.0s.
    assert leaf["magnitude"]["value"] == 50.0
    assert "9/10 packages cached" in leaf["deeper"][0]["blocker_note"]
    # State reader must read the SAME (50s) section -> 10% miss, NOT section 1's 50% miss.
    st = bp._cache_state_of_log(log, "install-lifecycle-build")
    assert st is not None and round(st["miss_pct"]) == 10


def test_apply_cache_dist_mostly_warm_none_median_is_not_contradictory():
    # PR #126 3rd-review: for a mostly-warm verdict with NO numeric upstream_median, the reframe
    # used to fall through to the miss-tail else-branch ("drilled run is from the miss-heavy
    # minority") — directly contradicting "the cache mostly HITS". It must say mostly-HITS (without
    # the median number), never the miss-heavy-minority wording.
    leaf = {"fix_key": "turbo-remote-cache",
            "deeper": [{"blocker_note": "0/110 cached - BIGGEST LEVER"}]}
    bp._apply_cache_dist(leaf, _cd("mostly-warm", med=None))
    note = leaf["deeper"][0]["blocker_note"]
    assert "mostly HITS" in note and "miss-heavy minority" not in note
    assert "BIGGEST LEVER" not in note


def test_apply_cache_dist_insufficient_strips_overclaim_and_discloses_basis():
    # PR #126 3rd-review: an `insufficient` verdict (fewer than 2 upstream runs exposed a cache
    # summary) has no cross-run grounding — the drilled run's native "BIGGEST LEVER" / churn framing
    # must be stripped and the single-run basis disclosed, not shipped as if it were grounded.
    churn_note = "3/128 cached - 98% rebuilt despite caching ON (cache-key churn?) - BIGGEST LEVER"
    leaf = {"fix_key": "turbo-partial-cache", "deeper": [{"blocker_note": churn_note}]}
    bp._apply_cache_dist(leaf, _cd("insufficient"))
    note = leaf["deeper"][0]["blocker_note"]
    assert "BIGGEST LEVER" not in note and "cache-key churn" not in note
    assert "single sampled run" in note and "cross-run hit rate" in note


def test_cache_health_block_renders_all_disclosure_lines():
    # T2 (2nd review): the health block's fork / no-summary / push-error / marker / caveat lines
    # were all untested. Exercise each live branch.
    cd = _cd("mostly-warm", med=9, rng=(2, 44), n=8, upstream_n=6, no_summary_n=1, fork_n=1,
             push={"median": None, "n": 0, "errors": 2})
    out = "\n".join(bp._cache_health_block(cd))
    assert "median miss **9%**" in out and "across 6 sampled run(s)" in out   # upstream_n, not n
    assert "fork-PR run(s) excluded" in out and "1 run(s) exposed no cache summary" in out
    assert "unknown — 2 log fetch(es) failed" in out                          # push-error line
    assert bp._CACHE_CONTEXT_MARKER in out and "Cache-context caveat" in out   # demoted -> marker+caveat
    # insufficient / absent -> empty (nothing measured to disclose).
    assert bp._cache_health_block(_cd("insufficient")) == []
    assert bp._cache_health_block(None) == []
    # a "no push runs" reason renders when push is None with a reason.
    out2 = "\n".join(bp._cache_health_block(_cd("miss-tail", push=None,
                                               push_reason="no push runs sampled for this workflow")))
    assert "no push runs sampled" in out2


# --- PR #126 4th-review (adversarial): frequency-demotion prose + median display ----------
def _freq_demoted_doc():
    # A `check-packages` present on ALL 20 PRs (present_on=20) but NEVER the per-PR slowest
    # (pole_n=0) — the exact expo/expo case: correctly demoted by the pole-frequency rule while a
    # heavier always-present `native` (pole_n=20) heads the list. Both are drilled poles.
    npop = 20
    checks = [
        {"name": "native", "p50_s": 1400.0, "present_on": npop, "workflow_file": "ci.yml", "pole_n": 20},
        {"name": "check-packages", "p50_s": 300.0, "present_on": npop, "workflow_file": "pkg.yml", "pole_n": 0},
    ]
    pops = [[0.1, [["native", 1400.0], ["check-packages", 300.0]]] for _ in range(npop)]
    return {"pr_critical_path": {
        "critical_path_check": "native", "critical_path_s": 1400.0,
        "checks": checks, "check_present_n_pr": npop, "populations": pops,
        "poles": [
            {"check": "native", "p50_s": 1400.0, "workflow_file": "ci.yml", "job": "native"},
            {"check": "check-packages", "p50_s": 300.0, "workflow_file": "pkg.yml", "job": "check-packages"},
        ],
    }}


def test_render_frequency_demoted_pole_states_frequency_not_presence():
    # F1 (4th adversarial review): a pole DEMOTED by the pole-frequency rule (never the per-PR
    # slowest) but present on EVERY PR used to render "Opt-in / rare — ran on only 20/20 sampled
    # PRs" — self-contradictory (20/20 is not rare), and the wrong reason (the true one is "never
    # the slowest"). With `pole_n` present the role text must state the FREQUENCY reason instead.
    out = bp.render(_freq_demoted_doc())
    # No presence-based contradiction: never "rare/opt-in — ran on only 20/20".
    assert "ran on only 20/20" not in out
    assert "Opt-in / rare" not in out
    # The honest frequency reason is stated, with the pole_n (0/20) and the true presence (20/20).
    assert "Rarely the merge gate" in out
    assert "0/20 sampled PRs" in out and "Present on 20/20 PRs" in out


def _minority_slow_doc(bench_present=20, bench_pole_n=1):
    # `test` is the gate (pole_n=18, p50 400s). `flaky-bench` is SLOWER (p50 700s > blocker) and
    # present on `bench_present`/20 PRs, but the actual per-PR slowest on only `bench_pole_n` PRs —
    # so it is demoted into `minority_slow` (not typical AND p50 > blocker).
    npop = 20
    checks = [
        {"name": "test", "p50_s": 400.0, "present_on": npop, "workflow_file": "ci.yml", "pole_n": 18},
        {"name": "flaky-bench", "p50_s": 700.0, "present_on": bench_present,
         "workflow_file": "bench.yml", "pole_n": bench_pole_n},
    ]
    # flaky-bench appears in exactly `bench_present` populations (the renderer's presence count is
    # populations-derived, not checks[].present_on): slowest on 1, present-but-fast on the rest.
    pops = [[0.1, [["flaky-bench", 2000.0], ["test", 400.0]]]]         # bench slowest once
    for i in range(npop - 1):
        row = [["test", 400.0]]
        if i < bench_present - 1:
            row.append(["flaky-bench", 100.0])                        # present but fast
        pops.append([0.1, row])
    return {"pr_critical_path": {
        "critical_path_check": "test", "critical_path_s": 400.0,
        "checks": checks, "check_present_n_pr": npop, "populations": pops,
        "poles": [
            {"check": "test", "p50_s": 400.0, "workflow_file": "ci.yml", "job": "test"},
            {"check": "flaky-bench", "p50_s": 700.0, "workflow_file": "bench.yml", "job": "flaky-bench"},
        ],
    }}


def test_render_minority_slow_frequency_demoted_states_frequency_not_presence():
    # F1 (fresh review, Angle B): a `minority_slow` member present on a MAJORITY of PRs but rarely
    # the per-PR slowest (pole_n < floor) rendered "ran on only 20/20 sampled PRs — it looks opt-in
    # / conditional (e.g. label-gated)" in the lead-in and "ran on a minority of sampled PRs" in the
    # summary note — both self-contradictory. It must state the frequency reason instead.
    out = bp.render(_minority_slow_doc(bench_present=20))
    assert "opt-in / conditional" not in out
    assert "ran on a minority of sampled PRs" not in out
    assert "ran on only 20/20" not in out
    # lead-in + summary both use the frequency framing.
    assert "runs on 20/20 sampled PRs, but it's rarely the actual slowest" in out
    assert "present on most sampled PRs but rarely the actual slowest" in out


def test_render_minority_slow_genuine_minority_keeps_opt_in_wording():
    # The other case must be unchanged: a check that genuinely ran on a MINORITY (present 3/20) is
    # still framed opt-in / conditional — the frequency reword must not swallow this true case.
    out = bp.render(_minority_slow_doc(bench_present=3))
    # (render flattens the em-dash separator to ASCII ` - `, per check_no_typographic_dashes)
    assert "ran on only 3/20 sampled PRs - it looks opt-in / conditional" in out
    assert "ran on a minority of sampled PRs (label-gated or path-filtered" in out


def test_render_legacy_no_pole_n_keeps_presence_wording():
    # Backward compat: a pre-`pole_n` doc (no `pole_n` on any check) still falls back to the legacy
    # PRESENCE demotion + its original "Opt-in / rare — ran on only N/npop" wording, unchanged.
    doc = _freq_demoted_doc()
    for c in doc["pr_critical_path"]["checks"]:
        c.pop("pole_n", None)
    # Legacy presence rule: check-packages present on all 20 stays "typical" (present > 50%), so to
    # exercise the rare-branch wording drop its presence below the floor.
    doc["pr_critical_path"]["checks"][1]["present_on"] = 3
    doc["pr_critical_path"]["populations"] = (
        [[0.1, [["native", 1400.0], ["check-packages", 300.0]]] for _ in range(3)]
        + [[0.1, [["native", 1400.0]]] for _ in range(17)])
    out = bp.render(doc)
    # (the render flattens the em-dash separator to an ASCII ` - `, per check_no_typographic_dashes)
    assert "Opt-in / rare - ran on only 3/20 sampled PRs" in out
    assert "Rarely the merge gate" not in out


def test_pct_disp_matches_mag_line_fmtm_rule():
    # F3 (4th adversarial review): the shared cache-health percent formatter must apply the SAME
    # whole-vs-one-decimal rule `_mag_line`'s local `fmtm` uses, so a half-percent median never
    # rounds across the churn floor and reads as disagreeing with the re-derived verdict.
    fmtm = (lambda x: f"{x:.0f}%" if abs(x - round(x)) < 0.05 else f"{x:.1f}%")
    for x in (0.0, 6.0, 39.0, 39.5, 40.0, 40.4, 51.5, 99.5, 100.0):
        assert bp._pct_disp(x) == fmtm(x), x
    assert bp._pct_disp(39.5) == "39.5%" and bp._pct_disp(40.0) == "40%"


def test_cache_health_block_half_percent_median_does_not_round_across_floor():
    # F3: a median of 39.5% (below the 40% churn floor → `mostly-warm`) must render "39.5%", not a
    # rounded "40%" that would sit ON the floor next to the "cache mostly HITS" verdict.
    # Push (default-branch) median carries the SAME half-percent hazard — a distinct 12.5% value
    # must render "12.5%", not a rounded "12%"/"13%".
    cd = _cd("mostly-warm", med=39.5, rng=(39, 40), n=2, upstream_n=2,
             push={"median": 12.5, "n": 3})
    out = "\n".join(bp._cache_health_block(cd))
    assert "median miss **39.5%**" in out          # PR bucket
    assert "median miss **40%**" not in out
    assert "median miss **12.5%**" in out           # push bucket (same formatter)
    assert "12%" not in out and "13%" not in out
    assert "mostly HITS" in out


# --------------------------------------------------------------------------- #
# Bug 3 — one canonical matrix-base parser. The trailing-paren REDUCTION is
# shared (blocking_path._matrix_base_raw), so the drill engine's key-building
# _matrix_base_name and the renderer's display _matrix_base can never disagree
# on WHERE the base ends (they used to: the old _matrix_base matched up to the
# FIRST `(`, while _matrix_base_name stripped only the TRAILING parenthetical —
# GitHub's actual matrix-leg naming, `<job> (<params>)`).
#
# Only the reduction is shared, NOT the display normalization. _matrix_base_name
# builds required-check MATCHING KEYS, so it must be byte-identical to the
# original — scope- and case-PRESERVING. _matrix_base builds display labels, so
# it layers `_clean_label` (strip `@scope/`) + `.lower()` on top. Both contracts
# are locked below so a future "just delegate to _matrix_base" shortcut (which
# would silently collide `@a/pkg build (18)` with `@b/pkg build (18)` and fold
# `Build`/`build` together in the engine's keys) fails loudly here.
# --------------------------------------------------------------------------- #
def test_matrix_base_parsers_agree_on_where_base_ends():
    cases = [
        "test (18, ubuntu)",   # canonical matrix leg -> "test"
        "test",                # no parenthetical at all -> None / passthrough
        "build (x) fast",      # embedded, non-trailing paren -> NOT a matrix leg
        "test (a) (b)",        # two trailing groups -> peel only the outer one
    ]
    for name in cases:
        renderer_base = bp._matrix_base(name)       # blocking_path = DISPLAY parser
        engine_base = cr._matrix_base_name(name)    # collect_runs = ENGINE key builder
        if renderer_base is None:
            assert engine_base == name.strip(), (name, renderer_base, engine_base)
        else:
            # They agree on WHERE the base ends; the renderer additionally
            # lowercases for display while the engine PRESERVES case (blessed by
            # test_matrix_base_name_preserves_scope_and_case_for_engine_keys).
            # Compare case-normalized so adding a mixed-case case here (e.g.
            # "Test (18, ubuntu)") can't spuriously fail this cut-point check.
            assert renderer_base == engine_base.lower(), (name, renderer_base, engine_base)
    # Spot-check the actual reduced values so a future edit can't satisfy the
    # "they agree" assertion above by breaking both sides identically.
    assert bp._matrix_base("test (18, ubuntu)") == "test"
    assert bp._matrix_base("test") is None
    assert bp._matrix_base("build (x) fast") is None
    assert bp._matrix_base("test (a) (b)") == "test (a)"


def test_matrix_base_name_preserves_scope_and_case_for_engine_keys():
    # The engine's _matrix_base_name feeds required-check MATCHING KEYS, so it
    # must preserve BOTH the monorepo `@scope/` prefix and the original case —
    # otherwise `@a/pkg build (18)` and `@b/pkg build (18)` would collide, and a
    # `Build` job would fold into a `build` job.
    assert cr._matrix_base_name("@myorg/pkg build (18)") == "@myorg/pkg build"
    assert cr._matrix_base_name("Build (18)") == "Build"
    # The renderer's display parser, by contrast, strips scope + lowercases.
    assert bp._matrix_base("@myorg/pkg build (18)") == "pkg build"
    assert bp._matrix_base("Build (18)") == "build"


def test_matrix_base_tolerates_one_level_of_nested_parens():
    # A matrix param VALUE can itself contain parens (e.g. a version string
    # "18 (LTS)"), so the trailing group must tolerate ONE nesting level or the
    # leg isn't recognized and loses its grouping to its siblings.
    assert bp._matrix_base_raw("test (18 (LTS), ubuntu)") == "test"
    assert cr._matrix_base_name("test (18 (LTS), ubuntu)") == "test"
    assert bp._matrix_base("test (18 (LTS), ubuntu)") == "test"
    # Non-nested behavior is unchanged, and DEEP (2-level) nesting stays not-a-leg.
    assert bp._matrix_base_raw("test (18, ubuntu)") == "test"
    assert bp._matrix_base_raw("build (x) fast") is None
    assert bp._matrix_base_raw("test (a (b (c)))") is None


# Note: the best-practice grade card renderer moved to a separate internal skill
# at the score-ectomy (2026-07-16); its render tests live in that skill's own
# tests. ci-speedup carries zero grading machinery, so there is no card surface
# to pin here anymore.


def test_count_noun_pluralizes_provenance_counts():
    # Provenance strings ("Where this data comes from") render census counts as
    # `N runs / M jobs across K workflows`; a naive f"{n} workflows" prints the
    # live-run nit `across 1 workflows`. `_count_noun` fixes singular/plural, and
    # passes a non-int (degraded/partial doc) through without pluralizing.
    assert bp._count_noun(1, "workflow") == "1 workflow"
    assert bp._count_noun(3, "workflow") == "3 workflows"
    assert bp._count_noun(1, "run") == "1 run"
    assert bp._count_noun(0, "job") == "0 jobs"
    assert bp._count_noun(None, "workflow") == "None workflows"


# ── Owner UX edit 2026-07-19: Long pole map, single Bottom-line block, name-collision ──

def _collision_doc(*, colliding: bool = True) -> dict:
    """A one-pole doc modeled on the live internal-dev-repo report: the check `test` IS
    the gate; a small `Test` step collides with the check name; the dominant step is the
    guards step. With `colliding=False` the small step is renamed so no collision fires."""
    small = "Test" if colliding else "Unit suite"
    return {
        "repo": "acme/site", "repo_visibility": "public",
        "scanned_at": "2026-07-19T00:00:00Z", "commit_sha": "09e8243", "skill_commit_sha": "dd51d85",
        "findings": [],
        "data_sources": {"runs_sampled": 18, "jobs_sampled": 26, "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "test", "p50_s": 538.0,
                "workflow_file": ".github/workflows/ci.yml", "job": "test",
                "dominant_step": "Verify the guards can actually fail (mutation registry)",
                "dominant_p50_s": 371.0,
                "steps": [
                    {"step": "Checkout", "category": "setup", "p50_s": 18.0},
                    {"step": small, "category": "test", "p50_s": 31.0},
                    {"step": "Verify the guards can actually fail (mutation registry)",
                     "category": "other", "p50_s": 371.0},
                ],
            }],
            "checks": [{"name": "test", "p50_s": 538.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"},
                       {"name": "guard shard 3/4", "p50_s": 150.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"}],
            "populations": [],
        },
    }


# Built to the REAL capture schema `collect_runs._step_timeline` writes — each entry
# keyed `name`/`number`/`start_s`/`dur_s` (verified live 2026-07-20 against
# `test-*.steps.json`) — NOT the `step` key the pole's P50 list uses. The old fixture
# was hand-built with `step` keys, a shape the capture pipeline never produces, which is
# exactly how issue #92 (`_check_step_collision` read only `step`, so a captured timeline
# scanned all-empty and the collision clause never fired on a real drilled pole) passed
# its own test. The schema-parity pin below pins these keys to the writer's output.
_COLLISION_TIMELINE = {
    "job_dur_s": 541.0, "run_url": "https://github.com/acme/site/actions/runs/99",
    "steps": [
        {"name": "Checkout", "number": 1, "start_s": 0.0, "dur_s": 18.0},
        {"name": "Test", "number": 2, "start_s": 18.0, "dur_s": 31.0},
        {"name": "Verify the guards can actually fail (mutation registry)",
         "number": 3, "start_s": 49.0, "dur_s": 371.0},
    ]}


def _map_section(md: str) -> str:
    """The Long pole map section only (up to the next `## ` header)."""
    return md.split("## 🗺️ Long pole map", 1)[1].split("\n## ", 1)[0]


def _turbo_cold_log() -> str:
    # Reused parsed-leaf fixture: a turbo cold-cache log `_parse_log` recognizes as
    # `turbo-remote-cache` with a single-level `deeper` of 2 rows + a blocker note.
    return "\n".join([
        "   • Remote caching disabled",
        "cache miss, executing aaa",
        "cache miss, executing bbb",
        " Tasks:    149 successful, 149 total",
        "Cached:    0 cached, 149 total",
        "  Time:    9m48.772s",
    ])


def _turbo_build_doc() -> dict:
    """A one-pole `build` doc whose dominant `Build` step's captured log parses to a turbo
    cold-cache leaf (category `build`, matching the pole's `dominant_category`, so it is
    crowned not demoted). Two checks on level 1; one drilled pole with a real Level 3."""
    return {
        "repo": "acme/site", "repo_visibility": "public",
        "scanned_at": "2026-07-19T00:00:00Z", "commit_sha": "09e8243",
        "skill_commit_sha": "dd51d85", "findings": [],
        "data_sources": {"runs_sampled": 18, "jobs_sampled": 26, "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "build", "p50_s": 600.0,
                "workflow_file": ".github/workflows/ci.yml", "job": "build",
                "dominant_step": "Build", "dominant_p50_s": 588.0,
                "dominant_category": "build",
                "steps": [
                    {"step": "Checkout", "category": "setup", "p50_s": 12.0},
                    {"step": "Build", "category": "build", "p50_s": 588.0},
                ],
            }],
            "checks": [{"name": "build", "p50_s": 600.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"},
                       {"name": "lint", "p50_s": 120.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"}],
            "populations": [],
        },
    }


def test_long_pole_map_renders_after_contents_before_pole1():
    # The FULL blocker cascade (owner-approved 2026-07-19), placed AFTER the Contents and
    # BEFORE Long pole 1. Checks-first: level 1 = the flat race of merge-gating checks;
    # ◀┐ descends into the gate check's steps (level 2); % is share of the check's own p50.
    md = bp.render(_collision_doc(), {}, {}, {"ci": _COLLISION_TIMELINE["run_url"]},
                   "2026-07-19T00:00:00Z", {"ci": _COLLISION_TIMELINE})
    assert "## 🗺️ Long pole map" in md
    seg = _map_section(md)
    # Hierarchy glossary (issue #96): rendered ONCE, under the map heading, above the
    # fence — the first place workflow/job/step all collide.
    _glossary = ("A **workflow** is one YAML file under `.github/workflows/`; a run of it "
                 "executes its **jobs** in parallel (each on its own runner); each job runs "
                 "its **steps** in sequence.")
    assert md.count(_glossary) == 1
    assert _glossary in seg
    assert seg.index(_glossary) < seg.index("```text")
    # Ordering: Contents → Long pole map → Long pole 1.
    i_contents = md.index("## 📋 Contents")
    i_map = md.index("## 🗺️ Long pole map")
    i_pole1 = md.index("## 🔴 Long pole 1:")
    assert i_contents < i_map < i_pole1
    # Level-1 lead + labels carry the workflow file (pin 1).
    assert "Level 1 - checks racing on every PR; the merge waits for the slowest:" in seg
    assert "test · ci.yml" in seg
    assert "guard shard 3/4 · ci.yml" in seg
    # The ◀┐ + connector + `▼ Level 2 — inside test` cascade renders (pin 2).
    assert "◀┐" in seg
    assert "┌" in seg and "┘" in seg
    assert "▼ Level 2 - inside test, steps run one after another:" in seg
    # Level-2 dominant step marked ◀, pct = share of the CHECK's p50: 371/538 → 69% (pin 3).
    assert "Verify the guards can actually f" in seg
    assert "69% ◀" in seg
    # No roll-up row in the map's level 2 — that lives in the pole section only (pin 4).
    assert "smaller steps" not in seg
    # Closing prose is the cascade line, AFTER the fence (pin 5); the old #75 line is gone here.
    assert "Each ◀ marks the blocker the next level opens." in md
    assert "Long pole 1 below drills the marked step" in md
    assert "each **Long pole** finding below drills" not in md
    assert "(the gate)" in md            # Contents tag survives, unrelated to the map


def test_long_pole_map_degenerate_single_check_no_one_bar_level():
    # Pin 6: a doc with a SINGLE check (and a single step) must NOT render a one-bar level 1
    # — degenerate levels collapse. With nothing that qualifies, the section is skipped.
    doc = _collision_doc()
    cp = doc["pr_critical_path"]
    cp["checks"] = [cp["checks"][0]]                       # one check
    cp["poles"][0]["steps"] = [cp["poles"][0]["steps"][0]]  # one step
    md = bp.render(doc, {}, {}, {}, "2026-07-19T00:00:00Z", {})
    assert "## 🗺️ Long pole map" not in md
    # The hierarchy glossary rides the map — absent when the map is skipped (issue #96).
    assert "A **workflow** is one YAML file under" not in md


def test_long_pole_map_level1_only_fallback_keeps_75_closing():
    # When the descent pole's check isn't among the shown level-1 rows (here: its steps are
    # stripped so level 2 can't draw), fall back to the level-1-only render with the ORIGINAL
    # #75 closing line — a data-poor repo renders exactly as before.
    doc = _collision_doc()
    doc["pr_critical_path"]["poles"][0]["steps"] = []      # no usable steps → no level 2
    md = bp.render(doc, {}, {}, {}, "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    assert "test · ci.yml" in seg                          # level 1 still draws (>=2 checks)
    assert "▼ Level 2" not in seg                          # no cascade
    assert "each **Long pole** finding below drills" in md  # the #75 closing line returns
    assert "Each ◀ marks the blocker the next level opens." not in md


def test_long_pole_map_level3_renders_with_leaf_and_pole_keeps_its_own():
    # Pin 7: when the descent pole's shared leaf has a `deeper` first level of >=2 rows, the
    # MAP renders Level 3 (with its blocker note), AND the pole section below still renders
    # its OWN Level 3 — the two share one derivation, so they can't disagree.
    md = bp.render(_turbo_build_doc(), {"ci": _turbo_cold_log()}, {},
                   {"ci": "https://github.com/acme/site/actions/runs/7"},
                   "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    assert "▼ Level 3 - inside `Build`: turbo builds 149 packages" in seg
    assert "rebuilt (cache miss)" in seg
    assert "Remote caching DISABLED - BIGGEST LEVER" in seg   # deeper[0] blocker note
    # The pole section (after the map) renders its own Level 3 too.
    pole = md.split("## 🔴 Long pole 1:", 1)[1]
    assert "Level 3 - inside `Build`" in pole


def test_long_pole_map_chain_form_keeps_lead_and_cascades():
    # Pin 8: on a `needs:` chain repo the level-1 lead keeps the chain-variant wording
    # ("run in sequence (`needs:`), so their times ADD on the gate path") and still cascades.
    doc = _collision_doc()
    doc["pr_critical_path"]["chain_summary"] = {
        "modal_chain": ["guard shard 3/4", "test"], "chain_p50_s": 688.0}
    md = bp.render(doc, {}, {}, {"ci": _COLLISION_TIMELINE["run_url"]},
                   "2026-07-19T00:00:00Z", {"ci": _COLLISION_TIMELINE})
    seg = _map_section(md)
    assert "Level 1 - checks racing on every PR - except" in seg
    assert "run in sequence (`needs:`), so their times ADD on the gate path" in seg
    assert "▼ Level 2 - inside test" in seg                 # still cascades


def test_long_pole_map_does_not_change_pole_waterfall():
    # Pin 9: the pole 1 waterfall is unchanged by the map — it still renders its own
    # Level 2 header line exactly as before (the map is presentation-only, shares no state
    # that alters the loop's per-pole render).
    md = bp.render(_collision_doc(), {}, {}, {"ci": _COLLISION_TIMELINE["run_url"]},
                   "2026-07-19T00:00:00Z", {"ci": _COLLISION_TIMELINE})
    pole = md.split("## 🔴 Long pole 1:", 1)[1]
    assert "Level 2 - inside that one job" in pole          # the pole's own timeline lead


# ── Long pole map: minority-presence honesty marking (2026-07-21) ─────────────────────
# A level-1 row can be "typical" (kept in `src`) by pole FREQUENCY or the required-check
# exemption yet have run on only a MINORITY of sampled PRs. Under the "every PR" lead a reader
# would misread its conditional time as the normal blocker (the playwright `Windows (firefox)`,
# 2/20, defect). The map marks such rows with ` †`, reframes the lead honestly, and adds a
# legend line with each marked row's real sampled-PR fraction. Presentation-only.

def _minority_map_doc(*, denom: int = 20, e2e_present: int = 2,
                      required: bool = True, chain: bool = False) -> dict:
    """Two level-1 checks: `unit` (present on every PR — the descent pole, with steps) and a
    slow `Windows E2E (firefox)` kept in `src` via the required exemption but present on only
    `e2e_present`/`denom` PRs (a minority). `denom` sets the presence denominator so the
    small-sample case (denom < the rare-presence floor) can be driven."""
    doc = {
        "repo": "acme/site", "repo_visibility": "public",
        "scanned_at": "2026-07-19T00:00:00Z", "commit_sha": "09e8243",
        "skill_commit_sha": "dd51d85", "findings": [],
        "data_sources": {"runs_sampled": 18, "jobs_sampled": 26, "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": denom, "sample_target": denom, "sample_complete": True,
            "check_present_n_pr": denom,
            "poles": [{
                "check": "unit", "p50_s": 538.0,
                "workflow_file": ".github/workflows/ci.yml", "job": "unit",
                "dominant_step": "Verify the guards can actually fail (mutation registry)",
                "dominant_p50_s": 371.0,
                "steps": [
                    {"step": "Checkout", "category": "setup", "p50_s": 18.0},
                    {"step": "Run tests", "category": "test", "p50_s": 31.0},
                    {"step": "Verify the guards can actually fail (mutation registry)",
                     "category": "other", "p50_s": 371.0},
                ],
            }],
            "checks": [
                {"name": "Windows E2E (firefox)", "p50_s": 4377.0,
                 "present_on": e2e_present, "workflow_file": ".github/workflows/e2e.yml"},
                {"name": "unit", "p50_s": 538.0, "present_on": denom,
                 "workflow_file": ".github/workflows/ci.yml"},
            ],
            "populations": [],
        },
    }
    if required:
        doc["required_checks"] = ["Windows E2E (firefox)"]
    if chain:
        doc["pr_critical_path"]["chain_summary"] = {
            "modal_chain": ["unit", "Windows E2E (firefox)"], "chain_p50_s": 4915.0}
    return doc


def _level1_bar_cols(seg: str) -> list[int]:
    """The column of the first bar glyph on each level-1 row (the block before `▼ Level 2`).
    Equal across rows == the fixed-width label field still pads to the same width, so bars and
    connectors stay aligned even after a ` †` marker is appended."""
    top = seg.split("▼ Level 2", 1)[0]
    return [line.index("█") for line in top.splitlines() if "█" in line]


def test_long_pole_map_minority_row_marks_reframes_and_legends():
    # A required check present on only 2/20 PRs renders in level 1 (required exemption); it
    # must be MARKED with ` †`, the lead reframed to "a typical PR" + the † disclosure, and a
    # legend must name it with its real fraction. `unit` (20/20) stays unmarked.
    md = bp.render(_minority_map_doc(), {}, {}, {}, "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    # The minority row carries the trailing ` †` (short enough to render un-truncated).
    assert "Windows E2E (firefox) · e2e.yml †" in seg
    # The majority descent row is NOT marked.
    unit_line = [l for l in seg.splitlines() if l.strip().startswith("unit ·")][0]
    assert "†" not in unit_line
    # The lead is reframed honestly (em dashes are flattened to ASCII at the render boundary).
    assert ("Level 1 - checks racing on a typical PR; the merge waits for the slowest - rows "
            "marked † ran on a minority of sampled PRs (path-conditional - they gate only the "
            "PRs that trigger them):") in seg
    assert "checks racing on every PR;" not in seg          # old lead gone on this doc
    # The legend names the marked row with its real fraction, AFTER the fence.
    assert "† `Windows E2E (firefox)` ran on 2/20 sampled PRs." in md
    assert md.index("```", md.index("## 🗺️")) < md.index("† `Windows E2E (firefox)` ran on")
    # Bars stay aligned across rows despite the ` †` on row 0.
    cols = _level1_bar_cols(seg)
    assert len(cols) == 2 and len(set(cols)) == 1


def test_long_pole_map_all_majority_active_filter_byte_identical():
    # With the presence filter ACTIVE (denom >= floor) but every rendered row present on every
    # PR, the output is unchanged: original "every PR" lead, no ` †`, no legend.
    md = bp.render(_minority_map_doc(e2e_present=20, required=False),
                   {}, {}, {}, "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    assert "Level 1 - checks racing on every PR; the merge waits for the slowest:" in seg
    assert "†" not in seg
    assert "ran on a minority of sampled PRs" not in md
    assert "sampled PRs." not in _map_section(md)            # no legend line


def test_long_pole_map_small_sample_no_minority_marking():
    # Below the rare-presence floor (denom < _RARE_PRESENCE_MIN_PR) presence is noise: the
    # filter is inactive, nothing is marked, and the map renders exactly as today.
    md = bp.render(_minority_map_doc(denom=4, e2e_present=1),
                   {}, {}, {}, "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    assert "Level 1 - checks racing on every PR; the merge waits for the slowest:" in seg
    assert "†" not in seg
    assert "ran on a minority of sampled PRs" not in md


def test_long_pole_map_chain_minority_arm_carries_the_dagger_clause():
    # On a `needs:` chain repo the chain-variant lead keeps its "run in sequence" wording AND
    # carries the minority † disclosure when a rendered row is minority-present.
    md = bp.render(_minority_map_doc(chain=True), {}, {}, {}, "2026-07-19T00:00:00Z", {})
    seg = _map_section(md)
    assert "Level 1 - checks racing on a typical PR - except" in seg
    assert "run in sequence (`needs:`), so their times ADD on the gate path" in seg
    assert "rows marked † ran on a minority of sampled PRs" in seg
    assert "Windows E2E (firefox) · e2e.yml †" in seg
    assert "† `Windows E2E (firefox)` ran on 2/20 sampled PRs." in md


def test_check_step_name_collision_disambiguates():
    # A check whose name collides with a small step name inside it gets ONE clarifying
    # clause on the pole header, and the waterfall lead is strengthened to "**steps** (not
    # checks)". Both are collision-triggered — absent on a non-colliding pole.
    md = bp.render(_collision_doc(colliding=True), {}, {},
                   {"ci": _COLLISION_TIMELINE["run_url"]}, "2026-07-19T00:00:00Z",
                   {"ci": _COLLISION_TIMELINE})
    assert "the check named `test`; its small `Test` step below is not the bottleneck" in md
    assert "the dominant step is `Verify the guards can actually fail…`" in md
    assert "its **steps** (not checks) run one after another" in md

    # No collision (the small step renamed): neither clause fires — never boilerplate.
    # Rename the real timeline key (`name`), the one the capture pipeline emits.
    tl2 = json.loads(json.dumps(_COLLISION_TIMELINE))
    tl2["steps"][1]["name"] = "Unit suite"
    md2 = bp.render(_collision_doc(colliding=False), {}, {},
                    {"ci": tl2["run_url"]}, "2026-07-19T00:00:00Z", {"ci": tl2})
    assert "is not the bottleneck" not in md2
    assert "(not checks)" not in md2
    assert "its steps run **one after another**" in md2      # plain lead returns


def test_check_step_collision_helper_ignores_dominant_name_match():
    # The shared name IS the dominant step → no disambiguation owed (a legitimately
    # name-matched bottleneck), so the helper returns the empty no-op.
    pole = {"check": "build", "dominant_step": "build",
            "steps": [{"step": "build"}, {"step": "checkout"}]}
    assert bp._check_step_collision(pole, None) == ("", "")
    # A NON-dominant step collides → (colliding_step, dominant) is returned.
    pole2 = {"check": "test", "dominant_step": "Verify guards",
             "steps": [{"step": "Test"}, {"step": "Verify guards"}]}
    assert bp._check_step_collision(pole2, None) == ("Test", "Verify guards")
    # No known dominant step (None / missing / empty) → no disambiguation owed. Without the
    # early-out, `dom == ""` makes the "not the dominant step" guard always true and the
    # fallback returns `(nm, nm)`, rendering "its small `Test` step is not the bottleneck —
    # the dominant step is `Test`" — a self-contradiction (Greptile P2, PR #75).
    for absent in (None, "", "   "):
        p = {"check": "test", "dominant_step": absent, "steps": [{"step": "Test"}]}
        assert bp._check_step_collision(p, None) == ("", "")
    assert bp._check_step_collision(
        {"check": "test", "steps": [{"step": "Test"}]}, None) == ("", "")


def test_collision_reader_accepts_the_exact_keys_the_timeline_writer_emits():
    # Schema-parity pin (issue #92): the collision scan reads step names off the captured
    # timeline; the timeline is written by `collect_runs._step_timeline`. If the writer's
    # step key drifts out from under the reader (the #92 bug: writer emits `name`, reader
    # read `step`), the collision clause silently dies on every drilled pole. So pin the
    # WRITER's real output to the READER: invoke the writer on a synthetic jobs payload,
    # then assert (a) it emits `name`, and (b) `_check_step_collision` recovers the
    # colliding step from that unmodified writer output.
    job = {
        "name": "test",
        "html_url": "https://github.com/o/r/actions/runs/9/job/77",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "id": 77,
        "steps": [
            {"name": "Checkout", "number": 2, "started_at": "2026-01-01T00:00:00Z",
             "completed_at": "2026-01-01T00:00:18Z"},
            {"name": "Test", "number": 3, "started_at": "2026-01-01T00:00:18Z",
             "completed_at": "2026-01-01T00:00:49Z"},   # small, collides with check `test`
            {"name": "Verify the guards can actually fail (mutation registry)",
             "number": 4, "started_at": "2026-01-01T00:00:49Z",
             "completed_at": "2026-01-01T00:05:00Z"},   # the real dominant step
        ],
    }
    tl = cr._step_timeline(job, "test", 300.0)
    # (a) The writer keys every step `name` (NOT `step`); `_tl_name` reads that key.
    assert tl["steps"] and all("name" in s for s in tl["steps"])
    assert all("step" not in s for s in tl["steps"])
    assert bp._tl_name(tl["steps"][1]) == "Test"
    # (b) The reader recovers the collision from the writer's UNMODIFIED output — the very
    # path that returned ('', '') before the fix (drilled poles always carry a timeline).
    pole = {"check": "test",
            "dominant_step": "Verify the guards can actually fail (mutation registry)"}
    assert bp._check_step_collision(pole, tl) == (
        "Test", "Verify the guards can actually fail (mutation registry)")


def test_collision_clause_renders_on_a_live_shaped_real_schema_timeline():
    # Live-shape regression pin (issue #92, confirmed on internal-dev-repo 2026-07-20):
    # the check `test` gates; a small non-dominant `Test` step reads as "so test isn't the
    # bottleneck"; the dominant step is a long install/verify step. Modeled on the real
    # capture (`test-*.steps.json`): a REAL-schema timeline (`name`/`number`/`start_s`/
    # `dur_s`). Both live surfaces must render: the role clause on the pole header AND the
    # "**steps** (not checks)" waterfall lead.
    dom_name = "Verify install-CTA instrumentation (production build) + 2 more install steps"
    doc = {
        "repo": "acme/site", "repo_visibility": "public",
        "scanned_at": "2026-07-20T00:00:00Z", "commit_sha": "09e8243",
        "skill_commit_sha": "dd51d85", "findings": [],
        "data_sources": {"runs_sampled": 18, "jobs_sampled": 26, "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "test", "p50_s": 177.0,
                "workflow_file": ".github/workflows/ci.yml", "job": "test",
                "dominant_step": dom_name, "dominant_p50_s": 92.0,
                "steps": [
                    {"step": "Checkout", "category": "setup", "p50_s": 13.0},
                    {"step": "Test", "category": "test", "p50_s": 34.0},
                    {"step": dom_name, "category": "other", "p50_s": 92.0},
                ],
            }],
            "checks": [{"name": "test", "p50_s": 177.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"},
                       {"name": "guard shard 2/4", "p50_s": 90.0, "present_on": 20,
                        "workflow_file": ".github/workflows/ci.yml"}],
            "populations": [],
        },
    }
    # REAL capture schema — `name`/`number`/`start_s`/`dur_s`, exactly what the writer emits.
    tl = {
        "job_dur_s": 177.0, "run_url": "https://github.com/acme/site/actions/runs/99",
        "steps": [
            {"name": "Checkout", "number": 3, "start_s": 2.0, "dur_s": 13.0},
            {"name": "Test", "number": 10, "start_s": 25.0, "dur_s": 34.0},
            {"name": dom_name, "number": 20, "start_s": 85.0, "dur_s": 92.0},
        ]}
    md = bp.render(doc, {}, {}, {"ci": tl["run_url"]}, "2026-07-20T00:00:00Z", {"ci": tl})
    # Surface 1: the role clause on the pole header.
    assert "the check named `test`; its small `Test` step below is not the bottleneck" in md
    assert "the dominant step is `Verify install-CTA instrumentation" in md
    # Surface 2: the strengthened waterfall lead.
    assert "its **steps** (not checks) run one after another" in md


def test_top_matter_is_one_bottom_line_blockquote_then_contents():
    # Owner UX edit 2026-07-19: metadata table → ONE Bottom-line blockquote → Contents,
    # nothing else. The folded headline claim + the config-era caveat live INSIDE the same
    # blockquote (no blank line splits it), and the bill-scope note moved to Data sources.
    doc = _collision_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-18T08:00:00Z",
        "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 16, "post_count": 2,
        "sufficiency_min": 6}]
    md = bp.render(doc, {}, {}, {"ci": _COLLISION_TIMELINE["run_url"]},
                   "2026-07-19T00:00:00Z", {"ci": _COLLISION_TIMELINE})
    # Everything from the Bottom line down to the Contents is ONE contiguous blockquote:
    # every non-blank line in that span is a '>' quote line (no blank line splits it).
    lines = md.splitlines()
    i_bl = next(i for i, l in enumerate(lines) if l.startswith("> **Bottom line.**"))
    i_toc = next(i for i, l in enumerate(lines) if l.startswith("## 📋 Contents"))
    between = "\n".join(lines[i_bl:i_toc])
    body = [l for l in lines[i_bl:i_toc] if l.strip()]           # drop the closing blank
    assert body and all(l.startswith(">") for l in body), between
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in between            # era caveat folded in
    # The bill-scope methodology note moved OUT of the top matter, down into Data sources.
    assert "keep the full sample by design" not in between
    ds = md.split("## 🗄️ Data sources", 1)[1]
    assert "keep the full sample by design" in ds


# --------------------------------------------------------------------------- #
# Fence-escaping (report-corruption / verifier-desync): repo-controlled free text
# — check/job/step names and verbatim job-log evidence lines — is dropped into the
# rendered Markdown. A stray triple-backtick would close a ```text fence early,
# corrupting the report AND desyncing verify_report's own fence split (it parses the
# same text with `re.findall(r"```text\n(.*?)```")`). `_fence_safe`/`_safe_span`
# neutralize it at the sinks; these lock the behaviour + the byte-identity contract.
# --------------------------------------------------------------------------- #

def _load_vr():
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "tests" / "verify_report.py"
    spec = importlib.util.spec_from_file_location("verify_report", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_report"] = mod  # so its @dataclass resolves
    spec.loader.exec_module(mod)
    return mod


def test_fence_safe_neutralizes_hostile_text_and_is_byte_identical_on_clean_input():
    # BYTE-IDENTICAL on clean single-line input (the invariant the alignment/keys rely on).
    for clean in ("test", "guard shard 2/4",
                  "Verify install-CTA instrumentation (production build)"):
        assert bp._fence_safe(clean) == clean
        # `_clean_label` (the scope-strip) is deliberately NOT fence-safed — its verbatim
        # verify_report._strip_scope twin is pinned by test_s1a — but `_lbl` (display) IS.
        # `_lbl` truncates to `_LBLW`; fence-safety must not change that, so a within-width clean
        # name is byte-identical (the long name only truncates — pre-existing behaviour).
        if len(clean) <= bp._LBLW:
            assert bp._lbl(clean) == clean
    # A run of >=3 backticks (the ONLY thing that can close a ```text fence) is defused to a
    # same-length, provably-non-terminating apostrophe run; 1-2 backticks (harmless in a fence)
    # are left intact.
    assert bp._fence_safe("a ```b``` c") == "a '''b''' c"
    assert bp._fence_safe("`x` and ``y``") == "`x` and ``y``"    # <3 runs untouched
    assert "`" * 3 not in bp._fence_safe("close ```` me")        # 4-run also defused
    # Embedded newlines/CR collapse to a single space (a name/step/log line is ONE line, so it
    # can never become its own all-backtick line inside a fence).
    assert bp._fence_safe("line1\nline2\r\nline3") == "line1 line2 line3"
    assert "\n" not in bp._fence_safe("a\n```\nb")
    # Dangerous control chars are dropped; tab is KEPT (legit log indentation, harmless in a fence).
    assert bp._fence_safe("a\x00b\x1bc\x07d") == "abcd"
    assert bp._fence_safe("indent\tok") == "indent\tok"
    # `_safe_span` wraps as inline code with NO surviving backtick to close the delimiter.
    span = bp._safe_span("test ```echo pwned```")
    assert span.startswith("`") and span.endswith("`")
    assert "```" not in span and span.count("`") == 2   # only the two delimiters


def _hostile_doc():
    doc = _doc_one_pole()
    pole = doc["pr_critical_path"]["poles"][0]
    pole["check"] = "test ```echo pwned```"
    # A step name carrying BOTH a triple-backtick and an embedded newline (waterfall-label sink).
    pole["steps"][0]["step"] = "Run ```rm -rf```\nline2"
    pole["dominant_step"] = "Run ```rm -rf```\nline2"
    return doc


def test_hostile_check_and_step_names_do_not_corrupt_the_rendered_report():
    md = bp.render(_hostile_doc(), {}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"}, "2026-06-08")
    # No raw triple-backtick from repo text survived anywhere — every sink defused it, so the
    # only ``` runs left are the renderer's own fence delimiters.
    assert "echo pwned" in md                       # the name still renders (readable)
    assert "```echo pwned```" not in md             # ...but defused, never as a live fence
    assert "```rm -rf```" not in md
    # Fence-delimiter lines are BALANCED (even) — the corruption + verifier-desync signature.
    assert len(re.findall(r"(?m)^`{3,}", md)) % 2 == 0
    # The pole heading renders on ONE physical line (the embedded newline was collapsed).
    hdr = next(l for l in md.splitlines() if "Long pole 1:" in l)
    assert "echo pwned" in hdr and "\n" not in hdr
    # The verifier PARSES the hostile heading correctly: the check name comes back clean
    # (wrapping backticks peeled, interior triple-backtick defused) — no desync.
    vr = _load_vr()
    wf, check, _body = vr._pole_header_sections(md)[0]
    assert wf == "pipeline.yml"
    assert "`" not in check and "echo pwned" in check
    # ...and the defense-in-depth balance check passes on the (now safe) report.
    assert vr.check_fences_balanced(md).ok


def test_hostile_evidence_line_does_not_break_its_fence():
    # Drive the verbatim-evidence sink end-to-end: an off-category leaf renders its captured log
    # lines inside a ```text fence. Inject a triple-backtick INTO the quoted log line.
    doc, logs = _nx_offcategory_doc()
    logs["Run Checks"] = (
        "> nx run-many --target=lint\n"
        "$ eslint . ```echo pwned``` --ext .ts\n"     # this line becomes verbatim evidence
        "> nx affected --target=test\n"
        "PASS  src/foo.spec.ts (312 tests) 41200ms\n")
    md = bp.render(doc, logs)
    assert "Secondary observation" in md and "eslint" in md   # the evidence fence rendered
    assert "```echo pwned```" not in md                       # ...with the ``` defused
    assert len(re.findall(r"(?m)^`{3,}", md)) % 2 == 0        # fences stay balanced
    assert _load_vr().check_fences_balanced(md).ok


def test_hostile_workflow_filename_does_not_break_the_pole_heading_span():
    # The pole heading wraps the workflow FILENAME as an inline code span. A workflow file is
    # repo-controlled text (it's the `.github/workflows/` dir listing), and a filename may
    # legally carry a backtick — a SINGLE backtick in `` `c`i.yml` `` closes the span early,
    # a >=3-run breaks out entirely. `_safe_span` must map every backtick so the filename can't.
    doc = _doc_one_pole()
    pole = doc["pr_critical_path"]["poles"][0]
    pole["workflow_file"] = ".github/workflows/c`i```.yml"   # single AND triple backtick
    md = bp.render(doc, {}, {}, {"pipeline": "https://github.com/o/r/actions/runs/123"},
                   "2026-06-08")
    hdr = next(l for l in md.splitlines() if "Long pole 1:" in l)
    # The safe form is exactly what `_safe_span(_wf_base(...))` produces — every backtick apostrophe-
    # mapped — and the raw breakout form must NOT survive anywhere in the heading.
    assert bp._safe_span(bp._wf_base(pole["workflow_file"])) in hdr
    assert "`c`i" not in hdr and "```" not in hdr
    # The verifier parses the (now safe) heading and the report's fences stay balanced.
    vr = _load_vr()
    assert vr.check_fences_balanced(md).ok
    assert vr._pole_header_sections(md)[0][0] == "c'i'''.yml"   # wf comes back apostrophe-mapped


def test_hostile_workflow_filename_keeps_the_gate_claim_verbatim_in_the_report():
    # Regression: the pole-gate-prompt Claim embeds the workflow filename and is emitted into the
    # agent prompt through `_fence_body` (per-line `_fence_safe`). A `Claim.rendered` MUST be byte-
    # identical to the report text (`check_claims_cover_framing_vocabulary` binds by exact-span
    # containment), so the manifest has to carry the SAME `_fence_safe(wf)` the fence emission
    # produces. A >=3-backtick filename that defused on emit but not in the claim silently false-
    # failed the framing-coverage guard. Build a doc whose gate line renders the workflow clause.
    check = "web-tests"
    wf = ".github/workflows/c```i.yml"                         # triple-backtick filename
    checks = [{"name": check, "p50_s": 900.0, "present_on": 10, "workflow_file": wf},
              {"name": "lint", "p50_s": 40.0, "present_on": 10, "workflow_file": wf}]
    pops = [[0.1, [[check, 900.0], ["lint", 40.0]]] for _ in range(10)]
    doc = {"pr_critical_path": {
        "critical_path_check": check, "critical_path_s": 900.0, "checks": checks,
        "check_present_n_pr": 10, "populations": pops,
        "poles": [{"check": check, "p50_s": 900.0, "workflow_file": wf, "job": check,
                   "dominant_step": "run tests", "dominant_p50_s": 850.0}]},
        "findings": [{"id": "f1", "pattern": "OPT24", "severity": "HIGH",
                      "title": "Long Test Job Without Sharding", "workflow_file": wf,
                      "affected_jobs": [check], "runner_min_saving": 0}]}
    md = bp.render(doc)
    cs = bp._LAST_CLAIMS
    # The gate claim with the workflow clause exists, and EVERY claim's rendered sentence appears
    # verbatim in the report — the invariant the framing-coverage guard enforces.
    assert any("its workflow" in c.rendered for c in cs.claims), "gate workflow clause did not render"
    for c in cs.claims:
        assert c.rendered in md, f"claim rendered not byte-identical in the report: {c.rendered!r}"
    # And the real guard passes end-to-end (manifest written next to the report, as main() does).
    import tempfile
    d = Path(tempfile.mkdtemp())
    rp = d / "report-2026-06-08.md"
    rp.write_text(md, encoding="utf-8")
    (d / (rp.name + ".claims.json")).write_text(json.dumps(cs.to_json()), encoding="utf-8")
    vr = _load_vr()
    assert vr.check_claims_cover_framing_vocabulary(md, rp).ok


def test_single_backtick_name_stays_symmetric_between_heading_and_verifier():
    # #108 bot review (P1): _safe_span maps EVERY backtick in the heading, so the
    # verifier's name normalizer must map them too or a 1-2-backtick check name
    # diverges between the rendered heading and the comparator. Both sides now drop
    # all backticks from NAMES (apostrophe-mapped); clean names stay byte-identical.
    assert bp._clean_label("run `unit` tests") == "run 'unit' tests"
    assert bp._clean_label("x ``y`` z") == "x ''y'' z"
    assert bp._clean_label("guard shard 2/4") == "guard shard 2/4"


# ── Aggregation-gate poles (issue #1) ─────────────────────────────────────────────────
# A success-aggregation gate is the trivial job that exists ONLY to `needs:` a set of real
# jobs so ONE check can be the single required status check (vercel/next.js `thank you,
# build`: job `buildPassed`, `needs: [deploy-target, build, build-wasm, build-native]`, body
# `run: exit 1`, P50 3s, required). Crowning it by frequency is CORRECT data; drilling it and
# prompting "capture timing, then optimize this step" is inert advice over a 3-second no-op.
# The renderer must instead tell the honest upstream story and point at the slowest member.

_AGG_DEPLOY = ".github/workflows/deploy.yml"
_AGG_CI = ".github/workflows/ci.yml"


def _agg_gate_doc() -> dict:
    """Two workflows. `deploy.yml` carries the SINK (`thank you, build`, 3s, terminal, its
    transitive `needs:` covering every non-terminal job) plus a conditional peer sink
    (`Potentially publish release`, terminal and uncovered — the `publishRelease` shape).
    `ci.yml` carries the near-miss: a real 3s `lint` job that `needs:` nothing."""
    return {
        "repo": "acme/site", "repo_visibility": "public",
        "scanned_at": "2026-07-28T00:00:00Z", "commit_sha": "09e8243",
        "skill_commit_sha": "dd51d85", "findings": [],
        "data_sources": {"runs_sampled": 20, "jobs_sampled": 60, "workflows_analyzed": 2},
        "workflow_job_graph": {
            _AGG_DEPLOY: {
                "target": {"name": "deploy-target", "needs": []},
                "build": {"name": "build", "needs": ["target"]},
                "matrixgen": {"name": "generate-native-matrix", "needs": ["target"]},
                "native": {"name": "stable - ${{ matrix.target }}", "matrix": True,
                           "needs": ["target", "matrixgen"]},
                "publish": {"name": "Potentially publish release",
                            "needs": ["target", "build", "native"]},
                "gate": {"name": "thank you, build", "needs": ["target", "build", "native"]},
            },
            _AGG_CI: {
                "lint": {"name": "lint", "needs": []},
                "unit": {"name": "unit", "needs": []},
            },
        },
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "check_present_n_pr": 20, "critical_path_check": "unit",
            "poles": [
                {"check": "unit", "p50_s": 600.0, "workflow_file": _AGG_CI, "job": "unit",
                 "dominant_step": "Run tests", "dominant_p50_s": 480.0,
                 "steps": [{"step": "Checkout", "category": "setup", "p50_s": 20.0},
                           {"step": "Run tests", "category": "test", "p50_s": 480.0}]},
                {"check": "thank you, build", "p50_s": 3.0, "workflow_file": _AGG_DEPLOY,
                 "job": "gate", "timing_source": "pr_check_runs", "steps": []},
                {"check": "lint", "p50_s": 3.0, "workflow_file": _AGG_CI, "job": "lint",
                 "dominant_step": "Run eslint", "dominant_p50_s": 2.0,
                 "steps": [{"step": "Run eslint", "category": "lint", "p50_s": 2.0}]},
            ],
            "checks": [
                {"name": "unit", "p50_s": 600.0, "present_on": 20, "workflow_file": _AGG_CI},
                {"name": "stable - x86_64-linux", "p50_s": 355.0, "present_on": 20,
                 "workflow_file": _AGG_DEPLOY},
                {"name": "build", "p50_s": 240.0, "present_on": 20,
                 "workflow_file": _AGG_DEPLOY},
                {"name": "deploy-target", "p50_s": 19.0, "present_on": 20,
                 "workflow_file": _AGG_DEPLOY},
                {"name": "thank you, build", "p50_s": 3.0, "present_on": 20,
                 "workflow_file": _AGG_DEPLOY},
                {"name": "lint", "p50_s": 3.0, "present_on": 20, "workflow_file": _AGG_CI},
            ],
            "populations": [],
        },
    }


def _pole_section(md: str, check: str) -> str:
    """The `## … Long pole N: … ▸ <check>` section body for `check`."""
    i = md.index(f"▸ `{check}`")
    i = md.rindex("\n## ", 0, i)
    j = md.find("\n## ", i + 4)
    return md[i:j if j != -1 else len(md)]


def test_aggregation_gate_pole_tells_the_upstream_story_not_a_prompt():
    # The whole fix (issue #1): the `needs:`-everything 3s sink renders the honest role line,
    # names its slowest MEASURED upstream member, points the reader there — and carries NO
    # drill and NO "optimize this step" agent prompt.
    md = bp.render(_agg_gate_doc(), {}, {}, {}, "2026-07-28T00:00:00Z", {})
    sec = _pole_section(md, "thank you, build")
    assert "**Aggregation gate" in sec
    assert "it exists to be the single required check" in sec
    assert "runs no work of its own" in sec
    # The slowest upstream is the matrix leg (355s), resolved through the `${{ }}` name
    # template — not the sink's own 3s and not the unrelated `unit` pole in the other workflow.
    assert "`stable - x86_64-linux` (~5m 55s)" in sec
    assert "**➡️ Where the wait actually is:**" in sec
    # The two inert artifacts are gone.
    assert "🤖 Prompt for your coding agent" not in sec
    assert "Level 2" not in sec
    assert "before optimizing" not in sec
    # The OTHER poles are untouched — each still drills and hands off.
    assert "🤖 Prompt for your coding agent" in _pole_section(md, "unit")


def test_aggregation_gate_pole_role_line_is_a_registered_claim():
    # Claims parity: the role line is a `pole_role_line` Claim whose rendered sentence appears
    # verbatim in the report, exactly like its neighbouring role lines.
    md = bp.render(_agg_gate_doc(), {}, {}, {}, "2026-07-28T00:00:00Z", {})
    cs = bp._LAST_CLAIMS
    agg = [c for c in cs.claims
           if c.kind == "pole_role_line" and "Aggregation gate" in c.rendered]
    assert len(agg) == 1
    assert agg[0].subject == "thank you, build"
    assert agg[0].fields["upstream_slowest"] == "stable - x86_64-linux"
    for c in cs.claims:
        assert c.rendered in md, f"claim not byte-identical in the report: {c.rendered!r}"


def test_aggregation_gate_near_miss_renders_byte_identically(monkeypatch):
    # A real 3s `lint` job with NO `needs:` coverage must keep today's rendering exactly —
    # duration alone never triggers the framing. Pinned byte-for-byte against the renderer
    # with detection disabled (the pre-fix behaviour).
    doc = _agg_gate_doc()
    after = _pole_section(bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {}), "lint")
    monkeypatch.setattr(bp, "_agg_gate_shape", lambda *a, **k: None)
    before = _pole_section(bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {}), "lint")
    assert after == before
    assert "**Aggregation gate" not in after
    assert "🤖 Prompt for your coding agent" in after


def test_aggregation_gate_yields_to_the_chain_member_framing(monkeypatch):
    # A sink that IS a modal-chain member keeps the chain-stage rendering (`thank you, next`
    # as stage 3/3): the chain model already frames it as serialized, and double-framing it
    # would contradict that. Pinned byte-for-byte against detection-disabled rendering.
    doc = _agg_gate_doc()
    doc["pr_critical_path"]["chain_summary"] = {
        "modal_chain": ["build", "stable - x86_64-linux", "thank you, build"],
        "chain_p50_s": 598.0}
    after = _pole_section(bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {}),
                          "thank you, build")
    monkeypatch.setattr(bp, "_agg_gate_shape", lambda *a, **k: None)
    before = _pole_section(bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {}),
                           "thank you, build")
    assert after == before
    assert "Stage 3/3 of the" in after and "gate chain" in after
    assert "**Aggregation gate" not in after


def test_agg_gate_shape_structural_conditions():
    # The shape helper itself: each condition is load-bearing.
    doc = _agg_gate_doc()
    graph = doc["workflow_job_graph"]
    checks = doc["pr_critical_path"]["checks"]
    poles = {p["check"]: p for p in doc["pr_critical_path"]["poles"]}
    hit = bp._agg_gate_shape(poles["thank you, build"], graph, checks)
    assert hit and hit["job_id"] == "gate"
    # The closure walks TRANSITIVELY: `matrixgen` is reached via `native`, so the
    # conditional peer sink `publish` is the only uncovered job — and it is terminal.
    assert hit["upstream"] == ["build", "matrixgen", "native", "target"]
    assert bp._clean_label(bp._check_name(hit["slowest"])) == "stable - x86_64-linux"
    # (a) duration: a job doing real work never matches on structure alone.
    heavy = dict(poles["thank you, build"], p50_s=180.0)
    assert bp._agg_gate_shape(heavy, graph, checks) is None
    # (b) coverage: a non-terminal job outside the closure → not an aggregation sink.
    gapped = json.loads(json.dumps(graph))
    gapped[_AGG_DEPLOY]["extra"] = {"name": "extra", "needs": ["target"]}
    gapped[_AGG_DEPLOY]["afterextra"] = {"name": "afterextra", "needs": ["extra"]}
    assert bp._agg_gate_shape(poles["thank you, build"], gapped, checks) is None
    # (b) a single-parent stage (a chain member's shape) needs >= 2 upstream jobs.
    thin = json.loads(json.dumps(graph))
    thin[_AGG_DEPLOY]["gate"]["needs"] = ["build"]
    assert bp._agg_gate_shape(poles["thank you, build"], thin, checks) is None
    # (b) the near-miss lint job: trivial, but `needs:` nothing.
    assert bp._agg_gate_shape(poles["lint"], graph, checks) is None
    # (c) step data DISQUALIFIES when it shows real work.
    busy = dict(poles["thank you, build"],
                steps=[{"step": "Run the suite", "p50_s": 400.0}])
    assert bp._agg_gate_shape(busy, graph, checks) is None
    # (d) no measured upstream check → nothing honest to point at.
    assert bp._agg_gate_shape(poles["thank you, build"], graph,
                              [c for c in checks
                               if c["workflow_file"] != _AGG_DEPLOY]) is None
    # No scanned graph for the workflow → the shape is unknowable, never guessed.
    assert bp._agg_gate_shape(poles["thank you, build"], {}, checks) is None


def test_aggregation_gate_discloses_both_thin_data_caveats():
    # Both caveats are independent and BOTH must render: an unmeasured upstream member (the
    # named "slowest" could be beaten by one with no timing) AND the absence of per-step data
    # (which is what "runs no work of its own" rests on, together with the measured P50). An
    # `elif` dropped the second exactly when the sample was thinnest.
    md = bp.render(_agg_gate_doc(), {}, {}, {}, "2026-07-28T00:00:00Z", {})
    sec = _pole_section(md, "thank you, build")
    assert "had no measured check timing in this sample" in sec
    assert "No per-step data was captured for this check" in sec


def test_aggregation_gate_pointer_links_the_upstream_members_own_pole():
    # When the slowest upstream member IS itself a rendered pole, the pointer links to THAT
    # pole's anchor — matched on the raw check name + workflow file (the spine's identity),
    # never on the cleaned display label, which can fold two distinct checks together.
    doc = _agg_gate_doc()
    doc["pr_critical_path"]["poles"].append(
        {"check": "stable - x86_64-linux", "p50_s": 355.0, "workflow_file": _AGG_DEPLOY,
         "job": "native", "dominant_step": "cargo build", "dominant_p50_s": 300.0,
         "steps": [{"step": "cargo build", "category": "build", "p50_s": 300.0}]})
    md = bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {})
    sec = _pole_section(md, "thank you, build")
    n = int(re.search(r"## .*Long pole (\d+): .*▸ `stable - x86_64-linux`", md).group(1))
    assert f"**➡️ Where the wait actually is:** [Long pole {n}](#pole-{n}) drills " \
           f"`stable - x86_64-linux` (5m 55s)" in sec
