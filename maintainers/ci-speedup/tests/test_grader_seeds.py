"""Tests for grader_seeds.py — the structured-grader → dogfood seed-bug mapper (PR-A, §2-A).

The deterministic plumbing is validated by FIXTURE-REPLAY (spec §3): a committed report+findings
known to trip an invariant → the pure mapping helper → assert the right seed. The LLM steps of the
loop are NOT exercised here (a live dogfood run is a labeled smoke test, never the merge gate)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import grader_seeds as gs
import measure_contradictions as mc

_VR = mc._load_verify_report()
_Check = _VR.Check
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The exact Check.name strings the mapper keys on.
_DROPPED = "no spine-dropped check is also framed on the merge-gating critical path"
_DATE = "scanned date matches the date in the filename"       # EXCLUDE
_LEAK = "no ci-secure template leakage (security-domain framing in ci-speedup prose)"  # TRIAGE
_COST_SPINE = "runner-minute cost spine source block is re-derivable"


def _required_schema_keys(bug: dict) -> bool:
    # The dogfood AUDIT schema requires these on every bug; the fix fan-out reads them.
    return {"title", "severity", "suspected_location", "evidence", "signature"} <= set(bug)


# --- the triage allowlist is total + correctly proportioned -------------------
def test_every_check_is_classified():
    # Invariant: every live verify_report check is in the allowlist (a new check fails this until
    # classified). run_checks always returns all checks (some skipped) regardless of input.
    names = {c.name for c in _VR.run_checks("# x\n", None, None, skill_repo=None)}
    assert set(gs.TRIAGE_ALLOWLIST) == names, set(gs.TRIAGE_ALLOWLIST) ^ names


def test_classification_counts_match_spec():
    counts = Counter(gs.TRIAGE_ALLOWLIST.values())
    assert counts[gs.AUTO_SEED] == 62   # +1 (pre-flip audit: static-only banner matches CI shape — no dormant-repo hedge at a live no-PR-gating repo, no "no run timing" beside priced timed runs);
    # +2 (issue #114: crowned cluster lever is on the merge-gating spine — no off-spine crown; issue #115: headline leads with the observed wall when the chain sum diverges from the makespan);
    # +1 (issue #106: every 🤖 gap-fill evidence line is verbatim from the captured job log — the injection-residual grounding backstop);
    # +1 (fence-escaping: code fences are balanced — no stray ``` breaks out of a fence / desyncs the verifier's fence split);
    # +1 (issue #80: per-PR chain/makespan spine bound to the kept config era — no dropped-era PR blends in);
    # +1 (issue #74: rendered era disclosure matches the enumerated config — no pre-only caveat over post-era checks);
    # +1 (issue #69: check enumeration is bound to the kept config era — no other-config check leaks in);
    # +2 (issue #66: recoverable ceiling carries a worst-case reconciliation; no drilled pole measures a pre-change config era without the era disclosure);
    # +1 (issue #56: Bottom-line crowned cluster lever is presence-eligible — no minority-workflow crown);
    # +1 (issue #49: Bottom-line headline leads with the stamped cluster-floor ceiling — no burial);
    # +2 (issues #43/#44/#45 measured sizing door: every saving carries a measured-basis stamp; cluster-floor lever escapes its sibling cap);
    # +1 (check_demoted_pole_not_framed_typical_gate: demoted pole must not carry typical-gate framing);
    # +3 (issue #25 physical-bounds family: aggregate total >= largest member; headline wait <= makespan; saving <= measured compute);
    # +1 (OPT72 payload-binning seed);
    # +1 (issue #16 off-category leaf-hijack: crowned leaf must agree with the pole's dominant measured category);
    # +1 (headline floor-reconciliation: Form-2 non-universal slowest check must disclose presence);
    # +1 (presence-causal headline guard: nx main-linux non-sequitur);
    # +7 Tier-2/cost-spine invariants (net of the 2026-07-21 pricing punt: -billing-class-honest,
    #    -sku-arbitrage-ceiling, +no-rate-derived-dollars); +1 ENG-1 chain headline; +1 PR-S3 Pending caveat;
    # +2 PR-P1 (the /timing-citation ban and the Tier-2 derivation-basis field);
    # +1 PR-P2 (the shallow cost-spine disclosure — G15);
    # +1 (the prefetch-plan-consumed check — the parallel gh pass, PR #215).
    # +1 (#212: workflows missing from the sample must be NAMED in the report).
    # +1 (payload-binned-as-build class — the OPT72 misroute guard, nrwl/nx).
    # +1 (MEASURED CAUSE never asserts unrendered timeline steps — the nrwl/nx playwright-parallel fix).
    # +1 (#12: no fileless/managed status check crowns the headline — disclosed as PR-lifetime latency).
    # +1 (#1: aggregation-gate poles tell the upstream story, never an optimize-this prompt).
    assert counts[gs.EXCLUDE] == 1
    assert counts[gs.TRIAGE] == 2


def test_classify_raises_on_unknown_check():
    try:
        gs.classify("a check that does not exist")
    except KeyError:
        return
    raise AssertionError("classify must raise (never silently default) on an unclassified check")


# --- PR-B: the closed-vocab `class` retrofit (§2, Item 1 / tracker log "B-coupling reminder") ---
def test_every_check_has_a_class():
    # Same total-classification invariant as test_every_check_is_classified, for CHECK_CLASS: a
    # new verify_report check must be classified here too before it can carry a class.
    names = {c.name for c in _VR.run_checks("# x\n", None, None, skill_repo=None)}
    assert set(gs.CHECK_CLASS) == names, set(gs.CHECK_CLASS) ^ names


def test_every_check_class_is_in_the_closed_vocab():
    assert set(gs.CHECK_CLASS.values()) <= gs.CLASS_ENUM
    assert gs._DIVERGENCE_CLASS in gs.CLASS_ENUM


def test_class_enum_matches_the_dogfood_workflows_bug_class_enum():
    # Lockstep check: grader_seeds.CLASS_ENUM (Python) must equal ci-speedup-dogfood.js's
    # BUG_CLASS_ENUM (JS) exactly — both are meant to be the SAME reused transcript root_cause
    # enum. Extract the JS array by regex (the same drift-proof pattern dogfood-retry.test.mjs
    # uses in the other direction), so a hand-edit to either side that doesn't mirror the other
    # fails here rather than silently forking the vocabulary.
    import re as _re

    workflow_path = (
        Path(__file__).resolve().parents[1] / "workflows" / "ci-speedup-dogfood.js"
    )
    src = workflow_path.read_text(encoding="utf-8")
    m = _re.search(r"const BUG_CLASS_ENUM = (\[[\s\S]*?\n\])", src)
    assert m, "could not locate BUG_CLASS_ENUM in ci-speedup-dogfood.js"
    js_values = {v.strip().strip("'\"") for v in _re.findall(r"'([^']+)'", m.group(1))}
    assert js_values == set(gs.CLASS_ENUM), js_values ^ set(gs.CLASS_ENUM)


def test_seed_from_check_carries_its_class():
    disp, bug = gs.seed_from_check(_Check(_DROPPED, ok=False, detail="contradiction X"))
    assert disp == gs.AUTO_SEED
    assert bug["class"] == gs.CHECK_CLASS[_DROPPED]
    assert bug["class"] in gs.CLASS_ENUM


def test_seed_from_divergence_carries_its_class():
    bug = gs.seed_from_divergence(True, "consumer→`build` vs headline→`lint`")
    assert bug["class"] == gs._DIVERGENCE_CLASS
    assert bug["class"] in gs.CLASS_ENUM


def test_excluded_check_never_seeds_so_never_carries_a_class():
    disp, bug = gs.seed_from_check(_Check(_DATE, ok=False, detail="mismatch"))
    assert disp == gs.EXCLUDE
    assert bug is None   # EXCLUDE never produces a bug dict at all — nothing to carry a class


# --- the pure mapping helper, per disposition / status ------------------------
def test_auto_seed_fail_becomes_a_seed():
    disp, bug = gs.seed_from_check(_Check(_DROPPED, ok=False, detail="contradiction X"))
    assert disp == gs.AUTO_SEED
    assert _required_schema_keys(bug)
    assert bug["signature"] == "grader-seed@check:" + gs._slug_check(_DROPPED)
    assert bug["severity"] == "needs-triage"          # locus-less
    assert "contradiction X" in bug["evidence"]
    assert bug["source"] == "grader-seed"


def test_cost_spine_seed_points_at_source_block_producer():
    disp, bug = gs.seed_from_check(_Check(_COST_SPINE, ok=False, detail="row total drift"))
    assert disp == gs.AUTO_SEED
    assert bug["signature"] == "grader-seed@check:" + gs._slug_check(_COST_SPINE)
    assert bug["severity"] == "needs-triage"
    assert "collect_runs.py" in bug["suspected_location"]
    assert "verify_report.py" in bug["suspected_location"]


def test_excluded_fail_never_seeds():
    disp, bug = gs.seed_from_check(_Check(_DATE, ok=False, detail="date mismatch"))
    assert disp == gs.EXCLUDE
    assert bug is None


def test_triage_fail_routes_to_triage_not_seed():
    disp, bug = gs.seed_from_check(_Check(_LEAK, ok=False, detail="leak"))
    assert disp == gs.TRIAGE
    assert bug is not None and _required_schema_keys(bug)


def test_skip_never_seeds():
    _disp, bug = gs.seed_from_check(_Check(_DROPPED, ok=False, detail="d", skipped=True))
    assert bug is None


def test_pass_never_seeds():
    _disp, bug = gs.seed_from_check(_Check(_DROPPED, ok=True, detail="clean"))
    assert bug is None


# --- the two dedup namespaces -------------------------------------------------
def test_locus_bearing_signature_joins_llm_namespace():
    # A seed WITH an honest file:symbol uses the same `<slug>@<file>:<symbol>` namespace as LLM bugs.
    sig = gs.seed_signature(locus="blocking_path.py:_render_headline", slug="encord-pole")
    assert sig == "encord-pole@blocking_path.py:_render_headline"


def test_seed_from_check_threads_slug_for_a_locus_bearing_seed():
    # A future locus-BEARING caller passes both `locus` and the bug's repo/descriptor `slug`; the
    # seed must land in the LLM-audit namespace `<slug>@<file>:<symbol>` (not `grader-seed@…`) so it
    # dedups against an LLM-audit bug at the same locus. Exercised THROUGH seed_from_check (not just
    # seed_signature) so the threading itself is covered.
    disp, bug = gs.seed_from_check(
        _Check(_DROPPED, ok=False, detail="d"),
        locus="blocking_path.py:_render_headline", slug="encord-pole")
    assert disp == gs.AUTO_SEED
    assert bug["signature"] == "encord-pole@blocking_path.py:_render_headline"
    assert bug["suspected_location"] == "blocking_path.py:_render_headline"
    assert bug["severity"] == "medium"          # locus-bearing → not needs-triage


def test_locus_less_signature_is_repo_independent():
    # Two reports tripping the SAME check → the SAME signature (dedup to one bug), regardless of
    # any per-repo slug — the locus-less namespace never carries the repo.
    a = gs.seed_signature(check_name=_DROPPED, slug="ownerA_repoA")
    b = gs.seed_signature(check_name=_DROPPED, slug="ownerB_repoB")
    assert a == b == "grader-seed@check:" + gs._slug_check(_DROPPED)


# --- the divergence seed ------------------------------------------------------
def test_divergence_true_makes_a_triage_candidate():
    seed = gs.seed_from_divergence(True, "consumer→`Autobahn` vs headline→`Benchmark`")
    assert seed is not None and _required_schema_keys(seed)
    assert seed["signature"] == "grader-seed@check:consumer-divergence"


def test_divergence_false_seeds_nothing():
    assert gs.seed_from_divergence(False, "no divergence") is None


def test_collect_routes_divergence_to_triage_not_seeds():
    # The divergence rides a crude proxy (over-counts), so it must be ADJUDICATED, never
    # auto-seeded into the fix fan-out. It lands in triage[], and seeds[] stays empty.
    out = gs.collect_seeds([], divergence=(True, "consumer→`A` vs headline→`B`"))
    assert out["seeds"] == []
    assert [s["signature"] for s in out["triage"]] == ["grader-seed@check:consumer-divergence"]


def test_collect_no_divergence_adds_nothing_to_triage():
    out = gs.collect_seeds([], divergence=(False, "no divergence"))
    assert out["seeds"] == [] and out["triage"] == []


# --- FIXTURE-REPLAY: committed report+findings → run_checks → collect_seeds ----
def _fixture_checks(report_path: Path):
    report = (_FIXTURES / "seed_fixture-report.md").read_text(encoding="utf-8")
    # Write under a dated filename that MISMATCHES the report's scanned date (2026-05-29) so the
    # EXCLUDE check (check_date_matches_filename) actually FAILs — letting the replay assert that an
    # EXCLUDED FAIL is recorded but never seeded.
    report_path.write_text(report, encoding="utf-8")
    findings = _FIXTURES / "seed_fixture-findings.json"
    return _VR.run_checks(report, report_path, findings, skill_repo=None)


def test_fixture_replay_drops_a_dropped_check_into_a_seed(tmp_path: Path):
    checks = _fixture_checks(tmp_path / "seed_fixture-2020-01-01.md")
    out = gs.collect_seeds(checks)

    sigs = [s["signature"] for s in out["seeds"]]
    # The dropped-check contradiction the fixture trips becomes an AUTO_SEED bug, right namespace.
    assert "grader-seed@check:" + gs._slug_check(_DROPPED) in sigs
    # Every seed carries the full audit-bug schema (so it dedups + fans out like an LLM bug).
    assert all(_required_schema_keys(s) for s in out["seeds"])

    # An EXCLUDED check that FAILed is recorded but NEVER seeded.
    assert _DATE in out["excluded"]
    assert all("scanned-date" not in s["signature"] for s in out["seeds"])


def test_fixture_replay_findings_are_valid_json():
    # Guard the committed fixture itself — a malformed findings file would make the replay vacuous.
    data = json.loads((_FIXTURES / "seed_fixture-findings.json").read_text(encoding="utf-8"))
    assert data["pr_critical_path"]["dropped_non_required_checks"] == ["Run integration tests"]


# --- no-silent-drops surfacing: skipped checks + divergence-probe status ------
def test_collect_records_skipped_checks_with_disposition():
    # A SKIPPED check is "couldn't check", not "clean" — collect_seeds must record it (with its
    # disposition) so an auto-seed skip is a visible coverage gap, never a silent pass.
    out = gs.collect_seeds([_Check(_DROPPED, ok=False, detail="findings unreadable", skipped=True)])
    assert out["skipped"] == [{"name": _DROPPED, "disposition": gs.AUTO_SEED}]
    assert out["seeds"] == [] and out["triage"] == []


def test_collect_omits_an_excluded_check_that_skipped():
    # An EXCLUDE check that SKIPPED is NOT a coverage gap — it never seeds either way — so it must
    # not appear in skipped[] (it didn't FAIL, so it's not in excluded[] either). Only AUTO_SEED /
    # TRIAGE skips are real "couldn't check" gaps.
    out = gs.collect_seeds([_Check(_DATE, ok=False, detail="filename carries no date", skipped=True)])
    assert out["skipped"] == []
    assert out["excluded"] == []


def test_collect_divergence_status_records_ran_state():
    # divergence=None → the probe did NOT run (distinguishable from "ran, clean").
    assert gs.collect_seeds([])["divergence"] == {
        "ran": False, "reason": "divergence probe not run (findings unavailable/unreadable)"}
    # ran + clean
    clean = gs.collect_seeds([], divergence=(False, "no divergence"))
    assert clean["divergence"] == {"ran": True, "diverges": False, "detail": "no divergence"}
    assert clean["triage"] == []
    # ran + diverges → status reflects it AND the triage candidate is added
    div = gs.collect_seeds([], divergence=(True, "consumer→`A` vs headline→`B`"))
    assert div["divergence"]["ran"] is True and div["divergence"]["diverges"] is True
    assert [s["signature"] for s in div["triage"]] == ["grader-seed@check:consumer-divergence"]


# --- CLI entrypoint (grader_seeds.main) ---------------------------------------
def _run_main(capsys, *argv) -> dict:
    rc = gs.main(list(argv))
    out = json.loads(capsys.readouterr().out)
    return {"rc": rc, **out}


def test_main_happy_path_emits_seed_json(tmp_path, capsys):
    md = tmp_path / "seed_fixture-report.md"
    md.write_text((_FIXTURES / "seed_fixture-report.md").read_text(encoding="utf-8"), encoding="utf-8")
    fj = _FIXTURES / "seed_fixture-findings.json"
    res = _run_main(capsys, "--report", str(md), "--findings", str(fj))
    assert res["rc"] == 0
    assert "grader-seed@check:" + gs._slug_check(_DROPPED) in [s["signature"] for s in res["seeds"]]
    # The probe ran on a well-formed findings file (clean here — the fixture has no divergence).
    assert res["divergence"]["ran"] is True


def test_main_unreadable_findings_does_not_crash(tmp_path, capsys):
    # A malformed findings file must NOT crash main; the divergence probe records ran:False, and the
    # findings-unreadable verify_report FAILs (auto-seed) still surface — no silent loss.
    md = tmp_path / "seed_fixture-report.md"
    md.write_text((_FIXTURES / "seed_fixture-report.md").read_text(encoding="utf-8"), encoding="utf-8")
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = _run_main(capsys, "--report", str(md), "--findings", str(bad))
    assert res["rc"] == 0
    assert res["divergence"]["ran"] is False


def test_main_emits_structured_error_on_grade_crash(tmp_path, capsys, monkeypatch):
    # A crash IN the grading (here: verify_report fails to load) must STILL emit structured JSON +
    # a non-zero exit — the dogfood agent parses stdout, so a bare traceback would silently drop
    # every seed. The result carries empty lists, divergence ran:false, and a loud `error`.
    md = tmp_path / "seed_fixture-report.md"
    md.write_text((_FIXTURES / "seed_fixture-report.md").read_text(encoding="utf-8"), encoding="utf-8")
    fj = _FIXTURES / "seed_fixture-findings.json"

    def _boom():
        raise RuntimeError("verify_report import blew up")
    monkeypatch.setattr(gs.mc, "_load_verify_report", _boom)

    res = _run_main(capsys, "--report", str(md), "--findings", str(fj))
    assert res["rc"] == 1
    assert res["seeds"] == [] and res["triage"] == [] and res["skipped"] == []
    assert res["divergence"]["ran"] is False
    assert "error" in res and "verify_report import blew up" in res["error"]


def test_main_readable_empty_findings_counts_as_probe_ran(tmp_path, capsys):
    # A READABLE but empty `{}` findings is "probe ran, nothing measurable" — NOT "probe not run".
    # (Keying main()'s probe call on truthiness would mislabel `{}` as a non-run; it keys on `is
    # None`, so only an unreadable file is a non-run.)
    md = tmp_path / "seed_fixture-report.md"
    md.write_text((_FIXTURES / "seed_fixture-report.md").read_text(encoding="utf-8"), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    res = _run_main(capsys, "--report", str(md), "--findings", str(empty))
    assert res["rc"] == 0
    assert res["divergence"] == {"ran": True, "diverges": False, "detail": "no critical_path_check"}
