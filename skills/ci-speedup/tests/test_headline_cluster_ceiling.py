"""Issue #49 — the render-layer single door: the Bottom-line / headline lever consumes the STAMPED
per-finding cluster-aware ceilings, and the burial guard can see the flag on real artifacts.

Two coupled defects fixed here:
  A. `blocking_path.render`'s Bottom line re-computed a sibling-capped per-leg headroom and buried the
     larger cluster-floor lever (OPT73) in "Also noticed" (mastodon: ~36s headline over a stamped 627s
     lever; electron: ~5m37s over 2635s). It must now LEAD with the stamped `wall_clock_p50_s`.
  B. `collect_runs` never persisted `cluster_floor_lever` to findings.json, so the burial guard SKIPped
     on every real report. The flag is now stamped, the guard FAILs on the burial, and its no-flag path
     is a LOUD narrow skip.

Synthetic fixtures mirror BOTH live shapes (the public numbers cited in the issue — 627s / 2635s and the
15m20s / 84m49s waits) without any third-party data dump. The live bundles verified the same flip.

Run: pytest -v skills/ci-speedup/tests/test_headline_cluster_ceiling.py
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)

_VERIFY = Path(__file__).resolve().parent / "verify_report.py"


def _vr():
    # Load THIS skill's verify_report by path under a unique name (ci-secure ships one too, so a plain
    # `import verify_report` can bind the wrong module on the shared pytest pythonpath).
    spec = importlib.util.spec_from_file_location("ci_speedup_verify_report_cluster", _VERIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_speedup_verify_report_cluster"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Fixtures — the two live shapes, minimal + synthetic.
# --------------------------------------------------------------------------- #
def _doc(*, pole_name: str, pole_p50: float, wf: str, legs: list[str], step: str,
         cluster_wc: float, chain: list[str], chain_p50: float, chain_win: float,
         checks: list[tuple[str, float]], flag: bool = True) -> dict:
    """A minimal render doc: one drilled pole, a `needs:` chain (chain_active), and one credited
    OPT73 cluster-floor lever whose stamped ceiling (`cluster_wc`) exceeds the chain headroom."""
    finding = {
        "id": "f-cluster", "pattern": "OPT73",
        "title": "OPT73 - Shared step recurs across the cluster - fix once, lower the floor",
        "workflow_file": wf, "affected_jobs": list(legs),
        "evidence": (f"the `{step}` step is 91% of the slowest cluster job `{pole_name}` "
                     f"and recurs across {len(legs)} concurrent jobs - a cluster-floor lever"),
        "wall_clock_p50_s": cluster_wc, "runner_min_saving": 700.0,
        "tier": 1, "realization": "direct",
    }
    if flag:
        finding["cluster_floor_lever"] = True
    return {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300, "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": pole_name, "p50_s": pole_p50, "workflow_file": wf, "job": pole_name,
                "dominant_step": step, "dominant_p50_s": pole_p50 * 0.9,
                "steps": [{"step": step, "category": "test", "p50_s": pole_p50 * 0.9}],
            }],
            "checks": [{"name": n, "p50_s": p} for n, p in checks],
            "chain_summary": {
                "modal_chain": list(chain), "chain_p50_s": chain_p50,
                "chain_win_p50_s": chain_win, "runner_up_p50_s": 100.0,
                "modal_n": 18, "n": 20,
            },
        },
        "findings": [finding],
    }


def _mastodon_shape(flag: bool = True) -> dict:
    # ~36s chain headroom buries a stamped 627s cluster lever; 15m20s = 920s wait.
    return _doc(pole_name="test (3.4)", pole_p50=837.0, wf=".github/workflows/test-ruby.yml",
                legs=["test (3.4)", "test (.ruby-version)", "test (3.3)"],
                step="Run bin/flatware rspec", cluster_wc=627.0,
                chain=["build (production)", "test (3.4)"], chain_p50=920.0, chain_win=36.0,
                checks=[("test (3.4)", 837.0), ("test (3.3)", 800.0),
                        ("Elastic Search integration", 202.0), ("build (production)", 90.0)],
                flag=flag)


def _electron_shape(flag: bool = True) -> dict:
    # ~5m37s chain headroom buries a stamped 2635s cluster lever; 84m49s = 5089s wait.
    return _doc(pole_name="windows-x64 / build / build", pole_p50=3130.0, wf=".github/workflows/build.yml",
                legs=["windows-x64 / build / build", "windows-x86 / build / build",
                      "windows-arm64 / build / build"],
                step="Build Electron", cluster_wc=2635.0,
                chain=["checkout-windows", "windows-x64 / build / build", "GitHub Actions Completed"],
                chain_p50=5089.0, chain_win=337.0,
                checks=[("windows-x64 / build / build", 3130.0), ("macos-x64 / build / build", 2368.0),
                        ("checkout-windows", 200.0)],
                flag=flag)


def _bottom_line(md: str) -> str:
    return next((ln for ln in md.splitlines() if "**Bottom line.**" in ln), "")


# --------------------------------------------------------------------------- #
# A. Renderer — the headline leads with the stamped cluster ceiling.
# --------------------------------------------------------------------------- #
def test_mastodon_shape_headline_leads_with_the_cluster_ceiling_not_the_per_leg_win():
    bl = _bottom_line(bp.render(_mastodon_shape()))
    # Leads with the 627s (10m 27s) cluster lever, names the shared step + cluster, points at OPT73.
    assert "**~10m 27s**" in bl, bl
    assert "Run bin/flatware rspec" in bl
    assert "3 concurrent legs" in bl and "test-ruby.yml" in bl
    assert "Also noticed" in bl
    # The buried sibling-capped 36s is NOT the headline number.
    assert "**~36s**" not in bl


def test_electron_shape_headline_reflects_the_cluster_win_not_the_sibling_capped_headroom():
    bl = _bottom_line(bp.render(_electron_shape()))
    # 2635s = 43m 55s addressable against the non-sibling macos-x64 floor, not the 5m37s sibling cap.
    assert "**~43m 55s**" in bl, bl
    assert "**~5m 37s**" not in bl
    assert "84m 49s" in bl   # the total merge wait is unchanged


def test_sequential_cluster_headline_does_not_mislabel_the_legs_as_concurrent():
    # A `needs:`-chained cluster runs SEQUENTIALLY; the headline must not claim the legs
    # run "concurrent … in lockstep … a sibling leg gates" (the deepgram f19 mislabel the
    # appendix already forbids). collect_runs persists cluster_legs_concurrent=False for it.
    d = _mastodon_shape()
    d["findings"][0]["cluster_legs_concurrent"] = False
    bl = _bottom_line(bp.render(d))
    assert "**~10m 27s**" in bl                    # still leads with the stamped ceiling
    assert "concurrent" not in bl.lower(), bl      # never "concurrent" for a serial cluster
    assert "in lockstep" not in bl.lower(), bl
    assert "sequential" in bl.lower() and "compound" in bl.lower()


def test_concurrent_cluster_headline_default_keeps_the_matrix_lockstep_wording():
    # The common matrix case (marker True or absent) keeps the concurrent/lockstep framing.
    d = _mastodon_shape()
    d["findings"][0]["cluster_legs_concurrent"] = True
    bl = _bottom_line(bp.render(d))
    assert "3 concurrent legs" in bl and "in lockstep" in bl
    assert "sequential" not in bl.lower()


def test_legacy_artifact_without_the_flag_renders_byte_identically():
    # Backward compat: a findings.json predating the persisted flag keeps the OLD chain-headroom
    # bottom line (the renderer keys strictly on the stamped marker).
    bl_legacy = _bottom_line(bp.render(_mastodon_shape(flag=False)))
    assert "**~36s**" in bl_legacy
    assert "fixing the whole chain is worth up to" in bl_legacy
    assert "**~10m 27s**" not in bl_legacy


def test_a_bill_only_cluster_lever_does_not_hijack_the_headline():
    # A stamped cluster lever whose wall-clock floored to 0 (off-path / scheduled) is NOT credited,
    # so it must not headline — the chain headroom stands.
    d = _mastodon_shape()
    d["findings"][0]["wall_clock_p50_s"] = 0.0
    d["findings"][0]["tier"] = 2
    d["findings"][0]["realization"] = "none"
    bl = _bottom_line(bp.render(d))
    assert "**~36s**" in bl
    assert "**~10m 27s**" not in bl


# --------------------------------------------------------------------------- #
# C. Guard — the burial invariant + the loud-skip discriminators.
# --------------------------------------------------------------------------- #
def _write_findings(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_burial_guard_passes_when_the_headline_leads_with_the_ceiling(tmp_path):
    vr = _vr()
    doc = _mastodon_shape()
    md = bp.render(doc)
    fp = _write_findings(tmp_path, doc)
    c = vr.check_headline_consumes_stamped_cluster_ceiling(md, fp)
    assert c.ok and not c.skipped, c.detail
    assert "10m 27s" in c.detail


def test_burial_guard_FAILS_when_the_headline_buries_the_stamped_ceiling(tmp_path):
    # The exact live defect: flag stamped (so it's a real credited cluster lever) but the report still
    # renders the sibling-capped ~36s. The invariant must FAIL.
    vr = _vr()
    doc = _mastodon_shape()
    buried_report = _bottom_line(bp.render(_mastodon_shape(flag=False)))  # the ~36s bottom line
    fp = _write_findings(tmp_path, doc)
    c = vr.check_headline_consumes_stamped_cluster_ceiling(buried_report, fp)
    assert not c.ok and not c.skipped, c.detail
    assert "BURIED" in c.detail and "10m 27s" in c.detail


def test_burial_guard_SKIPS_loudly_on_a_legacy_artifact_without_the_flag(tmp_path):
    vr = _vr()
    doc = _mastodon_shape(flag=False)   # OPT73 present, no persisted marker
    md = bp.render(doc)
    fp = _write_findings(tmp_path, doc)
    c = vr.check_headline_consumes_stamped_cluster_ceiling(md, fp)
    assert c.skipped and c.ok, c.detail
    # Loud + narrow: names the count and WHY it can't check — never reads clean.
    assert "NONE carries the persisted" in c.detail
    assert "Coverage gap, not a clean pass" in c.detail


def test_burial_guard_skips_quietly_when_no_cluster_lever_exists(tmp_path):
    vr = _vr()
    doc = _mastodon_shape()
    doc["findings"] = [{"id": "x", "pattern": "OPT24", "wall_clock_p50_s": 100.0}]
    fp = _write_findings(tmp_path, doc)
    c = vr.check_headline_consumes_stamped_cluster_ceiling(bp.render(doc), fp)
    assert c.skipped and c.ok
    assert "no cluster-floor lever" in c.detail


def test_burial_guard_passes_when_the_only_cluster_lever_is_bill_only(tmp_path):
    vr = _vr()
    doc = _mastodon_shape()
    doc["findings"][0]["wall_clock_p50_s"] = 0.0
    fp = _write_findings(tmp_path, doc)
    c = vr.check_headline_consumes_stamped_cluster_ceiling(bp.render(doc), fp)
    assert c.ok and not c.skipped
    assert "nothing to headline" in c.detail


def test_escapes_sibling_guard_loud_skip_names_the_unbounded_cluster_levers(tmp_path):
    # The other L8 hole: when OPT73 levers exist but none carried a bounded measured-critical-path cap,
    # the checked==0 path must skip LOUDLY (name the count), not read clean.
    vr = _vr()
    doc = _mastodon_shape()
    # A derivation with a non-measured-critical-path bound → nothing bounded to check.
    doc["findings"][0]["wall_clock_derivation"] = [
        {"bound": "developer-facing", "from_s": 713.0, "to_s": 627.0, "reason": "scheduled trigger"}]
    fp = _write_findings(tmp_path, doc)
    c = vr.check_cluster_lever_ceiling_escapes_sibling(bp.render(doc), fp)
    assert c.skipped and c.ok
    assert "OPT73 cluster lever(s) present but none carried a bounded" in c.detail
    assert "Coverage gap, not a clean pass" in c.detail


# --------------------------------------------------------------------------- #
# D. Review-loop hardening — selection/guard agreement at the boundaries.
# --------------------------------------------------------------------------- #
def test_cluster_lever_within_half_second_of_existing_win_does_not_hijack(tmp_path):
    # A cluster ceiling that does NOT strictly beat the existing win by the 0.5s render margin must
    # not rewrite the headline, and the guard must still pass (the chain win >= the cluster ceiling).
    vr = _vr()
    d = _mastodon_shape()
    d["findings"][0]["wall_clock_p50_s"] = 36.3   # within 0.5s of the 36s chain win
    d["pr_critical_path"]["chain_summary"]["chain_win_p50_s"] = 36.0
    bl = _bottom_line(bp.render(d))
    assert "fixing the whole chain is worth up to" in bl and "**~36s**" in bl
    c = vr.check_headline_consumes_stamped_cluster_ceiling(bp.render(d), _write_findings(tmp_path, d))
    assert c.ok and not c.skipped, c.detail


def test_two_credited_cluster_levers_headline_and_guard_agree_on_the_largest(tmp_path):
    # Renderer max-selection and guard max-selection must pick the SAME finding.
    vr = _vr()
    d = _mastodon_shape()
    bigger = copy.deepcopy(d["findings"][0])
    bigger["id"] = "f-cluster-2"
    bigger["workflow_file"] = ".github/workflows/big.yml"
    bigger["affected_jobs"] = ["a", "b", "c"]
    bigger["evidence"] = "the `Big build` step is 90% of the slowest cluster job `a` and recurs across 3 concurrent jobs - a cluster-floor lever"
    bigger["wall_clock_p50_s"] = 900.0            # 15m 0s, beats the 627s sibling finding
    d["findings"].append(bigger)
    bl = _bottom_line(bp.render(d))
    assert "**~15m 00s**" in bl and "big.yml" in bl and "Big build" in bl
    assert "**~10m 27s**" not in bl               # the smaller cluster lever is not the headline
    c = vr.check_headline_consumes_stamped_cluster_ceiling(bp.render(d), _write_findings(tmp_path, d))
    assert c.ok and not c.skipped and "big.yml" in c.detail, c.detail


def test_cluster_leads_over_a_top_fixable_win_when_no_chain(tmp_path):
    # Exercises the non-chain `_existing_win` arm: chain inactive, a positive top-fixable win, and a
    # larger cluster ceiling — the cluster still leads.
    vr = _vr()
    d = _mastodon_shape()
    d["pr_critical_path"].pop("chain_summary", None)   # chain inactive → _existing_win = top_fixable win
    bl = _bottom_line(bp.render(d))
    assert "**~10m 27s**" in bl                          # the 627s cluster ceiling still leads
    c = vr.check_headline_consumes_stamped_cluster_ceiling(bp.render(d), _write_findings(tmp_path, d))
    assert c.ok and not c.skipped, c.detail


def test_burial_guard_skips_loudly_when_bottom_line_has_no_addressable_win(tmp_path):
    # A credited cluster lever exists but the report's Bottom line renders no `~<clock>` at all (a
    # legitimate no-single-win framing — competing-path / fallback). That is NOT the "Also noticed"
    # burial the guard targets (a MAX cluster ceiling always fires the render's cluster branch), so
    # the guard must SKIP LOUDLY, never spuriously FAIL and never read clean.
    vr = _vr()
    doc = _mastodon_shape()   # flagged + credited (627s)
    fp = _write_findings(tmp_path, doc)
    report = ("> **Bottom line.** A typical PR waits **15m 20s** for the chain to finish; a competing "
              "path of comparable length gates just behind it, so shortening the chain alone buys little.")
    c = vr.check_headline_consumes_stamped_cluster_ceiling(report, fp)
    assert c.skipped and c.ok, c.detail
    assert "no addressable" in c.detail.lower()
    assert "Coverage gap, not a clean pass" in c.detail


# --------------------------------------------------------------------------- #
# #56 — the CONVERSE guard: the crowned cluster lever must be presence-eligible.
# A minority-present workflow (2/20) must not crown the typical-PR bottom line.
# --------------------------------------------------------------------------- #
def _presence_doc(*, wf: str, legs: list[str], step: str, cluster_wc: float,
                  wf_pole: int, npop: int = 20, other_pole: int = 13,
                  flag: bool = True, stamp_pole_n: bool = True,
                  required: list[str] | None = None) -> dict:
    """A minimal doc for the presence-eligibility guard: one credited OPT73 cluster lever on
    workflow `wf`, whose checks carry a stamped `pole_n` summing to `wf_pole` (the workflow's gate
    count), plus a separate majority workflow so a typical-PR ceiling exists. `check_present_n_pr`
    is the sample floor the guard divides by."""
    finding = {
        "id": "f-cluster", "pattern": "OPT73",
        "title": "OPT73 - Shared step recurs across the cluster - fix once, lower the floor",
        "workflow_file": wf, "affected_jobs": list(legs),
        "evidence": (f"the `{step}` step is 90% of the slowest cluster job `{legs[0]}` "
                     f"and recurs across {len(legs)} concurrent jobs - a cluster-floor lever"),
        "wall_clock_p50_s": cluster_wc, "runner_min_saving": 700.0,
        "tier": 1, "realization": "direct",
    }
    if flag:
        finding["cluster_floor_lever"] = True

    def _mk(name, p50, pn, cwf):
        c = {"name": name, "p50_s": p50, "workflow_file": cwf}
        if stamp_pole_n:
            c["pole_n"] = pn
        return c

    checks = [_mk(legs[0], cluster_wc + 100, wf_pole, wf),
              _mk("primary-gate", cluster_wc - 50, other_pole, ".github/workflows/tests_primary.yml")]
    return {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "required_checks": list(required or []),
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300, "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": npop, "sample_target": npop, "sample_complete": True,
            "check_present_n_pr": npop,
            "poles": [{
                "check": "primary-gate", "p50_s": cluster_wc - 50,
                "workflow_file": ".github/workflows/tests_primary.yml", "job": "primary-gate",
                "dominant_step": "Run test", "dominant_p50_s": (cluster_wc - 50) * 0.5,
                "steps": [{"step": "Run test", "category": "test", "p50_s": (cluster_wc - 50) * 0.5}],
            }],
            "checks": checks,
        },
        "findings": [finding],
    }


def _crown_bottom_line(secs: float, wf: str = "tests_secondary.yml") -> str:
    # Mirrors `blocking_path._cluster_headline_bottom_line`: the crown sentence names the cluster's
    # workflow and points at its **OPT73** entry — the two facts the crown guards identify it by.
    return (f"> **Bottom line.** A typical PR waits **80m 00s** for all checks to finish. The biggest "
            f"single measured win is **~{bp._clock(secs)}** - the `Run test` step recurs across the "
            f"3 concurrent legs of `{wf}`. See the **OPT73** entry under [Also noticed](#also-noticed) "
            "for the measured step, evidence, and fix recipe.")


def test_presence_guard_FAILS_when_a_minority_workflow_cluster_crowns(tmp_path):
    # The exact playwright #56 defect: `tests_secondary.yml` gates only 2/20 yet its OPT73 crowns
    # the bottom line. The converse guard must FAIL (the crown is not presence-eligible).
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Test msedge-dev on macos-latest", "Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2)
    report = _crown_bottom_line(3000.0)
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert not c.ok and not c.skipped, c.detail
    assert "MINORITY-present" in c.detail and "2/20" in c.detail


def test_presence_guard_PASSES_when_the_crowned_workflow_gates_a_majority(tmp_path):
    # A cluster on a workflow that gates 13/20 is genuinely on the typical merge path — its crown
    # is presence-eligible.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android", "chrome"],
                        step="Run test", cluster_wc=3000.0, wf_pole=13)
    report = _crown_bottom_line(3000.0)
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert c.ok and not c.skipped, c.detail
    assert "majority" in c.detail


def test_presence_guard_EXEMPTS_a_required_check_workflow(tmp_path):
    # A required check in the workflow gates by definition — never a false FAIL even at 2/20.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2,
                        required=["Windows (firefox)"])
    report = _crown_bottom_line(3000.0)
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert c.ok and not c.skipped, c.detail
    assert "REQUIRED" in c.detail


def test_presence_guard_SKIPS_loudly_without_stamped_pole_n(tmp_path):
    # A legacy artifact with no `pole_n` on the workflow's checks — the gate frequency can't be
    # re-derived, so the guard SKIPs LOUDLY (never reads clean on an unverifiable crown).
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2, stamp_pole_n=False)
    report = _crown_bottom_line(3000.0)
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert c.skipped and c.ok, c.detail
    assert "no stamped `pole_n`" in c.detail and "Coverage gap" in c.detail


def test_presence_guard_SKIPS_when_the_cluster_lever_is_buried_not_crowned(tmp_path):
    # A minority cluster that is NOT the rendered crown (a smaller ~clock leads) is the BURIAL
    # guard's target, not this one — SKIP, don't FAIL.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2)
    report = _crown_bottom_line(40.0)   # bottom line leads with ~40s, cluster (3000s) is buried
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert c.skipped and c.ok, c.detail
    assert "does not lead the Bottom line" in c.detail


def test_presence_guard_SKIPS_loudly_when_no_credited_cluster_lever_exists(tmp_path):
    # After the engine demotes a minority cluster to bill-only (wall_clock 0), there is no credited
    # cluster lever to crown — the guard verified NOTHING, so it must SKIP LOUDLY (never read as a
    # clean PASS), matching the sibling burial guard's coverage-gap semantics (L8).
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2)
    doc["findings"][0]["wall_clock_p50_s"] = 0.0   # demoted to bill-only
    report = _crown_bottom_line(3000.0)
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    # RED-PROOF: this branch must NOT report a silent clean pass (skipped unset). If it ever
    # reverts to a bare `Check(name, True, ...)` the `c.skipped` assertion fails loudly.
    assert c.skipped and c.ok, c.detail
    assert "no crowned lever to presence-check" in c.detail
    assert "Coverage gap, not a clean pass." in c.detail


def test_presence_guard_in_run_checks_registry(tmp_path):
    # The guard is wired into run_checks (so the dogfood grader-seed loop and CI see it).
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"],
                        step="Run test", cluster_wc=3000.0, wf_pole=2)
    fp = _write_findings(tmp_path, doc)
    report = _crown_bottom_line(3000.0)
    names = {c.name for c in vr.run_checks(report, tmp_path / "r.md", fp, skill_repo=None)}
    assert "Bottom-line crowned cluster lever is presence-eligible (no minority-workflow crown)" in names


# --------------------------------------------------------------------------- #
# E. Issue #114 — crown eligibility binds to the CLUSTER's OWN presence on the gating spine.
# A workflow can host a required check (its spine gate) while the crowned cluster's OWN jobs were
# dropped from the required-scoped spine (`off_spine=True`) — that off-spine cluster is
# bill/throughput, never merge-wait, so it must not crown the typical-PR headline.
# --------------------------------------------------------------------------- #
def test_off_spine_cluster_does_not_crown_the_headline():
    # home-assistant/core: a 10-leg `Run pytest` matrix (627s here) whose jobs are off the
    # required-scoped spine must NOT headline; the chain headroom (~36s) leads instead.
    d = _mastodon_shape()
    d["findings"][0]["off_spine"] = True
    bl = _bottom_line(bp.render(d))
    assert "**~10m 27s**" not in bl, bl                 # the off-spine cluster is NOT crowned
    assert "fixing the whole chain is worth up to" in bl and "**~36s**" in bl


def test_on_spine_cluster_still_crowns_control():
    # The same cluster WITHOUT the off-spine stamp still leads (the #49 behavior is unchanged).
    bl = _bottom_line(bp.render(_mastodon_shape()))
    assert "**~10m 27s**" in bl


def test_on_spine_guard_FAILS_when_an_off_spine_cluster_is_crowned(tmp_path):
    # The exact ha defect: the bottom line crowns a ~50m0s cluster whose finding is stamped
    # off_spine=True (its workflow gates via a DIFFERENT required check). The guard must FAIL.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/ci.yaml",
                        legs=["Run tests (1)", "Run tests (2)", "Run tests (3)"],
                        step="Run pytest", cluster_wc=3000.0, wf_pole=0,
                        required=["Check hassfest"])
    doc["findings"][0]["off_spine"] = True
    report = _crown_bottom_line(3000.0, wf="ci.yaml")
    c = vr.check_headline_cluster_lever_on_spine(report, _write_findings(tmp_path, doc))
    assert not c.ok and not c.skipped, c.detail
    assert "off_spine=True" in c.detail and "never crown" in c.detail


def test_on_spine_guard_SKIPS_when_the_off_spine_cluster_is_demoted(tmp_path):
    # After the render fix the off-spine cluster no longer leads (the chain headroom does), so the
    # max credited cluster does not lead — the guard has no crown to catch → LOUD narrow skip.
    vr = _vr()
    d = _mastodon_shape()
    d["findings"][0]["off_spine"] = True
    md = bp.render(d)
    c = vr.check_headline_cluster_lever_on_spine(md, _write_findings(tmp_path, d))
    assert c.skipped and c.ok, c.detail
    assert "does not lead" in c.detail and "Coverage gap, not a clean pass." in c.detail


def test_on_spine_guard_passes_an_on_spine_crown(tmp_path):
    # An on-spine cluster (no off_spine stamp) that leads is honest — the guard passes.
    vr = _vr()
    d = _mastodon_shape()
    md = bp.render(d)
    c = vr.check_headline_cluster_lever_on_spine(md, _write_findings(tmp_path, d))
    assert c.ok and not c.skipped, c.detail
    assert "on the gating spine" in c.detail


def test_burial_guard_ignores_an_off_spine_cluster(tmp_path):
    # The burial invariant (#49) must NOT fire on an off-spine cluster that the #114 fix demotes —
    # excluding off_spine from its credited set is what reconciles the two guards.
    vr = _vr()
    d = _mastodon_shape()
    d["findings"][0]["off_spine"] = True
    md = bp.render(d)                              # leads with the ~36s chain headroom now
    c = vr.check_headline_consumes_stamped_cluster_ceiling(md, _write_findings(tmp_path, d))
    assert c.ok and not c.skipped, c.detail
    assert "nothing to headline" in c.detail


def test_on_spine_guard_in_run_checks_registry(tmp_path):
    vr = _vr()
    d = _mastodon_shape()
    fp = _write_findings(tmp_path, d)
    names = {c.name for c in vr.run_checks(bp.render(d), tmp_path / "r.md", fp, skill_repo=None)}
    assert "Bottom-line crowned cluster lever is on the merge-gating spine (not off-spine)" in names


def test_volume_unknown_credited_cluster_is_appendix_owned_not_a_duplicate_pole():
    # A wall-clock-credited OPT73 whose monthly volume couldn't be fetched (runner_min_saving None)
    # must still be owned by the bill appendix, so the headline's "Also noticed" pointer resolves —
    # never rendered as a per-pole structural section the pointer wouldn't reach (and never twice).
    d = _mastodon_shape()
    d["findings"][0]["runner_min_saving"] = None
    assert bp._is_pole_structural(d["findings"][0]) is False   # appendix-owned on the wall-clock axis
    md = bp.render(d)
    assert md.count("📐 Structural root-cause") == 0            # not a duplicate per-pole section
    bl = _bottom_line(md)
    assert "**~10m 27s**" in bl and "Also noticed" in bl


# --------------------------------------------------------------------------- #
# F. A SMALL off-spine cluster under a BIGGER chain headline is not "crowned" (the mastra-ai/mastra
# shape). The renderer correctly leads with the chain headroom (~15m 31s on mastra) and demotes the
# 58s off-spine `prebuild.yml` cluster, but the crown guards read "rendered win >= cluster ceiling" as
# "the cluster leads" — so the chain's own number was attributed to the cluster and the on-spine
# guard FAILed a correct report. A cluster leads only when the rendered win IS its ceiling.
# --------------------------------------------------------------------------- #
def _small_off_spine_cluster_under_chain() -> dict:
    # Mastodon chain (~36s headroom leads) with a 20s off-spine cluster that cannot out-rank it.
    d = _mastodon_shape()
    d["findings"][0]["wall_clock_p50_s"] = 20.0
    d["findings"][0]["off_spine"] = True
    return d


def test_small_off_spine_cluster_under_a_chain_headline_is_not_crowned(tmp_path):
    vr = _vr()
    d = _small_off_spine_cluster_under_chain()
    md = bp.render(d)
    bl = _bottom_line(md)
    assert "fixing the whole chain is worth up to" in bl and "**~36s**" in bl, bl
    assert "OPT73" not in bl                         # the renderer did NOT crown the cluster
    c = vr.check_headline_cluster_lever_on_spine(md, _write_findings(tmp_path, d))
    assert c.ok and c.skipped, c.detail              # no off-spine crown -> loud narrow skip
    assert "does not lead" in c.detail and "Coverage gap, not a clean pass." in c.detail


def test_presence_guard_does_not_judge_a_cluster_a_bigger_win_outranks(tmp_path):
    # Same over-attribution in the presence guard: a minority-workflow cluster (2/20) whose 20s
    # ceiling sits under a bigger rendered win is not the crown, so it must not FAIL as one.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"], step="Run test",
                        cluster_wc=20.0, wf_pole=2)
    # A different, bigger lever leads: the chain headroom, not the cluster's own workflow.
    report = ("> **Bottom line.** A typical PR waits **80m 00s** for all checks to finish; fixing "
              "the whole chain is worth up to **~50m 00s**.")
    c = vr.check_headline_lever_is_presence_eligible(report, _write_findings(tmp_path, doc))
    assert c.ok and c.skipped, c.detail
    assert "does not lead" in c.detail


# The crown match is "within 2s", not exact: a fractional stamped ceiling renders to whole seconds
# (`_clock`), so a crowned cluster whose ceiling is 3000.4s shows as ~50m 00s. Both crown guards
# must still treat that as the cluster's crown and FAIL; an exact-equality helper would SKIP it.
def test_presence_guard_FAILS_a_crown_whose_fractional_ceiling_rounds_in_the_render(tmp_path):
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"], step="Run test",
                        cluster_wc=3000.4, wf_pole=2)
    c = vr.check_headline_lever_is_presence_eligible(_crown_bottom_line(3000.4),
                                                     _write_findings(tmp_path, doc))
    assert not c.ok and not c.skipped, c.detail


def test_on_spine_guard_FAILS_an_off_spine_crown_whose_fractional_ceiling_rounds(tmp_path):
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/ci.yaml",
                        legs=["Run tests (1)", "Run tests (2)", "Run tests (3)"],
                        step="Run pytest", cluster_wc=3001.6, wf_pole=0,
                        required=["Check hassfest"])
    doc["findings"][0]["off_spine"] = True
    report = _crown_bottom_line(3000.0, wf="ci.yaml")  # rendered 50m 00s, 1.6s under the ceiling
    c = vr.check_headline_cluster_lever_on_spine(report, _write_findings(tmp_path, doc))
    assert not c.ok and not c.skipped, c.detail


# G. The crown is identified by what the Bottom line NAMES, not by its number alone. A number match
# is a coincidence when the sentence leads with a different lever, and a crown sentence that names
# the cluster is that cluster's crown even if its number is off.
def test_off_spine_cluster_within_2s_of_a_chain_headline_is_not_crowned(tmp_path):
    # A 35s off-spine cluster under the ~36s chain headroom: the renderer leads with the chain and
    # never names the cluster, so the on-spine guard must not read the 1s-apart numbers as a crown.
    vr = _vr()
    d = _mastodon_shape()
    d["findings"][0]["wall_clock_p50_s"] = 35.0
    d["findings"][0]["off_spine"] = True
    md = bp.render(d)
    bl = _bottom_line(md)
    assert "fixing the whole chain is worth up to" in bl and "**~36s**" in bl, bl
    assert "OPT73" not in bl
    c = vr.check_headline_cluster_lever_on_spine(md, _write_findings(tmp_path, d))
    assert c.ok and c.skipped, c.detail
    assert "does not lead" in c.detail


def test_presence_guard_FAILS_a_named_minority_crown_whose_number_is_inflated(tmp_path):
    # The Bottom line names the minority workflow's cluster as the crown but prints a number far
    # above its stamped 20s ceiling. It is still that cluster's crown: FAIL, not an outranked skip.
    vr = _vr()
    doc = _presence_doc(wf=".github/workflows/tests_secondary.yml",
                        legs=["Windows (firefox)", "Android"], step="Run test",
                        cluster_wc=20.0, wf_pole=2)
    c = vr.check_headline_lever_is_presence_eligible(_crown_bottom_line(3000.0),
                                                     _write_findings(tmp_path, doc))
    assert not c.ok and not c.skipped, c.detail
