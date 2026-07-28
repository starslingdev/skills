"""Self-test for verify_report.py - proves its invariant checks actually FAIL on a
broken blocking-path report, not just pass on good ones.

verify_report is the production artifact gate, but it was only ever exercised
against well-formed worked examples; a check that silently always-passes would
give false confidence. Each test feeds a deliberately-broken report and asserts
the relevant check reports FAIL, plus a sanity case that the clean report passes.

Run via subprocess (NOT import): ci-secure also ships a `tests/verify_report.py`,
so importing `verify_report` collides on the shared pytest pythonpath and may bind
the wrong module. The CLI is the collision-proof entry point. (The Stream-1 tests
that need the module's internals use `_load_verify_report` below — a by-path load
under a UNIQUE module name, which is collision-proof for the same reason.)

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_verify_report_self.py
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_VERIFY = _SKILL_DIR / "tests" / "verify_report.py"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_phase0_literals_stay_coupled_to_the_renderer():
    # The Phase-0 contradiction checks key on EXACT renderer prose: the appendix on-path note
    # "sits ON the merge-gating critical path" and the headline "is the slowest check a typical PR
    # waits on". If the renderer reworded either, those checks would silently lose a context /
    # SKIP on real reports (the appendix sentence isn't caught by the must-fire guard, since a
    # bundle may carry no on-path appendix). Pin the literals to the renderer's SOURCE so a rename
    # breaks THIS test, forcing the verifier to be updated in lockstep.
    renderer = (_SKILL_DIR / "scripts" / "blocking_path.py").read_text(encoding="utf-8")
    verifier = _VERIFY.read_text(encoding="utf-8")
    # Fragments that appear CONTIGUOUSLY in both files (the renderer builds the headline across
    # f-string lines, so the full sentence isn't a single source literal — these stable fragments
    # are). A rename on either side breaks this, forcing a lockstep update.
    # The all-fileless-gate exemption (`check_speed_poles_complete` / `check_primary_section_present`)
    # keys on the renderer's "no editable workflow file" note as its SECONDARY signal (it SKIPs a
    # 0-pole report only when the findings also carry no file-backed pole). If the renderer reworded
    # that note, a legitimately-all-fileless report would lose its SKIP and FAIL its own gate, with
    # the masking-case fixture still green — so pin the literal here too, forcing a lockstep update.
    # ("no editable workflow file" is split across the renderer's f-string lines, so the
    #  load-bearing contiguous fragment is "editable workflow file" — the verifier's substring
    #  contains it, and a rename that drops/reworks it breaks both sides in lockstep.)
    # The #4 ceiling check (`check_pole_ceiling_within_cooccurrence`) parses two renderer phrasings
    # — the bottom-line "biggest single measured win is …" headline and the per-pole "What a change
    # here can buy … it gates until it drops to the next concurrent check" floor note. It is NOT in
    # `test_committed_reports._MUST_FIRE` (it can legitimately SKIP on a thin-sample / matrix-only /
    # static report), so a renderer rewording would silently SKIP it on every report with no test
    # catching the drift. Pin its anchors here so a rename breaks THIS test in lockstep.
    # The #7 TOC-label check (`check_toc_also_noticed_label_honest`) keys on the blanket-off-path
    # pointer literals ("below the critical path", "~0 wall-clock") and the reframed on-path phrase
    # ("DO sit on the critical path"); a renderer reword would silently flip its verdict. Pin those too.
    # The Data sources `job logs`-row honesty check (`_job_logs_count_violation`) keys on the row
    # LABEL the renderer emits ("job logs") and the count phrasing ("job log(s) sampled"); a reword
    # of either in `_data_sources_footer` would silently turn the re-derivation into a SKIP (the cell
    # would no longer match `_DS_JOB_LOGS_ROW_RE` / the count regex), reading clean. Pin both so a
    # rename breaks THIS test in lockstep instead.
    for literal in ("sits ON the merge-gating critical path",
                    "check a typical PR waits on",
                    "No per-step breakdown was captured",
                    "editable workflow file",
                    "biggest single measured win is",
                    "What a change here can buy",
                    "it gates until it drops to the next concurrent check",
                    # The chain-member note forms (#220): cell 3d keys its bound on
                    # the "up to **~<clock>**" figure and treats "~0s for now" as the
                    # only figure-less compliant form; 3c keys on "gate chain" in the
                    # role claim. A renderer reword must break here in lockstep, not
                    # silently un-bound the member notes.
                    "gate chain",
                    "~0s for now",
                    "below the critical path",
                    "~0 wall-clock",
                    "DO sit on the critical path",
                    "job logs",
                    "job log(s) sampled",
                    # The frequency-gate role line's floor disclosure — `_SPINE_FLOOR_NAME_RE` keys on
                    # this phrasing to recognize the pole's floor named on the spine; a renderer reword
                    # would silently reopen the wording-coupled false FAIL. Pin both sides in lockstep.
                    "slowest concurrent check is",
                    # The headline floor-reconciliation clause (form 1b AND the form-2 floor-lowered
                    # branch) — `check_headline_floor_presence_reconciled` matches the presence phrase to
                    # confirm a non-universal slowest check discloses its presence. A renderer reword
                    # would silently turn the re-derivation into a false FAIL on every lowered-floor
                    # report; pin both sides so a rename breaks THIS test in lockstep. ("PR finishes in"
                    # is the contiguous fragment — the full clause spans the renderer's f-string lines.)
                    "PR finishes in",
                    # `check_measured_cause_matches_rendered_timeline` locates each pole's agent
                    # prompt by the "🤖 Prompt for your coding agent" header and its cause by the
                    # "THE MEASURED CAUSE" bullet. If the renderer reworded either, the check would
                    # silently degrade to a permanent SKIP on real reports (it fires only on the
                    # rare reintroduced over-claim, so the drift would go unnoticed). Pin both.
                    "🤖 Prompt for your coding agent",
                    "THE MEASURED CAUSE",
                    # `check_gap_fill_evidence_grounded` (issue #106) locates each gap-fill block by
                    # the `_llm_analysis_block` heading and its evidence fence by the heading line. A
                    # renderer reword of either would silently turn the grounding gate into a
                    # permanent no-op (it fires only on a rare gap-fill pole), so pin both fragments —
                    # the em-dash-free "verbatim from the captured job log" is the contiguous one.
                    "🤖 LLM root-cause analysis",
                    "verbatim from the captured job log",
                    # The aggregation-gate role line + upstream pointer (issue #1).
                    # `check_aggregation_gate_poles_never_prescribe` finds such a pole by the role
                    # marker and asserts the pointer is present; `check_speed_poles_complete`
                    # exempts it from the prompt requirement on the same marker. A renderer reword
                    # would silently drop BOTH — the exemption (false FAIL on a correct report)
                    # and the invariant (a permanent no-op).
                    "**Aggregation gate",
                    "**➡️ Where the wait actually is:**"):
        assert literal in renderer, f"renderer no longer emits {literal!r} — update verify_report"
        assert literal in verifier, f"verify_report lost the {literal!r} coupling literal"


def _head_short_sha() -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


# A minimal well-formed blocking-path (speed) report that passes every report-only
# check exercised here. `SKILLSHA` is substituted per-test (HEAD for the provenance
# case; a placeholder elsewhere, where --skill-repo isn't passed so the check skips).
# The ```text fences are real (the prompt + waterfalls render in fenced blocks).
_GOOD_TMPL = """# demo - why is the merge slow?

> **Bottom line.** A typical PR waits **4m 15s** for all checks to finish; the per-pole drill-downs below trace where that time goes.

## 📋 Contents

1. [`build`](#pole-1) - 4m 15s

> **Where this data comes from**
>
> - Critical path scanned **2026-05-29**.

<a id="pole-1"></a>

## Long pole 1: `ci.yml` ▸ build - 4m 15s

```text
Where the job's ~4m 15s goes - every step, slowest first.
```

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the root cause below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.
```

## 🗄️ Data sources

| Source | Coverage | Used for |
| --- | --- | --- |
| ci-speedup static scan (skill commit `SKILLSHA`) | all workflows | Static pattern detection |
"""


def _good(head: str = "0000000") -> str:
    return _GOOD_TMPL.replace("SKILLSHA", head)


# Check-name substrings (must match verify_report.Check.name verbatim).
_DASH = "ASCII hyphens only"
_PRIMARY = "primary section present"
_HEADLINE = "headline names the wall-clock (merge-wait) axis"
_ANCHORS = "reference resolves to an anchor"
_RCA = "RCA hands off via prompts"
_ALSO = "'Also noticed' count"
_COVERAGE = "data basis disclosed"
_LEAK = "no ci-secure template leakage"
_PROVENANCE = "provenance"
_PATTERNS = "every rendered finding pattern exists"
_OFFSPINE = "no spine-dropped check is also framed on the merge-gating critical path"
_DRILLOWN = "drill belongs to its own job"
_STRUCT_STEP = "every file-backed structural lever carries a measured dominant step"
_PAYLOAD_BUILD = "no payload-bearing step is binned as `build`"
_CEILING = "ceiling within the co-occurrence floor"
_SPINE_DROP = "binding floor is disclosed on the spine"
_DOUBLE_FRAME = "minor-cleanup finding"
_TOC_LABEL = "on/off-path label matches the appendix"


def _toc_also_pointer(blanket: bool = True) -> str:
    if blanket:
        return ("\n**🧹 Also noticed** - 3 off-path hygiene findings that save runner-minutes "
                "(~0 wall-clock), below the critical path: [see below](#also-noticed).\n")
    return ("\n**🧹 Also noticed** - 3 findings (mostly off-path runner-minute savings; **one or "
            "more flagged DO sit on the critical path** and cut wall-clock): [see below](#also-noticed).\n")


def _also_section(on_path: bool) -> str:
    note = ("\n**Wall-clock:** this one sits ON the merge-gating critical path (a long pole).\n"
            if on_path else "")
    return ("\n## 🧹 Also noticed - residual hygiene\n\n"
            "<details>\n<summary><strong>OPT24 - Long Test Job</strong></summary>\n" + note + "\n</details>\n")


def test_toc_also_noticed_label_discriminator(tmp_path: Path):
    # Class A #7: the TOC pointer must not blanket-label the section off-path when the appendix holds
    # an on-path wall-clock lever.
    rep = _good()
    F = _TOC_LABEL
    # FAIL — blanket "below the critical path" pointer but the appendix has an on-path lever.
    assert _tag_for(rep + _toc_also_pointer(blanket=True) + _also_section(on_path=True), F, tmp_path) == "FAIL"
    # PASS — reframed pointer acknowledging the on-path lever.
    assert _tag_for(rep + _toc_also_pointer(blanket=False) + _also_section(on_path=True), F, tmp_path) == "PASS"
    # SKIP — appendix has NO on-path lever, so the blanket off-path pointer is correct.
    assert _tag_for(rep + _toc_also_pointer(blanket=True) + _also_section(on_path=False), F, tmp_path) == "SKIP"
    # FAIL — REVERSE mislabel: the pointer claims on-path but the appendix has no on-path lever.
    assert _tag_for(rep + _toc_also_pointer(blanket=False) + _also_section(on_path=False), F, tmp_path) == "FAIL"
    # SKIP — no TOC 'Also noticed' pointer at all.
    assert _tag_for(rep + _also_section(on_path=True), F, tmp_path) == "SKIP"


def test_also_noticed_count_honest_fires_on_reframed_pointer(tmp_path: Path):
    # Regression guard for the #7 reframe: dropping the "off-path hygiene" token from the pointer must
    # NOT make `check_also_noticed_count_honest` silently SKIP. With a reframed "1 finding" pointer and
    # one appendix <details>, the count check must FIRE (PASS), not skip.
    pointer = ("\n**🧹 Also noticed** - 1 finding (mostly off-path runner-minute savings; **one or "
               "more flagged DO sit on the critical path** and cut wall-clock): [see below](#also-noticed).\n")
    tag = _tag_for(_good() + pointer + _also_section(on_path=True), _ALSO, tmp_path)
    assert tag == "PASS", f"count-honest must FIRE (not SKIP) on a reframed pointer, got {tag}"


def _also_noticed_section(where_wf: str, where_job: str,
                          summary: str = "OPT24 - Long Test Job Without Sharding") -> str:
    return ("\n## 🧹 Also noticed - residual hygiene\n\n"
            f"<details>\n<summary><strong>{summary}</strong> · no bill saving · HIGH · 1 across 1 wf</summary>\n\n"
            f"**Where:** `{where_wf}` ({where_job})\n\n</details>\n")


def _drilled_pole(check: str = "test (22.x)", wf: str = "ci.yml") -> str:
    return f'\n<a id="pole-2"></a>\n\n## 🔴 Long pole 2: `{wf}` ▸ {check} - 5m 35s\n\n_role_\n'


def _double_frame_findings(job: str = "test", wf: str = "ci.yml", rm=0.0, wc=0.5,
                           extra_jobs=()) -> dict:
    # findings.json with one OPT24 finding (the valueless/credited + all-pole/not criteria come from
    # HERE, not the rendered Where) matching the appendix block the report renders.
    return {"findings": [{"pattern": "OPT24", "runner_min_saving": rm, "wall_clock_p50_s": wc,
                          "affected_jobs": [job, *extra_jobs],
                          "workflow_file": f".github/workflows/{wf}"}]}


def test_pole_not_reframed_as_hygiene_discriminator(tmp_path: Path):
    # Class A #5: a VALUELESS finding whose jobs are ALL drilled poles must not appear in the "Also
    # noticed" appendix. Pole 2 is `test (22.x)` on `ci.yml` (base `test`); valueless/all-pole is read
    # from findings.json (`affected_jobs`), not the lossy rendered Where.
    rep = _good() + _drilled_pole("test (22.x)", "ci.yml")
    F = _DOUBLE_FRAME
    sect = _also_noticed_section("ci.yml", "test")          # appendix OPT24 block on (ci.yml, test)
    # FAIL — valueless OPT24 on the pole job `test`, rendered in the appendix.
    assert _tag_for(rep + sect, F, tmp_path, findings=_double_frame_findings("test")) == "FAIL"
    # PASS — appendix finding on a DIFFERENT (non-pole) job.
    assert _tag_for(rep + _also_noticed_section("ci.yml", "lint"), F, tmp_path,
                    findings=_double_frame_findings("lint")) == "PASS"
    # PASS — a CREDITED bill lever (rm>0) on the pole job: legitimate different-axis entry, not flagged.
    assert _tag_for(rep + sect, F, tmp_path, findings=_double_frame_findings("test", rm=120.0)) == "PASS"
    # PASS — a CREDITED wall-clock lever (wc >= 30s) on the pole job.
    assert _tag_for(rep + sect, F, tmp_path, findings=_double_frame_findings("test", wc=60.0)) == "PASS"
    # PASS — a multi-job finding that ALSO touches a NON-pole job is kept (not all-poles) — the
    # fivetran-airflow shape the lossy rendered Where mis-read as all-pole.
    assert _tag_for(rep + sect, F, tmp_path,
                    findings=_double_frame_findings("test", extra_jobs=("postgres-test",))) == "PASS"
    # SKIP — no "Also noticed" appendix at all.
    assert _tag_for(rep, F, tmp_path, findings=_double_frame_findings("test")) == "SKIP"


def _struct_findings(*findings: dict) -> dict:
    return {"pr_critical_path": {}, "findings": list(findings)}


def test_structural_pole_has_measured_step_discriminator(tmp_path: Path):
    # Pins all four arms of the OPT75-fabrication-class invariant's discriminator (the headline
    # deliverable). Mutation-testing showed the check could be made vacuous with the whole suite still
    # green — this is the missing direct unit test.
    rep = _good()
    F = _STRUCT_STEP
    # FAIL — a FILE-BACKED (.yml) structural lever with NO measured step (name-inferred OPT75).
    offender = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": None,
                                 "workflow_file": ".github/workflows/pr-title-check.yml"})
    assert _tag_for(rep, F, tmp_path, findings=offender) == "FAIL", "file-backed + no measured step must FAIL"
    # EXEMPT/PASS — file-backed + None wall-clock BUT a measured step (`decomposition`): a LEGITIMATE
    # bill-only finding (a slower concurrent check gates), not a name-inferred lever.
    billonly = _struct_findings({"pattern": "OPT70", "wall_clock_p50_s": None,
                                 "workflow_file": ".github/workflows/test.yml",
                                 "decomposition": {"dominant_step": "Build"}})
    assert _tag_for(rep, F, tmp_path, findings=billonly) == "PASS", "bill-only (measured step) must be exempt"
    # EXEMPT/PASS — GENUINELY-FILELESS: workflow_file is a bare app/check name, not a `.yml` path.
    fileless = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": None, "workflow_file": "CodeQL"})
    assert _tag_for(rep, F, tmp_path, findings=fileless) == "PASS", "genuinely-fileless must be exempt"
    # PASS — file-backed structural lever WITH a measured wall-clock.
    measured = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": 540.3,
                                 "workflow_file": ".github/workflows/android.yml",
                                 "decomposition": {"dominant_step": "Run Unit test"}})
    assert _tag_for(rep, F, tmp_path, findings=measured) == "PASS", "measured file-backed lever must PASS"
    # PASS — a non-None wall_clock exempts ON ITS OWN: the offender condition requires `wall_clock_p50_s
    # is None` FIRST, so a measured magnitude is a measured step regardless of a `decomposition` key. This
    # isolates that arm (the `measured` case above also carries a decomposition; this one deliberately
    # does NOT, so it can't pass merely via the decomposition exemption).
    measured_no_decomp = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": 120.0,
                                           "workflow_file": ".github/workflows/x.yml"})
    assert _tag_for(rep, F, tmp_path, findings=measured_no_decomp) == "PASS", "non-None wall_clock alone exempts"
    # SKIP — no structural (OPT70/72/75) findings at all → not applicable.
    nonstruct = _struct_findings({"pattern": "OPT12", "workflow_file": ".github/workflows/x.yml"})
    assert _tag_for(rep, F, tmp_path, findings=nonstruct) == "SKIP", "no structural findings → SKIP"
    # Predicate-unification guard: an UPPERCASE `.YML` extension is still file-backed (the checker
    # lowercases, matching the engine-side drop) — so it must still FAIL, not slip through.
    upper = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": None,
                              "workflow_file": ".github/workflows/PR-TITLE.YML"})
    assert _tag_for(rep, F, tmp_path, findings=upper) == "FAIL", "uppercase .YML is still file-backed"
    # The OTHER measured-step signal (`measured_evidence`) also exempts.
    me_exempt = _struct_findings({"pattern": "OPT75", "wall_clock_p50_s": None,
                                  "workflow_file": ".github/workflows/x.yml",
                                  "measured_evidence": "step table: ..."})
    assert _tag_for(rep, F, tmp_path, findings=me_exempt) == "PASS", "measured_evidence also exempts"


def test_structural_step_category_payload_binned_as_build_discriminator(tmp_path: Path):
    # The payload-binned-as-build class (nrwl/nx `Run Checks/Lint/Test/Build`): a `build`-category
    # dominant step whose NAME clearly runs payload work (test/lint/…) is the classifier binning
    # payload into the redundant-work numerator → OPT72 misroute. The `dominant_category` /
    # `dominant_step` ground truth is read from findings.json, and "is really payload" is re-derived
    # INDEPENDENTLY of the engine classifier.
    rep = _good()
    F = _PAYLOAD_BUILD
    # FAIL — a combined step binned `build` whose name carries payload tokens (Test / Lint).
    offender = _struct_findings({"pattern": "OPT72", "workflow_file": ".github/workflows/ci.yml",
                                 "decomposition": {"dominant_category": "build",
                                                   "dominant_step": "Run Checks/Lint/Test/Build + 1 more build step"}})
    assert _tag_for(rep, F, tmp_path, findings=offender) == "FAIL", "payload-named build step must FAIL"
    # PASS — a PURE build step (no payload token) legitimately binned `build`.
    pure = _struct_findings({"pattern": "OPT72", "workflow_file": ".github/workflows/ci.yml",
                             "decomposition": {"dominant_category": "build",
                                               "dominant_step": "Build production bundle"}})
    assert _tag_for(rep, F, tmp_path, findings=pure) == "PASS", "pure build step must PASS"
    # PASS — the SAME payload-named step, correctly binned `test` (the post-fix classification).
    fixed = _struct_findings({"pattern": "OPT75", "workflow_file": ".github/workflows/ci.yml",
                              "decomposition": {"dominant_category": "test",
                                                "dominant_step": "Run Checks/Lint/Test/Build + 1 more build step"}})
    assert _tag_for(rep, F, tmp_path, findings=fixed) == "PASS", "payload step binned test must PASS"
    # PASS — scoped to `build`: a `checkout` step mentioning "test" is NOT flagged (checkout's leading
    # signal legitimately wins, and it isn't the OPT72-keyed build category).
    ckout = _struct_findings({"pattern": "OPT70", "workflow_file": ".github/workflows/ci.yml",
                              "decomposition": {"dominant_category": "checkout",
                                                "dominant_step": "Checkout and run tests"}})
    assert _tag_for(rep, F, tmp_path, findings=ckout) == "PASS", "non-build category must not be flagged"
    # SKIP — no decomposition-bearing structural finding at all → not applicable.
    nodecomp = _struct_findings({"pattern": "OPT72", "workflow_file": ".github/workflows/ci.yml"})
    assert _tag_for(rep, F, tmp_path, findings=nodecomp) == "SKIP", "no decomposition → SKIP"


def test_payload_binned_as_build_literals_stay_coupled_to_the_engine(tmp_path: Path):
    # L7 coupling: `_VR_BUILD_CATEGORY` / `_VR_PAYLOAD_TOKEN_RE` mirror the engine. A category rename
    # or a payload-token drop in collect_runs must break THIS test, not silently un-couple the check.
    cr = _load_collect_runs()
    vr = _load_verify_report()
    assert vr._VR_BUILD_CATEGORY in cr._SETUP_BUILD_CATEGORIES, "'build' left the setup/build category set"
    assert "test" in cr._PAYLOAD_CATEGORIES and "scan" in cr._PAYLOAD_CATEGORIES
    # The engine must classify the nx combined step as PAYLOAD (the fix), so the invariant's payload
    # re-derivation agrees with the engine's crown on the same name.
    assert cr._step_category("Run Checks/Lint/Test/Build") in cr._PAYLOAD_CATEGORIES
    # Every token the verifier keys on must be a genuine payload signal in the engine.
    for token, name in (("test", "Run tests"), ("lint", "Lint"), ("e2e", "Run e2e"),
                        ("spec", "Run spec"), ("playwright", "playwright test")):
        assert vr._VR_PAYLOAD_TOKEN_RE.search(name), token
        assert cr._step_category(name) in cr._PAYLOAD_CATEGORIES, (token, name)


_LEAFCAT = "crowned detector leaf agrees with the pole's dominant measured category"


def _leaf_pole_section(fix_key: str, wf: str = "ci.yml", check: str = "Lint", n: int = 2) -> str:
    # A second Long-pole section carrying the crown marker the leaf-category invariant keys on.
    return (f'\n<a id="pole-{n}"></a>\n\n## 🔴 Long pole {n}: `{wf}` ▸ {check} - 5m 08s\n\n'
            f'<!-- ci-speedup:leaf-crown fix_key={fix_key} -->\n\n'
            '```text\nWhere the job time goes.\n```\n\n'
            '#### 🤖 Prompt for your coding agent\n\n```text\nprompt\n```\n')


def _leaf_findings(dom_cat: str, wf: str = "ci.yml", check: str = "Lint") -> dict:
    return {"pr_critical_path": {"poles": [
        {"workflow_file": f".github/workflows/{wf}", "check": check,
         "dominant_category": dom_cat, "dominant_step": "Run Checks/Lint/Test/Build"}]}}


def test_detector_leaf_agrees_with_dominant_category_discriminator(tmp_path: Path):
    # The off-category leaf-hijack class (issue #16, nrwl/nx): an `eslint-no-cache` (`scan`) leaf
    # crowning a `test`-dominant combined step pins the pole's ceiling on a lint fix the measured
    # data contradicts. `dominant_category` ground truth is read from findings.json; the crowned
    # leaf's category is re-derived from the crown marker's fix_key.
    F = _LEAFCAT
    # FAIL — an eslint (scan) leaf crowned on a TEST-dominant pole (the nx bug shape).
    rep = _good() + _leaf_pole_section("eslint-no-cache", check="Lint")
    assert _tag_for(rep, F, tmp_path, findings=_leaf_findings("test", check="Lint")) == "FAIL", \
        "scan leaf on a test-dominant pole must FAIL"
    # PASS — the SAME eslint leaf on a genuinely scan-dominant (lint) pole.
    assert _tag_for(rep, F, tmp_path, findings=_leaf_findings("scan", check="Lint")) == "PASS", \
        "scan leaf on a scan-dominant pole must PASS"
    # PASS — a build leaf (buildx-no-cache) on a build-dominant pole (agreement across categories).
    repb = _good() + _leaf_pole_section("buildx-no-cache", check="Docker build")
    assert _tag_for(repb, F, tmp_path, findings=_leaf_findings("build", check="Docker build")) == "PASS", \
        "build leaf on a build-dominant pole must PASS"
    # FAIL — a test leaf (playwright) crowned on a build-dominant pole.
    repp = _good() + _leaf_pole_section("playwright-parallel", check="E2E")
    assert _tag_for(repp, F, tmp_path, findings=_leaf_findings("build", check="E2E")) == "FAIL", \
        "test leaf on a build-dominant pole must FAIL"
    # SKIP — no crown marker anywhere (a report of only generic/undetected poles).
    assert _tag_for(_good(), F, tmp_path, findings=_leaf_findings("test")) == "SKIP", \
        "no crowned leaf → SKIP"
    # SKIP — no --findings to source the dominant_category ground truth.
    assert _tag_for(rep, F, tmp_path) == "SKIP", "no --findings → SKIP"


def test_leaf_category_map_stays_coupled_to_the_engine():
    # L7 coupling: the verifier's `_VR_LEAF_STEP_CATEGORY` mirror + crown-marker regex must track the
    # engine. A map edit, a fix_key rename, or a marker reword must break THIS test, not silently
    # un-couple the check.
    bp = _load_blocking_path()
    vr = _load_verify_report()
    cr = _load_collect_runs()
    assert vr._VR_LEAF_STEP_CATEGORY == bp._LEAF_STEP_CATEGORY, "verifier mirror drifted from the engine map"
    valid = set(cr._SETUP_BUILD_CATEGORIES) | set(cr._PAYLOAD_CATEGORIES)
    for fk, cat in bp._LEAF_STEP_CATEGORY.items():
        assert cat in valid, (fk, cat)          # every leaf category is a real _step_category bin
        assert fk in bp._FIX_META, fk           # every mapped fix_key is a real detector fix_key
    # Every detector fix_key that can crown a pole is categorized (no leaf escapes the guard).
    fix_keys = {m.group(1) for m in re.finditer(r'"fix_key":\s*"([^"]+)"',
                                                _BLOCKING_PATH.read_text(encoding="utf-8"))}
    assert fix_keys <= set(bp._LEAF_STEP_CATEGORY), fix_keys - set(bp._LEAF_STEP_CATEGORY)
    # The crown-marker literal is coupled: the engine's rendered marker must match the verifier regex.
    assert "ci-speedup:leaf-crown" in bp._LEAF_CROWN_MARKER
    assert vr._VR_LEAF_CROWN_RE.search(bp._LEAF_CROWN_MARKER.format(fk="eslint-no-cache"))


def _ceiling_findings(pole: str = "build", pole_p50: float = 600.0, floor: str = "test",
                      floor_p50: float = 400.0, n_gating: int = 6,
                      floor_on: int | None = None,
                      floor_wf: str = ".github/workflows/test.yml") -> dict:
    """findings whose `populations` make `pole` the per-PR slowest on `n_gating` PRs, with `floor`
    co-occurring on `floor_on` of them (default all). The floor's VALUE is its `checks[].p50_s`.
    `floor_wf` defaults to a DIFFERENT workflow than the pole's `ci.yml`; pass `ci.yml` to model a
    matrix sibling leg (same base + same workflow — the engine's `_same_matrix` shape)."""
    floor_on = n_gating if floor_on is None else floor_on
    checks = [{"name": pole, "p50_s": pole_p50, "workflow_file": ".github/workflows/ci.yml"},
              {"name": floor, "p50_s": floor_p50, "workflow_file": floor_wf}]
    pops = []
    for i in range(n_gating):
        row = [[pole, pole_p50]]
        if i < floor_on:
            row.append([floor, min(floor_p50, pole_p50 - 1)])   # present, but below the pole
        pops.append([0.05, row])
    return {"pr_critical_path": {"checks": checks, "populations": pops}}


def _ceil_headline(check: str, dur: str) -> str:
    return ("\n> **Bottom line.** A typical PR waits **10m** for all checks to finish. The biggest "
            f"single measured win is **~{dur}** off the slowest fixable check, `{check}` - see "
            "[Long pole 1](#pole-1) for the drill-down to the biggest lever.\n")


def _ceil_floor_note(check: str, dur: str, floor_name: str = "unit", floor_dur: str = "6m 40s") -> str:
    # A Long-pole section whose body carries the SIMPLE-form floor note (the per-pole ceiling path,
    # distinct from the headline path). The `▸` header + the exact note phrasing are what
    # `_pole_header_sections` + `_CEILING_FLOOR_NOTE_RE` parse.
    return (f'\n<a id="pole-2"></a>\n\n## 🔴 Long pole 2: `ci.yml` ▸ {check} - 10m 00s\n\n'
            f"> **What a change here can buy (wall-clock):** up to **~{dur}** - it gates until it "
            f"drops to the next concurrent check, `{floor_name}` ({floor_dur}); below that the gate "
            "moves and further savings are runner-minutes, not wall-clock.\n")


def test_ceiling_check_per_pole_floor_note_and_majority_boundary(tmp_path: Path):
    # The per-pole SIMPLE floor-note path (not just the headline) must be bound — and going through
    # `_tag_for` exercises the live `_CEILING_FLOOR_NOTE_RE` against rendered-shaped text, so a regex
    # drift that breaks the match turns this FAIL into a SKIP and fails the test (drift guard).
    rep = _good()
    F = _CEILING
    over = _ceiling_findings(pole="integration", pole_p50=600.0, floor="unit", floor_p50=400.0, n_gating=6)
    # FAIL — the floor note claims ~9m (540s) but the co-occurrence floor caps it at ~200s.
    assert _tag_for(rep + _ceil_floor_note("integration", "9m 00s"), F, tmp_path, findings=over) == "FAIL"
    # PASS — an honest ~3m (180s) floor-note claim is within bound.
    assert _tag_for(rep + _ceil_floor_note("integration", "3m 00s"), F, tmp_path, findings=over) == "PASS"
    # STRICT-MAJORITY boundary (the definitional crux): the floor co-occurring on EXACTLY half the
    # gating PRs must NOT qualify (no floor → full pole addressable → ~9m passes); one more does.
    half = _ceiling_findings(pole="integration", floor="unit", n_gating=6, floor_on=3)   # 3/6 — not a majority
    assert _tag_for(rep + _ceil_floor_note("integration", "9m 00s"), F, tmp_path, findings=half) == "PASS"
    maj = _ceiling_findings(pole="integration", floor="unit", n_gating=6, floor_on=4)     # 4/6 — a majority
    assert _tag_for(rep + _ceil_floor_note("integration", "9m 00s"), F, tmp_path, findings=maj) == "FAIL"


def test_ceiling_check_discriminator(tmp_path: Path):
    # Pins all arms of the #4 addressable-ceiling invariant (Class A archetype). Floor `test` (400s)
    # co-occurs on a majority of `build`'s gating PRs, so the real win is pole_p50 − floor = 200s.
    rep = _good()
    F = _CEILING
    # FAIL — headline claims ~9m (540s) but the co-occurrence floor caps it at ~200s (+tol).
    over = _ceiling_findings(n_gating=6)
    assert _tag_for(rep + _ceil_headline("build", "9m 00s"), F, tmp_path, findings=over) == "FAIL"
    # PASS — same data, an honest ~3m (180s) claim is within the 200s ceiling (+tol).
    assert _tag_for(rep + _ceil_headline("build", "3m 00s"), F, tmp_path, findings=over) == "PASS"
    # PASS — the slow check does NOT co-occur on a majority (2/6), so it can't floor: the whole pole
    # is addressable and even ~9m is within bound (no false positive on a non-co-occurring sibling).
    sparse = _ceiling_findings(n_gating=6, floor_on=2)
    assert _tag_for(rep + _ceil_headline("build", "9m 00s"), F, tmp_path, findings=sparse) == "PASS"
    # SKIP — the pole gates too few PRs (<5) for a stable floor: thin-sample guard, not a false FAIL.
    thin = _ceiling_findings(n_gating=3)
    assert _tag_for(rep + _ceil_headline("build", "9m 00s"), F, tmp_path, findings=thin) == "SKIP"
    # SKIP — no per-PR populations to re-derive the floor from.
    nopop = {"pr_critical_path": {"checks": [{"name": "build", "p50_s": 600.0}]}}
    assert _tag_for(rep + _ceil_headline("build", "9m 00s"), F, tmp_path, findings=nopop) == "SKIP"
    # SKIP — no findings at all.
    assert _tag_for(rep + _ceil_headline("build", "9m 00s"), F, tmp_path) == "SKIP"


_DOOR = "carries a measured-basis stamp"
_SIBLING = "floors at a NON-sibling check"


def _door_findings(saving, basis=None, spine_ready=True) -> dict:
    f = {"pattern": "OPT73", "workflow_file": ".github/workflows/ci.yml",
         "affected_jobs": ["e2e-a"], "runner_min_saving": saving}
    if basis is not None:
        f["runner_min_basis"] = basis
    spine = {"render_ready": spine_ready,
             "rows": [{"workflow_file": ".github/workflows/ci.yml",
                       "job_name": "e2e-a", "billable_equiv_min_per_month": 300.0}]}
    return {"findings": [f], "runner_minute_spine": spine}


def test_saving_carries_measured_basis_discriminator(tmp_path: Path):
    # The sizing-door teeth (#43/#44/#45): under a render-ready spine, every credited
    # runner-minute saving must carry a recognized runner_min_basis stamp.
    rep = _good()
    F = _DOOR
    # FAIL — a positive saving with NO basis (a pattern that skipped the door).
    assert _tag_for(rep, F, tmp_path, findings=_door_findings(500.0, basis=None)) == "FAIL"
    # FAIL — the loud UNCLASSIFIED sentinel (a rm-crediting pattern with no declared policy).
    assert _tag_for(rep, F, tmp_path,
                    findings=_door_findings(500.0, basis="UNCLASSIFIED_door_policy")) == "FAIL"
    # FAIL — an unrecognized basis value.
    assert _tag_for(rep, F, tmp_path, findings=_door_findings(500.0, basis="handwaved")) == "FAIL"
    # PASS — a clamped saving with a recognized measured basis.
    assert _tag_for(rep, F, tmp_path,
                    findings=_door_findings(300.0, basis="measured_spine_clamped")) == "PASS"
    # PASS — the not-derivable whitelist basis is recognized.
    assert _tag_for(rep, F, tmp_path,
                    findings=_door_findings(300.0, basis="not_spine_derivable")) == "PASS"
    # SKIP — no render-ready spine, so the door doesn't run and there's no basis to require.
    assert _tag_for(rep, F, tmp_path,
                    findings=_door_findings(500.0, basis=None, spine_ready=False)) == "SKIP"
    # SKIP — no positive savings to bind.
    assert _tag_for(rep, F, tmp_path, findings=_door_findings(0.0, basis=None)) == "SKIP"


def _sibling_findings(floor_reason, from_s=639.0, to_s=40.5,
                      affected=("rspec (1)", "rspec (2)", "rspec (3)")) -> dict:
    return {"findings": [{
        "pattern": "OPT73", "workflow_file": ".github/workflows/test.yml",
        "affected_jobs": list(affected),
        "wall_clock_derivation": [
            {"bound": "measured-critical-path", "from_s": from_s, "to_s": to_s,
             "reason": floor_reason}]}]}


def test_cluster_lever_ceiling_escapes_sibling_discriminator(tmp_path: Path):
    # #44: an OPT73 cluster-floor lever must not be capped by one of its own matrix
    # sibling legs (which descend with the fix).
    rep = _good()
    F = _SIBLING
    # FAIL — the measured floor cap names a SIBLING leg (`rspec (2)`) as the gating floor.
    sib = "wall-clock capped at the measured critical-path floor — `rspec (2)` (800s) is a slower concurrent check"
    assert _tag_for(rep, F, tmp_path, findings=_sibling_findings(sib)) == "FAIL"
    # PASS — the floor is a NON-sibling check (`Elastic Search`), the correct cluster-aware floor.
    non = "wall-clock capped at the measured critical-path floor — `Elastic Search` (202s) is a slower concurrent check"
    assert _tag_for(rep, F, tmp_path, findings=_sibling_findings(non)) == "PASS"
    # SKIP — no OPT73 finding carries a measured-critical-path derivation entry.
    assert _tag_for(rep, F, tmp_path,
                    findings={"findings": [{"pattern": "OPT13", "affected_jobs": ["x"]}]}) == "SKIP"


def test_cluster_lever_unparseable_cap_is_loud_skip_not_green(tmp_path: Path):
    # A measured-critical-path cap EXISTS but its floor check couldn't be recovered
    # (reason names no backticked check — format drift). The check must NOT count it
    # as a verified non-offender PASS; it is a "couldn't check" LOUD SKIP.
    vr = _load_verify_report()
    rep = _good()
    # A cap with NO backtick name in the reason, and a missing-from_s variant.
    no_name = _sibling_findings("wall-clock capped at the measured critical-path floor (no names)")
    fp = tmp_path / "f.json"
    fp.write_text(json.dumps(no_name), encoding="utf-8")
    c = vr.check_cluster_lever_ceiling_escapes_sibling(rep, fp)
    assert c.skipped and "could NOT be bounded" in c.detail
    # A parseable sibling cap still FAILs even alongside an unparseable one.
    mixed = {"findings": [
        _sibling_findings("floor (no names)")["findings"][0],
        _sibling_findings("floored at `rspec (2)` (800s)")["findings"][0]]}
    fp2 = tmp_path / "f2.json"
    fp2.write_text(json.dumps(mixed), encoding="utf-8")
    c2 = vr.check_cluster_lever_ceiling_escapes_sibling(rep, fp2)
    assert not c2.ok and not c2.skipped


def test_ceiling_short_sample_skip_is_loud(tmp_path: Path):
    # #45 (D-ii): a rendered addressable-ceiling claim with NO per-PR populations to
    # bound it is a LOUD, NARROW skip that NAMES the unbounded claim — not a silent
    # clean pass. (Still SKIP, but the detail surfaces the coverage gap.)
    vr = _load_verify_report()
    rep = _good() + _ceil_headline("build", "9m 00s")
    nopop = {"pr_critical_path": {"checks": [{"name": "build", "p50_s": 600.0}]}}
    fp = tmp_path / "f.json"
    fp.write_text(json.dumps(nopop), encoding="utf-8")
    c = vr.check_pole_ceiling_within_cooccurrence(rep, fp)
    assert c.skipped and "could NOT be bounded" in c.detail and "headline win" in c.detail
    # No rendered ceiling at all → a genuine nothing-to-do skip (not a coverage gap).
    fp2 = tmp_path / "f2.json"
    fp2.write_text(json.dumps(nopop), encoding="utf-8")
    c2 = vr.check_pole_ceiling_within_cooccurrence(_good(), fp2)
    assert c2.skipped and "no rendered addressable ceiling" in c2.detail


def _gate_freq_findings(wf: str = "ci.yml", legs=(("leg-a", 3), ("leg-b", 2)), npop: int = 10) -> dict:
    """findings where workflow `wf` holds the per-PR pole via different legs on different PRs (so its
    true gate frequency is the SUM over legs), and an `other.yml` check gates the remaining PRs."""
    checks = [{"name": nm, "p50_s": 100.0, "workflow_file": f".github/workflows/{wf}"} for nm, _ in legs]
    checks.append({"name": "other", "p50_s": 200.0, "workflow_file": ".github/workflows/other.yml"})
    pops = []
    for nm, cnt in legs:
        for _ in range(cnt):
            pops.append([0.1, [[nm, 100.0], ["other", 10.0]]])   # this leg slowest (100 > 10)
    while len(pops) < npop:
        pops.append([0.1, [["other", 200.0]]])                   # `other` gates the rest
    return {"pr_critical_path": {"checks": checks, "populations": pops}}


def _gate_freq_claim(wf: str, n: int, m: int) -> str:
    return f"\n1. 🔴 [`leg-a`](#pole-1) - 5m · `{wf}` gates {n}/{m} PRs\n"


def test_gate_frequency_check_discriminator(tmp_path: Path):
    # Class A #2: the rendered "`wf` gates N/M PRs" must equal the populations re-derivation (summed
    # over ALL the workflow's checks/legs). True freq for ci.yml = 3+2 = 5 of 10 PRs.
    rep = _good()
    F = "per-workflow gate frequency"
    f = _gate_freq_findings()
    assert _tag_for(rep + _gate_freq_claim("ci.yml", 5, 10), F, tmp_path, findings=f) == "PASS"
    # FAIL — undercounts a leg (the matrix-sibling bug: 4 instead of 5).
    assert _tag_for(rep + _gate_freq_claim("ci.yml", 4, 10), F, tmp_path, findings=f) == "FAIL"
    # FAIL — wrong denominator (M != npop).
    assert _tag_for(rep + _gate_freq_claim("ci.yml", 5, 9), F, tmp_path, findings=f) == "FAIL"
    # SKIP — no findings to re-derive against.
    assert _tag_for(rep + _gate_freq_claim("ci.yml", 5, 10), F, tmp_path) == "SKIP"
    # PASS — MONOREPO scope-collision must NOT double-count: two DISTINCT scoped checks `@a/build` +
    # `@b/build` in one workflow (slowest on 3 + 2 PRs → true 5/10). Re-deriving by raw name keeps them
    # distinct; `_cmp_name` would collapse both to `build` and wrongly re-derive 10/10 → false FAIL.
    scoped = _gate_freq_findings(wf="ci.yml", legs=(("@a/build", 3), ("@b/build", 2)), npop=10)
    assert _tag_for(rep + _gate_freq_claim("ci.yml", 5, 10), F, tmp_path, findings=scoped) == "PASS"


def _bare_pole_section(check: str) -> str:
    # A drilled Long-pole section with NO floor note (so the pole's binding floor is named nowhere
    # in IT — disclosure must come from elsewhere or the #6 check fires).
    return f'\n<a id="pole-2"></a>\n\n## 🔴 Long pole 2: `ci.yml` ▸ {check} - 10m 00s\n\n_role_\n'


def test_spine_heavy_check_disclosure_discriminator(tmp_path: Path):
    # Pins the #6 invariant: a drilled pole's binding floor (a heavy concurrent check on a majority
    # of its gating PRs) must be disclosed on the SPINE, not silently dropped.
    rep = _good()
    F = _SPINE_DROP
    f = _ceiling_findings(pole="deploy", pole_p50=1000.0, floor="heavy", floor_p50=780.0, n_gating=6)
    # FAIL — `deploy` is drilled, its binding floor `heavy` (780s, on 6/6) is named NOWHERE on the spine.
    assert _tag_for(rep + _bare_pole_section("deploy"), F, tmp_path, findings=f) == "FAIL"
    # PASS — same data, but the pole's floor note NAMES `heavy` (disclosed on the spine).
    assert _tag_for(rep + _ceil_floor_note("deploy", "3m 40s", floor_name="heavy", floor_dur="13m 00s"),
                    F, tmp_path, findings=f) == "PASS"
    # PASS — `heavy` disclosed via the "Also slower" minority footnote instead of a floor note.
    also = rep + _bare_pole_section("deploy") + "\n> Also slower on **some** of the sampled PRs: `heavy` (~13m).\n"
    assert _tag_for(also, F, tmp_path, findings=f) == "PASS"
    # PASS — disclosed via the MANAGED-CAP floor-note variant ("no workflow file to speed up here, `X`").
    mgd = (rep + _bare_pole_section("deploy") + "\n> a concurrent check with no workflow file to "
           "speed up here, `heavy` (13m 00s), caps it there.\n")
    assert _tag_for(mgd, F, tmp_path, findings=f) == "PASS"
    # PASS — disclosed via the MATRIX two-number variant ("toward the next check, `X`").
    mtx = (rep + _bare_pole_section("deploy") + "\n> drops the whole matrix toward the next check, "
           "`heavy` (13m 00s), for up to **~5m** of merge wait.\n")
    assert _tag_for(mtx, F, tmp_path, findings=f) == "PASS"
    # PASS — disclosed via the FREQUENCY-GATE role line ("**The check most PRs gate on.** … the
    # slowest concurrent check is `X`"). This phrasing was previously unrecognized by
    # _SPINE_FLOOR_NAME_RE, so a legitimately-disclosed floor read as a silent spine drop — the
    # wording-coupled false FAIL found on out-of-sample dogfood repos (alibaba/page-agent).
    role = (rep + _bare_pole_section("deploy") + "\n**The check most PRs gate on.** A typical PR "
            "waits on this most often; the slowest concurrent check is `heavy` (~13m 00s), which "
            "sets the wall-clock floor.\n")
    assert _tag_for(role, F, tmp_path, findings=f) == "PASS"
    # SKIP — the binding floor is a MATRIX SIBLING LEG (same base AND same workflow `ci.yml`) → out of
    # #6 scope, not a silent drop. (The bare pole header renders `ci.yml`, so the floor shares it.)
    sib = _ceiling_findings(pole="test (3.12)", pole_p50=1000.0, floor="test (3.13)", floor_p50=780.0,
                            n_gating=6, floor_wf=".github/workflows/ci.yml")
    assert _tag_for(rep + _bare_pole_section("test (3.12)"), F, tmp_path, findings=sib) == "SKIP"
    # FAIL — same base but a DIFFERENT workflow file: a distinct job, NOT a matrix sibling, so the
    # name-only heuristic must NOT silently exclude it (the tightened workflow-corroborated check).
    distinct = _ceiling_findings(pole="lint (3.12)", pole_p50=1000.0, floor="lint (3.13)", floor_p50=780.0,
                                 n_gating=6, floor_wf=".github/workflows/other.yml")
    assert _tag_for(rep + _bare_pole_section("lint (3.12)"), F, tmp_path, findings=distinct) == "FAIL"
    # SKIP — no binding floor (the heavy check co-occurs on only a minority of gating PRs).
    sparse = _ceiling_findings(pole="deploy", floor="heavy", n_gating=6, floor_on=2)
    assert _tag_for(rep + _bare_pole_section("deploy"), F, tmp_path, findings=sparse) == "SKIP"


def _tag_for(report: str, check_substr: str, tmp_path: Path,
             skill_repo: str | None = None, findings: dict | None = None) -> str:
    """Run verify_report's CLI on `report` and return the PASS/FAIL/SKIP tag of the
    line whose name contains `check_substr`."""
    rp = tmp_path / "report-2026-05-29.md"
    rp.write_text(report, encoding="utf-8")
    cmd = [sys.executable, str(_VERIFY), "--report", str(rp)]
    if skill_repo:
        cmd += ["--skill-repo", skill_repo]
    if findings is not None:
        fp = tmp_path / "findings.json"
        fp.write_text(json.dumps(findings), encoding="utf-8")
        cmd += ["--findings", str(fp)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if check_substr in ln:
            return ln.split(None, 1)[0]  # leading PASS / FAIL / SKIP token
    raise AssertionError(f"no check line matched {check_substr!r} in:\n{out}")


def test_clean_report_passes_all_report_only_checks(tmp_path: Path):
    rep = _good()
    for sub in (_DASH, _PRIMARY, _HEADLINE, _ANCHORS, _RCA, _COVERAGE, _LEAK):
        assert _tag_for(rep, sub, tmp_path) == "PASS", sub


def _ds_job_logs_row(cov: str) -> str:
    # Append a `job logs` row (coverage cell `cov`) to the _good() Data sources table.
    return _good() + f"| job logs | {cov} | Step internals + cross-run magnitude (deeper levels) |\n"


def _logs_findings(logs_fetched: int | None = None, bundle: int = 0) -> dict:
    ds: dict = {"tiers_run": ["gh-timing", "job-logs"]}
    if logs_fetched is not None:
        ds["logs_fetched"] = logs_fetched
    return {"data_sources": ds, "data_bundle": {"logs": [{"i": i} for i in range(bundle)]}}


def test_data_sources_job_logs_count_honest_discriminator(tmp_path: Path):
    # The provenance-count honesty class (Tesorio/django-anon): the Data sources `job logs` Coverage
    # cell must faithfully reflect how many job logs were actually fetched, re-derived from findings
    # (`data_sources.logs_fetched`, or the `data_bundle.logs` manifest when absent). Folded into the
    # `check_coverage_disclosed` provenance-honesty check; targets its `_COVERAGE` line.
    F = _COVERAGE
    # FAIL — logs_fetched=0 but the cell reads the bare "fetched" (asserts a fetch that never happened).
    assert _tag_for(_ds_job_logs_row("fetched"), F, tmp_path, findings=_logs_findings(0)) == "FAIL"
    # PASS — a genuine zero rendered honestly as "none" (the engine fix).
    assert _tag_for(_ds_job_logs_row("none"), F, tmp_path, findings=_logs_findings(0)) == "PASS"
    # PASS — a positive count rendered faithfully (the common, already-correct case).
    assert _tag_for(_ds_job_logs_row("2 job log(s) sampled"), F, tmp_path, findings=_logs_findings(2)) == "PASS"
    # FAIL — the cell claims a count that disagrees with the fetched count (exact-int check, L6).
    assert _tag_for(_ds_job_logs_row("3 job log(s) sampled"), F, tmp_path, findings=_logs_findings(2)) == "FAIL"
    # PASS — logs_fetched absent → falls back to the data_bundle.logs manifest (2), matching the cell.
    assert _tag_for(_ds_job_logs_row("2 job log(s) sampled"), F, tmp_path,
                    findings=_logs_findings(None, bundle=2)) == "PASS"
    # FAIL — logs_fetched absent, bundle empty (0), yet the cell still asserts "fetched".
    assert _tag_for(_ds_job_logs_row("fetched"), F, tmp_path, findings=_logs_findings(None, bundle=0)) == "FAIL"
    # PASS — tier not run: the cell reads "not run"; the sub-check skips, disclosure still passes.
    assert _tag_for(_ds_job_logs_row("not run"), F, tmp_path, findings=_logs_findings(0)) == "PASS"
    # PASS — no --findings to cross-check; the report-only disclosure check still passes.
    assert _tag_for(_ds_job_logs_row("fetched"), F, tmp_path) == "PASS"
    # PASS — no `job logs` row at all; disclosure alone passes.
    assert _tag_for(_good(), F, tmp_path, findings=_logs_findings(0)) == "PASS"


_PREFETCH = "no unconsumed prefetches"


def test_prefetch_unconsumed_fails_and_requires_disclosure(tmp_path: Path):
    # R5: a non-zero `data_sources.prefetch_unconsumed` means the parallel gh pass paid for
    # calls the serial path never made (a fetch plan drifted from its call site). It must
    # fail the report AND be disclosed in the rendered artifact, not buried in stderr.
    F = _PREFETCH
    drifted = {"data_sources": {"prefetch_unconsumed": 3}}
    # FAIL — drift recorded but the report is silent about it.
    assert _tag_for(_good(), F, tmp_path, findings=drifted) == "FAIL"
    # FAIL — even a report that mentions the count loosely still fails (drift is a bug).
    disclosed = _good() + ("\n> - **⚠️ Prefetch drift:** 3 gh response(s) were fetched but "
                           "never consumed — a fetch plan disagreed with its call site.\n")
    assert _tag_for(disclosed, F, tmp_path, findings=drifted) == "FAIL"
    # PASS — a clean run (every prefetch consumed): the healthy, common case.
    assert _tag_for(_good(), F, tmp_path,
                    findings={"data_sources": {"prefetch_unconsumed": 0}}) == "PASS"
    # SKIP — a pre-stamp artifact with no key must never read as a clean zero.
    assert _tag_for(_good(), F, tmp_path, findings={"data_sources": {}}) == "SKIP"


def test_data_sources_gh_error_count_requires_rendered_disclosure(tmp_path: Path):
    F = _COVERAGE
    findings = {"data_sources": {"gh_error_count": 1}}
    # FAIL — findings recorded a failed gh call but the report's provenance is silent.
    assert _tag_for(_good(), F, tmp_path, findings=findings) == "FAIL"
    disclosed = (
        _good()
        + "\n**Data freshness.** 1 gh API call(s) failed during collection, so "
        "a few runs/jobs are absent from the sample.\n"
    )
    assert _tag_for(disclosed, F, tmp_path, findings=findings) == "PASS"
    assert _tag_for(_good(), F, tmp_path,
                    findings={"data_sources": {"gh_error_count": 0}}) == "PASS"


def test_dash_check_fails_on_each_typographic_dash(tmp_path: Path):
    for glyph in ("—", "–", "−"):  # em, en, minus
        bad = _good() + f"\nA range like 30{glyph}60s slipped through.\n"
        assert _tag_for(bad, _DASH, tmp_path) == "FAIL"
    assert _tag_for(_good(), _DASH, tmp_path) == "PASS"


def test_primary_section_fails_without_a_long_pole(tmp_path: Path):
    bad = _good().replace("## Long pole 1: `ci.yml` ▸ build - 4m 15s",
                          "## Some other section")
    assert _tag_for(bad, _PRIMARY, tmp_path) == "FAIL"


def test_headline_fails_without_a_wall_clock_axis(tmp_path: Path):
    bad = _good().replace(
        "> **Bottom line.** A typical PR waits **4m 15s** for all checks to finish; the per-pole drill-downs below trace where that time goes.",
        "> **Bottom line.** Some prose with no axis named.")
    assert _tag_for(bad, _HEADLINE, tmp_path) == "FAIL"


def test_headline_accepts_the_pr_floor_title(tmp_path: Path):
    # When the spine is the PR-FLOOR fallback (no file-backed required gate), the
    # renderer swaps the title to "why is CI slow on a PR?". That is the exact artifact
    # this feature produces, and it must clear the (mandatory) headline gate - else the
    # feature's own happy path dead-ends in a check it can't pass. Both title forms pass.
    pr_floor = _good().replace("demo - why is the merge slow?",
                               "demo - why is CI slow on a PR?")
    assert _tag_for(pr_floor, _HEADLINE, tmp_path) == "PASS"
    assert _tag_for(_good(), _HEADLINE, tmp_path) == "PASS"
    # A title naming neither story still fails (the gate hasn't gone slack).
    neither = _good().replace("demo - why is the merge slow?", "demo - a report")
    assert _tag_for(neither, _HEADLINE, tmp_path) == "FAIL"


def test_anchor_check_fails_on_unresolved_pole_ref(tmp_path: Path):
    bad = _good() + "\nSee [the other pole](#pole-9) for details.\n"  # no #pole-9 anchor
    assert _tag_for(bad, _ANCHORS, tmp_path) == "FAIL"
    assert _tag_for(_good(), _ANCHORS, tmp_path) == "PASS"


def test_anchor_check_covers_pre_start_wait(tmp_path: Path):
    # A queue-section pointer whose section didn't render (predicate drift) is a dead
    # link the anchor invariant must now catch — not silently pass.
    dead = _good() + "\n**⏳ Pre-start wait** — 1 job: [see below](#pre-start-wait).\n"
    assert _tag_for(dead, _ANCHORS, tmp_path) == "FAIL"
    ok = dead + '\n<a id="pre-start-wait"></a>\n\n## ⏳ Pre-start wait\n'
    assert _tag_for(ok, _ANCHORS, tmp_path) == "PASS"


def test_rca_check_fails_on_prescription_deadend_and_missing_disclaimer(tmp_path: Path):
    assert _tag_for(_good(), _RCA, tmp_path) == "PASS"
    # A prescribed **Fix:** line -> FAIL.
    assert _tag_for(_good() + "\n**Fix:** apply this diff.\n", _RCA, tmp_path) == "FAIL"
    # A dead-end "no catalog lever" -> FAIL.
    assert _tag_for(_good() + "\nNo catalog lever touches this.\n", _RCA, tmp_path) == "FAIL"
    # The coverage-gap dead-end marker (a pole that matched no detector AND got no
    # phase-4a fill) -> FAIL.
    assert _tag_for(_good() + "\ncaptured this job's log but matched no known "
                    "root-cause pattern — no drill-down available; this is a coverage "
                    "gap.\n", _RCA, tmp_path) == "FAIL"
    # Regression: the FILLED gap-fill label legitimately contains "no catalog pattern
    # matched" but is NOT a dead-end — it must still PASS (don't match on that phrase).
    assert _tag_for(_good() + "\n**🤖 LLM root-cause analysis** — no catalog pattern "
                    "matched this job, so the analysis below reads the captured log.\n",
                    _RCA, tmp_path) == "PASS"
    # A prompt block stripped of its no-prescription disclaimer -> FAIL.
    bad_prompt = _good().replace(
        "ci-speedup measured the root cause below but does NOT prescribe the fix -\n"
        "investigate it in the repo and apply a safe change.",
        "Here is the fix to apply.")
    assert _tag_for(bad_prompt, _RCA, tmp_path) == "FAIL"


def test_rca_check_fails_on_dangling_cross_run_reference(tmp_path: Path):
    # Class regression (goreleaser-check on caddyserver/caddy): a pole whose prompt/prose
    # cites "the cross-run check above" while that pole renders NO "🔬 Cross-run check"
    # section — a singleton magnitude sample suppresses the section, but the prompt template
    # emitted the validation claim unconditionally — is a DANGLING reference -> FAIL.
    dangling = _good().replace(
        "Where the job's ~4m 15s goes - every step, slowest first.",
        "Where the job's ~4m 15s goes - every step, slowest first.\n"
        "- The job's time is dominated by the `build` step: ~4m (73% of the job wall, "
        "validated across runs in the cross-run check above).")
    assert "🔬 Cross-run check" not in dangling          # the section really is absent
    assert _tag_for(dangling, _RCA, tmp_path) == "FAIL"
    # Control: the SAME reference is honest once the pole actually renders the section
    # (inserted before "## Data sources", so it lands inside the Long-pole 1 body).
    honest = dangling.replace(
        "## 🗄️ Data sources",
        "**🔬 Cross-run check** - the `build` step (wall): **4m** in the drilled run, "
        "3 runs sampled.\n\n## 🗄️ Data sources")
    assert _tag_for(honest, _RCA, tmp_path) == "PASS"
    # And a pole that neither cites nor renders a cross-run check is unaffected (baseline).
    assert _tag_for(_good(), _RCA, tmp_path) == "PASS"


_CAUSE_TL = "MEASURED CAUSE never asserts timeline steps"


def _pw_pole_section(cause: str, n_bars: int) -> str:
    """A second Long-pole section: a Level-2 waterfall with `n_bars` step rows, then an
    agent prompt whose MEASURED CAUSE is `cause`. Mirrors the playwright-parallel render
    shape (single-step nrwl/nx pole = one bar row)."""
    bars = "\n".join(
        f"   step-{j} name         {'█' * 20}   1m 0{j}s   {50 - j}%"
        for j in range(n_bars))
    return (
        "\n<a id=\"pole-2\"></a>\n\n"
        "## Long pole 2: `ci.yml` ▸ Run Checks/Lint/Test/Build - 10m 00s\n\n"
        "```text\n"
        "Level 2 - inside that one job, its steps run one after another:\n\n"
        f"{bars}\n"
        "```\n\n"
        "#### 🤖 Prompt for your coding agent\n\n"
        "```text\n"
        "ci-speedup measured the root cause below but does NOT prescribe the fix -\n"
        "investigate it in the repo and apply a safe change.\n\n"
        "THE MEASURED CAUSE\n"
        f"- {cause}\n\n"
        "WHERE TO LOOK\n"
        "- the workflow steps that invoke `playwright test`.\n"
        "```\n")


def test_measured_cause_timeline_claim_discriminator(tmp_path: Path):
    """The MEASURED CAUSE must not claim the finding is 'visible as sequential steps in
    the timeline above' when the pole's rendered waterfall shows <2 step rows (the
    nrwl/nx playwright-parallel bug). Re-derived from each pole's rendered step count."""
    F = _CAUSE_TL
    rep = _good()  # pole 1 has no MEASURED CAUSE -> nothing to check
    over = ("The job runs the spec files as separate, back-to-back `playwright test` "
            "invocations (visible as sequential steps in the timeline above), so they "
            "don't share a worker pool.")
    honest = ("The job's log shows the spec files run as two or more SEPARATE `playwright "
              "test` invocations rather than one run; confirm how they're scheduled.")
    # FAIL - the over-claim over a single-bar (single-step) waterfall.
    assert _tag_for(rep + _pw_pole_section(over, n_bars=1), F, tmp_path) == "FAIL"
    # FAIL - the actual nrwl/nx shape: no captured timeline renders ZERO bar rows.
    assert _tag_for(rep + _pw_pole_section(over, n_bars=0), F, tmp_path) == "FAIL"
    # PASS - exactly TWO rendered step rows is the threshold boundary (`step_rows < 2`);
    # >=2 rows back the plural "sequential steps" claim, so it must not FAIL here.
    assert _tag_for(rep + _pw_pole_section(over, n_bars=2), F, tmp_path) == "PASS"
    # PASS - same claim, but a genuinely multi-step timeline (>=2 bar rows) backs it.
    assert _tag_for(rep + _pw_pole_section(over, n_bars=3), F, tmp_path) == "PASS"
    # SKIP - the honest cause makes no timeline-shape claim, so there is nothing to check
    # (single-step waterfall and all).
    assert _tag_for(rep + _pw_pole_section(honest, n_bars=1), F, tmp_path) == "SKIP"
    # SKIP - no MEASURED CAUSE references the timeline anywhere in the report.
    assert _tag_for(rep, F, tmp_path) == "SKIP"


_POLES = "drills every gating pole"
_ONE_GATE = {"pr_critical_path": {"poles": [
    {"check": "build", "workflow_file": "ci.yml"}]}}
_TWO_GATE = {"pr_critical_path": {"poles": [
    {"check": "build", "workflow_file": "ci.yml"},
    {"check": "test", "workflow_file": "other.yml"}]}}


def test_poles_complete_fails_on_silently_dropped_second_pole(tmp_path: Path):
    rep = _good()  # one rendered "Long pole" section
    # findings say ONE gating check -> a one-pole report is correct.
    assert _tag_for(rep, _POLES, tmp_path, findings=_ONE_GATE) == "PASS"
    # findings say TWO distinct gating checks -> the one-pole report dropped one.
    assert _tag_for(rep, _POLES, tmp_path, findings=_TWO_GATE) == "FAIL"
    # no --findings -> the count half is skipped, symmetry still passes.
    assert _tag_for(rep, _POLES, tmp_path) == "PASS"


def test_poles_complete_exempts_the_all_fileless_gate(tmp_path: Path):
    # A MEASURED report (run history exists, so NOT static-only) whose every gating check
    # is managed/external with no editable workflow file: the renderer correctly drills NO
    # `## Long pole` (there is nothing to diff) and emits the "no editable workflow file"
    # note. The sibling `primary section present` check already exempts this shape; the
    # gating-pole-completeness check must too, or a legitimately-all-fileless report (e.g.
    # a repo gated entirely by external CI / app checks) fails its own artifact gate.
    fileless = (
        "# demo - why is the merge slow?\n\n"
        "> **Bottom line.** A typical PR waits **5m 00s** for all checks to finish; "
        "every gating check is managed/external.\n\n"
        "**5m 00s until all checks finish.** Every gating check is a managed/external "
        "check with no editable workflow file (see below) - there is no workflow to "
        "drill or diff.\n\n"
        "## 🗄️ Data sources\n\n"
        "| Source | Coverage | Used for |\n| --- | --- | --- |\n"
        "| ci-speedup static scan (skill commit `0000000`) | all workflows | scan |\n"
    )
    # No `## Long pole` section, and no static-only marker — yet it must not FAIL.
    assert "## Long pole" not in fileless
    assert _tag_for(fileless, _POLES, tmp_path) == "SKIP"
    # The primary-section sibling already accepts it; keep them in agreement.
    assert _tag_for(fileless, _PRIMARY, tmp_path) == "PASS"
    # A 0-pole report WITHOUT the fileless note is still a genuine dropped-spine FAIL.
    no_note = fileless.replace(
        "Every gating check is a managed/external check with no editable workflow "
        "file (see below) - there is no workflow to drill or diff.",
        "Something rendered, but the spine is missing.")
    assert _tag_for(no_note, _POLES, tmp_path) == "FAIL"
    # The fileless note must NOT launder a genuinely-dropped pole into a SKIP. The renderer's
    # `_is_file_pole` treats a pole with a workflow_file but no `job` as fileless, so such a
    # pole renders 0 `## Long pole` AND emits the note - but the findings still count it as a
    # drillable file-backed pole. When the note and the findings disagree, the dropped pole
    # wins: FAIL, not SKIP. (This is the false-negative the exemption could otherwise mask.)
    dropped_file_pole = {"pr_critical_path": {"poles": [
        {"check": "build", "workflow_file": ".github/workflows/ci.yml"}]}}  # has wf, no job
    assert _tag_for(fileless, _POLES, tmp_path, findings=dropped_file_pole) == "FAIL"
    # And the legitimate all-fileless gate still SKIPs when the findings AGREE there is no
    # file-backed pole (every pole managed/external, no workflow_file).
    all_managed = {"pr_critical_path": {"poles": [
        {"check": "Size Analysis | Emerge", "workflow_file": ""}]}}
    assert _tag_for(fileless, _POLES, tmp_path, findings=all_managed) == "SKIP"


def test_poles_complete_fails_when_headline_crown_is_a_triaged_workflow(tmp_path: Path):
    # paradedb/paradedb class: required checks unreadable, so the spine falls back to the
    # slowest-typical-PR check. When every heavier pole is minority-present (a rare label-gated
    # benchmark), the crown falls to a sub-floor lint whose workflow was triage-skipped (jobs
    # never fetched). The report then HEADLINES `critical_path_check` = that lint — a workflow it
    # ALSO discloses in data_sources.triaged_fast_workflows as "can't hold the merge pole" — so
    # the headline pole dead-ends ("no captured log" / "NO CATALOG PATTERN MATCHED"). The
    # poles-keyed `_triaged_pole_offenders` guard MISSES this: the crown lives in the `checks`
    # spine (workflow_file filled via the scanned-graph fallback) and needn't appear in `poles`
    # at all (the pole builders exclude triaged workflows). Re-derived from findings, so it fires
    # on ANY report of this class. `_good()` is well-formed; the contradiction is data-only.
    _typo = ".github/workflows/check-typo.yml"
    bad = {"pr_critical_path": {
        "critical_path_check": "Check Typo",
        "checks": [
            {"name": "Check Typo", "p50_s": 13.0, "present_on": 20, "workflow_file": _typo},
            {"name": "Run Benchmark Jobs", "p50_s": 4416.0, "present_on": 1},
        ]},
        "data_sources": {"triaged_fast_workflows": [_typo]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=bad) == "FAIL"
    # PASS — the crown's workflow was job-fetched (NOT in the triaged set): a drillable headline.
    ok = {"pr_critical_path": {
        "critical_path_check": "Check Typo",
        "checks": [{"name": "Check Typo", "p50_s": 13.0, "present_on": 20, "workflow_file": _typo}]},
        "data_sources": {"triaged_fast_workflows": [".github/workflows/other-lint.yml"]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=ok) == "PASS"
    # PASS — a genuinely FILELESS crown (an external review bot, no workflow_file on its spine
    # entry) is a legitimate no-drill headline, not a triaged-workflow dead-end.
    fileless = {"pr_critical_path": {
        "critical_path_check": "Claude Code Review",
        "checks": [{"name": "Claude Code Review", "p50_s": 900.0, "present_on": 20}]},
        "data_sources": {"triaged_fast_workflows": [_typo]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=fileless) == "PASS"


def test_crown_triaged_offender_helper_rederives_from_findings(tmp_path: Path):
    # Unit-level: the re-derivation keys the crown to its `checks[*].workflow_file` (the SAME
    # field the engine stamps and the renderer resolves the headline from) and tests membership
    # in data_sources.triaged_fast_workflows — never a rendered proxy.
    vr = _load_verify_report()
    _typo = ".github/workflows/check-typo.yml"

    def _off(doc: dict) -> str | None:
        fp = tmp_path / "f.json"
        fp.write_text(json.dumps(doc), encoding="utf-8")
        return vr._crown_triaged_offender(fp)

    assert _off({"pr_critical_path": {"critical_path_check": "Check Typo",
                 "checks": [{"name": "Check Typo", "workflow_file": _typo}]},
                 "data_sources": {"triaged_fast_workflows": [_typo]}}) is not None
    # crown not triaged → clean
    assert _off({"pr_critical_path": {"critical_path_check": "Check Typo",
                 "checks": [{"name": "Check Typo", "workflow_file": _typo}]},
                 "data_sources": {"triaged_fast_workflows": []}}) is None
    # no triaged set at all → clean
    assert _off({"pr_critical_path": {"critical_path_check": "Check Typo",
                 "checks": [{"name": "Check Typo", "workflow_file": _typo}]}}) is None
    # crown fileless (no workflow_file) → clean even with a populated triaged set
    assert _off({"pr_critical_path": {"critical_path_check": "Bot",
                 "checks": [{"name": "Bot"}]},
                 "data_sources": {"triaged_fast_workflows": [_typo]}}) is None
    # a non-crown check being triaged is fine — only the crown matters
    assert _off({"pr_critical_path": {"critical_path_check": "Build",
                 "checks": [{"name": "Build", "workflow_file": ".github/workflows/ci.yml"},
                            {"name": "Check Typo", "workflow_file": _typo}]},
                 "data_sources": {"triaged_fast_workflows": [_typo]}}) is None


def test_external_check_misbound_offenders_helper_rederives_from_findings(tmp_path: Path):
    # Unit-level (tokio-rs class): the re-derivation flags a no-sampled-job pole whose bound
    # workflow produces NO job matching its check — the managed/external check (Netlify
    # `Redirect rules`) that a match-anything `${{ matrix.target }}` name grabbed. Re-derived
    # from `workflow_job_graph` + `pr_critical_path.poles[*].{workflow_file,job,timing_source}`,
    # never a rendered proxy.
    vr = _load_verify_report()
    _ci = ".github/workflows/ci.yml"

    def _off(doc: dict):
        fp = tmp_path / "f.json"
        fp.write_text(json.dumps(doc), encoding="utf-8")
        return vr._external_check_misbound_offenders(fp)

    _wjg = {_ci: {
        "wasm32-wasip1": {"name": "${{ matrix.target }}", "matrix": True, "needs": []},
        "test": {"name": "Test suite", "needs": []}}}
    # OFFENDER: external check, no sampled job (`pr_check_runs`), bound to ci.yml via the
    # degenerate matrix name — no job in ci.yml actually produces `Redirect rules - tokio-rs`.
    bad = {"workflow_job_graph": _wjg, "pr_critical_path": {"poles": [
        {"check": "Redirect rules - tokio-rs", "workflow_file": _ci, "job": "wasm32-wasip1",
         "timing_source": "pr_check_runs", "p50_s": 22.0}]}}
    assert len(_off(bad)) == 1  # exactly one offender
    assert "Redirect rules - tokio-rs" in _off(bad)[0]
    # CLEAN (fixed-engine shape): the external check renders fileless — no workflow_file/job.
    clean = {"workflow_job_graph": _wjg, "pr_critical_path": {"poles": [
        {"check": "Redirect rules - tokio-rs", "timing_source": "pr_check_runs", "p50_s": 22.0}]}}
    assert _off(clean) == []
    # LEGIT triaged-fast in-repo check (no sampled job) whose workflow DOES produce it → not flagged.
    legit_triaged = {"workflow_job_graph": {
        ".github/workflows/test.yml": {"py": {"name": "Python ${{ matrix.python }}",
                                              "matrix": True, "needs": []}}},
        "pr_critical_path": {"poles": [
            {"check": "Python 3.9", "workflow_file": ".github/workflows/test.yml", "job": "py",
             "timing_source": "pr_check_runs"}]}}
    assert _off(legit_triaged) == []
    # LEGIT sampled matrix leg with a DEGENERATE name (`timing_source == workflow_jobs`) — its
    # binding is timing-proven, so it is skipped even though the name can't discriminate.
    legit_sampled = {"workflow_job_graph": _wjg, "pr_critical_path": {"poles": [
        {"check": "wasm32-wasip1", "workflow_file": _ci, "job": "wasm32-wasip1",
         "timing_source": "workflow_jobs"}]}}
    assert _off(legit_sampled) == []
    # No scanned job graph → can't re-derive → None (a SKIP, never a silent clean).
    assert _off({"pr_critical_path": {"poles": [
        {"check": "x", "workflow_file": _ci, "job": "wasm32-wasip1",
         "timing_source": "pr_check_runs"}]}}) is None


def test_poles_complete_fails_on_external_check_misbound_to_a_file(tmp_path: Path):
    # End-to-end: a report drilling the external check as a file-backed pole FAILs the poles-
    # complete gate via the mis-bound guard (checked before the static-only / fileless-note
    # branches so no note can launder it into a SKIP).
    _ci = ".github/workflows/ci.yml"
    findings = {"workflow_job_graph": {
        _ci: {"wasm32-wasip1": {"name": "${{ matrix.target }}", "matrix": True, "needs": []}}},
        "pr_critical_path": {"poles": [
            {"check": "Redirect rules - tokio-rs", "workflow_file": _ci, "job": "wasm32-wasip1",
             "timing_source": "pr_check_runs", "p50_s": 22.0}]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=findings) == "FAIL"


# ── Aggregation-gate poles (issue #1) ────────────────────────────────────────────────────
# A `needs:`-everything success sink (next.js `thank you, build`: 3s, `run: exit 1`) renders
# the honest upstream story INSTEAD of a drill + "optimize this step" prompt. Two coupled
# guards: `check_speed_poles_complete` must EXEMPT such a pole from its prompt requirement,
# and `check_aggregation_gate_poles_never_prescribe` must FAIL any aggregation-framed pole
# that carries a prompt, lacks the upstream pointer, or isn't re-derivable from the findings.
_AGG_GATE = "aggregation-gate poles"
_AGG_WF = ".github/workflows/deploy.yml"
_AGG_FINDINGS = {
    "workflow_job_graph": {_AGG_WF: {
        "target": {"name": "target", "needs": []},
        "build": {"name": "build", "needs": ["target"]},
        # A conditional PEER sink (uncovered by the gate's needs, but itself terminal) —
        # the `publishRelease` shape the "effectively all" rule must tolerate.
        "publish": {"name": "Potentially publish release", "needs": ["target", "build"]},
        "gate": {"name": "thank you, build", "needs": ["target", "build"]}}},
    "pr_critical_path": {"poles": [
        {"check": "thank you, build", "workflow_file": _AGG_WF, "job": "gate",
         "p50_s": 3.0, "timing_source": "pr_check_runs"}],
        "checks": [{"name": "build", "workflow_file": _AGG_WF, "p50_s": 355.0},
                   {"name": "target", "workflow_file": _AGG_WF, "p50_s": 19.0},
                   {"name": "thank you, build", "workflow_file": _AGG_WF, "p50_s": 3.0}]}}


def _agg_report(*, prompt: bool = False, pointer: bool = True, framed: bool = True) -> str:
    """The `_good()` report with its pole rewritten as an aggregation-gate pole."""
    role = ("**Aggregation gate - it exists to be the single required check.** Its job "
            "(`gate`) runs no work of its own (3s); it `needs:` 2 upstream jobs so one check "
            "can stand for all of them. So its 3s is not the wait - the wait IS that `needs:` "
            "upstream, whose slowest measured member is `build` (~5m 55s). That member, not "
            "this check, is the lever."
            if framed else "**The check most PRs gate on.**")
    body = [f"## Long pole 1: `deploy.yml` ▸ thank you, build - 3s", "", role, ""]
    if pointer:
        body += ["**➡️ Where the wait actually is:** `build` (5m 55s), the slowest measured "
                 "member of this gate's `needs:` upstream.", ""]
    if prompt:
        body += ["#### 🤖 Prompt for your coding agent", "", "```text",
                 "ci-speedup measured the root cause below but does NOT prescribe the fix -",
                 "capture timing, then optimize the dominant step.", "```", ""]
    old = _good().split("## Long pole 1:", 1)[1].split("## 🗄️ Data sources", 1)[0]
    return _good().replace("## Long pole 1:" + old, "\n".join(body) + "\n")


def test_aggregation_gate_pole_ships_without_a_prompt(tmp_path: Path):
    # GREEN both ways: the honest render passes the new invariant AND is exempted from the
    # "every pole carries a prompt" rule (which FAILs it without the exemption — the
    # pre-fix behaviour this test pins).
    rep = _agg_report()
    assert _tag_for(rep, _AGG_GATE, tmp_path, findings=_AGG_FINDINGS) == "PASS"
    assert _tag_for(rep, _POLES, tmp_path, findings=_AGG_FINDINGS) == "PASS"
    # The exemption is findings-gated, not framing-gated: with NO findings to re-derive the
    # shape from, the promptless pole is still a bare pole and FAILs.
    assert _tag_for(rep, _POLES, tmp_path) == "FAIL"


def test_aggregation_gate_pole_with_an_optimize_prompt_fails(tmp_path: Path):
    # RED: the exact defect from issue #1 — a `needs:`-everything 3s sink handed an
    # "optimize this step" agent prompt.
    assert _tag_for(_agg_report(prompt=True), _AGG_GATE, tmp_path,
                    findings=_AGG_FINDINGS) == "FAIL"


def test_aggregation_framing_without_the_pointer_fails(tmp_path: Path):
    # RED: framing with no upstream pointer is a dead end — the reader is told the lever is
    # elsewhere and never told where.
    assert _tag_for(_agg_report(pointer=False), _AGG_GATE, tmp_path,
                    findings=_AGG_FINDINGS) == "FAIL"


def test_aggregation_framing_unsupported_by_the_job_graph_fails(tmp_path: Path):
    # RED: the framing may never be applied to a pole that isn't structurally a sink. Here the
    # gate `needs:` nothing, so the shape doesn't re-derive from the job graph.
    bad = copy.deepcopy(_AGG_FINDINGS)
    bad["workflow_job_graph"][_AGG_WF]["gate"]["needs"] = []
    assert _tag_for(_agg_report(), _AGG_GATE, tmp_path, findings=bad) == "FAIL"


def test_aggregation_check_skips_when_no_pole_is_framed(tmp_path: Path):
    # A report with no aggregation-gate pole neither passes nor fails on this axis.
    assert _tag_for(_agg_report(framed=True, prompt=False).replace(
        "**Aggregation gate", "**The check most PRs gate on."), _AGG_GATE, tmp_path,
        findings=_AGG_FINDINGS) == "SKIP"


def test_agg_gate_pole_keys_rederives_the_shape(tmp_path: Path):
    # Unit-level: the verifier's re-derivation mirrors `blocking_path._agg_gate_shape` —
    # trivial P50 + terminal sink covering every non-terminal job + a measured upstream.
    vr = _load_verify_report()

    def _keys(doc: dict):
        fp = tmp_path / "f.json"
        fp.write_text(json.dumps(doc), encoding="utf-8")
        return vr._agg_gate_pole_keys(fp)

    assert _keys(_AGG_FINDINGS) == {("deploy.yml", vr._cmp_name("thank you, build"))}
    # Not trivial (a 3-minute job does real work) → no match on duration alone.
    heavy = copy.deepcopy(_AGG_FINDINGS)
    heavy["pr_critical_path"]["poles"][0]["p50_s"] = 180.0
    assert _keys(heavy) == set()
    # A trivial job that `needs:` nothing (the 3s-lint near miss) → no match.
    lint = copy.deepcopy(_AGG_FINDINGS)
    lint["workflow_job_graph"][_AGG_WF]["gate"]["needs"] = []
    assert _keys(lint) == set()
    # Coverage gap: a non-terminal job outside the closure → not an aggregation sink.
    gap = copy.deepcopy(_AGG_FINDINGS)
    gap["workflow_job_graph"][_AGG_WF]["extra"] = {"name": "extra", "needs": ["target"]}
    gap["workflow_job_graph"][_AGG_WF]["after"] = {"name": "after", "needs": ["extra"]}
    assert _keys(gap) == set()
    # No measured upstream check → nothing honest to point at → no match.
    unmeasured = copy.deepcopy(_AGG_FINDINGS)
    unmeasured["pr_critical_path"]["checks"] = [
        {"name": "thank you, build", "workflow_file": _AGG_WF, "p50_s": 3.0}]
    assert _keys(unmeasured) == set()


def test_vr_produces_check_mirrors_engine_degenerate_rule():
    # L7 coupling: verify_report's re-derivation of "does this workflow produce this check" must
    # mirror collect_runs' scanned matcher — same degenerate-template refusal, same matrix-template
    # / static-leg semantics — so the invariant can't silently drift from the engine it guards.
    vr = _load_verify_report()
    name = "ci_speedup_collect_runs_couple"
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "scripts" / "collect_runs.py")
    cr = importlib.util.module_from_spec(spec)
    sys.modules[name] = cr
    spec.loader.exec_module(cr)
    for tmpl in ("${{ matrix.target }}", "${{ a }}${{ b }}", "${{ a }} ${{ b }}",
                 "Python ${{ matrix.python }}", "${{ matrix.shard }} of ${{ matrix.total }}",
                 "Lint", "Test (${{ matrix.os }})"):
        assert vr._vr_template_is_degenerate(tmpl) == cr._name_template_is_degenerate(tmpl), tmpl
    # The scanned engine binder and the verifier's producer-check agree on a degenerate name.
    jg = {".github/workflows/ci.yml": {
        "j": {"name": "${{ matrix.target }}", "matrix": True, "needs": [], "reusable": False}}}
    assert cr._check_to_job_node_scanned("Redirect rules - x", jg) is None
    assert not vr._vr_job_produces_check("${{ matrix.target }}", True, "Redirect rules - x")
    # ...and on a NON-degenerate matrix name both accept the real leg.
    jg2 = {".github/workflows/t.yml": {
        "py": {"name": "Python ${{ matrix.python }}", "matrix": True, "needs": [], "reusable": False}}}
    assert cr._check_to_job_node_scanned("Python 3.9", jg2) is not None
    assert vr._vr_job_produces_check("Python ${{ matrix.python }}", True, "Python 3.9")


def test_poles_complete_fails_on_a_bare_pole_without_a_prompt(tmp_path: Path):
    # A second pole that is a bare timeline with no agent prompt (asymmetric).
    bare2 = ('\n<a id="pole-2"></a>\n\n## Long pole 2: `other.yml` ▸ test - 3m 00s\n\n'
             '```text\njust a timeline, no hand-off\n```\n\n')
    rep = _good().replace("## 🗄️ Data sources", bare2 + "## 🗄️ Data sources")
    assert _tag_for(rep, _POLES, tmp_path, findings=_TWO_GATE) == "FAIL"


def test_poles_complete_fails_on_an_all_events_blended_pole(tmp_path: Path):
    # (c) the pull_request_target blend class (roboflow/supervision pr-conflict-labeler):
    # a PR-critical-path pole whose workflow `per_workflow_timing.event_scope` is
    # `all-events` blends post-merge push runs (which gate ZERO merges) into the PR wait,
    # fabricating a false bimodal gate. Re-derived from the findings DATA, so it fires on
    # ANY report of this class. The report text is well-formed (drilled + prompt); the
    # blend is only visible in the per-workflow event_scope.
    _LABELER = ".github/workflows/pr-conflict-labeler.yml"
    blended = {
        "pr_critical_path": {"poles": [
            {"check": "main", "workflow_file": _LABELER, "p50_s": 124.0}]},
        "per_workflow_timing": {_LABELER: {"event_scope": "all-events",
                                           "long_pole_job": "main"}},
    }
    assert _tag_for(_good(), _POLES, tmp_path, findings=blended) == "FAIL"
    # After the engine fix the SAME workflow scopes to its developer-wait event
    # (pull_request_target) — only the PR-wait runs, no blend — so the guard is clean.
    scoped = {
        "pr_critical_path": {"poles": [
            {"check": "main", "workflow_file": _LABELER, "p50_s": 4.0}]},
        "per_workflow_timing": {_LABELER: {"event_scope": "pull_request_target",
                                           "long_pole_job": "main"}},
    }
    assert _tag_for(_good(), _POLES, tmp_path, findings=scoped) == "PASS"
    # Playwright-shaped narrow fix: the workflow-run sample may have no developer-event
    # jobs even though sampled PR check-runs prove the check gates PRs. That pole is
    # allowed only when stamped as PR-check-run timed and no all-events workflow-job drill
    # fields were borrowed from the push/schedule sample.
    check_run_timed = {
        "pr_critical_path": {"poles": [
            {"check": "Windows (firefox)", "workflow_file": ".github/workflows/tests.yml",
             "p50_s": 300.0, "timing_source": "pr_check_runs"}]},
        "per_workflow_timing": {".github/workflows/tests.yml": {
            "event_scope": "all-events", "long_pole_job": "Windows (firefox)"}},
    }
    assert _tag_for(_good(), _POLES, tmp_path, findings=check_run_timed) == "PASS"
    borrowed_drill = copy.deepcopy(check_run_timed)
    borrowed_drill["pr_critical_path"]["poles"][0]["dominant_step"] = "Run tests"
    assert _tag_for(_good(), _POLES, tmp_path, findings=borrowed_drill) == "FAIL"
    borrowed_bimodal = copy.deepcopy(check_run_timed)
    borrowed_bimodal["pr_critical_path"]["poles"][0]["bimodal"] = {
        "low_p50_s": 30.0, "high_p50_s": 900.0, "slow_frac": 0.25}
    assert _tag_for(_good(), _POLES, tmp_path, findings=borrowed_bimodal) == "FAIL"
    # A pole whose workflow has NO per_workflow_timing entry (genuinely-fileless /
    # external check) carries no re-derivable scope → exempt, not a false FAIL.
    fileless = {"pr_critical_path": {"poles": [
        {"check": "CodeQL", "workflow_file": "CodeQL", "p50_s": 90.0}]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=fileless) == "PASS"
    # CROSS-FIX REGRESSION: a genuine PUSH-only floor pole (`pr_floor_push_fallback`, the synthesis
    # `_select_pr_floor_workflows` makes for a repo with NO PR-volume workflow) is honestly `all-events`
    # (no developer event exists) and is DISCLOSED as the PR-floor, NOT a blended gate. The blend guard
    # must EXEMPT it — otherwise it false-fails every push-only-repo report the fallback exists to
    # support (the spine is required by `check_primary_section_present`, so failing it here makes the
    # gate unsatisfiable). The contradiction the per-fix review couldn't see.
    push_floor = {
        "pr_critical_path": {"gate_kind": "pr_floor_fallback", "poles": [
            {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 3090.0,
             "pr_floor_fallback": True, "pr_floor_push_fallback": True}]},
        "per_workflow_timing": {".github/workflows/ci.yml": {"event_scope": "all-events",
                                                             "long_pole_job": "test"}},
    }
    assert _tag_for(_good(), _POLES, tmp_path, findings=push_floor) == "PASS"
    # NARROWNESS (the second-review MINOR): the exemption is the PUSH flag ONLY. A case-1/1b structural
    # pole carries the broad `pr_floor_fallback` but NOT `pr_floor_push_fallback` — it is a real,
    # drillable, PR-scoped pole that CAN be genuinely event-blended, so the blend guard must STILL FAIL
    # it. Exempting all `pr_floor_fallback` poles (the first cut) would have silently disabled the guard
    # for this case.
    case1_blended = {
        "pr_critical_path": {"gate_kind": "pr_floor_fallback", "poles": [
            {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 3090.0,
             "pr_floor_fallback": True}]},   # NO pr_floor_push_fallback → still guarded
        "per_workflow_timing": {".github/workflows/ci.yml": {"event_scope": "all-events",
                                                             "long_pole_job": "test"}},
    }
    assert _tag_for(_good(), _POLES, tmp_path, findings=case1_blended) == "FAIL"


def test_check_run_timed_all_events_pole_renders_as_withheld_not_stunted(tmp_path: Path):
    # Rendered-path guard for the PR-10 contract: when sampled PR check-runs prove a
    # workflow check gates PRs but the workflow-job sample is all-events only, the
    # report must keep that gate visible as a pole and explicitly withhold the step
    # drill. It must not borrow push/schedule steps, drop to a lower pole, or render a
    # stunted "No per-step breakdown" drill.
    bp = _load_blocking_path()
    wf = ".github/workflows/tests.yml"
    doc = {
        "repo": "demo/repo",
        "pr_critical_path": {
            "sampled_pr_count": 8,
            "check_present_n_pr": 8,
            "critical_path_check": "Windows (firefox)",
            "critical_path_s": 300.0,
            "checks": [
                {"name": "Windows (firefox)", "p50_s": 300.0, "present_on": 8,
                 "pole_n": 8, "workflow_file": wf, "timing_source": "pr_check_runs"},
                {"name": "lint", "p50_s": 100.0, "present_on": 8, "pole_n": 0,
                 "workflow_file": ".github/workflows/lint.yml",
                 "timing_source": "workflow_jobs"},
            ],
            "populations": [[1.0, [["Windows (firefox)", 300.0], ["lint", 100.0]]]],
            "poles": [
                {"check": "Windows (firefox)", "workflow_file": wf, "job": "win",
                 "p50_s": 300.0, "timing_source": "pr_check_runs",
                 "job_timing_unavailable": (
                     "PR check-run timing was measured, but no sampled workflow job "
                     "for this check ran on a developer-facing event; step drill is "
                     "withheld rather than borrowing push/schedule job timings.")}
            ],
        },
        "per_workflow_timing": {
            wf: {"event_scope": "all-events", "long_pole_job": "win"}},
    }
    report = bp.render(doc)
    assert "Long pole 1" in report
    assert "Windows (firefox)" in report
    assert "workflow-job drill withheld" in report
    assert "No per-step breakdown was captured" not in report
    assert _tag_for(report, _POLES, tmp_path, findings=doc) == "PASS"


def test_poles_complete_exempts_a_bare_push_floor_pole(tmp_path: Path):
    # CROSS-FIX (second HIGH, found by the integrated-PR review): the STUNTED-pole guard must ALSO
    # exempt a genuine push-only floor pole, not just the blend guard. A `pr_floor_push_fallback` pole
    # is synthesized from push timing for a repo with no file-backed pole, so its job may have NO
    # sampled steps → the renderer emits the bare "No per-step breakdown was captured" body. Failing
    # that here while check_primary_section_present REQUIRES the spine is the same unsatisfiable-gate
    # contradiction as the blend bug, via a different rejection path. Exempt ONLY the narrow flag.
    bare = (
        "# demo - why is CI slow on a PR?\n\n"
        "> **Bottom line.** A typical PR waits **51m 30s** for all checks to finish; trace below.\n\n"
        '<a id="pole-1"></a>\n\n## Long pole 1: `ci.yml` ▸ test - 51m 30s\n\n'
        "```text\nWHERE THE TIME GOES\n- No per-step breakdown was captured for this job; "
        "profile its slowest step in the repo.\n```\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate it.\n```\n\n"
        "## 🗄️ Data sources\n\n| Source | Coverage | Used for |\n| --- | --- | --- |\n"
        "| ci-speedup static scan (skill commit `0000000`) | all | scan |\n")
    push_floor = {"pr_critical_path": {"gate_kind": "pr_floor_fallback", "poles": [
        {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 3090.0,
         "pr_floor_fallback": True, "pr_floor_push_fallback": True}]},
        "per_workflow_timing": {".github/workflows/ci.yml": {"event_scope": "all-events",
                                                             "long_pole_job": "test"}}}
    # The bare body is HONEST for a synthesized push-only floor → exempt → PASS.
    assert _tag_for(bare, _POLES, tmp_path, findings=push_floor) == "PASS"
    # The SAME bare pole WITHOUT the push flag (a case-1/1b structural pole, scoped to a real PR event
    # so the blend guard doesn't fire first) is a genuine stunted drill → must still FAIL.
    case1_bare = {"pr_critical_path": {"gate_kind": "pr_floor_fallback", "poles": [
        {"check": "test", "workflow_file": ".github/workflows/ci.yml", "p50_s": 3090.0,
         "pr_floor_fallback": True}]},
        "per_workflow_timing": {".github/workflows/ci.yml": {"event_scope": "pull_request",
                                                             "long_pole_job": "test"}}}
    assert _tag_for(bare, _POLES, tmp_path, findings=case1_bare) == "FAIL"


def test_poles_complete_fails_on_a_pole_with_a_prompt_but_no_drill(tmp_path: Path):
    # The bitmovin hole: a pole that HANDS OFF a prompt but whose body carries the renderer's
    # own "No per-step breakdown was captured" admission (no captured timeline AND no sampled
    # decomposition). SKILL.md 5a calls this "a timeline with no drill" and requires the gate to
    # FAIL it - the prompt-only test used to pass it (4 poles "each hands off via a prompt") while
    # the drill was empty. The bare drill renders the renderer's literal verbatim.
    undrilled = (
        '\n<a id="pole-2"></a>\n\n## Long pole 2: `other.yml` ▸ test - 3m 00s\n\n'
        '```text\nWHERE THE TIME GOES\n- No per-step breakdown was captured for this job; '
        'profile its slowest step in the repo.\n```\n\n'
        '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate it.\n```\n')
    rep = _good().replace("## 🗄️ Data sources", undrilled + "## 🗄️ Data sources")
    # FAIL even though BOTH poles carry a prompt: pole 2 has no per-step drill.
    assert _tag_for(rep, _POLES, tmp_path, findings=_TWO_GATE) == "FAIL"
    # A legit SHALLOW pole (names its dominant step from the sampled decomposition, no single-run
    # timeline) is NOT bare and must still PASS - it never emits the no-breakdown literal.
    shallow = (
        '\n<a id="pole-2"></a>\n\n## Long pole 2: `other.yml` ▸ test - 3m 00s\n\n'
        '```text\nWHERE THE TIME GOES\n- The job\'s time is dominated by the `unit` step: '
        '~2m 00s (60% of the job wall), from the sampled per-step decomposition (no single-run '
        'timeline was captured for this job).\n```\n\n'
        '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate it.\n```\n')
    rep_ok = _good().replace("## 🗄️ Data sources", shallow + "## 🗄️ Data sources")
    assert _tag_for(rep_ok, _POLES, tmp_path, findings=_TWO_GATE) == "PASS"


_WF = ".github/workflows/ci.yml"
# findings whose drill bundle captured ONLY `build` (not `test`).
_DRILLED_BUILD_ONLY = {
    "pr_critical_path": {"poles": [
        {"check": "build", "workflow_file": _WF, "p50_s": 400.0},
        {"check": "test", "workflow_file": _WF, "p50_s": 360.0}]},
    "data_bundle": {"logs": [
        {"check": "build", "workflow_file": _WF, "html_url": "u"}]},
}


def test_drill_ownership_fails_when_an_undrilled_pole_shows_a_drill(tmp_path: Path):
    # R1 safety net: a `test` pole that shows a representative-run drill, while the drill
    # bundle only captured `build`, borrowed `build`'s log — must FAIL.
    leaked = _good() + (
        '\n<a id="pole-2"></a>\n\n## Long pole 2: `ci.yml` ▸ test - 3m 00s\n\n'
        '```text\nWHERE THE TIME GOES (representative run 12345)\n- dominated by Build\n```\n\n'
        '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate it.\n```\n')
    assert _tag_for(leaked, _DRILLOWN, tmp_path, findings=_DRILLED_BUILD_ONLY) == "FAIL"


def test_drill_ownership_passes_when_the_drilled_pole_owns_its_drill(tmp_path: Path):
    # The drilled pole (`build`) showing its own representative run is correct → PASS;
    # an undrilled sibling rendering SHALLOW (no representative run) is also fine.
    ok = _good().replace(
        "Where the job's ~4m 15s goes - every step, slowest first.",
        "WHERE THE TIME GOES (representative run 12345)") + (
        '\n<a id="pole-2"></a>\n\n## Long pole 2: `ci.yml` ▸ test - 3m 00s\n\n'
        '```text\nno single-run timeline was captured for this job\n```\n\n'
        '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate it.\n```\n')
    assert _tag_for(ok, _DRILLOWN, tmp_path, findings=_DRILLED_BUILD_ONLY) == "PASS"


def test_drill_ownership_passes_for_a_scoped_monorepo_pole(tmp_path: Path):
    # Monorepo (#99 scoped-check class): `data_bundle.logs[].check` stores the RAW `@scope/`-prefixed
    # name, but the renderer strips the scope in the pole HEADER (`_clean_label`). The check must
    # compare by `_cmp_name` (scope-stripped) on BOTH sides — `_norm_check` alone never intersects, so
    # a CORRECT scoped drilled pole was falsely flagged as borrowing a sibling's log (a phantom R1
    # leak). The pole owns its own scoped log → must PASS, not FAIL.
    scoped = {"data_bundle": {"logs": [
        {"check": "@better-auth/db build", "workflow_file": _WF, "html_url": "u"}]}}
    rep = _good().replace(
        "Where the job's ~4m 15s goes - every step, slowest first.",
        "WHERE THE TIME GOES (representative run 12345)").replace(
        "## Long pole 1: `ci.yml` ▸ build - 4m 15s",
        "## Long pole 1: `ci.yml` ▸ db build - 4m 15s")   # rendered = scope-stripped `db build`
    assert _tag_for(rep, _DRILLOWN, tmp_path, findings=scoped) == "PASS"
    # FAIL direction still works for scoped names — the scope-strip must not over-broaden into
    # never-failing: a pole whose drill it does NOT own (rendered `migrate`, only `@a/db build`
    # drilled) is still a borrowed-log leak. (Distinct from the accepted `@a/build`==`@b/build`
    # cross-package residual `_cmp_name` documents — here the base names genuinely differ.)
    leaked = rep.replace("## Long pole 1: `ci.yml` ▸ db build - 4m 15s",
                         "## Long pole 1: `ci.yml` ▸ migrate - 4m 15s")
    assert _tag_for(leaked, _DRILLOWN, tmp_path, findings=scoped) == "FAIL"


# --- gap-fill evidence grounding (issue #106) --------------------------------
_GROUND = "gap-fill evidence line is verbatim"
# A captured job log for pole 1 (`build`). The em-dash line exercises the render-boundary dash
# flatten: the renderer strips it to a hyphen, so the check must strip the LOG side to match.
_GAPFILL_LOG = (
    "npm install running in CI\n"
    "  added 1200 packages in 3m 02s\n"
    "cache miss — cold start, rebuilding\n"
    "webpack build - production mode\n"
)


def _with_gapfill_block(evidence_lines: str) -> str:
    """`_good()` with a rendered 🤖 LLM root-cause analysis block (evidence fence) injected into
    pole 1's body — the exact shape `blocking_path._llm_analysis_block` emits, post em-dash-strip."""
    block = (
        "**🤖 LLM root-cause analysis** - no catalog pattern matched this job, so the analysis "
        "below is the skill's LLM reading the captured job log, **grounded in the lines quoted "
        "below**. It is a strong lead to verify, **not** a measured catalog detector - the "
        "timeline above is measured; this cause is inferred.\n\n"
        "The build is dominated by dependency install.\n\n"
        "**Evidence - verbatim from the captured job log:**\n\n"
        f"```text\n{evidence_lines}\n```\n\n")
    return _good().replace("#### 🤖 Prompt for your coding agent",
                           block + "#### 🤖 Prompt for your coding agent")


def _gapfill_findings(logs_dir: str | None, file: str | None = "build.log") -> dict:
    log = {"check": "build", "workflow_file": ".github/workflows/ci.yml"}
    if file is not None:
        log["file"] = file
    db: dict = {"logs": [log]}
    if logs_dir is not None:
        db["logs_dir"] = logs_dir
    return {"pr_critical_path": {"poles": [
        {"check": "build", "workflow_file": ".github/workflows/ci.yml", "p50_s": 255.0}]},
        "data_bundle": db}


def test_gap_fill_evidence_grounded_passes_on_verbatim_lines(tmp_path: Path):
    # Every quoted evidence line is a substring of the captured log (the em-dash line matches after
    # the render-boundary dash flatten is applied to the log side too) → PASS.
    (tmp_path / "build.log").write_text(_GAPFILL_LOG, encoding="utf-8")
    rep = _with_gapfill_block(
        "  added 1200 packages in 3m 02s\n"
        "cache miss - cold start, rebuilding\n"      # log had an em-dash; report shows a hyphen
        "webpack build - production mode")
    assert _tag_for(rep, _GROUND, tmp_path, findings=_gapfill_findings(str(tmp_path))) == "PASS"


def test_gap_fill_evidence_grounded_fails_on_a_fabricated_line(tmp_path: Path):
    # A quoted line that appears in NO captured log line → FAIL (the LLM reading fabricated it).
    (tmp_path / "build.log").write_text(_GAPFILL_LOG, encoding="utf-8")
    rep = _with_gapfill_block(
        "  added 1200 packages in 3m 02s\n"
        "  added 9999 packages in 9m 09s")           # never in the log
    assert _tag_for(rep, _GROUND, tmp_path, findings=_gapfill_findings(str(tmp_path))) == "FAIL"


def test_gap_fill_evidence_grounded_skips_loud_when_logs_absent(tmp_path: Path):
    # The block renders evidence but the captured log can't be located (logs_dir absent, or the file
    # is gone from a moved scratch dir) → loud SKIP, never a silent pass.
    rep = _with_gapfill_block("  added 1200 packages in 3m 02s")
    # (a) no logs_dir stamped in the bundle at all.
    assert _tag_for(rep, _GROUND, tmp_path, findings=_gapfill_findings(None)) == "SKIP"
    # (b) logs_dir points at a dir with no such file on disk (moved scratch / legacy artifact).
    gone = str(tmp_path / "moved-away")
    assert _tag_for(rep, _GROUND, tmp_path, findings=_gapfill_findings(gone)) == "SKIP"


def test_gap_fill_evidence_grounded_noops_without_a_block(tmp_path: Path):
    # A legacy/normal report with no 🤖 gap-fill block has nothing to ground → PASS (never a FAIL),
    # even with no --findings at all.
    assert _tag_for(_good(), _GROUND, tmp_path) == "PASS"


def test_expected_drilled_poles_collapses_matrix_legs_like_the_renderer(tmp_path: Path):
    # Two space-variant matrix legs ("Python 3.9"/"3.13") that share one `job` ("tests"). The
    # renderer collapses them into ONE pole via `_same_matrix`; `_expected_drilled_poles` mirrors
    # that exact predicate, so a 1-pole report PASSES.
    findings = {"pr_critical_path": {"poles": [
        {"check": "Python 3.9", "workflow_file": _WF, "job": "tests", "p50_s": 65.0},
        {"check": "Python 3.13", "workflow_file": _WF, "job": "tests", "p50_s": 62.0}]}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=findings) == "PASS"


def test_expected_drilled_poles_collapses_legs_named_by_full_variant(tmp_path: Path):
    # Escape-Technologies/graphql-armor regression: ONE matrix job (`name: Examples Node
    # ${{ matrix.node }}`, node:[18,20,22,24]) surfaces as 4 check-runs `Examples Node 18..24`,
    # and the findings set each leg's `job` to its OWN full leg name (job == check per leg, NOT a
    # shared `tests`). The renderer's `_same_matrix` token-diff rule collapses all four into ONE
    # rendered pole. The OLD `_expected_drilled_poles` grouped on the per-leg `job`, so it counted
    # 4 distinct groups, capped at 2, and FAILed the correct 1-pole report as "a pole was silently
    # dropped". Keying on the renderer's `_same_matrix` (check name + workflow) collapses these to
    # one expected pole, so the valid report PASSES.
    legs = [{"check": f"Examples Node {n}", "workflow_file": ".github/workflows/ci.yaml",
             "job": f"Examples Node {n}", "p50_s": 120.0 - n} for n in (18, 20, 22, 24)]
    findings = {"pr_critical_path": {"poles": legs}}
    assert _tag_for(_good(), _POLES, tmp_path, findings=findings) == "PASS"
    # Sanity: two genuinely-distinct gating jobs in DIFFERENT workflows still count as 2 — the fix
    # must not over-collapse and mask a real dropped second pole (the `_TWO_GATE` FAIL still holds).
    assert _tag_for(_good(), _POLES, tmp_path, findings=_TWO_GATE) == "FAIL"


def test_same_matrix_stays_coupled_to_the_engine():
    # `_expected_drilled_poles` collapses matrix legs with a LOCAL `_same_matrix` copy (verify_report
    # is standalone — no blocking_path import). If the engine retunes its matrix-collapse rule and the
    # verifier's copy lags, the verifier would expect a different pole count than the renderer drills —
    # the exact false-positive this fix removes, re-introduced in reverse. Pin them behavior-equal over
    # the shapes that exercise every branch (paren-base, full-variant token-diff, prefix/suffix, the
    # cross-workflow guard, and clear non-matrix pairs).
    vr = _load_verify_report()
    bp = _load_blocking_path()
    wf = ".github/workflows/ci.yaml"
    cases = [
        ("Examples Node 18", "Examples Node 20", wf, wf),    # full-variant single-token diff
        ("Examples Node 18", "Examples Node 24", wf, wf),
        ("Python 3.9", "Python 3.13", wf, wf),               # space-variant, shared job
        ("test (22.x)", "test (24.x)", wf, wf),              # paren matrix base
        ("integration-test (3.1.1, 3.12)", "integration-test (3.1.1, 3.13)", wf, wf),
        ("prisma-adapter Integration Test", "drizzle-adapter Integration Test", wf, wf),  # prefix/suffix
        ("build", "test", wf, wf),                            # distinct, never a matrix
        ("build", "build", wf, wf),                           # identical
        ("@a/db build", "@b/db build", wf, wf),               # scoped monorepo
        ("Python 3.13", "Python 3.13", "a.yml", "b.yml"),    # same name, DIFFERENT workflow → not one matrix
    ]
    mism = [(a, b) for a, b, wa, wb in cases
            if vr._same_matrix(a, b, wa, wb) != bp._same_matrix(a, b, wa, wb)]
    assert not mism, (
        f"verify_report._same_matrix drifted from blocking_path._same_matrix on {mism!r} — "
        "re-sync the verifier's matrix-collapse copy with the engine's `_same_matrix`")


_DROPPED_INTEGRATION = {
    "pr_critical_path": {"dropped_non_required_checks": ["Run integration tests"]}}


def test_off_spine_check_fails_when_a_dropped_check_is_rendered_as_a_pole(tmp_path: Path):
    # encord §6: `Run integration tests` is excluded from the spine (non-required) yet appears
    # as a Long-pole header — a header asserts it IS the critical path, contradicting the
    # footnote. The check must FAIL.
    bad = _good() + (
        '\n<a id="pole-2"></a>\n\n'
        '## Long pole 2: `sdk-pr.yml` ▸ Run integration tests - 17m 20s\n\n'
        '```text\nWhere the job goes.\n```\n\n'
        '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate.\n```\n')
    assert _tag_for(bad, _OFFSPINE, tmp_path, findings=_DROPPED_INTEGRATION) == "FAIL"


def test_off_spine_check_passes_when_no_dropped_check_headlines(tmp_path: Path):
    # The clean report's poles are spine checks, none dropped → PASS.
    assert _tag_for(_good(), _OFFSPINE, tmp_path, findings=_DROPPED_INTEGRATION) == "PASS"


def _scoped_pole(check: str) -> str:
    # The renderer ALWAYS strips the `@scope/` prefix (`_clean_label`), so a real monorepo pole
    # header shows the stripped label — NOT the scoped check-run name. Fixtures must mirror that.
    return (f'\n<a id="pole-2"></a>\n\n## Long pole 2: `pkg.yml` ▸ {check} - 8m 00s\n\n'
            '```text\nWhere the job goes.\n```\n\n'
            '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate.\n```\n')


def test_off_spine_check_now_catches_monorepo_scoped_contradiction(tmp_path: Path):
    # The fix: `dropped_*` stores the SCOPED name `@a/pkg build` while the pole header renders the
    # STRIPPED `pkg build`. The old exact-name compare never intersected → it was BLIND to every
    # monorepo contradiction (the scoped-check class #99 came from). `_cmp_name` strips scope on
    # both sides, so a dropped `@a/pkg build` rendered as its own `pkg build` pole now FAILS.
    mono = {"pr_critical_path": {"dropped_non_required_checks": ["@a/pkg build"]}}
    assert _tag_for(_good() + _scoped_pole("pkg build"), _OFFSPINE, tmp_path,
                    findings=mono) == "FAIL"
    # ACCEPTED RESIDUAL (documented in `_cmp_name`): a DIFFERENT package's `@b/pkg build` framed
    # on-path while `@a/pkg build` is dropped is indistinguishable after the strip → also flags.
    # For a CI regression net, this false-POSITIVE (a noisy block a human clears) is the right
    # bias vs the false-NEGATIVE it replaces (a shipped monorepo contradiction). Fleet-validated
    # 0/16 — the collision doesn't occur on real reports. A precise fix needs (check, wf) identity.
    assert _tag_for(_good() + _scoped_pole("pkg build"), _OFFSPINE, tmp_path,
                    findings={"pr_critical_path":
                              {"dropped_non_required_checks": ["@b/pkg build"]}}) == "FAIL"


# The "Also noticed" appendix on-path framing — where the ORIGINAL encord contradiction
# actually shipped (an OPT24 row, not a header). The check must catch this manifestation too.
_APPENDIX_ON_PATH = (
    "\n<details>\n"
    "<summary><strong>OPT24 - Long Test Job Without Sharding</strong> · ~17m 20s wall-clock · "
    "HIGH · 2 across 2 wf</summary>\n\n"
    "**Where:** `sdk-pr.yml` (Run integration tests), `test-sdk.yml` (Run integration tests)\n"
    "**Wall-clock:** unlike the other findings in this section, this one **sits ON the "
    "merge-gating critical path** (a long pole) - its catalog fix cuts developer wall-clock.\n"
    "</details>\n")


def test_off_spine_check_fails_on_appendix_on_path_framing_of_a_dropped_check(tmp_path: Path):
    # The dropped `Run integration tests` is framed "sits ON the merge-gating critical path" in
    # an appendix <details> row (its **Where:** names it) — the exact encord §6 shape. FAIL.
    bad = _good() + _APPENDIX_ON_PATH
    assert _tag_for(bad, _OFFSPINE, tmp_path, findings=_DROPPED_INTEGRATION) == "FAIL"


def test_off_spine_check_no_false_positive_on_short_name_substring_of_a_longer_job(tmp_path: Path):
    # The appendix Where-line names a longer job `integration-test-suite`; the dropped check is the
    # short `test`. Exact job-segment extraction must NOT treat `test` ⊂ `integration-test-suite`
    # as a contradiction (the raw-substring scan did). PASS.
    long_job = _APPENDIX_ON_PATH.replace("Run integration tests", "integration-test-suite")
    findings = {"pr_critical_path": {"dropped_non_required_checks": ["test"]}}
    assert _tag_for(_good() + long_job, _OFFSPINE, tmp_path, findings=findings) == "PASS"


def test_off_spine_check_catches_dropped_job_in_a_truncated_where_line(tmp_path: Path):
    # When an on-path group has >8 occurrences the renderer truncates: `j1`, …, `j8`, +N more.
    # The 8th (last-shown) segment is followed by `, +N more`, not `, \`` — the Where-job regex
    # must still extract it (the lookahead allows the `+` truncation suffix), or the 8th job's
    # contradiction is silently missed.
    where = ("**Where:** " + ", ".join(f"`ci.yml:{i}` (job{i})" for i in range(1, 9))
             + ", +5 more")
    block = ("\n<details>\n<summary><strong>OPT24</strong></summary>\n\n" + where + "\n"
             "**Wall-clock:** this one **sits ON the merge-gating critical path** (a long pole).\n"
             "</details>\n")
    findings = {"pr_critical_path": {"dropped_non_required_checks": ["job8"]}}
    assert _tag_for(_good() + block, _OFFSPINE, tmp_path, findings=findings) == "FAIL"


def test_off_spine_check_passes_when_appendix_on_path_names_only_kept_checks(tmp_path: Path):
    # Same appendix on-path note, but it frames `Linting and type checking` (NOT dropped) — no
    # contradiction. And the scope-prefixed dropped `@a/pkg build` never matches job `build`.
    kept_note = _APPENDIX_ON_PATH.replace("Run integration tests", "Linting and type checking")
    assert _tag_for(_good() + kept_note, _OFFSPINE, tmp_path,
                    findings=_DROPPED_INTEGRATION) == "PASS"
    mono = {"pr_critical_path": {"dropped_non_required_checks": ["@a/pkg build"]}}
    build_note = _APPENDIX_ON_PATH.replace("Run integration tests", "build")
    assert _tag_for(_good() + build_note, _OFFSPINE, tmp_path, findings=mono) == "PASS"


# --- the SAME cross-seam property, extended to the typical/rare (presence-demotion) class ---
# paradedb `Test pg_search`: a heavy job the spine DEMOTES as opt-in/rare (present on a MINORITY
# of PRs) must not ALSO be framed "sits ON the merge-gating critical path" in the appendix. The
# demotion is re-derived from `pr_critical_path.checks` presence (no `pole_n` here → legacy rule).
_PG = "Test pg_search on PostgreSQL 18 (pgrx - arm64)"


def _rare_demoted_findings(rare_present: int = 8, npop: int = 20) -> dict:
    # `build` present on every PR (typical); `_PG` present on a minority (rare/opt-in). No pole_n
    # stamped → `_rare_demoted_check_names` uses the legacy presence rule (present > npop*0.5).
    build_only = npop - rare_present
    pops = ([[0.05, [["build", 1000.0]]] for _ in range(build_only)]
            + [[0.05, [["build", 1000.0], [_PG, 1351.0]]] for _ in range(rare_present)])
    return {"pr_critical_path": {
        "check_present_n_pr": npop,
        "checks": [
            {"name": "build", "p50_s": 1000.0, "present_on": npop, "workflow_file": "ci.yml"},
            {"name": _PG, "p50_s": 1351.0, "present_on": rare_present,
             "workflow_file": "test-pg_search.yml"},
        ],
        "populations": pops,
    }}


def _pg_appendix_on_path() -> str:
    return _APPENDIX_ON_PATH.replace("Run integration tests", _PG)


def test_property_fails_when_a_rare_demoted_job_is_framed_on_path_in_appendix(tmp_path: Path):
    # The double-framing bug: `_PG` is presence-demoted (opt-in/rare) by the spine, yet the
    # appendix frames it "sits ON the merge-gating critical path". FAIL.
    bad = _good() + _pg_appendix_on_path()
    assert _tag_for(bad, _OFFSPINE, tmp_path, findings=_rare_demoted_findings()) == "FAIL"


def test_property_passes_when_appendix_on_path_names_only_a_typical_check(tmp_path: Path):
    # Same appendix on-path note, but framing the TYPICAL `build` (present on every PR) — no
    # contradiction, `build` is genuinely on the typical path. PASS.
    ok = _good() + _APPENDIX_ON_PATH.replace("Run integration tests", "build")
    assert _tag_for(ok, _OFFSPINE, tmp_path, findings=_rare_demoted_findings()) == "PASS"


def test_property_allows_a_rare_demoted_job_as_a_demoted_pole_header(tmp_path: Path):
    # A rare-demoted check IS legitimately drilled as a demoted "Long pole N" header (it carries
    # its own opt-in body framing) — a header is NOT the contradiction for the rare class, only
    # the appendix "sits ON" note is. So a pole header naming `_PG` must PASS (guards the
    # false-positive that reusing the full framed set for the rare arm would cause).
    header = (f'\n<a id="pole-2"></a>\n\n## Long pole 2: `test-pg_search.yml` ▸ {_PG} - 22m 31s\n\n'
              '```text\nWhere the job goes.\n```\n\n'
              '#### 🤖 Prompt for your coding agent\n\n```text\ninvestigate.\n```\n')
    assert _tag_for(_good() + header, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings()) == "PASS"


def test_property_does_not_demote_when_present_on_a_majority(tmp_path: Path):
    # A check present on a MAJORITY (14/20) is typical, not demoted — so framing it on-path is NO
    # contradiction. With nothing dropped and nothing demoted the check SKIPs (never FAILs) — the
    # re-derivation must not over-demote a majority check into a false positive.
    ok = _good() + _pg_appendix_on_path()
    assert _tag_for(ok, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings(rare_present=14)) == "SKIP"


def test_property_does_not_demote_on_a_too_small_sample(tmp_path: Path):
    # Below the min-PR floor (npop < _VR_RARE_PRESENCE_MIN_PR) the presence fraction is noise, so
    # nothing is demoted (mirrors _typical_check's small-sample guard) — even a 1/5 minority. With
    # nothing excluded the check SKIPs, never FAILs (no false contradiction on a tiny sample).
    ok = _good() + _pg_appendix_on_path()
    assert _tag_for(ok, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings(rare_present=1, npop=5)) == "SKIP"


def test_property_skips_when_nothing_dropped_or_demoted(tmp_path: Path):
    # No dropped checks AND no rare-demoted checks (build typical, no minority check) → SKIP,
    # honest — nothing excluded to contradict.
    findings = {"pr_critical_path": {
        "check_present_n_pr": 20,
        "checks": [{"name": "build", "p50_s": 1000.0, "present_on": 20, "workflow_file": "ci.yml"}],
        "populations": [[0.05, [["build", 1000.0]]] for _ in range(20)]}}
    assert _tag_for(_good(), _OFFSPINE, tmp_path, findings=findings) == "SKIP"


# --- Finding 2: the presence-minority clause on the MODERN `pole_n` path (verifier mirror) -------
# On the pole_n path `_typical` demotes any leg with `pole_n < _VR_POLE_RECUR_FLOOR`, so a
# FREQUENCY-demoted matrix leg present on EVERY PR is `not _typical`. The presence-minority clause
# in `_opt_in_rare` is what keeps it OUT of the demoted set (present > npop*0.5), so framing it
# on-path is NOT a contradiction. Delete that clause and the verifier over-demotes the leg → the
# PASS test below flips to FAIL. Mirrors `test_spine_rare_presence_clause_guards_...` on the engine.
_ALWAYS_LEG = "build (3.10, windows-latest)"      # pole_n 0 but present on all PRs — NOT opt-in
_MIN_SUITE = "Heavy conditional suite"            # pole_n 0 AND minority-present — opt-in/rare


def _pole_n_findings(min_present: int = 4, npop: int = 11) -> dict:
    # Every check carries `pole_n` → `_rare_demoted_check_names` takes the pole-frequency branch for
    # `_typical` and the presence clause for `_opt_in_rare`. `lint` is the typical gate (pole_n high),
    # `_ALWAYS_LEG` is frequency-demoted but present on every PR, `_MIN_SUITE` is minority-present.
    always_present = npop
    pops = ([[0.05, [["lint", 100.0], [_ALWAYS_LEG, 900.0]]] for _ in range(npop - min_present)]
            + [[0.05, [["lint", 100.0], [_ALWAYS_LEG, 900.0], [_MIN_SUITE, 1300.0]]]
               for _ in range(min_present)])
    return {"pr_critical_path": {
        "check_present_n_pr": npop,
        "checks": [
            {"name": "lint", "p50_s": 100.0, "present_on": npop, "pole_n": npop,
             "workflow_file": "ci.yml"},
            {"name": _ALWAYS_LEG, "p50_s": 900.0, "present_on": always_present, "pole_n": 0,
             "workflow_file": "ci.yml"},
            {"name": _MIN_SUITE, "p50_s": 1300.0, "present_on": min_present, "pole_n": 0,
             "workflow_file": "heavy.yml"},
        ],
        "populations": pops}}


def test_property_pole_n_path_passes_when_always_present_leg_framed_on_path(tmp_path: Path):
    # `_ALWAYS_LEG` is frequency-demoted (pole_n 0) but present on every PR — the presence clause
    # keeps it OUT of the opt-in/rare set, so framing it on-path is legitimate. PASS. (Removing the
    # presence-minority clause over-demotes it and turns this into a FAIL — the load-bearing guard.)
    ok = _good() + _APPENDIX_ON_PATH.replace("Run integration tests", _ALWAYS_LEG)
    assert _tag_for(ok, _OFFSPINE, tmp_path, findings=_pole_n_findings()) == "PASS"


def test_property_pole_n_path_fails_when_minority_suite_framed_on_path(tmp_path: Path):
    # `_MIN_SUITE` is pole_n-demoted AND minority-present (4/11) → opt-in/rare. Framing it on-path
    # in the appendix contradicts the spine's "a typical PR doesn't wait on it" footnote. FAIL.
    bad = _good() + _APPENDIX_ON_PATH.replace("Run integration tests", _MIN_SUITE)
    assert _tag_for(bad, _OFFSPINE, tmp_path, findings=_pole_n_findings()) == "FAIL"


# --- Finding 5: threshold boundaries of the minority decision `present <= npop * 0.5` -------------
def test_property_rare_boundary_exactly_half_demotes(tmp_path: Path):
    # Exactly 50% (10/20): `_typical` is `present > npop*0.5` → 10 > 10 is False → not typical;
    # `_opt_in_rare` is `present <= npop*0.5` → 10 <= 10 is True → demoted. Framing on-path FAILs.
    # Guards the `<=`/`<` and `>`/`>=` boundaries (either flip would drop this from the demoted set).
    bad = _good() + _pg_appendix_on_path()
    assert _tag_for(bad, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings(rare_present=10, npop=20)) == "FAIL"


def test_property_rare_boundary_just_over_half_not_demoted(tmp_path: Path):
    # Just over 50% (11/20): 11 > 10 → typical → NOT demoted. With nothing dropped the check SKIPs.
    # A `>`→`>=` flip in `_typical` would demote it and FAIL — this pins the boundary the other way.
    ok = _good() + _pg_appendix_on_path()
    assert _tag_for(ok, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings(rare_present=11, npop=20)) == "SKIP"


def test_property_rare_boundary_at_min_pr_sample_floor(tmp_path: Path):
    # At the sample-size floor npop == _VR_RARE_PRESENCE_MIN_PR (6): the floor is INCLUSIVE
    # (`npop >= 6`), so demotion is active. 3/6 <= 3.0 → demoted → framing on-path FAILs. (npop == 5
    # is below the floor → nothing demoted → SKIP, covered by the too-small-sample test above.)
    bad = _good() + _pg_appendix_on_path()
    assert _tag_for(bad, _OFFSPINE, tmp_path,
                    findings=_rare_demoted_findings(rare_present=3, npop=6)) == "FAIL"


# --- The demoted-pole typical-gate-framing class (caddy goreleaser-check) ------------------------
_DEMOTED_GATE = "typical-gate framing"    # matches check_demoted_pole_not_framed_typical_gate's name


def _demoted_pole_findings() -> dict:
    # `test` is the typical gate (pole_n high, present on every PR); `goreleaser-check` is FREQUENCY-
    # demoted — present on a MAJORITY (13/20) but the actual slowest on 0/20 → `not _typical` yet NOT
    # opt-in/rare. `_non_typical_pole_check_names` returns {goreleaser-check}; the caddy class.
    return {"pr_critical_path": {
        "sampled_pr_count": 20, "check_present_n_pr": 20,
        "checks": [
            {"name": "test", "p50_s": 400.0, "present_on": 20, "pole_n": 20,
             "workflow_file": ".github/workflows/ci.yml"},
            {"name": "goreleaser-check", "p50_s": 168.0, "present_on": 13, "pole_n": 0,
             "workflow_file": ".github/workflows/ci.yml"},
        ],
        "populations": ([[0.05, [["test", 400.0], ["goreleaser-check", 168.0]]] for _ in range(13)]
                        + [[0.05, [["test", 400.0]]] for _ in range(7)])}}


def _demoted_pole_report(demoted_prompt: str, demoted_toc_tail: str) -> str:
    # Minimal report exercising BOTH render sites: the Contents rows and the two pole drill sections.
    # `test` (typical) always keeps its typical-gate framing; only `goreleaser-check` (demoted) varies.
    return (
        "# caddyserver/caddy - why is the merge slow?\n\n"
        "## 📋 Contents\n\n"
        "1. 🔴 [`test`](#pole-1) - 6m 40s · `ci.yml` gates 20/20 PRs\n"
        f"2. 🟠 [`goreleaser-check`](#pole-2) - 2m 48s{demoted_toc_tail}\n\n"
        "## 🔴 Long pole 1: `ci.yml` ▸ test - 6m 40s\n\n"
        "**The slowest check a typical PR waits on.**\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\nTHE GATE\n"
        "- Slowest check a typical PR waits on: P50 6m 40s; its workflow `ci.yml` gates 20/20 sampled PRs.\n"
        "```\n\n"
        "## 🟠 Long pole 2: `ci.yml` ▸ goreleaser-check - 2m 48s\n\n"
        "**Rarely the merge gate - the actual slowest check a PR waits on, on only 0/20 sampled PRs.**\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\nTHE GATE\n"
        f"- {demoted_prompt}\n```\n")


def test_demoted_pole_fails_when_prompt_asserts_typical_gate(tmp_path: Path):
    # The bug: the demoted `goreleaser-check` pole's agent prompt says "Slowest check a typical PR
    # waits on" — contradicting its own "Rarely the merge gate" header. FAIL.
    bad = _demoted_pole_report(
        demoted_prompt="Slowest check a typical PR waits on: P50 2m 48s; its workflow `ci.yml` gates 20/20 sampled PRs.",
        demoted_toc_tail=" · rarely the merge pole")
    assert _tag_for(bad, _DEMOTED_GATE, tmp_path, findings=_demoted_pole_findings()) == "FAIL"


def test_demoted_pole_fails_when_toc_row_carries_gate_count(tmp_path: Path):
    # The Contents-row half of the class: the demoted pole's row borrows the workflow's typical-gate
    # count ("`ci.yml` gates 20/20 PRs") — the sibling `test` matrix's frequency, not this pole's. FAIL.
    bad = _demoted_pole_report(
        demoted_prompt="Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 13/20): P50 2m 48s.",
        demoted_toc_tail=" · `ci.yml` gates 20/20 PRs")
    assert _tag_for(bad, _DEMOTED_GATE, tmp_path, findings=_demoted_pole_findings()) == "FAIL"


def test_demoted_pole_passes_when_framed_to_match_its_header(tmp_path: Path):
    # The engine fix: the demoted pole's prompt uses the "Rarely the merge pole" framing and its
    # Contents row drops the gate-count tag ("rarely the merge pole"). The TYPICAL `test` pole keeps
    # its own typical-gate framing throughout — only the demoted pole is reframed. PASS.
    good = _demoted_pole_report(
        demoted_prompt="Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 13/20): P50 2m 48s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.",
        demoted_toc_tail=" · rarely the merge pole")
    assert _tag_for(good, _DEMOTED_GATE, tmp_path, findings=_demoted_pole_findings()) == "PASS"


# --- Transitive `needs:` chain member on-spine (home-assistant/core, issue #112) -----------------
def _chain_member_findings(*, with_chain: bool = True, sink_required: bool = True) -> dict:
    # The home-assistant/core shape: a modal `needs:` gate chain
    # `Collect information → Prepare dependencies → Check hassfest` feeding the REQUIRED sink
    # `Check hassfest` (18/18 PRs). The mid-chain stage `Prepare dependencies (3.14.5)` is NOT in
    # required_checks and is the actual slowest on 0/18 PRs (`pole_n` 0) — so without the chain arm
    # in `_vr_typical_predicate` it mis-classifies as spine-demoted, exactly the false positive #112
    # reports. `with_chain=False` drops the chain facts (the pre-fix control: the stage IS demoted).
    # `sink_required=False` points the chain at a non-required sink (the required-feed guard: the
    # stage stays demoted, so a chain that gates nothing required can't launder a rare check on-spine).
    # The chain lives in `chain_facts` (the SINGLE-DOOR source verify re-derives from via
    # `_vr_modal_chain` + the `chain_s` median); `chain_summary` is the renderer's reduced input, kept
    # alongside for realism (real artifacts stamp both) — verify must NOT read it back.
    cp: dict = {
        "sampled_pr_count": 18, "check_present_n_pr": 18,
        "critical_path_check": "Check hassfest",
        "checks": [
            {"name": "Check hassfest", "p50_s": 63.0, "present_on": 18, "pole_n": 18,
             "workflow_file": ".github/workflows/ci.yaml"},
            {"name": "Prepare dependencies (3.14.5)", "p50_s": 37.0, "present_on": 18, "pole_n": 0,
             "workflow_file": ".github/workflows/ci.yaml"},
            {"name": "Collect information & changes data", "p50_s": 21.0, "present_on": 18,
             "pole_n": 0, "workflow_file": ".github/workflows/ci.yaml"},
            # A genuinely spine-demoted pole (NOT a chain member, NOT required, `pole_n` 0) that
            # coexists with the chain stage, so the demoted set is non-empty and the demoted-gate
            # check actively runs its offender loop — proving it SKIPS the chain member's section
            # (exempted, not in the demoted set) while still policing a real demoted pole.
            {"name": "goreleaser-check", "p50_s": 20.0, "present_on": 12, "pole_n": 0,
             "workflow_file": ".github/workflows/ci.yaml"},
        ],
        "populations": [[0.05, [["Check hassfest", 63.0], ["Prepare dependencies (3.14.5)", 37.0],
                                ["Collect information & changes data", 21.0]]] for _ in range(18)],
    }
    if with_chain:
        chain = ["Collect information & changes data",
                 "Prepare dependencies (3.14.5)",
                 "Check hassfest" if sink_required else "Docs build (not required)"]
        cp["chain_facts"] = [{"chain": list(chain), "chain_s": 116.0} for _ in range(18)]
        cp["chain_summary"] = {"modal_chain": list(chain), "chain_p50_s": 116.0}
    required = ["Check hassfest", "Collect information & changes data"]
    return {"pr_critical_path": cp, "required_checks": required}


def test_chain_member_predicate_is_on_spine_when_chain_feeds_required():
    # Unit-pin the SHARED predicate (`_vr_typical_predicate` via `_non_typical_pole_check_names`):
    # a modal-chain member feeding a required sink is ON-spine (not demoted). RED on main's predicate,
    # which recognizes on-spine only via `required_checks` membership or a recurring `pole_n`.
    vr = _load_verify_report()
    mid = vr._cmp_name("Prepare dependencies (3.14.5)")
    # Fixed behavior: the chain member is NOT in the demoted set.
    demoted = vr._non_typical_pole_check_names(_chain_member_findings())
    assert mid not in demoted, f"chain member wrongly demoted: {demoted}"
    # Pre-fix control (no chain facts): the exact same stage IS demoted — proves the arm is what
    # rescues it, not some unrelated change to presence/pole_n handling.
    demoted_no_chain = vr._non_typical_pole_check_names(
        _chain_member_findings(with_chain=False))
    assert mid in demoted_no_chain, "control: stage must be demoted without the chain facts"
    # Required-feed guard: a chain whose sink is NOT required must not launder the stage on-spine.
    demoted_no_req = vr._non_typical_pole_check_names(
        _chain_member_findings(sink_required=False))
    assert mid in demoted_no_req, "guard: non-required-sink chain must not exempt its members"


def test_chain_member_predicate_reads_facts_not_summary():
    # Single-door pin (#19): verify re-derives the modal chain from `chain_facts`, NEVER from the
    # renderer's reduced `chain_summary`. A summary that CLAIMS the member on-spine (feeding a
    # required sink) but is UNBACKED by chain_facts must NOT exempt it — else a mis-reduced or
    # doctored summary could launder a rare pole onto the spine. RED if the arm ever reverts to
    # reading `chain_summary`.
    vr = _load_verify_report()
    mid = vr._cmp_name("Prepare dependencies (3.14.5)")
    data = _chain_member_findings()
    # Keep the (honest-looking) chain_summary but strip the chain_facts that back it.
    data["pr_critical_path"].pop("chain_facts", None)
    demoted = vr._non_typical_pole_check_names(data)
    assert mid in demoted, f"summary-only chain must not exempt its members: {demoted}"


def _chain_member_report(mid_prompt: str) -> str:
    # A report drilling the mid-chain stage. `mid_prompt` is its agent-prompt "THE GATE" line — the
    # live renderer gives a chain member the chain-framed line, but pin that the CHECK accepts the
    # typical-gate line for a genuine chain member (no false positive) and still rejects it for a
    # true demoted pole (the still-strict pin below reuses this body with non-chain findings).
    return (
        "# home-assistant/core - why is the merge slow?\n\n"
        "## 📋 Contents\n\n"
        "1. 🔴 [`Check hassfest`](#pole-1) - 1m 03s\n"
        "2. 🟡 [`Prepare dependencies (3.14.5)`](#pole-2) - 37s\n"
        "3. 🟡 [`goreleaser-check`](#pole-3) - 20s\n\n"
        "## 🔴 Long pole 1: `ci.yaml` ▸ Check hassfest - 1m 03s\n\n"
        "**Stage 3/3 of the gate chain.**\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\nTHE GATE\n"
        "- Stage 3/3 of the `needs:` gate chain (chain P50 1m 56s); this stage: P50 1m 03s.\n"
        "```\n\n"
        "## 🟡 Long pole 2: `ci.yaml` ▸ Prepare dependencies (3.14.5) - 37s\n\n"
        "**Stage 2/3 of the gate chain.**\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\nTHE GATE\n"
        f"- {mid_prompt}\n```\n\n"
        # A genuinely-demoted pole, correctly framed (no typical-gate phrase, no gate-count tail) —
        # so it adds no offender and the check reaches a genuine PASS while actively running.
        "## 🟡 Long pole 3: `ci.yaml` ▸ goreleaser-check - 20s\n\n"
        "**Rarely the merge gate - the actual slowest check a PR waits on: P50 20s.**\n\n"
        "#### 🤖 Prompt for your coding agent\n\n```text\nTHE GATE\n"
        "- Rarely the merge gate; speeding it helps only the PRs where it IS the pole.\n```\n")


def test_chain_member_pole_passes_even_with_typical_gate_framing(tmp_path: Path):
    # End-to-end (issue #112): with the chain findings the mid-chain stage is ON-spine, so the
    # typical-gate check no longer fires on it — even the literal "Slowest check a typical PR waits on"
    # line is accepted, because the stage genuinely is on the gate path (the renderer prefers the
    # chain framing, but the CHECK must not false-fail either wording). The coexisting genuinely-
    # demoted `goreleaser-check` keeps the demoted set NON-empty, so the check actively runs its
    # offender loop and reaches a real PASS (not a vacuous SKIP): it policed the demoted pole and
    # correctly skipped the exempted chain member. RED on main's predicate (FAILs on the chain stage).
    rep = _chain_member_report(
        "Slowest check a typical PR waits on: P50 37s.")
    assert _tag_for(rep, _DEMOTED_GATE, tmp_path,
                    findings=_chain_member_findings()) == "PASS"


def test_chain_member_still_strict_without_the_chain(tmp_path: Path):
    # The still-strict pin: the SAME report body framed on-gate, but with the chain facts REMOVED
    # (the stage is now a genuine spine-demoted pole), must STILL FAIL — the fix must not soften the
    # check for a truly-demoted pole carrying the typical-gate framing.
    rep = _chain_member_report(
        "Slowest check a typical PR waits on: P50 37s.")
    assert _tag_for(rep, _DEMOTED_GATE, tmp_path,
                    findings=_chain_member_findings(with_chain=False)) == "FAIL"


_SLOWEST = "headline 'slowest check' names the data layer's critical_path_check"


def _headline(check: str) -> str:
    return (f"\n**4m 15s until all checks finish** - `{check}` is the slowest check a "
            "typical PR waits on. The 2 heaviest checks run in parallel on a PR.\n")


def _headline_form2(slowest: str, gate: str = "lint") -> str:
    # The floor!=gate rendered form (`blocking_path.py` ~4257): the slowest typical check is NAMED
    # AFTER the phrase, and a DIFFERENT, more-frequent check is the gate. The named slowest is the
    # renderer's `floor_name` = `src[0].name` = the slowest TYPICAL check = `critical_path_check`.
    return (f"\n**4m 15s until all checks finish** - the slowest check a typical PR waits on is "
            f"`{slowest}` (~4m 15s); `{gate}` is the check most PRs gate on (drilled below).\n")


def test_property_fails_when_a_dropped_check_is_the_headline_slowest(tmp_path: Path):
    # The contradiction property (Phase 0) covers the HEADLINE context too: a check the spine
    # footnote EXCLUDED that is also named the headline "slowest" is a contradiction. FAIL.
    findings = {"pr_critical_path": {"dropped_non_required_checks": ["deploy"],
                                     "critical_path_check": "deploy"}}
    assert _tag_for(_good() + _headline("deploy"), _OFFSPINE, tmp_path,
                    findings=findings) == "FAIL"


def test_comparator_fails_when_headline_slowest_disagrees_with_stamp(tmp_path: Path):
    # The renderer re-derived a "slowest" superlative naming a different check than the data
    # layer's stamped p50 winner (critical_path_check) — the A3 re-derivation. FAIL.
    findings = {"pr_critical_path": {"critical_path_check": "Lint"}}
    assert _tag_for(_good() + _headline("build"), _SLOWEST, tmp_path, findings=findings) == "FAIL"


def test_comparator_passes_when_headline_matches_stamp_incl_scope_strip(tmp_path: Path):
    # Exact match passes; and the headline's scope-stripped label matches the full stamp
    # (`@scope/pkg build` stamp vs `pkg build` headline) without a false positive.
    exact = {"pr_critical_path": {"critical_path_check": "build"}}
    assert _tag_for(_good() + _headline("build"), _SLOWEST, tmp_path, findings=exact) == "PASS"
    scoped = {"pr_critical_path": {"critical_path_check": "@scope/pkg build"}}
    assert _tag_for(_good() + _headline("pkg build"), _SLOWEST, tmp_path,
                    findings=scoped) == "PASS"


def test_comparator_skips_only_when_there_is_no_slowest_claim(tmp_path: Path):
    # No headline "slowest" claim → nothing to contradict → SKIP (honest, not a false PASS).
    # But a report WITH the claim must never SKIP — it compares (guards the 3rd-review #1
    # silent-SKIP-on-CI-data failure mode).
    findings = {"pr_critical_path": {"critical_path_check": "build"}}
    assert _tag_for(_good(), _SLOWEST, tmp_path, findings=findings) == "SKIP"
    assert _tag_for(_good() + _headline("build"), _SLOWEST, tmp_path, findings=findings) == "PASS"


def test_comparator_validates_the_floor_not_gate_headline_form(tmp_path: Path):
    # CLASS regression (lancedb): the floor!=gate headline form ("the slowest check a typical PR
    # waits on is `X`; `gate` is the check most PRs gate on") must be VALIDATED against
    # critical_path_check, not SKIPped. The named slowest IS `floor_name` = src[0].name =
    # critical_path_check, so a correctly-targeted comparison PASSes on a match and FAILs on a
    # mislabel — it must NEVER go dark ("no slowest-check headline claim") on this branch, the
    # very branch where a renderer floor-name re-derivation is most likely to drift.
    match = {"pr_critical_path": {"critical_path_check": "windows"}}
    assert _tag_for(_good() + _headline_form2("windows"), _SLOWEST, tmp_path,
                    findings=match) == "PASS"
    # Scope-stripped match (full `@scope/pkg build` stamp vs `pkg build` headline) still reconciles.
    scoped = {"pr_critical_path": {"critical_path_check": "@scope/pkg build"}}
    assert _tag_for(_good() + _headline_form2("pkg build"), _SLOWEST, tmp_path,
                    findings=scoped) == "PASS"
    # A re-derived WRONG floor name (the bug this guard now catches) FAILs — previously SKIPped.
    mismatch = {"pr_critical_path": {"critical_path_check": "linux"}}
    assert _tag_for(_good() + _headline_form2("windows"), _SLOWEST, tmp_path,
                    findings=mismatch) == "FAIL"


def test_contradiction_property_covers_the_floor_not_gate_headline_form(tmp_path: Path):
    # The cross-seam contradiction property also sees the form-2 slowest naming: a spine-DROPPED
    # check named as the floor!=gate headline's slowest is a contradiction (FAIL), not a silent
    # miss. (floor_name is normally a TYPICAL check so this is rare, but the guard must not be blind.)
    findings = {"pr_critical_path": {"dropped_non_required_checks": ["deploy"],
                                     "critical_path_check": "deploy"}}
    assert _tag_for(_good() + _headline_form2("deploy"), _OFFSPINE, tmp_path,
                    findings=findings) == "FAIL"


_FLOOR_RECON = "headline reconciles a non-universal slowest check with the population floor"


def _headline_form2_floor(slowest: str = "windows", gate: str = "lint",
                          slowest_dur: str = "4m 15s", merge_dur: str = "1m 40s",
                          present: int = 4, npop: int = 10, reconciled: bool = True) -> str:
    # The floor-LOWERED form-2 headline: the slowest TYPICAL check is non-universal, so its own
    # `slowest_dur` exceeds the `merge_dur` population floor. `reconciled` toggles the presence
    # clause the fix requires (the buggy renderer emitted the un-reconciled variant).
    recon = (f", but it ran on only {present}/{npop} sampled PRs, so a typical PR finishes in "
             f"{merge_dur}" if reconciled else "")
    return (f"\n**{merge_dur} until all checks finish** - the slowest check a typical PR waits on is "
            f"`{slowest}` (~{slowest_dur}){recon}; `{gate}` is the check most PRs gate on (drilled below).\n")


def _floor_lowered_findings(slowest: str = "windows", slowest_p50: float = 255.0,
                            present: int = 4, npop: int = 10,
                            other: tuple[str, float] = ("lint", 100.0)) -> dict:
    # `present` PRs where `slowest` is the per-PR pole (its p50 the max); the remaining PRs run only
    # `other`. The median of the per-PR maxima is `other`'s p50 whenever present < npop/2 — strictly
    # below `slowest_p50`, so the engine LOWERS the floor (0 < present < npop and pop_floor < p50).
    pops = ([[0.05, [[slowest, slowest_p50], [other[0], other[1]]]] for _ in range(present)]
            + [[0.05, [[other[0], other[1]]]] for _ in range(npop - present)])
    return {"pr_critical_path": {
        "critical_path_check": slowest,
        "checks": [{"name": slowest, "p50_s": slowest_p50}, {"name": other[0], "p50_s": other[1]}],
        "populations": pops}}


def test_floor_reconciliation_fails_when_form2_omits_the_presence_caveat(tmp_path: Path):
    # CLASS regression (tauri `test (windows-latest)`): the form-2 headline named a 4/10 check "the
    # slowest check a typical PR waits on" (~4m 15s) beside a strictly-lower 1m 40s all-checks-finish
    # floor with NO presence disclosure — an internal contradiction re-derivable from `populations`
    # (pop_floor 1m 40s < the check's 4m 15s p50, present 4/10). The un-reconciled form must FAIL.
    findings = _floor_lowered_findings()
    assert _tag_for(_good() + _headline_form2_floor(reconciled=False), _FLOOR_RECON, tmp_path,
                    findings=findings) == "FAIL"
    # The SAME findings with the reconciliation clause present (the engine fix) PASS.
    assert _tag_for(_good() + _headline_form2_floor(reconciled=True), _FLOOR_RECON, tmp_path,
                    findings=findings) == "PASS"


def test_floor_reconciliation_skips_when_floor_is_not_lowered(tmp_path: Path):
    # A UNIVERSAL slowest check (present on every PR) sets the floor at its own p50 — the engine lowers
    # nothing, so no reconciliation is owed and the un-reconciled form-2 headline is correct → SKIP,
    # never a false FAIL. (present == npop: 10/10 → `0 < present < npop` is false.)
    universal = _floor_lowered_findings(present=10, npop=10)
    assert _tag_for(_good() + _headline_form2_floor(reconciled=False), _FLOOR_RECON, tmp_path,
                    findings=universal) == "SKIP"
    # No populations at all → the population floor is underivable → SKIP (keeps the p50 floor).
    no_pops = {"pr_critical_path": {"critical_path_check": "windows",
                                    "checks": [{"name": "windows", "p50_s": 255.0}]}}
    assert _tag_for(_good() + _headline_form2_floor(reconciled=False), _FLOOR_RECON, tmp_path,
                    findings=no_pops) == "SKIP"


def test_floor_reconciliation_skips_chain_and_generic_headlines(tmp_path: Path):
    # The reconciliation is only OWED when the report HEADLINES a slowest check. When the gate is a
    # `needs:` chain the headline is the CHAIN form (no "slowest check a typical PR waits on") and the
    # generic no-populations form only says "a PR waits on the slowest concurrent check" — neither
    # names a slowest check or states a floor to reconcile. The lowered-floor `populations` shape must
    # NOT make the check demand a clause those headlines never render → SKIP, never a false FAIL.
    findings = _floor_lowered_findings()   # a lowered-floor populations shape (present 4/10)
    chain = (_good() + "\n**2m 10s until all checks finish** - the gate is the `compile` -> `test` "
             "chain: `needs:` runs these checks one after another (the longest path on 8/10 sampled PRs).\n")
    assert _tag_for(chain, _FLOOR_RECON, tmp_path, findings=findings) == "SKIP"
    generic = (_good() + "\n**2m 10s until all checks finish.** A PR waits on the slowest concurrent "
               "check.\n")
    assert _tag_for(generic, _FLOOR_RECON, tmp_path, findings=findings) == "SKIP"


def test_floor_reconciliation_binds_the_exact_present_and_floor(tmp_path: Path):
    # L6: the re-derivation pins the ACTUAL present/npop and re-derived floor, not just any caveat.
    # A reconciliation clause that discloses the WRONG presence count (3/10, not the true 4/10) still
    # FAILs — the required literal is re-derived from the data, so a mis-stated count can't launder it.
    findings = _floor_lowered_findings(present=4, npop=10)
    wrong_count = _good() + _headline_form2_floor(present=3, npop=10, reconciled=True)
    assert _tag_for(wrong_count, _FLOOR_RECON, tmp_path, findings=findings) == "FAIL"


def test_floor_reconciliation_binds_the_re_derived_floor_duration(tmp_path: Path):
    # L6 (the FLOOR half): the required clause pins BOTH the present count AND the re-derived floor
    # DURATION. A clause with the RIGHT presence (4/10) but a WRONG floor number (9m 00s, not the
    # 1m 40s re-derived from `populations`) still FAILs — the floor half is data-bound, so a headline
    # can't disclose the right presence beside a contradictory floor number and launder past the check.
    findings = _floor_lowered_findings(present=4, npop=10)   # re-derived floor = 1m 40s (100s median)
    wrong_floor = _good() + _headline_form2_floor(present=4, npop=10, merge_dur="9m 00s", reconciled=True)
    assert _tag_for(wrong_floor, _FLOOR_RECON, tmp_path, findings=findings) == "FAIL"


def test_floor_reconciliation_skips_at_the_strict_floor_equals_p50_boundary(tmp_path: Path):
    # The engine lowers ONLY when `pop_floor < slowest_p50` (STRICT, blocking_path.py). At present=6/10
    # the median of the per-PR maxima ([255]*6 + [100]*4 → 255) equals the slowest check's own p50, so
    # the floor is NOT lowered and no reconciliation is owed → SKIP. Pins the check's `<` against a
    # `<=` drift that would false-FAIL a report whose slowest p50 lands exactly on the population median.
    boundary = _floor_lowered_findings(present=6, npop=10)
    assert _tag_for(_good() + _headline_form2_floor(present=6, npop=10, reconciled=False), _FLOOR_RECON,
                    tmp_path, findings=boundary) == "SKIP"


def test_floor_reconciliation_at_the_min_populations_threshold(tmp_path: Path):
    # The `_VR_RARE_PRESENCE_MIN_PR` (6) stability floor: at exactly 6 per-PR populations the floor IS
    # derivable (present=3/6 → median 177.5 < 255 → lowered), so the un-reconciled form FAILs; at 5 it
    # is NOT derivable → SKIP (the slowest check's p50 stands). Locks the threshold on both sides.
    assert _tag_for(_good() + _headline_form2_floor(present=3, npop=6, reconciled=False), _FLOOR_RECON,
                    tmp_path, findings=_floor_lowered_findings(present=3, npop=6)) == "FAIL"
    assert _tag_for(_good() + _headline_form2_floor(present=3, npop=5, reconciled=False), _FLOOR_RECON,
                    tmp_path, findings=_floor_lowered_findings(present=3, npop=5)) == "SKIP"


def test_also_noticed_count_check_fails_on_mismatch(tmp_path: Path):
    also = """
**🧹 Also noticed** - 2 off-path hygiene findings that save runner-minutes: [see below](#also-noticed).

<a id="also-noticed"></a>

## 🧹 Also noticed - residual hygiene

<details>
<summary><strong>OPT28 - Full Git History</strong> · -859 min/mo · MEDIUM · 8 across 4 wf</summary>
</details>

<details>
<summary><strong>OPT5 - pnpm Store Not Cached</strong> · -68 min/mo · MEDIUM · 1 across 1 wf</summary>
</details>
"""
    assert _tag_for(_good() + also, _ALSO, tmp_path) == "PASS"  # TOC 2 == 2 rows
    bad = (_good() + also).replace("- 2 off-path hygiene", "- 5 off-path hygiene")
    assert _tag_for(bad, _ALSO, tmp_path) == "FAIL"


def test_coverage_check_fails_without_a_data_basis(tmp_path: Path):
    bad = _good().replace("> **Where this data comes from**", "> (basis omitted)")
    bad = bad.replace("## 🗄️ Data sources", "## Footer")
    assert _tag_for(bad, _COVERAGE, tmp_path) == "FAIL"


def test_domain_leakage_check_fails_on_ci_secure_framing(tmp_path: Path):
    bad = _good() + "\nWhat an attacker can do: nothing, run zizmor to confirm.\n"
    assert _tag_for(bad, _LEAK, tmp_path) == "FAIL"
    assert _tag_for(_good(), _LEAK, tmp_path) == "PASS"


def test_rendered_pattern_check_fails_on_fabricated_pattern(tmp_path: Path):
    # A finding block for OPT99, which is NOT in the findings JSON -> fabrication FAIL.
    row = ("\n<details>\n<summary><strong>OPT99 - Invented finding</strong> · "
           "-10 min/mo · LOW · 1</summary>\n</details>\n")
    fj = {"findings": [{"pattern": "OPT5", "title": "real"}]}
    assert _tag_for(_good() + row, _PATTERNS, tmp_path, findings=fj) == "FAIL"
    fj_ok = {"findings": [{"pattern": "OPT99", "title": "now real"}]}
    assert _tag_for(_good() + row, _PATTERNS, tmp_path, findings=fj_ok) == "PASS"


def _head_scripts_tree() -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short",
         "HEAD:skills/ci-speedup/scripts"], capture_output=True, text=True).stdout.strip()


def _ancestor_sha() -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD~1"],
        capture_output=True, text=True).stdout.strip()


def _with_tree(report: str, tree: str) -> str:
    return report.replace("`)", f"`, scripts tree `{tree}`)", 1)


def test_provenance_decision_table(tmp_path: Path):
    """`check_skill_commit_provenance`, exhaustively: commit state x tree state.

    This table IS the check's contract. It exists because the check's worst bug — a
    report recording any real ancestor commit passed regardless of a stale `scripts
    tree`, letting a `main`-rendered report vouch for itself forever after `scripts/`
    changed — lived in an un-enumerated cell. Rules the table encodes:

      - A tree token, when present, GATES: it must match HEAD's `scripts/` tree.
        Ancestry never overrides it.
      - No tree token = a legacy report (pre-2026-07-08): commit ancestry decides,
        exactly as before the token existed.
      - A `-dirty` tree (rendered from uncommitted code) and a truncated token
        (< 7 chars would prefix-match anything) are never provenance.
    """
    head, anc, tree = _head_short_sha(), _ancestor_sha(), _head_scripts_tree()
    if not (head and tree):
        pytest.skip("git unavailable")
    if not anc:
        pytest.skip("checkout has no HEAD~1; the ancestor rows need one")
    unknown = "0123456"                      # a sha git has never seen (squashed away)
    table = [
        # (recorded commit, tree token or None,  expected)
        (head,    None,             "PASS"),  # legacy report at HEAD
        (anc,     None,             "PASS"),  # legacy report from an ancestor
        (unknown, None,             "FAIL"),  # legacy + unresolvable commit
        (head,    tree,             "PASS"),
        (anc,     tree,             "PASS"),
        (unknown, tree,             "PASS"),  # the feature: squash killed the commit,
                                              # the tree survives and vouches
        (head,    "deadbee1",       "FAIL"),  # stale/forged tree gates even at HEAD
        (anc,     "deadbee1",       "FAIL"),  # THE bug: ancestry must not override
        (unknown, "deadbee1",       "FAIL"),
        (unknown, f"{tree}-dirty",  "FAIL"),  # rendered from uncommitted code
        (anc,     f"{tree}-dirty",  "FAIL"),  # ...even with a good commit
        (unknown, tree[:1],         "FAIL"),  # truncated token (prefix-match trap)
    ]
    for commit, token, expected in table:
        report = _good(commit) if token is None else _with_tree(_good(commit), token)
        got = _tag_for(report, _PROVENANCE, tmp_path, skill_repo=str(_REPO_ROOT))
        assert got == expected, (
            f"provenance(commit={commit!r}, tree={token!r}): got {got}, "
            f"expected {expected}")


def test_provenance_ignores_tokens_outside_the_data_sources_footer(tmp_path: Path):
    """Token parsing is anchored to the footer row. Unanchored matching — first OR last
    — lets other report text decide provenance: the renderer emits boilerplate after the
    footer, so an appended clean token would override a `-dirty` footer and defeat the
    dirty-tree refusal."""
    tree = _head_scripts_tree()
    if not tree:
        pytest.skip("git unavailable")
    dirty_footer = _with_tree(_good("0123456"), f"{tree}-dirty")

    # A clean decoy AFTER the footer must not rescue a dirty render.
    assert _tag_for(dirty_footer + f"\n\nscripts tree `{tree}`\n", _PROVENANCE,
                    tmp_path, skill_repo=str(_REPO_ROOT)) == "FAIL"
    # A valid decoy BEFORE the footer must not rescue a fabricated one either.
    decoy = f"Run `git rev-parse HEAD:scripts` -> scripts tree `{tree}`\n\n"
    assert _tag_for(decoy + _with_tree(_good("0123456"), "deadbee1"), _PROVENANCE,
                    tmp_path, skill_repo=str(_REPO_ROOT)) == "FAIL"
    # And when a report QUOTES a complete footer row (a code block citing another
    # report), the real footer — the last one — decides. First-match would read the
    # quoted garbage and fail a legitimate report.
    quoted = ("Quoting another report:\n"
              "| ci-speedup static scan (skill commit `9999999`, scripts tree "
              "`deadbee1`) | x | y |\n\n")
    assert _tag_for(quoted + _with_tree(_good("0123456"), tree), _PROVENANCE,
                    tmp_path, skill_repo=str(_REPO_ROOT)) == "PASS"


# --- Installed-copy provenance (issue #2) ----------------------------------------
# An INSTALLED skill copy (no git repo) stamps a lockfile content-hash form,
# `installed:<hash12>` or the terminal `installed:unversioned`, rendered as a plain
# `skill build` identity string (no commit URL). A live/installed run verifies
# WITHOUT --skill-repo, where the identity string IS honest provenance. A committed
# worked example verifies WITH --skill-repo and must carry a real resolvable sha, so
# the installed form is rejected there.
def _installed_footer(form: str) -> str:
    return _GOOD_TMPL.replace(
        "ci-speedup static scan (skill commit `SKILLSHA`)",
        f"ci-speedup static scan (skill build `{form}`)")


def test_installed_form_is_accepted_without_skill_repo(tmp_path: Path):
    for form in ("installed:abc123def456", "installed:unversioned"):
        assert _tag_for(_installed_footer(form), _PROVENANCE, tmp_path) == "PASS", form


def test_installed_form_is_rejected_with_skill_repo(tmp_path: Path):
    # Committed/worked-example strictness: a source-checkout render must stamp a real
    # git sha; the installed content-hash form there is a regression.
    for form in ("installed:abc123def456", "installed:unversioned"):
        assert _tag_for(_installed_footer(form), _PROVENANCE, tmp_path,
                        skill_repo=str(_REPO_ROOT)) == "FAIL", form


def test_installed_form_rejects_a_malformed_hash(tmp_path: Path):
    # Only exactly-12-hex or the literal `unversioned` are valid; a short/non-hex
    # token is not accepted (falls through to the no-provenance failure).
    for bad in ("installed:xyz", "installed:abc123def45", "installed:ABC123DEF456"):
        assert _tag_for(_installed_footer(bad), _PROVENANCE, tmp_path) == "FAIL", bad


# --- Stream 1 ---------------------------------------------------------------------------------
# verify_report is STANDALONE BY DESIGN (no skill imports), so the values it shares with the
# engine are deliberate verbatim copies kept honest ONLY by a test (the engine's own docstrings
# say so — "kept honest by the test, exactly like gh_utils.py"). Until now no such test existed
# for the engine values verify_report duplicates. S1a adds them — one per load-bearing verbatim
# copy: the `_WALL_CLOCK_LONG_POLE_FLOOR_S` constant (from blocking_path.py), the `_wf_is_file_backed`
# predicate (from collect_runs.py), and the `_strip_scope` scope-strip (from blocking_path._clean_label,
# the comparator-identity basis inside `_cmp_name`). A drift between the engine and any of these
# copies now turns a test RED instead of silently mis-validating every report. (Mirror comments that
# are NON-load-bearing — `_fmt_clock`/`_clock` cosmetic detail strings — or INTENTIONALLY distinct —
# `_CEILING_MIN_GATING_PR` vs the engine's `_RARE_PRESENCE_MIN_PR` — are deliberately not pinned;
# behavioral re-derivations like `_matrix_base` mirror engine LOGIC via a different implementation,
# so a textual drift test doesn't apply.)
_SCRIPTS = _SKILL_DIR / "scripts"
_BLOCKING_PATH = _SCRIPTS / "blocking_path.py"
_COLLECT_RUNS = _SCRIPTS / "collect_runs.py"


def _assign_value(src: str, name: str) -> str | None:
    """The right-hand side of a top-level `NAME = <literal>` assignment (first match), trimmed of
    a trailing inline comment. None when absent."""
    m = re.search(rf"^{re.escape(name)}\s*=\s*([^\n#]+)", src, re.MULTILINE)
    return m.group(1).strip() if m else None


def test_s1_per_pole_structural_set_stays_coupled_to_the_engine():
    """`_VR_PER_POLE_STRUCTURAL` (the accounting's structural exclusion set) is a
    verbatim copy of `blocking_path._PER_POLE_STRUCTURAL_PATTERNS`. If the engine
    grows `_STRUCTURAL_PATTERNS` and the verifier's copy lags, the renderer's and
    verifier's accounting silently diverge over the new pattern — a report renders
    one story and verifies another (greptile P2 on PR-P1)."""
    bp = _load_blocking_path()
    vr = _load_verify_report()
    assert vr._VR_PER_POLE_STRUCTURAL == bp._PER_POLE_STRUCTURAL_PATTERNS, (
        f"structural exclusion sets drifted: verifier {sorted(vr._VR_PER_POLE_STRUCTURAL)} "
        f"vs engine {sorted(bp._PER_POLE_STRUCTURAL_PATTERNS)} — re-sync the "
        "verify_report copy")


def test_s3_pending_caveat_set_and_markers_stay_coupled_to_the_renderer():
    """PR-S3: `_VR_PENDING_CAVEAT_PATTERNS` is a verbatim copy of
    `blocking_path._PENDING_CAVEAT_PATTERNS`, and every verifier marker must
    appear in the renderer's `_PENDING_CAVEAT_LINES` text — a renderer rewrap
    or reword that breaks a marker would blind the check on every future
    report while old reports kept passing."""
    bp = _load_blocking_path()
    vr = _load_verify_report()
    assert vr._VR_PENDING_CAVEAT_PATTERNS == bp._PENDING_CAVEAT_PATTERNS, (
        f"Pending-caveat pattern sets drifted: verifier "
        f"{sorted(vr._VR_PENDING_CAVEAT_PATTERNS)} vs engine "
        f"{sorted(bp._PENDING_CAVEAT_PATTERNS)} — re-sync the verify_report copy")
    caveat_text = "\n".join(bp._PENDING_CAVEAT_LINES)
    for marker in vr._VR_PENDING_CAVEAT_MARKERS:
        assert marker in caveat_text, (
            f"verifier marker {marker!r} not found in the renderer's caveat "
            "text — the check would fail every fresh report")


def test_s3_skip_family_prompt_carries_pending_caveat(tmp_path: Path):
    """PR-S3 red/green: a rendered skip-family prompt carries the §8.1 Pending
    caveat (green), and a report whose family prompt lacks it fails (red)."""
    vr = _load_verify_report()
    bp = _load_blocking_path()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [{
        "id": "f-skip",
        "pattern": "OPT32",
        "severity": "MEDIUM",
        "title": "Missing `paths`/`paths-ignore` on Expensive Workflows",
        "workflow_file": ".github/workflows/ci.yml",
        "line": 3,
        "affected_jobs": [],
        "evidence": "expensive workflow runs on every PR including docs-only changes",
        "runner_min_saving": 50.0,
        "wall_clock_p50_s": 0.0,
        "realization": "none",
        "sizing_basis": "modeled",
        "fix_recipe_anchor": "opt32--missing-pathspaths-ignore-on-expensive-workflows",
    }]
    report = bp.render(doc)
    assert "required-status 'Pending' landmine" in report
    chk = vr.check_skip_family_prompts_carry_pending_caveat(report)
    assert chk.ok and not chk.skipped, chk

    # Red: strip the caveat from the family prompt — the check must fail.
    stripped = "\n".join(
        line for line in report.splitlines()
        if not any(marker.split("\n")[0] in line
                   for marker in vr._VR_PENDING_CAVEAT_MARKERS))
    chk_red = vr.check_skip_family_prompts_carry_pending_caveat(stripped)
    assert not chk_red.ok
    assert "OPT32" in chk_red.detail

    # Vacuous: a report with no family prompt skips.
    chk_skip = vr.check_skip_family_prompts_carry_pending_caveat("# empty\n")
    assert chk_skip.ok and chk_skip.skipped


def test_z_fmt_tier2_saved_min_twins_agree_and_keep_subminute_precision():
    """PR-Z: (a) S1a-style source pin — the renderer/verifier _fmt_tier2_saved_min
    twins must produce IDENTICAL nonzero output over a value grid (zero/None
    sentinels intentionally differ); (b) sub-minute savings keep one decimal —
    a real 0.2 min/mo row must never render '0 min/mo' (display precision,
    not an admission floor)."""
    bp = _load_blocking_path()
    vr = _load_verify_report()
    for v in (0.1, 0.2, 0.49, 0.94, 0.95, 1.0, 2.4, 53.6, 442.5, 1234.0):
        assert bp._fmt_tier2_saved_min(v) == vr._fmt_tier2_saved_min(v), v
    assert bp._fmt_tier2_saved_min(0.2) == "0.2 min/mo"
    assert bp._fmt_tier2_saved_min(0.95) == "1 min/mo"
    assert bp._fmt_tier2_saved_min(53.6) == "54 min/mo"
    # Zero/None sentinels: renderer display-dash vs verifier skip-sentinel.
    assert bp._fmt_tier2_saved_min(0) == "—" and vr._fmt_tier2_saved_min(0) == "-"


def _real_opt46_note() -> str:
    """OPT46's measured-evidence note as the PRODUCTION detector actually emits it
    (not a hand-written stand-in) — so the assertions below are a guard on shipped
    code rather than on the fixture the test authored."""
    name = "ci_speedup_collect_runs_self"
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "scripts" / "collect_runs.py")
    cr = importlib.util.module_from_spec(spec)
    sys.modules[name] = cr
    spec.loader.exec_module(cr)
    runs, jobs_per_run = [], []
    for i in range(5):  # 5 runs that all race → 4 superseded
        runs.append({"head_branch": "feature/x", "head_sha": f"s{i}", "event": "push",
                     "id": f"r{i}", "status": "completed", "conclusion": "success",
                     "run_started_at": f"2026-06-01T00:{i:02d}:00Z",
                     "created_at": f"2026-06-01T00:{i:02d}:00Z",
                     "updated_at": f"2026-06-01T00:{i + 30:02d}:00Z"})
        jobs_per_run.append([{"name": "build", "status": "completed",
                              "conclusion": "success",
                              "started_at": f"2026-06-01T00:{i:02d}:00Z",
                              "completed_at": f"2026-06-01T00:{i + 10:02d}:00Z"}])
    out = cr._detect_opt46_superseded_runs(
        ".github/workflows/ci.yml", runs, jobs_per_run,
        {"on": {"push": {}, "pull_request": {}}, "name": "CI"}, 42, 0)
    assert out, "OPT46 detector produced no finding — fixture drifted from production"
    return str(out[0]["measured_evidence"]["note"])


def test_z_tier2_prompt_embeds_detector_guardrail(tmp_path: Path):
    """PR-Z (§6 card-shape contract: 'certificate + guardrail embedded'): a
    Tier-2 finding whose measured-evidence note carries a GUARDRAIL sentence
    ships it verbatim inside its agent prompt.

    The note is taken from the REAL OPT46 detector, so the second half of this
    test is a live guard on production text: if `collect_runs` ever went back to
    telling the agent to ADD a bare `cancel-in-progress: true` (that sentence sits
    ABOVE the catalog link in the prompt and outranks it), this fails."""
    bp = _load_blocking_path()
    doc = _tier2_doc_for_verify()
    note = _real_opt46_note()
    doc["findings"][0]["measured_evidence"]["note"] = note
    report = bp.render(doc)
    prompt = report.split("🤖 Prompt for your coding agent", 1)[1]
    # (a) the detector's GUARDRAIL tail ships verbatim into the prompt — modulo the
    # renderer's ASCII-folding of the em dash / en dash on its way into the block.
    guardrail = bp._strip_emdashes(note[note.index("GUARDRAIL"):])
    assert guardrail and guardrail in prompt
    # (b) …and what ships must never prescribe the bare form. Every mention of it
    # in the shipped text must be a negation ("never a bare ...").
    for token in ("cancel-in-progress: true", "cancel-in-progress:true"):
        idx = 0
        while (idx := guardrail.find(token, idx)) >= 0:
            assert "bare" in guardrail[max(0, idx - 20):idx], (
                f"the OPT46 agent prompt prescribes `{token}`: ...{guardrail[:idx]}")
            idx += len(token)
    assert "adding cancel-in-progress" not in prompt


def test_s1a_wall_clock_floor_constant_stays_coupled_to_the_engine():
    # `_WALL_CLOCK_LONG_POLE_FLOOR_S` is duplicated from blocking_path.py into verify_report.py with
    # only a "keep in sync with the engine" comment. If the engine retunes the floor and the
    # verifier's copy lags, the #5 double-frame invariant (which gates a "credited wall-clock lever"
    # at exactly this value) silently disagrees with the renderer. Pin the two equal.
    engine = _assign_value(_BLOCKING_PATH.read_text(encoding="utf-8"), "_WALL_CLOCK_LONG_POLE_FLOOR_S")
    verifier = _assign_value(_VERIFY.read_text(encoding="utf-8"), "_WALL_CLOCK_LONG_POLE_FLOOR_S")
    assert engine is not None, "engine no longer defines _WALL_CLOCK_LONG_POLE_FLOOR_S — re-point this test"
    assert verifier is not None, "verify_report lost its _WALL_CLOCK_LONG_POLE_FLOOR_S copy"
    assert float(engine) == float(verifier), (
        f"_WALL_CLOCK_LONG_POLE_FLOOR_S drifted: engine {engine!r} vs verifier {verifier!r} — "
        "re-sync the verify_report copy with blocking_path.py")


def _def_return_lines(src: str, header_pattern: str) -> list[str] | None:
    """Every `return …` STATEMENT (whitespace-normalized, in body order) inside the function whose
    `def` header matches `header_pattern` — which MUST carry a `(?P<indent>…)` group for the def's
    own indentation. Captures the WHOLE indented body (every return, not just the first — so a guard
    still bites if a one-line body grows an early-return guard clause) AND re-joins a MULTI-LINE
    bracket-continued return into one statement, so a one-side-only auto-format line-wrap can't
    collapse two different predicates to an identical `return (` and silently blind the drift guard.
    None when the def is absent; [] when it has no captured return. (A docstring line beginning with
    `return ` is a contrived residual; both files would have to carry the identical fake to slip past
    a compare. Bracket depth ignores brackets inside SIMPLE string literals — so `endswith(")")`
    re-joins correctly — but it is a heuristic, not a tokenizer: an escaped or triple-quoted bracket
    could in principle truncate a join. The three pinned one-liners contain no such literal, and the
    join is a strict improvement over comparing only a multi-line return's first physical line.)"""
    m = re.search(header_pattern, src, re.MULTILINE)
    if not m:
        return None
    body_indent = len(m.group("indent"))
    body: list[str] = []
    for line in src[m.end():].splitlines():
        if not line.strip():
            continue  # blank line — not the end of the body
        if len(line) - len(line.lstrip()) <= body_indent:
            break     # dedented to/below the def → end of the function body
        body.append(line.strip())
    # Depth-count brackets, but strip simple "…"/'…' string literals first so a bracket INSIDE a
    # string (e.g. `endswith(")")`) doesn't mis-count and truncate a multi-line join. Not a full
    # tokenizer (escapes / triple-quotes); a residual miscount mis-joins, which the compare surfaces
    # as a difference — see the docstring caveat. (Stripping is only for counting; the stored/compared
    # `stmt` keeps the raw text.)
    _delta = lambda s: (lambda t: sum(t.count(c) for c in "([{") - sum(t.count(c) for c in ")]}"))(
        re.sub(r"\"[^\"]*\"|'[^']*'", "", s))
    returns: list[str] = []
    i = 0
    while i < len(body):
        if body[i].startswith("return ") or body[i] == "return":
            stmt, depth = body[i], _delta(body[i])
            while depth > 0 and i + 1 < len(body):   # gather bracket-continued lines
                i += 1
                stmt += " " + body[i]
                depth += _delta(body[i])
            returns.append(re.sub(r"\s+", " ", stmt))
        i += 1
    return returns


def test_s1a_wf_is_file_backed_predicate_stays_coupled_to_the_engine():
    # `_wf_is_file_backed` is a deliberate verbatim copy: collect_runs.py DROPS a name-inferred
    # file-backed structural lever; verify_report.py FLAGS one. The producer and the checker must
    # agree on file-backedness textually, or a freshly-generated report could fail its own
    # invariant (or let a fabrication through). The engine docstring promises this is "kept honest
    # by the test" — this IS that test. Compare the FULL return-statement list, so an early-return
    # guard added to one side but not the other is caught too.
    engine = _def_return_lines(_COLLECT_RUNS.read_text(encoding="utf-8"),
                               r"^(?P<indent>[ \t]*)def _wf_is_file_backed\(wf\)\s*->\s*bool:[ \t]*\n")
    verifier = _def_return_lines(_VERIFY.read_text(encoding="utf-8"),
                                 r"^(?P<indent>[ \t]*)def _wf_is_file_backed\(wf\)\s*->\s*bool:[ \t]*\n")
    assert engine, "collect_runs no longer defines _wf_is_file_backed(wf) -> bool with a return — re-point this test"
    assert verifier, "verify_report lost its _wf_is_file_backed copy (or its return)"
    assert engine == verifier, (
        f"_wf_is_file_backed drifted between the engine and the verifier:\n"
        f"  collect_runs: {engine!r}\n  verify_report: {verifier!r}\n"
        "re-sync the verify_report copy (it must mirror the engine's file-backed drop exactly)")


def test_s1a_strip_scope_stays_coupled_to_the_engine():
    # `_strip_scope` (verify_report) is a verbatim copy of `blocking_path._clean_label` — the
    # `@scope/` strip that is the comparator-identity basis inside `_cmp_name` (every on-path /
    # dropped-check / floor gate compares names through it). If the engine ever retunes its scope
    # syntax and this copy lags, those comparators silently false-positive/negative — the SAME risk
    # class as the other two S1a copies. Pin the return bodies equal. (The verifier's docstring
    # already states it "mirrors `blocking_path._clean_label`".)
    engine = _def_return_lines(_BLOCKING_PATH.read_text(encoding="utf-8"),
                               r"^(?P<indent>[ \t]*)def _clean_label\(s: str\)\s*->\s*str:[ \t]*\n")
    verifier = _def_return_lines(_VERIFY.read_text(encoding="utf-8"),
                                 r"^(?P<indent>[ \t]*)def _strip_scope\(s: str\)\s*->\s*str:[ \t]*\n")
    assert engine, "blocking_path no longer defines _clean_label(s: str) -> str with a return — re-point this test"
    assert verifier, "verify_report lost its _strip_scope copy (or its return)"
    assert engine == verifier, (
        f"the scope-strip drifted between the engine and the verifier:\n"
        f"  blocking_path._clean_label: {engine!r}\n  verify_report._strip_scope: {verifier!r}\n"
        "re-sync _strip_scope with blocking_path._clean_label (the `_cmp_name` identity basis)")


def test_s1a_fence_safe_stays_coupled_to_the_engine():
    # `_strip_scope`'s return line (pinned above) DELEGATES to `_fence_safe`, whose body — plus
    # `_defuse_backtick_runs` and the two module-level regexes — is duplicated FREE-HAND into
    # verify_report.py (the engine copy carries a docstring, the verifier twin doesn't, so a raw
    # source compare can't pin them). If the two `_fence_safe`s ever diverge, a hostile name
    # normalizes DIFFERENTLY on the render side vs the comparator side and the on-path / dropped-
    # check / floor comparators false-positive/negative — the exact bug class this fix closes.
    # The return-line pin above does NOT see past the `_fence_safe(...)` call, so pin the transform
    # itself, two ways that between them bite on ANY drift: (1) the compiled regex PATTERNS must be
    # byte-equal (catches a change to the control-char set or the >=3-backtick run threshold on
    # inputs no battery lists), and (2) an adversarial-input BATTERY must produce byte-identical
    # output from both sides (catches a change to the order of operations or the newline-collapse —
    # e.g. dropping the `[\r\n]+`->space step on one side only).
    bp = _load_blocking_path()
    vr = _load_verify_report()
    assert bp._FENCE_RUN_RE.pattern == vr._FENCE_RUN_RE.pattern, \
        "the >=3-backtick run regex drifted between blocking_path and verify_report"
    assert bp._FENCE_CTRL_RE.pattern == vr._FENCE_CTRL_RE.pattern, \
        "the control-char regex drifted between blocking_path and verify_report"
    battery = [
        "", "build", "Integration Test (3.13)", "npm run build",
        "```", "``", "`", "a```b", "``````", "a`b`c", "```\n```", "``\n`", "`\n``",
        "line1\nline2", "line1\r\nline2", "a\n\n\nb", "tab\tindent", "trailing ",
        "\x00nul", "\x07bel", "\x1besc", "\x7fdel", "\x85nel", "\x9fc1", "\x0bvt", "\x0cff",
        "mix\x00`` `\n```end", "~~~ tilde ~~~", "smart‘quoteˋ", "@scope/name",
    ]
    for s in battery:
        assert bp._defuse_backtick_runs(s) == vr._defuse_backtick_runs(s), \
            f"_defuse_backtick_runs diverged on {s!r}"
        assert bp._fence_safe(s) == vr._fence_safe(s), \
            f"_fence_safe diverged between the engine and the verifier on {s!r} — re-sync the " \
            "verify_report twin (render side and comparator side must normalize identically)"


def test_s1b_check_requires_a_nonempty_detail():
    # Lesson B5/L8: "a SKIP that reads clean is a false negative." The UNIVERSAL guarantee is
    # structural — `Check.__post_init__` rejects a blank detail at EVERY construction site, so a
    # future check that forgets its coverage reason fails the moment any test reaches that branch
    # (no fixture matrix to keep complete). Pin that contract directly.
    vr = _load_verify_report()
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError):
            vr.Check("some check", True, blank)
    with pytest.raises(ValueError):
        vr.Check("some check", True)          # default detail="" is also blank
    ok = vr.Check("some check", True, "all good")   # a real detail is accepted
    assert ok.detail == "all good"


def test_s1b_every_check_surfaces_a_coverage_detail(tmp_path: Path):
    # Complements the structural `Check.__post_init__` guarantee (pinned by the test above) by
    # EXERCISING the real checks end-to-end: it runs `run_checks` across a scenario matrix chosen to
    # drive the checks through their PASS, SKIP, "findings unreadable", and FAIL branches, asserting
    # run_checks completes (no check raises) and every returned Check carries a non-empty detail.
    # The structural invariant is what makes the guarantee universal — this test does NOT claim to
    # reach every branch (it can't, without an ever-growing fixture matrix; ~half the data-driven
    # FAIL and --skill-repo branches aren't hit here). Its specific job is to keep the "findings
    # unreadable" path covered (the false-negative an earlier sampling version left unguarded) and to
    # catch a check that raises mid-run.
    vr = _load_verify_report()
    good = _good()
    scenarios = [
        # (label, report, findings: None | dict | raw-json-str)
        # 1) no findings → findings-dependent checks take their SKIP-with-detail branch.
        ("no-findings", good, None),
        # 2) valid findings → data-driven checks take PASS-with-detail branches.
        ("valid-findings", good + _ceil_headline("build", "3m 00s"), _ceiling_findings(n_gating=6)),
        # 3) MALFORMED findings file (exists but unparseable) → every findings-reading check takes
        #    its "findings unreadable" / "could not read findings JSON" branch — the exact
        #    false-negative the adversarial panel proved the old sampling guard left unguarded.
        ("malformed-findings", good, "{ not valid json"),
        # 4) broken report → FAIL-with-detail branches (RCA prescribes a fix).
        ("fail-report", good + "\n**Fix:** apply this diff.\n", None),
    ]
    saw_unreadable = False
    for label, report, findings in scenarios:
        rp = tmp_path / f"report-2026-05-29-{label}.md"
        rp.write_text(report, encoding="utf-8")
        fp = None
        if findings is not None:
            fp = tmp_path / f"findings-{label}.json"
            fp.write_text(findings if isinstance(findings, str) else json.dumps(findings),
                          encoding="utf-8")
        checks = vr.run_checks(report, rp, fp, None)
        assert checks, f"[{label}] run_checks returned no checks"
        for c in checks:
            assert (c.detail or "").strip(), (
                f"[{label}] check {c.name!r} (ok={c.ok}, skipped={c.skipped}) has a BLANK detail — "
                "a silent coverage gap (every check must say why it passed / skipped / failed)")
        if label == "malformed-findings":
            # Prove the unreadable branch was actually exercised, so the blank-detail assertion above
            # genuinely covers it (rather than every check SKIPping on an earlier guard).
            saw_unreadable = any(
                "unread" in (c.detail or "").lower() or "could not read" in (c.detail or "").lower()
                for c in checks)
    assert saw_unreadable, ("the malformed-findings scenario drove no check into its 'findings "
                            "unreadable' branch — the fixture no longer exercises that path")


def _load_verify_report():
    # Load THIS skill's verify_report by path under a unique name (ci-secure ships one too, so a
    # plain `import verify_report` can bind the wrong module on the shared pytest pythonpath). Used
    # only to unit-test the module-internal strip helpers directly.
    path = _VERIFY
    name = "ci_speedup_verify_report_self"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_blocking_path():
    # Load the engine renderer by path under a unique name, so a coupling test can compare the
    # verifier's standalone `_same_matrix` copy against the engine's authoritative one directly.
    name = "ci_speedup_blocking_path_self"
    spec = importlib.util.spec_from_file_location(name, _BLOCKING_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_collect_runs():
    # Load THIS skill's collect_runs by path under a unique name — same rationale as
    # `_load_verify_report`: avoid binding a sibling skill's module on the shared
    # pytest pythonpath. Used to drive the real cost-spine source producer.
    name = "ci_speedup_collect_runs_spine_self"
    spec = importlib.util.spec_from_file_location(name, _COLLECT_RUNS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _spine_job_named(name: str) -> dict:
    return {
        "name": name,
        "started_at": "2026-06-01T00:00:00Z",
        "completed_at": "2026-06-01T00:01:15Z",
        "conclusion": "success",
        "labels": ["ubuntu-latest"],
        "_run_created_at": "2026-06-01T00:00:00Z",
    }


def test_runner_minute_spine_job_name_with_newline_round_trips(tmp_path: Path):
    """Regression (CLASS; existing invariant `runner-minute cost spine source block
    is re-derivable` / `check_runner_minute_spine_contract`): a job whose name
    carries embedded whitespace — a `name: |` block-scalar, seen as
    `Cypress E2E -\\nDocumentation` on vuestorefront/storefront-ui — must be STAMPED
    in whitespace-normalized form by the source producer, so the identity the
    renderer emits into the markdown cell (which flattens CR/LF to a single space
    and cannot carry a raw newline) round-trips back to the source row.

    Before the fix the producer stamped the raw multi-line name verbatim, so the
    rendered cell (`Cypress E2E - Documentation`) no longer equalled the stored
    JSON (`Cypress E2E -\\nDocumentation`) and the contract check reported
    `rendered row 1: no matching source row`. The fix lives in the SOURCE producer
    (`collect_runs._spine_identity_text`), never the renderer — a cell physically
    cannot hold the newline, so the JSON must already be flat."""
    cr = _load_collect_runs()
    bp = _load_blocking_path()
    vr = _load_verify_report()
    wf = ".github/workflows/e2e-docs.yml"
    raw_name = "Cypress E2E -\nDocumentation"
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": [[_spine_job_named(raw_name)]]}}, {}, {wf: 100},
        "private", workflows_in_play={wf})
    assert spine is not None and spine["render_ready"] is True
    # The stamped identity must be the flattened form the renderer emits, so the
    # source row is re-derivable from the rendered cell (no raw CR/LF survives).
    stamped = spine["rows"][0]["job_name"]
    assert stamped == "Cypress E2E - Documentation", repr(stamped)
    assert "\n" not in stamped and "\r" not in stamped
    doc = {
        "repo": "vuestorefront/storefront-ui",
        "repo_visibility": "private",
        "per_workflow_monthly_volume": {wf: 100},
        "data_sources": {"cost_spine_job_fetch_failures": 0},
        "runner_minute_spine": spine,
        "findings": [],
    }
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    chk = vr.check_runner_minute_spine_contract(report, findings_path)
    assert chk.ok, chk.detail


def _spine_job_for_seconds(name: str, seconds: int) -> dict:
    """A cost-spine job that ran for exactly `seconds` (0 => zero-span, which
    bills 0 billable minutes per collect_runs._billable_equiv_min)."""
    start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "name": name,
        "started_at": start.strftime(fmt),
        "completed_at": end.strftime(fmt),
        "conclusion": "success",
        "labels": ["ubuntu-latest"],
        "_run_created_at": start.strftime(fmt),
    }


def test_runner_minute_spine_mixed_zero_span_bucket_re_derives_billing_floor(tmp_path: Path):
    """Regression (CLASS; existing invariant `runner-minute cost spine source block
    is re-derivable` / `check_runner_minute_spine_contract`): a job bucket that
    mixes one short real run with zero-span occurrences (started_at ==
    completed_at, which GitHub bills 0 minutes) has a POSITIVE mean compute-second
    but a sub-1.0 MEAN billable-minute. Seen on electron/electron
    (`row 233: positive-duration job has <1 billable minute`).

    The old check asserted `mean_s > 0 -> mean_billed >= 1.0`, which is UNSOUND at
    the aggregate level: it cannot tell a legitimate mixed bucket from a broken
    engine that forgot to round each job up to a whole minute. The source producer
    now stamps `sampled_positive_duration_occurrence_count`, so the per-occurrence
    1-minute floor (`round(mean_billed * occurrences) >= positive_occurrences`) is
    re-derivable from the row alone — the legit mixed bucket passes, an un-rounded
    engine still fails."""
    cr = _load_collect_runs()
    bp = _load_blocking_path()
    vr = _load_verify_report()
    wf = ".github/workflows/ci.yml"
    # One 30s run (billed 1 min) + two zero-span runs (billed 0): mean_s = 10s > 0,
    # mean_billed = 1/3 min < 1.0 — the exact shape the old check false-failed on.
    runs = [
        [_spine_job_for_seconds("build", 30)],
        [_spine_job_for_seconds("build", 0)],
        [_spine_job_for_seconds("build", 0)],
    ]
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 100},
        "private", workflows_in_play={wf})
    assert spine is not None and spine["render_ready"] is True
    row = spine["rows"][0]
    # The producer emits the ground truth the floor is re-derived from.
    assert row["sampled_positive_duration_occurrence_count"] == 1
    assert row["sampled_job_occurrence_count"] == 3
    assert row["mean_sampled_compute_seconds"] > 0
    assert row["mean_sampled_billable_equiv_minutes"] < 1.0
    doc = {
        "repo": "electron/electron",
        "repo_visibility": "private",
        "per_workflow_monthly_volume": {wf: 100},
        "data_sources": {"cost_spine_job_fetch_failures": 0},
        "runner_minute_spine": spine,
        "findings": [],
    }
    report = "# report\n\n" + "\n".join(bp._runner_minute_spine_block(doc)) + "\n"
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    chk = vr.check_runner_minute_spine_contract(report, findings_path)
    assert chk.ok, chk.detail

    # The tightened floor still catches a genuinely broken (un-rounded) engine:
    # each 30s job billed 0.5 min instead of the whole-minute round-up.
    broken = json.loads(findings_path.read_text(encoding="utf-8"))
    brow = broken["runner_minute_spine"]["rows"][0]
    brow["sampled_positive_duration_occurrence_count"] = 3
    brow["sampled_job_occurrence_count"] = 3
    brow["mean_sampled_billable_equiv_minutes"] = 0.5  # < positive-occurrence floor
    findings_path.write_text(json.dumps(broken), encoding="utf-8")
    broken_chk = vr.check_runner_minute_spine_contract(report, findings_path)
    assert not broken_chk.ok
    assert "per-occurrence" in broken_chk.detail and "1-minute floor" in broken_chk.detail


def test_s1c_strip_render_artifacts_and_line_suffix_helpers():
    # S1c: the generic-decoration strip and the `:line` strip are now single named helpers (no more
    # per-invariant re-implementation — the inconsistency that bit #5/#7). Pin their behavior so a
    # future edit can't quietly change what a fact-comparison normalizes away.
    vr = _load_verify_report()
    # backticks + asterisks + surrounding whitespace are removed.
    assert vr._strip_render_artifacts("  `build` ") == "build"
    assert vr._strip_render_artifacts("**Run tests**") == "Run tests"
    # a stray typographic dash is normalized to an ASCII hyphen (every glyph the dash check guards).
    for dash in vr._TYPOGRAPHIC_DASHES:
        assert vr._strip_render_artifacts(f"a{dash}b") == "a-b", dash
    # idempotent on already-clean input.
    assert vr._strip_render_artifacts("plain text") == "plain text"
    # _norm_check is the decoration strip THEN case-fold — behavior-identical to the old inline form.
    assert vr._norm_check(" `Build (3.12)` ") == "build (3.12)"
    # _strip_line_suffix drops a rendered `:line` suffix and nothing else.
    assert vr._strip_line_suffix("ci.yml:149") == "ci.yml"
    assert vr._strip_line_suffix("ci.yml") == "ci.yml"          # no suffix → unchanged
    assert vr._strip_line_suffix("a/b/ci.yaml:7") == "a/b/ci.yaml"
    # a colon that is NOT a trailing :line (e.g. a Windows-ish stray) only strips the trailing digits.
    assert vr._strip_line_suffix("ci.yml:149:2") == "ci.yml:149"


def test_wrong_type_findings_container_does_not_crash(tmp_path: Path):
    # verify_report is a bug-catcher fed possibly-malformed skill output. Its per-entry parsing was
    # hardened, but the top-level CONTAINERS (the JSON object, `pr_critical_path`, `data_bundle`, and
    # the array fields) were guarded only against falsy (`or {}`/`or []`), not wrong-TYPE. A wrong-type
    # container (findings as an object, pr_critical_path as a list, …) used to AttributeError → a
    # traceback instead of the gate's contracted PASS/FAIL lines + exit 0/1. The `_as_dict`/`_as_list`
    # coercions make every such container degrade to "no findings": each malformed container must
    # produce the SAME check output as an empty `{}` findings, with no traceback and exit 0/1.
    rp = tmp_path / "report-2026-05-29.md"
    rp.write_text(_good(), encoding="utf-8")

    def _run(findings_obj) -> subprocess.CompletedProcess:
        fp = tmp_path / "f.json"
        fp.write_text(json.dumps(findings_obj), encoding="utf-8")
        return subprocess.run([sys.executable, str(_VERIFY), "--report", str(rp), "--findings", str(fp)],
                              capture_output=True, text=True)

    baseline = _run({})  # valid but empty → every data-driven check SKIPs
    assert "Traceback" not in baseline.stderr and baseline.returncode in (0, 1)
    base_lines = [ln for ln in baseline.stdout.splitlines() if ln[:4] in ("PASS", "FAIL", "SKIP")]
    assert base_lines, "expected check lines from the baseline run"

    malformed = [
        {"findings": {"a": "b"}},                              # findings is an OBJECT, not a list
        {"findings": "not a list"},
        {"pr_critical_path": [1, 2, 3]},                       # pr_critical_path is a LIST, not an object
        {"pr_critical_path": {"poles": {"x": 1}}},             # poles an object
        {"pr_critical_path": {"poles": ["str", 1]}},           # pole ENTRIES non-dict
        {"pr_critical_path": {"checks": "nope"}},              # checks a string
        {"pr_critical_path": {"checks": ["str", 2]}},          # check ENTRIES non-dict
        {"pr_critical_path": {"populations": {"k": "v"}}},     # populations an object
        {"pr_critical_path": {"dropped_non_required_checks": {"x": 1}}},
        {"pr_critical_path": {"critical_path_check": 5}},
        {"data_bundle": [1, 2]},                               # data_bundle a list
        {"data_bundle": {"logs": "x"}},                        # logs a string
        [1, 2, 3],                                             # top-level NOT an object
        "just a string",
        12345,
    ]
    for m in malformed:
        r = _run(m)
        assert "Traceback" not in r.stderr, f"wrong-type container crashed the gate: {m!r}\n{r.stderr}"
        assert r.returncode in (0, 1), f"unexpected exit for {m!r}: rc={r.returncode}"
        lines = [ln for ln in r.stdout.splitlines() if ln[:4] in ("PASS", "FAIL", "SKIP")]
        assert lines == base_lines, (
            f"a wrong-type findings container must degrade to empty-findings behavior, "
            f"but {m!r} changed the check output:\n{lines}\nvs baseline\n{base_lines}")

    # Wrong-type SCALAR fields INSIDE a structurally-valid finding dict — one level deeper than the
    # containers above. These are valid-enough findings that legitimately change which checks fire, so
    # they can't be compared to the empty-findings baseline; we assert only the crash contract (no
    # traceback, exit 0/1). `measured_signal` non-string used to crash `.strip()`; an unhashable
    # `pattern` (list/dict) used to crash the `in <frozenset>` membership test.
    no_crash_only = [
        {"findings": [{"pattern_class": "data-driven", "measured_signal": 123}]},
        {"findings": [{"pattern_class": "data-driven", "measured_signal": [1, 2]}]},
        {"findings": [{"pattern_class": "data-driven", "measured_signal": {"a": 1}}]},
        {"findings": [{"pattern": [1, 2, 3]}]},                 # unhashable pattern
        {"findings": [{"pattern": {"a": 1}}]},
        {"findings": [{"pattern": 70}]},                        # non-str, hashable
        # A non-dict `checks` ENTRY only reaches the `for c in checks` loops when populations ALSO
        # co-occur (those loops sit AFTER a populations gate) — the realistic shape the container-only
        # `{"checks": [...]}` case can't reach. Bites the gate-frequency / spine-drop / ceiling guards.
        {"pr_critical_path": {"populations": [[1.0, [["x", 5.0]]]], "checks": ["str", 2]}},
        # A non-dict `data_bundle.logs` ENTRY (a non-empty logs LIST, not a string) reaches the
        # drill-ownership check's `e.get` set-comprehension; bites its entry guard.
        {"data_bundle": {"logs": ["str", 1]}},
    ]
    for m in no_crash_only:
        r = _run(m)
        assert "Traceback" not in r.stderr, f"wrong-type scalar field crashed the gate: {m!r}\n{r.stderr}"
        assert r.returncode in (0, 1), f"unexpected exit for {m!r}: rc={r.returncode}"

    # `affected_jobs` as a truthy non-iterable scalar crashes the `for j in jobs` iteration in
    # check_pole_not_reframed_as_hygiene — but that branch is reached ONLY when the report has a drilled
    # pole AND an "Also noticed" appendix naming the finding's OPT pattern, which `_good()` lacks. Use a
    # richer report so the guard is actually exercised end-to-end.
    rich = _good() + _also_noticed_section("ci.yml", "build", "OPT99 - Some Finding")
    rp2 = tmp_path / "report-2026-05-29-rich.md"
    rp2.write_text(rich, encoding="utf-8")
    fp2 = tmp_path / "f-rich.json"
    fp2.write_text(json.dumps({"findings": [{"pattern": "OPT99", "affected_jobs": 5,
                                             "workflow_file": ".github/workflows/ci.yml"}]}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(_VERIFY), "--report", str(rp2), "--findings", str(fp2)],
                       capture_output=True, text=True)
    assert "Traceback" not in r.stderr, f"a non-iterable affected_jobs scalar crashed the gate:\n{r.stderr}"
    assert r.returncode in (0, 1)


# --- The cache-hit-rate class (check_cache_*_distribution) --------------------------
# The expo/expo false positive: a `turbo-partial-cache` pole framed "cache-key churn -
# BIGGEST LEVER" off ONE slow-mode drilled run, while the job's cache HIT ~91% of the
# time across runs. These discriminators prove the two new checks catch that class:
# a churn/lever framing over a mostly-warm measured distribution FAILs, and the reframed
# (caveated, marker-bearing) report PASSes — the class-fix red→green.
_CACHE_CLAIM = "carries a cross-run cache distribution"
_CACHE_FRAMING = "framing matches its measured hit-rate"

# The original (pre-fix) churn framing the renderer emitted, and its reframed form.
_CHURN_NOTE = "72/128 cached - 44% rebuilt despite caching ON (cache-key churn?) - BIGGEST LEVER"
_WARM_NOTE = ("72/128 cached - 44% rebuilt despite caching ON - but the cache mostly HITS "
              "across sampled PRs (median miss 9%)")
_COLD_NOTE = "0/110 cached · Remote caching DISABLED - BIGGEST LEVER"
_CACHE_MARKER = "<!-- ci-speedup:cache-context -->"


def _cache_pole_report(note_line: str, *, with_marker: bool = False,
                       wf: str = "sdk.yml", check: str = "check-packages") -> str:
    """A one-pole blocking-path report whose pole body carries `note_line` (a cache
    root-cause claim), optionally followed by the cache-context machine marker."""
    marker = f"\n{_CACHE_MARKER}\n" if with_marker else ""
    return (
        "# demo - why is the merge slow?\n\n"
        "> **Bottom line.** A typical PR waits **4m 52s** for all checks to finish.\n\n"
        f'<a id="pole-1"></a>\n\n'
        f"## Long pole 1: `{wf}` ▸ {check} - 4m 52s\n\n"
        "```text\nWhere the job's time goes.\n```\n\n"
        f"{note_line}\n{marker}\n"
        "#### \U0001f916 Prompt for your coding agent\n\n```text\nhand-off\n```\n\n"
        "## \U0001f5c4️ Data sources\n\n| Source | Coverage | Used for |\n"
        "| --- | --- | --- |\n"
        "| ci-speedup static scan (skill commit `0000000`) | all workflows | scan |\n"
    )


def _cache_val(miss: float, fork: bool = False) -> dict:
    return {"value": miss, "fork": fork, "duration_s": 100.0,
            "cache_state": {"miss_pct": miss, "cold": miss >= 99.5, "remote_off": False}}


def _cache_dist(verdict: str, values: list[dict], tail_prev: float,
                fix_key: str = "turbo-partial-cache") -> dict:
    up = [v["value"] for v in values if not v.get("fork")]
    return {
        "fix_key": fix_key, "metric": "miss_pct", "floor_pct": 40.0, "tail_min_frac": 0.25,
        "pr": {"values": values, "n": len(values),
               "fork_n": sum(1 for v in values if v.get("fork")),
               "upstream_median": (sorted(up)[len(up) // 2] if up else None),
               "upstream_range": ([min(up), max(up)] if up else None)},
        "tail": {"threshold_dur_s": None, "prevalence_max": tail_prev, "qual_n": 10},
        "push": {"values": [], "n": 0, "median": 3.0}, "push_reason": None,
        "verdict": verdict, "push_logs_fetched": 2,
    }


def _cache_findings(cache_dist: dict | None, *, probe: bool = True,
                    wf: str = ".github/workflows/sdk.yml", check: str = "check-packages") -> dict:
    ds: dict = {"tiers_run": ["gh-timing", "job-logs"]}
    if probe:
        ds["cache_dist_probe"] = {"cache_poles": 1, "push_logs_fetched": 2, "pr_logs_reused": 6}
    pole: dict = {"check": check, "workflow_file": wf, "p50_s": 292.0}
    if cache_dist is not None:
        pole["cache_dist"] = cache_dist
    return {"data_sources": ds, "pr_critical_path": {"poles": [pole]}}


# A mostly-warm distribution: 6 upstream runs cluster near 9% miss, one 44% blip; the
# drilled run was the slow-mode 44% (deliberately). Re-derives to `mostly-warm`.
_WARM_VALS = [_cache_val(9), _cache_val(12), _cache_val(7), _cache_val(44),
              _cache_val(3), _cache_val(2)]


def test_vr_cache_claim_missing_distribution_fails(tmp_path: Path):
    # New-schema findings (cache_dist_probe present) + a rendered churn claim, but the pole
    # carries NO cache_dist → the claim rests on the single drilled run, which is the bug.
    rep = _cache_pole_report(_CHURN_NOTE)
    f = _cache_findings(None)  # pole present, cache_dist absent
    assert _tag_for(rep, _CACHE_CLAIM, tmp_path, findings=f) == "FAIL"
    # With the distribution stamped, the same report passes the claim-backed check.
    f2 = _cache_findings(_cache_dist("mostly-warm", _WARM_VALS, 0.1))
    assert _tag_for(rep, _CACHE_CLAIM, tmp_path, findings=f2) == "PASS"


def test_vr_cache_claim_old_findings_skips_with_detail(tmp_path: Path):
    # Pre-cache_dist findings (no data_sources.cache_dist_probe): the guard is N/A and must
    # SKIP with a stated reason, so committed worked examples stay green until regenerated.
    rep = _cache_pole_report(_CHURN_NOTE)
    f = _cache_findings(None, probe=False)
    assert _tag_for(rep, _CACHE_CLAIM, tmp_path, findings=f) == "SKIP"
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) == "SKIP"


def test_vr_cache_framing_warm_distribution_with_churn_framing_fails(tmp_path: Path):
    # The core class: a mostly-warm measured distribution, yet the report still frames it as
    # "cache-key churn - BIGGEST LEVER" with no caveat marker. Must FAIL.
    rep = _cache_pole_report(_CHURN_NOTE)
    f = _cache_findings(_cache_dist("mostly-warm", _WARM_VALS, 0.1))
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) == "FAIL"


def test_vr_cache_framing_warm_with_caveat_and_reframed_note_passes(tmp_path: Path):
    # The engine-fixed render of the SAME mostly-warm pole: churn hint + BIGGEST LEVER stripped,
    # reframed around the warm median, and the cache-context marker present → PASS (red→green).
    rep = _cache_pole_report(_WARM_NOTE, with_marker=True)
    f = _cache_findings(_cache_dist("mostly-warm", _WARM_VALS, 0.1))
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) == "PASS"


def test_vr_cache_framing_cold_exemption_allows_broad_claim(tmp_path: Path):
    # Exemption direction 1: a genuinely COLD cache (every upstream run 0-cached) legitimately
    # keeps a broad "Remote caching DISABLED - BIGGEST LEVER" claim, no caveat marker needed.
    rep = _cache_pole_report(_COLD_NOTE)
    cold_vals = [_cache_val(100), _cache_val(100), _cache_val(100)]
    f = _cache_findings(_cache_dist("cold", cold_vals, 1.0, fix_key="turbo-remote-cache"))
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) == "PASS"


def test_vr_cache_framing_stamped_cold_but_warm_values_fails(tmp_path: Path):
    # Exemption direction 2 (the narrow-flag pin): the engine STAMPS verdict "cold" but the
    # pole's own values are warm → re-derived "mostly-warm" != stamped "cold" → FAIL. A buggy
    # or adversarial engine cannot widen the cold exemption by lying in the stamp.
    rep = _cache_pole_report(_COLD_NOTE)  # broad claim, no marker
    f = _cache_findings(_cache_dist("cold", _WARM_VALS, 0.1))  # values contradict the stamp
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) == "FAIL"


def test_vr_cache_framing_miss_tail_requires_caveat(tmp_path: Path):
    # A miss-tail verdict (median low, but a material miss-heavy tail) keeps the lever but MUST
    # carry the caveat marker and drop the churn framing. Without the marker → FAIL; with it → PASS.
    tail_dist = _cache_dist("miss-tail", _WARM_VALS, 0.4)
    no_marker = _cache_pole_report("72/128 cached - 44% rebuilt despite caching ON")
    assert _tag_for(no_marker, _CACHE_FRAMING, tmp_path, findings=_cache_findings(tail_dist)) == "FAIL"
    with_marker = _cache_pole_report("72/128 cached - 44% rebuilt despite caching ON",
                                     with_marker=True)
    assert _tag_for(with_marker, _CACHE_FRAMING, tmp_path, findings=_cache_findings(tail_dist)) == "PASS"


def test_vr_cache_dist_malformed_values_skip_not_crash(tmp_path: Path):
    # Defensive: a malformed cache_dist (values as a dict, not a list) coerces to empty →
    # re-derives "insufficient"; with a matching stamp the checks complete without crashing.
    # Uses a NEUTRAL cache-claim note (no BIGGEST LEVER / churn framing) so this test stays about
    # the crash contract — the insufficient over-claim ban is exercised separately below.
    neutral_note = "72/128 cached - 44% rebuilt despite caching ON"
    rep = _cache_pole_report(neutral_note)
    bad = {"fix_key": "turbo-partial-cache", "pr": {"values": {"oops": 1}},
           "tail": "not-a-dict", "verdict": "insufficient"}
    f = _cache_findings(bad)
    rp = tmp_path / "report-2026-05-29.md"
    rp.write_text(rep, encoding="utf-8")
    fp = tmp_path / "findings.json"
    fp.write_text(json.dumps(f), encoding="utf-8")
    r = subprocess.run([sys.executable, str(_VERIFY), "--report", str(rp), "--findings", str(fp)],
                       capture_output=True, text=True)
    assert "Traceback" not in r.stderr, f"malformed cache_dist crashed the gate:\n{r.stderr}"
    assert r.returncode in (0, 1)
    # The framing check completed with a real verdict (stamped insufficient == re-derived); a
    # neutral note carries no over-claim, so it passes.
    assert _tag_for(rep, _CACHE_FRAMING, tmp_path, findings=f) in ("PASS", "SKIP")


def test_vr_cache_framing_insufficient_still_bans_overclaim(tmp_path: Path):
    # PR #126 3rd-review (adversarial): the false-PASS gap. On a thin/fork-heavy sample the verdict
    # is `insufficient` (fewer than 2 upstream runs exposed a summary), so the cross-run grounding
    # can't be established — but the drilled run's native "cache-key churn - BIGGEST LEVER" framing
    # used to ship unchecked (the framing guard policed only miss-tail/mostly-warm). It must NOT
    # over-claim: an insufficient pole whose body still labels BIGGEST LEVER / frames churn FAILs.
    one_upstream = [_cache_val(88)]   # a single non-fork run -> re-derives "insufficient"
    dist = _cache_dist("insufficient", one_upstream, 1.0)
    f = _cache_findings(dist)
    # Over-claiming body (churn + BIGGEST LEVER) under an insufficient verdict → FAIL.
    assert _tag_for(_cache_pole_report(_CHURN_NOTE), _CACHE_FRAMING, tmp_path, findings=f) == "FAIL"
    # The engine-fixed render: over-claim stripped, single-run basis disclosed → PASS. `insufficient`
    # needs no cache-context marker (there is no distribution to size against).
    basis_note = ("72/128 cached - 44% rebuilt despite caching ON - basis: a single sampled run's "
                  "log; too few runs exposed a cache summary to establish the cross-run hit rate")
    assert _tag_for(_cache_pole_report(basis_note), _CACHE_FRAMING, tmp_path, findings=f) == "PASS"


def test_vr_pole_header_preserves_hyphenated_check_name():
    # PR #126 3rd-review (adversarial, HIGH): `_pole_header_sections` split the post-▸ text on ANY
    # dash and took [0], so a check whose name contains a spaced hyphen ("Build - Docker") was
    # truncated to "Build". The render boundary flattens the em-dash dur separator to an ASCII ' - '
    # too, so the dur can't be told from an internal hyphen by glyph — the fix anchors on the
    # trailing _clock-shaped duration. This truncation broke the (wf, check) key the cache
    # invariants map poles by → a mostly-warm over-claim slipped through (false PASS) and a correct
    # report could be forced to FAIL (false FAIL). Assert hyphenated names survive, dur stripped.
    vr = _load_verify_report()
    for dash in ("—", "-"):   # em-dash (pre-sanitize) AND ASCII hyphen (post-_strip_emdashes)
        secs = vr._pole_header_sections(
            f"## 🔴 Long pole 1: `ci.yml` ▸ Build - Docker {dash} 5m 00s\n\nbody\n\n## next\n")
        assert secs == [("ci.yml", "Build - Docker", "\n\nbody\n\n")], f"dash={dash!r}: {secs}"
    # A plain (un-hyphenated) name still has ONLY its trailing duration stripped.
    secs2 = vr._pole_header_sections("## 🔴 Long pole 1: `ci.yml` ▸ build - 2m 54s\n\nx\n")
    assert secs2 == [("ci.yml", "build", "\n\nx\n")], secs2
    # A seconds-only duration is stripped too.
    secs3 = vr._pole_header_sections("## 🔴 Long pole 1: `ci.yml` ▸ lint - eslint - 45s\n\ny\n")
    assert secs3 == [("ci.yml", "lint - eslint", "\n\ny\n")], secs3


# --- The phantom-gate class (check_headline_pole_actually_gates) --------------------
# The expo/expo bug: the headline `critical_path_check` was `check-packages`, which by the
# findings' own populations was the actual slowest job (the merge gate) on ZERO of 20 PRs —
# a lightweight always-present check crowned over the real 20-minute native suites. This
# invariant re-derives the per-PR pole from populations and fails a phantom headline.
_HEADGATE = "headline critical-path check is an actual recurring gate"


def _pops(prs: list[list[tuple[str, float]]]) -> list:
    """`populations` shape: [share, [[check, dur], ...]] per PR."""
    return [[1.0 / max(len(prs), 1), [[c, d] for c, d in pr]] for pr in prs]


# 20 PRs: `heavy` is the slowest on 8, `med` on 12; `check-packages` runs on all 20 but is
# NEVER the slowest (a heavier sibling always co-runs) → pole frequency 0 (the expo shape).
_PHANTOM_POPS = _pops([[("check-packages", 180.0), ("heavy", 1400.0)]] * 8
                      + [[("check-packages", 180.0), ("med", 400.0)]] * 12)


def test_vr_headline_pole_phantom_gate_fails(tmp_path: Path):
    # Phantom headline: check-packages is the actual pole on 0/20 → FAIL.
    bad = {"pr_critical_path": {"critical_path_check": "check-packages",
                                "populations": _PHANTOM_POPS}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=bad) == "FAIL"
    # Engine-fixed headline: `med` gates 12/20 (>= floor) → PASS (red→green).
    ok = {"pr_critical_path": {"critical_path_check": "med", "populations": _PHANTOM_POPS}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=ok) == "PASS"
    # `heavy` gates exactly 8/20 (>= floor 2) → PASS too.
    ok2 = {"pr_critical_path": {"critical_path_check": "heavy", "populations": _PHANTOM_POPS}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=ok2) == "PASS"


def test_vr_headline_pole_required_check_exempt(tmp_path: Path):
    # A required check gates by definition (branch protection) even if never the slowest → PASS.
    f = {"required_checks": ["check-packages"],
         "pr_critical_path": {"critical_path_check": "check-packages",
                              "populations": _PHANTOM_POPS}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=f) == "PASS"


def test_vr_headline_pole_thin_sample_skips(tmp_path: Path):
    # Below the min-PR floor the pole frequency is noise → SKIP, not a false FAIL.
    thin = {"pr_critical_path": {"critical_path_check": "check-packages",
                                 "populations": _pops([[("check-packages", 180.0),
                                                        ("heavy", 1400.0)]] * 3)}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=thin) == "SKIP"


def test_vr_headline_pole_monorepo_name_collision(tmp_path: Path):
    # Adversarial (PR #126 review): two scoped monorepo checks share a post-scope name. The guard
    # must key on the RAW name, not a scope-normalized one, or a real phantom `@teamB/build` (pole
    # on 0) false-PASSES by colliding with `@teamA/build` (pole on many).
    pops = _pops([[("@teamA/build", 1400.0), ("@teamB/build", 180.0)]] * 8
                 + [[("@teamA/build", 1400.0), ("light", 90.0)]] * 8)
    # @teamB/build is never the slowest -> phantom headline must FAIL (not laundered by collision).
    bad = {"pr_critical_path": {"critical_path_check": "@teamB/build", "populations": pops,
                                "checks": [{"name": "@teamA/build"}, {"name": "@teamB/build"},
                                           {"name": "light"}]}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=bad) == "FAIL"
    # @teamA/build IS the real gate -> PASS.
    ok = {"pr_critical_path": {"critical_path_check": "@teamA/build", "populations": pops,
                               "checks": [{"name": "@teamA/build"}, {"name": "@teamB/build"}]}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=ok) == "PASS"


def test_vr_headline_pole_fragmented_repo_no_clean_gate_passes(tmp_path: Path):
    # Adversarial (PR #126 review): a fragmented repo where every PR is gated by a DIFFERENT check
    # (each the pole on 1 PR). No check reaches the recurrence floor, so the engine legitimately
    # crowns the slowest-overall — there is no better recurring gate to prefer. Must NOT false-FAIL.
    pops = _pops([[(f"c{i}", 1000.0 + i)] for i in range(8)])   # 8 PRs, 8 distinct one-PR poles
    f = {"pr_critical_path": {"critical_path_check": "c7", "populations": pops,
                              "checks": [{"name": f"c{i}"} for i in range(8)]}}
    assert _tag_for(_good(), _HEADGATE, tmp_path, findings=f) == "PASS"


# --- plan 007: the anti-unregistered-claim meta-guard (source lint + coupling + coverage) ------

_FRAMING_LINT_TEMPLATES = (
    "slowest check a typical PR waits on",
    "slowest check a PR waits on",
    "slowest a PR waits on",
    "wall-clock-neutral runner spend",
    "machine-derived proof",
    "modeled bill opportunities remain",
)


def _framing_literal_violations(source: str, templates=_FRAMING_LINT_TEMPLATES) -> list[tuple[int, str]]:
    """Every occurrence of a `FRAMING_VOCABULARY` phrase in a STRING LITERAL in `source` must sit
    inside a statement that constructs a `Claim(...)`. Returns (lineno, offending_static_run) per
    violation.

    AST-based, not a raw substring scan, for two robustness reasons the naive approach gets wrong:
      1. The renderer builds sentences ACROSS implicitly-concatenated f-string lines (e.g. the
         headline's "... typical PR " / "waits on is ..."), so the phrase is not a contiguous
         substring of the raw source — the parser merges the adjacent literals into one node, so we
         see the true rendered contiguity. A `FormattedValue` (`{x}`) legitimately BREAKS contiguity,
         so we never falsely join across an interpolation.
      2. Comments are not in the AST, so a comment mentioning the phrase is correctly ignored (only
         real string literals are linted). Docstrings are Constant nodes and ARE linted — none in the
         renderer carry a template today, and one that did would (correctly) have to be a claim or be
         reworded."""
    tree = ast.parse(source)
    parents: dict = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing_stmt(node):
        cur = parents.get(node)
        while cur is not None and not isinstance(cur, ast.stmt):
            cur = parents.get(cur)
        return cur

    def static_runs(node):
        # Maximal contiguous static-text runs; a FormattedValue breaks a run.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            buf: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    buf.append(v.value)
                elif buf:
                    yield "".join(buf)
                    buf = []
            if buf:
                yield "".join(buf)

    def has_claim_call(stmt) -> bool:
        return stmt is not None and any(
            isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "Claim")
                or (isinstance(n.func, ast.Name) and n.func.id == "Claim"))
            for n in ast.walk(stmt))

    lowered = [t.lower() for t in templates]

    def _norm(s: str) -> str:
        # Collapse whitespace runs so a double-spaced or line-wrapped framing literal (which
        # markdown renders as the sentence) is still matched — mirrors the coverage check's
        # whitespace-flexible scan so the two guards can't be split by a whitespace variant.
        return re.sub(r"\s+", " ", s).lower()

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        # A JoinedStr's own visit yields the joined static runs (and catches a phrase split across
        # its adjacent literals); skip its Constant children so an f-string framing literal isn't
        # counted twice (once at the JoinedStr, once at the child Constant).
        if isinstance(node, ast.Constant) and isinstance(parents.get(node), ast.JoinedStr):
            continue
        hit = next((r for r in static_runs(node) if any(t in _norm(r) for t in lowered)), None)
        if hit is None:
            continue
        if not has_claim_call(enclosing_stmt(node)):
            violations.append((getattr(node, "lineno", 0), hit))
    return violations


def test_framing_source_lint_every_family_literal_is_a_claim():
    # The meta-guard's TEETH: an unregistered "slowest check ... waits on" framing sentence in the
    # renderer fails CI at source level. Real source: ZERO violations (every family literal is wrapped
    # in a Claim). Then red-green on synthetic snippets.
    source = (_SKILL_DIR / "scripts" / "blocking_path.py").read_text(encoding="utf-8")
    real = _framing_literal_violations(source)
    assert real == [], f"unregistered framing literal(s) in blocking_path.py: {real}"

    # RED — a rogue role assignment OUTSIDE a Claim(...).
    rogue = ('def r(cs):\n'
             '    role = "**The slowest check a PR waits on.**"\n'
             '    return role\n')
    assert _framing_literal_violations(rogue), "a bare framing literal must be flagged"

    # RED — the split-across-lines trap: an unregistered sentence built across two f-string lines is
    # STILL caught (a raw substring scan would miss it — the phrase is not contiguous in the source).
    split = ('def r(x):\n'
             '    role = (f"the slowest check a typical "\n'
             '            f"PR waits on {x}")\n'
             '    return role\n')
    assert _framing_literal_violations(split), "a split-literal framing sentence must be flagged"

    # GREEN — the SAME literal, wrapped in a Claim(...), is accepted.
    wrapped = ('def r(cs, x):\n'
               '    return cs.add(claims.Claim(kind="pole_role_line", subject=x, fields={},\n'
               '        rendered="**The slowest check a PR waits on.**"))\n')
    assert _framing_literal_violations(wrapped) == [], "a Claim-wrapped literal must be accepted"

    # GREEN — a comment mentioning the phrase is not a string literal, so it is ignored.
    commented = ('def r():\n'
                 '    # this is the slowest check a PR waits on line, described in prose\n'
                 '    return 1\n')
    assert _framing_literal_violations(commented) == [], "a comment must not be flagged"

    # RED — STATEMENT scope: a bare framing literal in its OWN statement must still be flagged even
    # when an UNRELATED Claim(...) exists in a SEPARATE statement nearby. This pins the lint to the
    # enclosing statement, not the whole function/module — a mutation broadening `has_claim_call` to
    # the tree would make the real-source `== []` pass vacuously (every literal is "near a Claim"),
    # and only this discriminator catches it.
    separate = ('def r(cs, x):\n'
                '    role = "**The slowest check a PR waits on.**"\n'
                '    cs.add(claims.Claim(kind="pole_role_line", subject=x, fields={},\n'
                '        rendered="an unrelated backing sentence"))\n'
                '    return role\n')
    assert _framing_literal_violations(separate), (
        "a framing literal in a statement separate from the Claim must still be flagged "
        "(the lint scopes to the statement, not the function)")

    # GREEN — a framing literal that IS a Claim's `rendered`, in a statement that ALSO contains
    # other calls, is accepted (the Claim call in the SAME statement covers it).
    mixed = ('def r(cs, x):\n'
             '    return foo(bar(), cs.add(claims.Claim(kind="pole_role_line", subject=x,\n'
             '        fields={}, rendered="**The slowest check a PR waits on.**")))\n')
    assert _framing_literal_violations(mixed) == [], (
        "a Claim-wrapped literal alongside other calls in the same statement must be accepted")

    # RED — whitespace variant (guard-evasion hardening): a double-spaced framing literal (renders
    # as the sentence) is still flagged, mirroring the coverage check's whitespace-flexible scan.
    ws_literal = ('def r():\n'
                  '    role = "**The slowest check a  PR waits on.**"\n'
                  '    return role\n')
    assert _framing_literal_violations(ws_literal), "a double-spaced framing literal must be flagged"


def test_framing_vocabulary_stays_coupled_to_claims():
    # verify_report embeds its OWN copy of FRAMING_VOCABULARY (it imports no engine module). Pin the
    # copy equal to the source of truth so the two can't silently drift (a phrase added to claims.py
    # but not the gate would leave that phrase unguarded by the coverage check).
    vr = _load_verify_report()
    bp = _load_blocking_path()
    assert vr._FRAMING_VOCABULARY == bp.claims.FRAMING_VOCABULARY
    # The source lint's copy in THIS test is the same tuple, too.
    assert _FRAMING_LINT_TEMPLATES == bp.claims.FRAMING_VOCABULARY


def test_gate_kind_and_family_literals_stay_coupled_to_claims():
    # The manifest path keys on hardcoded kind/family literals; a rename in claims.py would silently
    # de-activate it (safe fallback → dead manifest → no other failing test). Pin the literals to
    # claims.py so a rename fails HERE, loudly.
    bp = _load_blocking_path()
    claims = bp.claims
    # Kinds/families verify_report's manifest + coverage paths depend on.
    assert "headline_slowest" in claims.KNOWN_KINDS
    fams = claims.ClaimSet().families_migrated
    assert "headline" in fams and "slowest_gate_framing" in fams and "runner_minutes" in fams
    # The renderer kinds plan 007 added must be registered (else ClaimSet.add raises at render time).
    for k in ("pole_role_line", "pole_gate_prompt", "minority_slow_note",
              "tier2_headline", "tier2_section_lead", "tier2_neutrality_line",
              "headline_chain"):
        assert k in claims.KNOWN_KINDS
    # And the verifier SOURCE still references the literals it keys on (a rename on the gate side).
    verifier = _VERIFY.read_text(encoding="utf-8")
    for literal in ("headline_slowest", "slowest_gate_framing", "runner_minutes",
                    "tier2_headline", "tier2_section_lead", "tier2_neutrality_line",
                    "headline_chain"):
        assert literal in verifier, f"verify_report lost the {literal!r} manifest-path literal"


def _framing_manifest(claims_list, family=True) -> dict:
    return {"families_migrated": (["headline", "slowest_gate_framing"] if family else ["headline"]),
            "claims": claims_list}


def _run_coverage(tmp_path: Path, report: str, manifest: dict | None):
    """Write report (+ optional manifest) and return the coverage Check, called directly."""
    vr = _load_verify_report()
    tmp_path.mkdir(parents=True, exist_ok=True)
    rp = tmp_path / "report.md"
    rp.write_text(report, encoding="utf-8")
    if manifest is not None:
        (tmp_path / "report.md.claims.json").write_text(json.dumps(manifest), encoding="utf-8")
    return vr.check_claims_cover_framing_vocabulary(report, rp)


def test_claims_cover_framing_vocabulary_red_green(tmp_path: Path):
    # The report-level meta-guard: every family phrase occurrence must be backed by a claim whose
    # rendered sentence spans it. The prompt gate line renders INSIDE a ```text fence and capitalizes
    # "Slowest" — both must be covered (fenced scan + case-insensitive match).
    gate_line = "- Slowest check a typical PR waits on: P50 4m 15s; its workflow `ci.yml` gates 5/9 sampled PRs."
    report = ("# demo\n\n```text\nTHE GATE\n" + gate_line + "\n```\n\n"
              "**The slowest check a typical PR waits on.**\n")
    backing = [
        {"kind": "pole_gate_prompt", "subject": "build", "fields": {}, "rendered": gate_line},
        {"kind": "pole_role_line", "subject": "build", "fields": {},
         "rendered": "**The slowest check a typical PR waits on.**"},
    ]
    # GREEN — both occurrences backed.
    ok = _run_coverage(tmp_path / "g", report, _framing_manifest(backing))
    assert ok.ok and not ok.skipped, ok.detail

    # RED — drop the prompt-gate claim; the fenced, capitalized occurrence is now unbacked.
    red = _run_coverage(tmp_path / "r", report, _framing_manifest(backing[1:]))
    assert not red.ok and not red.skipped, "an unbacked framing phrase must FAIL"
    assert "Slowest check a typical PR waits on" in red.detail

    # SKIP — manifest-less (a committed artifact): the text guards still cover it.
    skip1 = _run_coverage(tmp_path / "s1", report, None)
    assert skip1.skipped, skip1.detail
    # SKIP — a 002a-era manifest that does NOT declare the slowest_gate_framing family.
    skip2 = _run_coverage(tmp_path / "s2", report, _framing_manifest(backing, family=False))
    assert skip2.skipped, skip2.detail

    # Containment, not line-equality: one line carrying TWO different family phrases needs a backing
    # claim for EACH. A single claim covering only phrase 1 leaves phrase 2 unbacked.
    two = ("# demo\n\n`X` is the slowest check a typical PR waits on but rarely the actual "
           "slowest check a PR waits on here.\n")
    only_first = [{"kind": "headline_slowest", "subject": "X", "fields": {},
                   "rendered": "`X` is the slowest check a typical PR waits on but rarely the actual "
                               "slowest check a PR waits on here."}]
    # This single claim spans BOTH phrases, so it covers both -> PASS (the claim's rendered contains
    # the whole line). Now a claim that spans only the FIRST phrase must leave the second unbacked.
    assert _run_coverage(tmp_path / "t1", two, _framing_manifest(only_first)).ok
    half = [{"kind": "headline_slowest", "subject": "X", "fields": {},
             "rendered": "`X` is the slowest check a typical PR waits on"}]
    red2 = _run_coverage(tmp_path / "t2", two, _framing_manifest(half))
    assert not red2.ok and not red2.skipped, "the second phrase on the same line must be unbacked"
    assert "slowest check a PR waits on" in red2.detail

    # Whitespace variant (guard-evasion hardening): a double-spaced / line-wrapped framing phrase —
    # which markdown renders as the sentence — must still be FOUND and, unbacked, FAIL (an exact-
    # whitespace scan would miss it and let it slip past).
    ws = "# demo\n\n`X` is the slowest check a typical  PR waits\non, unregistered.\n"
    ws_res = _run_coverage(tmp_path / "ws", ws, _framing_manifest([]))
    assert not ws_res.ok and not ws_res.skipped, "a double-spaced/wrapped framing phrase must not escape"


def _tier2_doc_for_verify() -> dict:
    doc = {
        "repo": "demo/repo",
        "repo_visibility": "private",
        "scanned_at": "2026-07-04T00:00:00Z",
        "skill_commit_sha": _head_short_sha() or "7039302",
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
        "findings": [{
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
                "ref": ("superseded runs: same head_branch, a newer run started before this "
                        "one finished (timestamp overlap); cancellation cause is inference"),
            },
            "sku": "linux_2_core",
            "sku_class": "linux-standard",
            "billing_class": "dollar",
            "usd_saving_per_month": 0.72,
            "measured_evidence": {
                "summary": ("3 superseded runs by timestamp overlap, credited at the 90% "
                            "mean cancellable remainder"),
                "table": {"headers": ["Workflow", "Overlapping runs"],
                          "rows": [["`ci.yml`", "3 confirmed"]]},
                "note": "Cancellation cause is inference. Credited on the remainder basis.",
            },
            "fix_recipe_anchor": "opt46--superseded-runs",
        }],
    }
    spine_doc = _render_ready_runner_minute_spine_doc()
    doc["data_sources"] = copy.deepcopy(spine_doc["data_sources"])
    doc["per_workflow_monthly_volume"] = copy.deepcopy(spine_doc["per_workflow_monthly_volume"])
    doc["runner_minute_spine"] = copy.deepcopy(spine_doc["runner_minute_spine"])
    row = doc["runner_minute_spine"]["rows"][0]
    row["job_name"] = "cleanup"
    row["raw_compute_runner_min_per_month"] = 150.0
    row["billable_equiv_min_per_month"] = 200.0
    row["sku_weighted_billable_min_per_month"] = 200.0
    row["usd_per_month"] = 1.2
    doc["runner_minute_spine"]["totals"] = {
        "row_count": 1,
        "raw_compute_runner_min_per_month": 150.0,
        "billable_equiv_min_per_month": 200.0,
        "sku_weighted_billable_min_per_month": 200.0,
        "usd_per_month": 1.2,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    return doc


def _tier2_dedupe_drops_a_finding_doc() -> dict:
    """Issue #4 shape: a promoted Tier-2 finding (so the section + lead render) plus TWO
    EXACT-duplicate modeled findings (same source/pattern/workflow_file/line/evidence).
    The renderer de-overlaps them with `_dedupe_findings` BEFORE building the section, so
    the section-lead's 'not promoted: N modeled item(s)' tail counts ONE, matching the
    single appendix row the reader sees. A re-derivation that counts the RAW findings list
    would see TWO and disagree — the off-by-one this fixture pins."""
    doc = _tier2_doc_for_verify()
    dup_a = {
        "id": "f-dup-a", "pattern": "OPT17", "severity": "LOW",
        "title": "Sleep-Based Readiness Wait",
        "workflow_file": ".github/workflows/ci.yml", "line": 42,
        "affected_jobs": ["integration"],
        "evidence": "job `integration` runs `sleep 10` for a readiness wait",
        "runner_min_saving": 30.0, "wall_clock_p50_s": 0.0, "sizing_basis": "modeled",
        "fix_recipe_anchor": "opt17--sleep-based-readiness-wait",
    }
    dup_b = copy.deepcopy(dup_a)
    dup_b["id"] = "f-dup-b"  # a distinct id, but identical on the dedupe key → collapsed
    doc["findings"].append(dup_a)
    doc["findings"].append(dup_b)
    return doc


def _tier2_dedupe_drops_a_promoted_finding_doc() -> dict:
    """Issue #4, PROMOTED side: the base promoted Tier-2 finding plus an EXACT-duplicate of
    it (identical source/pattern/workflow_file/line/evidence; only `id` differs). The
    renderer de-overlaps them with `_dedupe_findings` BEFORE ranking, so the section shows
    ONE promoted row and the lead's neutral-finding count / credited-minute total reflect
    ONE. `_tier2_ranked` does no internal dedup, so a re-derivation over the RAW findings
    would rank TWO and inflate count/raw_min/usd — the money-and-minutes half of the same
    off-by-one the tail-side fixture (`_tier2_dedupe_drops_a_finding_doc`) pins."""
    doc = _tier2_doc_for_verify()
    dup = copy.deepcopy(doc["findings"][0])
    dup["id"] = str(dup.get("id") or "f0") + "-dup"  # distinct id, identical dedupe key → collapsed
    doc["findings"].append(dup)
    return doc


def _runner_minute_spine_doc() -> dict:
    return {
        "repo": "demo/repo",
        "repo_visibility": "private",
        "per_workflow_monthly_volume": {
            ".github/workflows/ci.yml": 10,
        },
        "runner_minute_spine": {
            "schema_version": 1,
            "source": "jobs_api_sampled_runs",
            "coverage_scope": "sampled_workflows_with_job_data",
            "complete_repo_coverage": False,
            "render_ready": False,
            "render_blocker": (
                "first data-only slice: rows cover sampled workflows with job data; "
                "latest-attempt and prior-attempt samples are scaled to all-status "
                "workflow volume; unsampled/triaged workflows are not yet complete"),
            "extrapolation_basis": (
                "sampled_job_occurrence_fraction_x_all_status_30d_workflow_volume"),
            "attempt_coverage": "latest_and_prior",
            # Derived fact (PR-S2): equals `prior_attempt_row_count > 0`.
            "prior_attempts_included": False,
            "latest_attempt_row_count": 1,
            "prior_attempt_row_count": 0,
            "repo_visibility": "private",
            "rates_verified_date": "2026-07-03",
            "rows": [{
                "workflow_file": ".github/workflows/ci.yml",
                "job_name": "build",
                "runner_label": "ubuntu-latest",
                "sku": "linux_2_core",
                "event_scope": "all-events",
                "status_filter": "success",
                "attempt_filter": "latest",
                "volume_filter": "all-status",
                "sample_window_start": "2026-06-01T00:00:00Z",
                "sample_window_end": "2026-06-02T00:00:00Z",
                "sampled_workflow_run_count": 2,
                "sampled_job_occurrence_count": 2,
                "sampled_positive_duration_occurrence_count": 2,
                "occurrence_fraction": 1.0,
                "workflow_30d_volume": 100,
                "effective_monthly_job_volume": 100.0,
                "mean_sampled_compute_seconds": 60.0,
                "mean_sampled_billable_equiv_minutes": 1.5,
                "raw_compute_runner_min_per_month": 100.0,
                "billable_equiv_min_per_month": 150.0,
                "sku_weighted_billable_min_per_month": 150.0,
                "billing_class": "dollar",
                "usd_per_month": 0.9,
                "share_of_all_row_total": 1.0,
            }],
            "totals": {
                "row_count": 1,
                "raw_compute_runner_min_per_month": 100.0,
                "billable_equiv_min_per_month": 150.0,
                "sku_weighted_billable_min_per_month": 150.0,
                "usd_per_month": 0.9,
                "percentage_denominator": "all_rows_billable_equiv_min_per_month",
            },
        },
        "findings": [],
    }


def _rendered_runner_minute_spine_report(*, workflow: str = ".github/workflows/ci.yml",
                                         job: str = "build",
                                         runner: str = "ubuntu-latest",
                                         sku: str = "linux_2_core",
                                         event: str = "all-events",
                                         status: str = "success",
                                         attempt: str = "latest",
                                         volume: str = "all-status",
                                         billing: str = "dollar",
                                         raw: str = "100.000",
                                         billable: str = "150.000",
                                         weighted: str = "150.000",
                                         usd: str = "$0.90",
                                         share: str = "100.000%",
                                         extra_rows: str = "",
                                         total_raw: str = "100.000",
                                         total_billable: str = "150.000",
                                         total_weighted: str = "150.000",
                                         total_usd: str = "$0.90",
                                         total_share: str = "100.000%",
                                         suffix: str = "") -> str:
    # Minutes-only cost-spine table (the priced-dollar surface — SKU / Billing /
    # Weighted min/mo / USD/mo columns — was excised 2026-07-20). The sku / billing /
    # weighted / usd / total_weighted / total_usd kwargs are accepted for call-site
    # compatibility but never rendered.
    del sku, billing, weighted, usd, total_weighted, total_usd
    return f"""# report

<!-- ci-speedup:runner-minute-spine -->
| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| `{workflow}` | `{job}` | `{runner}` | {event} | {status} | {attempt} | {volume} | {raw} | {billable} | {share} |
{extra_rows}| Total |  |  |  |  |  |  | {total_raw} | {total_billable} | {total_share} |
{suffix}
"""


def _render_ready_runner_minute_spine_doc() -> dict:
    doc = _runner_minute_spine_doc()
    doc["data_sources"] = {"cost_spine_job_fetch_failures": 0}
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = True
    spine["render_ready"] = True
    spine["render_blocker"] = ""
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    return doc


def _render_ready_runner_minute_spine_doc_with_prior() -> dict:
    doc = _render_ready_runner_minute_spine_doc()
    prior = copy.deepcopy(doc["runner_minute_spine"]["rows"][0])
    prior["attempt_filter"] = "prior"
    prior["status_filter"] = "all-status"
    prior["sampled_workflow_run_count"] = 10
    prior["sampled_job_occurrence_count"] = 1
    prior["sampled_positive_duration_occurrence_count"] = 1
    prior["occurrence_fraction"] = 0.1
    prior["effective_monthly_job_volume"] = 10.0
    prior["raw_compute_runner_min_per_month"] = 10.0
    prior["billable_equiv_min_per_month"] = 15.0
    prior["sku_weighted_billable_min_per_month"] = 15.0
    prior["usd_per_month"] = 0.09
    prior["share_of_all_row_total"] = 0.091
    doc["runner_minute_spine"]["rows"][0]["share_of_all_row_total"] = 0.909
    doc["runner_minute_spine"]["rows"].append(prior)
    doc["runner_minute_spine"]["prior_attempt_row_count"] = 1
    doc["runner_minute_spine"]["prior_attempts_included"] = True  # derived: count > 0
    doc["runner_minute_spine"]["totals"] = {
        "row_count": 2,
        "raw_compute_runner_min_per_month": 110.0,
        "billable_equiv_min_per_month": 165.0,
        "sku_weighted_billable_min_per_month": 165.0,
        "usd_per_month": 0.99,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    return doc


def _render_ready_runner_minute_spine_doc_with_mixed_billing() -> dict:
    doc = _render_ready_runner_minute_spine_doc()
    doc["repo_visibility"] = "public"
    spine = doc["runner_minute_spine"]
    spine["repo_visibility"] = "public"
    capacity = spine["rows"][0]
    capacity["billing_class"] = "capacity"
    capacity["share_of_all_row_total"] = 0.5

    dollar = copy.deepcopy(capacity)
    dollar["job_name"] = "larger"
    dollar["runner_label"] = "linux_4_core"
    dollar["sku"] = "linux_4_core"
    dollar["billing_class"] = "dollar"
    dollar["sku_weighted_billable_min_per_month"] = 300.0
    dollar["usd_per_month"] = 1.8
    dollar["share_of_all_row_total"] = 0.5
    spine["rows"].append(dollar)
    spine["latest_attempt_row_count"] = 2
    spine["totals"] = {
        "row_count": 2,
        "raw_compute_runner_min_per_month": 200.0,
        "billable_equiv_min_per_month": 300.0,
        "sku_weighted_billable_min_per_month": 450.0,
        "usd_per_month": 2.7,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    return doc


def _render_ready_runner_minute_spine_doc_many_rows(count: int = 13) -> dict:
    doc = _render_ready_runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    base = copy.deepcopy(spine["rows"][0])
    rows = []
    for idx in range(count):
        row = copy.deepcopy(base)
        row["job_name"] = f"build-{idx:02d}"
        row["share_of_all_row_total"] = round(1 / count, 3)
        rows.append(row)
    spine["rows"] = rows
    spine["latest_attempt_row_count"] = count
    spine["prior_attempt_row_count"] = 0
    spine["totals"] = {
        "row_count": count,
        "raw_compute_runner_min_per_month": round(100.0 * count, 3),
        "billable_equiv_min_per_month": round(150.0 * count, 3),
        "sku_weighted_billable_min_per_month": round(150.0 * count, 3),
        "usd_per_month": round(0.9 * count, 2),
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    return doc


def _rendered_prior_runner_minute_spine_row(*, status: str = "all-status",
                                            attempt: str = "prior",
                                            share: str = "9.100%") -> str:
    return (
        "| `.github/workflows/ci.yml` | `build` | `ubuntu-latest` | "
        f"all-events | {status} | {attempt} | all-status | "
        f"10.000 | 15.000 | {share} |\n")


def _tier2_artifacts(tmp_path: Path, doc: dict | None = None):
    bp = _load_blocking_path()
    doc = copy.deepcopy(doc if doc is not None else _tier2_doc_for_verify())
    preserve_spine = bool(doc.pop("_preserve_runner_minute_spine", False))
    if not preserve_spine:
        _ensure_tier2_source_rows(doc)
    report = bp.render(doc)
    report_path = tmp_path / "tier2.md"
    findings_path = tmp_path / "findings.json"
    report_path.write_text(report, encoding="utf-8")
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "tier2.md.claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json(), indent=2) + "\n", encoding="utf-8")
    return report, report_path, findings_path


def _tier2_spine_row(doc: dict, *, job: str = "cleanup",
                     workflow: str = ".github/workflows/ci.yml",
                     sku: str = "linux_2_core", runner: str = "ubuntu-latest",
                     billing: str = "dollar", raw: float = 150.0,
                     billable: float = 200.0, weighted: float | None = 200.0,
                     usd: float | None = 1.2, event: str = "all-events",
                     status: str = "success", attempt: str = "latest",
                     volume: str = "all-status") -> dict:
    row = copy.deepcopy(doc["runner_minute_spine"]["rows"][0])
    row["workflow_file"] = workflow
    row["job_name"] = job
    row["runner_label"] = runner
    row["sku"] = sku
    row["event_scope"] = event
    row["status_filter"] = status
    row["attempt_filter"] = attempt
    row["volume_filter"] = volume
    row["billing_class"] = billing
    row["raw_compute_runner_min_per_month"] = raw
    row["billable_equiv_min_per_month"] = billable
    row["sku_weighted_billable_min_per_month"] = weighted
    row["usd_per_month"] = usd
    return row


def _set_tier2_spine_rows(doc: dict, rows: list[dict]) -> None:
    doc["_preserve_runner_minute_spine"] = True
    billed_total = sum(float(row.get("billable_equiv_min_per_month") or 0.0) for row in rows)
    usd_vals = [row.get("usd_per_month") for row in rows if row.get("usd_per_month") is not None]
    for row in rows:
        billed = float(row.get("billable_equiv_min_per_month") or 0.0)
        row["share_of_all_row_total"] = round(billed / billed_total, 3) if billed_total else 0.0
    spine = doc["runner_minute_spine"]
    spine["rows"] = rows
    spine["latest_attempt_row_count"] = sum(
        1 for row in rows if str(row.get("attempt_filter") or "") == "latest")
    spine["prior_attempt_row_count"] = sum(
        1 for row in rows if str(row.get("attempt_filter") or "") == "prior")
    # Derived fact (PR-S2): the flag states what the sample contains.
    spine["prior_attempts_included"] = spine["prior_attempt_row_count"] > 0
    workflows = sorted({str(row.get("workflow_file") or "") for row in rows if row.get("workflow_file")})
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": len(workflows),
        "row_workflow_count": len(workflows),
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    doc["per_workflow_monthly_volume"] = {wf: 10 for wf in workflows}
    spine["totals"] = {
        "row_count": len(rows),
        "raw_compute_runner_min_per_month": round(
            sum(float(row.get("raw_compute_runner_min_per_month") or 0.0) for row in rows), 3),
        "billable_equiv_min_per_month": round(billed_total, 3),
        "sku_weighted_billable_min_per_month": round(sum(
            float(row.get("sku_weighted_billable_min_per_month") or 0.0) for row in rows), 3),
        "usd_per_month": round(sum(float(v) for v in usd_vals), 2) if usd_vals else None,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }


def _is_tier2_source_fixture_finding(f: dict) -> bool:
    return (isinstance(f, dict)
            and not f.get("advisory")
            and f.get("sizing_basis") == "measured"
            and isinstance(f.get("tier2_neutrality"), dict)
            and bool(f.get("tier2_neutrality"))
            and (f.get("runner_min_saving") or 0))


def _source_rows_for_finding(doc: dict, f: dict) -> list[dict]:
    wf = str(f.get("workflow_file") or ".github/workflows/ci.yml")
    sku = str(f.get("sku") or (
        "unpriced" if str(f.get("billing_class") or "") == "unpriced"
        or str(f.get("sku_class") or "") == "unpriced" else "linux_2_core"))
    runner = "linux_4_core" if sku == "linux_4_core" else (
        "self-hosted" if sku == "unpriced" else "ubuntu-latest")
    billing = str(f.get("billing_class") or "dollar")
    saving = float(f.get("runner_min_saving") or 0.0)
    raw = max(saving + 30.0, 150.0)
    billable = raw + 50.0
    expected_usd = f.get("usd_saving_per_month")
    usd = None if billing == "unpriced" else round(float(expected_usd or 0.0) + 0.48, 2)
    weighted = billable if sku != "linux_4_core" else billable * 2.0
    explicit = f.get("runner_minute_source_filter") if isinstance(
        f.get("runner_minute_source_filter"), dict) else {}
    event = str(explicit.get("event_scope") or f.get("event_scope") or "all-events")
    status = "success"
    attempt = "latest"
    volume = "all-status"
    pat = str(f.get("pattern") or "")
    if pat == "OPT64":
        status = "all-status"
        attempt = "prior"
    status = str(explicit.get("status_filter") or f.get("status_filter") or status)
    attempt = str(explicit.get("attempt_filter") or f.get("attempt_filter") or attempt)
    volume = str(explicit.get("volume_filter") or f.get("volume_filter") or volume)
    jobs = [str(job) for job in (f.get("affected_jobs") or []) if str(job)]
    rerun_job = str(f.get("rerun_dominant_job") or "").strip()
    if rerun_job and rerun_job not in jobs:
        jobs.append(rerun_job)
    burn = f.get("timeout_default_burn") if isinstance(f.get("timeout_default_burn"), dict) else {}
    for job in [burn.get("job_template"), burn.get("job_key")]:
        if job and str(job) not in jobs:
            jobs.append(str(job))
    for sample in burn.get("samples") or []:
        if isinstance(sample, dict) and sample.get("job_name") and str(sample["job_name"]) not in jobs:
            jobs.append(str(sample["job_name"]))
    if not jobs:
        jobs = [f"source {f.get('id', 'tier2')}"]
    per_job_raw = raw / len(jobs)
    per_job_billable = billable / len(jobs)
    per_job_weighted = weighted / len(jobs) if weighted is not None else None
    per_job_usd = usd / len(jobs) if usd is not None else None
    return [
        _tier2_spine_row(doc, workflow=wf, job=job, sku=sku, runner=runner,
                         billing=billing, raw=per_job_raw,
                         billable=per_job_billable, weighted=per_job_weighted,
                         usd=per_job_usd, event=event, status=status,
                         attempt=attempt, volume=volume)
        for job in jobs
    ]


def _ensure_tier2_source_rows(doc: dict) -> None:
    if "runner_minute_spine" not in doc:
        return
    rows: list[dict] = []
    for finding in doc.get("findings") or []:
        if _is_tier2_source_fixture_finding(finding):
            rows.extend(_source_rows_for_finding(doc, finding))
    if rows:
        _set_tier2_spine_rows(doc, rows)
        doc.pop("_preserve_runner_minute_spine", None)


def _sku_ceiling_finding() -> dict:
    return {
        "id": "f-sku",
        "pattern": "OPT66",
        "severity": "LOW",
        "title": "SKU Arbitrage Ceiling from Expensive Hosted Runners",
        "workflow_file": ".github/workflows/ci.yml",
        "line": 0,
        "affected_jobs": ["windows tests"],
        "evidence": (
            "job `windows tests` ran on `windows_2_core`; candidate "
            "`linux_2_core_arm` is a published-rate ceiling. This is not credited."
        ),
        "runner_min_saving": None,
        "wall_clock_p50_s": None,
        "realization": "none",
        "sizing_basis": "measured",
        "measured_signal": (
            "published-rate SKU ceiling for `windows tests`: `windows_2_core` "
            "to `linux_2_core_arm`; runner_min_saving intentionally uncredited"
        ),
        "measured_evidence": {
            "summary": "published-rate SKU ceiling for `windows tests`",
            "table": {"headers": ["Job", "Current SKU"],
                      "rows": [["`windows tests`", "`windows_2_core`"]]},
            "note": "Ceiling facts only; no runner_min_saving is credited.",
        },
        "sku_arbitrage_ceiling": {
            "kind": "opt66_sku_arbitrage_ceiling",
            "job_name": "windows tests",
            "current_sku": "windows_2_core",
            "candidate_sku": "linux_2_core_arm",
            "potential_delta_usd_per_month": 10.0,
        },
        "fix_recipe_anchor": "opt66--sku-arbitrage-ceiling-from-expensive-hosted-runners",
    }


def _sku_ceiling_doc_for_verify() -> dict:
    doc = _tier2_doc_for_verify()
    doc["findings"] = [_sku_ceiling_finding()]
    return doc


def _offpath_hygiene_finding(fid: str, line: int, job: str,
                             wf: str = ".github/workflows/ci.yml") -> dict:
    """A minimal hygiene finding that lands in the 'Also noticed' appendix — used so the
    appendix is non-empty and the pole-double-frame check can assert PASS (rather than SKIP
    on an empty appendix). What forces it INTO the appendix is that it is `modeled` with a
    positive `runner_min_saving` (so `_on_pole_job`'s first guard returns False regardless
    of pole status); it is placed on a NON-pole `job` only to keep it clearly distinct from
    the on-pole ceiling under test — do not "simplify" it onto a pole job expecting it to
    still render, that is not what keeps it in."""
    return {
        "id": fid, "pattern": "OPT17", "severity": "LOW",
        "title": "Sleep-Based Readiness Wait", "workflow_file": wf, "line": line,
        "affected_jobs": [job],
        "evidence": f"job `{job}` runs `sleep 10` for a readiness wait",
        "runner_min_saving": 30.0, "wall_clock_p50_s": 0.0, "sizing_basis": "modeled",
        "fix_recipe_anchor": "opt17--sleep-based-readiness-wait",
    }


def _sku_ceiling_on_headline_pole_doc() -> dict:
    """Issue #3 shape: an OPT66 SKU-arbitrage CEILING whose only affected job IS the
    drilled headline pole (an expensive windows/mac runner), plus one off-pole hygiene
    finding so the 'Also noticed' appendix exists. The ceiling carries no credited saving
    (`runner_min_saving=None`), so re-listing it as an 'Also noticed · ~0 wall-clock'
    row would contradict the headline that crowned the same job the biggest lever."""
    doc = _sku_ceiling_doc_for_verify()
    cp = doc["pr_critical_path"]
    cp["critical_path_check"] = "windows tests"
    cp["checks"] = [{"name": "windows tests",
                     "workflow_file": ".github/workflows/ci.yml",
                     "p50_s": 300.0, "present_on": 4, "pole_n": 4}]
    cp["poles"] = [{"check": "windows tests", "job": "windows tests",
                    "workflow_file": ".github/workflows/ci.yml", "p50_s": 300.0}]
    doc["findings"].append(_offpath_hygiene_finding("f-hygiene", 60, "docs-lint"))
    return doc


def _tier2_promoted_finding(fid: str, runner_min: float = 120.0,
                            *, proof: str = "post_completion_waste") -> dict:
    f = copy.deepcopy(_tier2_doc_for_verify()["findings"][0])
    f["id"] = fid
    f["title"] = f"Superseded runs never cancelled {fid}"
    f["line"] = 12 + int(re.sub(r"\D", "", fid) or "0")
    f["fix_key"] = f"OPT46:{fid}"
    f["fix_recipe_anchor"] = f"opt46--superseded-runs--{fid}"
    f["runner_min_saving"] = runner_min
    f["usd_saving_per_month"] = round(runner_min * 0.006, 2)
    if proof == "below_cluster_floor":
        f["pattern"] = "OPT33"
        f["title"] = "Runner-min-only job below the floor"
        f["affected_jobs"] = ["lint"]
        f["evidence"] = "lint is below the concurrent cluster floor"
        f["measured_signal"] = "job lint p50 100s over sampled PRs"
        f["tier2_neutrality"] = {
            "proof": "below_cluster_floor",
            "margin_s": 200.0,
            "ref": "floor_p50 300s - affected job p50 100s",
        }
        f["measured_evidence"] = {
            "summary": "lint job p50 is below the concurrent floor",
            "table": {"headers": ["Job", "p50"], "rows": [["`lint`", "100s"]]},
        }
    elif proof == "below_cluster_floor_opt65":
        f["pattern"] = "OPT65"
        f["title"] = "Billing Rounding Waste from Tiny Matrix Legs"
        f["affected_jobs"] = ["tiny (1)", "tiny (2)", "tiny (3)"]
        f["fix_key"] = f"OPT65:{fid}"
        f["evidence"] = (
            "2 sampled tiny matrix runs had exact billing-rounding waste from "
            "sum(ceil(job_seconds/60)) - ceil(sum(job_seconds)/60)")
        f["measured_signal"] = (
            "exact billing rounding delta "
            "sum(ceil(job_seconds/60))-ceil(sum(job_seconds)/60) for matrix base `tiny`")
        f["tier2_neutrality"] = {
            "proof": "below_cluster_floor",
            "margin_s": 45.0,
            "ref": "matrix base `tiny` combined credited leg p50 75s below floor_p50 120s",
        }
        f["rounding_waste"] = {
            "kind": "opt65_billing_rounding",
            "matrix_base": "tiny",
            "credited_jobs": ["tiny (1)", "tiny (2)", "tiny (3)"],
            "sampled_waste_min": 2,
            "sampled_successful_run_count": 1,
            "monthly_volume": 10,
            "scale": 10.0,
            "runner_min_saving": runner_min,
            "max_combined_leg_p50_s": 75.0,
            "sku": "linux_2_core",
            "samples": [{
                "sample": 1,
                "jobs": ["tiny (1)", "tiny (2)", "tiny (3)"],
                "durations_s": [20.0, 20.0, 20.0],
                "waste_min": 2,
            }],
        }
        f["measured_evidence"] = {
            "summary": "exact per-job billing rounding waste from sampled job timestamps",
            "table": {
                "headers": ["Sample", "Rounded legs", "Job durations", "Rounding waste min"],
                "rows": [["sample 1", "3 legs", "20s, 20s, 20s", "2"]],
            },
            "note": ("Measured with sum(ceil(job_seconds/60)) - "
                     "ceil(sum(job_seconds)/60); runtime is unchanged."),
        }
        f["fix_recipe_anchor"] = "opt65--billing-rounding-waste-from-tiny-matrix-legs"
    elif proof == "non_pr_event":
        f["pattern"] = "OPT36"
        f["title"] = "Cron Schedule Too Frequent"
        f["workflow_file"] = ".github/workflows/nightly.yml"
        f["affected_jobs"] = []
        f["evidence"] = "scheduled runs repeated the same head_sha"
        f["measured_signal"] = (
            "event=schedule total_count x mean job-minutes "
            "(30 schedule run(s)/30d; 3 same-head_sha redundant run(s); 4 timed run(s))")
        f["tier2_neutrality"] = {
            "proof": "non_pr_event",
            "margin_s": None,
            "ref": ("event=schedule subset only; consecutive same-head_sha schedule runs; "
                    "schedule is not a developer PR/merge event"),
        }
        f["tier2_run_subset_events"] = ["schedule"]
        f["measured_evidence"] = {
            "summary": "3 consecutive same-head_sha schedule runs by event=schedule",
            "table": {"headers": ["Workflow", "Consecutive same-head_sha schedule runs"],
                      "rows": [["`nightly.yml`", "3 redundant"]]},
            "note": "Schedule burn is counted only on event=schedule same-head_sha runs.",
        }
    elif proof == "post_completion_waste_opt35":
        f["pattern"] = "OPT35"
        f["title"] = "Missing `fail-fast` on Non-Diagnostic Matrix Dimensions"
        f["workflow_file"] = ".github/workflows/ci.yml"
        f["affected_jobs"] = ["test"]
        f["fix_key"] = f"OPT35:{fid}"
        f["evidence"] = (
            "1 sampled failed run left shard sibling jobs running after the first "
            "failed shard; ~90 runner-min/mo of post-failure matrix compute")
        f["measured_signal"] = (
            "fail-fast:false shard matrix post-failure sibling compute "
            "(1 failed run; 15.0 sampled min; scale 6)")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("fail-fast:false shard matrix: first failed shard already makes "
                    "the run fail; sibling shard compute after that failure is "
                    "post-completion waste"),
        }
        f["measured_evidence"] = {
            "summary": "1 failed run had sibling shard compute after first failed shard",
            "table": {
                "headers": ["Matrix job", "First failed shard", "Post-failure min"],
                "rows": [["`test`", "test (1)", "15.0"]],
            },
            "note": ("Counts only shard-indexed fail-fast:false matrices; diagnostic "
                     "matrices are excluded. Post-failure minutes start after the "
                     "first failed shard completed."),
        }
        f["fix_recipe_anchor"] = "opt35--missing-fail-fast"
    elif proof == "post_completion_waste_opt64":
        f["pattern"] = "OPT64"
        f["title"] = "Repeated Workflow Attempts From Same Failing Job"
        f["workflow_file"] = ".github/workflows/ci.yml"
        f["affected_jobs"] = []
        f["fix_key"] = f"OPT64:{fid}"
        f["evidence"] = (
            "1 sampled run_attempt>1 workflow run had prior-attempt jobs present "
            "in filter=all but absent from filter=latest; the unique dominant "
            "failing job was test and it appeared again in the latest attempt.")
        f["measured_signal"] = (
            "run_attempt>1 prior-attempt job delta from filter=all minus "
            "filter=latest; dominant failing job `test` present in latest attempt "
            "(25.0 sampled prior-attempt min; scale 4)")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("run_attempt>1: `filter=all` exposes prior-attempt jobs, "
                    "`filter=latest` is the superseding latest attempt; the "
                    "dominant failing job `test` identifies the retry cause, so "
                    "prior-attempt compute is post-completion waste"),
        }
        f["measured_evidence"] = {
            "summary": "1 run_attempt>1 retry had prior attempt compute behind test",
            "table": {
                "headers": ["Run", "Latest attempt", "Dominant failing job",
                            "Prior attempt compute min"],
                "rows": [["`run-1`", "2", "`test`", "25.0"]],
            },
            "note": ("Measured from GitHub jobs API filter=all minus filter=latest "
                     "for workflow runs whose run_attempt > 1. The latest attempt "
                     "supersedes the prior attempt. Emitted only when a dominant "
                     "failing job appears again; ambiguous ties are withheld."),
        }
        f["rerun_dominant_job"] = "test"
        f["fix_recipe_anchor"] = "opt64--repeated-workflow-attempts-from-same-failing-job"
    elif proof == "post_completion_waste_opt57":
        f["pattern"] = "OPT57"
        f["title"] = "Missing `timeout-minutes` on Known-Flaky Integration Jobs"
        f["workflow_file"] = ".github/workflows/ci.yml"
        f["affected_jobs"] = ["integration"]
        f["fix_key"] = f"OPT57:{fid}"
        f["evidence"] = (
            "1 sampled failed/timed-out integration job occurrence burned near "
            "GitHub's 360 minute default timeout while successful samples had "
            "p99 20.0 min over 5 timed samples.")
        f["measured_signal"] = (
            "near-default timeout burn for `integration` "
            "(1 failed/timed-out occurrence; 330.0 sampled min above "
            "timeout-minutes 30; successful p99 1200.0s over 5 timed samples; scale 5)")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("failed/timed-out run: near-default timeout burn happens after "
                    "the job has exceeded a timeout-minutes value derived above "
                    "the same job's successful p99; that failed-run compute cannot "
                    "produce a green merge result"),
        }
        f["timeout_default_burn"] = {
            "kind": "opt57_timeout_default_burn",
            "job_key": "integration",
            "job_template": "Integration Tests",
            "default_timeout_minutes": 360,
            "near_default_threshold_s": 20520.0,
            "successful_duration_p99_s": 1200.0,
            "successful_duration_samples": 5,
            "recommended_timeout_minutes": 30,
            "sampled_timeout_occurrences": 1,
            "sampled_timeout_burn_min": 330.0,
            "sample_denominator": 6,
            "monthly_volume": 30,
            "scale": 5.0,
            "runner_min_saving": runner_min,
            "run_ids": ["timeout-1"],
            "samples": [{
                "run_id": "timeout-1",
                "job_name": "Integration Tests",
                "conclusion": "timed_out",
                "duration_s": 21600.0,
                "waste_s": 19800.0,
            }],
        }
        f["measured_evidence"] = {
            "summary": (
                "1 sampled failed/timed-out job burned near GitHub's 360 minute "
                "default timeout; timeout-minutes 30 is above p99."),
            "table": {
                "headers": ["Run", "Job", "Failed duration min",
                            "Successful p99 min", "Recommended timeout min",
                            "Default-timeout burn min"],
                "rows": [["`timeout-1`", "`Integration Tests`", "360.0",
                          "20.0", "30", "330.0"]],
            },
            "note": ("Counts only jobs without timeout-minutes whose failed/timed-out "
                     "duration reached GitHub's 360 minute default and whose p99 "
                     "successful samples support the recommendation."),
        }
        f["fix_recipe_anchor"] = (
            "opt57--missing-timeout-minutes-on-known-flaky-integration-jobs")
    return f


def test_tier2_verify_checks_pass_on_rendered_claims(tmp_path: Path):
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(tmp_path)
    checks = [
        vr.check_tier2_neutrality_derived(report, findings_path, report_path),
        vr.check_tier2_measured_basis(report, findings_path),
        vr.check_tier2_total_deoverlapped(report, findings_path, report_path),
        vr.check_tier2_headline_matches_stamp(report, findings_path, report_path),
        vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path),
    ]
    assert all(c.ok and not c.skipped for c in checks), [c for c in checks if not c.ok or c.skipped]


def test_no_rate_derived_dollars_passes_clean_report_fails_doctored_r_row(tmp_path: Path):
    # Pricing excised 2026-07-20: a clean rendered report carries no rate-derived
    # `$N`/USD token on its minutes surfaces, so `check_no_rate_derived_dollars`
    # passes. Doctoring a `$12/mo` into an R-row body (a visible, non-fenced line)
    # must fail the pin.
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)

    clean = vr.check_no_rate_derived_dollars(report, findings_path)
    assert clean.ok and not clean.skipped, clean

    doctored = report.replace("**After the gate.**",
                              "**After the gate.** $12/mo", 1)
    assert doctored != report, "fixture must contain the bottom-line to doctor"
    bad = vr.check_no_rate_derived_dollars(doctored, findings_path)
    assert not bad.ok
    assert "$12/mo" in bad.detail


def test_no_rate_derived_dollars_allows_dollars_inside_code_fences(tmp_path: Path):
    # A `$` inside a ``` code fence (agent prompts, shell echoes) is legitimate and
    # must NOT trip the pin — the sweep runs over `_visible_markdown_lines` only.
    vr = _load_verify_report()
    report = ("# report\n\n"
              "```sh\n$ some-cli --cost $12/mo\necho USD\n```\n\n"
              "Plain prose with no price.\n")
    chk = vr.check_no_rate_derived_dollars(report, None)
    assert chk.ok and not chk.skipped, chk


def test_no_rate_derived_dollars_is_wired_into_run_checks(tmp_path: Path):
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(tmp_path)
    checks = {c.name: c for c in vr.run_checks(report, report_path, findings_path, skill_repo=None)}
    name = "no rate-derived dollars on the minutes surfaces"
    assert name in checks
    assert checks[name].ok and not checks[name].skipped, checks[name]


def test_no_rate_derived_dollars_fails_spelled_out_figure():
    # Bypass (a): a spelled-out "N dollars"/"N cents" figure matches no `$`/USD
    # token, so pre-fix it slipped through. The regex now catches dollars/cents.
    vr = _load_verify_report()
    for prose in ("This wastes ~42 dollars per month at your rate.",
                  "That check burns 1200 cents/mo of runner spend."):
        bad = vr.check_no_rate_derived_dollars("# report\n\n" + prose + "\n", None)
        assert not bad.ok, prose


def test_no_rate_derived_dollars_fails_indented_dollar_figure():
    # Bypass (b): a `$`-figure on a 4-space-indented line was dropped by the
    # visible-lines filter (indentation read as a code block). The sweep now
    # scans indented lines too (fences still protect legitimate `$`).
    vr = _load_verify_report()
    report = "# report\n\nSee the spine:\n\n    R-3 saves $4200/mo after the gate.\n"
    bad = vr.check_no_rate_derived_dollars(report, None)
    assert not bad.ok
    assert "$4200/mo" in bad.detail


def test_no_rate_derived_dollars_allows_sanctioned_pricing_sentence():
    # The one sanctioned methodology sentence legitimately ends in "dollars";
    # its exact phrase is stripped before matching, so it PASSES — while any
    # other dollars token on a different line still fails.
    vr = _load_verify_report()
    ok = vr.check_no_rate_derived_dollars(
        "# report\n\nAll figures are runner-minutes; multiply by your runner's "
        "per-minute rate to get dollars.\n", None)
    assert ok.ok and not ok.skipped, ok


def test_no_rate_derived_dollars_passes_normal_minutes_only_report():
    # A normal minutes-only report carries no `$`/USD/dollars/cents token on any
    # surface, indented or not — it must PASS.
    vr = _load_verify_report()
    report = ("# report\n\nThe gate waits 12 min on the slowest check; the job "
              "spends 340 runner-minutes per month.\n\n    build     340 min/mo\n")
    ok = vr.check_no_rate_derived_dollars(report, None)
    assert ok.ok and not ok.skipped, ok


def test_tier2_source_check_fails_without_render_ready_cost_spine(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, _backed_findings_path = _tier2_artifacts(tmp_path)
    unbacked = _tier2_doc_for_verify()
    unbacked.pop("runner_minute_spine")
    findings_path = tmp_path / "unbacked-findings.json"
    findings_path.write_text(json.dumps(unbacked, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)

    assert not chk.ok
    assert "rendered Tier-2 R-rows but no source-backed eligible findings" in chk.detail


def test_tier2_source_check_fails_rendered_rows_without_tier2_stamps(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, _good_findings_path = _tier2_artifacts(tmp_path)
    bare = _tier2_doc_for_verify()
    for key in ("sizing_basis", "tier2_neutrality", "billing_class", "usd_saving_per_month"):
        bare["findings"][0].pop(key, None)
    findings_path = tmp_path / "bare-findings.json"
    findings_path.write_text(json.dumps(bare, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)

    assert not chk.ok
    assert "fail closed instead of compat SKIP" in chk.detail


def test_tier2_renderer_suppresses_unbacked_savings_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc.pop("runner_minute_spine")
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and chk.skipped, chk


def test_tier2_verifier_ignores_spine_without_totals_like_renderer(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["runner_minute_spine"].pop("totals")
    doc["_preserve_runner_minute_spine"] = True
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    checks = [
        vr.check_tier2_neutrality_derived(report, findings_path, report_path),
        vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path),
    ]
    assert all(c.ok and c.skipped for c in checks), checks


def test_tier2_renderer_suppresses_non_positive_savings_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"][0]["runner_min_saving"] = -10.0
    doc["findings"][0]["usd_saving_per_month"] = -0.06
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and chk.skipped, chk


def test_tier2_source_check_fails_when_source_line_is_tampered(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)
    tampered = report.replace("150.000 raw min/mo", "999.000 raw min/mo", 1)

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(tampered, findings_path)

    assert not chk.ok
    assert "source-block line does not match cost spine" in chk.detail


def test_tier2_source_check_normalizes_source_line_like_renderer(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    wf = ".github/workflows/ci | slow.yml"
    doc["findings"][0]["workflow_file"] = wf
    row = _tier2_spine_row(doc, workflow=wf, job="cleanup",
                           raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(doc, [row])
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))

    assert ".github/workflows/ci \\| slow.yml" in source
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_check_requires_one_exact_source_line(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))
    duplicate = report.replace(source, source.replace("150.000", "999.000") + "\n" + source, 1)

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(duplicate, findings_path)

    assert not chk.ok
    assert "expected exactly one Source block line" in chk.detail


def test_tier2_source_check_rejects_duplicate_marker_ids(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)
    duplicate = (
        report
        + "\n<!-- ci-speedup:tier2-finding id=f-promoted pattern=OPT46 -->\n"
        + "## 🟢 Runner saving 2: `ci.yml` - 999 min/mo ($9.00/mo)\n"
        + "- **Source block:** forged source line.\n"
    )

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(duplicate, findings_path)

    assert not chk.ok
    assert "duplicate Tier-2 marker id" in chk.detail


def test_tier2_source_check_ignores_expected_text_outside_source_line(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))
    tampered = report.replace(source, source.replace("150.000", "999.000"), 1)
    tampered = tampered.replace("Cancellation cause is inference.",
                                f"Cancellation cause is inference. {source}", 1)

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(tampered, findings_path)

    assert not chk.ok
    assert "source-block line does not match cost spine" in chk.detail


def test_tier2_source_check_fails_when_savings_exceed_source_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    row = _tier2_spine_row(doc, raw=50.0, billable=60.0, weighted=60.0, usd=0.36)
    _set_tier2_spine_rows(doc, [row])
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report

    forged_dir = tmp_path / "forged"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir, _tier2_doc_for_verify())

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)

    assert not chk.ok
    assert "rendered Tier-2 R-rows but no source-backed eligible findings" in chk.detail


def test_tier2_source_check_uses_billable_bound_for_rounding_waste(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-round", 20.0, proof="below_cluster_floor_opt65")
    ]
    rows = [
        _tier2_spine_row(doc, job="tiny (1)", raw=2.0, billable=10.0, weighted=10.0, usd=0.06),
        _tier2_spine_row(doc, job="tiny (2)", raw=2.0, billable=10.0, weighted=10.0, usd=0.06),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" in report
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_check_fails_when_rounding_waste_exceeds_billable_source(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-round", 20.0, proof="below_cluster_floor_opt65")
    ]
    rows = [
        _tier2_spine_row(doc, job="tiny (1)", raw=30.0, billable=5.0, weighted=5.0, usd=0.03),
        _tier2_spine_row(doc, job="tiny (2)", raw=30.0, billable=5.0, weighted=5.0, usd=0.03),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-rounding"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir,
        _tier2_doc_for_verify() | {"findings": [
            _tier2_promoted_finding("f-round", 20.0, proof="below_cluster_floor_opt65")
        ]})
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_source_check_fails_when_row_savings_label_is_tampered(tmp_path: Path):
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(tmp_path)
    # Scope the tamper to the R-row region (heading onward) so the section
    # lead's identical "120 min/mo" strings stay intact — the fault stays
    # isolated to the row body this check binds.
    head, sep, row_region = report.partition("## 🟢 Runner saving 1: ")
    assert sep, "synthetic Tier-2 report must contain the Runner saving 1 heading"
    tampered = head + sep + row_region.replace("120 min/mo", "999 min/mo")

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(tampered, findings_path)

    assert not chk.ok
    assert "missing savings label" in chk.detail


def test_tier2_source_binding_filters_workflow_and_raw_job(tmp_path: Path):
    # Dollars/SKU excised 2026-07-20: the source binding now narrows by workflow +
    # job only (no SKU dimension), so both `build` rows of ci.yml — across runner
    # labels — bind, while `deploy` (other job) and other.yml (other workflow) stay
    # out. The rendered source line carries minutes only, no `$` tail.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"][0]["affected_jobs"] = ["build"]
    rows = [
        _tier2_spine_row(doc, job="build", raw=150.0, billable=200.0, usd=1.2),
        _tier2_spine_row(doc, job="deploy", raw=900.0, billable=900.0, usd=5.4),
        _tier2_spine_row(doc, job="build", sku="linux_4_core", runner="linux_4_core",
                         raw=800.0, billable=800.0, weighted=1600.0, usd=9.6),
        _tier2_spine_row(doc, workflow=".github/workflows/other.yml", job="build",
                         raw=700.0, billable=700.0, usd=4.2),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))

    assert "matched 2 rows" in source
    assert "950.000 raw min/mo, 1000.000 billable min/mo." in source
    assert all(decoy not in source for decoy in ("900.000", "700.000", "$"))
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_binding_preserves_scoped_and_case_sensitive_jobs(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"][0]["affected_jobs"] = ["@pkg-a/test"]
    rows = [
        _tier2_spine_row(doc, job="@pkg-a/test", raw=150.0, billable=200.0, usd=1.2),
        _tier2_spine_row(doc, job="@pkg-b/test", raw=900.0, billable=900.0, usd=5.4),
        _tier2_spine_row(doc, job="@pkg-a/Test", raw=800.0, billable=800.0, usd=4.8),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))

    assert "matched 1 row" in source
    assert "150.000 raw min/mo, 200.000 billable min/mo." in source
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_binding_matches_matrix_rows_by_raw_base(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["affected_jobs"] = ["test"]
    f["runner_min_saving"] = 100.0
    f["usd_saving_per_month"] = 0.6
    rows = [
        _tier2_spine_row(doc, job="test (3.12)", raw=60.0, billable=80.0, usd=0.48),
        _tier2_spine_row(doc, job="test (3.13)", raw=70.0, billable=90.0, usd=0.54),
        _tier2_spine_row(doc, job="test-extra", raw=900.0, billable=900.0, usd=5.4),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))

    assert "matched 2 rows" in source
    assert "130.000 raw min/mo, 170.000 billable min/mo." in source
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_binding_prefers_exact_job_over_matrix_base_decoys(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["affected_jobs"] = ["cleanup"]
    rows = [
        _tier2_spine_row(doc, job="cleanup", raw=50.0, billable=60.0,
                         weighted=60.0, usd=0.36),
        _tier2_spine_row(doc, job="cleanup (decoy)", raw=900.0, billable=900.0,
                         weighted=900.0, usd=5.4),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report

    forged_dir = tmp_path / "forged-exact-first"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir, _tier2_doc_for_verify())

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)

    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_source_binding_filters_cost_spine_dimensions(tmp_path: Path):
    vr = _load_verify_report()
    opt64 = _tier2_doc_for_verify()
    opt64["findings"] = [
        _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    ]
    latest_row = _tier2_spine_row(opt64, job="test", status="success", attempt="latest",
                                  raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(opt64, [latest_row])
    latest_dir = tmp_path / "latest"
    latest_dir.mkdir()
    latest_report, _report_path, latest_findings = _tier2_artifacts(latest_dir, opt64)
    assert "<!-- ci-speedup:tier2-finding" not in latest_report
    forged_prior_dir = tmp_path / "forged-prior"
    forged_prior_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_prior_dir,
        _tier2_doc_for_verify() | {"findings": [
            _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
        ]})
    latest_chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, latest_findings)
    assert not latest_chk.ok
    assert "no source-backed eligible findings" in latest_chk.detail

    prior = _tier2_doc_for_verify()
    prior["findings"] = [
        _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    ]
    prior_row = _tier2_spine_row(prior, job="test", status="all-status", attempt="prior",
                                 raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(prior, [prior_row])
    prior_dir = tmp_path / "prior"
    prior_dir.mkdir()
    prior_report, _prior_report_path, prior_findings = _tier2_artifacts(prior_dir, prior)
    assert "<!-- ci-speedup:tier2-finding" in prior_report
    prior_chk = vr.check_tier2_savings_rows_backed_by_cost_spine(prior_report, prior_findings)
    assert prior_chk.ok and not prior_chk.skipped, prior_chk


def test_tier2_source_binding_requires_opt64_dominant_job(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    ]
    decoy_prior = _tier2_spine_row(doc, job="deploy", status="all-status", attempt="prior",
                                   raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(doc, [decoy_prior])
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-opt64-dominant"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir,
        _tier2_doc_for_verify() | {"findings": [
            _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
        ]})
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_source_binding_rejects_required_filter_override(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    finding = _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    finding["runner_minute_source_filter"] = {
        "status_filter": "success",
        "attempt_filter": "latest",
        "volume_filter": "all-status",
    }
    doc["findings"] = [finding]
    latest_row = _tier2_spine_row(doc, job="test", status="success", attempt="latest",
                                  raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(doc, [latest_row])
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-opt64-override"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir,
        _tier2_doc_for_verify() | {"findings": [
            _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
        ]})
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_source_binding_uses_all_events_envelope_for_schedule(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-schedule", 90.0, proof="non_pr_event")
    ]
    all_events = _tier2_spine_row(doc, workflow=".github/workflows/nightly.yml",
                                  job="nightly", event="all-events",
                                  raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(doc, [all_events])
    all_events_dir = tmp_path / "all-events"
    all_events_dir.mkdir()
    report, _report_path, findings_path = _tier2_artifacts(all_events_dir, doc)
    source = next(line for line in report.splitlines() if line.lstrip("- ").startswith("**Source block:**"))

    assert "<!-- ci-speedup:tier2-finding" in report
    assert "matched 1 row" in source
    assert "150.000 raw min/mo, 200.000 billable min/mo." in source
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_binding_honors_explicit_event_filter_when_stamped(tmp_path: Path):
    vr = _load_verify_report()
    schedule = _tier2_doc_for_verify()
    finding = _tier2_promoted_finding("f-schedule", 90.0, proof="non_pr_event")
    finding["runner_minute_source_filter"] = {"event_scope": "schedule"}
    schedule["findings"] = [finding]
    schedule_row = _tier2_spine_row(schedule, workflow=".github/workflows/nightly.yml",
                                    job="nightly", event="schedule",
                                    raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(schedule, [schedule_row])
    schedule_dir = tmp_path / "schedule"
    schedule_dir.mkdir()
    ok_report, _ok_report_path, ok_findings = _tier2_artifacts(schedule_dir, schedule)
    assert "<!-- ci-speedup:tier2-finding" in ok_report
    ok_chk = vr.check_tier2_savings_rows_backed_by_cost_spine(ok_report, ok_findings)
    assert ok_chk.ok and not ok_chk.skipped, ok_chk


def test_tier2_source_binding_accepts_latest_success_envelope_for_failed_run_detectors(
    tmp_path: Path,
):
    vr = _load_verify_report()
    for proof in ("post_completion_waste_opt35", "post_completion_waste_opt57"):
        doc = _tier2_doc_for_verify()
        doc["findings"] = [_tier2_promoted_finding(f"f-{proof}", 90.0, proof=proof)]
        rows = _source_rows_for_finding(doc, doc["findings"][0])
        assert all(row["status_filter"] == "success" for row in rows)
        assert all(row["attempt_filter"] == "latest" for row in rows)
        _set_tier2_spine_rows(doc, rows)
        case_dir = tmp_path / proof
        case_dir.mkdir()
        report, _report_path, findings_path = _tier2_artifacts(case_dir, doc)
        assert "<!-- ci-speedup:tier2-finding" in report
        chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
        assert chk.ok and not chk.skipped, (proof, chk)


def test_tier2_source_binding_rejects_prior_attempt_envelope_for_non_opt64(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    row = _tier2_spine_row(doc, job="cleanup", status="all-status", attempt="prior",
                           raw=150.0, billable=200.0, usd=1.2)
    _set_tier2_spine_rows(doc, [row])
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report

    forged_dir = tmp_path / "forged-non-opt64-prior"
    forged_dir.mkdir()
    forged_report, _forged_report_path, _forged_findings_path = _tier2_artifacts(
        forged_dir, _tier2_doc_for_verify())

    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)

    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


# --- PR-S2 contract: OPT64 wide-attribution source binding (R1 WIDE) ---------
# An OPT64 finding credits the WHOLE retried run's prior-attempt compute across
# ALL SKUs (R1: wide attribution), so its source binding is every prior-attempt
# row of its workflow — the dominant job's own row alone can never cover a
# whole-run claim. Sibling OPT64 findings on one workflow share that ONE
# prior-row cover, guarded by an explicit no-double-count rule: if the group's
# combined claim exceeds the shared cover, NONE of the siblings is
# source-backed (order-independent fail-close, no cherry-picking).


def _opt64_finding(fid: str, saving: float, dominant: str,
                   line: int | None = None) -> dict:
    f = _tier2_promoted_finding(fid, saving, proof="post_completion_waste_opt64")
    f["rerun_dominant_job"] = dominant
    if line is not None:
        # Sibling OPT64 fixtures need distinct lines: _tier2_promoted_finding
        # derives `line` from the fid's DIGITS, and every "f-opt64x" id shares
        # "64" — identical lines would trip the renderer's location dedupe and
        # silently drop a sibling.
        f["line"] = line
    return f


def test_tier2_opt64_wide_binding_binds_all_prior_rows_across_runners(tmp_path: Path):
    # Cell: prior rows exist across runner labels; the dominant job's prior row is
    # present; the finding's whole-run claim is covered only by the FULL set.
    # (Dollars/SKU excised 2026-07-20: the source line names minutes only, no SKU
    # spread and no `$` tail.)
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _opt64_finding("f-opt64", 100.0, "test")
    doc["findings"] = [f]
    rows = [
        _tier2_spine_row(doc, job="test", sku="windows_2_core",
                         runner="windows-latest", status="all-status",
                         attempt="prior", raw=30.0, billable=40.0),
        _tier2_spine_row(doc, job="build (3.12, ubuntu)", sku="linux_2_core",
                         status="all-status", attempt="prior", raw=90.0,
                         billable=110.0),
        _tier2_spine_row(doc, job="build (3.12, macos)", sku="macos_standard",
                         runner="macos-latest", status="all-status",
                         attempt="prior", raw=60.0, billable=80.0),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" in report
    source = next(line for line in report.splitlines()
                  if line.lstrip("- ").startswith("**Source block:**"))
    assert "matched 3 prior-attempt rows for `.github/workflows/ci.yml`" in source
    assert "180.000 raw min/mo, 230.000 billable min/mo." in source
    assert "$" not in source
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_opt64_wide_binding_excludes_latest_rows_from_cover(tmp_path: Path):
    # Cell: the wide cover is prior-attempt rows ONLY — a big latest-attempt row
    # must never pad an OPT64 whole-run retry claim.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [_opt64_finding("f-opt64", 100.0, "test")]
    rows = [
        _tier2_spine_row(doc, job="test", status="all-status", attempt="prior",
                         raw=30.0, billable=40.0, usd=0.24),
        _tier2_spine_row(doc, job="test", status="success", attempt="latest",
                         raw=500.0, billable=600.0, usd=3.6),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-opt64-latest-pad"
    forged_dir.mkdir()
    forged_doc = _tier2_doc_for_verify()
    forged_doc["findings"] = [_opt64_finding("f-opt64", 100.0, "test")]
    forged_report, _fr, _ff = _tier2_artifacts(forged_dir, forged_doc)
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_opt64_wide_binding_requires_dominant_job_prior_row_among_many(
    tmp_path: Path,
):
    # Cell: a rich multi-SKU prior set that does NOT contain the finding's
    # dominant failing job is no binding at all — the flaky job's own retries
    # must be visible in the spine before its whole-run claim can lean on it.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [_opt64_finding("f-opt64", 100.0, "test")]
    rows = [
        _tier2_spine_row(doc, job="other", status="all-status", attempt="prior",
                         raw=300.0, billable=350.0, usd=2.1),
        _tier2_spine_row(doc, job="other2", sku="windows_2_core",
                         runner="windows-latest", status="all-status",
                         attempt="prior", raw=300.0, billable=350.0, usd=3.5),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-opt64-no-dominant"
    forged_dir.mkdir()
    forged_doc = _tier2_doc_for_verify()
    forged_doc["findings"] = [_opt64_finding("f-opt64", 100.0, "test")]
    forged_report, _fr, _ff = _tier2_artifacts(forged_dir, forged_doc)
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_opt64_group_guard_blocks_sibling_over_claim(tmp_path: Path):
    # Cell (the R1 no-double-count guard): each sibling alone fits under the
    # shared prior-row cover, but their combined claim exceeds it — NONE may
    # render as source-backed, and a forged report that renders them fails.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f1 = _opt64_finding("f-opt64a", 100.0, "test", line=21)
    f2 = _opt64_finding("f-opt64b", 100.0, "test2", line=22)
    doc["findings"] = [f1, f2]
    rows = [
        _tier2_spine_row(doc, job="test", status="all-status", attempt="prior",
                         raw=80.0, billable=100.0, usd=0.6),
        _tier2_spine_row(doc, job="test2", status="all-status", attempt="prior",
                         raw=70.0, billable=90.0, usd=0.54),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report

    forged_dir = tmp_path / "forged-opt64-group"
    forged_dir.mkdir()
    forged_doc = _tier2_doc_for_verify()
    forged_doc["findings"] = [
        _opt64_finding("f-opt64a", 100.0, "test", line=21),
        _opt64_finding("f-opt64b", 100.0, "test2", line=22),
    ]
    forged_rows = [
        _tier2_spine_row(forged_doc, job="test", status="all-status",
                         attempt="prior", raw=300.0, billable=350.0, usd=2.1),
        _tier2_spine_row(forged_doc, job="test2", status="all-status",
                         attempt="prior", raw=300.0, billable=350.0, usd=2.1),
    ]
    _set_tier2_spine_rows(forged_doc, forged_rows)
    forged_report, _fr, _ff = _tier2_artifacts(forged_dir, forged_doc)
    assert "<!-- ci-speedup:tier2-finding" in forged_report
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_opt64_wide_gate_ignores_loose_affected_jobs(tmp_path: Path):
    # Review fold-in (S2-3): the presence gate tests the DOMINANT job itself —
    # a stale affected_jobs entry that happens to match a prior row must not
    # satisfy it when the dominant job has no prior row.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _opt64_finding("f-opt64", 50.0, "test")
    f["affected_jobs"] = ["other"]  # matches a prior row; dominant does not
    doc["findings"] = [f]
    rows = [
        _tier2_spine_row(doc, job="other", status="all-status", attempt="prior",
                         raw=300.0, billable=350.0, usd=2.1),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    assert "<!-- ci-speedup:tier2-finding" not in report
    forged_dir = tmp_path / "forged-opt64-loose-jobs"
    forged_dir.mkdir()
    forged_doc = _tier2_doc_for_verify()
    forged_doc["findings"] = [_opt64_finding("f-opt64", 50.0, "test")]
    forged_report, _fr, _ff = _tier2_artifacts(forged_dir, forged_doc)
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(forged_report, findings_path)
    assert not chk.ok
    assert "no source-backed eligible findings" in chk.detail


def test_tier2_opt64_group_guard_passes_exact_partition(tmp_path: Path):
    # Cell: siblings that exactly partition the shared cover (the real
    # psf/requests shape — Σ savings == Σ prior raw) both promote.
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f1 = _opt64_finding("f-opt64a", 90.0, "test", line=21)
    f2 = _opt64_finding("f-opt64b", 60.0, "test2", line=22)
    doc["findings"] = [f1, f2]
    rows = [
        _tier2_spine_row(doc, job="test", status="all-status", attempt="prior",
                         raw=80.0, billable=100.0, usd=0.6),
        _tier2_spine_row(doc, job="test2", status="all-status", attempt="prior",
                         raw=70.0, billable=90.0, usd=0.54),
    ]
    _set_tier2_spine_rows(doc, rows)
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)

    markers = [line for line in report.splitlines()
               if "<!-- ci-speedup:tier2-finding" in line]
    assert len(markers) == 2
    chk = vr.check_tier2_savings_rows_backed_by_cost_spine(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_source_check_is_wired_into_run_checks(tmp_path: Path):
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(tmp_path)
    checks = {c.name: c for c in vr.run_checks(report, report_path, findings_path, skill_repo=None)}
    name = "Tier-2 savings rows are backed by runner-minute cost spine"

    assert name in checks
    assert checks[name].ok and not checks[name].skipped, checks[name]

    tampered = report.replace("150.000 raw min/mo", "999.000 raw min/mo", 1)
    red = {c.name: c for c in vr.run_checks(tampered, report_path, findings_path, skill_repo=None)}
    assert not red[name].ok


def test_runner_minute_spine_contract_accepts_valid_source_block(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(_runner_minute_spine_doc(), indent=2) + "\n",
                             encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_accepts_complete_source_block(tmp_path: Path):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_complete_source_block_not_render_ready(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["render_ready"] = False
    spine["render_blocker"] = "renderer skipped despite complete coverage"
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "complete_repo_coverage requires render_ready true" in chk.detail


def test_runner_minute_spine_contract_rejects_inconsistent_complete_coverage(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    doc["per_workflow_monthly_volume"][".github/workflows/docs.yml"] = 10
    spine["complete_repo_coverage"] = True
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 2,
        "row_workflow_count": 1,
        "omitted_workflows": [".github/workflows/docs.yml"],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "complete_repo_coverage" in chk.detail


def test_runner_minute_spine_contract_rejects_triaged_included_without_rows(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    doc["per_workflow_monthly_volume"][".github/workflows/docs.yml"] = 10
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 2,
        "row_workflow_count": 1,
        "omitted_workflows": [".github/workflows/docs.yml"],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [".github/workflows/docs.yml"],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "triaged_workflows_included" in chk.detail


def test_runner_minute_spine_contract_rejects_non_boolean_complete_coverage(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = "true"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "complete_repo_coverage must be boolean" in chk.detail


@pytest.mark.parametrize("field", [
    "workflow_count",
    "row_workflow_count",
    "job_fetch_failures",
])
def test_runner_minute_spine_contract_rejects_fractional_workflow_coverage_counts(
    tmp_path: Path,
    field: str,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = True
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    spine["workflow_coverage"][field] = 1.5
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert f"workflow_coverage.{field}" in chk.detail


def test_runner_minute_spine_contract_rejects_malformed_workflow_coverage_lists(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": ".github/workflows/docs.yml",
        "unknown_volume_workflows": ".github/workflows/missing.yml",
        "triaged_workflows_included": ".github/workflows/lint.yml",
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "omitted_workflows must be a list" in chk.detail
    assert "unknown_volume_workflows must be a list" in chk.detail
    assert "triaged_workflows_included must be a list" in chk.detail


def test_runner_minute_spine_contract_rejects_understated_workflow_denominator(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    doc["per_workflow_monthly_volume"][".github/workflows/docs.yml"] = 10
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = True
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "workflow_count does not match per_workflow_monthly_volume" in chk.detail
    assert "omitted_workflows does not match rows and volume" in chk.detail


def test_runner_minute_spine_contract_rejects_missing_finding_backed_workflow_volume(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    doc["findings"] = [{
        "id": "f2",
        "pattern": "OPT32",
        "workflow_file": ".github/workflows/docs.yml",
    }]
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert not chk.ok
    assert "per_workflow_monthly_volume missing finding-backed workflow" in chk.detail


def test_runner_minute_spine_contract_treats_uppercase_workflow_extension_as_file_backed(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    doc["findings"] = [{
        "id": "f2",
        "pattern": "OPT32",
        "workflow_file": ".github/workflows/DOCS.YML",
    }]
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert not chk.ok
    assert "per_workflow_monthly_volume missing finding-backed workflow" in chk.detail


def test_runner_minute_spine_contract_rejects_omitted_workflow_with_rows(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [".github/workflows/ci.yml"],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "omitted_workflows contains workflow with rows" in chk.detail


def test_runner_minute_spine_contract_rejects_understated_row_workflow_count(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 0,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "row_workflow_count does not match row workflows" in chk.detail


def test_runner_minute_spine_contract_rejects_data_source_fetch_failure_drift(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    doc["data_sources"] = {"cost_spine_job_fetch_failures": 1}
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "does not match data_sources" in chk.detail


def test_runner_minute_spine_contract_requires_data_source_fetch_failure_count(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    doc.pop("data_sources")
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert not chk.ok
    assert "data_sources.cost_spine_job_fetch_failures is required" in chk.detail


def test_runner_minute_spine_contract_rejects_missing_unknown_volume_disclosure(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    doc["per_workflow_monthly_volume"][".github/workflows/docs.yml"] = None
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = True
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "unknown_volume_workflows does not match volume gaps" in chk.detail


def test_runner_minute_spine_contract_accepts_negative_volume_as_unknown(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    unknown = ".github/workflows/negative.yml"
    doc["per_workflow_monthly_volume"][unknown] = -1
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["complete_repo_coverage"] = False
    spine["render_ready"] = False
    spine["render_blocker"] = "negative workflow volume keeps coverage incomplete"
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [unknown],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    doc["data_sources"] = {"cost_spine_job_fetch_failures": 0}
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert chk.ok, chk.detail


def test_runner_minute_spine_contract_rejects_render_without_source(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({"findings": []}, indent=2) + "\n",
                             encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        "<!-- ci-speedup:runner-minute-spine -->\n", findings_path)

    assert not chk.ok
    assert "without runner_minute_spine" in chk.detail


def test_runner_minute_spine_contract_rejects_render_without_readable_findings(tmp_path: Path):
    vr = _load_verify_report()

    chk = vr.check_runner_minute_spine_contract(
        "<!-- ci-speedup:runner-minute-spine -->\n", tmp_path / "missing.json")

    assert not chk.ok
    assert "without readable findings JSON" in chk.detail


def test_runner_minute_spine_contract_rejects_malformed_source_block(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps({"runner_minute_spine": [], "findings": []}, indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "not an object" in chk.detail


def test_runner_minute_spine_contract_rejects_empty_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    doc["runner_minute_spine"]["rows"] = []
    doc["runner_minute_spine"]["totals"] = {
        "row_count": 0,
        "raw_compute_runner_min_per_month": 0.0,
        "billable_equiv_min_per_month": 0.0,
        "sku_weighted_billable_min_per_month": 0.0,
        "usd_per_month": 0.0,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert not chk.ok
    assert "rows must be non-empty" in chk.detail


def test_runner_minute_spine_contract_accepts_empty_blocked_coverage(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    wf = ".github/workflows/ci.yml"
    spine = doc["runner_minute_spine"]
    spine["coverage_scope"] = "sampled_workflows_in_play_with_job_data"
    spine["rows"] = []
    spine["latest_attempt_row_count"] = 0
    spine["prior_attempt_row_count"] = 0
    spine["totals"] = {
        "row_count": 0,
        "raw_compute_runner_min_per_month": 0.0,
        "billable_equiv_min_per_month": 0.0,
        "sku_weighted_billable_min_per_month": 0.0,
        "usd_per_month": 0.0,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    spine["workflow_coverage"] = {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 0,
        "omitted_workflows": [wf],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }
    doc["data_sources"] = {"cost_spine_job_fetch_failures": 0}
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert chk.ok, chk.detail


def test_runner_minute_spine_contract_rejects_empty_sampled_only_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    spine = doc["runner_minute_spine"]
    spine["rows"] = []
    spine["latest_attempt_row_count"] = 0
    spine["prior_attempt_row_count"] = 0
    spine["totals"] = {
        "row_count": 0,
        "raw_compute_runner_min_per_month": 0.0,
        "billable_equiv_min_per_month": 0.0,
        "sku_weighted_billable_min_per_month": 0.0,
        "usd_per_month": 0.0,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "explicit coverage is blocked" in chk.detail


def test_runner_minute_spine_contract_rederives_billable_totals(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    doc["runner_minute_spine"]["rows"][0]["billable_equiv_min_per_month"] = 149.0
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "billable_equiv_min_per_month" in chk.detail


def test_runner_minute_spine_contract_rejects_understated_billable_mean(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    row = doc["runner_minute_spine"]["rows"][0]
    row["mean_sampled_compute_seconds"] = 61.0
    row["mean_sampled_billable_equiv_minutes"] = 1.0
    row["raw_compute_runner_min_per_month"] = 101.667
    row["billable_equiv_min_per_month"] = 100.0
    row["sku_weighted_billable_min_per_month"] = 100.0
    row["usd_per_month"] = 0.6
    doc["runner_minute_spine"]["totals"]["raw_compute_runner_min_per_month"] = 101.667
    doc["runner_minute_spine"]["totals"]["billable_equiv_min_per_month"] = 100.0
    doc["runner_minute_spine"]["totals"]["sku_weighted_billable_min_per_month"] = 100.0
    doc["runner_minute_spine"]["totals"]["usd_per_month"] = 0.6
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "duration lower bound" in chk.detail


def test_runner_minute_spine_contract_accepts_non_exact_rounded_mean(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    row = doc["runner_minute_spine"]["rows"][0]
    row["sampled_workflow_run_count"] = 3
    row["sampled_job_occurrence_count"] = 3
    row["sample_window_end"] = "2026-06-03T00:00:00Z"
    row["mean_sampled_compute_seconds"] = 21.0
    row["mean_sampled_billable_equiv_minutes"] = 1.333
    row["raw_compute_runner_min_per_month"] = 35.0
    row["billable_equiv_min_per_month"] = 133.3
    row["sku_weighted_billable_min_per_month"] = 133.3
    row["usd_per_month"] = 0.8
    doc["runner_minute_spine"]["totals"]["raw_compute_runner_min_per_month"] = 35.0
    doc["runner_minute_spine"]["totals"]["billable_equiv_min_per_month"] = 133.3
    doc["runner_minute_spine"]["totals"]["sku_weighted_billable_min_per_month"] = 133.3
    doc["runner_minute_spine"]["totals"]["usd_per_month"] = 0.8
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_accepts_prior_attempt_rows(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    prior = copy.deepcopy(doc["runner_minute_spine"]["rows"][0])
    prior["attempt_filter"] = "prior"
    prior["status_filter"] = "all-status"
    prior["sampled_workflow_run_count"] = 10
    prior["sampled_job_occurrence_count"] = 1
    prior["sampled_positive_duration_occurrence_count"] = 1
    prior["occurrence_fraction"] = 0.1
    prior["effective_monthly_job_volume"] = 10.0
    prior["raw_compute_runner_min_per_month"] = 10.0
    prior["billable_equiv_min_per_month"] = 15.0
    prior["sku_weighted_billable_min_per_month"] = 15.0
    prior["usd_per_month"] = 0.09
    doc["runner_minute_spine"]["rows"].append(prior)
    doc["runner_minute_spine"]["rows"][0]["share_of_all_row_total"] = 0.909
    doc["runner_minute_spine"]["rows"][1]["share_of_all_row_total"] = 0.091
    doc["runner_minute_spine"]["prior_attempt_row_count"] = 1
    doc["runner_minute_spine"]["prior_attempts_included"] = True  # derived: count > 0
    doc["runner_minute_spine"]["totals"] = {
        "row_count": 2,
        "raw_compute_runner_min_per_month": 110.0,
        "billable_equiv_min_per_month": 165.0,
        "sku_weighted_billable_min_per_month": 165.0,
        "usd_per_month": 0.99,
        "percentage_denominator": "all_rows_billable_equiv_min_per_month",
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_prior_row_with_success_filter(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    row = doc["runner_minute_spine"]["rows"][0]
    row["attempt_filter"] = "prior"
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "prior rows must use status_filter" in chk.detail


def test_runner_minute_spine_contract_rejects_tampered_share(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    doc["runner_minute_spine"]["rows"][0]["share_of_all_row_total"] = 0.99
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "share_of_all_row_total" in chk.detail


def test_runner_minute_spine_contract_requires_sample_window(tmp_path: Path):
    vr = _load_verify_report()
    doc = _runner_minute_spine_doc()
    doc["runner_minute_spine"]["rows"][0].pop("sample_window_start")
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "sample window" in chk.detail


def test_runner_minute_spine_contract_blocks_visible_render_until_ready(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(_runner_minute_spine_doc(), indent=2) + "\n",
                             encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        "<!-- ci-speedup:runner-minute-spine -->\n", findings_path)

    assert not chk.ok
    assert "render_ready" in chk.detail


def test_runner_minute_spine_contract_rejects_source_only_render_ready(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract("# report\n", findings_path)

    assert not chk.ok
    assert "render_ready must be false" in chk.detail


def test_runner_minute_spine_contract_accepts_rendered_table_when_ready(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(), findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_total_row_before_data(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = """# report

<!-- ci-speedup:runner-minute-spine -->
| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| Total |  |  |  |  |  |  | 100.000 | 150.000 | 100.000% |
| `.github/workflows/ci.yml` | `build` | `ubuntu-latest` | all-events | success | latest | all-status | 100.000 | 150.000 | 100.000% |
"""

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "Total row must be final" in chk.detail


def test_runner_minute_spine_contract_rejects_total_row_metadata_cells(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = _rendered_runner_minute_spine_report().replace(
        "| Total |  |  |  |  |  |  |",
        "| Total | build | ubuntu-latest | all-events | success | latest | all-status |")

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "Total row has non-empty metadata cells" in chk.detail


def test_runner_minute_spine_contract_accepts_escaped_pipe_in_rendered_identity(tmp_path: Path):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    doc["runner_minute_spine"]["rows"][0]["job_name"] = "build | lint"
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(job="build \\| lint"), findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_unmarked_visible_table(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(_runner_minute_spine_doc(), indent=2) + "\n",
                             encoding="utf-8")
    report = _rendered_runner_minute_spine_report().replace(
        "<!-- ci-speedup:runner-minute-spine -->\n", "")

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "missing the runner-minute-spine marker" in chk.detail


def test_runner_minute_spine_contract_rejects_table_hidden_inside_marker_comment(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    table = _rendered_runner_minute_spine_report().split(
        "<!-- ci-speedup:runner-minute-spine -->\n", 1)[1]
    report = f"# report\n<!-- ci-speedup:runner-minute-spine\n{table}-->\n"

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "no visible markdown table" in chk.detail


def test_runner_minute_spine_contract_rejects_table_hidden_inside_code_fence(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    table = _rendered_runner_minute_spine_report().split(
        "<!-- ci-speedup:runner-minute-spine -->\n", 1)[1]
    report = f"# report\n<!-- ci-speedup:runner-minute-spine -->\n```markdown\n{table}```\n"

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "no visible markdown table" in chk.detail


def test_runner_minute_spine_contract_rejects_table_hidden_inside_indented_code(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    table = _rendered_runner_minute_spine_report().split(
        "<!-- ci-speedup:runner-minute-spine -->\n", 1)[1]
    indented_table = "\n".join(f"    {line}" if line else line for line in table.splitlines())
    report = f"# report\n<!-- ci-speedup:runner-minute-spine -->\n{indented_table}\n"

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "no visible markdown table" in chk.detail


def test_runner_minute_spine_contract_rejects_suffixed_marker_name(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = _rendered_runner_minute_spine_report().replace(
        "<!-- ci-speedup:runner-minute-spine -->",
        "<!-- ci-speedup:runner-minute-spine-disabled -->")

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "missing the runner-minute-spine marker" in chk.detail


def test_runner_minute_spine_contract_rejects_unmarked_cost_spine_heading(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        "# report\n\n### Cost spine: where runner minutes go\n", findings_path)

    assert not chk.ok
    assert "missing the runner-minute-spine marker" in chk.detail


def test_runner_minute_spine_contract_rejects_unmarked_cost_spine_total(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        "# report\n\n| Total |  |  |  |  |  |  | 100.000 | 150.000 | 100.000% |\n",
        findings_path)

    assert not chk.ok
    assert "missing the runner-minute-spine marker" in chk.detail


def test_runner_minute_spine_contract_rejects_multiple_marked_tables(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = (
        _rendered_runner_minute_spine_report()
        + "\n## Later\n"
        + _rendered_runner_minute_spine_report(usd="$9.99")
    )

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "multiple runner-minute-spine markers" in chk.detail


def test_runner_minute_spine_contract_rejects_later_unmarked_table(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    later_unmarked = _rendered_runner_minute_spine_report(usd="$9.99").replace(
        "<!-- ci-speedup:runner-minute-spine -->\n", "")
    report = _rendered_runner_minute_spine_report() + "\n## Later\n" + later_unmarked

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "missing the runner-minute-spine marker" in chk.detail


def test_runner_minute_spine_contract_rejects_rendered_row_without_source(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(job="deploy"), findings_path)

    assert not chk.ok
    assert "no matching source row" in chk.detail


@pytest.mark.parametrize(("render_kwargs", "field"), [
    ({"workflow": ".github/workflows/deploy.yml"}, "workflow"),
    ({"job": "deploy"}, "job"),
    ({"runner": "windows-latest"}, "runner"),
    ({"event": "push"}, "event"),
    ({"status": "all-status"}, "status"),
    ({"attempt": "prior"}, "attempt"),
    ({"volume": "success"}, "volume"),
])
def test_runner_minute_spine_contract_rejects_each_identity_mismatch(
    tmp_path: Path, render_kwargs: dict, field: str,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(**render_kwargs), findings_path)

    assert not chk.ok, field
    assert "no matching source row" in chk.detail


def test_runner_minute_spine_contract_rejects_rendered_billable_drift(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(billable="149.000"), findings_path)

    assert not chk.ok
    assert "billable min/mo" in chk.detail


@pytest.mark.parametrize(("render_kwargs", "expected_detail"), [
    ({"raw": "99.000"}, "raw min/mo"),
    ({"share": "99.000%"}, "share"),
    ({"total_raw": "99.000"}, "total raw min/mo"),
    ({"total_share": "99.000%"}, "total share"),
])
def test_runner_minute_spine_contract_rejects_rendered_numeric_drift(
    tmp_path: Path, render_kwargs: dict, expected_detail: str,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(**render_kwargs), findings_path)

    assert not chk.ok
    assert expected_detail in chk.detail


def test_runner_minute_spine_contract_rejects_percent_syntax_for_non_share_columns(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            raw="10000%",
            billable="15000%",
            weighted="15000%",
            usd="90%"),
        findings_path)

    assert not chk.ok
    assert "raw min/mo" in chk.detail


def test_runner_minute_spine_contract_rejects_rendered_total_drift(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(total_billable="149.000"), findings_path)

    assert not chk.ok
    assert "total billable min/mo" in chk.detail


def test_runner_minute_spine_contract_rejects_duplicate_rendered_total_rows(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            suffix="| Total |  |  |  |  |  |  | 99.000 | 99.000 | 100.000% |\n"),
        findings_path)

    assert not chk.ok
    assert "duplicate Total rows" in chk.detail


def test_runner_minute_spine_contract_rejects_duplicate_rendered_headers(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = _rendered_runner_minute_spine_report().replace(
        "| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |",
        "| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share | Share |")
    report = report.replace(
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |")

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "duplicate columns" in chk.detail


def test_runner_minute_spine_contract_rejects_separator_width_mismatch(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    report = _rendered_runner_minute_spine_report().replace(
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: |")

    chk = vr.check_runner_minute_spine_contract(report, findings_path)

    assert not chk.ok
    assert "separator width" in chk.detail


def test_runner_minute_spine_contract_rejects_extra_rendered_cells(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")
    lines = _rendered_runner_minute_spine_report().splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("| `.github/workflows/ci.yml`"):
            lines[idx] = line + " unchecked |"
            break

    chk = vr.check_runner_minute_spine_contract("\n".join(lines) + "\n", findings_path)

    assert not chk.ok
    assert "row width" in chk.detail


def test_runner_minute_spine_contract_accepts_rendered_prior_attempt_row(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            extra_rows=_rendered_prior_runner_minute_spine_row(),
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99"),
        findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_rendered_prior_share_drift(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            extra_rows=_rendered_prior_runner_minute_spine_row(share="8.000%"),
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99"),
        findings_path)

    assert not chk.ok
    assert "rendered row 2: share" in chk.detail


def test_runner_minute_spine_contract_accepts_rendered_hidden_row_disclosure(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_many_rows(13), indent=2) + "\n",
        encoding="utf-8")
    extra_rows = "".join(
        "| `.github/workflows/ci.yml` | `build-{idx:02d}` | `ubuntu-latest` | "
        "all-events | success | latest | all-status | "
        "100.000 | 150.000 | 7.700% |\n".format(idx=idx)
        for idx in range(1, 12))

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            job="build-00",
            share="7.700%",
            extra_rows=extra_rows,
            total_raw="1300.000",
            total_billable="1950.000",
            total_weighted="1950.000",
            total_usd="$11.70",
            suffix="+1 more runner-minute row hidden\n"),
        findings_path)

    assert chk.ok and not chk.skipped, chk


def test_runner_minute_spine_contract_rejects_hidden_rows_under_cap(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99",
            suffix="+1 more runner-minute row hidden\n"),
        findings_path)

    assert not chk.ok
    assert "sorted visible source rows" in chk.detail


def test_runner_minute_spine_contract_rejects_hidden_row_disclosure_without_hidden_word(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99",
            suffix="+1 more runner-minute row\n"),
        findings_path)

    assert not chk.ok
    assert "without disclosure" in chk.detail


def test_runner_minute_spine_contract_rejects_hidden_row_disclosure_inside_comment(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99",
            suffix="<!-- +1 more runner-minute row hidden -->\n"),
        findings_path)

    assert not chk.ok
    assert "without disclosure" in chk.detail


def test_runner_minute_spine_contract_rejects_hidden_row_disclosure_inside_indented_code(
    tmp_path: Path,
):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99",
            suffix="    +1 more runner-minute row hidden\n"),
        findings_path)

    assert not chk.ok
    assert "without disclosure" in chk.detail


def test_runner_minute_spine_contract_rejects_hidden_rows_without_disclosure(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            share="90.900%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99"),
        findings_path)

    assert not chk.ok
    assert "without disclosure" in chk.detail


def test_runner_minute_spine_contract_rejects_bogus_hidden_row_disclosure(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            suffix="+1 more runner-minute row hidden\n"),
        findings_path)

    assert not chk.ok
    assert "none are hidden" in chk.detail


def test_runner_minute_spine_contract_rejects_hidden_high_cost_row(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(
        json.dumps(_render_ready_runner_minute_spine_doc_with_prior(), indent=2) + "\n",
        encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            status="all-status",
            attempt="prior",
            raw="10.000",
            billable="15.000",
            weighted="15.000",
            usd="$0.09",
            share="9.100%",
            total_raw="110.000",
            total_billable="165.000",
            total_weighted="165.000",
            total_usd="$0.99",
            suffix="+1 more runner-minute row hidden\n"),
        findings_path)

    assert not chk.ok
    assert "sorted visible source rows" in chk.detail


def test_runner_minute_spine_contract_accepts_null_rendered_cells_for_unpriced_row(
    tmp_path: Path,
):
    vr = _load_verify_report()
    doc = _render_ready_runner_minute_spine_doc()
    row = doc["runner_minute_spine"]["rows"][0]
    row["runner_label"] = "self-hosted linux"
    row["sku"] = "unpriced"
    row["billing_class"] = "unpriced"
    row["sku_weighted_billable_min_per_month"] = None
    row["usd_per_month"] = None
    doc["runner_minute_spine"]["totals"]["sku_weighted_billable_min_per_month"] = 0.0
    doc["runner_minute_spine"]["totals"]["usd_per_month"] = 0.0
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    chk = vr.check_runner_minute_spine_contract(
        _rendered_runner_minute_spine_report(
            runner="self-hosted linux",
            sku="unpriced",
            billing="unpriced",
            weighted="-",
            usd="-",
            total_weighted="0.000",
            total_usd="-"),
        findings_path)

    assert chk.ok and not chk.skipped, chk


def test_legacy_opt66_artifact_still_excluded_from_appendix_on_drilled_pole(tmp_path: Path):
    """LEGACY-TOLERANCE guard (originally the issue #3 regression). The engine no longer
    emits OPT66 (retired in the 2026-07-20 pricing excision — see the catalog's REMOVED
    stub), but a PRE-excision findings.json can still carry one, and the renderer must
    keep handling it: an OPT66 whose only job IS the drilled headline pole must NOT
    render in 'Also noticed' (the pole already headlines that job; a '~0 wall-clock
    minor cleanup' row on it contradicts the headline). The fixture is deliberately the
    legacy shape — do not "modernize" it; its point is that old artifacts stay safe."""
    vr = _load_verify_report()
    report, _report_path, findings_path = _tier2_artifacts(
        tmp_path, _sku_ceiling_on_headline_pole_doc())
    appendix = vr._section(report, "Also noticed") or ""
    assert appendix, "the off-pole hygiene finding should populate an 'Also noticed' appendix"
    assert "OPT17" in appendix, "the off-pole hygiene finding must still render"
    assert "OPT66" not in appendix, "the on-pole SKU ceiling must be excluded, not reframed"
    chk = vr.check_pole_not_reframed_as_hygiene(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_total_deoverlapped_counts_deduped_collection(tmp_path: Path):
    """Issue #4 regression (artifact-level: producer → renderer → verify). When the
    renderer's `_dedupe_findings` collapses an exact-duplicate occurrence, the section
    lead's 'not promoted: N modeled item(s)' tail counts the DEDUPED collection its rows
    render from — here ONE modeled item, not two. Pre-fix, the re-derivation counted the
    RAW findings list and reported '1 != 2', a false FAIL against a correct report."""
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(
        tmp_path, _tier2_dedupe_drops_a_finding_doc())
    # The renderer collapsed the duplicate: exactly one modeled item in the lead tail.
    assert "not promoted: 1 modeled item(s)" in report
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_total_deoverlapped_dedupes_promoted_side(tmp_path: Path):
    """Issue #4 regression, PROMOTED side (artifact-level). An exact-duplicate PROMOTED
    finding must not inflate the section-lead's credited-minute total or its neutral-finding
    count: the renderer de-overlaps BEFORE ranking, so the reader sees ONE promoted row.
    Pre-fix, `check_tier2_total_deoverlapped` re-derived count/raw_min/usd from the RAW list
    and ranked the collapsed duplicate twice — a false FAIL against a correct report (the
    money-and-minutes companion to test_tier2_total_deoverlapped_counts_deduped_collection,
    which pins the not-promoted tail side)."""
    vr = _load_verify_report()
    doc = _tier2_dedupe_drops_a_promoted_finding_doc()
    # Non-vacuous: the fixture really carries two findings that collapse to one dedupe key.
    keys = {(f.get("source", "ci-speedup"), f.get("pattern"), f.get("workflow_file"),
             f.get("line"), (f.get("evidence") or "").strip()) for f in doc["findings"]}
    assert len(keys) < len(doc["findings"]), "fixture must contain a real exact-duplicate"
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    # The renderer credits the promoted finding once, so the section reads one neutral finding.
    assert "1 neutral finding" in (vr._section(report, "Runner-minute reductions") or "")
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_vr_dedupe_findings_stays_coupled_to_the_engine():
    # `check_tier2_total_deoverlapped` re-derives the Tier-2 lead tail from a LOCAL
    # `_vr_dedupe_findings` copy (verify_report is standalone — no blocking_path import).
    # If the engine retunes its `_dedupe_findings` collapse rule and the verifier's copy
    # lags, the re-derivation would count a different population than the renderer's rows
    # render from — the exact count-drift false-positive this fix removes, re-introduced in
    # reverse. Pin them behavior-equal over shapes that exercise every branch of the dedupe
    # KEY (source/pattern/workflow_file/line/evidence): exact dup, evidence whitespace-strip
    # dup, all-default dup, a source-DEFAULT-driven collapse (present-vs-omitted `source`,
    # which pins the sole non-empty default value), and a distinct entry differing on EACH key
    # component, with order preserved. Non-dict entries are intentionally out of scope: the engine would raise on
    # them, so a report only exists when every finding is a dict — the mirror's extra
    # non-dict passthrough can never diverge on the shapes a real report actually carries.
    vr = _load_verify_report()
    bp = _load_blocking_path()
    # Each entry carries a unique `id` (NOT part of the dedupe key) purely as a survivor
    # tag — comparing the surviving ids pins order AND which occurrence is kept, even
    # where several entries share an evidence string.
    f_a = {"id": "a", "source": "ci-speedup", "pattern": "OPT17",
           "workflow_file": "ci.yml", "line": 42, "evidence": "sleep 10"}
    f_a_dup = {**f_a, "id": "a-dup"}                      # exact-duplicate key → collapsed
    f_a_ws = {**f_a, "id": "a-ws", "evidence": "  sleep 10  "}  # strip-equal → collapsed
    f_src = {**f_a, "id": "src", "source": "other"}       # distinct on source
    f_pat = {"id": "pat", "pattern": "OPT35", "workflow_file": "ci.yml",  # distinct pattern; source defaulted
             "line": 42, "evidence": "sleep 10"}
    f_wf = {**f_a, "id": "wf", "workflow_file": "other.yml"}    # distinct on workflow_file
    f_line = {**f_a, "id": "line", "line": 7}            # distinct on line
    f_ev = {**f_a, "id": "ev", "evidence": "different"}  # distinct on evidence
    f_defaults = {"id": "def"}                           # every dedupe key defaulted
    f_defaults_dup = {"id": "def-dup", "line": 0}        # dup of f_defaults via defaults
    f_none_ev = {"id": "none-ev", "pattern": "OPTX", "evidence": None}  # evidence None → "" ; distinct
    # This pair's collapse decision HINGES on the `source` default VALUE: one states
    # `source` explicitly, the other omits it (→ the default), and they are otherwise
    # identical on the four remaining key fields. They collapse only while BOTH functions
    # default `source` to the same string — so if the engine's default were retuned and the
    # mirror's lagged (or vice-versa), engine and mirror would disagree on this pair and the
    # test below would catch it. Without this, a drift in the sole non-empty default is invisible.
    f_srcdef = {"id": "srcdef", "source": "ci-speedup", "pattern": "OPTS",
                "workflow_file": "s.yml", "line": 99, "evidence": "sd"}
    f_srcomit = {"id": "srcomit", "pattern": "OPTS",     # omits source → default; else == f_srcdef
                 "workflow_file": "s.yml", "line": 99, "evidence": "sd"}
    findings = [f_a, f_a_dup, f_a_ws, f_src, f_pat, f_wf,
                f_line, f_ev, f_defaults, f_defaults_dup, f_none_ev, f_srcdef, f_srcomit]
    engine_out = bp._dedupe_findings([dict(x) for x in findings])  # engine takes a list
    mirror_out = vr._vr_dedupe_findings({"findings": findings, "repo": "demo/repo"})
    # The engine actually collapsed four occurrences (a-dup, a-ws, def-dup, srcomit), so the
    # equality below is meaningful — including srcomit's default-driven collapse into srcdef.
    assert [f["id"] for f in engine_out] == \
           ["a", "src", "pat", "wf", "line", "ev", "def", "none-ev", "srcdef"], engine_out
    # Same survivors, same order — the mirror must not drift from the engine.
    assert [f["id"] for f in mirror_out["findings"]] == [f["id"] for f in engine_out], (
        "verify_report._vr_dedupe_findings drifted from blocking_path._dedupe_findings — "
        "re-sync the verifier's dedupe copy with the engine's `_dedupe_findings`")
    # The dict wrapper preserves sibling top-level keys untouched.
    assert mirror_out["repo"] == "demo/repo"


def test_tier2_post_completion_accepts_opt35_fail_fast_evidence(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-opt35", 90.0, proof="post_completion_waste_opt35")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_post_completion_accepts_opt64_rerun_attempt_evidence(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-opt64", 100.0, proof="post_completion_waste_opt64")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_post_completion_accepts_opt57_timeout_burn_evidence(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding("f-opt57", 1650.0, proof="post_completion_waste_opt57")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_post_completion_opt57_rederives_runner_minutes(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _tier2_promoted_finding("f-opt57", 9999.0, proof="post_completion_waste_opt57")
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok
    assert "runner_min_saving+overlap" in chk.detail


def test_tier2_post_completion_opt57_rederives_timeout_recommendation(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _tier2_promoted_finding("f-opt57", 1650.0, proof="post_completion_waste_opt57")
    f["timeout_default_burn"]["recommended_timeout_minutes"] = 10
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok
    assert "recommended_timeout_minutes" in chk.detail


def test_tier2_post_completion_opt57_accepts_full_precision_p99_boundary(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _tier2_promoted_finding("f-opt57", 1645.0, proof="post_completion_waste_opt57")
    burn = f["timeout_default_burn"]
    burn["successful_duration_p99_s"] = 1200.0004
    burn["recommended_timeout_minutes"] = 31
    burn["sampled_timeout_burn_min"] = 329.0
    burn["runner_min_saving"] = 1645.0
    burn["samples"][0]["waste_s"] = 19740.0
    f["runner_min_saving"] = 1645.0
    f["measured_signal"] = f["measured_signal"].replace(
        "timeout-minutes 30", "timeout-minutes 31")
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_post_completion_opt57_rejects_non_finite_numbers(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _tier2_promoted_finding("f-opt57", 1650.0, proof="post_completion_waste_opt57")
    f["timeout_default_burn"]["successful_duration_p99_s"] = "NaN"
    f["timeout_default_burn"]["successful_duration_samples"] = "NaN"
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok
    assert "successful_duration" in chk.detail


def test_tier2_post_completion_opt57_binds_evidence_to_affected_job(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _tier2_promoted_finding("f-opt57", 1650.0, proof="post_completion_waste_opt57")
    f["affected_jobs"] = ["lint"]
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok
    assert "affected_jobs" in chk.detail


# ---- PR-P1: the lead's accounting tail (decision table over unpromoted findings) --


def _cert_deferred_finding(fid: str, saving: float) -> dict:
    """Measured, positive saving, NO neutrality certificate — the OPT47 class."""
    return {"id": fid, "pattern": "OPT47", "title": "double-trigger duplicate runs",
            "workflow_file": ".github/workflows/ci.yml", "severity": "MEDIUM",
            "sizing_basis": "measured", "runner_min_saving": saving,
            "measured_signal": "duplicate_run_count x mean_job_min_per_run",
            "wall_clock_p50_s": 0.0}


def test_tier2_lead_accounting_decision_table(tmp_path: Path):
    """The tail accounts for every positive-saving finding by basis and reason
    (PR-P1, exit criterion item 1). Green row: a cert-deferred and a modeled
    unpromoted finding render with their exact counts and the claim carries the
    matching fields."""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"].append(_cert_deferred_finding("f-cd", 400.0))
    doc["findings"].append({"id": "f-mod", "pattern": "OPT45", "title": "modeled thing",
                            "workflow_file": ".github/workflows/ci.yml",
                            "severity": "LOW", "sizing_basis": "modeled",
                            "runner_min_saving": 12.0, "wall_clock_p50_s": 0.0})
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "not promoted: 1 measured item(s) (1 certificate-deferred)" in report
    assert "1 modeled item(s); see Also noticed" in report
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk

    # Tampered tail (a count the findings don't support) -> FAIL.
    tampered = report.replace("1 modeled item(s)", "3 modeled item(s)", 1)
    chk = vr.check_tier2_total_deoverlapped(tampered, findings_path, report_path)
    assert not chk.ok and "modeled item(s)" in chk.detail

    # The retired counter's wording must never come back.
    relapsed = report.replace(
        "not promoted: 1 measured item(s) (1 certificate-deferred) · "
        "1 modeled item(s); see Also noticed",
        "+2 unmeasured item(s) remain in Also noticed", 1)
    chk = vr.check_tier2_total_deoverlapped(relapsed, findings_path, report_path)
    assert not chk.ok

    # Digit-prefix tamper: "1 modeled" must not hide inside "11 modeled"
    # (adversarial review F5 — the tail is matched as one exact string).
    prefixed = report.replace("1 modeled item(s)", "11 modeled item(s)", 1)
    chk = vr.check_tier2_total_deoverlapped(prefixed, findings_path, report_path)
    assert not chk.ok

    # Claim-FIELD tamper (adversarial review F4: this equality was enforced but
    # unpinned — a refactor dropping a field_map entry survived the whole suite).
    sidecar = report_path.parent / (report_path.name + ".claims.json")
    poisoned = sidecar.read_text().replace(
        '"not_promoted_other": 0', '"not_promoted_other": 3', 1)
    assert poisoned != sidecar.read_text(), "fixture lacks the not_promoted_other field"
    sidecar.write_text(poisoned, encoding="utf-8")
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert not chk.ok and "not_promoted_other" in chk.detail


def test_tier2_lead_accounting_treats_empty_certificate_as_deferred(tmp_path: Path):
    """`tier2_neutrality: {}` is NO certificate. The renderer's `_is_tier2_finding`
    requires a non-empty dict; the verifier's first cut checked only isinstance and
    classified the same finding as 'without source rows' — renderer and verifier
    told different stories about the same artifact (greptile P1 on PR-P1). Both must
    say certificate-deferred."""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = _cert_deferred_finding("f-empty-cert", 250.0)
    f["tier2_neutrality"] = {}
    doc["findings"].append(f)
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "1 certificate-deferred" in report
    assert "without source rows" not in report
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_lead_accounting_fails_closed_on_unclassifiable_finding(tmp_path: Path):
    """A positive-saving finding no bucket covers is rendered VISIBLY as 'other'
    and FAILS verification — the alternative is exactly the silent drop the
    accounting exists to end. (basis=None, not OPT73.)"""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"].append({"id": "f-mystery", "pattern": "OPT33",
                            "title": "unstamped positive saver",
                            "workflow_file": ".github/workflows/ci.yml",
                            "severity": "LOW", "runner_min_saving": 30.0,
                            "wall_clock_p50_s": 0.0})
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "1 other item(s)" in report, "renderer must surface the bucket, not drop it"
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert not chk.ok and "unaccountable" in chk.detail


# ---- PR-P2 (G15): the shallow cost-spine disclosure re-derives from the stamp ----
#
# The decision table (`data_sources.cost_spine_shallow_workflows` — the NAMES list
# is the ground truth the check re-derives from; the renderer reads only the count
# stamp, which is exactly why the two must be held coherent):
#   no findings path / unreadable findings       -> SKIP ("no findings to compare")
#   key absent                                   -> SKIP (pre-stamp artifact, compat)
#   key present, empty list                      -> SKIP (nothing left shallow)
#   key present, not a list                      -> FAIL (malformed stamp, fail-closed)
#   non-empty, count stamp != len(names)         -> FAIL (stamp drift — L1: the names
#                                                    are the data; the count is derived)
#   non-empty, shallow_runs missing/non-int      -> FAIL (stamp drift — mirroring the
#                                                    renderer blindly would bless a
#                                                    "shallow None-run" disclosure)
#   non-empty, disclosure present with the count
#     re-derived from the names (anchored match) -> PASS
#   non-empty, disclosure absent or count drift  -> FAIL ("1 ..." must never hide
#                                                    inside "11 ..." — digit-anchored)


def _appendix_modeled_finding(fid: str = "f-mod") -> dict:
    """A modeled positive-saving finding — appendix-resident, so the Also-noticed
    host section (one of the disclosure's two normal carriers) renders."""
    return {"id": fid, "pattern": "OPT45", "title": "modeled thing",
            "workflow_file": ".github/workflows/ci.yml", "severity": "LOW",
            "sizing_basis": "modeled", "runner_min_saving": 12.0,
            "wall_clock_p50_s": 0.0}


def test_cost_spine_shallow_disclosure_decision_table(tmp_path: Path):
    """G15/§5.5: a non-empty `cost_spine_shallow_workflows` stamp means part of the
    runner-minute source block rests on the shallow sample — the rendered report
    must say so. Green row first (real render), then each tamper row must FAIL."""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"].append(_appendix_modeled_finding())
    doc["data_sources"]["cost_spine_shallow_workflows"] = [
        ".github/workflows/a.yml", ".github/workflows/b.yml"]
    doc["data_sources"]["cost_spine_shallow_workflow_count"] = 2
    doc["data_sources"]["shallow_runs"] = 10
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "2 runner-minute source workflow(s) still use a shallow 10-run" in report
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert chk.ok and not chk.skipped, chk

    # The disclosure line dropped by a renderer regression (the exact G15 shape:
    # "a renderer regression could drop it from a shipped report without the
    # one-command verifier noticing") -> FAIL.
    stripped = "\n".join(ln for ln in report.splitlines()
                         if "cost-spine sample" not in ln)
    chk = vr.check_cost_spine_shallow_disclosed(stripped, findings_path)
    assert not chk.ok and not chk.skipped

    # Digit-prefix tamper: the stamp says 2, the report says 12. The expected
    # string "2 runner-minute ..." is a substring of the tampered line, so an
    # unanchored match would pass — the check must anchor on the digit boundary.
    prefixed = report.replace("2 runner-minute source workflow(s)",
                              "12 runner-minute source workflow(s)", 1)
    chk = vr.check_cost_spine_shallow_disclosed(prefixed, findings_path)
    assert not chk.ok

    # Count-stamp drift: the renderer renders the COUNT stamp; the verifier
    # re-derives from the NAMES. A pair that disagrees is a malformed artifact
    # (collect_runs writes both together) and must fail closed, not be read
    # through whichever side happens to match the prose.
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["data_sources"]["cost_spine_shallow_workflow_count"] = 3
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert not chk.ok and "drift" in chk.detail

    # Depth-stamp drift (greptile P2): shallow_runs missing while workflows are
    # stamped shallow. Mirroring the renderer blindly would bless a
    # "shallow None-run cost-spine sample" disclosure as compliant — the pair is
    # incoherent (collect_runs always stamps the depth), so it fails closed.
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["data_sources"]["cost_spine_shallow_workflow_count"] = 2
    del data["data_sources"]["shallow_runs"]
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert not chk.ok and "shallow_runs" in chk.detail


def test_cost_spine_shallow_disclosure_skip_and_fail_closed_rows(tmp_path: Path):
    """Absent stamp ⇒ SKIP (pre-stamp compat, the committed better-auth/langfuse/
    mastra corpora); empty list ⇒ SKIP (nothing left shallow); a malformed stamp
    ⇒ FAIL (never silently vacuous)."""
    vr = _load_verify_report()

    absent_dir = tmp_path / "absent"
    absent_dir.mkdir()
    doc = _tier2_doc_for_verify()
    doc["data_sources"].pop("cost_spine_shallow_workflows", None)
    doc["data_sources"].pop("cost_spine_shallow_workflow_count", None)
    report, _rp, findings_path = _tier2_artifacts(absent_dir, doc)
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert chk.ok and chk.skipped and "pre-stamp" in chk.detail, chk

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    doc = _tier2_doc_for_verify()
    doc["data_sources"]["cost_spine_shallow_workflows"] = []
    doc["data_sources"]["cost_spine_shallow_workflow_count"] = 0
    report, _rp, findings_path = _tier2_artifacts(empty_dir, doc)
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert chk.ok and chk.skipped, chk

    # Malformed stamp (not a list): fail closed — a hand-edited artifact must not
    # skate through as "vacuously fine".
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["data_sources"]["cost_spine_shallow_workflows"] = {"oops": True}
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert not chk.ok and not chk.skipped

    # No findings to compare -> SKIP, mirroring the other findings-driven checks.
    chk = vr.check_cost_spine_shallow_disclosed(report, None)
    assert chk.ok and chk.skipped


def test_cost_spine_shallow_disclosure_renders_without_host_sections(tmp_path: Path):
    """The disclosure normally rides the Pre-start-wait or Also-noticed sections.
    Found while authoring this check's decision table: a doc with a non-empty
    shallow stamp and NEITHER host section rendered NO disclosure anywhere — an
    honest render silently hiding its own coverage gap. The renderer must emit
    the note standalone in that case, and the check holds it there."""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()   # one promoted Tier-2 row; no appendix, no queue
    doc["data_sources"]["cost_spine_shallow_workflows"] = [".github/workflows/x.yml"]
    doc["data_sources"]["cost_spine_shallow_workflow_count"] = 1
    doc["data_sources"]["shallow_runs"] = 10
    report, _report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "Also noticed" not in report and "Pre-start wait" not in report, (
        "fixture drift: a host section rendered, so this no longer exercises "
        "the standalone-emission corner")
    assert "1 runner-minute source workflow(s) still use a shallow 10-run" in report
    chk = vr.check_cost_spine_shallow_disclosed(report, findings_path)
    assert chk.ok and not chk.skipped, chk


def test_no_timing_endpoint_citation_red_green(tmp_path: Path):
    """§4.5/G8: the closing-down `/timing` endpoints may never be cited as a data
    source — in prose or in a claim. But only endpoint-SHAPED citations: a job
    legitimately named after a `timing` directory must never redden a report
    (adversarial review F1)."""
    vr = _load_verify_report()
    report, report_path, _findings_path = _tier2_artifacts(tmp_path)
    assert vr.check_no_timing_endpoint_citation(report, report_path).ok
    cited = report + "\nDerived from GET /repos/o/r/actions/runs/1/timing.\n"
    chk = vr.check_no_timing_endpoint_citation(cited, report_path)
    assert not chk.ok and "/timing" in chk.detail
    # A monorepo job named after a path containing "timing" is CONTENT, not a
    # citation — must stay green.
    benign = report + "\n**Where:** job `test (packages/timing)` in ci.yml\n"
    assert vr.check_no_timing_endpoint_citation(benign, report_path).ok
    # A claim citing it is as wrong as prose: poison the sidecar.
    sidecar = report_path.parent / (report_path.name + ".claims.json")
    sidecar.write_text(sidecar.read_text().replace(
        "jobs_api_timestamps", "runs/{id}/timing", 1), encoding="utf-8")
    assert not vr.check_no_timing_endpoint_citation(report, report_path).ok


def test_tier2_claims_carry_derivation_basis(tmp_path: Path):
    """G8: every tier2_* claim records derivation_basis='jobs_api_timestamps' as a
    machine-readable field; stripping it FAILS."""
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(tmp_path)
    chk = vr.check_tier2_claims_derivation_basis(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk
    sidecar = report_path.parent / (report_path.name + ".claims.json")
    stripped = sidecar.read_text().replace('"derivation_basis": "jobs_api_timestamps",', "")
    stripped = stripped.replace('"derivation_basis": "jobs_api_timestamps"', '"x": "y"')
    sidecar.write_text(stripped, encoding="utf-8")
    chk = vr.check_tier2_claims_derivation_basis(report, findings_path, report_path)
    assert not chk.ok


def test_tier2_total_check_accepts_overlap_displacement(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["runner_min_saving"] = 105.0
    f["runner_min_overlap_s"] = 15.0
    f["tier2_overlap_note"] = "1 sampled run id already credited by a higher-ranked finding"
    f["usd_saving_per_month"] = 0.63
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_verify_caps_visible_rows_but_totals_all_eligible(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding(f"f-{i:02d}", 200.0 - i)
        for i in range(13)
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert len(vr._tier2_markers(report)) == 12
    checks = [
        vr.check_tier2_neutrality_derived(report, findings_path, report_path),
        vr.check_tier2_total_deoverlapped(report, findings_path, report_path),
    ]
    assert all(c.ok and not c.skipped for c in checks), [c for c in checks if not c.ok or c.skipped]


def test_tier2_hidden_rows_past_cap_are_still_verified(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["findings"] = [
        _tier2_promoted_finding(f"f-{i:02d}", 200.0 - i)
        for i in range(13)
    ]
    hidden = doc["findings"][-1]
    hidden["tier2_neutrality"] = {"proof": "made_up", "margin_s": None}
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "f-12" not in dict(vr._tier2_markers(report))
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "f-12" in chk.detail


def test_tier2_below_floor_allows_nonrendered_concurrent_check_name(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    wf = ".github/workflows/ci.yml"
    doc["pr_critical_path"]["checks"].append(
        {"name": "lint", "workflow_file": wf, "p50_s": 100.0, "present_on": 4, "pole_n": 0})
    doc["per_workflow_timing"] = {
        wf: {"floor_p50": 300.0, "job_p50": {"build": 300.0, "lint": 100.0}}
    }
    doc["findings"] = [_tier2_promoted_finding("f-below", 40.0, proof="below_cluster_floor")]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_rounding_waste_rederives_below_floor_certificate(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    wf = ".github/workflows/ci.yml"
    doc["per_workflow_timing"] = {
        wf: {"floor_p50": 120.0, "job_p50": {
            "build": 120.0,
            "tiny (1)": 20.0,
            "tiny (2)": 30.0,
            "tiny (3)": 25.0,
        }}
    }
    doc["findings"] = [
        _tier2_promoted_finding("f-round", 20.0, proof="below_cluster_floor_opt65")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_rounding_waste_requires_rederivable_floor_margin(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["per_workflow_timing"] = {}
    doc["findings"] = [
        _tier2_promoted_finding("f-round", 20.0, proof="below_cluster_floor_opt65")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "below-floor margin" in chk.detail


def test_tier2_rounding_waste_requires_rederivable_runner_minutes(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    wf = ".github/workflows/ci.yml"
    doc["per_workflow_timing"] = {
        wf: {"floor_p50": 120.0, "job_p50": {
            "build": 120.0,
            "tiny (1)": 20.0,
            "tiny (2)": 30.0,
            "tiny (3)": 25.0,
        }}
    }
    doc["findings"] = [
        _tier2_promoted_finding("f-round", 21.0, proof="below_cluster_floor_opt65")
    ]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "runner_min_saving" in chk.detail


def test_tier2_lead_carries_no_dollar_surface(tmp_path: Path):
    """Pricing excised 2026-07-20: the section lead is minutes-only. A repo whose
    findings carry a positive runner-minute saving renders the credited-minutes
    lead with NO rate-derived dollar sentence, and `check_tier2_total_deoverlapped`
    re-derives that minutes-only lead."""
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["runner_min_saving"] = 0.5
    f["runner_min_range_s"] = None
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "at published rates (as of" not in report
    assert "$" not in (vr._section(report, "Runner-minute reductions") or "")
    chk = vr.check_tier2_total_deoverlapped(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_total_check_fails_when_visible_section_lead_is_tampered(tmp_path: Path):
    vr = _load_verify_report()
    report, report_path, findings_path = _tier2_artifacts(tmp_path)
    tampered = report.replace("120 min/mo credited after de-overlap",
                              "999 min/mo credited after de-overlap", 1)
    chk = vr.check_tier2_total_deoverlapped(tampered, findings_path, report_path)
    assert not chk.ok


def test_tier2_capacity_headline_uses_minutes_not_unqualified_dollars(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["repo_visibility"] = "public"
    f = doc["findings"][0]
    f["billing_class"] = "capacity"
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    after_gate = next(line for line in report.splitlines() if "**After the gate.**" in line)
    assert "$0.72/mo of wall-clock-neutral runner spend" not in after_gate
    assert "120 min/mo of wall-clock-neutral runner minutes" in after_gate
    chk = vr.check_tier2_headline_matches_stamp(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_post_completion_requires_overlap_evidence_stamp(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["evidence"] = "old runs were expensive"
    f["measured_signal"] = "mean job-minutes"
    f["tier2_neutrality"]["ref"] = "detector asserted this after the fact"
    f["measured_evidence"] = {
        "summary": "expensive runs existed",
        "table": {"headers": ["Workflow", "Runs"], "rows": []},
    }
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "post_completion_waste" in chk.detail


def test_tier2_post_completion_unsupported_pattern_names_extension_gap(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f["pattern"] = "OPT99"
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok
    assert "unsupported post_completion_waste pattern 'OPT99'" in chk.detail


def test_tier2_non_pr_event_cert_rederives_event_subset(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["events_by_wf"] = {".github/workflows/nightly.yml": ["schedule"]}
    doc["per_workflow_timing"] = {
        ".github/workflows/nightly.yml": {"events": ["schedule"]}
    }
    doc["findings"] = [_tier2_promoted_finding("f-schedule", 90.0, proof="non_pr_event")]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert chk.ok and not chk.skipped, chk


def test_tier2_non_pr_event_requires_subset_stamp(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["events_by_wf"] = {".github/workflows/nightly.yml": ["schedule"]}
    f = _tier2_promoted_finding("f-schedule", 90.0, proof="non_pr_event")
    f.pop("tier2_run_subset_events")
    doc["findings"] = [f]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "non_pr_event lacks stamped event-subset evidence" in chk.detail


def test_tier2_non_pr_event_requires_persisted_event_mirror(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    doc["events_by_wf"] = {}
    doc["per_workflow_timing"] = {}
    doc["findings"] = [_tier2_promoted_finding("f-schedule", 90.0, proof="non_pr_event")]
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    chk = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not chk.ok and "non_pr_event lacks stamped event-subset evidence" in chk.detail


def test_opt36_schedule_probe_folds_event_into_persisted_mirror(tmp_path: Path):
    # CLASS regression for "Tier-2 R-rows carry re-derived wall-clock-neutral
    # certificates" (grader seed check_tier2_neutrality_derived). On a
    # [push, schedule] workflow whose main-pass SUCCESS slice happened to be all
    # push, events_by_wf omitted "schedule" — so OPT36's real schedule-burn
    # non_pr_event certificate failed `subset ⊆ events_by_wf[wf]` and
    # verify_report FAILed on tauri/caddy/mastodon. The engine now folds the
    # events observed by the dedicated schedule probe into the persisted mirror.
    cr = _load_collect_runs()
    vr = _load_verify_report()
    wf = ".github/workflows/nightly.yml"

    def _doc(events: list[str]):
        d = _tier2_doc_for_verify()
        d["events_by_wf"] = {wf: list(events)}
        d["per_workflow_timing"] = {wf: {"events": list(events)}}
        d["findings"] = [_tier2_promoted_finding("f-schedule", 90.0,
                                                 proof="non_pr_event")]
        return d

    # BEFORE the fold: the main-pass success slice saw only push -> the cert
    # cannot be re-derived and the invariant fails (this is the reported bug).
    report, report_path, findings_path = _tier2_artifacts(tmp_path, _doc(["push"]))
    pre = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert not pre.ok and "non_pr_event lacks stamped event-subset evidence" in pre.detail

    # The engine fold: the dedicated schedule probe observed genuine
    # event=schedule runs, so those events are unioned into the mirror. The fold
    # is monotonic (push is preserved) and idempotent.
    events_by_wf: dict[str, set[str]] = {wf: {"push"}}
    schedule_runs = [{"event": "schedule"}, {"event": "schedule"}]
    cr._fold_observed_events(events_by_wf, wf, schedule_runs)
    assert events_by_wf[wf] == {"push", "schedule"}
    cr._fold_observed_events(events_by_wf, wf, schedule_runs)
    assert events_by_wf[wf] == {"push", "schedule"}
    # None / empty runs are a no-op — never a fabricated event.
    cr._fold_observed_events(events_by_wf, wf, None)
    cr._fold_observed_events(events_by_wf, wf, [{"event": None}, {}])
    assert events_by_wf[wf] == {"push", "schedule"}

    # AFTER the fold the persisted mirror carries schedule and the same
    # certificate re-derives cleanly.
    fixed = _doc(sorted(events_by_wf[wf]))
    report, report_path, findings_path = _tier2_artifacts(tmp_path, fixed)
    post = vr.check_tier2_neutrality_derived(report, findings_path, report_path)
    assert post.ok and not post.skipped, post


def test_opt36_schedule_probe_fold_is_wired_into_collect():
    # The fold must fire inside collect()'s schedule block, not just exist — a
    # helper nobody calls leaves the mirror incomplete on real runs.
    cr = _load_collect_runs()
    src = inspect.getsource(cr.collect)
    assert "_fold_observed_events(events_by_wf, wf_path, schedule_runs)" in src


def test_tier2_modeled_fallback_headline_must_be_bound(tmp_path: Path):
    vr = _load_verify_report()
    doc = _tier2_doc_for_verify()
    f = doc["findings"][0]
    f.pop("tier2_neutrality")
    f["sizing_basis"] = "modeled"
    f["sku"] = None
    f["sku_class"] = "unpriced"
    f["billing_class"] = "unpriced"
    f["usd_saving_per_month"] = None
    report, report_path, findings_path = _tier2_artifacts(tmp_path, doc)
    assert "modeled bill opportunities remain" in report
    tampered = report.replace(
        "> **After the gate.** modeled bill opportunities remain in Also noticed - not promoted: unmeasured.\n\n",
        "",
        1)
    chk = vr.check_tier2_headline_matches_stamp(tampered, findings_path, report_path)
    assert not chk.ok


def test_tier2_top_level_stamp_surface_without_per_finding_stamps_fails_closed(tmp_path: Path):
    vr = _load_verify_report()
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({
        "repo_visibility": "private",
        "rates_verified_date": "2026-07-03",
        "events_by_wf": {"ci.yml": ["pull_request"]},
        "findings": [{"pattern": "OPT46", "runner_min_saving": 10.0}],
    }), encoding="utf-8")
    chk = vr.check_tier2_neutrality_derived("# demo\n", findings_path, None)
    assert not chk.ok and not chk.skipped


def _minority_slow_doc_dash():
    """Mirror of test_blocking_path._minority_slow_doc, but the frequency-demoted minority check name
    carries an EN-DASH (U+2013). `test` is the gate; the dash-named check is slower (p50 700s) yet the
    per-PR slowest on only 1 PR → minority_slow → the plural minority_slow_note (4963) renders, and it
    interpolates the dash-bearing name via `_fmt_ms`. Returns (doc, dash_name)."""
    name = "flaky – bench"
    npop = 20
    checks = [
        {"name": "test", "p50_s": 400.0, "present_on": npop, "workflow_file": "ci.yml", "pole_n": 18},
        {"name": name, "p50_s": 700.0, "present_on": npop, "workflow_file": "bench.yml", "pole_n": 1},
    ]
    pops = [[0.1, [[name, 2000.0], ["test", 400.0]]]]          # dash-check slowest once
    for _ in range(npop - 1):
        pops.append([0.1, [["test", 400.0], [name, 100.0]]])   # present but fast on the rest
    return {"pr_critical_path": {
        "critical_path_check": "test", "critical_path_s": 400.0,
        "checks": checks, "check_present_n_pr": npop, "populations": pops,
        "poles": [
            {"check": "test", "p50_s": 400.0, "workflow_file": "ci.yml", "job": "test"},
            {"check": name, "p50_s": 700.0, "workflow_file": "bench.yml", "job": "flaky-bench"},
        ],
    }}, name


def test_coverage_passes_on_fresh_minority_render_with_unicode_dash_name(tmp_path: Path):
    # Regression (PR #134 review): the plural minority_slow_note interpolates check NAMES, which can
    # carry Unicode dash glyphs. The report-wide `_strip_emdashes` flattens them to ASCII; without the
    # matching strip at claim construction the manifest keeps the glyph and the coverage guard
    # FALSE-FAILS a legitimate report. Render end-to-end and assert coverage PASSES. Also pins subject
    # correctness for the non-headline kinds (no comparator guards those subjects otherwise).
    bp = _load_blocking_path()
    vr = _load_verify_report()
    doc, dash_name = _minority_slow_doc_dash()
    report = bp.render(doc)
    # sanity: the render actually exercised the plural minority_slow_note fragment (4963).
    assert "present on most sampled PRs but rarely the actual slowest" in report
    manifest = bp._LAST_CLAIMS.to_json()
    rp = tmp_path / "report.md"
    rp.write_text(report, encoding="utf-8")
    (tmp_path / "report.md.claims.json").write_text(json.dumps(manifest), encoding="utf-8")
    cov = vr.check_claims_cover_framing_vocabulary(report, rp)
    assert cov.ok and not cov.skipped, (
        f"coverage must PASS on a fresh minority render (em-dash parity): {cov.detail}")
    # subject correctness: every non-headline framing claim names a real check from the doc (a
    # wrong-but-nonempty subject would poison the typed-claims layer with no comparator to catch it).
    real = {"test", dash_name}
    saw_kinds = set()
    for c in manifest["claims"]:
        if c["kind"] in ("pole_role_line", "minority_slow_note", "pole_gate_prompt"):
            saw_kinds.add(c["kind"])
            for part in (p.strip() for p in str(c["subject"]).split(",")):
                assert part in real, f"{c['kind']} subject {c['subject']!r} names a non-check {part!r}"
    assert "minority_slow_note" in saw_kinds, "the render did not emit a minority_slow_note claim"


def test_coverage_passes_on_fresh_npop_zero_render(tmp_path: Path):
    # Closes the last review residual: the npop=0 blocker-role branch (blocking_path.py 5012,
    # "**The slowest check a PR waits on.**") is exercised by no other test. With empty populations
    # AND a 0 presence denominator, npop stays 0, so the blocker role hits that branch. Assert it
    # renders, is a registered pole_role_line claim whose subject is the pole's check, and the
    # coverage check PASSES end-to-end (its first execution of that branch + its claim path).
    bp = _load_blocking_path()
    vr = _load_verify_report()
    doc = {"pr_critical_path": {
        "critical_path_check": "build", "critical_path_s": 300.0,
        "checks": [{"name": "build", "p50_s": 300.0}],
        "check_present_n_pr": 0, "populations": [],
        "poles": [{"check": "build", "p50_s": 300.0, "workflow_file": "ci.yml", "job": "build"}],
    }}
    report = bp.render(doc)
    assert "**The slowest check a PR waits on.**" in report, "npop=0 blocker role did not render"
    manifest = bp._LAST_CLAIMS.to_json()
    role_claims = [c for c in manifest["claims"]
                   if c["kind"] == "pole_role_line"
                   and c["rendered"] == "**The slowest check a PR waits on.**"]
    assert role_claims, "the npop=0 role was not registered as a pole_role_line claim"
    assert all(c["subject"] == "build" for c in role_claims), "npop=0 role subject is not the pole's check"
    rp = tmp_path / "report.md"
    rp.write_text(report, encoding="utf-8")
    (tmp_path / "report.md.claims.json").write_text(json.dumps(manifest), encoding="utf-8")
    cov = vr.check_claims_cover_framing_vocabulary(report, rp)
    assert cov.ok and not cov.skipped, f"coverage must PASS on the npop=0 render: {cov.detail}"


# --- ENG-1 PR-N2: check_headline_chain_matches_stamp (decision-table red/green) ---

def _chain_check(tmp_path: Path, report: str, findings: dict | None,
                 manifest: dict | None):
    vr = _load_verify_report()
    rp = tmp_path / "report.md"
    rp.write_text(report, encoding="utf-8")
    fp = None
    if findings is not None:
        fp = tmp_path / "findings.json"
        fp.write_text(json.dumps(findings), encoding="utf-8")
    sidecar = tmp_path / "report.md.claims.json"
    if manifest is not None:
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
    elif sidecar.exists():
        sidecar.unlink()  # a prior call's manifest must not leak into this one
    return vr.check_headline_chain_matches_stamp(report, fp, rp)


def _chain_findings(chains, dropped=None):
    facts = []
    for i, (members, s) in enumerate(chains):
        facts.append({"sha": f"s{i}", "chain": list(members),
                      "member_spans_s": {m: s / len(members) for m in members},
                      "chain_s": s, "co_longest_n": 1, "runner_up_s": 0.0,
                      "fallback": None, "makespan_s": s})
    return {"findings": [], "pr_critical_path": {
        "chain_facts": facts,
        "dropped_non_pr_checks": list(dropped or []),
        "dropped_non_required_checks": []}}


def _chain_claim_manifest(subject="compile → test", p50=104.0,
                          merge_dur="1m 44s", modal_n=None, n=None,
                          rendered="the gate is the `compile` → `test` chain: "
                                   "`needs:` runs these checks one after another"):
    return {"families_migrated": ["headline"], "claims": [
        {"kind": "headline_chain", "subject": subject,
         "fields": {"chain_p50_s": p50, "merge_dur": merge_dur,
                    "modal_n": modal_n, "n": n},
         "rendered": rendered}]}


_CHAIN_REPORT = ("# repo — why is the merge slow?\n\n"
                 "> **Bottom line.** A typical PR waits **1m 44s** for the "
                 "`compile` → `test` chain to finish — merge wait.\n\n"
                 "the gate is the `compile` → `test` chain: `needs:` runs these "
                 "checks one after another\n")


def test_chain_check_cell2_legacy_artifact_skips(tmp_path: Path):
    c = _chain_check(tmp_path, "# r\n> **Bottom line.** waits.\n",
                     {"findings": [], "pr_critical_path": {}}, None)
    assert c.ok and c.skipped, c.detail


def test_chain_check_cell3_happy_path_passes(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0),
                                (("compile", "test"), 104.0)])
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(modal_n=2, n=2))
    assert c.ok and not c.skipped, c.detail


def test_chain_check_cell3_subject_and_p50_drift_fail(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(subject="compile → lint", modal_n=1, n=1))
    assert not c.ok, "subject drift must FAIL"
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(p50=999.0, modal_n=1, n=1))
    assert not c.ok, "chain_p50_s drift must FAIL"


def test_chain_check_cell4_chained_facts_without_claim_fail(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    # A summary-bearing (N2-era) artifact — the renderer had everything it
    # needed and still framed the gate the old way. (Summary-less facts are
    # the N1-era SKIP, covered in its own cell test.)
    findings["pr_critical_path"]["chain_summary"] = {
        "modal_chain": ["compile", "test"]}
    c = _chain_check(tmp_path, "# r\n> **Bottom line.** waits — merge.\n",
                     findings, None)
    assert not c.ok and "mints no headline_chain" in c.detail


def test_chain_check_cell5_singleton_modal_with_claim_fails(tmp_path: Path):
    findings = _chain_findings([(("test",), 66.0), (("test",), 66.0)])
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(modal_n=2, n=2))
    assert not c.ok and "singleton" in c.detail
    # ... and without a claim it SKIPs (the classic forms apply).
    c = _chain_check(tmp_path, "# r\n> **Bottom line.** waits — merge.\n",
                     findings, None)
    assert c.ok and c.skipped


def test_chain_check_cell6_dropped_member_fails(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)],
                               dropped=["compile"])
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(modal_n=1, n=1))
    assert not c.ok and "spine-DROPPED" in c.detail


def test_chain_check_cell7_bottom_line_wait_mismatch_fails(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    report = _CHAIN_REPORT.replace("**1m 44s**", "**9m 09s**")
    c = _chain_check(tmp_path, report, findings,
                     _chain_claim_manifest(modal_n=1, n=1))
    assert not c.ok, c.detail


def test_chain_check_rendered_bind_is_exactly_once(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    doubled = _CHAIN_REPORT + ("the gate is the `compile` → `test` chain: "
                               "`needs:` runs these checks one after another\n")
    c = _chain_check(tmp_path, doubled, findings,
                     _chain_claim_manifest(modal_n=1, n=1))
    assert not c.ok and "appears 2x" in c.detail


def test_chain_check_self_vouching_merge_dur_fails(tmp_path: Path):
    # Pass-A probe: facts say 104s but the report + claim BOTH say 9m 09s —
    # internally consistent and WRONG. The rendered figure must re-derive.
    findings = _chain_findings([(("compile", "test"), 104.0)])
    report = _CHAIN_REPORT.replace("**1m 44s**", "**9m 09s**")
    c = _chain_check(tmp_path, report, findings,
                     _chain_claim_manifest(merge_dur="9m 09s", modal_n=1, n=1))
    assert not c.ok and "facts-re-derived" in c.detail


def test_chain_check_modal_counts_must_rederive(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(modal_n=13, n=13))
    assert not c.ok and "modal_n/n" in c.detail


def test_chain_check_n1_era_artifact_without_summary_skips(tmp_path: Path):
    # An installed-skill artifact collected between #193 and #194 stamps facts
    # but no chain_summary; the renderer keys on the summary and rendered
    # classic — that must SKIP, not hard-FAIL (pass-A finding 8).
    findings = _chain_findings([(("compile", "test"), 104.0)])
    assert "chain_summary" not in findings["pr_critical_path"]
    c = _chain_check(tmp_path, "# r\n> **Bottom line.** waits — merge.\n",
                     findings, None)
    assert c.ok and c.skipped and "N1-era" in c.detail
    # A summary-bearing artifact with no claim still FAILS (cell 4).
    findings["pr_critical_path"]["chain_summary"] = {"modal_chain": ["compile", "test"]}
    c = _chain_check(tmp_path, "# r\n> **Bottom line.** waits — merge.\n",
                     findings, None)
    assert not c.ok and "mints no headline_chain" in c.detail


def test_chain_check_member_sum_must_rederive(tmp_path: Path):
    findings = _chain_findings([(("compile", "test"), 104.0)])
    findings["pr_critical_path"]["chain_facts"][0]["chain_s"] = 500.0  # inflated
    c = _chain_check(tmp_path, _CHAIN_REPORT, findings,
                     _chain_claim_manifest(modal_n=1, n=1))
    assert not c.ok and "does not re-derive from its member spans" in c.detail


# --- cells 3c (V5: paren-safe member boundary) + 3d (V2: floor-note headroom bound) ---

_PAREN_SUBJECT = "compile (3.10) → test (3.13)"
_PAREN_HEADLINE = ("the gate is the `compile (3.10)` → `test (3.13)` chain: "
                   "`needs:` runs these checks one after another")


def _paren_member_report(note_line: str, member: str = "test (3.13)") -> str:
    """A chain report whose drilled member name ends in a NON-WORD char — the
    exact artifact class (matrix legs like `test (3.13)`) cell 3c's old `\\b`
    boundary silently skipped (review V5)."""
    return ("# repo — why is the merge slow?\n\n"
            "> **Bottom line.** A typical PR waits **1m 44s** for the "
            "`compile (3.10)` → `test (3.13)` chain to finish — merge wait.\n\n"
            f"{_PAREN_HEADLINE}\n\n"
            f"## 🟡 Long pole 1: `ci.yml` ▸ {member} - 1m 06s\n\n"
            f"{note_line}\n\n"
            "## Also noticed\n")


def _paren_findings(headroom_s: float = 5.0):
    """One fact for the paren-membered chain; runner_up set so the facts-derived
    per-PR headroom (chain_s − runner_up_s) is `headroom_s`."""
    findings = _chain_findings([(("compile (3.10)", "test (3.13)"), 104.0)])
    findings["pr_critical_path"]["chain_facts"][0]["runner_up_s"] = 104.0 - headroom_s
    return findings


def _paren_manifest(with_role: bool = True, modal_n: int = 1, n: int = 1):
    manifest = _chain_claim_manifest(subject=_PAREN_SUBJECT, modal_n=modal_n, n=n,
                                     rendered=_PAREN_HEADLINE)
    if with_role:
        manifest["claims"].append({
            "kind": "pole_role_line", "subject": "test (3.13)",
            "fields": {"stage": 2, "chain_len": 2},
            "rendered": "**Stage 2/2 of the gate chain.**"})
    return manifest


_COMPLIANT_NOTE = ("> **What a change here can buy (wall-clock):** up to **~5s** - "
                   "this check is stage 2/2 of the gate chain.")


def test_chain_check_cell3c_paren_member_without_stage_role_fails(tmp_path: Path):
    """Review V5 red: `\\b` after `re.escape("test (3.13)")` cannot match between
    `)` and a space, so the old regex never saw this member's Long pole section
    and the missing chain-stage role sailed through. The explicit non-word/end
    boundary must now catch it."""
    c = _chain_check(tmp_path, _paren_member_report(_COMPLIANT_NOTE),
                     _paren_findings(), _paren_manifest(with_role=False))
    assert not c.ok and "no chain-stage role claim" in c.detail


def test_chain_check_cell3c_paren_member_with_stage_role_passes(tmp_path: Path):
    c = _chain_check(tmp_path, _paren_member_report(_COMPLIANT_NOTE),
                     _paren_findings(), _paren_manifest())
    assert c.ok, c.detail
    # The detail is loud about what was actually bounded (L8).
    assert "floor-note figure(s)" in c.detail


def test_chain_check_cell3c_bracket_member_boundary_also_matches(tmp_path: Path):
    """The boundary fix is general, not `)`-specific (plan cell 5's second
    sub-case): a `]`-ended member name must also be seen."""
    findings = _chain_findings([(("compile [a]", "test [x]"), 104.0)])
    findings["pr_critical_path"]["chain_facts"][0]["runner_up_s"] = 99.0
    subject = "compile [a] → test [x]"
    headline = ("the gate is the `compile [a]` → `test [x]` chain: "
                "`needs:` runs these checks one after another")
    report = ("# repo — why is the merge slow?\n\n"
              "> **Bottom line.** A typical PR waits **1m 44s** for the "
              "`compile [a]` → `test [x]` chain to finish — merge wait.\n\n"
              f"{headline}\n\n"
              "## 🟡 Long pole 1: `ci.yml` ▸ test [x] - 1m 06s\n\n"
              f"{_COMPLIANT_NOTE}\n\n"
              "## Also noticed\n")
    manifest = _chain_claim_manifest(subject=subject, modal_n=1, n=1,
                                     rendered=headline)
    c = _chain_check(tmp_path, report, findings, manifest)
    assert not c.ok and "no chain-stage role claim" in c.detail


def test_chain_check_cell3d_member_note_exceeding_headroom_fails(tmp_path: Path):
    """Review V2 red: the committed deepgram artifact rendered 'for up to
    **~28s**' on a chain member whose facts-derived headroom is ~5s — with
    every check green. The new bound must kill that exact shape."""
    old_note = ("> **What a change here can buy (wall-clock):** this job's matrix "
                "legs run in parallel, so speeding **this one leg** saves only ~28s "
                "(the next leg, `compile (3.10)`, is 38s). Because the legs share "
                "one job config, a change that speeds *every* leg at once drops the "
                "whole matrix toward the next check, `compile (3.10)` (38s), for up "
                "to **~28s** of merge wait.")
    c = _chain_check(tmp_path, _paren_member_report(old_note),
                     _paren_findings(headroom_s=5.0), _paren_manifest())
    assert not c.ok and "sized as a concurrent sibling" in c.detail


def test_chain_check_cell3d_unrecognized_note_form_fails_loud(tmp_path: Path):
    """Post-merge hardening (#220 review residual): a renderer reword that stops
    matching the figure regex must FAIL loud, not pass with zero figures bounded
    — vacuous-green is the exact failure mode 3d exists to kill."""
    reworded = ("> **What a change here can buy (wall-clock):** as much as ~9s - "
                "this check is stage 2/2 of the gate chain.")
    c = _chain_check(tmp_path, _paren_member_report(reworded),
                     _paren_findings(), _paren_manifest())
    assert not c.ok and "matches no known form" in c.detail
    # ...while the zero-headroom form (the only legitimately figure-less one)
    # still passes.
    zero_form = ("> **What a change here can buy (wall-clock):** ~0s for now - this "
                 "check is stage 2/2 of the gate chain (`needs:` serializes it), "
                 "but a competing path of comparable length gates the merge just "
                 "behind the chain.")
    c = _chain_check(tmp_path, _paren_member_report(zero_form),
                     _paren_findings(), _paren_manifest())
    assert c.ok, c.detail


def test_chain_check_cell3d_bound_uses_median_of_per_pr_wins(tmp_path: Path):
    """The bound re-derives from the FACTS (median of per-PR chain_s −
    runner_up_s), never from chain_summary — a drifted/absent summary win must
    not loosen it. Two facts with wins 4s and 6s → bound 5s: a ~6s claim within
    clock tolerance of nothing (6 > 5 + 0.51) FAILS; ~5s passes."""
    findings = _chain_findings([(("compile (3.10)", "test (3.13)"), 104.0),
                                (("compile (3.10)", "test (3.13)"), 104.0)])
    facts = findings["pr_critical_path"]["chain_facts"]
    facts[0]["runner_up_s"] = 100.0   # win 4
    facts[1]["runner_up_s"] = 98.0    # win 6 → median 5
    note_6s = ("> **What a change here can buy (wall-clock):** up to **~6s** - "
               "this check is stage 2/2 of the gate chain.")
    c = _chain_check(tmp_path, _paren_member_report(note_6s),
                     findings, _paren_manifest(modal_n=2, n=2))
    assert not c.ok and "sized as a concurrent sibling" in c.detail
    c = _chain_check(tmp_path, _paren_member_report(_COMPLIANT_NOTE),
                     findings, _paren_manifest(modal_n=2, n=2))
    assert c.ok, c.detail


# --- Owner-requested TOC fix: the 💸 Contents entry re-derives ------------------

def test_tier2_toc_entry_total_and_rows_must_rederive(tmp_path: Path):
    """The first-class Contents entry's total and row links must match the
    re-derived Tier-2 values — red on drifted totals, missing rows, or a
    section rendered without the entry at all."""
    vr = _load_verify_report()
    # Reuse the committed deepgram artifact (real ranked rows) — the report
    # text is synthetic around the extracted section.
    fj = _SKILL_DIR / "reports" / "deepgram-python-sdk" / "findings.json"
    report_path = _SKILL_DIR / "reports" / "deepgram-python-sdk" / "blocking-path-speed.md"
    if not fj.exists():
        # No committed worked-example corpus in this public repo — skip LOUDLY (never a
        # silent vacuous pass). Runs again the moment a corpus reappears (a generated
        # examples/ report, or in the internal development repo).
        pytest.skip("no committed report corpora in this repo — corpus guards run "
                    "against generated reports / in the internal development repo")
    report = report_path.read_text(encoding="utf-8")
    ok = vr.check_tier2_total_deoverlapped(
        report, fj, report_path)
    assert ok.ok, f"committed report must pass: {ok.detail}"
    # Drift the Contents total only — must FAIL naming the Contents.
    import re as _re
    drifted = _re.sub(r"(\*\*💸 Runner-minute reductions\*\* - ~).+?( of measured)",
                      r"\g<1>999.0 min/mo\g<2>", report, count=1)
    assert drifted != report, "fixture lost the 💸 entry"
    bad = vr.check_tier2_total_deoverlapped(drifted, fj, report_path)
    assert not bad.ok and "Contents total" in bad.detail
    # Drop one enumerated row — must FAIL on the count.
    dropped = _re.sub(r"^1\. 🟢 \[.+?\]\(#r-1\) - .*\n", "", report, count=1,
                      flags=_re.MULTILINE)
    assert dropped != report
    bad = vr.check_tier2_total_deoverlapped(dropped, fj, report_path)
    assert not bad.ok and "R-row link" in bad.detail
    # Remove the whole entry while the section stays — must FAIL loudly.
    gone = report.replace("**💸 Runner-minute reductions**", "**Runner-minute reductions**", 1)
    bad = vr.check_tier2_total_deoverlapped(gone, fj, report_path)
    assert not bad.ok and "first-class" in bad.detail


# --- issue #25: the physical-bounds invariant family (artifact-level + discriminators) ----------
# Artifact-level (producer → renderer → verify): a synthetic findings doc shaped like the named
# round-2 repos is fed through the REAL renderer, then the new bounds checks run on the output. The
# renderer FIX makes the rendered figure coherent; running the same test against origin/main's
# renderer (with THIS verify_report) goes red — the red-proof recorded in the PR body.

def _bounds_artifacts(tmp_path: Path, doc: dict):
    bp = _load_blocking_path()
    report = bp.render(doc)
    rp = tmp_path / "bounds.md"
    fp = tmp_path / "bounds-findings.json"
    rp.write_text(report, encoding="utf-8")
    fp.write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "bounds.md.claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    return report, rp, fp


def _tokio_chain_bounds_doc() -> dict:
    """tokio-rs/tokio (issue #22): a `compile -> miri-test` `needs:` chain whose per-PR SUMMED-span
    p50 (17m18) is DILUTED by fast PRs BELOW miri-test's own 18m36 check p50, with a 19m14 measured
    makespan. The renderer must clamp the rendered chain total up to the largest member (18m36), never
    render the 17m18 sum that sits below a member it claims to add in."""
    compile_p50, miri_p50 = 300.0, 1116.0        # 5m00, 18m36  (checks[].p50_s — each member's drill)
    chain_s, makespan_s = 1038.0, 1154.0          # 17m18 diluted sum, 19m14 measured wall
    checks = [
        {"name": "compile", "p50_s": compile_p50, "present_on": 20, "pole_n": 0,
         "workflow_file": ".github/workflows/ci.yml"},
        {"name": "miri-test", "p50_s": miri_p50, "present_on": 20, "pole_n": 20,
         "workflow_file": ".github/workflows/ci.yml"},
    ]
    pops = [[0.05, [["compile", compile_p50], ["miri-test", miri_p50]]] for _ in range(20)]
    facts = [{"sha": f"s{i}", "chain": ["compile", "miri-test"],
              "member_spans_s": {"compile": 200.0, "miri-test": 838.0},   # sum == chain_s (dilution)
              "chain_s": chain_s, "co_longest_n": 1, "runner_up_s": 0.0,
              "fallback": None, "makespan_s": makespan_s} for i in range(20)]
    return {"repo": "tokio-rs/tokio", "findings": [], "pr_critical_path": {
        "critical_path_check": "miri-test", "critical_path_s": miri_p50,
        "checks": checks, "check_present_n_pr": 20, "populations": pops,
        "poles": [{"check": "compile", "p50_s": compile_p50,
                   "workflow_file": ".github/workflows/ci.yml", "job": "compile"},
                  {"check": "miri-test", "p50_s": miri_p50,
                   "workflow_file": ".github/workflows/ci.yml", "job": "miri-test"}],
        "chain_facts": facts,
        "chain_summary": {"n": 20, "chain_p50_s": chain_s, "modal_chain": ["compile", "miri-test"],
                          "modal_n": 20, "runner_up_p50_s": 0.0, "chain_win_p50_s": 500.0,
                          "makespan_p50_s": makespan_s, "divergence_pct": -10.05}}}


def _nx_makespan_bounds_doc() -> dict:
    """nrwl/nx (issue #24): `main-linux` is the crowned slowest gate whose 46m conditional p50 (over a
    wider run-sample) lowers to a 15m08 population floor, but the MEASURED span-capped makespan is only
    11m00. The renderer must cap the headline wait at the measured makespan, never crown the 15m08
    population floor that overstates the actual wall."""
    main_p50 = 2760.0        # 46m00 conditional p50 (checks[].p50_s over a wider sample)
    per_pr_main = 908.0      # 15m08 sampled per-PR value -> population floor
    makespan_s = 660.0       # 11m00 measured span-capped wall
    checks = [
        {"name": "main-linux", "p50_s": main_p50, "present_on": 19, "pole_n": 19,
         "workflow_file": ".github/workflows/ci.yml"},
        {"name": "lint", "p50_s": 120.0, "present_on": 20, "pole_n": 0,
         "workflow_file": ".github/workflows/lint.yml"},
    ]
    pops = ([[0.05, [["main-linux", per_pr_main], ["lint", 120.0]]] for _ in range(19)]
            + [[0.05, [["lint", 120.0]]]])
    facts = [{"sha": f"s{i}", "chain": ["main-linux"], "member_spans_s": {"main-linux": makespan_s},
              "chain_s": makespan_s, "co_longest_n": 1, "runner_up_s": 0.0,
              "fallback": None, "makespan_s": makespan_s} for i in range(20)]
    return {"repo": "nrwl/nx", "findings": [], "pr_critical_path": {
        "critical_path_check": "main-linux", "critical_path_s": main_p50,
        "checks": checks, "check_present_n_pr": 20, "populations": list(pops),
        "poles": [{"check": "main-linux", "p50_s": main_p50,
                   "workflow_file": ".github/workflows/ci.yml", "job": "main-linux"}],
        "chain_facts": facts,
        "chain_summary": {"n": 20, "chain_p50_s": makespan_s, "modal_chain": ["main-linux"],
                          "modal_n": 20, "runner_up_p50_s": 0.0, "chain_win_p50_s": 0.0,
                          "makespan_p50_s": makespan_s, "divergence_pct": 0.0}}}


def test_bounds_chain_total_never_below_member_tokio(tmp_path: Path):
    """Issue #22 regression (artifact-level). The rendered chain total is clamped up to miri-test's own
    18m36, never the 17m18 diluted sum that sits below a member it claims to add in. RED-PROOF: against
    origin/main's renderer this asserts FAIL (the headline totals 17m18 < the 18m36 member)."""
    vr = _load_verify_report()
    report, rp, fp = _bounds_artifacts(tmp_path, _tokio_chain_bounds_doc())
    import re as _re
    md = _re.search(r"\*\*(.+?) until all checks finish", report)
    assert md and md.group(1) == "18m 36s", f"expected the clamped 18m36 total, got {report[:400]!r}"
    a = vr.check_aggregate_total_ge_largest_member(report, fp, rp)
    assert a.ok and not a.skipped, a.detail
    # bound (b) stays satisfiable at the same time (member floor <= measured makespan).
    b = vr.check_headline_wait_within_makespan(report, fp, rp)
    assert b.ok and not b.skipped, b.detail
    # the existing chain-stamp check re-derives the SAME clamped total (composes with the fix).
    c = vr.check_headline_chain_matches_stamp(report, fp, rp)
    assert c.ok and not c.skipped, c.detail


def test_bounds_headline_wait_within_makespan_nx(tmp_path: Path):
    """Issue #24 regression (artifact-level). The crowned headline wait is capped at the 11m00 measured
    makespan, never the 15m08 population floor that overstates the wall. RED-PROOF: against origin/main's
    renderer this asserts FAIL (the headline says 15m08 > the 11m00 makespan)."""
    vr = _load_verify_report()
    report, rp, fp = _bounds_artifacts(tmp_path, _nx_makespan_bounds_doc())
    import re as _re
    md = _re.search(r"\*\*(.+?) until all checks finish", report)
    assert md and md.group(1) == "11m 00s", f"expected the makespan-capped 11m00 wait, got {report[:400]!r}"
    b = vr.check_headline_wait_within_makespan(report, fp, rp)
    assert b.ok and not b.skipped, b.detail
    # the reconciliation check re-derives the SAME capped floor (composes with the fix).
    r = vr.check_headline_floor_presence_reconciled(report, fp)
    assert r.ok and not r.skipped, r.detail


# --- discriminator arms (checker-side FAIL / PASS / SKIP) --------------------------------------
_AGG = "rendered chain total is never below the largest member it sums"
_MAKESPAN = "headline merge-wait is never above the measured makespan p50"
_COMPUTE = "each finding's runner-minute saving is within its jobs' measured compute"


def _bounds_chain_findings(*, chain_s=1038.0, makespan_s=1154.0, member_p50=1116.0) -> dict:
    facts = [{"sha": f"s{i}", "chain": ["compile", "miri-test"],
              "member_spans_s": {"compile": 200.0, "miri-test": chain_s - 200.0},
              "chain_s": chain_s, "runner_up_s": 0.0, "makespan_s": makespan_s} for i in range(20)]
    return {"findings": [], "pr_critical_path": {
        "chain_facts": facts, "dropped_non_pr_checks": [], "dropped_non_required_checks": [],
        "checks": [{"name": "compile", "p50_s": 300.0},
                   {"name": "miri-test", "p50_s": member_p50}]}}


def _chain_report(merge_dur: str) -> str:
    # The pinned renderer literal the bounds checks text-key on (L7).
    return (f"# repo — why is the merge slow?\n\n"
            f"**{merge_dur} until all checks finish** — the gate is the `compile` → `miri-test` "
            "chain: `needs:` runs these checks one after another.\n")


def test_bounds_aggregate_discriminator(tmp_path: Path):
    findings = _bounds_chain_findings(member_p50=1116.0)   # miri-test renders 18m36
    # FAIL: a 17m18 total below the 18m36 member.
    assert _tag_for(_chain_report("17m 18s"), _AGG, tmp_path, findings=findings) == "FAIL"
    # PASS: the clamped 18m36 total meets the member.
    assert _tag_for(_chain_report("18m 36s"), _AGG, tmp_path, findings=findings) == "PASS"
    # SKIP: no chain (singleton modal) — no aggregate total is claimed.
    single = {"findings": [], "pr_critical_path": {
        "chain_facts": [{"sha": "s0", "chain": ["test"], "chain_s": 100.0, "makespan_s": 100.0}],
        "checks": [{"name": "test", "p50_s": 100.0}]}}
    assert _tag_for(_chain_report("1m 40s"), _AGG, tmp_path, findings=single) == "SKIP"


def test_bounds_makespan_discriminator(tmp_path: Path):
    findings = _bounds_chain_findings(makespan_s=660.0, member_p50=300.0)  # 11m00 wall, small member
    # FAIL: a 15m08 wait above the 11m00 measured makespan.
    assert _tag_for(_chain_report("15m 08s"), _MAKESPAN, tmp_path, findings=findings) == "FAIL"
    # PASS: at/under the 11m00 makespan.
    assert _tag_for(_chain_report("11m 00s"), _MAKESPAN, tmp_path, findings=findings) == "PASS"
    # SKIP: no makespan stamped (legacy artifact) — no measured wall to bound against.
    legacy = {"findings": [], "pr_critical_path": {
        "chain_facts": [{"sha": "s0", "chain": ["a", "b"], "chain_s": 100.0}],
        "checks": [{"name": "a", "p50_s": 10.0}, {"name": "b", "p50_s": 90.0}]}}
    assert _tag_for(_chain_report("2m 00s"), _MAKESPAN, tmp_path, findings=legacy) == "SKIP"


def _compute_findings(*, saving: float, billable: float) -> dict:
    return {"findings": [{"pattern": "OPT40", "workflow_file": ".github/workflows/ci.yml",
                          "affected_jobs": ["cleanup"], "runner_min_saving": saving}],
            "runner_minute_spine": {"render_ready": True, "rows": [
                {"workflow_file": ".github/workflows/ci.yml", "job_name": "cleanup",
                 "billable_equiv_min_per_month": billable}]}}


def test_bounds_compute_discriminator(tmp_path: Path):
    # FAIL: a saving above the job's measured billable compute.
    assert _tag_for(_good(), _COMPUTE, tmp_path,
                    findings=_compute_findings(saving=500.0, billable=200.0)) == "FAIL"
    # PASS: a saving within the measured compute.
    assert _tag_for(_good(), _COMPUTE, tmp_path,
                    findings=_compute_findings(saving=150.0, billable=200.0)) == "PASS"
    # SKIP: no render-ready cost spine — no measured compute to bound against.
    no_spine = {"findings": [{"pattern": "OPT40", "workflow_file": ".github/workflows/ci.yml",
                              "affected_jobs": ["cleanup"], "runner_min_saving": 500.0}]}
    assert _tag_for(_good(), _COMPUTE, tmp_path, findings=no_spine) == "SKIP"


def _collision_findings(*, saving: float) -> dict:
    # issue #52 shape: OPT73's cluster is the two matrix legs of ONE reusable-workflow
    # job (base `build-image / build-image`); the spine ALSO carries a name-similar but
    # DIFFERENT job (`build-image-streaming / build-image`). The honest bound is the two
    # build-image legs summed ONCE (16,642.4). Pre-fix, iterating the raw legs re-added
    # the base's already-summed compute per leg → 33,284.8, so an over-credit read green.
    wf = ".github/workflows/build-push-pr.yml"
    return {"findings": [{"pattern": "OPT73", "workflow_file": wf,
                          "affected_jobs": ["build-image / build-image (linux/amd64)",
                                            "build-image / build-image (linux/arm64)"],
                          "runner_min_saving": saving}],
            "runner_minute_spine": {"render_ready": True, "rows": [
                {"workflow_file": wf, "job_name": "build-image / build-image (linux/amd64)",
                 "billable_equiv_min_per_month": 8128.996},
                {"workflow_file": wf, "job_name": "build-image / build-image (linux/arm64)",
                 "billable_equiv_min_per_month": 8513.411},
                {"workflow_file": wf, "job_name": "build-image-streaming / build-image (linux/amd64)",
                 "billable_equiv_min_per_month": 1154.111},
                {"workflow_file": wf, "job_name": "build-image-streaming / build-image (linux/arm64)",
                 "billable_equiv_min_per_month": 1202.596}]}}


def test_bounds_compute_matrix_leg_collision_discriminator(tmp_path: Path):
    # The bound is the two build-image legs counted ONCE (16,642.4), NOT doubled
    # (33,284.8) and NOT widened by the name-similar streaming legs. A saving above
    # 16,642.4 (past the 2% tolerance) FAILs; at/under it PASSes. Pre-fix, 18,165.8
    # read as within measured compute (the double-count bug this closes).
    assert _tag_for(_good(), _COMPUTE, tmp_path,
                    findings=_collision_findings(saving=18165.8)) == "FAIL"
    assert _tag_for(_good(), _COMPUTE, tmp_path,
                    findings=_collision_findings(saving=16642.4)) == "PASS"


def _compute_gap_findings() -> dict:
    # A finding with a real credited saving whose affected job resolves to NO spine row (the
    # fragile-join miss): the spine has a `cleanup` row, but the finding names a `ghost-job`.
    return {"findings": [{"pattern": "OPT40", "workflow_file": ".github/workflows/ci.yml",
                          "affected_jobs": ["ghost-job"], "runner_min_saving": 500.0}],
            "runner_minute_spine": {"render_ready": True, "rows": [
                {"workflow_file": ".github/workflows/ci.yml", "job_name": "cleanup",
                 "billable_equiv_min_per_month": 200.0}]}}


def test_bounds_compute_unbounded_gap_skips_loud(tmp_path: Path):
    """Silent-pass guard: when a credited saving exists but NONE of its jobs resolve to a spine row,
    the check bounded NOTHING and must SKIP loud — never a green PASS (an unbounded saving rendered
    as "within measured compute" is the fragile-join false-negative)."""
    assert _tag_for(_good(), _COMPUTE, tmp_path, findings=_compute_gap_findings()) == "SKIP"
    # The detail must name the coverage gap so a silent relabel is caught, not just the tag.
    vr = _load_verify_report()
    fp = tmp_path / "gap-findings.json"
    fp.write_text(json.dumps(_compute_gap_findings()), encoding="utf-8")
    res = vr.check_saving_within_measured_compute(_good(), fp)
    assert res.ok and res.skipped, res.detail
    assert "could not bound any credited saving" in res.detail and "coverage gap" in res.detail


def _member_above_makespan_doc() -> dict:
    """Relaxed-ceiling case — bounds (a) and (b) jointly satisfiable when a member out-measures the
    wall. The largest chain member's own measured p50 (18m36) EXCEEDS the span-capped makespan
    (15m00), so the clamp floors the diluted 13m20 sum UP to the 18m36 member, rendering a headline
    ABOVE the makespan. bound (b) relaxes its ceiling to max(makespan, largest member) so a correct
    report does NOT false-FAIL; bound (a) still holds (total == the member). This is the one branch
    that exists solely to keep (a) and (b) from contradicting each other on a fixed report."""
    compile_p50, miri_p50 = 300.0, 1116.0        # 5m00, 18m36  (checks[].p50_s)
    chain_s, makespan_s = 800.0, 900.0            # 13m20 diluted sum, 15m00 span-capped wall (< member)
    checks = [
        {"name": "compile", "p50_s": compile_p50, "present_on": 20, "pole_n": 0,
         "workflow_file": ".github/workflows/ci.yml"},
        {"name": "miri-test", "p50_s": miri_p50, "present_on": 20, "pole_n": 20,
         "workflow_file": ".github/workflows/ci.yml"},
    ]
    pops = [[0.05, [["compile", compile_p50], ["miri-test", miri_p50]]] for _ in range(20)]
    facts = [{"sha": f"s{i}", "chain": ["compile", "miri-test"],
              "member_spans_s": {"compile": 200.0, "miri-test": 600.0},
              "chain_s": chain_s, "co_longest_n": 1, "runner_up_s": 0.0,
              "fallback": None, "makespan_s": makespan_s} for i in range(20)]
    return {"repo": "tokio-rs/tokio", "findings": [], "pr_critical_path": {
        "critical_path_check": "miri-test", "critical_path_s": miri_p50,
        "checks": checks, "check_present_n_pr": 20, "populations": pops,
        "poles": [{"check": "compile", "p50_s": compile_p50,
                   "workflow_file": ".github/workflows/ci.yml", "job": "compile"},
                  {"check": "miri-test", "p50_s": miri_p50,
                   "workflow_file": ".github/workflows/ci.yml", "job": "miri-test"}],
        "chain_facts": facts,
        "chain_summary": {"n": 20, "chain_p50_s": chain_s, "modal_chain": ["compile", "miri-test"],
                          "modal_n": 20, "runner_up_p50_s": 0.0, "chain_win_p50_s": 500.0,
                          "makespan_p50_s": makespan_s, "divergence_pct": -11.11}}}


def test_bounds_member_p50_above_makespan_relaxes_ceiling(tmp_path: Path):
    """The load-bearing joint-satisfiability branch: largest member p50 (18m36) > makespan (15m00).
    The renderer floors the diluted sum up to the member — a headline ABOVE the measured wall — and
    both bound checks must PASS (bound (b)'s ceiling relaxes to the member; bound (a) holds)."""
    vr = _load_verify_report()
    report, rp, fp = _bounds_artifacts(tmp_path, _member_above_makespan_doc())
    import re as _re
    md = _re.search(r"\*\*(.+?) until all checks finish", report)
    assert md and md.group(1) == "18m 36s", f"expected the member-floored 18m36, got {report[:400]!r}"
    # bound (b) does NOT false-FAIL even though 18m36 > the 15m00 makespan (ceiling relaxed).
    b = vr.check_headline_wait_within_makespan(report, fp, rp)
    assert b.ok and not b.skipped, b.detail
    assert "relaxed" in b.detail or "chain ceiling" in b.detail, b.detail
    # bound (a) holds: the total equals the largest member.
    a = vr.check_aggregate_total_ge_largest_member(report, fp, rp)
    assert a.ok and not a.skipped, a.detail
    # the chain-stamp check re-derives the SAME clamped total (and the new chain_wait_p50_s field).
    c = vr.check_headline_chain_matches_stamp(report, fp, rp)
    assert c.ok and not c.skipped, c.detail


def test_no_dollars_check_ignores_backticked_repo_names():
    # #104 review (P1): repo-derived identity tokens render in backticks; a job named
    # `usd-integration-test` or a step `build-$4-shards` must not fail a legitimate
    # minutes-only report. Skill-emitted dollar prose (never backticked) still fails.
    vr = _load_verify_report()
    ok = vr.check_no_rate_derived_dollars(
        "- job `usd-integration-test` / step `build-$4-shards` took 3m 10s")
    assert ok.ok, ok.detail
    bad = vr.check_no_rate_derived_dollars("R-1 saves $4.20/mo at published rates")
    assert not bad.ok


def test_no_dollars_fence_tracker_survives_mixed_delimiters():
    # #107 bot review (P1): a ~~~ line INSIDE a backtick fence is content, not a
    # closer. The old unconditional toggle desynced there and re-entered fence
    # mode at the real closer, hiding later prose — letting a dollar figure pass.
    vr = _load_verify_report()
    report = "\n".join([
        "prose before",
        "```text",
        "~~~",              # content inside the backtick fence, NOT a closer
        "echo hi",
        "```",              # the real closer
        "Costs $4,200/mo.", # must be SEEN and FAIL
    ])
    chk = vr.check_no_rate_derived_dollars(report)
    assert not chk.ok and "$4,200" in chk.detail
    # And the converse nesting: backticks inside a tilde fence stay content.
    report2 = "\n".join(["~~~", "```", "~~~", "clean minutes prose"])
    assert vr.check_no_rate_derived_dollars(report2).ok
    # Run-length rule (#107 bot review ×2): a valid 4-backtick fence may CONTAIN a
    # 3-backtick line; the closer must be a same-char run >= the opener's length.
    report3 = "\n".join([
        "````text",
        "```",              # content inside the 4-backtick fence, NOT a closer
        "echo hi",
        "````",             # the real closer
        "Costs $4,200/mo.", # must be SEEN and FAIL
    ])
    chk3 = vr.check_no_rate_derived_dollars(report3)
    assert not chk3.ok and "$4,200" in chk3.detail
    # A LONGER run than the opener also closes (>= rule, not ==).
    report4 = "\n".join(["```text", "````", "clean minutes prose"])
    assert vr.check_no_rate_derived_dollars(report4).ok
    # Closer must be delimiter-ONLY (#107 bot review ×3): an info-string line
    # (```text) inside an open fence is content, never a closer.
    report5 = "\n".join([
        "```",
        "```text",          # content: has an info string, cannot close
        "echo hi",
        "```",              # the real closer (bare)
        "Costs $4,200/mo.", # must be SEEN and FAIL
    ])
    chk5 = vr.check_no_rate_derived_dollars(report5)
    assert not chk5.ok and "$4,200" in chk5.detail
    # Trailing whitespace after the closer is still a valid closer.
    report6 = "\n".join(["```text", "```   ", "clean minutes prose"])
    assert vr.check_no_rate_derived_dollars(report6).ok


def test_check_fences_balanced_pins_the_verifier_against_a_stray_fence():
    # Defense-in-depth for the fence-escaping bug: repo text (check/job/step names, verbatim
    # log/YAML evidence) that emits a stray ``` corrupts the report AND desyncs THIS verifier's
    # own `re.findall(r"```text\n(.*?)```")` split — the identical input fools the safety net.
    # `check_fences_balanced` catches a future renderer regression LOUD (a CommonMark fence walk
    # ending still inside a fence), rather than letting the parser mis-split silently.
    vr = _load_verify_report()
    good = _good()
    assert vr.check_fences_balanced(good).ok, "a well-formed report's fences all open and close"
    # Append ONE stray fence delimiter -> opens a fence that never closes -> FAIL (never a silent
    # mis-split); the report now ends inside a fence.
    corrupt = good + "\n```\n"
    chk = vr.check_fences_balanced(corrupt)
    assert not chk.ok and not chk.skipped
    assert "without a close" in chk.detail
    # A balanced open+close pair added back is fine again (the walk ends outside any fence).
    assert vr.check_fences_balanced(good + "\n```text\nx\n```\n").ok


def test_strip_scope_maps_every_backtick_like_clean_label():
    # #108 bot review (P1): the heading renders names with EVERY backtick mapped
    # (apostrophes); _strip_scope must converge identically or a 1-2-backtick name
    # diverges between heading and comparator. Parity battery with the renderer.
    vr = _load_verify_report()
    import blocking_path as bp
    for name in ("run `unit` tests", "x ``y`` z", "clean name", "test ```w``` q",
                 "@scope/pkg `t`"):
        assert vr._strip_scope(name) == bp._clean_label(name), name


def test_gap_fill_grounding_rejects_text_spliced_across_adjacent_log_lines(tmp_path: Path):
    # #110 bot review: whole-log newline collapse let "request failed retrying" pass when
    # the log held "…request failed\nretrying…" — text assembled across two adjacent lines
    # is a fabrication. Matching is per log LINE now; a genuine single-line quote passes.
    (tmp_path / "build.log").write_text(
        "request failed\nretrying now\n" + _GAPFILL_LOG, encoding="utf-8")
    spliced = _with_gapfill_block("request failed retrying")
    assert _tag_for(spliced, _GROUND, tmp_path,
                    findings=_gapfill_findings(str(tmp_path))) == "FAIL"
    genuine = _with_gapfill_block("request failed")
    assert _tag_for(genuine, _GROUND, tmp_path,
                    findings=_gapfill_findings(str(tmp_path))) == "PASS"
