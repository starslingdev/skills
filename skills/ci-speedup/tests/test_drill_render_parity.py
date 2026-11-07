"""Regression: the drill-bundle CAPTURE pole set must equal the report's RENDERED
pole set, so every rendered long pole has its OWN captured log.

Bug (lancedb/lancedb): Long pole 2 (`python.yml ▸ Doctest`) rendered the pydantic1x
job's evidence — run/job id, step timeline, and cross-run magnitudes all belonged to a
DIFFERENT job. Root cause: the capture-time drill selection sorted the non-gate matrices
by IMPACT only (`max(median, bimodal slow mode)`), which pulled the rare-but-slow
`build - aarch64-apple-darwin` leg (present on a MINORITY of PRs, but long when it runs)
above the TYPICAL `Doctest` pole. So capture drilled `build` while the renderer demoted
it (typical-first tiering) and rendered `Doctest`. `Doctest` then had no captured log,
and `blocking_path._match_key`'s exact-workflow-stem rule (`python`) bound its drill to
the sibling pydantic1x bundle — the wrong job's evidence.

The collector and renderer now share the same pole selector, including actual gate
frequency, required checks, and impact within typical/rare tiers.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_drill_render_parity.py
"""

from __future__ import annotations

from pathlib import Path

from blocking_path import _match_key, _pole_owner_keys, _select_render_poles
from collect_runs import (
    _RARE_PRESENCE_FRAC,
    _RARE_PRESENCE_MIN_PR,
)


def test_undrilled_sibling_pole_does_not_borrow_drilled_log():
    """R1 (Fieldguide): two poles in the SAME workflow, only `build` drilled. The
    identity-aware binding must give `build` its key and the undrilled `test` pole
    NONE — so `test` renders with no captured log instead of inheriting `build`'s
    waterfall/evidence/prompt. This is the binding `render()` now uses in place of the
    bare workflow-stem `_match_key`, which DID leak across the sibling (asserted below
    as the contrast). The PR #94 attempt missed this because its test supplied no logs,
    so the stem-borrow never fired."""
    build = {"check": "build", "workflow_file": ".github/workflows/ci.yml", "p50_s": 300.0}
    test = {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 280.0}
    poles = [build, test]
    doc = {"data_bundle": {"logs": [
        {"check": "build", "workflow_file": ".github/workflows/ci.yml", "html_url": "u"}]}}
    keys = _pole_owner_keys(doc, poles, set())
    assert keys.get(id(build))            # the drilled pole owns its render key
    assert id(test) not in keys           # the undrilled sibling owns NO key — no borrow
    # Contrast — the OLD bare-stem matcher WOULD have leaked build's log onto `test`:
    drilled_logs = {keys[id(build)]: "BUILD-LOG"}
    assert _match_key(drilled_logs, "ci.yml", "test") == "BUILD-LOG"  # the R1 bug, shown
    # …whereas the identity binding gives `test` no key, so render binds nothing for it.
    assert keys.get(id(test)) is None


def test_depth5_render_subset_logs_no_sibling_inheritance():
    """R1 end-to-end at the raised render depth: two distinct jobs in ONE workflow both
    render (`build` drilled, `test` not). The drilled pole shows its representative-run
    drill; the undrilled sibling must render shallow (sampled decomposition) and must NOT
    inherit `build`'s representative run. The new verify_report drill-ownership check must
    pass on the output. (The depth-2 / no-logs PR #94 test missed exactly this.)"""
    import sys
    import blocking_path as bp
    import importlib.util
    _vr_path = Path(__file__).resolve().parents[1] / "tests" / "verify_report.py"
    _spec = importlib.util.spec_from_file_location("verify_report", _vr_path)
    vr = importlib.util.module_from_spec(_spec)
    sys.modules["verify_report"] = vr  # so verify_report's @dataclass resolves
    _spec.loader.exec_module(vr)

    wf = ".github/workflows/ci.yml"
    pole = lambda check, p50, dom: {
        "check": check, "p50_s": p50, "workflow_file": wf, "job": check,
        "dominant_step": "Build" if check == "build" else "Run tests",
        "dominant_p50_s": dom, "dominant_share": dom / p50,
        "steps": [{"step": "Build" if check == "build" else "Run tests",
                   "category": "build" if check == "build" else "test", "p50_s": dom}]}
    build, test = pole("build", 400.0, 300.0), pole("test", 360.0, 280.0)
    run_url = "https://github.com/o/r/actions/runs/12345"
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 30, "jobs_sampled": 60, "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": 3, "sample_target": 3, "sample_complete": True,
            "poles": [build, test]},
        # Only `build` is drilled — its entry drives the owner-key binding.
        "data_bundle": {"logs": [{"check": "build", "workflow_file": wf,
                                  "html_url": run_url}]},
        "findings": [],
    }
    steps = {"ci": {"run_url": run_url, "job_dur_s": 300.0,
                    "steps": [{"name": "Build", "dur_s": 300.0,
                               "start_s": 0.0, "end_s": 300.0}]}}
    md = bp.render(doc, logs={}, samples={}, log_runs={"ci": run_url},
                   captured_at="2026-06-08", steps=steps)
    # Both jobs render (matrix-grouped, not workflow-grouped).
    assert "Long pole 1:" in md and "Long pole 2:" in md
    secs = {check: body for wf_, check, body in vr._pole_header_sections(md)}
    assert "build" in secs and "test" in secs
    # The drilled pole shows its representative-run drill; the sibling must NOT inherit it.
    assert "representative run 12345" in secs["build"]
    assert "representative run" not in secs["test"], "undrilled sibling inherited build's drill (R1)"
    # The verify_report safety net passes (no cross-job leak in the output).
    chk = vr.check_pole_drill_belongs_to_its_job(md, _FindingsFile(doc))
    assert chk.ok, chk.detail


class _FindingsFile:
    """Minimal stand-in for a findings Path that verify_report can json-load."""
    def __init__(self, doc): self._doc = doc
    def read_text(self, encoding="utf-8"):
        import json
        return json.dumps(self._doc)


def test_shallow_pole_prompt_uses_sampled_decomposition_not_a_missing_timeline():
    """R2: a pole with NO drilled timeline must name its dominant step from the sampled
    per-step decomposition and say so — never point at a 'step timeline above' that this
    pole doesn't render."""
    import blocking_path as bp
    pole = {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 360.0,
            "dominant_step": "Run tests", "dominant_p50_s": 280.0, "dominant_share": 0.78,
            "steps": [{"step": "Run tests", "category": "test", "p50_s": 280.0}]}
    prompt = bp._build_generic_agent_prompt(
        pole, [], None, "o/r", "abc1234", 5, 10, timeline=None)
    assert "no single-run timeline was captured" in prompt
    assert "Run tests" in prompt
    assert "step timeline above" not in prompt   # the reverted #94 wording must be gone

    # With neither a timeline NOR a sampled dominant step → honest "no breakdown" fallback.
    bare = {"check": "x", "workflow_file": ".github/workflows/ci.yml", "p50_s": 100.0}
    p2 = bp._build_generic_agent_prompt(bare, [], None, "o/r", "abc1234", 5, 10, timeline=None)
    assert "No per-step breakdown was captured" in p2
    assert "step timeline above" not in p2


def test_pole_owner_keys_falls_back_to_sole_owner_without_a_bundle():
    """Hand-run render (no drill bundle): each supplied --log key binds to the pole it
    UNIQUELY owns; an ambiguous key that could borrow across poles binds to none."""
    a = {"check": "Unit Tests", "workflow_file": ".github/workflows/ci.yml", "p50_s": 200.0}
    b = {"check": "Integration Tests", "workflow_file": ".github/workflows/it.yml", "p50_s": 300.0}
    poles = [a, b]
    # `Unit Tests` uniquely owns "unit"; "tests" is a substring of BOTH → owns neither.
    keys = _pole_owner_keys({}, poles, {"unit", "tests"})
    assert keys.get(id(a)) == "unit"
    assert id(b) not in keys              # only matched via the ambiguous "tests" → refused

# lancedb scenario. The present-first spine puts the two TYPICAL python.yml jobs first
# (pydantic1x is the slowest-median typical gate), then the rare aarch64 build leg.
_GATE = ("pydantic1x", 1810.0)
_DOCTEST = ("Doctest", 1810.0)
_BUILD = ("build - aarch64-apple-darwin", 1700.0)
_MATRICES = [_GATE, _DOCTEST, _BUILD]

# Presence over 20 sampled PRs: both python jobs are typical; the aarch64 build leg is
# path-filtered (Cargo.toml/Cargo.lock), so it ran on a minority — 8/20 <= 0.5.
_PRESENT = {"pydantic1x": 20, "Doctest": 20, "build - aarch64-apple-darwin": 8}
_NPOP = 20

# The aarch64 build is BIMODAL — its slow mode (2033s) is the highest IMPACT in the set,
# which is exactly what a flat impact sort would (wrongly) promote above Doctest.
_BIMODAL_SLOW = {"build - aarch64-apple-darwin": 2033.0}


def _impact(check: str, p50: float) -> float:
    return max(p50, _BIMODAL_SLOW.get(check, 0.0))


def _is_typical(check: str) -> bool:
    # The SAME `_RARE_PRESENCE_*` rule the renderer / `_rank_spine_present_first` use.
    if _NPOP < _RARE_PRESENCE_MIN_PR:
        return True
    return _PRESENT.get(check, 0) > _NPOP * _RARE_PRESENCE_FRAC


def _selected_matrices(required=()):
    poles = [{"check": name, "job": name, "workflow_file": ".github/workflows/ci.yml",
              "p50_s": median, "bimodal": {"high_p50_s": _BIMODAL_SLOW.get(name, 0)}}
             for name, median in _MATRICES]
    doc = {"required_checks": list(required), "pr_critical_path": {
        "check_present_n_pr": _NPOP,
        "checks": [{"name": name, "present_on": count} for name, count in _PRESENT.items()]}}
    selected, _ = _select_render_poles(doc, poles)
    return [(p["check"], p["p50_s"]) for p in selected]


def test_rare_high_impact_pole_demoted_below_typical_in_drill_order():
    """The TYPICAL Doctest pole is drilled as #2 — the same pole the renderer shows —
    NOT the rare-but-high-impact aarch64 build leg. This is the renderer's tiering."""
    ordered = _selected_matrices()
    top2 = [c for c, _ in ordered[:2]]
    assert top2 == ["pydantic1x", "Doctest"], top2
    assert "build - aarch64-apple-darwin" not in top2
    # The rare leg is REORDERED to the bottom tier, never dropped.
    assert ordered[-1][0] == "build - aarch64-apple-darwin"


def test_gate_always_leads_regardless_of_rest_impact():
    """The present-first gate (matrices[0]) stays #1 even if a later matrix has higher
    impact — the renderer's gate_rep is likewise the head of the typical-first order."""
    ordered = _selected_matrices()
    assert ordered[0] == _GATE


def test_required_but_rare_check_stays_typical_in_drill_order():
    # The aarch64 build leg is rare by presence (8/20 <= 0.5) but here it is REQUIRED. The renderer
    # keeps required checks typical, so the capture order must too — otherwise a merge-gating pole is
    # demoted below an optional one and loses drill/render parity (the exact class this PR fixes).
    ordered = _selected_matrices({"build - aarch64-apple-darwin"})
    names = [c for c, _ in ordered]
    assert names[0] == "build - aarch64-apple-darwin"  # required gate leads
    assert names[-1] != "build - aarch64-apple-darwin", names    # NOT demoted to the rare bottom
    # Required → typical, and (highest impact) it drills ahead of the lower-impact Doctest.
    assert names.index("build - aarch64-apple-darwin") < names.index("Doctest"), names


def test_flat_impact_sort_reproduces_the_mis_binding():
    """Documents the pre-fix divergence the regression guards against: a flat impact sort
    of the non-gate matrices (the old behavior) drills `build` as #2, leaving the RENDERED
    Doctest pole logless and mis-bound by `_match_key` to the sibling pydantic1x bundle."""
    others = sorted(_MATRICES[1:], key=lambda m: -_impact(m[0], m[1]))
    buggy_top2 = [_MATRICES[0][0], others[0][0]]
    assert buggy_top2 == ["pydantic1x", "build - aarch64-apple-darwin"]
    # With `build` captured but `Doctest` rendered, the captured logs key the unique
    # python.yml stem `python` to the pydantic1x bundle, so `_match_key` borrows it for
    # Doctest — exactly the wrong-job evidence seen in the lancedb report.
    # NB: `npm-publish` is lancedb's REAL workflow stem — the `build - aarch64-apple-darwin`
    # native-bindings leg lives in `npm-publish.yml` (Cargo-path-filtered), not a placeholder.
    captured_logs = {"python": "pydantic1x-bundle", "npm-publish": "build-bundle"}
    assert _match_key(captured_logs, "python.yml", "Doctest") == "pydantic1x-bundle"
