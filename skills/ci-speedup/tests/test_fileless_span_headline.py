"""Regression tests for issue #12 — a fileless/managed status check's PR-lifetime span must
NEVER crown the merge-wait headline; it is DISCLOSED as PR-lifetime status-gating latency instead.

The class (electron/electron, round-3): the headline merge-wait read ~8 days (11659m), crowned by
`Backport Labels Added` / `faraday/cage` — fileless app/label status checks whose check-run span
(`_duration_s(started_at, completed_at)`) measures PR-LIFETIME human/bot label-gating latency, not
CI wall-clock. `_pole_caps` builds de-inflation caps only from sampled jobs, so a check with NO
sampled job is never capped and its raw span flows into `critical_path_s` /
`chain_summary.makespan_p50_s` and crowns the headline over file-backed poles tracing <1% of it.

The fix (`collect_runs._partition_fileless_checks`): exclude non-job-groundable checks from the
crowning basis at the data layer, stamp them in `fileless_status_checks`, and DISCLOSE the slowest
one in the report. `blocking_path` renders the disclosure; `verify_report`'s
`check_headline_basis_excludes_fileless` re-derives the disjointness + disclosure bind.

Run: pytest -v skills/ci-speedup/tests/test_fileless_span_headline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_TESTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402
import verify_report as vr  # noqa: E402

# electron: ~11659 minutes (8 days) of PR-lifetime label-gating latency read as a check-run span.
_EIGHT_DAY_S = 11659 * 60  # 699540.0


# --- Engine: the partition is the fix seam (revert reds this) ------------------------------------

def test_partition_excludes_fileless_label_gate_from_the_crowning_basis():
    """The electron shape: a fileless `Backport Labels Added` label gate carrying an 8-day span
    beside job-backed checks. The partition drops it from the job-groundable basis; the crown
    re-derived from that basis is the slowest REAL job, not the queue-inflated 8-day span."""
    pr_check_p50 = {
        "Backport Labels Added": float(_EIGHT_DAY_S),  # fileless: PR-lifetime label latency
        "CI / test": 600.0,                            # job-backed
        "CI / lint": 120.0,                            # job-backed
    }
    timing_source = {
        "Backport Labels Added": "pr_check_runs",      # no sampled workflow job
        "CI / test": "workflow_jobs",
        "CI / lint": "workflow_jobs",
    }
    groundable, fileless = cr._partition_fileless_checks(
        pr_check_p50, timing_source, crit_by_wf={}, job_graph={})
    assert set(groundable) == {"CI / test", "CI / lint"}
    assert set(fileless) == {"Backport Labels Added"}

    # The crown re-derived from the job-groundable basis is the slowest REAL job (600s), never the
    # 8-day fileless span. (n_pr=1 < _RARE_PRESENCE_MIN_PR, so plain p50 order — no demotion.)
    ranked, _present, _n, _freq = cr._rank_spine_present_first(
        groundable, per_sha_checks=[{"CI / test": 600.0, "CI / lint": 120.0}],
        req_names=frozenset())
    assert ranked[0][0] == "CI / test"
    assert ranked[0][1] == 600.0

    # Counter-proof of the BUG: ranking the UN-partitioned basis (pre-fix) crowns the fileless span.
    ranked_pre, *_ = cr._rank_spine_present_first(
        pr_check_p50, per_sha_checks=[dict(pr_check_p50)], req_names=frozenset())
    assert ranked_pre[0][0] == "Backport Labels Added"


def test_partition_keeps_a_triage_skipped_but_file_backed_check_in_the_basis():
    """A check with a `pr_check_runs` source but a workflow FILE (the scanned job graph maps it) is
    real CI compute the crown-recovery pass can still recover — it stays in the crowning basis. Only
    a check with NO workflow anywhere (a bot/app/label gate) is fileless."""
    job_graph = {".github/workflows/ci.yml": {"validate": {"name": "validate", "matrix": False}}}
    pr_check_p50 = {"validate": 200.0, "Socket Security": 30.0}
    timing_source = {"validate": "pr_check_runs", "Socket Security": "pr_check_runs"}
    groundable, fileless = cr._partition_fileless_checks(
        pr_check_p50, timing_source, crit_by_wf={}, job_graph=job_graph)
    assert "validate" in groundable          # scanned-graph mapped -> job-groundable (recoverable)
    assert set(fileless) == {"Socket Security"}  # no workflow anywhere -> genuinely fileless


def test_ambiguous_cross_workflow_gate_stays_in_the_crowning_basis_not_fileless():
    """Issue #59 blast radius: a monorepo declares a same-named heavy job (`Build`) in TWO PR
    workflows, and that check IS the real merge gate (the top pole). `_map_check_to_job` now BAILS
    to None on the file attribution (it can't pick pkg-a vs pkg-b to drill), and the scanned graph
    likewise refuses cross-file ambiguity — so BOTH `_partition_fileless_checks` fallbacks miss it.
    The regression to guard: the spine must still GROUND its crown magnitude on the real job p50
    (`_check_grounded_job_p50`, the slowest same-named job a PR waits on) and stamp it `workflow_jobs`
    so the partition keeps it in the crowning basis. Without that, a REAL file-backed merge gate would
    be silently dropped into the fileless bucket, uncrowned and mislabelled PR-lifetime status-gating
    latency — worse than the original mis-attribution bug."""
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {"job_p50": {"Build": 120.0}},
        ".github/workflows/pkg-b.yml": {"job_p50": {"Build": 900.0}},  # slower — a PR waits on this
    }
    job_graph = {
        ".github/workflows/pkg-a.yml": {"build": {"name": "Build", "needs": [], "reusable": False}},
        ".github/workflows/pkg-b.yml": {"build": {"name": "Build", "needs": [], "reusable": False}},
    }
    # File attribution is genuinely ambiguous -> mapper + scanned graph both bail (unchanged).
    assert cr._map_check_to_job("Build", crit_by_wf, require_developer_timing=True) is None
    assert cr._check_to_job_node_scanned("Build", job_graph) is None
    # ...but the crown MAGNITUDE is unambiguous: the slowest same-named job (900s), NOT None.
    assert cr._check_grounded_job_p50(
        "Build", crit_by_wf, require_developer_timing=True) == 900.0
    # The spine stamps that grounded p50 as `workflow_jobs`, so the partition keeps `Build` in the
    # crowning basis and it crowns as the merge gate — never dropped to the fileless bucket.
    pr_check_p50 = {"Build": 900.0, "lint": 30.0}
    timing_source = {"Build": "workflow_jobs", "lint": "workflow_jobs"}
    groundable, fileless = cr._partition_fileless_checks(
        pr_check_p50, timing_source, crit_by_wf, job_graph)
    assert "Build" in groundable and "Build" not in fileless
    ranked, *_ = cr._rank_spine_present_first(
        groundable, per_sha_checks=[{"Build": 900.0, "lint": 30.0}],
        req_names=frozenset())
    assert ranked[0][0] == "Build"  # the real gate crowns, not `lint`
    # Counter-proof of the regression: had the spine left it a bare check-run span (mapper bailed ->
    # `pr_check_runs`, no scanned file), the partition would have dropped the real gate to fileless.
    _g2, f2 = cr._partition_fileless_checks(
        {"Build": 900.0, "lint": 30.0},
        {"Build": "pr_check_runs", "lint": "workflow_jobs"}, crit_by_wf, job_graph)
    assert "Build" in f2  # the bug shape the grounding prevents
    # A genuinely fileless bot check has no producing job -> grounded magnitude stays None.
    assert cr._check_grounded_job_p50(
        "Socket Security", crit_by_wf, require_developer_timing=True) is None
    # Subset-tier ambiguity grounds too: `@x/pkg Build` scope-prefixes a `Build` job in BOTH
    # workflows (mapper bails on the file, but the crown magnitude is still the slowest, 900s).
    assert cr._map_check_to_job(
        "@x/pkg Build", crit_by_wf, require_developer_timing=True) is None
    assert cr._check_grounded_job_p50(
        "@x/pkg Build", crit_by_wf, require_developer_timing=True) == 900.0
    # Boundary of the rescue: when EVERY colliding workflow is all-events (push/schedule only, no
    # developer-facing sample), the developer-timing filter leaves zero candidates, so the grounding
    # declines (None) just like the mapper — an all-events collision is NOT rescued into the crowning
    # basis (unchanged from before this fix; only developer-timed gates are grounded).
    all_events = {
        ".github/workflows/pkg-a.yml": {"event_scope": "all-events", "job_p50": {"Build": 120.0}},
        ".github/workflows/pkg-b.yml": {"event_scope": "all-events", "job_p50": {"Build": 900.0}},
    }
    assert cr._check_grounded_job_p50(
        "Build", all_events, require_developer_timing=True) is None
    # ...but without the developer-timing filter the magnitude is still resolvable (the max).
    assert cr._check_grounded_job_p50("Build", all_events) == 900.0


def test_partition_all_fileless_leaves_an_empty_crowning_basis():
    """A degenerate repo whose every gating check is a bot/app/label gate: the crowning basis is
    empty (nothing job-groundable to crown), the whole set falls to the fileless bucket."""
    pr_check_p50 = {"CLA bot": 500.0, "Backport Labels Added": float(_EIGHT_DAY_S)}
    timing_source = {"CLA bot": "pr_check_runs", "Backport Labels Added": "pr_check_runs"}
    groundable, fileless = cr._partition_fileless_checks(
        pr_check_p50, timing_source, crit_by_wf={}, job_graph={})
    assert groundable == {}
    assert set(fileless) == {"CLA bot", "Backport Labels Added"}


# --- Artifact-level: render + verify the disclosure and the class invariant ----------------------

def _base_doc() -> dict:
    return {
        "repo": "electron/electron",
        "repo_visibility": "public",
        "scanned_at": "2026-07-17T00:00:00Z",
        "skill_commit_sha": "7039302",
        "commit_sha": "abcdef1234567890",
        "findings": [],
    }


def _fixed_doc() -> dict:
    """A findings doc as the POST-FIX engine produces it: the fileless label gate is EXCLUDED from
    `checks[]` / `critical_path_check` and STAMPED in `fileless_status_checks`; the crown is the
    job-backed `CI / test`."""
    doc = _base_doc()
    doc["pr_critical_path"] = {
        "critical_path_check": "CI / test",
        "critical_path_s": 600.0,
        "sampled_pr_count": 12,
        "sample_target": 12,
        "check_present_n_pr": 12,
        "checks": [
            {"name": "CI / test", "workflow_file": ".github/workflows/ci.yml",
             "p50_s": 600.0, "present_on": 12, "pole_n": 12},
            {"name": "CI / lint", "workflow_file": ".github/workflows/ci.yml",
             "p50_s": 120.0, "present_on": 12, "pole_n": 0},
        ],
        "poles": [{"check": "CI / test", "job": "test",
                   "workflow_file": ".github/workflows/ci.yml", "p50_s": 600.0}],
        "fileless_status_checks": [
            {"name": "Backport Labels Added", "span_s": float(_EIGHT_DAY_S),
             "basis": "pr_lifetime_status_gating_latency"},
            {"name": "faraday/cage", "span_s": 90000.0,
             "basis": "pr_lifetime_status_gating_latency"},
        ],
        "all_checks_fileless": False,
    }
    return doc


def _write(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_report_crowns_the_job_backed_check_and_discloses_the_fileless_gate(tmp_path):
    doc = _fixed_doc()
    report = bp.render(doc)
    # The headline crowns the job-backed check, NOT the 8-day fileless span.
    assert "`CI / test`" in report or "CI / test" in report
    assert "8 days" not in report.lower()
    # The fileless gate is DISCLOSED as PR-lifetime status-gating latency, near the headline.
    assert "PR-lifetime status-gating latency" in report
    assert "Backport Labels Added" in report
    # And the class invariant passes on the fixed artifact.
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert chk.ok and not chk.skipped, chk.detail


def test_class_check_fails_when_a_fileless_span_leaks_into_the_crown(tmp_path):
    """The FAIL discriminator: a doc where the fileless label gate leaked back into the crowning
    basis (critical_path_check + checks[]) while still being stamped fileless — exactly the pre-fix
    electron artifact. The class check must FAIL."""
    doc = _fixed_doc()
    cp = doc["pr_critical_path"]
    cp["critical_path_check"] = "Backport Labels Added"
    cp["critical_path_s"] = float(_EIGHT_DAY_S)
    cp["checks"].insert(0, {"name": "Backport Labels Added",
                            "p50_s": float(_EIGHT_DAY_S), "present_on": 12, "pole_n": 12})
    report = bp.render(doc)
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert not chk.ok and not chk.skipped
    assert "crowns the merge-wait basis" in chk.detail


def test_class_check_fails_when_the_disclosure_is_missing(tmp_path):
    """The disclosure-bind FAIL: fileless checks were excluded but the rendered report carries no
    PR-lifetime disclosure — an excluded gate silently dropped."""
    doc = _fixed_doc()
    fp = _write(tmp_path, doc)
    report_no_disclosure = (
        "# electron/electron — why is the merge slow?\n\n"
        "> **Bottom line.** A typical PR waits **10m 00s** for all checks to finish.\n")
    chk = vr.check_headline_basis_excludes_fileless(report_no_disclosure, fp, None)
    assert not chk.ok and not chk.skipped
    assert "no PR-lifetime-status-gating-latency disclosure" in chk.detail


def test_class_check_skips_on_a_legacy_artifact_without_the_stamp(tmp_path):
    """A pre-#12 artifact has no `fileless_status_checks` key — nothing to bind, so SKIP (older
    committed reports keep verifying exactly as before)."""
    doc = _fixed_doc()
    del doc["pr_critical_path"]["fileless_status_checks"]
    del doc["pr_critical_path"]["all_checks_fileless"]
    report = bp.render(doc)
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert chk.skipped


def test_class_check_passes_when_no_fileless_checks_present(tmp_path):
    """The new engine stamps an EMPTY `fileless_status_checks` on a repo with no fileless gates —
    the check passes (nothing owed), never a false FAIL."""
    doc = _fixed_doc()
    doc["pr_critical_path"]["fileless_status_checks"] = []
    report = bp.render(doc)
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert chk.ok and not chk.skipped


def test_degenerate_all_fileless_report_says_so(tmp_path):
    """The all-fileless degenerate arm: no job-groundable check exists to crown. The report says so
    honestly (naming the slowest fileless gate as PR-lifetime latency) rather than crowning garbage,
    and the class check passes."""
    doc = _base_doc()
    doc["pr_critical_path"] = {
        "critical_path_check": None,
        "critical_path_s": 0.0,
        "sampled_pr_count": 8,
        "check_present_n_pr": 8,
        "checks": [],
        "poles": [],
        "fileless_status_checks": [
            {"name": "Backport Labels Added", "span_s": float(_EIGHT_DAY_S),
             "basis": "pr_lifetime_status_gating_latency"},
            {"name": "CLA bot", "span_s": 4000.0,
             "basis": "pr_lifetime_status_gating_latency"},
        ],
        "all_checks_fileless": True,
    }
    report = bp.render(doc)
    assert "every gating check here is fileless" in report.lower()
    assert "PR-lifetime status-gating latency" in report
    assert "Backport Labels Added" in report
    # The degenerate early-return bypasses `render`'s terminal `_strip_emdashes`, so it must scrub
    # its own prose — no typographic dash may survive (verify_report's ASCII-hyphens-only invariant).
    assert not any(g in report for g in ("—", "–", "―", "−")), \
        "a typographic dash survived the degenerate all-fileless render boundary"
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert chk.ok and not chk.skipped, chk.detail


# --- Additional coverage (PR #40 review hardening) -----------------------------------------------

def test_partition_grounds_a_check_via_the_sampled_timing_mapper():
    """Arm B of the three-way OR: a check whose source is `pr_check_runs` (NOT `workflow_jobs`) and
    which no scanned graph maps, but which `_map_check_to_job(..., require_developer_timing=True)`
    resolves against `crit_by_wf`, stays JOB-GROUNDABLE — it is real developer-timed CI compute, not
    a fileless gate. Guards the arm (never the deciding path in the other partition tests, which all
    pass `crit_by_wf={}`) that keeps a matrix/reusable-job check like `@scope/pkg Integration Test`
    out of the fileless bucket — the mirror-image silent drop (a real pole vanishing) of the bug."""
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"Integration Test": 900.0}}}
    pr_check_p50 = {"@scope/pkg Integration Test": 900.0, "CLA bot": 30.0}
    timing_source = {"@scope/pkg Integration Test": "pr_check_runs", "CLA bot": "pr_check_runs"}
    groundable, fileless = cr._partition_fileless_checks(
        pr_check_p50, timing_source, crit_by_wf=crit_by_wf, job_graph={})
    assert "@scope/pkg Integration Test" in groundable   # arm B (sampled-timing mapper) grounds it
    assert set(fileless) == {"CLA bot"}                   # no workflow anywhere -> fileless


def test_class_check_fails_when_a_fileless_span_leaks_into_only_the_poles(tmp_path):
    """Disjointness FAIL for the `poles[]` slot ALONE (the crown + checks[] stay clean). A stamped
    fileless name appearing only as a drilled long pole still means an excluded PR-lifetime span
    leaked back into the crowned set — the class check must FAIL, not pass because the headline
    slot happened to be clean."""
    doc = _fixed_doc()
    doc["pr_critical_path"]["poles"].append(
        {"check": "Backport Labels Added", "job": "n/a",
         "workflow_file": ".github/workflows/none.yml", "p50_s": float(_EIGHT_DAY_S)})
    report = bp.render(doc)
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert not chk.ok and not chk.skipped
    assert "crowns the merge-wait basis" in chk.detail and "poles[]" in chk.detail


def test_class_check_fails_when_a_fileless_span_leaks_into_only_the_modal_chain(tmp_path):
    """Disjointness FAIL for the `modal_chain` slot ALONE — the limb that stays dormant in every
    other test (no fixture sets `chain_facts`). A modal gate chain whose members include a stamped
    fileless check would let a chain-sum headline inflate on a PR-lifetime span; the check must
    catch it. Populates `chain_facts` so `_vr_modal_chain` yields a >=2-member modal chain."""
    doc = _fixed_doc()
    doc["pr_critical_path"]["chain_facts"] = [
        {"sha": "a", "chain": ["CI / test", "Backport Labels Added"], "chain_s": 700.0},
        {"sha": "b", "chain": ["CI / test", "Backport Labels Added"], "chain_s": 700.0},
    ]
    report = bp.render(doc)
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert not chk.ok and not chk.skipped
    assert "crowns the merge-wait basis" in chk.detail and "modal_chain" in chk.detail


def test_class_check_fails_when_the_disclosed_span_drifts_from_the_stamp(tmp_path):
    """The SECOND disclosure FAIL mode (the disclosure<->stamp span bind): the report DOES carry the
    PR-lifetime marker, but the slowest stamped fileless check's span is NOT the one disclosed — the
    rendered disclosure drifted from the stamped `fileless_status_checks` list. Distinct from the
    missing-marker mode; catches a report that discloses a different gate/span than the engine
    stamped."""
    doc = _fixed_doc()  # slowest stamped is `Backport Labels Added` at 11659m
    fp = _write(tmp_path, doc)
    report_drift = (
        "# electron/electron — why is the merge slow?\n\n"
        "> **Fileless status gate (disclosed, not headlined).** `faraday/cage` shows ~1500m 00s, "
        "but that span is PR-lifetime status-gating latency - a bot/label/external-app gate.\n")
    chk = vr.check_headline_basis_excludes_fileless(report_drift, fp, None)
    assert not chk.ok and not chk.skipped
    assert "drifted from the stamped" in chk.detail


def test_static_only_report_still_discloses_and_enforces_the_fileless_gate(tmp_path):
    """Regression lock for the silent-drop (PR #40 review): an all-fileless repo that ALSO has a
    static hygiene finding renders via the STATIC-ONLY body, not `render`'s degenerate arm. The
    fileless disclosure must ride along (never silently dropped behind the static short-circuit),
    and the class check must ENFORCE the bind there (not blanket-skip static-only), so a dropped
    disclosure fails rather than ships green."""
    doc = _base_doc()
    doc["findings"] = [{"pattern": "CACHE1", "severity": "medium",
                        "workflow_file": ".github/workflows/push.yml",
                        "title": "Cache the dependency install",
                        "evidence": "no cache configured", "runner_min_saving": 40.0}]
    doc["pr_critical_path"] = {
        "critical_path_check": None, "critical_path_s": 0.0,
        "sampled_pr_count": 8, "sample_target": 8, "check_present_n_pr": 8,
        "checks": [], "poles": [],
        "fileless_status_checks": [
            {"name": "Backport Labels Added", "span_s": float(_EIGHT_DAY_S),
             "basis": "pr_lifetime_status_gating_latency"},
            {"name": "CLA bot", "span_s": 4000.0,
             "basis": "pr_lifetime_status_gating_latency"}],
        "all_checks_fileless": True}
    report = bp.render(doc)
    assert vr._is_static_only(report)                       # it IS the static-only path
    assert "PR-lifetime status-gating latency" in report    # disclosure rode along
    assert "Backport Labels Added" in report
    fp = _write(tmp_path, doc)
    chk = vr.check_headline_basis_excludes_fileless(report, fp, None)
    assert chk.ok and not chk.skipped, chk.detail           # ENFORCED, not skipped
