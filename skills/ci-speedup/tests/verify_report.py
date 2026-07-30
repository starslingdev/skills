#!/usr/bin/env python3
"""ci-speedup report self-check - turns "did my change keep the report correct?"
into one PASS/FAIL command instead of a manual grep through a long report.

Parses a rendered ci-speedup **blocking-path** report (markdown) and asserts the
structural invariants every report must satisfy. Optionally cross-checks against
the findings JSON it came from and the skill's git checkout.

    verify_report.py --report blocking-path-speed.md
    verify_report.py --report r.md --findings findings.json --skill-repo ~/Development/skills

Exit 0 if all checks pass, 1 otherwise. Each check prints PASS/FAIL + detail.

Standalone by design: no imports of blocking_path.py/scan.py/config (so it has no
PyYAML/config-collision dependency and can run anywhere a report lands - including
the e2e harness and CI). The report it validates is the measurement-spine report:
a wall-clock critical-path drill, RCA-only - every recognized root cause hands off
via an agent prompt and the report never prescribes a fix.

THIS FILE IS THE CLASS-INVARIANT SINK for the dogfood loop. When dogfooding finds a
report-faithfulness bug (a wrong sizing, a dropped check, a mislabeled pole, a
fabricated lever), the DEFAULT fix is NOT to patch the one renderer line that
produced it - it is to add a deterministic check HERE that RE-DERIVES the truth from
the findings JSON (`pr_critical_path`, especially the per-PR `populations` ground
truth) and asserts the rendered report matches, so the whole CLASS is caught on
every future report. A new check must (1) re-derive, never proxy the renderer;
(2) be classified in `maintainers/ci-speedup/scripts/grader_seeds.py`
`TRIAGE_ALLOWLIST` (which wires it back into the dogfood bug list); and (3) go red
on the offending report, green after the engine fix. Patch a single instance only
when the bug genuinely cannot be expressed as a re-derivation property.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    def __post_init__(self) -> None:
        # Lesson L8 (no silent coverage): a Check must always state WHY it passed / skipped / failed.
        # A blank detail reads as "clean" with no reason — exactly the silent coverage gap the lesson
        # forbids. Enforce the contract STRUCTURALLY: `frozen=True` makes the object immutable, so this
        # __post_init__ check is a true durable invariant (not just a construction-time precondition a
        # later `c.detail = ""` could defeat) — every Check that exists has a non-blank detail. We
        # enforce here at the constructor rather than chasing every branch with a (never-complete)
        # fixture matrix. `raise`, not `assert`, so it holds under `python -O`. Every check in this
        # file already supplies a non-empty detail, so this never fires on correct code; it bites a
        # future check that forgets one (the suite goes red the moment any discriminator test reaches
        # that branch). The fields are read-only, so no consumer mutates a Check (verified: the dogfood
        # grader + measure_contradictions only READ name/ok/detail/skipped).
        if not (self.detail or "").strip():
            raise ValueError(
                f"Check({self.name!r}) was constructed with a blank detail — every check must state "
                "its coverage reason so a SKIP/PASS/FAIL never reads as a silent 'clean'")


# --- small parse helpers -----------------------------------------------------
def _as_dict(v: object) -> dict:
    """A JSON value coerced to a dict — `{}` for ANY non-dict (a list / str / number / None). The
    findings JSON's CONTAINERS (the top-level object, `pr_critical_path`, `data_bundle`) are trusted
    here for SHAPE only: verify_report is a bug-catcher fed possibly-malformed skill output, so a
    wrong-TYPE container must make the dependent check SKIP with its normal "no findings" detail, never
    crash the gate with an AttributeError (the CLI's contract is PASS/FAIL lines + exit 0/1, "anywhere
    a report lands"). The `or {}` idiom only guarded the falsy case; this also guards wrong-type."""
    return v if isinstance(v, dict) else {}


def _as_list(v: object) -> list:
    """A JSON value coerced to a list — `[]` for ANY non-list. Companion to `_as_dict` for the findings
    JSON's array fields (`findings`, `poles`, `checks`, `populations`, `logs`, `dropped_*`): a wrong-type
    field (e.g. `findings` rendered as an object) becomes empty, so the check reads "no findings" rather
    than iterating a dict's keys into an AttributeError."""
    return v if isinstance(v, list) else []


def _is_workflow_file(path: str) -> bool:
    path = str(path or "").strip()
    return path.startswith(".github/workflows/") and _wf_is_file_backed(path)


def _title(report: str) -> str:
    m = re.search(r"^#\s+(.*)$", report, re.MULTILINE)
    return m.group(1) if m else ""


def _section(report: str, header_re: str) -> str:
    """The body of the first `## …` section whose header matches, up to the next
    `## ` (or end). "" when absent."""
    m = re.search(rf"^##\s+.*{header_re}.*?$", report, re.MULTILINE)
    if not m:
        return ""
    rest = report[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


# A no-run-history (static-only) report: an archived / brand-new / low-activity repo
# whose Actions run history aged out, so collect_runs sampled 0 runs and there is no
# measurable critical path. The renderer drops the (unmeasurable) spine and ships the
# static hygiene appendix instead — a legitimate report, not a dead-end, so the
# spine-shaped checks (primary Long-pole section, gating-pole drill) recognize it and
# don't demand a spine that physically can't exist.
#
# Key off the invisible machine marker `blocking_path._render_static_only` stamps, NOT
# the human banner prose: a MEASURED report whose LLM gap-fill / evidence text happens
# to quote "no run history to measure" must not be misclassified static-only (that would
# skip the gating-pole-completeness guard and satisfy the primary-section guard for a
# report that DOES have a spine — failing those invariants open). Keep this literal in
# sync with `_STATIC_ONLY_MARKER` in scripts/blocking_path.py.
_STATIC_ONLY_MARKER = "<!-- ci-speedup:static-only -->"


def _is_static_only(report: str) -> bool:
    return _STATIC_ONLY_MARKER in report


# Merge-path trigger events, re-derived independently of the renderer so the static-only
# escape hatch in `check_primary_section_present` stays HONEST. Mirror
# `collect_runs._PR_VOLUME_EVENTS` / `_PUSH_VOLUME_EVENTS` / `_VOLUME_CONTAMINATING_EVENTS`
# verbatim: a workflow anchors the developer's merge wait when it ran on a PR-developer
# event, OR — for a push-only repo that merges straight to the default branch — on push,
# AND its volume is CI-clean (no human-chatter triggers that dwarf its CI run count). Keep
# these literals in sync with collect_runs; verify_report is standalone (no skill imports).
_VR_PR_VOLUME_EVENTS = frozenset({"pull_request", "merge_group", "pull_request_target"})
_VR_PUSH_VOLUME_EVENTS = frozenset({"push"})
_VR_VOLUME_CONTAMINATING_EVENTS = frozenset({
    "issue_comment", "pull_request_review", "pull_request_review_comment",
    "issues", "discussion", "discussion_comment",
})
_VR_DEVELOPER_EVENTS = _VR_PR_VOLUME_EVENTS


def _measured_merge_path_pole(findings_path: Path | None) -> tuple[float, str, str] | None:
    """The slowest MEASURED merge-path long pole re-derived from `per_workflow_timing`, or
    None. Returns `(long_pole_p50_s, workflow_file, long_pole_job)`. Mirrors
    `collect_runs._select_pr_floor_workflows`: PR-volume workflows first, then (only when
    none ran) push workflows, each CI-clean with a timed long pole. None when findings are
    absent/unreadable, when `per_workflow_timing` predates the `events` stamp, or when no
    merge-path workflow was timed (a genuinely no-run-history repo) — so a static-only
    report stays legitimate exactly when the spine is truly unmeasurable."""
    if findings_path is None:
        return None
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pwt = _as_dict(_as_dict(data).get("per_workflow_timing"))

    def _qualifying(volume_events: frozenset[str]) -> list[tuple[float, str, str]]:
        out: list[tuple[float, str, str]] = []
        for wf, crit in pwt.items():
            crit = _as_dict(crit)
            lp = crit.get("long_pole_p50")
            lp = float(lp) if isinstance(lp, (int, float)) else 0.0
            job = str(crit.get("long_pole_job") or "")
            evs = set(_as_list(crit.get("events")))
            if (job and lp > 0 and (evs & volume_events)
                    and not (evs & _VR_VOLUME_CONTAMINATING_EVENTS)):
                out.append((lp, str(wf), job))
        return out

    cands = _qualifying(_VR_PR_VOLUME_EVENTS) or _qualifying(_VR_PUSH_VOLUME_EVENTS)
    return max(cands) if cands else None


# --- checks (report-only) ----------------------------------------------------
def check_primary_section_present(report: str, findings_path: Path | None = None) -> Check:
    """The report must carry its primary object: the wall-clock spine (>=1 Long
    pole, or the fileless 'no workflow to drill' note). A reader must always reach
    the report's point in one hop. A no-run-history (static-only) report carries the
    hygiene appendix as its primary object instead — but ONLY when the spine is truly
    unmeasurable. If `per_workflow_timing` holds a measured merge-path long pole (e.g. a
    push-only repo whose `test` job timed at 51.5s), dead-ending to static-only "no run
    history" buries the slowest measured job — the PR-floor fallback must synthesize a
    spine from it. Re-derived from the findings JSON, never proxied off the renderer."""
    name = "primary section present (Long poles)"
    has_spine = bool(re.search(r"^##\s+.*Long pole \d+:", report, re.MULTILINE)) \
        or "no editable workflow file" in report
    if has_spine:
        return Check(name, True, "critical-path spine present")
    if _is_static_only(report):
        pole = _measured_merge_path_pole(findings_path)
        if pole is not None:
            return Check(name, False,
                         f"report dead-ends to static-only 'no run history' but "
                         f"per_workflow_timing has a measured merge-path long pole "
                         f"(`{pole[1]}` ▸ {pole[2]} @ {pole[0]:.0f}s) - the PR-floor "
                         f"fallback must synthesize a spine from it, not drop to "
                         f"hygiene-only")
        return Check(name, True, "static-only: no measured merge-path spine to drill")
    return Check(name, False, "report has no `## Long pole N:` section or fileless note")


def check_static_only_banner_matches_ci_shape(report: str,
                                              findings_path: Path | None) -> Check:
    """A static-only report's headline banner must diagnose the repo's ACTUAL CI
    shape, re-derived from the findings JSON. Two false-claim classes it kills:
    (1) hedging "archived, brand-new, or low-activity … aged out" at a repo none of
    whose scanned workflows fires on a PR event — that repo may be running CI daily
    (git scrapers, mirrors, release-only projects); the honest diagnosis is "no
    PR-gating CI"; (2) claiming "found no run timing" / "No run history was
    available" while `data_sources.runs_sampled` > 0 — the same report prices
    runner-minute findings off those timed runs, a direct self-contradiction."""
    name = "static-only banner matches CI shape"
    if not _is_static_only(report):
        return Check(name, True, "not a static-only report", skipped=True)
    if findings_path is None:
        return Check(name, True, "no --findings to inspect", skipped=True)
    try:
        doc = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Check(name, True, "findings unreadable", skipped=True)
    # A broken-fetch report renders the "Collection FAILED/INCOMPLETE" banner instead
    # of a repo-shape diagnosis — its honesty is check_run_list_gaps_named's job.
    if "Collection FAILED" in report or "Collection was INCOMPLETE" in report:
        return Check(name, True, "broken-fetch banner (shape not diagnosed)",
                     skipped=True)
    wt = _as_dict(doc.get("workflow_triggers"))
    pr_wfs = [w for w, evs in wt.items() if isinstance(evs, list)
              and (_VR_PR_VOLUME_EVENTS & {str(e) for e in evs})]
    runs_n = _as_dict(doc.get("data_sources")).get("runs_sampled")
    timed = isinstance(runs_n, (int, float)) and runs_n > 0
    problems: list[str] = []
    if wt and not pr_wfs:
        if "an archived, brand-new, or low-activity repo" in report:
            problems.append(
                "banner hedges 'archived/brand-new/low-activity' but no scanned "
                "workflow fires on a PR event - the honest diagnosis is no-PR-gating CI")
        if "No PR-gating CI to measure" not in report:
            problems.append(
                "no scanned workflow fires on a PR event but the 'No PR-gating CI to "
                "measure' banner is absent")
    if timed and "found no run timing" in report:
        problems.append(
            f"report claims 'found no run timing' while runs_sampled={int(runs_n)} "
            "timed run(s) price findings in the same report")
    if timed and "No run history was available" in report:
        problems.append(
            f"bottom line claims 'No run history was available' while "
            f"runs_sampled={int(runs_n)}")
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, "banner diagnosis matches workflow_triggers/runs_sampled")


def check_pole_anchors_resolve(report: str) -> Check:
    """Every in-report jump target (#pole-N, #r-N, #also-noticed, #pre-start-wait,
    #runner-minute-reductions) resolves to
    an emitted `<a id=…>` anchor - a TOC / headline link that lands nowhere is a silent
    break (e.g. a queue-section pointer whose section didn't render)."""
    name = "every #pole-N / #also-noticed / #pre-start-wait / #runner-minute-reductions reference resolves to an anchor"
    targets = r"pole-\d+|r-\d+|also-noticed|pre-start-wait|runner-minute-reductions"
    refs = set(re.findall(rf"\]\(#({targets})\)", report))
    anchors = set(re.findall(rf'<a\s+id="({targets})"', report))
    missing = sorted(refs - anchors)
    return Check(name, not missing,
                 f"{len(refs)} ref(s), all resolve" if not missing
                 else f"referenced but no anchor: {', '.join(missing)}")


# A pole's prompt / prose that CITES a cross-run check ("… validated across runs in the
# cross-run check above", "the timeline + cross-run check above are measured") tells the
# truth ONLY when that pole actually RENDERS the "🔬 Cross-run check" section. The engine
# suppresses that section whenever the magnitude sample holds fewer than 2 values (a
# singleton drilled-run sample — `blocking_path._mag_line` returns [] on `len(vals) < 2`),
# so a prompt template that emits the validation claim UNCONDITIONALLY (whenever a
# timeline-derived dominant step exists) leaves a DANGLING reference to a section that
# isn't there. Re-derive the truth per pole: the locator phrase must co-occur, in the SAME
# rendered pole, with the section marker. Literals pinned to blocking_path's emit sites —
#   reference: "…in the cross-run check above" (`_build_agent_prompt` /
#              `_build_generic_agent_prompt`) and "the timeline + cross-run check above
#              are measured" (`_llm_analysis_block`);
#   section:   "**🔬 Cross-run check**" (the `_mag_line` header the engine emits).
# Both "above" and "below" locators are matched so the class stays caught regardless of
# which direction a future template points; the one caption that says "…below" only renders
# alongside the section, so matching it is a harmless no-op, never a false positive.
_CROSS_RUN_REF_RE = re.compile(r"cross-run check (?:above|below)", re.IGNORECASE)
_CROSS_RUN_SECTION_MARKER = "🔬 Cross-run check"


def check_rca_hands_off_never_prescribes(report: str) -> Check:
    """The contract: ci-speedup does measured RCA and HANDS OFF to the user's agent
    - it never prescribes a fix and never dead-ends. So: no dead-end language, no
    bolded `**Fix:**` / `**Or de-scope:**` prescriptions anywhere, and every agent
    prompt carries the no-prescription disclaimer (and renders in a copy-friendly
    fence). It also never dangles: a prompt/prose that cites 'the cross-run check
    above' must render inside a pole that actually shows that section (a singleton
    magnitude sample suppresses the section, so the claim must be suppressed too)."""
    name = "RCA hands off via prompts (never prescribes a fix, never dead-ends)"
    low = report.lower()
    # Dead-end markers. The coverage-gap marker is anchored to the FULL rendered
    # sentence "no drill-down available; this is a coverage gap" — `blocking_path`
    # emits exactly that (both render sites) only when a pole matched no detector AND
    # got no phase-4a LLM gap-fill (a real dead-end). Matching the full sentence, not
    # the bare "no drill-down available" fragment, keeps 100% recall on the renderer
    # while shrinking the false-positive surface against agent-authored gap-fill prose
    # that might coincidentally use those words. It is NOT "no catalog pattern matched",
    # which also appears in the FILLED `🤖 LLM root-cause analysis` label (a filled
    # pole, not a dead-end), so we must NOT match on that phrase. The remaining three
    # are deliberately loose guards against an AGENT writing dead-end/lever prose (a
    # different purpose than the renderer marker), so they stay as fragments.
    dead = next((b for b in ("no drill-down available; this is a coverage gap",
                             "no catalog lever", "no catalog fix",
                             "inherent cost (no lever)")
                 if b in low), None)
    if dead:
        return Check(name, False, f"report dead-ends with {dead!r} - a coverage-gap "
                     "pole must be filled by the phase-4a LLM gap-fill (a `🤖 LLM "
                     "root-cause analysis` block), never shipped as a dead-end")
    for stale in ("**Fix:**", "**Or de-scope:**", "What to fix, and when it pays off"):
        if stale in report:
            return Check(name, False, f"report prescribes a fix ({stale!r}) - RCA hands "
                         "off via prompts, it must not prescribe")
    prompts = report.count("🤖 Prompt for your coding agent")
    disclaimers = report.count("does NOT prescribe the fix")
    if prompts and disclaimers != prompts:
        # The renderer OWNS the disclaimer (every prompt builder — `_build_agent_prompt`,
        # `_build_generic_agent_prompt`, `_hygiene_prompt`, and `_llm_agent_prompt` for the
        # gap-fill — emits exactly one). A mismatch is a RENDERER/template bug, not something
        # to patch at audit time: never hand-edit the report or the skill's scripts to balance
        # the count — fix the prompt builder that dropped (or doubled) its disclaimer.
        return Check(name, False, f"{prompts} agent prompt(s) but {disclaimers} "
                     "no-prescription disclaimer(s) - a prompt builder in blocking_path.py is "
                     "missing/doubling its disclaimer (renderer bug; fix the builder, do NOT "
                     "hand-edit the report or the analysis to balance the count)")
    dangling = [f"`{wf}` ▸ {check}"
                for wf, check, body in _pole_header_sections(report)
                if _CROSS_RUN_REF_RE.search(body)
                and _CROSS_RUN_SECTION_MARKER not in body]
    if dangling:
        return Check(name, False,
                     "pole prompt/prose cites 'the cross-run check above' but that pole "
                     "renders NO '🔬 Cross-run check' section - a singleton magnitude "
                     "sample suppressed the section while a prompt builder in "
                     "blocking_path.py emitted the cross-run validation claim "
                     "unconditionally (fix the builder to gate the claim on whether the "
                     "cross-run check actually rendered): " + "; ".join(dangling))
    return Check(name, True, f"{prompts} prompt(s), all hand off; no prescriptions"
                 if prompts else "RCA-only; no prescriptions, no dead-ends")


# A MEASURED CAUSE that claims the finding is "visible ... in the timeline above" (or "as
# sequential steps in the timeline"). The MEASURED CAUSE is derived from the job LOG the
# detector parsed, NOT from the pole's rendered per-step timeline (a separate artifact), so
# such a claim asserts a SHAPE the timeline can contradict. Anchored to the renderer prose
# (`blocking_path._FIX_META[...]['cause']`) and pinned by test_verify_report_self.py.
_CAUSE_TIMELINE_CLAIM_RE = re.compile(
    r"(?:visible|shown|rendered|appear\w*)[^.\n]*\bin the timeline above\b"
    r"|\bsequential steps in the timeline\b"
    r"|\bsteps in the timeline above\b",
    re.IGNORECASE)
# A per-step waterfall / Gantt ROW: a RUN of the ASCII bar glyphs the drill draws
# (`█` running, `░` elapsed-before). Requiring >=2 ADJACENT glyphs matches a real bar
# field (every rendered bar is a long run) while EXCLUDING the timeline legend line,
# which embeds isolated single `░`/`█` glyphs in prose (`` `░` = time already elapsed,
# `█` = the step running ``) and is not itself a step row. One run per rendered step row,
# so counting runs re-derives how many step rows the pole's waterfall actually shows —
# on both the captured-timeline (Gantt) and the no-timeline (P50-bar) render paths.
_WATERFALL_BAR_RE = re.compile(r"[█░]{2,}")


def check_measured_cause_matches_rendered_timeline(report: str) -> Check:
    """No agent prompt's MEASURED CAUSE may point at the pole's rendered timeline as
    showing the finding "as sequential steps in the timeline above" unless that pole's
    waterfall actually renders >=2 step rows.

    The MEASURED CAUSE is sourced from a static `_FIX_META[...]['cause']` describing what
    the detector read out of the job LOG — it cannot know what the SEPARATE per-step
    timeline renders. Detector C (playwright-parallel) fires on >=2 `playwright test
    <spec>` hits ANYWHERE in the joined log with no step-structure check, yet its canned
    cause asserted the invocations are "visible as sequential steps in the timeline
    above". When those invocations live in ONE step (nrwl/nx: a single
    `Run Checks/Lint/Test/Build` step), or the pole captured no timeline, the waterfall
    draws ONE bar row — the report's own timeline contradicts the cause. This is the R2
    rule (generic prompts never point at a "step timeline above" the pole doesn't render),
    applied to the catalog MEASURED CAUSE path, and re-derived from each pole's rendered
    step-row count so a cause referencing a genuinely multi-step timeline still passes.

    Scope limit: this is a coarse belt-and-suspenders net that only tells a single-bar
    waterfall apart from a multi-row one. It counts EVERY bar-glyph run in the pole body
    above the prompt, so a genuine multi-step pole — or a single-Level-2-step pole that
    ALSO renders a Level-3 log-drill (each drill sub-row is a `█` run too) — reads as
    >=2 rows and PASSes even if it RE-introduced a timeline-shape over-claim. In practice
    the only catalog cause that trips `_CAUSE_TIMELINE_CLAIM_RE` is playwright-parallel,
    whose leaf carries `deeper: []` (no drill), so that inflation path is unreachable via
    the current catalog; the primary defense either way is the catalog cause text itself
    (`_FIX_META[...]['cause']`, pinned by test_blocking_path)."""
    name = "agent-prompt MEASURED CAUSE never asserts timeline steps the pole doesn't render"
    bad: list[str] = []
    checked = 0
    # `_pole_sections` returns each body WITHOUT its `## … Long pole N:` header line, so the
    # header must be recovered from the full report (positionally aligned — both iterate the
    # same `Long pole N` matches in order) to name the offending pole in the failure message.
    headers = re.findall(r"^##\s+.*?(Long pole \d+:.*)$", report, re.MULTILINE)
    for i, body in enumerate(_pole_sections(report), start=1):
        pi = body.find("🤖 Prompt for your coding agent")
        if pi < 0:
            continue
        above, prompt = body[:pi], body[pi:]  # the waterfall renders ABOVE the prompt
        m = re.search(r"THE MEASURED CAUSE\s*\n(.*?)(?:\n\s*\n|\Z)", prompt, re.S)
        if not m or not _CAUSE_TIMELINE_CLAIM_RE.search(m.group(1)):
            continue
        checked += 1
        step_rows = sum(1 for ln in above.splitlines() if _WATERFALL_BAR_RE.search(ln))
        if step_rows < 2:
            hdr = headers[i - 1].strip() if i - 1 < len(headers) else f"pole {i}"
            bad.append(f"{hdr} (waterfall renders {step_rows} step row(s))")
    if bad:
        return Check(name, False,
                     "MEASURED CAUSE claims the finding is visible as sequential steps in "
                     "the timeline above, but the pole's rendered waterfall shows <2 steps: "
                     + "; ".join(bad[:3]) + " - the cause asserts a timeline shape the "
                     "report contradicts (fix the `_FIX_META` cause to describe the LOG "
                     "evidence, not the rendered timeline)")
    if checked:
        return Check(name, True, f"{checked} MEASURED CAUSE(s) reference the timeline; each "
                     "pole renders >=2 step rows to back it")
    return Check(name, True, "no MEASURED CAUSE points at the rendered timeline's step "
                 "structure", skipped=True)


# Verbatim copy of blocking_path._PENDING_CAVEAT_PATTERNS (verify_report is
# standalone by design — no skill imports); kept honest by the source-coupling
# pin in test_verify_report_self.py, like the other engine constants.
_VR_PENDING_CAVEAT_PATTERNS = frozenset(
    {"OPT32", "OPT33", "OPT34", "OPT39", "OPT40", "OPT47"})

# The load-bearing markers of the §8.1 required-check "Pending" caveat every
# skip-family / trigger-scope prompt must carry. Each pins one element of the
# contract: the Pending mechanism, the documented-safe job-level `if:` shape,
# the twin-workflow trick labeled community-workaround-NOT-docs, and the
# UNKNOWN-is-required rule.
_VR_PENDING_CAVEAT_MARKERS = (
    "required-status 'Pending' landmine",
    "documented-safe shape is a job-level `if:`",
    "community-known workaround,\nNOT in current GitHub docs",
    "Treat required-status UNKNOWN as required",
)

_VR_PROMPT_PATTERN_RE = re.compile(r"^Pattern: (OPT\d+) - ", re.MULTILINE)


def check_skip_family_prompts_carry_pending_caveat(report: str) -> Check:
    """§8.1 landmine 1, finally a contract: every rendered agent prompt for a
    skip-family / trigger-scope pattern (a fix that adds paths:/branches:/
    types: filters or skip conditions) must carry the required-check "Pending"
    caveat — the mechanism, the documented-safe job-level `if:` shape, the
    twin-workflow trick labeled as a community workaround (never docs-cited),
    and the UNKNOWN-is-required rule. A skip lever shipped as a "quick win"
    without it can permanently block merges on repos with required checks."""
    name = "Skip-family prompts carry the required-check Pending caveat"
    fences = re.findall(r"```text\n(.*?)```", report, re.DOTALL)
    prompt_blocks = [b for b in fences if _VR_PROMPT_PATTERN_RE.search(b)]
    family_blocks = [
        (m.group(1), b) for b in prompt_blocks
        for m in [_VR_PROMPT_PATTERN_RE.search(b)]
        if m and m.group(1) in _VR_PENDING_CAVEAT_PATTERNS
    ]
    if not family_blocks:
        return Check(name, True, "no skip-family/trigger-scope prompt rendered",
                     skipped=True)
    bad: list[str] = []
    for pat, block in family_blocks:
        missing = [mk.replace("\n", " ") for mk in _VR_PENDING_CAVEAT_MARKERS
                   if mk not in block]
        if missing:
            bad.append(f"{pat}: prompt missing caveat marker(s) {missing[:2]}")
    return Check(name, not bad,
                 f"{len(family_blocks)} skip-family prompt(s) carry the Pending caveat"
                 if not bad else "; ".join(bad[:6]))


def check_headline_names_wall_clock(report: str) -> Check:
    """The title + Bottom line must name the wall-clock story - the axis the report
    ranks. So a reader can't mistake which number sorts the report. Two valid titles:
    the merge-gate question, or - when the spine is the PR-FLOOR fallback (no file-backed
    required gate) - the honest "why is CI slow on a PR?" the renderer swaps in."""
    name = "headline names the wall-clock (merge-wait) axis"
    title = _title(report).lower()
    m = re.search(r"^>\s*\*\*Bottom line\.\*\*(.*)$", report, re.MULTILINE)
    bottom = (m.group(1).lower() if m else "")
    if not bottom:
        return Check(name, False, "no `> **Bottom line.**` headline")
    if not ("why is the merge slow" in title or "why is ci slow on a pr" in title):
        return Check(name, False, f"title unexpected: {_title(report)!r}")
    ok = any(p in bottom for p in ("checks to finish", "merge", "wall"))
    return Check(name, ok, "headline names the merge-wait axis" if ok
                 else f"headline names no wall-clock/merge axis: {bottom[:60]!r}")


def check_also_noticed_count_honest(report: str) -> Check:
    """When the TOC advertises 'Also noticed - N findings', N must equal what the
    appendix accounts for: the rows shown PLUS the '+K more' pointer. A miscount
    would silently over- or under-state the off-path hygiene set."""
    name = "TOC 'Also noticed' count == appendix rows + hidden pointer"
    # Match all pointer wordings: the legacy "N off-path hygiene findings", the PR-3
    # residual "N additional hygiene findings", and the Class A #7 reframed
    # "N findings (mostly off-path …; one or more DO sit on the critical path)". The count
    # extraction must not silently SKIP just because the label is honest for its section shape.
    m = re.search(r"\*\*🧹 Also noticed\*\*\s*-\s*(\d+)\s+(?:(?:off-path|additional) hygiene )?finding", report)
    if not m:
        return Check(name, True, "no Also-noticed appendix in this report", skipped=True)
    advertised = int(m.group(1))
    body = _section(report, "🧹 Also noticed")
    # Each shown pattern is a <details> block in the "Also noticed" appendix.
    shown = len(re.findall(r"<summary>", body))
    hidden = 0
    mh = re.search(r"\+(\d+)\s+more hygiene pattern", body)
    if mh:
        hidden = int(mh.group(1))
    ok = advertised == shown + hidden
    return Check(name, ok, f"{advertised} == {shown} shown + {hidden} hidden" if ok
                 else f"TOC says {advertised} but appendix has {shown}+{hidden}")


# The Data sources footer's `job logs` row: `| job logs | <coverage> | <used-for> |`. The coverage
# cell is the middle column. Keyed on the row LABEL the renderer emits ("job logs") and the count
# phrasing ("job log(s) sampled") - both pinned by the renderer-literal coupling test (L7), so a
# reword of the row breaks that test rather than silently turning this re-derivation into a SKIP.
_DS_JOB_LOGS_ROW_RE = re.compile(r"^\|\s*job logs\s*\|\s*(.+?)\s*\|", re.MULTILINE)


def _job_logs_count_violation(report: str, findings_path: Path | None) -> tuple[str | None, str]:
    """Re-derive how many job logs were ACTUALLY fetched and assert the Data sources footer's
    `job logs` Coverage cell doesn't assert a fetch that never happened. Returns
    `(violation_or_None, coverage_note)` so the caller folds the verdict into its detail (L8: a
    skip/pass must state its reason, never read blank-clean).

    Ground truth is re-derived from `findings.json`, MIRRORING the renderer's own keying EXACTLY
    (L3): `data_sources.logs_fetched` when it is an int (a genuine `0` included), else the persisted
    `data_bundle.logs` manifest length. The assertion is EXACT for an explicit rendered count (a
    deterministic integer - L6) and, for the bare "fetched" wording, a directional contradiction: it
    asserts a positive-but-unknown fetch, so it is dishonest IFF zero logs were fetched. A genuine
    zero must render "none" (the `f"… job log(s) sampled" if logs_n else "none"` branch in
    blocking_path's `_data_sources_footer`); the old falsy-int `"fetched"` branch overstated it
    (Tesorio/django-anon: `logs_fetched=0` rendered "fetched")."""
    if not findings_path:
        return None, ""
    m = _DS_JOB_LOGS_ROW_RE.search(report)
    if not m:
        return None, ""
    cell = _strip_render_artifacts(m.group(1)).lower()
    if cell == "not run":
        return None, "; job-logs tier not run"
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    top = _as_dict(data)
    logs_n = _as_dict(top.get("data_sources")).get("logs_fetched")
    if not isinstance(logs_n, int):   # mirror the renderer's fallback to the persisted bundle manifest
        logs_n = len(_as_list(_as_dict(top.get("data_bundle")).get("logs")))
    mc = re.search(r"(\d+)\s+job log", cell)   # an explicit "N job log(s) sampled" count
    if mc:
        claimed = int(mc.group(1))
        if claimed != logs_n:
            return (f"Data sources 'job logs' cell claims {claimed} log(s) but {logs_n} were "
                    "fetched (re-derived from data_sources.logs_fetched / data_bundle.logs)"), ""
        return None, f"; job-logs count honest ({logs_n} fetched)"
    # No explicit count: a bare "fetched" asserts a positive-but-unknown fetch - honest only if a log
    # was actually fetched. A genuine zero must read "none", not "fetched" (the bug this catches).
    if "fetched" in cell and logs_n == 0:
        return ("Data sources 'job logs' cell reads 'fetched' but 0 job logs were fetched - it "
                "asserts a fetch that never happened (a genuine zero must read 'none')"), ""
    return None, f"; job-logs coverage honest ({logs_n} fetched)"


def _gh_errors_disclosure_violation(report: str, findings_path: Path | None) -> tuple[str | None, str]:
    """Require rendered disclosure when collection recorded failed GitHub calls."""
    if not findings_path:
        return None, ""
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    data_sources = _as_dict(_as_dict(data).get("data_sources"))
    errors = data_sources.get("gh_error_count")
    if isinstance(errors, bool) or not isinstance(errors, int) or errors <= 0:
        return None, "; gh API failures absent"
    plain = _strip_render_artifacts(report).lower()
    if str(errors) in plain and "gh api" in plain and "failed" in plain:
        return None, f"; gh API failure disclosure honest ({errors} failed)"
    return (
        f"findings record {errors} gh API error(s) but the report does not disclose "
        "failed gh API calls"), ""


def _detectors_skipped_violation(report: str,
                                 findings_path: Path | None) -> tuple[str | None, str]:
    """A detector that could not be evaluated must be NAMED in the report.

    `collect_runs` skips the run-elimination family for a workflow whose run list it
    could not fetch (rather than sizing it against a laundered empty page and rendering
    "0 of 0 runs" as clean). That refusal only helps if the reader is told: an absent
    finding and a finding that found nothing look identical on the page. So when
    `data_sources.detectors_skipped` is non-empty, the rendered report MUST name every
    affected workflow — file name and detector ids both, since "some detectors were
    skipped somewhere" is not a disclosure a reader can act on."""
    if not findings_path:
        return None, ""
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    skipped = _as_dict(_as_dict(data).get("data_sources")).get("detectors_skipped")
    if not isinstance(skipped, list) or not skipped:
        return None, "; no detectors skipped"
    plain = _strip_render_artifacts(report)
    missing: list[str] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        wf = str(entry.get("workflow") or "")
        dets = [str(d) for d in _as_list(entry.get("detectors"))]
        if not wf or not dets:
            continue
        named = _wf_base(wf) in plain and all(d in plain for d in dets)
        if not named:
            missing.append(f"{_wf_base(wf)} ({'/'.join(dets)})")
    if missing:
        return (
            "findings record detectors that were never evaluated, but the report does "
            f"not name them: {', '.join(missing)} — an absent finding reads as clean, "
            "so an undisclosed skip is a silent false negative"), ""
    return None, f"; {len(skipped)} skipped-detector workflow(s) named"


def check_coverage_disclosed(report: str, findings_path: Path | None = None) -> Check:
    """The report must disclose its data basis (a provenance block or the Data
    sources footer), any incomplete-coverage banner must name the unscanned file(s)
    - an unscanned file may never read as clean (no silent drops) - AND the Data
    sources `job logs` Coverage cell must faithfully reflect how many job logs were
    actually fetched: a genuine zero reads "none", never a bare "fetched" that asserts
    a fetch that never happened (re-derived from findings, see `_job_logs_count_violation`)."""
    name = "data basis disclosed; coverage gaps named, not silent"
    disclosed = ("Where this data comes from" in report
                 or "## 🗄️ Data sources" in report)
    if not disclosed:
        return Check(name, False, "no provenance block or Data sources footer")
    # The provenance table must not assert a log fetch that never happened (re-derived from findings).
    logs_violation, logs_note = _job_logs_count_violation(report, findings_path)
    if logs_violation:
        return Check(name, False, logs_violation)
    gh_violation, gh_note = _gh_errors_disclosure_violation(report, findings_path)
    if gh_violation:
        return Check(name, False, gh_violation)
    skip_violation, skip_note = _detectors_skipped_violation(report, findings_path)
    if skip_violation:
        return Check(name, False, skip_violation)
    if "Incomplete coverage" in report:
        banner = _section_quote(report, "Incomplete coverage")
        if "**" not in banner:
            return Check(name, False, "Incomplete-coverage banner names no file")
        return Check(name, True, "coverage gap disclosed and files named"
                     + logs_note + gh_note + skip_note)
    return Check(name, True, "data basis disclosed" + logs_note + gh_note + skip_note)


# The two `data_sources` lists naming workflows that left the MEASURED sample: the
# run-list fetch failed (no runs), or every per-run job fetch failed (runs, but no job
# timing — so its checks would fall back to queue-inflated check-run spans). Both mean
# the same thing to a reader: the workflow is absent, and the critical path above was
# computed from the survivors.
_SAMPLE_GAP_KEYS = ("run_list_fetch_failures", "job_fetch_failures")


def check_run_list_gaps_named(report: str, findings_path: Path | None = None) -> Check:
    """A workflow whose RUN-LIST fetch failed — or whose per-run JOB fetches ALL
    failed — is MISSING from the sample, and the critical path is then computed from
    the SURVIVORS. If the vanished one was the merge gate, the report headlines a
    confident, WRONG gate. So: if either gap list is non-empty, the rendered report
    MUST NAME each affected workflow.

    This is the invariant that makes the bug it was written for uncatchable again.
    That bug: `collect()` built the by-name disclosure, then an unconditional
    re-stamp 200 lines later overwrote `partial_reason` with a bare error COUNT ("1
    gh API call(s) failed during collection") — which reads as a rounding error and
    satisfied every existing guard, including `_gh_errors_disclosure_violation`
    (count + "gh api" + "failed"). The helper-level unit test of the disclosure passed
    the whole time, because it tested a function the artifact never used. Report-level,
    re-derived from the DATA: the only thing that could have caught it.

    FAILS CLOSED on a MISSING key, not just a malformed one, whenever the gh tier ran.
    It used to skip — and since the stamps were only written on the main path, the
    invariant SKIPPED on every committed worked example and executed only in its own
    unit tests. A guard that skips on the whole corpus is a guard that isn't there;
    `collect()` now stamps both keys on every exit, so their absence from a gh-tier run
    means something wrote `data_sources` without going through the disclosure."""
    name = "workflows missing from the sample are NAMED in the report"
    if not findings_path:
        return Check(name, True, "no findings to compare", skipped=True)
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    ds = _as_dict(_as_dict(data).get("data_sources"))
    absent = [k for k in _SAMPLE_GAP_KEYS if k not in ds]
    if absent:
        if not ds.get("gh_available"):
            # The gh tier never ran (no --repo, gh missing): there is no sample to have
            # gaps in. Nothing to check, and nothing hidden.
            return Check(name, True, "gh tier did not run", skipped=True)
        return Check(name, False,
                     f"the gh tier ran but data_sources omits {', '.join(absent)} — the "
                     "sample-gap stamps are what tell a reader a whole workflow left the "
                     "audit, and a missing stamp makes this invariant vacuous. Failing "
                     "closed: every collect() exit must stamp them, even when empty")
    missing: set[str] = set()
    for k in _SAMPLE_GAP_KEYS:
        gaps = ds.get(k)
        if not isinstance(gaps, list):
            return Check(name, False,
                         f"{k} is not a list ({type(gaps).__name__}) — "
                         "malformed stamp, failing closed")
        missing |= {str(_as_dict(g).get("workflow_file")) for g in gaps
                    if _as_dict(g).get("workflow_file")}
    if not missing:
        return Check(name, True, "no workflow dropped out of the sample")
    plain = _strip_render_artifacts(report)
    unnamed = sorted(w for w in missing if w not in plain)
    if unnamed:
        return Check(name, False,
                     f"{len(unnamed)} workflow(s) whose sample fetch FAILED are absent "
                     f"from the sample but are NOT named in the report "
                     f"({', '.join(unnamed[:3])}) — the audit's critical path is computed "
                     "from the survivors, so a vanished merge gate would be headlined as "
                     "a confident, wrong gate under a disclosure that reads like a "
                     "rounding error")
    return Check(name, True,
                 f"all {len(missing)} workflow(s) missing from the sample are named "
                 f"({', '.join(sorted(missing)[:3])})")


def check_prefetch_plan_consumed(report: str, findings_path: Path | None = None) -> Check:
    """§2.2: the parallel gh pass prefetches only endpoints its call sites are certain to
    request, so `data_sources.prefetch_unconsumed` must be 0.

    A non-zero count means a fetch plan drifted from its call site: the run paid for gh
    calls the serial path would never have made (wasted rate-limit budget on a token whose
    secondary limit is exactly what the fetch governor exists to respect). It does NOT
    corrupt the measurements — a prefetch changes *when* a call is issued, never which one
    or what comes back — so this fails the report rather than voiding its data, and the
    rendered artifact must SAY so (the renderer emits a prefetch-drift line) rather than
    burying it in a stderr warning nobody kept.

    Skipped, not passed, on a pre-stamp artifact: the committed worked examples were
    collected before this key existed, and a missing key must never read as a clean zero."""
    name = "the fetch plan matches its call sites (no unconsumed prefetches)"
    if not findings_path:
        return Check(name, True, "no findings to compare", skipped=True)
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    ds = _as_dict(_as_dict(data).get("data_sources"))
    if "prefetch_unconsumed" not in ds:
        return Check(name, True, "pre-stamp artifact (no prefetch_unconsumed key)",
                     skipped=True)
    n = ds.get("prefetch_unconsumed")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return Check(name, False,
                     f"prefetch_unconsumed is not a count ({n!r}) — malformed stamp, "
                     "failing closed")
    if n == 0:
        return Check(name, True, "every prefetched response was consumed")
    if "Prefetch drift" not in report:
        return Check(name, False,
                     f"{n} prefetched gh response(s) were never consumed AND the report "
                     "does not disclose it — a drifted fetch plan, rendered as clean")
    return Check(name, False,
                 f"{n} prefetched gh response(s) were never consumed — a fetch plan "
                 "drifted from its call site (the run paid for gh calls the serial path "
                 "would not have made; the measured data is unaffected)")


def check_cost_spine_shallow_disclosed(report: str, findings_path: Path | None = None) -> Check:
    """§5.5/G15: when the bill-pole fetch loop leaves non-selected cost-spine
    workflows at the shallow sample, `collect_runs` stamps their names in
    `data_sources.cost_spine_shallow_workflows` — and the rendered report must
    carry the report-level shallow-sample disclosure, or shallow runner-minute
    coverage silently reads as exact. The expected sentence's count is
    RE-DERIVED from the stamped NAMES list (the count stamp is derived data,
    held coherent with the list), and matched digit-anchored so "1 …" can never
    hide inside "11 …". The #169 gh-API-failure disclosure has had this guard
    since it shipped (`_gh_errors_disclosure_violation`); this closes the same
    hole for the #174 disclosure."""
    name = "shallow cost-spine sample is disclosed (re-derived from the stamped workflows)"
    if not findings_path:
        return Check(name, True, "no findings to compare", skipped=True)
    try:
        data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    ds = _as_dict(_as_dict(data).get("data_sources"))
    if "cost_spine_shallow_workflows" not in ds:
        return Check(name, True,
                     "pre-stamp artifact (no cost_spine_shallow_workflows key)",
                     skipped=True)
    wfs = ds.get("cost_spine_shallow_workflows")
    if not isinstance(wfs, list):
        return Check(name, False,
                     f"cost_spine_shallow_workflows is not a list "
                     f"({type(wfs).__name__}) — malformed stamp, failing closed")
    names = [str(w) for w in wfs if str(w).strip()]
    if not names:
        return Check(name, True, "no cost-spine workflow left shallow (stamp empty)",
                     skipped=True)
    n = len(names)
    count_stamp = ds.get("cost_spine_shallow_workflow_count")
    if isinstance(count_stamp, bool) or not isinstance(count_stamp, int) or count_stamp != n:
        return Check(name, False,
                     f"stamp drift: cost_spine_shallow_workflow_count={count_stamp!r} but "
                     f"the names list carries {n} workflow(s) — collect_runs writes the "
                     "pair together; the renderer renders the count while this check "
                     "re-derives from the names, so an incoherent pair fails closed")
    # Same coherence bar for the sample depth: the collector stamps `shallow_runs`
    # on every run that can leave a workflow shallow, so a non-empty names list
    # with a missing/malformed depth is a hand-tampered artifact — and mirroring
    # the renderer blindly would bless a "shallow None-run" disclosure as
    # compliant (greptile P2 on this PR). Fail closed instead.
    shallow_runs = ds.get("shallow_runs")
    if isinstance(shallow_runs, bool) or not isinstance(shallow_runs, int):
        return Check(name, False,
                     f"stamp drift: shallow_runs={shallow_runs!r} while {n} workflow(s) "
                     "are stamped cost-spine-shallow — the disclosure cannot state a "
                     "real sample depth, so the artifact fails closed")
    # Mirror the renderer's exact assembled segment (blocking_path's shallow_note);
    # the red/green fixtures render through the real renderer, so a reword there
    # breaks them rather than letting this literal drift silently.
    expected = (f"{n} runner-minute source workflow(s) still use a shallow "
                f"{shallow_runs}-run cost-spine sample")
    if re.search(r"(?<![0-9])" + re.escape(expected), report):
        return Check(name, True,
                     f"disclosure present; count {n} re-derived from the stamped names")
    return Check(name, False,
                 f"findings stamp {n} shallow cost-spine workflow(s) "
                 f"(e.g. {', '.join(names[:3])}) but the report renders no "
                 f"{expected!r} disclosure — shallow runner-minute coverage "
                 "would read as exact")


def _section_quote(report: str, marker: str) -> str:
    """The blockquote (`> …` lines) containing `marker`, through the blank line."""
    lines = report.splitlines()
    for i, ln in enumerate(lines):
        if marker in ln:
            out = []
            for j in range(i, len(lines)):
                if not lines[j].startswith(">"):
                    break
                out.append(lines[j])
            return "\n".join(out)
    return ""


def check_date_matches_filename(report: str, report_path: Path | None) -> Check:
    name = "scanned date matches the date in the filename"
    if report_path is None:
        return Check(name, True, "no filename to compare", skipped=True)
    fn = re.search(r"(\d{4}-\d{2}-\d{2})", report_path.name)
    if not fn:
        return Check(name, True, "filename carries no date", skipped=True)
    m = re.search(r"scanned\s+\*\*(\d{4}-\d{2}-\d{2})\*\*", report)
    if not m:
        m = re.search(r"Analyzer ran at `(\d{4}-\d{2}-\d{2})", report)
    if not m:
        return Check(name, False, "no scanned date found in the report")
    ok = m.group(1) == fn.group(1)
    return Check(name, ok, f"{m.group(1)} == {fn.group(1)}" if ok
                 else f"report scanned date {m.group(1)!r} != filename {fn.group(1)!r}")


# ci-secure's security framing that must NOT survive into a ci-speedup report.
# Phrase-precise: an audited repo may legitimately run zizmor, and that name will
# appear in a finding's evidence - that's the AUDITED REPO's content, not a leak.
_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"what an attacker (?:can|could) do", "attacker-capability prose label"),
    (r"attacker_scenario", "attacker_scenario field"),
    (r"ci-secure (?:skill|catalog|report|scan|finding|audit)", "ci-secure self-reference"),
    (r"blended under the same severity", "zizmor methodology text"),
    (r"\brun zizmor\b", "zizmor recommendation"),
)

# Every typographic dash the render boundary (_strip_emdashes) must flatten to an
# ASCII "-". A report that still carries any of these slipped past the sanitizer.
_TYPOGRAPHIC_DASHES = ("—", "–", "‒", "―", "−")


def check_no_typographic_dashes(report: str) -> Check:
    name = "report uses ASCII hyphens only (no em/en/figure/bar/minus dash)"
    found = sorted({g for g in _TYPOGRAPHIC_DASHES if g in report})
    return Check(name, not found, "clean - ASCII hyphens only"
                 if not found else
                 f"typographic dash(es) survived the render boundary: {found}")


_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,})([^`]*)$")   # opener: run + info string (no backtick)
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,})[ \t]*$")   # closer: delimiter-only line


def check_fences_balanced(report: str) -> Check:
    """Every fenced code block must open AND close, so a CommonMark fence walk over the report
    must NOT end still inside a fence. This is the defense-in-depth twin of the renderer's
    `_fence_safe`/`_safe_span` guards: those stop repo text (check/job/step names, verbatim
    log/YAML evidence) from ever emitting a stray ```` ``` ````, which would corrupt the report on
    GitHub AND desync THIS verifier's own `re.findall(r"```text\n(.*?)```")` split (the identical
    stray fence fools the safety net with the same input). If a fence breaks out, fail LOUD here
    rather than let the parser mis-split silently. The walk mirrors CommonMark closing-fence rules
    (a closer is a delimiter-ONLY line whose run is >= the opener's) so it stays consistent with the
    non-fence tracker used elsewhere in this file — NOTE for maintainers: if `_nonfence_markdown_lines`
    is present, reuse its fence-state walk here rather than keeping this second implementation."""
    name = "code fences are balanced (no stray ``` breaking out of a fence)"
    opener_len = 0            # 0 == not currently inside a fence
    opened = 0
    for line in report.splitlines():
        if opener_len:
            m = _FENCE_CLOSE_RE.match(line)
            if m and len(m.group(1)) >= opener_len:
                opener_len = 0        # closed
        else:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                opener_len = len(m.group(1))
                opened += 1
    if opener_len == 0:
        return Check(name, True, f"balanced - {opened} fenced block(s), all opened and closed")
    return Check(name, False,
                 "a ``` fence opened without a close (or repo text emitted a stray ```): the "
                 "report ends inside a fence, which corrupts it on GitHub and desyncs the "
                 "verifier's fence split — fix the renderer sink that leaked it")


def check_no_domain_leakage(report: str) -> Check:
    name = "no ci-secure template leakage (security-domain framing in ci-speedup prose)"
    hits = [why for pat, why in _LEAK_PATTERNS if re.search(pat, report, re.IGNORECASE)]
    return Check(name, not hits,
                 "clean - only audited-repo content references security tooling, if any"
                 if not hits else f"template leak: {', '.join(hits)}")


# --- checks (cross-reference, optional) --------------------------------------
def check_skill_commit_provenance(report: str, skill_repo: Path | None) -> Check:
    """Provenance: the skill code that produced this report must be inspectable here.

    TWO tokens, because the two are provenance for different things and only one of
    them survives this repo's squash-merge convention:

      `skill commit `<sha>``  — COLLECT-time, carried in findings.json. A report
          rendered on a branch records that branch's sha; the squash-merge then
          discards the commit, so the sha is unresolvable on `main` forever after.
          Real history, permanently unverifiable. It cannot be the gate.
      `scripts tree `<sha>``  — RENDER-time tree of `scripts/`. A squash-merge
          PRESERVES trees, so this token still resolves after the merge. It is
          `scripts/` rather than the whole skill dir because the skill dir contains
          `reports/`: stamping it would make a committed report change the tree it
          claims to have been produced under.

    The tree must equal HEAD's `scripts/` tree exactly — not merely be *some*
    ancestor's. That is deliberate and is what makes the spec's §9 staging rule
    mechanical: change `scripts/`, and every committed report is stale until
    re-rendered (free — re-rendering reads committed findings.json, zero gh calls).
    An ancestor-tree match would let a report from five commits ago vouch for itself.
    """
    name = "report's skill commit is HEAD or an ancestor of it (provenance)"
    # ANCHORED to the Data-sources footer row, not a loose scan of the whole document.
    # An unanchored `re.search` lets prose anywhere in the report supply a token; taking
    # the LAST loose match is no better, because the renderer emits boilerplate AFTER
    # the footer, so a line appended below it would outvote the real one — and would
    # override a `-dirty` footer, defeating the dirty-tree refusal. Coupled to the
    # renderer's footer label; `test_verify_report_self` pins the literal.
    ms = list(re.finditer(
        r"ci-speedup static scan \(skill commit `([0-9a-f]+)(-dirty)?`"
        r"(?:, scripts tree `([0-9a-f]+)(-dirty)?`)?\)", report))
    if not ms:
        # An INSTALLED skill copy (no git repo) stamps a content-hash provenance
        # form via the skills-CLI lockfile — `installed:<hash12>` — or the terminal
        # `installed:unversioned` when no lockfile entry exists (run.py
        # `_skill_lock_provenance`, issue #2). Neither is a git commit, so there is
        # nothing to resolve against a checkout; the footer's identity string IS the
        # honest provenance for a live/installed run (which verifies WITHOUT a
        # --skill-repo). But a COMMITTED worked example is always rendered from a
        # source checkout, so when --skill-repo is supplied the installed form is a
        # regression — hold those to a real, resolvable sha.
        # Same anchoring policy as the git-sha branch above (finditer + last match),
        # so both provenance forms share one rule and can't drift.
        inst_ms = list(re.finditer(
            r"ci-speedup static scan \(skill build "
            r"`(installed:(?:[0-9a-f]{12}|unversioned))`\)", report))
        inst = inst_ms[-1] if inst_ms else None
        if inst:
            if skill_repo is None:
                return Check(name, True, f"recorded {inst.group(1)} (installed skill "
                             "copy; content-hash provenance, no git ref to resolve)")
            return Check(name, False, f"recorded {inst.group(1)} but --skill-repo was "
                         "given: a committed/worked-example report must carry a "
                         "resolvable git sha, not an installed content-hash form "
                         "(re-render from a source checkout)")
        return Check(name, False, "no `skill commit` recorded in the Data sources footer")
    m = ms[-1]
    recorded, dirty = m.group(1), bool(m.group(2))
    # A `-dirty` tree was rendered from uncommitted code: the sha names the committed
    # tree, not the code that ran. Never usable as provenance.
    tree_dirty = bool(m.group(4))
    rec_tree = m.group(3) if (m.group(3) and not tree_dirty) else None
    if skill_repo is None:
        note = "recorded " + recorded + ("-dirty" if dirty else "")
        return Check(name, True, note + " (no --skill-repo to compare)", skipped=True)

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(skill_repo), *args],
                              capture_output=True, text=True, timeout=10)

    def _head_scripts_tree() -> str | None:
        """FULL 40-char tree sha, so a recorded token of any length compares by
        prefix against the real thing. Comparing two short shas both ways would
        accept a 40-char token that merely shares a 7-char prefix with HEAD."""
        for path in ("skills/ci-speedup/scripts", "scripts"):
            r = _git("rev-parse", f"HEAD:{path}")
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        return None

    try:
        head = _git("rev-parse", "--short", "HEAD").stdout.strip()
        known = _git("rev-parse", "--verify", "--quiet",
                     recorded + "^{commit}").returncode == 0
        is_ancestor = known and _git(
            "merge-base", "--is-ancestor", recorded, "HEAD").returncode == 0
        head_tree = _head_scripts_tree()
    except (OSError, subprocess.SubprocessError):
        # OSError covers PermissionError/NotADirectoryError (a `git` on PATH that
        # cannot be executed); SubprocessError covers TimeoutExpired. Neither may
        # crash the verifier.
        return Check(name, True, "git unavailable", skipped=True)

    exact = known and (head.startswith(recorded) or recorded.startswith(head))
    # Length floor before the prefix compare: without it a forged 1-char token
    # (`scripts tree `7``) prefix-matches a real `723eb05` and passes vacuously.
    # `git rev-parse --short` never emits fewer than 7 chars.
    tree_ok = bool(rec_tree and head_tree and len(rec_tree) >= 7
                   and head_tree.startswith(rec_tree))

    # THE TREE GATES WHENEVER IT IS PRESENT. `ok = exact or is_ancestor or tree_ok`
    # was wrong: a report recording any real ancestor commit passed regardless of its
    # tree, so a report generated on `main` kept vouching for itself forever after
    # `scripts/` changed — precisely the self-vouching this check exists to forbid,
    # and it silently voided the "re-render with the change" rule. The ancestor path
    # now serves ONLY its original purpose: pre-tree-token reports (rendered before
    # 2026-07-08) still verify exactly as they used to.
    if m.group(3):                       # a tree token is present (clean or dirty)
        ok = tree_ok
    else:                                # legacy artifact: commit-only provenance
        ok = exact or is_ancestor

    head_tree_short = (head_tree or "")[:7]
    detail = f"recorded {recorded}{'-dirty' if dirty else ''} vs HEAD {head}"

    if ok and tree_ok:
        detail += (f" (scripts tree {rec_tree} matches HEAD's {head_tree_short} - the "
                   "rendering code IS inspectable here, whether or not the commit "
                   "survived a squash)")
        if dirty:
            detail += "; commit recorded with uncommitted changes (flagged)"
        return Check(name, True, detail)
    if ok and exact:
        detail += " (HEAD; legacy report with no `scripts tree` token)"
        return Check(name, True, detail)
    if ok and is_ancestor:
        detail += (" (ancestor of HEAD; legacy report with no `scripts tree` token - "
                   "commit-only provenance, as before 2026-07-08)")
        return Check(name, True, detail)

    # --- failures, each naming its own cause ------------------------------------------
    if tree_dirty:
        return Check(name, False, detail + (
            " (rendered from a DIRTY `scripts/` tree, so the stamped sha names the "
            "committed tree rather than the code that ran - commit `scripts/`, then "
            "re-render)"))
    if rec_tree and head_tree is None:
        return Check(name, False, detail + (
            " (cannot resolve HEAD's `scripts/` tree in this checkout; is --skill-repo "
            "the repo root?)"))
    if rec_tree:
        stale = " even though its commit is an ancestor" if is_ancestor else ""
        return Check(name, False, detail + (
            f" (scripts tree {rec_tree} != HEAD's {head_tree_short}{stale} - `scripts/` "
            "changed since this report was rendered: re-render it, no gh calls needed)"))
    if not known:
        return Check(name, False,
                     f"recorded {recorded} is not a commit in this skill checkout "
                     f"(HEAD {head}) - squashed away, fabricated, or wrong sha; "
                     "re-render the report so it carries a `scripts tree` token")
    # Legacy report (no tree token) whose commit is real but on a divergent branch.
    return Check(name, False, detail + (
        " (NOT an ancestor of HEAD - recorded on a divergent branch? re-render the "
        "report so it carries a `scripts tree` token)"))


def check_rendered_patterns_exist(report: str, findings_path: Path | None) -> Check:
    """Every OPT-id rendered as a finding (the Also-noticed appendix) must be a real
    pattern in the findings JSON - the report never invents a finding. The
    manual-review checklist (OPT13/15/18) is static guidance, not a finding, so it's
    excluded."""
    name = "every rendered finding pattern exists in the findings JSON"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    json_pats = {str(f.get("pattern", "")) for f in _as_list(_as_dict(data).get("findings"))
                 if isinstance(f, dict)}
    # Finding patterns render as a <details> summary (the "Also noticed" appendix).
    # Manual-review patterns use a `- **[OPTnn]` bullet and are deliberately NOT
    # findings, so they're excluded.
    rendered = set(re.findall(r"<summary><strong>(OPT\d+)", report))
    fabricated = sorted(rendered - json_pats)
    return Check(name, not fabricated,
                 f"all {len(rendered)} rendered pattern(s) exist in the JSON"
                 if not fabricated else
                 f"rendered but absent from findings JSON: {', '.join(fabricated)}")


def check_data_driven_have_signal(findings_path: Path | None) -> Check:
    name = "every data-driven finding carries a measured signal (no unsupported claims)"
    if findings_path is None:
        return Check(name, True, "no --findings to inspect", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    bad, n = [], 0
    for f in _as_list(_as_dict(data).get("findings")):
        if isinstance(f, dict) and f.get("pattern_class") == "data-driven":
            n += 1
            if not str(f.get("measured_signal") or "").strip():   # str(): a non-str signal must not crash .strip()
                bad.append(f"{f.get('pattern')} ({f.get('id')})")
    if n == 0:
        return Check(name, True, "no data-driven findings in this run", skipped=True)
    return Check(name, not bad, f"all {n} data-driven findings carry a signal" if not bad
                 else f"missing measured_signal: {', '.join(bad)}")


def _pole_sections(report: str) -> list[str]:
    """Each `## … Long pole N: …` section body, up to the next `## ` (or end)."""
    out = []
    for m in re.finditer(r"^##\s+.*Long pole \d+:.*$", report, re.MULTILINE):
        rest = report[m.end():]
        nxt = re.search(r"^##\s", rest, re.MULTILINE)
        out.append(rest[: nxt.start()] if nxt else rest)
    return out


def _wf_base(wf: str) -> str:
    """Mirror of `blocking_path._wf_base`: a workflow path reduced to its basename, so two poles
    are compared for same-matrix membership on the FILE, not the full path. A matrix lives in one
    workflow file, so `_same_matrix` uses this to never fold two different files' legs together."""
    return str(wf).rsplit("/", 1)[-1]


def _matrix_paren_base(name: str) -> str | None:
    """Mirror of `blocking_path._matrix_base`: the job name BEFORE its `(matrix params)`,
    lowercased; None when the name carries no parenthesised params. Uses `_strip_scope`
    (the verifier's `_clean_label` twin) so a monorepo `@scope/` prefix is dropped first,
    exactly as the engine does. Named `_matrix_paren_base` (not `_matrix_base`) because this
    file already defines a DIFFERENT `_matrix_base` (a trailing-`(variant)` strip over an
    already-`_cmp_name`'d floor name) — these are distinct helpers; do not merge them."""
    m = re.match(r"(.+?)\s*\(", _strip_scope(name))
    return m.group(1).strip().lower() if m and m.group(1).strip() else None


def _same_matrix(a: str, b: str,
                 wf_a: str | None = None, wf_b: str | None = None) -> bool:
    """Mirror of `blocking_path._same_matrix` — are two check names legs of ONE job matrix?
    The renderer collapses sibling legs into a single rendered pole via this exact predicate
    (matrix-base match, or a long shared token prefix/suffix, or a single mid-string token
    diff on >=3 tokens), and a matrix lives in ONE workflow file. `_expected_drilled_poles`
    must use the SAME collapse the renderer does or it over-counts legs as distinct poles.
    Kept behavior-coupled to the engine by `test_same_matrix_stays_coupled_to_the_engine`;
    `_strip_scope` is the verifier's verbatim `_clean_label` twin (already pinned coupled),
    so this is a faithful copy. (verify_report is standalone — no blocking_path import.)"""
    if wf_a and wf_b and _wf_base(wf_a) != _wf_base(wf_b):
        return False
    ta = [t for t in re.split(r"[^a-z0-9]+", _strip_scope(a).lower()) if t]
    tb = [t for t in re.split(r"[^a-z0-9]+", _strip_scope(b).lower()) if t]
    if not ta or not tb or ta == tb:
        return ta == tb and bool(ta)
    ba, bb = _matrix_paren_base(a), _matrix_paren_base(b)
    if ba is not None and bb is not None:
        return ba == bb
    if abs(len(ta) - len(tb)) > 1:
        return False
    if len(ta) == len(tb) >= 3 and sum(1 for x, y in zip(ta, tb) if x != y) == 1:
        return True
    pre = suf = 0
    for x, y in zip(ta, tb):
        if x == y:
            pre += 1
        else:
            break
    for x, y in zip(reversed(ta), reversed(tb)):
        if x == y:
            suf += 1
        else:
            break
    return pre >= 2 or suf >= 2


def _expected_drilled_poles(findings_path: Path | None) -> int | None:
    """How many distinct gating CHECKS a speed report should drill, re-derived
    INDEPENDENTLY of the renderer from the findings - so a renderer selection/dedup
    bug (e.g. collapsing two distinct gating jobs into one pole) is caught when the
    two disagree. Collapses matrix-sibling legs with the renderer's OWN `_same_matrix`
    predicate (keyed on the check name + workflow file), counts the distinct groups
    among the FILE poles - a managed/external check with no workflow_file is never
    drilled - capped at a conservative floor of 2 (this only guards against dropping BELOW two
    poles; it is NOT the renderer's `_TOP_WORKFLOWS`, which is 5). Conservative: it merges exactly what
    the renderer's `by_matrix` loop merges, so it never demands MORE poles than the
    renderer legitimately collapses to. None when findings/poles are unavailable.

    Keying on the check name via `_same_matrix` (NOT the per-leg `job` field) is the fix
    for the matrix-leg over-count: a job whose legs are NAMED by their full variant
    (graphql-armor `Examples Node 18`..`24`, each surfacing as its own check AND its own
    `job`) carries a DISTINCT `job` per leg, so grouping on `job` counted 4 groups for a
    matrix the renderer collapses to ONE pole - a false 'pole silently dropped' FAIL. The
    `_same_matrix` token-diff rule collapses both that shape and the shared-`job` shape
    (`Python 3.9`..`3.13`) identically, matching the renderer leg-for-leg."""
    if not findings_path:
        return None
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    poles = _as_list(_as_dict(_as_dict(data).get("pr_critical_path")).get("poles"))
    reps: list[tuple[str, str]] = []  # (workflow_file, check) of each kept representative
    for p in poles:
        if not isinstance(p, dict):
            continue
        wf = str(p.get("workflow_file", "")).strip()
        if not wf:
            continue  # managed/external check (no editable workflow) - not drilled
        # The renderer collapses on the CHECK name (`by_matrix` over `poles[].check`); fall back
        # to `job` only when a pole carries no check, so an otherwise-keyless pole still groups.
        check = str(p.get("check") or p.get("job") or "")
        if any(_same_matrix(check, rep_check, wf, rep_wf) for rep_wf, rep_check in reps):
            continue
        reps.append((wf, check))
    return min(len(reps), 2) if reps else None  # 2 = a deliberate conservative floor (NOT _TOP_WORKFLOWS, =5)


def _all_events_scoped_poles(findings_path: Path | None) -> list[str]:
    """Re-derive the BLEND-bug offenders from the findings JSON: every pole on the PR
    critical path (`pr_critical_path.poles`) whose owning workflow was scoped to
    `all-events` in `per_workflow_timing[workflow_file].event_scope` while the
    pole itself uses workflow-job timing.

    Why this is a contradiction (the class this catches). A pole on the PR critical
    path is BY DEFINITION something a developer waits on to merge - so the engine must
    have measured it from that workflow's developer-facing event runs (`_crit_for`'s
    `event_scope` = `pull_request` / `pull_request_target` / `merge_group`). An
    `all-events` scope means `_developer_event` found NO developer event yet the
    workflow still surfaced as a PR pole - so its timing is BLENDED across triggers
    (push-to-default, schedule, ...), mixing post-merge runs no PR ever waits on into
    the PR wait. That is exactly the roboflow/supervision `pr-conflict-labeler` defect:
    a `push` + `pull_request_target` labeler scoped to `all-events` (pull_request_target
    was missing from `_DEVELOPER_EVENTS`), so its heavy post-merge push mode (a 120s
    retry sleep, gating zero merges) became a false "~2m on ~30% of PRs" bimodal gate.

    A pure re-derivation from the findings DATA (the engine's own per-workflow
    `event_scope` string, not the rendered prose). Three exemptions: (1) poles whose
    workflow has no `per_workflow_timing` entry (a genuinely-fileless / external check)
    carry no re-derivable scope - no blended in-repo measurement to flag; (2) a
    `pr_floor_push_fallback` pole, the DELIBERATE push-only floor synthesized for a
    PR-volume-less repo, is honestly `all-events` (no developer event exists) and is
    disclosed as the PR-floor, not a blended gate - flagging it would false-fail the very
    push-only reports the fallback supports. The exemption is the NARROW push flag, not
    the broad `pr_floor_fallback` (also on case-1/1b structural poles, which stay caught);
    (3) a `timing_source=pr_check_runs` pole whose P50 came from sampled PR check-runs
    because no developer-event workflow job sample was available. That exemption is
    allowed only when the pole carries no workflow-job drill metadata (`steps`,
    dominant fields, or `bimodal`); otherwise the report would be borrowing
    all-events job facts while claiming PR timing.
    Returns the offender labels (empty when clean)."""
    if not findings_path:
        return []
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return []
    poles = _as_list(_as_dict(data.get("pr_critical_path")).get("poles"))
    pwt = _as_dict(data.get("per_workflow_timing"))
    offenders: list[str] = []
    for p in poles:
        if not isinstance(p, dict):
            continue
        # A `pr_floor_push_fallback` pole is the DELIBERATE, disclosed PUSH-only floor synthesized for a
        # repo with NO PR-volume workflow at all (`_select_pr_floor_workflows`'s push fallback): it is
        # honestly an `all-events` measurement because the repo has no developer event to scope to, and
        # the report frames it as the PR-floor ("why is CI slow on a PR?"), not as a blended PR gate.
        # Exempt it — the opposite of the bug this guard targets (an UNdisclosed all-events pole
        # masquerading as a real PR gate). NOTE: exempt on the NARROW `pr_floor_push_fallback`, NOT the
        # broad `pr_floor_fallback` — the latter is ALSO stamped on case-1/1b structural poles (real,
        # drillable, PR-scoped) which CAN be genuinely event-blended and must stay caught here. Without
        # the exemption, the guard false-fails every push-only-repo report the fallback exists to support
        # (the spine is required by check_primary_section_present yet failed here — unsatisfiable gate).
        if p.get("pr_floor_push_fallback"):
            continue
        wf = p.get("workflow_file")
        scope = _as_dict(pwt.get(wf)).get("event_scope")
        if scope == "all-events":
            if p.get("timing_source") == "pr_check_runs":
                if not (p.get("steps") or p.get("dominant_step")
                        or p.get("dominant_category") or p.get("bimodal")):
                    continue
                offenders.append(
                    f"`{p.get('check')}` (workflow `{wf}` scoped all-events, "
                    "but check-run-timed pole carries workflow-job drill fields)")
                continue
            offenders.append(f"`{p.get('check')}` (workflow `{wf}` scoped all-events)")
    return offenders


def _push_floor_pole_keys(findings_path: Path | None) -> set[tuple[str, str]]:
    r"""`(workflow-basename, _cmp_name(check))` for each `pr_critical_path.poles[*]` flagged
    `pr_floor_push_fallback` — the genuine PUSH-only floor synthesized for a repo with no file-backed
    pole. Such a pole is synthesized from per-workflow timing and may legitimately carry NO per-step
    breakdown (the job has no sampled steps), so the stunted-pole guard must EXEMPT it (a bare body is
    honest here, not a malformed drill). Keyed so a rendered `## Long pole … \`wf\` ▸ check` header maps
    back to it. Empty set when none / unreadable — so a non-fallback report keeps the guard unchanged."""
    if not findings_path:
        return set()
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()
    keys: set[tuple[str, str]] = set()
    for p in _as_list(_as_dict(data.get("pr_critical_path")).get("poles")):
        if isinstance(p, dict) and p.get("pr_floor_push_fallback"):
            keys.add((_wf_base(str(p.get("workflow_file") or "")), _cmp_name(str(p.get("check") or ""))))
    return keys


def _triaged_pole_offenders(findings_path: Path | None) -> list[str] | None:
    """Re-derive the triaged-fast-pole contradiction from the findings JSON (never a
    rendered-text proxy). A workflow disclosed in `data_sources.triaged_fast_workflows`
    had its jobs deliberately NOT fetched (it "can't hold the merge pole"), so it must
    never surface as a drilled `pr_critical_path.poles[*]` — such a pole has no sampled
    job to decompose and renders BARE ("no captured log" + "NO CATALOG PATTERN MATCHED"),
    directly contradicting that disclosure. Offender ⇔ a pole whose `workflow_file` (the
    same field the renderer drills off) is in the triaged set. Returns the offender
    descriptions, [] when clean, or None when findings are unavailable/unreadable."""
    if not findings_path:
        return None
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    triaged = {str(w) for w in _as_list(
        _as_dict(_as_dict(data).get("data_sources")).get("triaged_fast_workflows"))}
    if not triaged:
        return []
    poles = _as_list(_as_dict(_as_dict(data).get("pr_critical_path")).get("poles"))
    out: list[str] = []
    for p in poles:
        if not isinstance(p, dict):
            continue
        wf = str(p.get("workflow_file") or "").strip()
        if wf and wf in triaged:
            out.append(f"`{p.get('check')}` (file `{wf}`, triaged-fast — jobs never fetched)")
    return out


def _crown_triaged_offender(findings_path: Path | None) -> str | None:
    """Re-derive the headline-crown-on-a-triaged-workflow contradiction from the findings JSON
    (never a rendered proxy). The crowned `pr_critical_path.critical_path_check` is what the
    report HEADLINES as the merge gate ("the slowest check a typical PR waits on"). If its
    producing workflow — the `workflow_file` the engine stamps on the matching `checks[*]` spine
    entry (the SAME field the renderer resolves the headline pole from, filled via the scanned
    job-graph fallback for a triaged workflow) — is in `data_sources.triaged_fast_workflows`, the
    report crowns as its headline a workflow it ALSO discloses as triaged-fast ("can't hold the
    merge pole", jobs never fetched). The headline pole then has no captured/sampled job to drill
    and dead-ends ("no captured log" / "NO CATALOG PATTERN MATCHED").

    This is the CROWN analog of `_triaged_pole_offenders`, which keys on `poles[*].workflow_file`:
    the crown lives in the `checks` spine and needn't appear in `poles` (the pole builders exclude
    triaged workflows), so a triaged crown escapes the poles-only guard. Returns the offender
    description, or None when clean / unavailable / the crown is genuinely fileless (an external
    bot — a legitimate no-drill headline, no workflow_file on its spine entry)."""
    if not findings_path:
        return None
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    cp = _as_dict(data.get("pr_critical_path"))
    crown = cp.get("critical_path_check")
    if not crown:
        return None
    triaged = {str(w) for w in _as_list(
        _as_dict(data.get("data_sources")).get("triaged_fast_workflows"))}
    if not triaged:
        return None
    crown_wf = ""
    for c in _as_list(cp.get("checks")):
        if isinstance(c, dict) and str(c.get("name") or "") == str(crown):
            crown_wf = str(c.get("workflow_file") or "").strip()
            break
    if crown_wf and _is_workflow_file(crown_wf) and crown_wf in triaged:
        return f"`{crown}` (file `{crown_wf}`, triaged-fast — jobs never fetched)"
    return None


# Matrix-name-template re-derivation (import-free copy of collect_runs' matcher; L7 — kept
# coupled to the engine by `test_vr_produces_check_mirrors_engine_degenerate_rule`). Mirrors
# `_name_template_regex` / `_name_template_is_degenerate` / the scanned check->job name match so
# the invariant below re-derives "does this workflow produce this check" the SAME way the engine
# binds a check to a file — never a rendered proxy.
_MATRIX_PLACEHOLDER_RE = re.compile(r"\$\{\{.*?\}\}")


def _vr_template_is_degenerate(template: str) -> bool:
    """Mirror of `collect_runs._name_template_is_degenerate`: a job `name:` that is ENTIRELY
    `${{...}}` placeholder(s) with no literal anchor compiles to the match-anything regex `^.+?$`,
    so it is evidence-free — it must NOT be read as proof a job produces a given check-run."""
    if "${{" not in template:
        return False
    return all(not p.strip() for p in _MATRIX_PLACEHOLDER_RE.split(template))


def _vr_job_produces_check(job_name: str, is_matrix: bool, check: str) -> bool:
    """True iff a workflow job with display `name:` `job_name` (matrix-flagged iff `is_matrix`)
    legitimately produces the check-run `check`, mirroring collect_runs' scanned check->job
    matching: exact display name, a NON-degenerate matrix-`name:` template (`${{...}}` -> `.+?`),
    or a static matrix-leg (`Name` -> `Name (leg…)`). A DEGENERATE all-placeholder template is
    refused — a match-anything `^.+?$` is not evidence of production (it would claim every
    managed/external check-run)."""
    if job_name == check:
        return True
    if "${{" in job_name:
        if _vr_template_is_degenerate(job_name):
            return False
        parts = _MATRIX_PLACEHOLDER_RE.split(job_name)
        return bool(re.match("^" + ".+?".join(re.escape(p) for p in parts) + "$", check))
    if is_matrix:
        return bool(re.match("^" + re.escape(job_name) + r"(?: \(.*\))?$", check))
    return False


def _external_check_misbound_offenders(findings_path: Path | None) -> list[str] | None:
    """Re-derive the managed/external-check-bound-to-a-workflow-file contradiction from
    findings.json (never a rendered proxy). A `pr_critical_path.poles[*]` with a real
    `workflow_file` + `job` but NO sampled job backing it (`timing_source != 'workflow_jobs'`)
    got that file from the SCANNED job graph — legitimate ONLY when some job in that workflow
    genuinely PRODUCES the check (a real in-repo check whose fast workflow was triaged out of
    job-fetching). It is FABRICATED when the check is a Netlify/CLA/app MANAGED check that
    appears in NO workflow YAML: the bug where a job whose `name:` is an entirely-placeholder
    matrix template (`${{ matrix.target }}`) compiles to a match-anything `^.+?$`, grabs the
    foreign check-run, and renders it as a file-backed long pole with a wrong-file agent prompt.

    Production is re-derived from `workflow_job_graph` with the engine's own name matching
    (`_vr_job_produces_check`), degenerate templates refused. A pole WITH sampled job timing
    (`timing_source == 'workflow_jobs'`) is skipped — its binding is timing-proven, so a
    degenerate-named-but-real matrix leg is never mis-flagged. Offender ⇔ a no-sampled-job
    file-backed pole whose bound workflow produces no job matching its check. Returns offenders,
    [] when clean, None when findings / the scanned job graph are unavailable (can't re-derive —
    surfaced as a SKIP by the caller so the coverage gap is never silent, L8)."""
    if not findings_path:
        return None
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    wjg = _as_dict(data.get("workflow_job_graph"))
    if not wjg:
        return None  # no scanned graph → can't re-derive production
    out: list[str] = []
    for p in _as_list(_as_dict(data.get("pr_critical_path")).get("poles")):
        if not isinstance(p, dict):
            continue
        wf = str(p.get("workflow_file") or "").strip()
        job = str(p.get("job") or "").strip()
        check = str(p.get("check") or "")
        if not (wf and job and _is_workflow_file(wf)):
            continue  # fileless/external poles carry no workflow_file+job — nothing mis-bound
        if str(p.get("timing_source") or "workflow_jobs") == "workflow_jobs":
            continue  # a real sampled job backs this pole — the binding is timing-proven
        jobs = _as_dict(wjg.get(wf))
        produced = any(
            _vr_job_produces_check(
                str(_as_dict(info).get("name") or jid),
                bool(_as_dict(info).get("matrix")), check)
            for jid, info in jobs.items())
        if not produced:
            out.append(f"`{check}` bound to `{wf}` ▸ `{job}`, but no job in that workflow "
                       f"produces it (a managed/external check mis-anchored via a match-anything "
                       f"matrix-`name:` template)")
    return out


# The renderer's own admission that a pole carries NO per-step breakdown at all - emitted by
# blocking_path.py ONLY in the genuinely-bare else-branch (no captured single-run timeline AND no
# sampled per-step decomposition). A shallow pole names its dominant step "from the sampled per-step
# decomposition" and a drilled pole shows a representative-run timeline, so NEITHER reaches this
# branch - making this literal an exact, false-positive-free bare signal. SKILL.md 5a says such a
# "timeline with no drill" must FAIL the gate. (Pinned to the renderer source by
# `test_phase0_literals_stay_coupled_to_the_renderer` so a reword breaks both sides in lockstep.)
_NO_PER_STEP_DRILL = "No per-step breakdown was captured"

# ── Aggregation-gate poles (issue #1) ────────────────────────────────────────────────────
# A success-aggregation gate is the trivial job that exists ONLY to `needs:` a set of real
# jobs so one check can be the single required status check (next.js' `thank you, build`:
# job `buildPassed`, `needs: [deploy-target, build, build-wasm, build-native]`, P50 3s).
# `blocking_path._agg_gate_shape` detects that shape and renders the honest upstream story
# INSTEAD of a drill + "optimize this step" prompt — so such a pole legitimately carries no
# agent prompt, and `check_speed_poles_complete`'s prompt requirement must not false-FAIL it.
# The exemption is NOT taken on the report's word: the shape is RE-DERIVED from findings.json
# (`workflow_job_graph` + `pr_critical_path`), and the rendered section must also show the
# renderer's aggregation framing — findings-derived structure AND honest rendering, both.
# The paired invariant lives in `check_aggregation_gate_poles_never_prescribe`.
# (Literals pinned to the renderer by `test_phase0_literals_stay_coupled_to_the_renderer`.)
_AGG_GATE_ROLE_MARKER = "**Aggregation gate"
_AGG_GATE_POINTER_MARKER = "**➡️ Where the wait actually is:**"
_VR_AGG_GATE_TRIVIAL_S = 30.0


def _agg_gate_pole_keys(findings_path: Path | None) -> set[tuple[str, str]]:
    """`{(wf_base, _cmp_name(check))}` for every `pr_critical_path.poles[*]` matching the
    aggregation-gate shape, re-derived from findings.json alone — mirroring
    `blocking_path._agg_gate_shape` (a) trivial P50, (b) terminal job whose transitive
    `needs:` closure (>= 2 jobs) covers every non-terminal job in its workflow, (c) no
    sampled step above the trivial threshold, (d) >= 1 upstream member with a measured check.

    Deliberately re-derived (never read off the rendered report): the render is what this
    guard is auditing. The renderer's additional carve-outs (a modal-chain member, or a pole
    that matched a log detector / carries a routed structural lever) can only make the
    renderer render MORE than this set — and those poles all carry a prompt, so they never
    reach the exemption's `bare` list anyway."""
    if not findings_path:
        return set()
    data, err = _load_findings_doc(findings_path)
    if err:
        return set()
    graph = _as_dict(data.get("workflow_job_graph"))
    cp = _as_dict(data.get("pr_critical_path"))
    checks = [_as_dict(c) for c in _as_list(cp.get("checks"))]
    out: set[tuple[str, str]] = set()
    for pole in _as_list(cp.get("poles")):
        pole = _as_dict(pole)
        if (_num(pole.get("p50_s")) or 0.0) > _VR_AGG_GATE_TRIVIAL_S:
            continue
        wf = str(pole.get("workflow_file") or "")
        jobs = _as_dict(graph.get(wf))
        if not jobs:
            continue
        check = str(pole.get("check") or "")
        jid = str(pole.get("job") or "")
        if jid not in jobs:
            cands = [k for k, meta in jobs.items()
                     if _vr_job_produces_check(str(_as_dict(meta).get("name") or k),
                                               bool(_as_dict(meta).get("matrix")), check)]
            if len(cands) != 1:
                continue
            jid = cands[0]
        needs_of = {k: [str(n) for n in _as_list(_as_dict(m).get("needs"))]
                    for k, m in jobs.items()}
        depended_on = {n for ns in needs_of.values() for n in ns}
        if jid in depended_on:
            continue
        closure: set[str] = set()
        frontier = list(needs_of.get(jid) or [])
        while frontier:
            n = frontier.pop()
            if n in closure or n not in jobs:
                continue
            closure.add(n)
            frontier.extend(needs_of.get(n) or [])
        if len(closure) < 2 or not {k for k in jobs if k in depended_on} <= closure:
            continue
        if any((_num(_as_dict(s).get("p50_s")) or 0.0) > _VR_AGG_GATE_TRIVIAL_S
               for s in _as_list(pole.get("steps"))):
            continue
        measured = any(
            str(c.get("workflow_file") or "") == wf
            and _vr_job_produces_check(
                str(_as_dict(jobs.get(j)).get("name") or j),
                bool(_as_dict(jobs.get(j)).get("matrix")),
                str(c.get("name") or c.get("check") or ""))
            for j in closure for c in checks)
        if measured:
            out.add((_wf_base(wf), _cmp_name(check)))
    return out


def check_aggregation_gate_poles_never_prescribe(
        report: str, findings_path: Path | None) -> Check:
    """An aggregation-gate pole must render the honest upstream story and NOTHING that tells
    the reader to optimize a job that runs no work (issue #1).

    Two directions, both re-derived from findings.json (`_agg_gate_pole_keys`):

    - A pole rendered with the aggregation framing must genuinely BE one structurally, and
      must carry the "where the wait actually is" pointer at the slowest upstream member —
      framing without the structure, or without the pointer, is a dead end.
    - A pole rendered with the aggregation framing must carry NO `🤖 Prompt for your coding
      agent`: that prompt asks the reader to capture timing and speed up a 3-second no-op,
      which is exactly the inert advice this shape exists to suppress. (The complementary
      exemption in `check_speed_poles_complete` lets such a pole ship without a prompt; this
      is the invariant that keeps the exemption from being a hole.)"""
    name = "aggregation-gate poles tell the upstream story, never an optimize-this prompt"
    framed = [(wf, check, body) for wf, check, body in _pole_header_sections(report)
              if _AGG_GATE_ROLE_MARKER in body]
    if not framed:
        return Check(name, True, "no aggregation-gate pole rendered", skipped=True)
    keys = _agg_gate_pole_keys(findings_path)
    if findings_path and not keys:
        return Check(name, False,
                     f"{len(framed)} pole(s) render the aggregation-gate framing, but findings.json "
                     "supports NO pole of that shape (re-derived from workflow_job_graph + "
                     "pr_critical_path) - the framing must never be applied to a pole that isn't "
                     "structurally a `needs:`-everything success sink")
    bad: list[str] = []
    for wf, check, body in framed:
        where = f"`{wf}` ▸ {check}"
        if findings_path and (_wf_base(wf), _cmp_name(check)) not in keys:
            bad.append(f"{where}: framed as an aggregation gate but findings don't re-derive "
                       "that shape for it")
        if "Prompt for your coding agent" in body:
            bad.append(f"{where}: carries an agent prompt over a job that runs no work - "
                       "the sink must point at its slowest upstream member instead")
        if _AGG_GATE_POINTER_MARKER not in body:
            bad.append(f"{where}: no upstream pointer - the reader is left with a role line "
                       "and nowhere to go")
    if bad:
        return Check(name, False, "; ".join(bad[:4]))
    return Check(name, True, f"{len(framed)} aggregation-gate pole(s): each re-derives from the "
                 "job graph, points at its slowest upstream member, and prescribes nothing")


def check_speed_poles_complete(report: str, findings_path: Path | None) -> Check:
    """The report must drill one fully-formed long pole per independent gating check
    (>=2 when >=2 gate), and EVERY pole must carry the same hand-off as the first - a
    `Prompt for your coding agent` - AND an actual per-step drill. Catches (a) a
    silently-dropped second pole (the regression where a 2-pole report rendered only
    1), (b) a bare/stunted pole - SKILL.md 5a defines that as "a timeline with no
    drill OR no prompt", so BOTH halves fail the gate: a pole whose body shows the
    renderer's own no-breakdown admission (`_NO_PER_STEP_DRILL`) carries no per-step
    drill at all (neither a captured single-run timeline nor a sampled per-step
    decomposition - a legit shallow pole names its dominant step and never reaches that
    branch), and a pole missing the agent prompt is asymmetric to pole 1, and (c) a
    BLENDED pole whose timing mixes non-PR (push/schedule) runs into the PR wait - a
    PR-critical-path pole scoped to `all-events` in `per_workflow_timing` (re-derived by
    `_all_events_scoped_poles`); (c) is the pull_request_target class: a `push` +
    `pull_request_target` workflow that found no developer event fell back to all-events
    scoping and fabricated a bimodal gate from post-merge push runs no PR waits on."""
    name = "report drills every gating pole, each with a hand-off prompt"
    # Triaged-fast-pole contradiction (findings-internal, re-derived — never a render proxy):
    # a workflow disclosed as triaged-fast ("can't hold the merge pole", jobs never fetched)
    # must NOT leak into the drilled poles as a bare, undrillable lever. Checked FIRST so the
    # static-only / fileless-note branches below can never launder this class into a SKIP.
    _triaged = _triaged_pole_offenders(findings_path)
    if _triaged:
        return Check(name, False,
                     "triaged-fast workflow(s) leaked into the critical-path poles as a bare, "
                     "undrilled long pole — its jobs were never fetched (disclosed in "
                     "data_sources.triaged_fast_workflows as 'can't hold the merge pole'), so it "
                     "renders 'no captured log' / 'NO CATALOG PATTERN MATCHED', contradicting the "
                     "coverage note: " + "; ".join(_triaged))
    # Headline-crown-on-a-triaged-workflow (findings-internal, re-derived). The CROWN analog of
    # the offender guard above: `_triaged_pole_offenders` keys on `poles[*].workflow_file`, but
    # the crowned `critical_path_check` lives in the `checks` SPINE (its workflow_file filled via
    # the scanned job-graph fallback) and needn't appear in `poles` at all — the pole builders
    # already EXCLUDE triaged workflows. So when every heavier pole is minority-present and the
    # crown falls to a sub-floor triaged lint, the report HEADLINES a workflow it also discloses
    # as "can't hold the merge pole", and that headline pole dead-ends ('no captured log' / 'NO
    # CATALOG PATTERN MATCHED') — the exact class the poles-keyed guard slips past. Checked here
    # (not folded into the static-only branches) so no note can launder it into a SKIP.
    _crown_off = _crown_triaged_offender(findings_path)
    if _crown_off:
        return Check(name, False,
                     "the headline critical_path_check maps to a workflow disclosed in "
                     "data_sources.triaged_fast_workflows ('can't hold the merge pole', jobs "
                     "never fetched) — so the crowned headline pole has no captured job to drill "
                     "and dead-ends ('no captured log' / 'NO CATALOG PATTERN MATCHED'), "
                     "contradicting the coverage note: " + _crown_off)
    # Managed/external-check-mis-bound-to-a-file contradiction (findings-internal, re-derived —
    # never a render proxy). A no-sampled-job pole whose bound workflow produces NO job matching
    # its check got its `workflow_file` from a match-anything (`^.+?$`) matrix-name template that
    # grabbed a foreign Netlify/CLA/app check — it renders as a bogus file-backed long pole with a
    # wrong-file agent prompt. Checked here (before static-only / the fileless-note branches) so no
    # note can launder it into a SKIP.
    _misbound = _external_check_misbound_offenders(findings_path)
    if _misbound:
        return Check(name, False,
                     "managed/external check(s) mis-anchored to a workflow file they aren't "
                     "produced by — a job whose entirely-placeholder matrix `name:` compiled to a "
                     "match-anything template grabbed a foreign check-run, rendering it as a "
                     "file-backed long pole with a wrong-file agent prompt: " + "; ".join(_misbound))
    if _is_static_only(report):
        # No run history -> no measurable critical path -> no pole to drill. The
        # static-only report is a legitimate outcome, not a dropped pole.
        return Check(name, True, "no-run-history report: no measurable pole to drill",
                     skipped=True)
    # (c) Blend guard, re-derived from the findings DATA before any report parsing: a
    # PR-critical-path pole scoped to `all-events` blends non-PR runs into the PR wait
    # (the pull_request_target class). A drilled pole that LOOKS complete is still
    # malformed if its timing is event-blended, so this fails ahead of the count/prompt
    # checks. (No --findings, or no all-events pole -> falls through unchanged.)
    blended = _all_events_scoped_poles(findings_path)
    if blended:
        return Check(name, False,
                     "PR-critical-path pole(s) scoped to all-events - non-PR (push/schedule) runs "
                     "are blended into the PR wait, fabricating a bimodal gate from post-merge runs "
                     "no PR waits on (pull_request_target / missing developer-event scoping): "
                     + "; ".join(blended))
    sections = _pole_sections(report)
    rendered = len(sections)
    expected = _expected_drilled_poles(findings_path)
    if rendered == 0:
        # Zero drilled poles. This is a LEGITIMATE all-fileless gate ONLY when every gating
        # check is managed/external with no editable workflow file - the renderer drills no
        # `## Long pole` (nothing to diff) and emits the "no editable workflow file" note. But
        # "rendered 0 + that note" is NOT sufficient to exempt on its own: the renderer's
        # `_is_file_pole` also classifies a pole that HAS a workflow_file but no `job` as
        # fileless, so a genuinely-dropped/mislabeled pole can reach this branch wearing the
        # note. Corroborate against the findings, which count a pole as drillable on its
        # `workflow_file` ALONE - so they disagree exactly on that dropped pole.
        if expected:
            # findings carry >=1 file-backed pole, yet the report drilled NONE: a drillable
            # gating pole was dropped or mislabeled fileless. This is the dropped-spine
            # false-negative the check exists to catch; the prose note must NOT launder it
            # into a SKIP. (A real all-fileless gate has no file-backed pole, so expected is
            # falsy and we never reach here.)
            return Check(name, False, f"findings support {expected} file-backed gating pole(s) "
                         "but the report drills NONE - a drillable pole was dropped or "
                         "mislabeled fileless (e.g. a pole with a workflow_file but no job; "
                         "re-run this gate against the NEW artifacts after a regen)")
        if "no editable workflow file" in report:
            # findings agree there is nothing file-backed to drill (expected is None), and the
            # renderer emitted the fileless note - a genuine all-managed/external gate. When no
            # --findings was passed `expected` is also None, so this falls back to trusting the
            # note as the only available signal (the sibling `check_primary_section_present`
            # keys on the same literal - keep them coupled).
            return Check(name, True, "all gating checks are managed/external (no editable "
                         "workflow file); findings carry no file-backed pole - no pole to drill",
                         skipped=True)
        return Check(name, False, "speed report has no '## Long pole' section")
    if expected is not None and rendered < expected:
        return Check(name, False, f"findings support {expected} distinct gating pole(s) "
                     f"but the report drills only {rendered} - a pole was silently "
                     "dropped (re-run this gate against the NEW artifacts after a regen)")
    # Bare/stunted poles, EXCEPT a genuine push-only floor pole (`pr_floor_push_fallback`): that pole
    # is synthesized from push timing for a repo with no file-backed pole to drill, so a "no per-step
    # breakdown" body is HONEST (the job has no sampled steps), not a stunted drill — and failing it
    # here while check_primary_section_present REQUIRES the spine would be an unsatisfiable gate (the
    # push-only-repo contradiction). Mapped findings→render by `(wf-base, _cmp_name(check))`; only the
    # NARROW push flag is exempt, so a case-1/1b structural pole that renders bare is still caught.
    _push_floor = _push_floor_pole_keys(findings_path)
    stunted = [f"`{wf}` ▸ {check}"
               for wf, check, body in _pole_header_sections(report)
               if _NO_PER_STEP_DRILL in body
               and (_wf_base(wf), _cmp_name(check)) not in _push_floor]
    if stunted:
        return Check(name, False, f"long pole(s) {stunted} carry NO per-step breakdown - a "
                     "bare/stunted pole (a timeline with no drill, per SKILL.md 5a), not the "
                     "same drill as pole 1 (it hands off a prompt over an empty drill)")
    # AGGREGATION-GATE exemption (issue #1). A pole whose job exists only to `needs:` the
    # rest of its workflow (a 3s success sink) renders the honest upstream story INSTEAD of a
    # drill + prompt — a prompt there would tell the reader to speed up a job that runs no
    # work. Exempt ONLY when the shape re-derives from findings.json AND the section actually
    # carries the renderer's aggregation framing; `check_aggregation_gate_poles_never_prescribe`
    # holds the other half (such a pole must carry the upstream pointer and no prompt), so the
    # exemption can't launder a genuinely stunted pole.
    # Keyed by the section BODY, never by ordinal: `_pole_sections` and `_pole_header_sections`
    # split on the same header line and so yield byte-identical bodies, but their header
    # patterns differ (the latter also requires the `` `wf` ▸ check `` shape) — a header that
    # ever fails the stricter pattern would shift the ordinals and move the exemption onto a
    # DIFFERENT pole. Matching on bodies cannot drift that way.
    _agg_keys = _agg_gate_pole_keys(findings_path)
    _agg_exempt_bodies = {body for wf, check, body in _pole_header_sections(report)
                          if _AGG_GATE_ROLE_MARKER in body
                          and (_wf_base(wf), _cmp_name(check)) in _agg_keys}
    bare = [i + 1 for i, s in enumerate(sections)
            if "Prompt for your coding agent" not in s and s not in _agg_exempt_bodies]
    if bare:
        return Check(name, False, f"long pole(s) {bare} lack an agent prompt - a bare/"
                     "stunted pole, not the same drill as pole 1")
    exp = f" (matches {expected} expected from findings)" if expected is not None else ""
    return Check(name, True, f"{rendered} pole(s){exp}, each drilled and handing off via a prompt")


def _pole_header_sections(report: str) -> list[tuple[str, str, str]]:
    r"""[(workflow_base, check, section_body), …] for each `## … Long pole N: \`WF\` ▸ CHECK`
    section — the header's own (wf, check) plus its body up to the next `## `."""
    out: list[tuple[str, str, str]] = []
    pat = re.compile(r"^##\s+.*Long pole \d+:\s*`([^`]+)`\s*▸\s*(.+)$", re.MULTILINE)
    for m in pat.finditer(report):
        wf = m.group(1).strip()
        # The check is everything after ▸ with ONLY the trailing " — DUR" stripped. The renderer
        # appends the duration as `... ▸ {check} — {_clock(secs)}`, and `_strip_emdashes` flattens
        # that em-dash to an ASCII ' - ' at the render boundary — so in the final report the dur
        # separator is INDISTINGUISHABLE by glyph from a hyphen inside the check name itself.
        # Splitting on the FIRST dash therefore truncated a hyphenated check ("Build - Docker" ->
        # "Build"), breaking the (wf, check) key the cache invariants map poles by. Anchor instead
        # on `_clock`'s output shape ("51s" or "5m 00s") at END of the header line and strip only
        # that trailing " - DUR"; a real check name doesn't end in a clock duration.
        check = re.sub(r"\s+[—–-]\s+(?:\d+m\s+\d+s|\d+s)\s*$", "", m.group(2).strip()).strip()
        # The renderer wraps the check as an inline code span (`_safe_span`), so it arrives as
        # `\`build\``; peel the ONE wrapping backtick pair back to the bare NAME (the `_safe_span`
        # apostrophe-maps any interior backtick, so the only backticks left are the delimiters).
        # A hand-crafted BARE-check fixture has no pair, so this is a no-op there.
        if len(check) >= 2 and check[0] == "`" and check[-1] == "`":
            check = check[1:-1].strip()
        rest = report[m.end():]
        nxt = re.search(r"^##\s", rest, re.MULTILINE)
        out.append((wf, check, rest[: nxt.start()] if nxt else rest))
    return out


def _strip_render_artifacts(text: str) -> str:
    """Remove the GENERIC markdown decorations a fact-comparison must ignore, in ONE place so
    invariants don't each re-implement a slightly different strip (the inconsistency that bit
    Class A #5/#7). Strips emphasis backticks/asterisks, normalizes any stray typographic dash to
    an ASCII hyphen, and trims surrounding whitespace.

    SCOPE — this is the generic DECORATION strip only. The POSITIONAL/structural transforms are
    deliberately NOT folded in here, because they are applied at only some call sites and conflating
    them would change established comparator semantics: a leading `@scope/` monorepo prefix
    (`_strip_scope`), a trailing matrix `(variant)` (`_matrix_base`), and a `wf:line` suffix
    (`_strip_line_suffix`) each stay their own single-use named helper. A `+N more` truncation is a
    parse concern (handled by the `**Where:**` segment regexes' lookahead), not a strip."""
    s = re.sub(r"[`*]", "", str(text))
    for dash in _TYPOGRAPHIC_DASHES:
        s = s.replace(dash, "-")
    return s.strip()


def _strip_line_suffix(wf: str) -> str:
    """Drop a rendered `:line` suffix from a workflow path (`ci.yml:149` -> `ci.yml`). The appendix
    `**Where:**` line carries it; the pole header does not, so it must be stripped before the two
    are reconciled by basename (the only place this transform is needed)."""
    return re.sub(r":\d+$", "", str(wf))


def _norm_check(s: str) -> str:
    # Generic-decoration strip (shared) then case-fold. Behavior-identical to the prior inline
    # `re.sub(r"[`*]", "", s).strip().lower()` on renderer-clean (ASCII-hyphen) input; the added
    # dash-normalization only ever fires on a stray typographic dash, biasing toward MATCH —
    # consistent with `_cmp_name`'s documented catch-over-miss stance.
    return _strip_render_artifacts(s).lower()


# A pole section shows a CAPTURED-LOG drill (not a shallow sampled-decomposition render)
# iff it cites a representative run id or a cross-run check — both render only when a raw
# log was bound to that pole.
_DRILL_SIGNAL_RE = re.compile(r"\(representative run \d|\U0001f52c Cross-run check")


def check_pole_drill_belongs_to_its_job(report: str, findings_path: Path | None) -> Check:
    """No rendered pole may show a drill (representative-run timeline / cross-run check) for a
    job that wasn't actually drilled — the R1 cross-job log leak, where an undrilled pole
    sharing a workflow with a drilled one inherited the sibling's log/evidence/audit-link.
    Re-derived from the findings' OWN drill bundle (`data_bundle.logs`), independent of the
    renderer: a pole section that LOOKS drilled but whose (check, workflow) is not in the
    drilled set borrowed another job's log. Standalone (no blocking_path import)."""
    name = "each rendered pole's drill belongs to its own job (no cross-job log leak)"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no drill bundle to check",
                     skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    entries = _as_list(_as_dict(_as_dict(data).get("data_bundle")).get("logs"))
    if not entries:
        return Check(name, True, "no drill bundle captured — nothing drilled to mis-bind",
                     skipped=True)
    # `_cmp_name` (scope-stripped), NOT `_norm_check`: the pole HEADER name is already `_clean_label`'d
    # by the renderer (the `@scope/` prefix dropped), but `data_bundle.logs[].check` is stored
    # RAW/scoped by collect_runs — so a plain `_norm_check` compare never intersects for a monorepo
    # check (`@a/db build` vs the rendered `db build`), falsely flagging a CORRECT scoped pole as
    # borrowing a sibling's log. Every other rendered-vs-findings name join in this file uses
    # `_cmp_name` for exactly this reason (the #99 scoped-check class); this one was the lone exception.
    drilled = {(_cmp_name(str(e.get("check", ""))),
                Path(str(e.get("workflow_file", ""))).name) for e in entries if isinstance(e, dict)}
    offenders = []
    for wf, check, body in _pole_header_sections(report):
        if not _DRILL_SIGNAL_RE.search(body):
            continue  # shallow pole (sampled decomposition) — no captured drill to mis-bind
        if (_cmp_name(check), Path(wf).name) not in drilled:
            offenders.append(f"`{wf}` ▸ {check}")
    if offenders:
        return Check(name, False,
                     "pole(s) show a captured-run drill for a job that was NOT drilled "
                     "(borrowed a sibling's log — the R1 leak): " + "; ".join(offenders))
    return Check(name, True, f"all drilled-looking poles own their log "
                 f"({len(drilled)} drilled job(s))")


# --- gap-fill evidence grounding (issue #106) --------------------------------
# The phase-4a gap-fill has the driving agent READ a third-party job log and author an analysis
# JSON `{cause, breakdown, evidence, prompt}` that renders ~verbatim as a `🤖 LLM root-cause
# analysis` block (`blocking_path._llm_analysis_block`). "Ground it — every claim traces to lines
# you quote in `evidence`, copied verbatim from the log" is a PROSE rule in SKILL.md/gap-fill.md; a
# crafted log is a prompt-injection surface (a coverage-gap pole reads the raw log with the user's
# shell + gh token in scope). This check converts that ONE prose rule into a mechanical gate: every
# quoted evidence line the report renders must actually be a substring of the captured log it claims
# to quote. It cannot corrupt any measured number (those stay deterministic); it enforces that the
# agent's log READING is grounded, not fabricated. See SECURITY.md "Untrusted CI data reaches the
# driving agent". Both literals are pinned renderer<->verifier by test_verify_report_self.py.
_LLM_ANALYSIS_MARKER = "🤖 LLM root-cause analysis"           # `_llm_analysis_block` heading
# The evidence fence: the heading line ("**Evidence — verbatim from the captured job log:**", its
# em-dash flattened to a hyphen at the render boundary) then the ```text fence holding one
# `_fence_safe`'d line per evidence element. Anchor on the em-dash-free fragment so the match is
# glyph-agnostic; capture the fence body.
_LLM_EVIDENCE_FENCE_RE = re.compile(
    r"verbatim from the captured job log[^\n]*\n+```text\n(.*?)\n```", re.DOTALL)


def _ground_transform(text: str) -> str:
    """Apply to EACH LOG LINE the SAME transforms the renderer applies to an evidence line embedded
    in the report, so a substring compare is honest: `_fence_safe` (the verbatim twin of
    `blocking_path._fence_safe` — defuse >=3-backtick runs, strip control chars) then flatten every
    typographic dash to an ASCII hyphen (via `_TYPOGRAPHIC_DASHES`). The transform is applied
    PER LOG LINE, never to the whole collapsed log: whole-log newline collapse let fabricated text
    SPLICED across two adjacent log lines pass as "verbatim" (#110 bot review) — the gap-fill
    contract quotes verbatim log LINES, so each evidence line must be a substring of a single
    transformed log line. No third transform is invented — both are the renderer's own, already
    mirrored in this file."""
    corpus = _fence_safe(text)
    for dash in _TYPOGRAPHIC_DASHES:
        corpus = corpus.replace(dash, "-")
    return corpus


def check_gap_fill_evidence_grounded(report: str, findings_path: Path | None) -> Check:
    """Every quoted `evidence` line in a rendered `🤖 LLM root-cause analysis` block must be a
    SUBSTRING of the captured log that block claims to read (issue #106 — the injection residual's
    cheap backstop). The gap-fill lets the agent read an untrusted third-party log and author the
    block's prose; this gate makes "ground it in the quoted lines" enforceable instead of trusted.

    Offline: the captured logs live in the `.data` bundle at `data_bundle.logs_dir` / each
    `data_bundle.logs[].file`. A block is bound to its log by the SAME (check, workflow) identity
    `check_pole_drill_belongs_to_its_job` uses. When the log CAN be read and a quoted line is not
    found in it -> FAIL, naming the offending line. When the log is unreadable (moved scratch,
    logs_dir absent, legacy artifact, or the block's pole doesn't bind a log entry) -> loud SKIP,
    never a silent pass. A report with no gap-fill block has nothing to ground -> PASS."""
    name = "every 🤖 gap-fill evidence line is verbatim from the captured job log (issue #106)"
    blocks = [(wf, check, body) for wf, check, body in _pole_header_sections(report)
              if _LLM_ANALYSIS_MARKER in body and _LLM_EVIDENCE_FENCE_RE.search(body)]
    if not blocks:
        return Check(name, True, "no 🤖 gap-fill block renders evidence - nothing to ground")
    if not findings_path:
        return Check(name, True, f"{len(blocks)} gap-fill block(s) render evidence but no "
                     "--findings to locate the captured logs - cannot verify grounding",
                     skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, f"{err} - cannot locate the captured logs to verify grounding",
                     skipped=True)
    db = _as_dict(data.get("data_bundle"))
    logs_dir = db.get("logs_dir")
    entries = [e for e in _as_list(db.get("logs")) if isinstance(e, dict)]
    # Bind (check, workflow-basename) -> resolved log path, mirroring check_pole_drill_belongs_to_its_job.
    log_by_key: dict[tuple[str, str], str] = {}
    for e in entries:
        if e.get("file"):
            log_by_key[(_cmp_name(str(e.get("check", ""))),
                        Path(str(e.get("workflow_file", ""))).name)] = str(e["file"])
    offenders: list[str] = []
    unverifiable: list[str] = []
    for wf, check, body in blocks:
        pole = f"`{wf}` ▸ {check}"
        fn = log_by_key.get((_cmp_name(check), Path(wf).name))
        if not logs_dir or not fn:
            unverifiable.append(f"{pole} (no captured-log path in data_bundle.logs_dir/file)")
            continue
        log_path = Path(str(logs_dir)) / fn
        try:
            raw_log = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unverifiable.append(f"{pole} (captured log absent on disk: {log_path})")
            continue
        # Per-LINE corpus (see _ground_transform): a quoted evidence line must live
        # inside ONE log line — a splice across adjacent lines is a fabrication.
        corpus_lines = [_ground_transform(ln) for ln in raw_log.splitlines()]
        for fence in _LLM_EVIDENCE_FENCE_RE.findall(body):
            for line in fence.split("\n"):
                if line.strip() and not any(line in cl for cl in corpus_lines):
                    offenders.append(f"{pole}: {line!r}")
    if offenders:
        return Check(name, False,
                     "gap-fill evidence line(s) are NOT verbatim in the captured job log - the "
                     "LLM reading fabricated (or altered) a quoted line, so the block is not "
                     "grounded (fix the analysis JSON's `evidence` to copy real log lines; NEVER "
                     "hand-edit the report or the renderer to pass): " + "; ".join(offenders))
    if unverifiable:
        return Check(name, True,
                     "cannot verify grounding - the captured log(s) for these gap-fill block(s) "
                     "are not readable from the findings bundle (moved scratch / legacy artifact): "
                     + "; ".join(unverifiable), skipped=True)
    return Check(name, True, f"all {len(blocks)} gap-fill block(s) quote evidence verbatim from "
                 "their captured job log")


# The literal sentence the appendix renders for a finding the renderer believes sits ON the
# merge-gating critical path (blocking_path's `_saves_wall_clock`-gated "Wall-clock:" note).
_ON_PATH_SENTENCE = "sits ON the merge-gating critical path"
# The typical/rare split thresholds — VERBATIM copies of blocking_path's `_RARE_PRESENCE_MIN_PR`
# / `_POLE_RECUR_FLOOR` / `_RARE_PRESENCE_FRAC` (this file stays import-free from the engine; the
# `_VR_*` copies are DATA read the same way as findings.json). `_rare_demoted_check_names` mirrors
# `_typical_check` with these so the re-derived demotion can't disagree with the rendered spine.
# This is the SINGLE definition of all three — the phantom-gate class below (`_vr_pole_*`) reads
# `_VR_POLE_RECUR_FLOOR` / `_VR_RARE_PRESENCE_MIN_PR` from here too, so a threshold edit can't
# silently no-op by touching only one of two copies. Coupled to the engine by a self-test
# (`test_structural_findings.py`).
_VR_RARE_PRESENCE_MIN_PR = 6
_VR_POLE_RECUR_FLOOR = 2
_VR_RARE_PRESENCE_FRAC = 0.5
# Issue #115: the chain-vs-makespan divergence beyond which the headline WALL must lead with the
# observed makespan (not the chain sum). Mirrors `blocking_path._CHAIN_MAKESPAN_DIVERGENCE_PCT`
# and the Model-check emission (`abs(divergence_pct) > 25`); the wall-understated arm is the
# NEGATIVE one (`(chain_p50 - makespan)/makespan * 100 < -25`).
_VR_CHAIN_DIVERGENCE_PCT = 25.0
# The headline's slowest-check claim, FORM 1 (gate-is-slowest branch): the gate IS the slowest,
# so the renderer writes "`X` is the slowest check a typical PR waits on" with the name FIRST.
_HEADLINE_SLOWEST_RE = re.compile(
    r"`([^`]+)`\s+is the slowest check a typical PR waits on")
# FORM 2 (floor != frequency-gate branch, `blocking_path.py` ~4257): the most-gating check isn't
# the slowest, so the renderer states both facts — "the slowest check a typical PR waits on is
# `{floor_name}`; `{gate}` is the check most PRs gate on" — with the slowest's name AFTER the
# phrase. In BOTH forms the NAMED slowest is `floor_name` = `src[0].name` = the slowest TYPICAL
# check = the data layer's `critical_path_check`, so the SAME stamp comparison is correct for both;
# only the surface word order differs. Capturing form 2 here is what keeps the headline<->stamp
# guard live on the floor!=gate branch (the more failure-prone one) instead of SKIPping it.
_HEADLINE_SLOWEST_FORM2_RE = re.compile(
    r"the slowest check a typical PR waits on is `([^`]+)`")


def _headline_slowest_label(report: str) -> str | None:
    """The check label the headline NAMES as 'the slowest check a typical PR waits on', from
    EITHER rendered form (form 1: name-first gate-is-slowest; form 2: name-after floor!=gate). The
    returned label is the renderer's `_clean_label`'d `floor_name` = `src[0].name` in both — the
    slowest TYPICAL check, which the data layer stamps as `critical_path_check` — so the one stamp
    comparison covers both forms. Returns the raw captured label, or None when neither form is
    present (no slowest-check headline to validate)."""
    m = _HEADLINE_SLOWEST_RE.search(report)
    if m:
        return m.group(1)
    m = _HEADLINE_SLOWEST_FORM2_RE.search(report)
    if m:
        return m.group(1)
    return None


# Verbatim twin of `blocking_path._fence_safe` (+ `_defuse_backtick_runs`). The renderer
# fence-safes every NAME through `_clean_label`, so `_strip_scope` (the comparator's identity
# basis) must apply the IDENTICAL transform or a hostile name would normalize differently on the
# two sides and the on-path / dropped-check / floor comparators would false-positive/negative.
_FENCE_RUN_RE = re.compile(r"`{3,}")
_FENCE_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _defuse_backtick_runs(s: str) -> str:
    return _FENCE_RUN_RE.sub(lambda m: "'" * len(m.group(0)), s)


def _fence_safe(s: object) -> str:
    s = _FENCE_CTRL_RE.sub("", str(s))
    s = re.sub(r"[\r\n]+", " ", s)
    return _defuse_backtick_runs(s)


def _strip_scope(s: str) -> str:
    """Drop a leading `@scope/` monorepo prefix THEN fence-safe — the renderer label transforms
    this standalone checker replicates (mirrors `blocking_path._clean_label`: `re.sub(r"^@[^ /]+/")`
    then `_fence_safe`). A bounded NAME normalization, NOT a ranking re-derivation: the report labels
    a check `@better-auth-test/prisma-adapter Integration Test` as `prisma-adapter Integration Test`,
    so comparing a headline label to the full `critical_path_check` stamp needs this strip or it
    false-positives; the `_fence_safe` keeps the comparison correct for a hostile name the renderer
    defused. We deliberately do NOT replicate any p50/typicality logic — the comparator stays a field
    comparison, never a re-ranker (that would re-introduce the bug class)."""
    return _fence_safe(re.sub(r"^@[^ /]+/", "", s)).replace("`", "'")


def _on_path_framed_check_names(report: str) -> set[str]:
    """The set of EXACT check NAMES the rendered report frames as ON the merge-gating critical
    path in a single-name context — a Long-pole header (`## Long pole N: \\`wf\\` ▸ CHECK`, a
    pole IS the critical path) and the headline "the slowest check a typical PR waits on" claim in
    EITHER rendered form (name-first form 1 OR name-after form 2 — `_headline_slowest_label`). (The
    appendix on-path note names jobs inside a CONCATENATED `**Where:**` line, so it can't join this
    exact set cleanly — its job names are extracted by `_on_path_appendix_jobs`.) `_norm_check`-
    normalized; the headline's `_clean_label`'d label is reconciled with the full stamp via
    `_strip_scope` on the STAMP side, not here."""
    names = {_cmp_name(check) for _wf, check, _body in _pole_header_sections(report)}
    slowest = _headline_slowest_label(report)
    if slowest:
        names.add(_cmp_name(slowest))
    names.discard("")
    return names


def _cmp_name(s: str) -> str:
    """The basis on which a dropped check and an on-path framing are compared for identity:
    scope-stripped (`_strip_scope`) THEN `_norm_check`. The `dropped_*` sets store the RAW
    check-run name with its `@scope/` prefix (`@a/pkg build`), but EVERY rendered framing site
    (`_pole_header_sections`, the headline, the appendix `**Where:**` jobs) has already had the
    scope dropped by `blocking_path._clean_label` — so an exact-name compare would NEVER intersect
    for a monorepo check, silently blinding the gate to the very scoped-check class #99 came from.
    Stripping scope on BOTH sides closes that false-negative. The residual is a rare false-POSITIVE
    on a genuine monorepo where `@a/pkg build` is dropped while a DIFFERENT package's `@b/pkg build`
    is framed on-path (both → `pkg build`): for a CI regression net, erring toward CATCHING (a
    noisy block a human clears) over MISSING (a shipped contradiction) is the right bias; a precise
    fix needs `(check, workflow_file)` identity from findings.json (future work — the gate is a
    backstop to the scope-agnostic `off_spine` source fix, not the primary prevention)."""
    return _norm_check(_strip_scope(str(s)))


# A `**Where:**` segment `\`wf:line\` (job)` — capture the job, non-greedily, allowing ONE level of
# matrix parens by anchoring the close on the next `, \`` segment, a `, +N more` truncation suffix,
# or end-of-line.
_WHERE_JOB_RE = re.compile(r"`[^`]+`\s*\((.*?)\)(?=\s*,\s*(?:`|\+)|\s*$)")


def _on_path_appendix_jobs(report: str) -> tuple[set[str], list[str]]:
    """For each "Also noticed" `<details>` group carrying the on-path note, parse its `**Where:**`
    line into the SET of `_cmp_name`-normalized job names (one per `\\`wf:line\\` (job)` segment).
    Returns (exact_jobs, fallback_lines): a dropped check is matched EXACTLY against `exact_jobs`
    (so a short name like `test` doesn't false-positive on a longer job `integration-test-suite`,
    which a raw substring scan did). `fallback_lines` holds the whole normalized line for any block
    whose Where line yielded NO parseable segment — matched by substring there, so a format drift
    degrades to the old (over-eager, never silent) behavior rather than going blind (a missed
    contradiction is the worse direction for this gate)."""
    exact: set[str] = set()
    fallback: list[str] = []
    for block in report.split("<details>"):
        if _ON_PATH_SENTENCE not in block:
            continue
        m = re.search(r"^\*\*Where:\*\*\s*(.+)$", block, re.MULTILINE)
        line = m.group(1) if m else block
        segs = _WHERE_JOB_RE.findall(line)
        if segs:
            exact |= {_cmp_name(s) for s in segs}
        else:
            fallback.append(_cmp_name(line))
    exact.discard("")
    return exact, fallback


def _vr_gate_counts(cp: dict) -> tuple[dict, int]:
    """Standalone mirror of `blocking_path._gate_counts`: from the per-PR `populations`
    ground truth, how often each check is PRESENT (ran with a positive p50) and the number of
    populations, across sampled PRs. `({}, 0)` when no populations were recorded. Returns only
    (present, n) — the typical/rare re-derivation needs `pole_n` (stamped on `checks[]`) for the
    primary path and this presence map only for the legacy `pole_n`-less fallback."""
    present: dict[str, int] = {}
    n = 0
    for entry in _as_list(cp.get("populations")):
        try:
            _share, cks = entry
            pos = [(str(nm), float(p)) for nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            continue
        if not pos:
            continue
        n += 1
        for nm, _p in pos:
            present[nm] = present.get(nm, 0) + 1
    return present, n


def _rare_demoted_check_names(data: dict) -> set[str]:
    """The set of `_cmp_name`-normalized check names the spine demotes as OPT-IN / RARE — the
    exact class whose level-1 footnote reads "a typical PR doesn't wait on it" — re-derived from
    `pr_critical_path` (never a proxy of the renderer). A check is opt-in/rare iff BOTH:
      (1) the typical/rare split demotes it (`not _typical_check` — mirrors `blocking_path`'s
          predicate: required-exemption, small-sample floor, pole-frequency floor, legacy
          presence fallback, with the same populations-first `npop`/`present`), AND
      (2) it is PRESENT on only a MINORITY of sampled PRs (`present <= npop * _VR_RARE_PRESENCE_FRAC`,
          with the `npop >= _VR_RARE_PRESENCE_MIN_PR` sample-size floor).

    The PRESENCE-minority clause (2) is load-bearing: it separates the opt-in class (paradedb
    `Test pg_search`, on 8/20 PRs — a typical PR genuinely skips it) from a FREQUENCY-demoted
    matrix leg that is present on EVERY PR but rarely the single slowest (requests `build
    (3.10, windows-latest)`, present 11/11, `pole_n` 0). The latter's job/matrix IS on the
    critical path every PR (a sibling leg gates, and an OPT73/OPT24 across the matrix legitimately
    cuts wall-clock), so its "sits ON the critical path" framing is TRUE, not a contradiction —
    `_ms_freq_demoted` gives it a DIFFERENT footnote ("throughput/cost, a heavier check gates
    ahead"), never the "a typical PR doesn't wait on it" wording this class is about. A required
    check is never demoted. Empty when there is no `checks[]` list to judge."""
    cp = _as_dict(data.get("pr_critical_path"))
    checks = [c for c in _as_list(cp.get("checks")) if isinstance(c, dict)]
    if not checks:
        return set()
    _typical, present, npop = _vr_typical_predicate(data)

    def _opt_in_rare(nm: str) -> bool:
        if _typical(nm):
            return False
        return npop >= _VR_RARE_PRESENCE_MIN_PR and present.get(nm, 0) <= npop * _VR_RARE_PRESENCE_FRAC

    demoted = {_cmp_name(str(c.get("name", "")))
               for c in checks
               if str(c.get("name", "")) and _opt_in_rare(str(c.get("name", "")))}
    demoted.discard("")
    return demoted


def _vr_typical_predicate(data: dict):
    """(typical_fn, present, npop) — the standalone mirror of `blocking_path._typical_check`'s
    closure, re-derived from `pr_critical_path` (never a renderer proxy). ONE definition of the
    typical/rare split shared by `_rare_demoted_check_names` (the opt-in/minority subset) and
    `_non_typical_pole_check_names` (the full demoted set), so the two can't diverge from each other
    or from the engine: required-exemption first, then modal-gate-CHAIN membership (issue #112),
    then the pole-frequency floor when `pole_n` is fully stamped (small-sample floor short-circuits
    to typical), else the legacy presence rule. The populations-first `present`/`npop` fall back to
    the stampless `present_on`/`check_present_n_pr` counts exactly as `_rare_demoted_check_names` did
    before this was extracted.

    Modal-chain exemption (issue #112): a transitive `needs:` member of the modal blocking chain
    (re-derived from `chain_facts` via `_vr_modal_chain`, the mirror of `chain_summary.modal_chain`
    the renderer keys on) whose chain FEEDS a required check is ON-spine — its time ADDS on
    the gate path (SKILL/ARCHITECTURE), so the renderer drills it on-spine (Stage N/M) and never
    demotes it (`blocking_path._demoted_gate_framing` excludes `_chain_set` members). Such a member
    is neither in `required_checks` nor carries a recurring `pole_n` (home-assistant/core `Prepare
    dependencies (3.14.5)`, stage 2/3 of `Collect information … → Prepare dependencies → Check
    hassfest`, `pole_n` 0), so without this arm the frequency floor below would wrongly demote it and
    fail the on-gate framing the renderer correctly used. Re-derived from the stamped chain facts
    (single-door, #19 — never from prose). The required-sink guard (`modal[-1] in required`) mirrors
    the renderer's intent that the chain gates the merge and keeps a chain that feeds nothing required
    from laundering a rare check onto the spine."""
    cp = _as_dict(data.get("pr_critical_path"))
    checks = [c for c in _as_list(cp.get("checks")) if isinstance(c, dict)]
    required = {str(c) for c in _as_list(data.get("required_checks"))}
    pole_n = {str(c.get("name", "")): c.get("pole_n") for c in checks}
    have_pole_n = bool(pole_n) and all(v is not None for v in pole_n.values())
    nfloor = int(cp.get("check_present_n_pr") or 0)
    present, npop = _vr_gate_counts(cp)
    if not npop:
        _pres = {str(c.get("name", "")): int(c.get("present_on") or 0) for c in checks}
        _denom = int(cp.get("check_present_n_pr") or 0)
        _complete = all(c.get("present_on") is not None for c in checks)
        if _pres and _denom and _complete:
            present, npop = _pres, _denom

    # Modal gate-chain members (issue #112). Re-derive the modal chain + its p50 from `chain_facts`
    # (via `_vr_modal_chain` + the `chain_s` median) — SINGLE-DOOR (#19): the renderer keys on
    # `chain_summary`, so verify must NOT read it back (a bound taken against the renderer can't be
    # vouched for by the renderer's own reduced input). `_chain_active` mirrors `blocking_path`'s
    # `chain_active` off the FACTS: >= 2 members AND a positive chain p50. `_modal[-1] in required`
    # is a VERIFIER-ONLY narrowing — the renderer runs no required-sink test — that keeps verify
    # STRICTER than the renderer, mirroring its INTENT that the chain gates the merge and stopping a
    # chain that feeds nothing required from laundering a rare check onto the spine.
    _modal = _vr_modal_chain(cp)
    _chain_spans = sorted(
        float(f.get("chain_s"))
        for f in _as_list(cp.get("chain_facts"))
        if isinstance(f, dict) and isinstance(f.get("chain_s"), (int, float)))
    _chain_p50 = 0.0
    if _chain_spans:
        _cmid = len(_chain_spans) // 2
        _chain_p50 = (_chain_spans[_cmid] if len(_chain_spans) % 2
                      else (_chain_spans[_cmid - 1] + _chain_spans[_cmid]) / 2.0)
    _chain_active = len(_modal) >= 2 and _chain_p50 > 0.0
    _chain_feeds_required = bool(_modal) and _modal[-1] in required
    chain_members = set(_modal) if (_chain_active and _chain_feeds_required) else set()

    def _typical(nm: str) -> bool:
        if nm in required:
            return True
        if nm in chain_members:
            return True
        if have_pole_n:
            if nfloor < _VR_RARE_PRESENCE_MIN_PR:
                return True
            return int(pole_n.get(nm) or 0) >= _VR_POLE_RECUR_FLOOR
        if npop < _VR_RARE_PRESENCE_MIN_PR:
            return True
        return present.get(nm, 0) > npop * _VR_RARE_PRESENCE_FRAC

    return _typical, present, npop


def _non_typical_pole_check_names(data: dict) -> set[str]:
    """The `_cmp_name`-normalized set of check names the spine DEMOTED as NOT typical — the SUPERSET
    of `_rare_demoted_check_names` that ALSO includes a FREQUENCY-demoted check present on a MAJORITY
    of PRs but rarely the actual slowest (the caddy `goreleaser-check`: present 13/20, `pole_n` 0,
    which the presence-minority clause deliberately keeps OUT of the opt-in/rare set but which is
    still `not _typical`). This is the set whose per-pole header reads "Rarely the merge gate" / "Opt
    -in / rare" / "Required · path-conditional" — never "the slowest check a typical PR waits on" — so
    the typical-gate framing must not appear in its drill prompt or its Contents row. Re-derived from
    `checks[].pole_n`/presence with the SHARED `_vr_typical_predicate`. Empty when no `checks[]`."""
    cp = _as_dict(data.get("pr_critical_path"))
    checks = [c for c in _as_list(cp.get("checks")) if isinstance(c, dict)]
    if not checks:
        return set()
    _typical, _present, _npop = _vr_typical_predicate(data)
    out = {_cmp_name(str(c.get("name", "")))
           for c in checks
           if str(c.get("name", "")) and not _typical(str(c.get("name", "")))}
    out.discard("")
    return out


def check_dropped_check_not_framed_on_path(report: str, findings_path: Path | None) -> Check:
    r"""**The cross-seam contradiction property (Phase 0).** No check the spine EXCLUDED (listed
    in `dropped_non_pr_checks` / `dropped_non_required_checks`) may ALSO be framed as sitting ON
    the merge-gating critical path — the encord §6 class, generalized. It is a quantified
    PROPERTY over two sets read from the artifact + the rendered report (never a proxy of the
    renderer): the EXCLUDED set (the `dropped_*` footnote — present even in stampless bundles,
    which is why this keys on it and not `off_spine`) and the ON-PATH-FRAMED set built from EVERY
    framing context — `_on_path_framed_check_names` (pole headers ∪ the headline "slowest" claim,
    matched by EXACT name) ∪ `_on_path_appendix_jobs` (the appendix on-path notes' `**Where:**`
    job names, matched EXACTLY, with a substring fallback only when a Where line won't parse). The
    assertion is `EXCLUDED ∩ ON_PATH == ∅`. Because the on-path side is a SET assembled from every
    context, a contradiction shape not yet enumerated (a future on-path context) is caught by
    extending that set, not by adding a bespoke check — the class-wide guard, not a point-fix.

    The EXCLUDED set has TWO members, each with its OWN correct on-path scope:
      • HARD-DROPPED checks (`dropped_*`) — excluded from the spine entirely, so they must not be
        framed on-path in ANY context (pole header ∪ headline ∪ appendix). Full-scope comparison.
      • RARE-DEMOTED checks (`_rare_demoted_check_names`, re-derived from `checks[].pole_n`/presence
        the same way `_typical_check` demotes them) — these ARE still drilled as demoted "Long pole
        N" headers WITH their own "opt-in / rare" body framing, so a header is NOT a contradiction
        for them. Their ONLY contradiction is the appendix "sits ON the merge-gating critical path"
        note, which asserts the OPPOSITE of the spine footnote's "a typical PR doesn't wait on it"
        (the paradedb `Test pg_search` double-framing). Appendix-scope comparison only.

    Match is by exact normalized check NAME, so a monorepo `@a/pkg build` (scope-prefixed) never
    collides with a different check; the appendix/header names a JOB, so it only trips when the
    job name equals the excluded check name (the encord shape) — the same-token cross-scope
    residual `_stamp_off_spine_findings` documents (job `build` vs dropped CHECK `@a/pkg build`)
    is out of exact-name scope by design."""
    name = "no spine-dropped check is also framed on the merge-gating critical path"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no spine to contradict",
                     skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    dropped = {_cmp_name(n) for n in
               (_as_list(cp.get("dropped_non_pr_checks"))
                + _as_list(cp.get("dropped_non_required_checks")))}
    dropped.discard("")
    rare_demoted = _rare_demoted_check_names(_as_dict(data))
    if not dropped and not rare_demoted:
        return Check(name, True, "no checks dropped or rare-demoted from the spine",
                     skipped=True)
    # On-path framing: an exact-name context (headers ∪ headline ∪ parsed appendix jobs) OR —
    # only when an appendix Where line wouldn't parse — a substring of that raw line.
    framed = _on_path_framed_check_names(report)
    appendix_jobs, appendix_fallback = _on_path_appendix_jobs(report)
    framed_all = framed | appendix_jobs
    # HARD-DROPPED: contradiction in ANY framing context. RARE-DEMOTED: contradiction ONLY in the
    # appendix on-path note (a demoted Long-pole header carries its own opt-in framing — not a
    # contradiction). The substring fallback stays appendix-scoped for both.
    contradictions = sorted(
        {d for d in dropped
         if d in framed_all or any(d in w for w in appendix_fallback)}
        | {d for d in rare_demoted
           if d in appendix_jobs or any(d in w for w in appendix_fallback)})
    if contradictions:
        return Check(name, False,
                     "check(s) the spine EXCLUDED (dropped, or typical/rare-demoted as opt-in) "
                     "are ALSO framed on the merge-gating critical path (header / appendix / "
                     "headline — the encord §6 + paradedb double-framing class): "
                     + "; ".join(contradictions))
    return Check(name, True,
                 f"none of {len(dropped)} dropped + {len(rare_demoted)} rare-demoted "
                 "spine check(s) framed on-path (headers ∪ appendix ∪ headline)")


# The TYPICAL-gate framing phrase the agent-prompt "THE GATE" line renders for a TYPICAL pole
# ("Slowest check a typical PR waits on: P50 …"). A DEMOTED pole's OWN header disowns it ("Rarely
# the merge gate …"), so this phrase inside a demoted pole's drill is the caddy goreleaser-check
# contradiction. Lower-cased to match `_norm_check`. Deliberately the "typical" variant — the
# demoted header's "the actual slowest check a PR waits on" is the DIFFERENT (typical-less) phrase.
_TYPICAL_GATE_PROMPT_PHRASE = "slowest check a typical pr waits on"
# A Contents row's workflow gate-count tail ("· `wf` gates N/M PRs"). Attributed next to a DEMOTED
# pole (whose OWN gate frequency is far lower) it is the same conflation the prompt line commits.
_TOC_GATES_TAIL_RE = re.compile(r"gates\s+\d+/\d+\s+PRs", re.IGNORECASE)
# A Contents pole row: `… [`check`](#pole-N) …` — capture the check name and the tail after the link.
_TOC_POLE_ROW_RE = re.compile(r"\[`([^`]+)`\]\(#pole-\d+\)([^\n]*)")


def check_demoted_pole_not_framed_typical_gate(report: str,
                                                findings_path: Path | None) -> Check:
    """**The demoted-pole typical-gate-framing class.** A pole the spine DEMOTED (NOT typical — re-
    derived from `checks[].pole_n`/presence the SAME way `_typical_check` demotes it,
    `_non_typical_pole_check_names`) must NOT carry the TYPICAL-gate framing its OWN "Rarely the merge
    gate" header disowns, in EITHER of its two render sites:
      • its agent-prompt "THE GATE" line saying "Slowest check a typical PR waits on"
        (`_TYPICAL_GATE_PROMPT_PHRASE`), and
      • its Contents row carrying a "`wf` gates N/M PRs" tail (`_TOC_GATES_TAIL_RE`) — the workflow's
        typical-gate frequency, driven by a SIBLING required check, borrowed next to a pole that is
        the actual slowest on a small minority of PRs.
    The caddy `goreleaser-check` class: `pole_n` 0, present 13/20 (so NOT opt-in/rare, but still not
    typical), yet its prompt read "Slowest check a typical PR waits on … ci.yml gates 20/20" while its
    header read "Rarely the merge gate … on only 0/20". Re-derived from findings, never a renderer
    proxy; the pole section is `_pole_header_sections` and the Contents rows are scoped to the
    `## Contents` section so a `#pole-N` link elsewhere can't false-match."""
    name = "no spine-demoted pole carries the typical-gate framing (prompt / Contents)"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no spine to contradict",
                     skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    demoted = _non_typical_pole_check_names(_as_dict(data))
    if not demoted:
        return Check(name, True, "no non-typical poles to check", skipped=True)
    offenders: list[str] = []
    # 1) The per-pole drill: a demoted pole's section must not contain the typical-gate prompt phrase.
    for wf, check, body in _pole_header_sections(report):
        if _cmp_name(check) in demoted and _TYPICAL_GATE_PROMPT_PHRASE in _norm_check(body):
            offenders.append(f"`{wf}` ▸ {check} (agent prompt: 'typical PR waits on')")
    # 2) The Contents rows: a demoted pole's row must not carry a workflow gate-count tail.
    toc = _section(report, "Contents")
    for m in _TOC_POLE_ROW_RE.finditer(toc):
        row_check, tail = m.group(1), m.group(2)
        if _cmp_name(row_check) in demoted and _TOC_GATES_TAIL_RE.search(tail):
            offenders.append(f"Contents `{row_check}` (gate-count tail)")
    if offenders:
        return Check(name, False,
                     "spine-demoted pole(s) framed with the typical-gate language their own "
                     "'Rarely the merge gate' header disowns (the caddy goreleaser-check class): "
                     + "; ".join(sorted(set(offenders))))
    return Check(name, True,
                 f"none of {len(demoted)} demoted pole(s) carry typical-gate framing "
                 "(prompt ∪ Contents)")


# Claims layer (plan 002, increment 1: headline family). `blocking_path.render()` writes
# `<report>.claims.json` next to a NEWLY rendered report (via `main()`, when `--out` is
# given) — a typed manifest of the judgment-bearing sentences it built, so a comparator
# HERE can read FIELDS instead of parsing prose. Deliberately a plain JSON read, no
# `blocking_path`/`claims` import: this file stays import-free from the engine (see the
# "`_VR_*` copies keep verify_report import-free" note further below) — the manifest is
# DATA, read the same way `findings.json` is, never a reason to import the renderer.
def _load_claims(report_path: Path | None) -> dict | None:
    """The claims manifest for `report_path`, or None when absent/unreadable. None means
    "no manifest" to every caller — an OLDER artifact rendered before this manifest
    existed, or a corrupt/wrong-type file — and every caller degrades to the pre-existing
    text-parsing path for that claim family, exactly like a report with no findings.json
    degrades to a skip. Never raises."""
    if report_path is None:
        return None
    manifest_path = report_path.parent / (report_path.name + ".claims.json")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_headline_slowest_matches_stamp(report: str, findings_path: Path | None,
                                          report_path: Path | None = None) -> Check:
    """**Phase-0 comparator.** The headline's "`X` is the slowest check a typical PR waits on"
    must name the SAME check the data layer stamped as `critical_path_check` (`collect_runs`
    `pr_checks_tuple[0]` — the p50 winner of the PRESENT-FIRST ordering: typical checks ranked
    above rare/opt-in ones, so it is the slowest TYPICAL check, not necessarily the global p50
    max). Catches the renderer naming a different "slowest" than the data layer (e.g. a
    frequency-gate drift) — without the verifier re-ranking: it is a FIELD-vs-text comparison
    (stamp vs the rendered label), the only renderer transform replicated being the bounded
    `_strip_scope` label normalization. It binds BOTH headline forms (`_headline_slowest_label`):
    the name-first "`X` is the slowest…" gate-is-slowest form AND the renderer's honest disclosure
    form ("…the slowest check … is `floor_name`; `gate` is the check most PRs gate on"). In form 2
    the NAMED slowest is `floor_name` = `src[0].name` = the slowest TYPICAL check = the data layer's
    `critical_path_check` (NOT the frequency gate), so the SAME stamp comparison validates it WITHOUT
    false-positiving. Excluding form 2 (the prior behavior, on the mistaken rationale that there
    `floor_name` ≠ the frequency gate — true, but the comparison is against `critical_path_check`,
    not the gate) let the guard go DARK on exactly the floor!=gate branch — the more failure-prone
    one — where a renderer re-derived a WRONG floor name would have slipped through unverified.
    Standalone; keys on `critical_path_check`, present even in stampless bundles.

    **Manifest path (plan 002).** When `report_path` resolves a `<report>.claims.json`
    manifest that carries at least one usable `headline_slowest` claim (and `"headline"` is
    in its `families_migrated`), the comparison reads that claim's `subject` (field, not
    text-extracted) vs `critical_path_check` — instead of regex-extracting the label from
    prose — AND asserts each claim's `rendered` sentence appears EXACTLY ONCE in the report
    (the manifest<->prose bind: catches the report and its own manifest drifting apart, which
    a pure field comparison alone could not). No manifest, "headline" not migrated, OR a
    manifest that declares "headline" migrated yet yields NO usable headline_slowest claim
    all fall back to the original `_headline_slowest_label` text-parsing path unchanged —
    so the fallback is never WEAKER than having no manifest (a report whose prose names the
    wrong check still FAILS, never SKIPs). This is how an OLDER committed artifact (no
    manifest) keeps verifying exactly as before."""
    name = "headline 'slowest check' names the data layer's critical_path_check"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no headline gate", skipped=True)

    # Decide FIRST whether there is anything to validate (a manifest claim, or — falling
    # back — a text-parsed label), the same way the pre-manifest code checked for a label
    # before ever opening findings.json. This ordering matters: a report with no headline
    # claim at all must SKIP identically regardless of what garbage findings.json carries
    # (`test_wrong_type_findings_container_does_not_crash` pins exactly this — every
    # malformed findings container must degrade to the SAME output as `{}`, which only
    # holds if a missing claim/label short-circuits before the stamp is even read).
    manifest = _load_claims(report_path)
    headline_claims: list[dict] = []
    if bool(manifest) and "headline" in _as_list(manifest.get("families_migrated")):
        headline_claims = [c for c in _as_list(manifest.get("claims"))
                            if isinstance(c, dict) and c.get("kind") == "headline_slowest"]
    # The manifest path is "active" only when it actually yields a usable headline claim.
    # A manifest that declares "headline" migrated but carries ZERO headline_slowest claims
    # (empty/wrong-type `claims`, or no claim of that kind) must NOT return SKIP — that would
    # be strictly WEAKER than having no manifest at all (the text path would still FAIL a
    # wrong headline). Instead, fall through to the text-parsing validation, so "manifest
    # present but headline claim missing" behaves EXACTLY like "no manifest" — the fallback
    # promise stays literally true, never weaker. (Not reachable from the real renderer: its
    # only claim-less headline branch is `not npop`, whose prose the text regex also doesn't
    # match — so a genuine no-npop report SKIPs either way. This guards a crafted/corrupt
    # manifest.)
    manifest_active = bool(headline_claims)
    label: str | None = None
    if not manifest_active:
        label = _headline_slowest_label(report)
        if not label:
            return Check(name, True, "no 'slowest check a typical PR waits on' headline claim",
                         skipped=True)

    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    stamp = _as_dict(_as_dict(data).get("pr_critical_path")).get("critical_path_check")
    if not stamp:
        return Check(name, True, "no critical_path_check stamped", skipped=True)
    stamped = _cmp_name(stamp)

    if manifest_active:
        for c in headline_claims:
            subject = _cmp_name(str(c.get("subject", "")))
            if subject != stamped:
                return Check(name, False,
                             f"manifest claim (claims.json) names `{c.get('subject')}` the "
                             f"slowest check, but the data layer stamped `{stamp}` as "
                             "`critical_path_check` — the renderer re-derived a different "
                             "'slowest' (re-derive ⇒ contradiction risk)")
            rendered = str(c.get("rendered", ""))
            occurrences = report.count(rendered) if rendered else 0
            if occurrences != 1:
                return Check(name, False,
                             f"manifest claim's rendered sentence appears {occurrences} "
                             "time(s) in the report (expected exactly 1) — claims.json has "
                             f"drifted from the prose it was minted from: {rendered!r}")
        # Text floor (plan 007, lying-manifest guard): the manifest can STRENGTHEN the text
        # check but never REPLACE it. Even on the manifest path, run the prose extraction; a
        # crafted-but-internally-consistent manifest (subject==stamp, rendered present once)
        # cannot launder a rendered headline that NAMES a different check. If the extraction
        # parses a label that disagrees with the stamp, FAIL regardless of the manifest. (When
        # it parses nothing — e.g. the not-npop branch — the manifest result stands.)
        floor_label = _headline_slowest_label(report)
        if floor_label and _cmp_name(floor_label) != stamped:
            return Check(name, False,
                         f"the rendered headline calls `{floor_label}` the slowest check, but "
                         f"the data layer stamped `{stamp}` as `critical_path_check` — the "
                         "manifest cannot override the prose floor (lying-manifest guard)")
        return Check(name, True,
                     f"headline slowest (manifest) `{stamp}` == stamped critical_path_check; "
                     f"{len(headline_claims)} claim(s) bound 1:1 to the rendered prose "
                     "(prose floor agrees)")

    # Fallback: no manifest (or "headline" not migrated in it) — an older artifact, verified
    # exactly as before the claims layer existed.
    headline = _cmp_name(label or "")
    if headline != stamped:
        return Check(name, False,
                     f"the headline calls `{label}` the slowest check, but the data layer "
                     f"stamped `{stamp}` as `critical_path_check` — the renderer re-derived a "
                     "different 'slowest' (re-derive ⇒ contradiction risk)")
    return Check(name, True, f"headline slowest `{label}` == stamped critical_path_check")


def check_headline_floor_presence_reconciled(report: str, findings_path: Path | None) -> Check:
    """**The headline floor-reconciliation class (Form-2 presence disclosure).** When the headline
    names the slowest TYPICAL check (`critical_path_check`) whose OWN p50 EXCEEDS the population-
    weighted floor the SAME sentence states as "X until all checks finish" — i.e. the engine LOWERED
    that floor because the check is non-universal — the headline MUST reconcile the two: it may not
    label a check "the slowest check a typical PR waits on" (~its full p50) beside a strictly-LOWER
    all-checks-finish floor without disclosing the check ran on only N/M sampled PRs. Form 1 (the
    gate-is-slowest floor-lowered branch) always disclosed this; Form 2 (floor != frequency-gate)
    omitted it, so it labeled a 5/20 path-filtered check "the slowest check a typical PR waits on"
    beside a 7m16s floor with no presence caveat — while that check's identical matrix siblings
    carried the opt-in footnote (tauri `test (windows-latest)`).

    Re-derived ENTIRELY from `pr_critical_path` (never the rendered floor text), mirroring the
    engine's lowering condition EXACTLY:
      * named slowest  = `critical_path_check` (the same stamp `check_headline_slowest_matches_stamp`
        binds; its NAME is validated there — here we re-derive the FLOOR reconciliation);
      * `slowest_p50`  = that check's `checks[].p50_s` — a FIELD read keyed on the stamp, never a
        re-rank (the bug class this whole family guards against);
      * `pop_floor`    = MEDIAN of the per-PR maxima in `populations` (`statistics.median` ==
        `blocking_path._median`), None below `_VR_RARE_PRESENCE_MIN_PR` populations (→ no lowering);
      * present/npop   = from `populations` (`_vr_gate_counts`, the engine's `_gate_counts` present/n).
    The engine lowers the floor ⇔ `0 < present < npop` AND `pop_floor < slowest_p50` (its
    `floor_p50 = _pop_floor` guard). When lowered, the report MUST reconcile the lower floor in ONE of
    two data-pinned ways (which one is faithful depends on presence — see
    `check_headline_presence_causal_only_when_minority`): the MINORITY presence caveat `ran on only
    {present}/{npop} sampled PRs, so a typical PR finishes in {merge_dur}`, OR the MAJORITY
    conditional-p50-overstatement clause `is a conditional p50 that overstates the typical wait; across
    sampled PRs the median PR's slowest check finishes in {merge_dur}` (`merge_dur` =
    `_fmt_clock(pop_floor)` = the engine's `_clock(floor_p50)`). SKIPs on no findings /
    static-only / no slowest-check headline (chain-form or generic — no floor to reconcile) / no
    populations / no lowering — a universal (or population-less) slowest check keeps its p50 floor and
    needs no reconciliation. Standalone; keys on `pr_critical_path` fields + the headline form only."""
    name = "headline reconciles a non-universal slowest check with the population floor"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no headline floor to reconcile",
                     skipped=True)
    # The reconciliation is only OWED when the report actually HEADLINES a slowest check ("the
    # slowest check a typical PR waits on" — the form-1/form-2 families the sibling
    # `check_headline_slowest_matches_stamp` also keys on). When the gate is a `needs:` chain the
    # headline is the CHAIN form ("the gate is the X → Y chain") and when no populations exist it is
    # the generic form ("a PR waits on the slowest concurrent check") — neither NAMES a slowest check
    # nor states a floor to reconcile, even though the populations may still carry a lowered-floor
    # shape. Re-deriving the lowering from `populations` alone would then demand a clause the chain /
    # generic headline never renders — a false FAIL. Gate on the headline form exactly as the sibling
    # does, so the re-derivation only runs on the branches that can actually contradict themselves.
    if _headline_slowest_label(report) is None:
        return Check(name, True, "no 'slowest check a typical PR waits on' headline — no floor to "
                     "reconcile (chain-form / generic headline)", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    stamp = cp.get("critical_path_check")
    # A non-string / empty stamp is a malformed or stampless bundle — degrade to the SAME SKIP as an
    # empty findings container (the wrong-type-container degradation contract), never a spurious FAIL.
    if not isinstance(stamp, str) or not stamp:
        return Check(name, True, "no critical_path_check stamped", skipped=True)
    stamped = _cmp_name(stamp)

    # The named slowest check's OWN p50 — a FIELD read from checks[], keyed the same way the engine
    # keys `src[0]` (== `critical_path_check`): exact raw name first, `_cmp_name` fallback (the same
    # scope-tolerant normalization the sibling name check uses), never a re-rank.
    slowest_p50: float | None = None
    for c in _as_list(cp.get("checks")):
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name", ""))
        if nm == str(stamp) or _cmp_name(nm) == stamped:
            try:
                slowest_p50 = float(c.get("p50_s"))
            except (TypeError, ValueError):
                slowest_p50 = None
            if nm == str(stamp):
                break
    if slowest_p50 is None:
        return Check(name, True, "critical_path_check carries no numeric checks[].p50_s", skipped=True)

    present_map, npop = _vr_gate_counts(cp)          # the engine's _gate_counts (present, n)
    if not npop:
        return Check(name, True, "no per-PR populations to re-derive presence/floor from",
                     skipped=True)
    spresent = present_map.get(str(stamp))
    if spresent is None:
        for nm, cnt in present_map.items():
            if _cmp_name(nm) == stamped:
                spresent = cnt
                break
    spresent = spresent or 0

    # The population-weighted floor = median of the per-PR maxima (mirrors `_population_typical_floor`:
    # None below the stability floor, so the engine keeps the slowest check's p50 and lowers nothing).
    maxima = [max(p for _n, p in pr) for pr in _populations_per_pr(cp) if pr]
    if len(maxima) < _VR_RARE_PRESENCE_MIN_PR:
        return Check(name, True,
                     f"only {len(maxima)} per-PR populations (< {_VR_RARE_PRESENCE_MIN_PR}) — the "
                     "population floor is not derivable, so the slowest check's p50 stands unlowered",
                     skipped=True)
    pop_floor = statistics.median(maxima)

    # The engine LOWERS the floor (→ the Form-2 contradiction risk) ⇔ the slowest is non-universal AND
    # the population floor is strictly below its p50 — EXACTLY blocking_path's `floor_p50 = _pop_floor`
    # guard (`0 < _spresent < npop` and `_pop_floor < slowest_p50`). Otherwise no reconciliation is owed.
    if not (0 < spresent < npop and pop_floor < slowest_p50):
        return Check(name, True,
                     f"`{stamp}` floor not lowered (present {spresent}/{npop}, "
                     f"pop-floor {_fmt_clock(pop_floor)} vs p50 {_fmt_clock(slowest_p50)}) — no "
                     "reconciliation owed", skipped=True)

    # PHYSICAL BOUND (issue #24): the renderer caps the population floor at the MEASURED makespan p50
    # (the median per-PR span-capped wall) — a re-run-inflated per-PR check p50 can leave `pop_floor`
    # above the actual wall, and "X until all checks finish" can never exceed it. Mirror that cap when
    # re-deriving `merge_dur` so the disclosed floor number matches the capped headline. makespan =
    # median of the per-PR `chain_facts.makespan_s` (== `_chain_summary`'s `makespan_p50_s`); absent
    # facts leave `pop_floor` uncapped (the pre-#24 behavior).
    _ms_vals = sorted(float(f.get("makespan_s"))
                      for f in _as_list(cp.get("chain_facts"))
                      if isinstance(f, dict) and isinstance(f.get("makespan_s"), (int, float)))
    if _ms_vals:
        _mmid = len(_ms_vals) // 2
        _makespan_p50 = (_ms_vals[_mmid] if len(_ms_vals) % 2
                         else (_ms_vals[_mmid - 1] + _ms_vals[_mmid]) / 2.0)
        if _makespan_p50 < pop_floor:
            pop_floor = _makespan_p50

    merge_dur = _fmt_clock(pop_floor)                # == blocking_path._clock(floor_p50)
    # The lowered floor may be reconciled in EITHER of two faithful ways, and which one is correct
    # depends on how often the slowest check runs (see `check_headline_presence_causal_only_when_minority`):
    #   * MINORITY presence — a typical PR SKIPS the check, so its ABSENCE lowers the median; the
    #     presence-causal clause ("ran on only N/npop, so a typical PR finishes in {merge_dur}") is the
    #     honest disclosure. The Form-1 minority sub-branch and Form 2 both render this.
    #   * MAJORITY presence — a typical PR RUNS the check, so presence CANNOT be the cause; Form 1
    #     instead reconciles via the conditional-p50-overstatement framing ("its ~DUR is a conditional
    #     p50 that overstates the typical wait; across sampled PRs the median PR's slowest check
    #     finishes in {merge_dur}"). Demanding the PRESENCE clause here would contradict the
    #     presence-causal guard on the very same report (the nx `main-linux` 19/20 shape).
    # Accept EITHER reconciliation — both re-derived to pin {merge_dur}. This check owes only that the
    # lower floor is DISCLOSED and its number is right; the presence-causal guard separately forbids the
    # WRONG (presence) form at majority. A headline that discloses NEITHER still FAILs (the tauri bug).
    presence_recon = (f"ran on only {spresent}/{npop} sampled PRs, "
                      f"so a typical PR finishes in {merge_dur}")
    overstatement_recon = ("is a conditional p50 that overstates the typical wait; across sampled "
                           f"PRs the median PR's slowest check finishes in {merge_dur}")
    if presence_recon not in report and overstatement_recon not in report:
        return Check(name, False,
                     f"the headline names `{stamp}` (~{_fmt_clock(slowest_p50)}) as the slowest "
                     f"check a typical PR waits on while stating a strictly-lower {merge_dur} "
                     "all-checks-finish floor, but discloses no reconciliation — the population floor "
                     f"was lowered (present {spresent}/{npop}), so the headline must disclose either "
                     "the presence caveat (minority-present) or the conditional-p50 overstatement "
                     f"(majority-present); expected {presence_recon!r} or {overstatement_recon!r}")
    return Check(name, True,
                 f"headline reconciles `{stamp}` ({spresent}/{npop} presence, ~"
                 f"{_fmt_clock(slowest_p50)}) with the {merge_dur} population floor")


# The presence-CAUSAL headline template (blocking_path `elif gate_is_slowest:`, non-universal
# disclosure, MINORITY sub-branch): "`X` is the slowest check a typical PR waits on (~DUR), but it
# ran on only N/M sampled PRs, so a typical PR finishes in ...". The "ran on only N/M, so" clause
# blames the LOWER typical merge floor on the check's PRESENCE. Capture the check name and the
# rendered N/M; `\s*` between fields absorbs the report-wide em-dash strip / soft wraps.
_HEADLINE_PRESENCE_CAUSAL_RE = re.compile(
    r"`([^`]+)`\s+is the slowest check a typical PR waits on\s*\([^)]*\)\s*,\s*"
    r"but it ran on only\s+(\d+)\s*/\s*(\d+)\s+sampled PRs\s*,\s*so a typical PR finishes in")


def check_headline_presence_causal_only_when_minority(report: str,
                                                      findings_path: Path | None) -> Check:
    """A headline that blames the LOWER typical merge wait on the slowest check's PRESENCE
    ("...but it ran on only N/M sampled PRs, so a typical PR finishes in ...") must name a check
    that is present on a MINORITY of sampled PRs. Presence can only pull the MEDIAN wait below a
    check's own conditional p50 when a typical (median) PR SKIPS the check — which requires minority
    presence. At MAJORITY presence the median PR RUNS the check, so presence is NOT the mechanism
    (the floor drop is a duration / population skew) and the "ran on only N/M, so" clause is an
    unsupported causal claim — the nx `main-linux` non-sequitur (slowest ~46m 33s, present 19/20,
    yet "so a typical PR finishes in 10m 32s": 95% presence cannot lower a 46m median to 10m).

    Re-derives present/npop for the NAMED check from the per-PR `populations` ground truth (never
    trusts the rendered N/M — a re-derivation, not a proxy of the renderer) and FAILS when
    present > npop * _VR_RARE_PRESENCE_FRAC. This is a REGRESSION guard: the current engine no longer
    renders the presence-causal template for a majority-present check, so on a correctly-rendered nx
    report the regex finds nothing and the check SKIPs — the nx example above is the PRE-FIX bug class,
    and the check FAILs only a report that RE-INTRODUCES that template at majority presence. Standalone;
    SKIPs when no such headline is rendered, the report is static-only, the sample is below the
    `_VR_RARE_PRESENCE_MIN_PR` stability floor, or `populations` don't resolve the named check."""
    name = "presence-causal headline only when the slowest check is minority-present"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no presence-causal headline gate",
                     skipped=True)
    m = _HEADLINE_PRESENCE_CAUSAL_RE.search(report)
    if not m:
        return Check(name, True, "no presence-causal 'ran on only N/M ... so' headline claim",
                     skipped=True)
    label = m.group(1)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    present, npop = _vr_gate_counts(cp)
    if npop < _VR_RARE_PRESENCE_MIN_PR:
        return Check(name, True,
                     f"only {npop} sampled PR population(s) (< {_VR_RARE_PRESENCE_MIN_PR}) — "
                     "presence fraction is noise, not re-derivable", skipped=True)
    # Re-derive the named check's presence from `populations`, comparing on the SAME basis the
    # rendered label carries (`_clean_label`'d / scope-stripped via `_cmp_name`). Take the MAX over
    # any raw names that normalize to the same label rather than summing — summing two distinct
    # checks' PR-presence counts could double-count a PR and spuriously inflate presence.
    target = _cmp_name(label)
    matches = [cnt for raw, cnt in present.items() if _cmp_name(raw) == target]
    if not matches:
        return Check(name, True,
                     f"headline check `{label}` not found in populations — cannot re-derive "
                     "presence", skipped=True)
    pres = max(matches)
    if pres > npop * _VR_RARE_PRESENCE_FRAC:
        return Check(name, False,
                     f"headline blames the lower typical wait on `{label}`'s presence "
                     f"(\"ran on only .../{npop} sampled PRs, so a typical PR finishes in ...\"), "
                     f"but the per-PR populations show it present on {pres}/{npop} PRs — a MAJORITY "
                     f"(> {_VR_RARE_PRESENCE_FRAC:g}·npop). A typical PR RUNS it, so presence "
                     "cannot lower the median wait; the floor drop is a duration/population skew, "
                     "and the presence-causal 'so' clause is a non-sequitur the populations "
                     "contradict")
    return Check(name, True,
                 f"headline presence-causal claim OK: `{label}` present {pres}/{npop} "
                 f"(<= {_VR_RARE_PRESENCE_FRAC:g}·npop) — a typical PR genuinely skips it")


def check_headline_chain_matches_stamp(report: str, findings_path: Path | None,
                                       report_path: Path | None = None) -> Check:
    """ENG-1 PR-N2: the chain-form headline re-derives from `chain_facts`.

    Decision table (the contract; red/green-tested in test_verify_report_self):
    | cell | state | verdict |
    | 1 | no findings / static-only | SKIP |
    | 2 | no `chain_facts` (legacy artifact) and no headline_chain claim | SKIP (compat-vacuous) |
    | 2b | `chain_facts` present but `chain_summary` ABSENT (an N1-era artifact) and no claim | SKIP — the renderer keys on the summary and correctly rendered classic; the e2e corpus pins summary presence on fresh collections |
    | 3 | headline_chain claim(s) present | EVERY claim: subject == re-derived modal chain (joined " → "); chain_p50_s == re-derived p50 AND `merge_dur` == `_fmt_clock`(re-derived p50) (the RENDERED figure re-derives — the claim never vouches for itself); `modal_n`/`n` fields == re-derived counts; rendered EXACTLY once; else FAIL |
    | 3b | any per-PR fact where sum(member_spans_s) != chain_s (±0.01) | FAIL (the promised "chain sum re-derives from member spans") |
    | 3c | a modal member has a rendered "## … Long pole" section but no pole_role_line claim naming it with the chain-stage framing | FAIL (a serialized member framed as concurrent — the promised guard). Member names are matched with an explicit non-word/end boundary, not `\\b` (review V5: `\\b` was regex-dead after a `)`-ended matrix-leg name) |
    | 3d | a drilled modal member's rendered "What a change here can buy" figure exceeds the facts-derived chain headroom (median per-PR max(chain_s − runner_up_s, 0) + clock tolerance) | FAIL (review V2 / OD-F2 — a serialized member sized as a concurrent sibling); compat-vacuous without `chain_facts` (OD-E2), undrilled members surfaced in the detail |
    | 4 | re-derived modal chain has >= 2 members (and a summary is stamped) but NO headline_chain claim | FAIL (the renderer went dark on a chained gate) |
    | 5 | modal chain < 2 members but a headline_chain claim exists | FAIL (chain framing over a singleton gate) |
    | 6 | any modal-chain member in `dropped_non_pr_checks`/`dropped_non_required_checks` | FAIL (spine-scoping invariant, PR-N2 requirement i) |
    | 7 | the Bottom line renders "waits **X** for the ... chain" | X == the claim's merge_dur field (itself tied to the re-derived p50 by cell 3); else FAIL |

    Re-derivation mirrors `collect_runs._chain_summary` (median of `chain_s`;
    modal member tuple by count then lexicographic) from the STAMPED per-PR
    `chain_facts` — never from `chain_summary` itself (the summary is the
    renderer's input; re-deriving it from the facts is what makes a drifted
    summary FAIL here instead of vouching for itself)."""
    name = "chain headline re-derives from the stamped chain facts"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no chain gate", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, False, "findings.json unreadable")
    cp = data.get("pr_critical_path") if isinstance(data, dict) else None
    cp = cp if isinstance(cp, dict) else {}
    facts = [f for f in _as_list(cp.get("chain_facts")) if isinstance(f, dict)]

    manifest = _load_claims(report_path)
    chain_claims = [c for c in _as_list(manifest.get("claims"))
                    if isinstance(c, dict) and c.get("kind") == "headline_chain"] \
        if bool(manifest) else []

    if not facts and not chain_claims:
        return Check(name, True, "no chain_facts stamped (pre-chain artifact) "
                     "and no chain claim — nothing to re-derive", skipped=True)

    # Cell 3b: every fact's chain sum must re-derive from its member spans —
    # an inflated stamped chain_s would otherwise ride into the headline with
    # nothing re-adding the members.
    for f in facts:
        spans = f.get("member_spans_s")
        if not isinstance(spans, dict):
            return Check(name, False, "a chain fact carries no member_spans_s")
        try:
            total = sum(float(v) for v in spans.values())
            if abs(total - float(f.get("chain_s") or 0.0)) > 0.01:
                return Check(name, False,
                             f"chain_s {f.get('chain_s')} does not re-derive from its "
                             f"member spans (sum {round(total, 3)})")
        except (TypeError, ValueError):
            return Check(name, False, "non-numeric member span in a chain fact")

    # Re-derive the modal chain + p50 from the facts (mirror of _chain_summary).
    counts: dict[tuple, int] = {}
    for f in facts:
        key = tuple(str(m) for m in _as_list(f.get("chain")))
        counts[key] = counts.get(key, 0) + 1
    modal = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else ()
    spans = sorted(float(f.get("chain_s") or 0.0) for f in facts)
    p50 = 0.0
    if spans:
        mid = len(spans) // 2
        p50 = spans[mid] if len(spans) % 2 else (spans[mid - 1] + spans[mid]) / 2.0

    dropped = {str(x) for x in _as_list(cp.get("dropped_non_pr_checks"))}
    dropped |= {str(x) for x in _as_list(cp.get("dropped_non_required_checks"))}
    hit = [m for m in modal if m in dropped]
    if hit:
        return Check(name, False,
                     f"modal chain member(s) {hit} are spine-DROPPED checks — the chain "
                     "facts were not scoped to the merge-gating spine (requirement i)")

    if len(modal) >= 2 and not chain_claims:
        # Cell 2b: an N1-era artifact stamps facts but no summary — the
        # renderer (which keys on the summary) correctly rendered classic.
        # Only a summary-bearing artifact with a >=2-member modal chain and no
        # claim means the renderer went dark.
        if not isinstance(cp.get("chain_summary"), dict):
            return Check(name, True,
                         "chain_facts without chain_summary (N1-era artifact) — the "
                         "renderer correctly rendered the classic forms", skipped=True)
        return Check(name, False,
                     f"chain_facts show a {len(modal)}-member modal chain "
                     f"({' → '.join(modal)}) but the report mints no headline_chain "
                     "claim — the renderer framed a serialized gate the old way")
    if len(modal) < 2:
        if chain_claims:
            return Check(name, False,
                         "a headline_chain claim exists but the re-derived modal chain "
                         f"has {len(modal)} member(s) — chain framing over a singleton gate")
        return Check(name, True,
                     f"modal chain is a singleton ({' → '.join(modal) or 'none'}) — "
                     "the classic headline forms apply", skipped=True)

    expect_subject = " → ".join(modal)
    modal_n = counts.get(tuple(modal), 0)
    # The physically-coherent rendered wait (issue #22): the sum `p50` clamped into
    # [largest modal member p50, measured makespan p50] — the SAME clamp the renderer applies
    # (`_chain_total`). Re-derived from the FACTS + `checks[]`, never from `chain_summary` (which the
    # renderer keys on) so a drifted clamp FAILs here. makespan = median of the per-PR `makespan_s`
    # (mirrors `_chain_summary`'s `makespan_p50_s`); the member floor = max stamped `checks[].p50_s`
    # over the modal members (what each member's own drill renders as its p50).
    _ms = sorted(float(f.get("makespan_s")) for f in facts
                 if isinstance(f.get("makespan_s"), (int, float)))
    makespan_p50 = None
    if _ms:
        _mmid = len(_ms) // 2
        makespan_p50 = _ms[_mmid] if len(_ms) % 2 else (_ms[_mmid - 1] + _ms[_mmid]) / 2.0
    _modal_set = set(modal)
    _member_p50s = [float(c.get("p50_s")) for c in _as_list(cp.get("checks"))
                    if isinstance(c, dict) and str(c.get("name", "")) in _modal_set
                    and isinstance(c.get("p50_s"), (int, float))]
    largest_member = max(_member_p50s) if _member_p50s else 0.0
    chain_wait = p50
    if makespan_p50 is not None and chain_wait > makespan_p50:
        chain_wait = makespan_p50
    if largest_member > chain_wait:
        chain_wait = largest_member
    fields: dict = {}
    for claim in chain_claims:  # EVERY claim validates (never only the first)
        subject = str(claim.get("subject") or "")
        if subject != expect_subject:
            return Check(name, False,
                         f"headline_chain subject {subject!r} != re-derived modal chain "
                         f"{expect_subject!r}")
        fields = claim.get("fields") if isinstance(claim.get("fields"), dict) else {}
        try:
            claimed_p50 = float(fields.get("chain_p50_s"))
        except (TypeError, ValueError):
            return Check(name, False, "headline_chain claim carries no numeric chain_p50_s")
        if abs(claimed_p50 - p50) > 0.51:
            return Check(name, False,
                         f"headline_chain chain_p50_s {claimed_p50} != re-derived {round(p50, 3)}")
        # The RENDERED figure must re-derive too — a self-consistent wrong
        # merge_dur (fields agree with the prose but not the facts) must FAIL
        # (the pass-A self-vouching probe). The rendered wait is the CLAMPED total
        # (issue #22: the sum floored to the largest member, capped at the measured
        # makespan), not the raw sum `p50` — so re-derive against `chain_wait`.
        if str(fields.get("merge_dur")) != _fmt_clock(chain_wait):
            return Check(name, False,
                         f"rendered chain wait {fields.get('merge_dur')!r} != the "
                         f"facts-re-derived clamped total {_fmt_clock(chain_wait)!r} "
                         f"(sum {_fmt_clock(p50)}, member floor {_fmt_clock(largest_member)}, "
                         f"makespan cap {_fmt_clock(makespan_p50) if makespan_p50 is not None else 'n/a'})")
        # The stamped numeric `chain_wait_p50_s` is what `merge_dur` rounds from — assert it
        # re-derives too, so the raw seconds field can't silently drift from the rendered clock.
        if "chain_wait_p50_s" in fields:
            try:
                if abs(float(fields.get("chain_wait_p50_s")) - chain_wait) > 0.51:
                    return Check(name, False,
                                 f"stamped chain_wait_p50_s {fields.get('chain_wait_p50_s')} != "
                                 f"the facts-re-derived clamped total {round(chain_wait, 3)}")
            except (TypeError, ValueError):
                return Check(name, False, "headline_chain claim carries a non-numeric chain_wait_p50_s")
        try:
            if int(fields.get("modal_n")) != modal_n or int(fields.get("n")) != len(facts):
                return Check(name, False,
                             f"claimed modal_n/n {fields.get('modal_n')}/{fields.get('n')} != "
                             f"re-derived {modal_n}/{len(facts)}")
        except (TypeError, ValueError):
            return Check(name, False, "headline_chain claim carries no numeric modal_n/n")
        rendered = str(claim.get("rendered") or "")
        n_rendered = report.count(rendered) if rendered else 0
        if n_rendered != 1:
            return Check(name, False,
                         f"headline_chain rendered sentence appears {n_rendered}x (expected exactly 1)")
    m = re.search(r"waits \*\*(.+?)\*\* for the .+? chain", report)
    if m and str(fields.get("merge_dur")) != m.group(1):
        return Check(name, False,
                     f"Bottom line chain wait {m.group(1)!r} != the claim's merge_dur "
                     f"{fields.get('merge_dur')!r}")
    # Cell 3d's bound (review V2 / OD-F2), re-derived from the FACTS: the median
    # over per-PR stamps of max(chain_s − runner_up_s, 0) — the same
    # both-counted co-longest rule as the headline win (the runner-up is the
    # whole-chain-zeroed re-walk, so a co-longest competitor nets a PR's win to
    # 0). Never read from `chain_summary` (the renderer's input must not vouch
    # for itself) and never from the rendered text (L1).
    wins = sorted(max(float(f.get("chain_s") or 0.0)
                      - float(f.get("runner_up_s") or 0.0), 0.0) for f in facts)
    _wmid = len(wins) // 2
    win_bound = (wins[_wmid] if len(wins) % 2 else
                 (wins[_wmid - 1] + wins[_wmid]) / 2.0) if wins else 0.0
    # Cell 3c: a modal member with its own rendered Long pole section must
    # carry the chain-stage role — a serialized member framed as concurrent is
    # the contradiction this whole model exists to kill.
    role_claims = [c for c in _as_list(manifest.get("claims"))
                   if isinstance(c, dict) and c.get("kind") == "pole_role_line"]
    undrilled: list[str] = []
    bounded_figs = 0
    for member in modal:
        # Explicit non-word/end boundary, NOT r"\b" (review V5): `\b` cannot
        # match between a `)` and a space, so a matrix-leg member like
        # `test (3.13)` silently skipped this whole guard — regex-dead on the
        # exact artifact class (both deepgram modal members end in `)`).
        # The renderer wraps the post-▸ check as an inline code span (`_safe_span`), so the
        # member is `\`test\`` in the header, not bare `test`. Match EITHER the exact
        # backtick-wrapped member (closing backtick bounds it — no `test`/`test-suite`
        # false match) OR the legacy bare member with a non-word/end boundary.
        m = re.search(r"## .*Long pole \d+:.*▸ (?:`" + re.escape(member) + r"`|"
                      + re.escape(member) + r"(?=\W|$))", report)
        if not m:
            undrilled.append(member)
            continue  # not drilled — no section to mis-frame
        staged = any(str(c.get("subject") or "") == member
                     and "gate chain" in str(c.get("rendered") or "")
                     for c in role_claims)
        if not staged:
            return Check(name, False,
                         f"chain member `{member}` has a Long pole section but no "
                         "chain-stage role claim — a serialized member framed as "
                         "concurrent")
        # Cell 3d: the member's rendered "What a change here can buy" figure(s)
        # must not exceed the facts-derived chain headroom. Directional upper
        # bound + clock-rounding tolerance (L6); a malformed figure FAILS loud
        # rather than parsing as 0 (which would false-pass).
        nxt = report.find("\n## ", m.end())
        section = report[m.start():nxt if nxt != -1 else len(report)]
        for line in section.splitlines():
            if "What a change here can buy (wall-clock):" not in line:
                continue
            figs = re.findall(r"up to \*\*~([0-9hms :]+?)\*\*", line)
            if not figs:
                if "~0s for now" in line:
                    continue  # the zero-headroom (co-longest) form claims no win
                # Fail LOUD on an unrecognized form rather than pass vacuously
                # (post-merge review of #220): a renderer reword that stops
                # matching the figure regex must break here, not silently
                # un-bound every member note (L7/L8).
                return Check(name, False,
                             f"chain member `{member}`'s floor note matches no "
                             "known form (no 'up to **~<clock>**' figure and not "
                             "the zero-headroom form) — cell 3d cannot bound it; "
                             "update the check together with the renderer wording")
            for fig in figs:
                secs = _parse_clock_to_s(fig)
                if secs is None:
                    return Check(name, False,
                                 f"unparseable win figure {fig!r} in chain member "
                                 f"`{member}`'s floor note")
                bounded_figs += 1
                if secs > win_bound + 0.51:
                    return Check(name, False,
                                 f"chain member `{member}`'s floor note claims "
                                 f"~{fig} but the facts re-derive the chain headroom "
                                 f"at ~{_fmt_clock(win_bound)} (median per-PR "
                                 "chain_s − runner_up_s) — a serialized member sized "
                                 "as a concurrent sibling")
    return Check(name, True,
                 f"chain headline `{expect_subject}` re-derived from {len(facts)} chain fact(s), "
                 f"p50 ~{_fmt_clock(p50)}, claim(s) bound exactly once; "
                 f"{bounded_figs} member floor-note figure(s) within the "
                 f"facts-derived headroom ~{_fmt_clock(win_bound)}"
                 + (f"; undrilled member(s) skipped: {', '.join(undrilled)}"
                    if undrilled else ""))


# --- The physical-bounds invariant family (issue #25) ------------------------------------------
# Three machine-checkable coherence guards over the rendered spine. All re-derive their ground-truth
# comparison values from `findings.json` (never rendered text as source; L1), mirror the engine's
# exact keying/metric/selection (L3/L4/L5), SKIP loud on missing inputs (L8), and pin the renderer
# literals they text-key on (L7). Each catches a Class-C bound violation internally that would
# otherwise only surface via dogfood: a rendered total below a member it sums (issue #22), a headline
# wait above the measured wall (issue #24), or a saving above the compute it claims to cut.

def _vr_chain_makespan_p50(cp: dict) -> float | None:
    """The measured makespan p50 re-derived from `chain_facts[].makespan_s` (median) — mirrors
    `collect_runs._chain_summary`'s `makespan_p50_s`. Re-derived from the FACTS, never read from
    `chain_summary` (the renderer's own input must not vouch for a bound taken against it). None when
    no fact carries a numeric makespan (a legacy pre-makespan artifact)."""
    vals = sorted(float(f.get("makespan_s")) for f in _as_list(cp.get("chain_facts"))
                  if isinstance(f, dict) and isinstance(f.get("makespan_s"), (int, float)))
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _vr_modal_chain(cp: dict) -> list[str]:
    """The modal chain (most-frequent member tuple, ties lexicographic) re-derived from
    `chain_facts` — the same reduction as `_chain_summary`. [] when < 2 members or no facts."""
    counts: dict[tuple, int] = {}
    for f in _as_list(cp.get("chain_facts")):
        if not isinstance(f, dict):
            continue
        key = tuple(str(m) for m in _as_list(f.get("chain")))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return []
    modal = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return list(modal) if len(modal) >= 2 else []


def _vr_headline_merge_dur_s(report: str, report_path: Path | None) -> tuple[float | None, str]:
    """The rendered headline merge-wait in seconds + a provenance string. Prefer the claims manifest
    (`headline_chain` / `headline_slowest` carry a `merge_dur` field — a FIELD read, not a text
    scrape), else the pinned renderer literal `**<clock> until all checks finish**` (L7). (None, why)
    when neither is present (a chainless/npop-less headline mints no such figure)."""
    manifest = _load_claims(report_path)
    if bool(manifest):
        for kind in ("headline_chain", "headline_slowest"):
            for c in _as_list(manifest.get("claims")):
                if isinstance(c, dict) and c.get("kind") == kind:
                    md = str(_as_dict(c.get("fields")).get("merge_dur") or "")
                    secs = _parse_clock_to_s(md)
                    if secs is not None:
                        return secs, f"claim {kind}.merge_dur {md!r}"
    m = re.search(r"\*\*([0-9hms :]+?) until all checks finish", report)
    if m:
        secs = _parse_clock_to_s(m.group(1))
        if secs is not None:
            return secs, f"rendered {m.group(1)!r}"
    return None, "no 'X until all checks finish' headline figure"


def check_aggregate_total_ge_largest_member(report: str, findings_path: Path | None,
                                            report_path: Path | None = None) -> Check:
    """**Physical bound (a) — an aggregate total is never below a member it sums (issue #22).** The
    chain headline frames the wait as its `needs:` members' times adding up, so the rendered chain
    total must be >= the largest single member's own p50 — a serial chain cannot finish faster than
    its longest stage. The engine's `chain_p50` is a median of per-PR summed spans DILUTED by fast
    PRs, so it could render a "17m18 total" while `miri-test`'s own drill showed 18m36 (tokio). Locate
    the total in the rendered headline (`merge_dur`), source the member p50 from `findings.json`
    (`checks[].p50_s` for the modal members — what each member's own Long-pole drill renders; L1), and
    FAIL when the total is below the largest member. SKIPs when there is no chain headline (no
    aggregate total is claimed), or the modal members carry no numeric p50 (nothing to bound against).
    Standalone; keys on `pr_critical_path.chain_facts` + `checks[]`."""
    name = "rendered chain total is never below the largest member it sums"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no aggregate total", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    modal = _vr_modal_chain(cp)
    if len(modal) < 2:
        return Check(name, True, "no >=2-member modal chain — no aggregate total claimed",
                     skipped=True)
    total_s, prov = _vr_headline_merge_dur_s(report, report_path)
    if total_s is None:
        return Check(name, True, f"chain modal exists but no rendered total found ({prov})",
                     skipped=True)
    modal_set = set(modal)
    members = [(str(c.get("name", "")), float(c.get("p50_s")))
               for c in _as_list(cp.get("checks"))
               if isinstance(c, dict) and str(c.get("name", "")) in modal_set
               and isinstance(c.get("p50_s"), (int, float))]
    if not members:
        return Check(name, True, "modal members carry no numeric checks[].p50_s to bound against",
                     skipped=True)
    mname, mp50 = max(members, key=lambda kv: kv[1])
    # Directional lower bound + clock-rounding tolerance (L6): the total is rendered to whole seconds.
    if total_s + 1.5 < mp50:
        return Check(name, False,
                     f"the chain headline totals {_fmt_clock(total_s)} ({prov}) but its member "
                     f"`{mname}` renders {_fmt_clock(mp50)} (checks[].p50_s) — a serial `needs:` "
                     "chain cannot finish faster than its longest stage; the total is below a "
                     "member it claims to sum (the tokio miri-test shape)")
    return Check(name, True,
                 f"chain total {_fmt_clock(total_s)} >= largest member `{mname}` "
                 f"{_fmt_clock(mp50)} ({len(members)} modal member(s) bounded)")


def check_headline_wait_within_makespan(report: str, findings_path: Path | None,
                                        report_path: Path | None = None) -> Check:
    """**Physical bound (b) — the headline wait never exceeds the measured makespan (issue #24).** The
    headline's "X until all checks finish" is a WALL; it can never exceed the measured makespan p50
    (the median per-PR max(end)-min(start) over the span-CAPPED spine checks). The nx `main-linux`
    shape crowned a ~15m08 population floor (the median of per-PR maxima, taken over re-run-inflated
    check-run clocks — its own conditional check p50 was a wider-sample ~46m) as the typical wait
    while the measured makespan was ~11m00. Re-derive the makespan from
    `chain_facts[].makespan_s` (median; the mirror of `chain_summary.makespan_p50_s`, NEVER read from
    the summary the renderer keys on) and FAIL when the rendered `merge_dur` exceeds it. For a CHAIN
    headline the ceiling relaxes to `max(makespan, largest member p50)` — a member's own measured p50
    is itself a hard floor (bound a), so the two bounds stay jointly satisfiable. SKIPs when no
    makespan was stamped (legacy artifact) or no 'until all checks finish' figure is rendered
    (chain-less npop-less headline). Standalone; keys on `pr_critical_path.chain_facts` + `checks[]`."""
    name = "headline merge-wait is never above the measured makespan p50"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no headline wall to bound", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    makespan = _vr_chain_makespan_p50(cp)
    if makespan is None:
        return Check(name, True, "no chain_facts makespan stamped (legacy artifact) — no measured "
                     "wall to bound against", skipped=True)
    wait_s, prov = _vr_headline_merge_dur_s(report, report_path)
    if wait_s is None:
        return Check(name, True, f"no rendered 'until all checks finish' figure ({prov})",
                     skipped=True)
    ceiling = makespan
    # A chain headline may floor to a member above the wall (bound a wins); relax the ceiling to that
    # member so bounds (a) and (b) can't contradict each other on the same coherent, fixed report.
    modal = _vr_modal_chain(cp)
    if len(modal) >= 2:
        modal_set = set(modal)
        member_p50s = [float(c.get("p50_s")) for c in _as_list(cp.get("checks"))
                       if isinstance(c, dict) and str(c.get("name", "")) in modal_set
                       and isinstance(c.get("p50_s"), (int, float))]
        if member_p50s:
            ceiling = max(ceiling, max(member_p50s))
    # Directional upper bound + clock-rounding tolerance (L6).
    if wait_s > ceiling + 1.5:
        _tail = "" if ceiling == makespan else f" (relaxed to the largest chain member {_fmt_clock(ceiling)})"
        return Check(name, False,
                     f"the headline says {_fmt_clock(wait_s)} until all checks finish ({prov}) but "
                     f"the measured makespan p50 is {_fmt_clock(makespan)}{_tail} — a wall can never "
                     "exceed the measured max(end)-min(start); the crowned figure overstates the "
                     "typical wait (the nx main-linux shape)")
    return Check(name, True,
                 f"headline wait {_fmt_clock(wait_s)} ({prov}) <= measured makespan "
                 f"{_fmt_clock(makespan)}"
                 + ("" if ceiling == makespan else f" (chain ceiling {_fmt_clock(ceiling)})"))


def check_headline_wait_is_divergence_correct(report: str, findings_path: Path | None,
                                              report_path: Path | None = None) -> Check:
    """**The chain-vs-makespan divergence-lead class (#115, the CONVERSE of the #24 makespan cap).**
    A serial `needs:` chain finishes only when its last stage ends, but the OBSERVED per-PR wall also
    carries the QUEUE GAPS between stages, so the chain-sum UNDERSTATES the real wait. When the
    measured makespan p50 materially exceeds the chain sum (divergence beyond the Model-check
    threshold — `(chain_p50 - makespan)/makespan * 100 < -25`), the "A typical PR waits **X**"
    headline figure must lead with the OBSERVED makespan, not the chain sum (withastro/astro: a
    16m18s chain sum led the headline while the measured wall was ~69m04s, divergence -76% — and the
    report's own Model check said "Budget on the observed wall"). Re-derives makespan (median
    `chain_facts[].makespan_s`), the chain p50 (median `chain_s`) and the clamped chain wait from the
    FACTS (never from `chain_summary`, the renderer's input), and FAILs when the rendered typical-PR
    wait understates the makespan while the facts say the wall is materially bigger. SKIPs when there
    is no >=2-member modal chain, no stamped makespan, the divergence is within threshold (the
    honest chain-sum lead), or no "typical PR waits" figure is rendered. Standalone; keys on
    `pr_critical_path.chain_facts` + `checks[]` and the rendered bottom line."""
    name = "headline leads with the observed wall when the chain sum diverges from the makespan"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no chain headline to bound", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    modal = _vr_modal_chain(cp)
    if len(modal) < 2:
        return Check(name, True, "no >=2-member modal chain — the classic headline forms apply",
                     skipped=True)
    makespan = _vr_chain_makespan_p50(cp)
    if makespan is None or makespan <= 0:
        return Check(name, True, "no chain_facts makespan stamped (legacy artifact) — no measured "
                     "wall to lead with", skipped=True)
    # Re-derive the chain p50 (median chain_s) and the clamped chain wait — the SAME clamp the
    # renderer applies (sum floored to the largest modal member p50, capped at the makespan).
    spans = sorted(float(f.get("chain_s") or 0.0) for f in _as_list(cp.get("chain_facts"))
                   if isinstance(f, dict) and isinstance(f.get("chain_s"), (int, float)))
    if not spans:
        return Check(name, True, "chain facts carry no numeric chain_s — nothing to re-derive",
                     skipped=True)
    _mid = len(spans) // 2
    chain_p50 = spans[_mid] if len(spans) % 2 else (spans[_mid - 1] + spans[_mid]) / 2.0
    modal_set = set(modal)
    member_p50s = [float(c.get("p50_s")) for c in _as_list(cp.get("checks"))
                   if isinstance(c, dict) and str(c.get("name", "")) in modal_set
                   and isinstance(c.get("p50_s"), (int, float))]
    largest_member = max(member_p50s) if member_p50s else 0.0
    chain_wait = min(chain_p50, makespan)
    chain_wait = max(chain_wait, largest_member)
    divergence = (chain_p50 - makespan) / makespan * 100.0
    if divergence >= -_VR_CHAIN_DIVERGENCE_PCT or makespan <= chain_wait + 0.5:
        return Check(name, True,
                     f"chain-vs-makespan divergence {divergence:+.0f}% within ±"
                     f"{_VR_CHAIN_DIVERGENCE_PCT:.0f}% (chain wait {_fmt_clock(chain_wait)}, makespan "
                     f"{_fmt_clock(makespan)}) — the chain-sum lead is honest", skipped=True)
    # Parse the rendered "A typical PR waits **X**" figure from the Bottom-line paragraph.
    bl = next((ln for ln in report.splitlines() if "**Bottom line.**" in ln), "")
    m = re.search(r"A typical PR waits \*\*~?\s*([0-9hms  :]+?)\*\*", bl)
    wait_s = _parse_clock_to_s(m.group(1)) if m else None
    if wait_s is None:
        return Check(name, True,
                     "chain diverges from the makespan but the Bottom line renders no 'A typical PR "
                     "waits **X**' figure (a no-wait framing) — nothing to bound. Coverage gap, not "
                     "a clean pass.", skipped=True)
    # Directional bound + clock-rounding tolerance: a diverging report must lead with the wall.
    if wait_s + 2.0 < makespan:
        return Check(name, False,
                     f"the chain sum ({_fmt_clock(chain_wait)}) leads the headline (A typical PR waits "
                     f"{_fmt_clock(wait_s)}) but the measured makespan p50 is {_fmt_clock(makespan)} "
                     f"(divergence {divergence:+.0f}%) — queue gaps between serial stages stretch the "
                     "real wait well past the sum; the headline must lead with the observed wall and "
                     "demote the chain sum to attribution (#115, the astro shape)")
    return Check(name, True,
                 f"headline leads with the observed wall {_fmt_clock(wait_s)} >= makespan "
                 f"{_fmt_clock(makespan)} (chain sum {_fmt_clock(chain_wait)}, divergence "
                 f"{divergence:+.0f}%)")


def check_headline_basis_excludes_fileless(report: str, findings_path: Path | None,
                                           report_path: Path | None = None) -> Check:
    """**The fileless-span headline-cap class (issue #12).** The PR-lifetime latency of a
    fileless/managed status check — a bot gate, a label gate, an external app check that produces
    NO sampled workflow job — is NEVER a valid basis for the CI merge-wait headline. Its only timing
    is a `pr_check_runs` span measured from the check's CREATION, so a label that sat open for 8 days
    reads as an 8-day "CI wait" though no CI compute ran (electron/electron: `Backport Labels Added`
    crowning ~8 days while the file-backed poles trace <1% of it). `_pole_caps` cannot de-inflate a
    span it has no sampled job p50 for, so without the engine's exclusion the raw span flows into
    `critical_path_s` / `chain_summary.makespan_p50_s` and crowns the headline.

    The engine (`collect_runs._partition_fileless_checks`) drops the fileless set from the crowning
    basis and stamps it in `pr_critical_path.fileless_status_checks`. This check re-derives the
    contract from `pr_critical_path` (never a rendered proxy for the disjointness) and asserts:
      (a) the crowning basis and the fileless set are DISJOINT — `critical_path_check`, every
          `checks[].name`, every `poles[].check`, and every modal-chain member is ABSENT from the
          stamped `fileless_status_checks`. A fileless name in ANY crowned slot means an excluded
          PR-lifetime span leaked back into the headline/makespan/critical-path basis.
      (b) the fileless set is DISCLOSED, not silently dropped — when `fileless_status_checks` is
          non-empty the rendered report carries the PR-lifetime-status-gating-latency disclosure AND
          the slowest fileless check's stamped span (the rendered disclosure binds to the stamped
          list). The all-fileless degenerate flag renders its own honest line.

    SKIPs on no findings / static-only, and on a LEGACY artifact with no `fileless_status_checks`
    key (pre-#12 engine — nothing to bind). Standalone; keys on `pr_critical_path` fields + the
    disclosure marker only (never a re-rank)."""
    name = "no fileless/managed status check crowns the headline (disclosed as PR-lifetime latency)"
    if not findings_path:
        return Check(name, True, "no findings — no headline basis", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    if "fileless_status_checks" not in cp:
        return Check(name, True, "no fileless_status_checks stamped (pre-#12 artifact) — nothing "
                     "to bind", skipped=True)
    fileless = [c for c in _as_list(cp.get("fileless_status_checks")) if isinstance(c, dict)]
    fileless_names = {_cmp_name(str(c.get("name", ""))) for c in fileless if c.get("name")}
    fileless_names.discard("")
    if not fileless_names:
        # No fileless gates stamped. A static-only report has no headline basis to police at all;
        # a measured report with an empty stamp genuinely owes nothing (basis fully job-groundable).
        if _is_static_only(report):
            return Check(name, True, "no findings / static-only — no headline basis", skipped=True)
        return Check(name, True, "no fileless/managed checks on the sampled PRs — basis is fully "
                     "job-groundable")
    # A NON-EMPTY fileless stamp must be enforced even on a static-only report: an all-fileless repo
    # that ALSO has static hygiene findings renders static-only, and blanket-skipping there would
    # let the very silent-drop this check exists to catch (an excluded gate disclosed nowhere) ship
    # green. Disjointness (a) is trivially satisfied on a static-only doc (no crowned slots), so
    # only the disclosure bind (b) below actually bites there.

    # (a) DISJOINTNESS — re-derive every CROWNED slot from pr_critical_path and assert none is a
    # fileless name. `critical_path_check` is the headline crown; `checks[]` is the whole crowning
    # basis (the ranked spine); `poles[]` are the drilled long poles; the modal chain members are
    # what a chain headline sums. If a fileless span were still in ANY of these it would crown or
    # inflate the merge-wait figure.
    crowned: list[tuple[str, str]] = []
    _crit = cp.get("critical_path_check")
    if _crit:
        crowned.append(("critical_path_check", str(_crit)))
    for c in _as_list(cp.get("checks")):
        if isinstance(c, dict) and c.get("name"):
            crowned.append(("checks[]", str(c.get("name"))))
    for p in _as_list(cp.get("poles")):
        if isinstance(p, dict) and p.get("check"):
            crowned.append(("poles[]", str(p.get("check"))))
    for m in _vr_modal_chain(cp):
        crowned.append(("modal_chain", str(m)))
    leaked = sorted({f"{slot}=`{val}`" for slot, val in crowned
                     if _cmp_name(val) in fileless_names})
    if leaked:
        return Check(name, False,
                     f"a fileless/managed status check crowns the merge-wait basis: {', '.join(leaked)} "
                     f"— but it is stamped in fileless_status_checks (PR-lifetime status-gating "
                     "latency, no sampled workflow job), so its span must never crown the headline / "
                     "makespan / critical path (the electron/electron `Backport Labels Added` shape)")

    # (b) DISCLOSURE — the excluded set must be surfaced, not silently dropped. The renderer emits
    # the PR-lifetime-status-gating-latency marker for every fileless disclosure form (per-gate and
    # all-fileless degenerate), so its ABSENCE means the excluded checks vanished from the report.
    _marker = "PR-lifetime status-gating latency"
    if _marker not in report:
        return Check(name, False,
                     f"{len(fileless_names)} fileless/managed check(s) were excluded from the "
                     "crowning basis but the report carries no PR-lifetime-status-gating-latency "
                     "disclosure — an excluded gate must be disclosed, never silently dropped")
    # Bind the DISCLOSED span to the STAMPED slowest fileless check (sorted slowest-first by the
    # engine), so a disclosure that named a different check/span than the stamp is caught.
    _slowest = max(fileless, key=lambda c: _num(c.get("span_s")) or 0.0)
    _slow_span = _num(_slowest.get("span_s"))
    if _slow_span is not None and _fmt_clock(_slow_span) not in report:
        return Check(name, False,
                     f"the slowest stamped fileless check `{_slowest.get('name')}` "
                     f"({_fmt_clock(_slow_span)}) is not disclosed in the report — the rendered "
                     "fileless disclosure has drifted from the stamped fileless_status_checks list")
    # The all-fileless degenerate flag, when set, must render its own honest "every gating check is
    # fileless" line rather than crowning a status-gating span.
    if cp.get("all_checks_fileless") and "every tracked check" not in report.lower() \
            and "every gating check here is fileless" not in report.lower():
        return Check(name, False,
                     "all_checks_fileless is stamped but the report does not say every gating check "
                     "is fileless — a degenerate all-fileless repo must say so, not crown garbage")
    _deg = " (all-fileless degenerate case disclosed)" if cp.get("all_checks_fileless") else ""
    return Check(name, True,
                 f"{len(fileless_names)} fileless/managed check(s) excluded from the crowning basis "
                 f"and disclosed as PR-lifetime status-gating latency{_deg}")


def check_saving_within_measured_compute(report: str, findings_path: Path | None) -> Check:
    """**Physical bound (c) — a runner-minute saving never exceeds the compute it cuts.** A finding's
    credited `runner_min_saving` (monthly billable minutes it claims to remove) can never exceed the
    MEASURED monthly billable compute of the jobs it touches — you cannot save more minutes than the
    jobs consume. Re-derive each affected job's measured monthly billable compute from the
    `runner_minute_spine` rows (`billable_equiv_min_per_month`, the sampled-and-extrapolated cost the
    spine already stamps and `check_runner_minute_spine_contract` re-derives), matched to the
    finding's `affected_jobs` by workflow_file + job base name (matrix `(variant)` stripped, the same
    `_cmp_name`/base join the spine uses), resolving a finding's YAML job key against the spine's
    `name:`-overridden display name through the scanned `workflow_job_graph` (issue #2) so a
    same-workflow match always beats the cross-workflow same-name fallback (which stays on the
    LITERAL base — graph aliases never widen it). A finding whose jobs ALL resolve to spine rows
    must have saving <= their summed billable compute (directional upper bound + tolerance; L6).
    SKIPs loud when
    there is no render-ready cost spine, or when savings exist but NONE of them resolve any affected
    job to a spine row — the check bounded nothing, so it SKIPs loud rather than pass green (the
    fragile-join failure mode: if the spine's job-naming ever drifts from `affected_jobs`, every
    finding misses the join and an unbounded saving must never render as "within measured compute";
    L8). A per-finding coverage gap where at least one OTHER finding is bounded, and a partial gap
    where only some of a finding's jobs resolve, both surface in the detail (bounded on the resolved
    subset only — never silently presented as full coverage). Standalone; keys on
    `runner_minute_spine.rows` + `findings[].runner_min_saving`/`affected_jobs`."""
    name = "each finding's runner-minute saving is within its jobs' measured compute"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    spine = _as_dict(data.get("runner_minute_spine"))
    rows = _as_list(spine.get("rows"))
    if not rows or spine.get("render_ready") is not True:
        return Check(name, True, "no render-ready runner-minute cost spine — no measured compute to "
                     "bound against", skipped=True)

    # Measured monthly billable compute per (workflow_file, job base). Sum across a job's matrix legs /
    # event scopes / attempts — a finding's saving is over the whole job, so its compute is too.
    def _base(job: str) -> str:
        # Strip a trailing matrix `(variant)` so a finding's bare job name matches its expanded legs.
        return _cmp_name(re.sub(r"\s*\([^()]*\)\s*$", "", str(job or "")).strip())

    # YAML KEY ↔ `name:` OVERRIDE (issue #2). A finding names its job by YAML key (`lint`), but the
    # spine records the job under its rendered DISPLAY name (`Lint project (depot-windows-2022)`) —
    # the key misses the join entirely. Resolve both identities through the scanned
    # `workflow_job_graph` (`{wf: {job_id: {name, ...}}}`, already stamped on the artifact, so no
    # producer change): key → declared `name:`, and DISPLAY name → key for the reverse direction.
    # Candidates stay per-workflow, so a same-workflow resolution is always tried before the
    # cross-workflow same-name fallback below — biome's OPT33 on `lint` bound the unrelated
    # `pull_request_markdown.yml` job literally named `lint` (553 min/mo) instead of its own job's
    # 13,381.6, and false-FAILed. An artifact with no graph keeps the bare-name behavior.
    # Graph-resolved aliases are SAME-WORKFLOW ONLY — they never widen the cross-workflow fallback,
    # which stays on the LITERAL base exactly as before this fix. An alias is evidence about THIS
    # workflow's job ("`lint` here renders as `Lint project`"); carrying it into a foreign workflow
    # would let a job with no spine row of its own bind an unrelated namesake's compute and INFLATE
    # the upper bound, masking an oversized finding. Unresolvable stays an honest coverage gap.
    graph = _as_dict(data.get("workflow_job_graph"))

    def _identities(wf: str, job: str) -> list[str]:
        """Job bases `job` may appear under in `wf`'s spine rows, literal first (so an already-
        matching name keeps today's binding), then the graph-resolved counterpart identity.
        Only valid WITHIN `wf` — the cross-workflow fallback must not use these aliases.
        An AMBIGUOUS display name (two job keys in `wf` rendering to the same name) yields no
        alias: the spine indexes by that one name, so aliasing a key onto it would bound the
        finding by BOTH jobs' summed compute and inflate the ceiling. Ambiguity stays an honest
        coverage gap, same as the cross-workflow rule above."""
        b = _base(job)
        out = [b] if b else []
        jobs_in_wf = _as_dict(graph.get(wf))
        names: dict[str, int] = {}
        for jid, info in jobs_in_wf.items():
            nm = _base(_as_dict(info).get("name") or jid)
            if nm:
                names[nm] = names.get(nm, 0) + 1
        for jid, info in jobs_in_wf.items():
            nm = _base(_as_dict(info).get("name") or jid)
            if names.get(nm, 0) > 1:
                continue  # collision: this display name identifies more than one job — no alias.
            # key → its `name:` override, and display name → its key; both directions, one pass.
            for alias in ((nm,) if _base(jid) == b else ((_base(jid),) if nm == b else ())):
                if alias and alias not in out:
                    out.append(alias)
        return out

    compute: dict[tuple[str, str], float] = {}
    for r in rows:
        r = _as_dict(r)
        wf = str(r.get("workflow_file") or "")
        jb = _base(r.get("job_name") or "")
        bill = _num(r.get("billable_equiv_min_per_month")) or 0.0
        if wf and jb:
            compute[(wf, jb)] = compute.get((wf, jb), 0.0) + bill

    checked = 0
    uncovered: list[str] = []
    partial: list[str] = []
    for f in _as_list(data.get("findings")):
        f = _as_dict(f)
        saving = _num(f.get("runner_min_saving"))
        if saving is None or saving <= 0:
            continue
        wf = str(f.get("workflow_file") or "")
        jobs = [str(j) for j in _as_list(f.get("affected_jobs")) if str(j).strip()]
        if not jobs:
            continue
        # EXACT JOB IDENTITY (issue #52). Reduce the affected jobs to their DISTINCT
        # (workflow_file, base-job) identities before summing — a base-job being the
        # full job name modulo its matrix-leg parenthetical (`_base`). `compute`
        # already SUMS a base's matrix legs into one figure, so listing several legs
        # of ONE job (`build-image / build-image (linux/amd64)` and `(linux/arm64)`)
        # must add that summed figure ONCE, not once per leg — iterating the raw legs
        # double-counted the base (mastodon: 16,642.4 added twice → 33,284.8, wide
        # enough that OPT73's 18,165.8 credit read "within measured compute" when it
        # was not). A DIFFERENT base (`build-image-streaming / build-image`) stays its
        # own identity. This applies the SAME dedupe principle as the door's
        # `_measured_billable_for_jobs` (L3), each through its own base key — the door's
        # `_whole_run_cancel_base_key` is at least as strict as this `_base` (it does no
        # scope-stripping), so the two gates tighten together and the door still bounds
        # to a subset of what this guard bounds.
        matched = 0
        distinct = 0
        bound = 0.0
        seen: set[str] = set()
        for j in jobs:
            b = _base(j)
            if not b:
                continue
            # Same-workflow identities first (literal, then graph-resolved) — a job that resolves
            # in its OWN workflow never reaches the cross-workflow fallback, so a foreign namesake
            # can't win. `compute` already sums a base's matrix legs, so the resolved display name
            # brings the whole job's compute (all legs) in one figure.
            cands = _identities(wf, j)
            key = next(((wf, c) for c in cands if (wf, c) in compute), None)
            ident = key[1] if key else b
            if ident in seen:
                continue
            seen.add(ident)
            distinct += 1
            if key:
                matched += 1
                bound += compute[key]
            else:
                # Job base present under ANY workflow file (a reusable-workflow caller loses the wf).
                # LITERAL base only — graph aliases are same-workflow evidence and must not widen
                # this cross-workflow match (see `_identities`); a job with no row in its own
                # workflow stays an honest coverage gap rather than binding a foreign namesake.
                alt = [v for (w, jb), v in compute.items() if jb == b]
                if alt:
                    matched += 1
                    bound += max(alt)
        if matched == 0:
            uncovered.append(f"{f.get('pattern', '?')}({', '.join(jobs[:2])})")
            continue
        if matched < distinct:
            # Some jobs resolved, some didn't: `bound` is understated (missing the unresolved
            # jobs' compute), so surface the partial coverage — never present it as a full bound.
            partial.append(f"{f.get('pattern', '?')}({matched}/{distinct} jobs)")
        checked += 1
        # Directional upper bound + tolerance (L6): the spine's per-run mean is measured while the
        # finding's saving is a model over the same monthly volume, so allow small rounding slack.
        if saving > bound * 1.02 + 0.1:
            return Check(name, False,
                         f"finding `{f.get('pattern', '?')}` credits {saving:g} min/mo of runner-time "
                         f"but its affected job(s) {jobs} measure only {round(bound, 1):g} min/mo of "
                         "billable compute in the cost spine — a fix cannot save more minutes than "
                         "the jobs consume (the saving model overstates the job's measured cost)")
    detail = f"{checked} credited saving(s) within measured compute"
    if partial:
        detail += (f"; {len(partial)} bounded on the resolved job subset only "
                   f"(partial coverage): {', '.join(partial[:4])}")
    if uncovered:
        detail += (f"; {len(uncovered)} finding(s) had no affected job in the cost spine "
                   f"(coverage gap, not bounded): {', '.join(uncovered[:4])}")
    if checked == 0:
        # Nothing was actually bounded. A bare no-savings run SKIPs; but if there WERE savings we
        # simply couldn't LOCATE in the spine (every affected job missed the join), SKIP LOUDLY
        # rather than pass green — an unbounded saving must never render as "within measured
        # compute" (L8; the fragile-join silent-pass this check would otherwise report).
        why = ("no credited runner-minute savings to bound" if not uncovered
               else f"could not bound any credited saving — {detail}")
        return Check(name, True, why, skipped=True)
    return Check(name, True, detail)


# --- coverage: every "slowest ... waits on" framing phrase is a registered claim (plan 007) ---
# EMBEDDED copy of `claims.FRAMING_VOCABULARY` — verify_report imports no engine module (so this
# runs standalone against any committed report), so it carries its own copy. The coupling test
# `test_framing_vocabulary_stays_coupled_to_claims` pins this tuple equal to `claims.FRAMING_VOCABULARY`
# so the two can't drift. Matched CASE-INSENSITIVELY (the agent-prompt gate line capitalizes
# "Slowest ..."). The three phrases are mutually non-substring, so an occurrence counts once.
_FRAMING_VOCABULARY = (
    "slowest check a typical PR waits on",
    "slowest check a PR waits on",
    "slowest a PR waits on",
    "wall-clock-neutral runner spend",
    "machine-derived proof",
    "modeled bill opportunities remain",
)


def _spans_exact(haystack: str, needle: str) -> list[tuple[int, int]]:
    """All [start, end) spans where `needle` occurs in `haystack` (exact, case-sensitive)."""
    spans, start = [], 0
    while needle:
        i = haystack.find(needle, start)
        if i == -1:
            break
        spans.append((i, i + len(needle)))
        start = i + 1
    return spans


def _framing_spans(report: str, template: str) -> list[tuple[int, int]]:
    """Spans where `template` occurs in `report`, case-insensitively AND whitespace-flexibly: any
    run of whitespace between the template's words matches. Markdown collapses a double space or a
    soft line-wrap to one visible space, so a framing phrase that is double-spaced or line-wrapped
    in the renderer still RENDERS as the sentence — matching it flexibly keeps such a variant from
    escaping the guard (the exact-cased claim spans stay exact, so a registered claim, built from
    the same f-string, still covers it; an UNregistered variant is found here and left unbacked).
    Matched against the ORIGINAL string via `re`, so a length-changing case-fold (`İ`/`ﬁ`/`ß`)
    can't drift the offsets against the claim spans."""
    words = template.split()
    if not words:
        return []
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)
    return [(m.start(), m.end()) for m in pattern.finditer(report)]


def check_claims_cover_framing_vocabulary(report: str, report_path: Path | None = None) -> Check:
    """**The anti-whack-a-mole meta-guard (plan 007).** Every occurrence of a
    `FRAMING_VOCABULARY` phrase in the report must be physically inside a registered claim's
    `rendered` sentence — so a NEW "slowest check ... waits on" framing sentence cannot ship
    unless it was minted through a `Claim`. Active only when the report carries a claims
    manifest declaring the `slowest_gate_framing` family migrated (plan 007's renderer); a
    manifest-less committed artifact or a 002a-era manifest (which declares only `["headline"]`)
    SKIPs — the retained text-parsing guards still cover those. Occurrences are found
    case-insensitively (the prompt gate line capitalizes "Slowest") and whitespace-flexibly (a
    double space / soft line-wrap that markdown renders as one space can't hide a phrase), and
    scanned across the WHOLE report INCLUDING fenced code blocks (the prompt gate line renders
    inside ```text fences — exempting fenced lines would let it escape). Coverage is by SPAN CONTAINMENT, not
    line-equality: one report line can carry two different family phrases (a headline claim's
    phrase AND a concatenated minority-note phrase), and each occurrence needs its own backing
    claim whose rendered sentence spans it."""
    name = "every 'slowest ... waits on' framing phrase is a registered claim"
    manifest = _load_claims(report_path)
    if not manifest or "slowest_gate_framing" not in _as_list(manifest.get("families_migrated")):
        return Check(name, True,
                     "no claims manifest declaring the slowest_gate_framing family (manifest-less "
                     "or pre-007 artifact) — coverage meta-guard not applicable; the text-parsing "
                     "guards still cover this report", skipped=True)
    # Spans occupied by claim `rendered` sentences that ACTUALLY appear in the report (a claim
    # whose sentence isn't present backs nothing). Any-kind: a framing phrase may sit in a
    # headline_slowest, pole_role_line, pole_gate_prompt, or minority_slow_note claim.
    # NOTE (defense-in-depth limit): a claim backs any report text EXACTLY equal to its full
    # `rendered`, so a claim whose `rendered` were a bare template phrase could "cover" an
    # unrelated occurrence. Not reachable from the real renderer (every claim's rendered is a
    # decorated full sentence — `**...**`, `: P50 ...`, trailing names), and the source lint is
    # the second backstop; a precise fix would track each claim's actual emission offset.
    claim_spans: list[tuple[int, int]] = []
    for c in _as_list(manifest.get("claims")):
        if isinstance(c, dict):
            claim_spans.extend(_spans_exact(report, str(c.get("rendered", ""))))
    n_occ, uncovered = 0, []
    for tmpl in _FRAMING_VOCABULARY:
        for (s, e) in _framing_spans(report, tmpl):
            n_occ += 1
            if not any(cs <= s and e <= ce for (cs, ce) in claim_spans):
                line = report.count("\n", 0, s) + 1
                uncovered.append(f"line {line}: {report[s:e]!r} (phrase {tmpl!r})")
    if uncovered:
        return Check(name, False,
                     f"{len(uncovered)} framing phrase occurrence(s) are NOT backed by any "
                     "registered claim (an unregistered 'slowest ... waits on' sentence shipped, "
                     "or a manifest lost a framing claim): " + "; ".join(uncovered[:6]))
    return Check(name, True,
                 f"all {n_occ} framing phrase occurrence(s) across {len(_FRAMING_VOCABULARY)} "
                 "template(s) are backed by a registered claim's rendered sentence")


# --- Tier 2: runner-minute reductions (wall-clock-neutral) -------------------------------

_TIER2_MARKER_RE = re.compile(
    r"<!--\s*ci-speedup:tier2-finding\s+id=([^\s]+)\s+pattern=(OPT\d+)\s*-->")
_RUNNER_MINUTE_SPINE_MARKER_RE = re.compile(
    r"<!--\s*ci-speedup:runner-minute-spine(?:\s+[\s\S]*?)?-->",
    re.IGNORECASE)
_RUNNER_MINUTE_SPINE_HIDDEN_RE = re.compile(
    r"\+(\d+)\s+more\s+runner-minute\s+rows?\s+hidden",
    re.IGNORECASE)
_RUNNER_MINUTE_SPINE_VISIBLE_SURFACE_RE = re.compile(
    r"(?im)^(?:#{1,6}\s+.*(?:cost\s+spine|where\s+runner\s+minutes\s+go)|"
    r"\|\s*total\s*\|(?:[^|\n]*\|){6}\s*[-\d])")
# The single minutes-only cost-spine table shape (the priced-dollar surface — SKU,
# Billing, Weighted min/mo, USD/mo columns — was excised 2026-07-20; git history
# preserves it, #98/#100). Figures are runner-minutes only.
_RUNNER_MINUTE_SPINE_REQUIRED_HEADERS = {
    "workflow": "workflow_file",
    "job": "job_name",
    "runner": "runner_label",
    "event": "event_scope",
    "status": "status_filter",
    "attempt": "attempt_filter",
    "volume": "volume_filter",
    "raw min/mo": "raw_compute_runner_min_per_month",
    "billable min/mo": "billable_equiv_min_per_month",
    "share": "share_of_all_row_total",
}
_VR_RUNNER_MINUTE_SPINE_CAP = 12
_VR_TIER2_CAP = 12
_TIER2_TOPLEVEL_STAMP_KEYS = ("events_by_wf", "repo_visibility")


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        out = float(v)
        return out if math.isfinite(out) else None
    try:
        out = float(str(v))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _load_findings_doc(findings_path: Path | None) -> tuple[dict, str | None]:
    if findings_path is None:
        return {}, "no --findings to inspect"
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"findings unreadable: {e}"
    return _as_dict(data), None


def _tier2_findings(data: dict) -> list[dict]:
    return [f for f in _as_list(data.get("findings"))
            if isinstance(f, dict)
            and not f.get("advisory")
            and f.get("sizing_basis") == "measured"
            and isinstance(f.get("tier2_neutrality"), dict)
            and bool(f.get("tier2_neutrality"))
            and (_num(f.get("runner_min_saving")) or 0.0) > 0
            and _tier2_source_rows_cover_saving(f, _tier2_spine_source_rows(f, data))
            and _tier2_opt64_group_cover_ok(f, data)]


def _has_tier2_stamp_surface(data: dict) -> bool:
    for f in _as_list(data.get("findings")):
        if isinstance(f, dict) and ("sizing_basis" in f or "tier2_neutrality" in f):
            return True
    return False


def _has_tier2_top_level_surface(data: dict) -> bool:
    return any(k in data for k in _TIER2_TOPLEVEL_STAMP_KEYS)


def _has_positive_runner_min_candidate(data: dict) -> bool:
    return any(isinstance(f, dict)
               and not f.get("advisory")
               and not str(f.get("tier2_superseded_by") or "").strip()
               and (_num(f.get("runner_min_saving")) or 0.0) > 0
               for f in _as_list(data.get("findings")))


def _tier2_ranked(data: dict) -> list[dict]:
    # Priced-dollar ranking is retired (2026-07-20); rank by runner-minutes saved.
    return sorted(_tier2_findings(data),
                  key=lambda f: (-(_num(f.get("runner_min_saving")) or 0.0),
                                 str(f.get("pattern", "")),
                                 str(f.get("id", ""))))


def _tier2_visible_ranked(data: dict) -> list[dict]:
    return _tier2_ranked(data)[:_VR_TIER2_CAP]


def _tier2_has_modeled_value(data: dict) -> bool:
    return any(isinstance(f, dict)
               and not f.get("advisory")
               and not str(f.get("tier2_superseded_by") or "").strip()
               and f.get("sizing_basis") != "measured"
               and (_num(f.get("runner_min_saving")) or 0.0) > 0
               for f in _as_list(data.get("findings")))


def _close(got: object, want: float, tol: float = 0.011) -> bool:
    val = _num(got)
    return val is not None and abs(val - want) <= tol


def _split_markdown_row(line: str) -> list[str]:
    raw = str(line).strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    cells: list[str] = []
    cell: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw) and raw[i + 1] == "|":
            cell.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cell))
            cell = []
        else:
            cell.append(ch)
        i += 1
    cells.append("".join(cell))
    return [_strip_runner_spine_cell(cell) for cell in cells]


def _strip_runner_spine_cell(cell: object) -> str:
    text = str(cell).strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1].strip()
    return text


def _visible_markdown_lines(text: str) -> list[str]:
    no_comments = re.sub(r"<!--[\s\S]*?-->", "", str(text or ""))
    out: list[str] = []
    in_fence = False
    for line in no_comments.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and not line.startswith(("    ", "\t")):
            out.append(line)
    return out


def _nonfence_markdown_lines(text: str) -> list[str]:
    """Like `_visible_markdown_lines`, but KEEPS indented lines — it drops only
    HTML comments and fenced code blocks. The rate-derived-dollar sweep uses this
    so a `$`-figure hidden on a 4-space-indented line can't escape (the fence skip
    still protects legitimate `$` inside agent-prompt / shell-echo code fences)."""
    no_comments = re.sub(r"<!--[\s\S]*?-->", "", str(text or ""))
    out: list[str] = []
    # Track WHICH delimiter opened the fence: per CommonMark, a fence closes only
    # on its own delimiter kind — a `~~~` line INSIDE a backtick fence is content,
    # not a closer. An unconditional toggle desyncs on that shape and re-enters
    # fence mode at the real closer, hiding later prose from the sweep (#107 bot
    # review, P1).
    # (char, run_length) of the OPENING fence, or None outside a fence. Per
    # CommonMark a fence closes only on a run of the SAME character AT LEAST as
    # long as the opener — so a ```-line inside a ````-fence is content, and a
    # ~~~-line inside a backtick fence is content (#107 bot review ×2).
    fence: "tuple[str, int] | None" = None

    def _run(stripped: str, ch: str) -> int:
        n = 0
        while n < len(stripped) and stripped[n] == ch:
            n += 1
        return n

    for line in no_comments.splitlines():
        stripped = line.lstrip()
        if fence is None:
            for ch in ("`", "~"):
                n = _run(stripped, ch)
                if n >= 3:
                    fence = (ch, n)
                    break
            else:
                out.append(line)
        else:
            ch, opened = fence
            # A CLOSER is delimiter-only (+ trailing whitespace): an info-string
            # line like ```text inside an open fence is content, not a closer
            # (#107 bot review ×3 — closer spec now complete: open = run>=3 with
            # optional info string; close = same char, run >= opener, nothing
            # else on the line).
            run_length = _run(stripped, ch)
            if run_length >= opened and not stripped[run_length:].strip():
                fence = None
    return out


def _markdown_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in cells)


def _runner_spine_rendered_null(cell: object) -> bool:
    text = _strip_render_artifacts(str(cell)).strip().lower()
    return text in {"", "-", "null", "none", "n/a"}


def _runner_spine_render_num(cell: object, *, percent_allowed: bool = False) -> float | None:
    text = _strip_render_artifacts(str(cell)).strip()
    if _runner_spine_rendered_null(text):
        return None
    pct = text.endswith("%")
    if pct and not percent_allowed:
        return None
    text = text.rstrip("%").replace("$", "").replace(",", "").strip()
    val = _num(text)
    if val is None:
        return None
    return val / 100.0 if pct else val


def _runner_spine_key_from_row(row: dict) -> tuple[str, str, str, str, str, str, str]:
    return (
        str(row.get("workflow_file") or ""),
        str(row.get("job_name") or ""),
        str(row.get("runner_label") or ""),
        str(row.get("event_scope") or ""),
        str(row.get("status_filter") or ""),
        str(row.get("attempt_filter") or ""),
        str(row.get("volume_filter") or ""),
    )


def _runner_spine_visible_sort_key(row: dict) -> tuple:
    billable = _num(_as_dict(row).get("billable_equiv_min_per_month")) or 0.0
    raw = _num(_as_dict(row).get("raw_compute_runner_min_per_month")) or 0.0
    return (-billable, -raw, _runner_spine_key_from_row(_as_dict(row)))


def _visible_runner_minute_spine_table_count(report: str) -> int:
    count = 0
    for line in _visible_markdown_lines(report):
        if not line.strip().startswith("|"):
            continue
        headers = {h.lower() for h in _split_markdown_row(line)}
        if set(_RUNNER_MINUTE_SPINE_REQUIRED_HEADERS).issubset(headers):
            count += 1
    return count


def _visible_runner_minute_spine_surface_count(report: str) -> int:
    return sum(1 for line in _visible_markdown_lines(report)
               if _RUNNER_MINUTE_SPINE_VISIBLE_SURFACE_RE.search(line))


def _parse_runner_minute_spine_table(
    report: str,
) -> tuple[list[dict], dict | None, int | None, str | None]:
    marker = _RUNNER_MINUTE_SPINE_MARKER_RE.search(report or "")
    if not marker:
        return [], None, None, None
    section = report[marker.end():]
    next_heading = re.search(r"^##\s+", section, re.MULTILINE)
    if next_heading:
        section = section[:next_heading.start()]
    lines = _visible_markdown_lines(section)
    table_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
    if table_idx is None or table_idx + 1 >= len(lines):
        return [], None, None, "rendered cost-spine marker has no visible markdown table"
    headers = [h.lower() for h in _split_markdown_row(lines[table_idx])]
    separator = _split_markdown_row(lines[table_idx + 1])
    if not _markdown_separator(separator):
        return [], None, None, "rendered cost-spine table is missing a separator row"
    if len(separator) != len(headers):
        return [], None, None, "rendered cost-spine table separator width does not match headers"
    if len(headers) != len(set(headers)):
        return [], None, None, "rendered cost-spine table has duplicate columns"
    expected_headers = set(_RUNNER_MINUTE_SPINE_REQUIRED_HEADERS)
    missing = [h for h in _RUNNER_MINUTE_SPINE_REQUIRED_HEADERS if h not in headers]
    if missing:
        return [], None, None, (
            f"rendered cost-spine table missing columns: {', '.join(missing)}")
    extra = [h for h in headers if h not in expected_headers]
    if extra:
        return [], None, None, (
            f"rendered cost-spine table has extra columns: {', '.join(extra)}")
    idx = {h: headers.index(h) for h in headers}
    rendered: list[dict] = []
    total: dict | None = None
    total_count = 0
    table_end_idx = len(lines)
    for row_idx, line in enumerate(lines[table_idx + 2:], table_idx + 2):
        if not line.strip().startswith("|"):
            table_end_idx = row_idx
            break
        cells = _split_markdown_row(line)
        if len(cells) != len(headers):
            return [], None, None, "rendered cost-spine table row width does not match headers"
        row = {
            source_key: cells[idx[header]]
            for header, source_key in _RUNNER_MINUTE_SPINE_REQUIRED_HEADERS.items()
        }
        if str(row.get("workflow_file") or "").lower() == "total":
            total_count += 1
            if total_count > 1:
                return [], None, None, "rendered cost-spine table has duplicate Total rows"
            non_total_cells = [
                row.get("job_name"), row.get("runner_label"),
                row.get("event_scope"), row.get("status_filter"), row.get("attempt_filter"),
                row.get("volume_filter"),
            ]
            if any(str(cell or "").strip() for cell in non_total_cells):
                return [], None, None, "rendered cost-spine Total row has non-empty metadata cells"
            total = row
        else:
            if total is not None:
                return [], None, None, "rendered cost-spine Total row must be final"
            rendered.append(row)
    if not rendered:
        return [], total, None, "rendered cost-spine table has no data rows"
    hidden_count: int | None = None
    trailing_lines = lines[table_end_idx:]
    first_trailing = next((line.strip() for line in trailing_lines if line.strip()), "")
    if first_trailing:
        hidden_match = _RUNNER_MINUTE_SPINE_HIDDEN_RE.fullmatch(first_trailing)
        hidden_count = int(hidden_match.group(1)) if hidden_match else None
        if hidden_count is None and any(
                _RUNNER_MINUTE_SPINE_HIDDEN_RE.search(line) for line in trailing_lines):
            return [], None, None, "rendered cost-spine hidden-row disclosure is not adjacent to the table"
    return rendered, total, hidden_count, None


def _rendered_number_problem(label: str, got_cell: object, want: object, *,
                             tol: float = 0.011,
                             percent_allowed: bool = False) -> str | None:
    got = _runner_spine_render_num(got_cell, percent_allowed=percent_allowed)
    expected = _num(want)
    if expected is None:
        return None if _runner_spine_rendered_null(got_cell) else f"{label} should render as null"
    if got is None or abs(got - expected) > tol:
        return f"{label} does not match source"
    return None


def check_runner_minute_spine_contract(report: str, findings_path: Path | None) -> Check:
    name = "runner-minute cost spine source block is re-derivable"
    marker_count = len(_RUNNER_MINUTE_SPINE_MARKER_RE.findall(report or ""))
    rendered = marker_count > 0
    visible_spine_table_count = _visible_runner_minute_spine_table_count(report)
    visible_spine_surface_count = _visible_runner_minute_spine_surface_count(report)
    unmarked_visible = (
        visible_spine_table_count > (1 if rendered else 0) or
        (not rendered and visible_spine_surface_count > 0))
    visible_surface = rendered or visible_spine_table_count > 0 or visible_spine_surface_count > 0
    data, err = _load_findings_doc(findings_path)
    if err:
        if visible_surface:
            return Check(name, False,
                         f"rendered cost-spine marker/table without readable findings JSON: {err}")
        return Check(name, True, err, skipped=True)
    has_spine = "runner_minute_spine" in data
    spine = data.get("runner_minute_spine")
    if not isinstance(spine, dict):
        if has_spine:
            return Check(name, False, "runner_minute_spine source block is not an object")
        if visible_surface:
            return Check(
                name, False,
                "rendered cost-spine marker/table without runner_minute_spine source block")
        return Check(name, True, "no runner_minute_spine block and no rendered cost spine", skipped=True)
    rows_raw = spine.get("rows")
    totals_raw = spine.get("totals")
    render_ready = spine.get("render_ready")
    problems: list[str] = []
    if not isinstance(rows_raw, list):
        problems.append("runner_minute_spine.rows must be a list")
        rows: list = []
    elif not rows_raw and (
            render_ready is True or str(spine.get("coverage_scope") or "") !=
            "sampled_workflows_in_play_with_job_data"):
        problems.append(
            "runner_minute_spine.rows must be non-empty unless explicit coverage is blocked")
        rows: list = []
    else:
        rows = rows_raw
    if not isinstance(totals_raw, dict):
        problems.append("runner_minute_spine.totals must be an object")
        totals = {}
    else:
        totals = totals_raw
    rendered_rows: list[dict] = []
    rendered_total: dict | None = None
    rendered_hidden_count: int | None = None
    render_parse_error: str | None = None
    if rendered:
        if render_ready is not True:
            problems.append("rendered cost-spine marker is blocked while render_ready is not true")
        else:
            (rendered_rows, rendered_total, rendered_hidden_count,
             render_parse_error) = (
                _parse_runner_minute_spine_table(report))
    if marker_count > 1:
        problems.append("multiple runner-minute-spine markers are not allowed")
    if unmarked_visible:
        problems.append("visible cost-spine surface is missing the runner-minute-spine marker")
    if spine.get("schema_version") != 1:
        problems.append(f"schema_version={spine.get('schema_version')!r}")
    if str(spine.get("source") or "") != "jobs_api_sampled_runs":
        problems.append(f"source={spine.get('source')!r}")
    coverage_scope = str(spine.get("coverage_scope") or "")
    if coverage_scope not in {
            "sampled_workflows_with_job_data",
            "sampled_workflows_in_play_with_job_data",
    }:
        problems.append(f"coverage_scope={spine.get('coverage_scope')!r}")
    if not rendered and render_ready is not False:
        problems.append("render_ready must be false until rendered cells are round-tripped")
    if render_ready is False and not str(spine.get("render_blocker") or "").strip():
        problems.append("render_blocker is required while render_ready is false")
    if str(spine.get("extrapolation_basis") or "") != (
            "sampled_job_occurrence_fraction_x_all_status_30d_workflow_volume"):
        problems.append(f"extrapolation_basis={spine.get('extrapolation_basis')!r}")
    if str(spine.get("attempt_coverage") or "") != "latest_and_prior":
        problems.append(f"attempt_coverage={spine.get('attempt_coverage')!r}")
    stamped_prior_count = spine.get("prior_attempt_row_count")
    if bool(spine.get("prior_attempts_included")) != (
            isinstance(stamped_prior_count, int) and stamped_prior_count > 0):
        problems.append(
            "prior_attempts_included must equal prior_attempt_row_count > 0 "
            "(derived fact: it states what the sample contains, PR-S2)")
    repo_visibility = str(spine.get("repo_visibility") or data.get("repo_visibility") or "").lower()
    if repo_visibility not in {"public", "private", "internal"}:
        repo_visibility = ""
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    source_rows_by_key: dict[tuple[str, str, str, str, str, str, str], dict] = {}
    row_workflows: set[str] = set()
    latest_attempt_row_count = 0
    prior_attempt_row_count = 0
    total_raw = total_billed = 0.0
    for idx, raw_row in enumerate(rows, 1):
        row = _as_dict(raw_row)
        key = _runner_spine_key_from_row(row)
        if not all(key):
            problems.append(f"row {idx}: incomplete identity {key!r}")
        elif key in seen:
            problems.append(f"row {idx}: duplicate identity {key!r}")
        seen.add(key)
        source_rows_by_key[key] = row
        row_workflows.add(key[0])
        if key[3] not in {"all-events", "sampled-scope", "pull_request", "pull_request_target",
                          "merge_group", "push", "schedule"}:
            problems.append(f"row {idx}: event_scope={key[3]!r}")
        if key[4] not in {"success", "all-status"}:
            problems.append(f"row {idx}: status_filter={key[4]!r}")
        if key[5] not in {"latest", "prior"}:
            problems.append(f"row {idx}: attempt_filter={key[5]!r}")
        if key[5] == "latest":
            latest_attempt_row_count += 1
        if key[5] == "prior":
            prior_attempt_row_count += 1
        if key[5] == "latest" and key[4] != "success":
            problems.append(f"row {idx}: latest rows must use status_filter='success'")
        if key[5] == "prior" and key[4] != "all-status":
            problems.append(f"row {idx}: prior rows must use status_filter='all-status'")
        if key[6] != "all-status":
            problems.append(f"row {idx}: volume_filter={key[6]!r}")
        sample_start = str(row.get("sample_window_start") or "").strip()
        sample_end = str(row.get("sample_window_end") or "").strip()
        if not sample_start or not sample_end:
            problems.append(f"row {idx}: sample window start/end required")
        elif sample_start > sample_end:
            problems.append(f"row {idx}: sample_window_start after sample_window_end")
        workflow_n = _num(row.get("sampled_workflow_run_count"))
        occurrence_n = _num(row.get("sampled_job_occurrence_count"))
        monthly = _num(row.get("workflow_30d_volume"))
        mean_s = _num(row.get("mean_sampled_compute_seconds"))
        mean_billed = _num(row.get("mean_sampled_billable_equiv_minutes"))
        if (workflow_n is None or workflow_n <= 0 or occurrence_n is None or occurrence_n <= 0
                or monthly is None or monthly < 0 or mean_s is None or mean_s < 0
                or mean_billed is None or mean_billed < 0):
            problems.append(f"row {idx}: invalid numeric basis")
            continue
        occurrence_fraction = occurrence_n / workflow_n
        effective_monthly = monthly * occurrence_fraction
        raw_min = (mean_s / 60.0) * effective_monthly
        billed_min = mean_billed * effective_monthly
        # Per-occurrence 1-minute billing floor, made re-derivable by the source
        # block. GitHub bills each RUN job a minimum of 1 minute, so the sample's
        # total billable minutes must be >= the number of positive-duration
        # occurrences. Zero-span occurrences bill 0 (they drag the MEAN below 1.0
        # while the MEAN compute-second stays positive), so the old aggregate test
        # `mean_s > 0 -> mean_billed >= 1` false-fired on any bucket that mixed a
        # short real run with zero-span occurrences (seen on electron/electron).
        # We recover the integer sample total (mean_billed * occurrences rounds
        # back to an integer — each term is a whole ceil'd minute) and compare it
        # to the stamped positive-duration count.
        pos_occ = _num(row.get("sampled_positive_duration_occurrence_count"))
        if pos_occ is None or pos_occ < 0 or pos_occ > occurrence_n:
            problems.append(
                f"row {idx}: sampled_positive_duration_occurrence_count "
                f"{row.get('sampled_positive_duration_occurrence_count')!r} outside "
                f"[0, {occurrence_n:g}]")
        elif (mean_s > 0) and pos_occ <= 0:
            problems.append(
                f"row {idx}: positive mean compute seconds but zero positive-duration "
                f"occurrences (billing floor is unverifiable)")
        elif round(mean_billed * occurrence_n) < pos_occ:
            problems.append(
                f"row {idx}: sampled billable minutes below the per-occurrence "
                f"1-minute floor ({round(mean_billed * occurrence_n):g} < {pos_occ:g})")
        if mean_billed + 0.011 < mean_s / 60.0:
            problems.append(
                f"row {idx}: mean_sampled_billable_equiv_minutes below duration lower bound")
        if not _close(row.get("occurrence_fraction"), round(occurrence_fraction, 3)):
            problems.append(f"row {idx}: occurrence_fraction does not rederive")
        if not _close(row.get("effective_monthly_job_volume"), round(effective_monthly, 3)):
            problems.append(f"row {idx}: effective_monthly_job_volume does not rederive")
        if not _close(row.get("raw_compute_runner_min_per_month"), round(raw_min, 3)):
            problems.append(f"row {idx}: raw_compute_runner_min_per_month does not rederive")
        if not _close(row.get("billable_equiv_min_per_month"), round(billed_min, 3)):
            problems.append(f"row {idx}: billable_equiv_min_per_month does not rederive")
        total_raw += round(raw_min, 3)
        total_billed += round(billed_min, 3)
    if not _close(totals.get("row_count"), float(len(rows)), 0.0):
        problems.append("totals.row_count does not match rows")
    if not _close(totals.get("raw_compute_runner_min_per_month"), round(total_raw, 3)):
        problems.append("totals.raw_compute_runner_min_per_month does not rederive")
    if not _close(totals.get("billable_equiv_min_per_month"), round(total_billed, 3)):
        problems.append("totals.billable_equiv_min_per_month does not rederive")
    if str(totals.get("percentage_denominator") or "") != "all_rows_billable_equiv_min_per_month":
        problems.append("totals.percentage_denominator is not all-row billable minutes")
    if spine.get("latest_attempt_row_count") != latest_attempt_row_count:
        problems.append("latest_attempt_row_count does not match rows")
    if spine.get("prior_attempt_row_count") != prior_attempt_row_count:
        problems.append("prior_attempt_row_count does not match rows")
    complete_flag = spine.get("complete_repo_coverage")
    if coverage_scope == "sampled_workflows_with_job_data":
        if complete_flag is not False:
            problems.append("complete_repo_coverage must be false for sampled-only coverage")
    elif coverage_scope == "sampled_workflows_in_play_with_job_data":
        workflow_coverage = _as_dict(spine.get("workflow_coverage"))
        omitted_raw = workflow_coverage.get("omitted_workflows")
        unknown_raw = workflow_coverage.get("unknown_volume_workflows")
        triaged_raw = workflow_coverage.get("triaged_workflows_included")
        if not isinstance(omitted_raw, list):
            problems.append("workflow_coverage.omitted_workflows must be a list")
        if not isinstance(unknown_raw, list):
            problems.append("workflow_coverage.unknown_volume_workflows must be a list")
        if not isinstance(triaged_raw, list):
            problems.append("workflow_coverage.triaged_workflows_included must be a list")
        omitted_workflows = [str(wf) for wf in _as_list(omitted_raw)]
        unknown_volume_workflows = [str(wf) for wf in _as_list(unknown_raw)]
        triaged_included = [str(wf) for wf in _as_list(triaged_raw)]
        workflow_count = _num(workflow_coverage.get("workflow_count"))
        row_workflow_count = _num(workflow_coverage.get("row_workflow_count"))
        fetch_failures = _num(workflow_coverage.get("job_fetch_failures"))
        data_sources = _as_dict(data.get("data_sources"))
        has_ds_fetch_failures = "cost_spine_job_fetch_failures" in data_sources
        ds_fetch_failures = (
            _num(data_sources.get("cost_spine_job_fetch_failures"))
            if has_ds_fetch_failures else None)
        volumes_raw = data.get("per_workflow_monthly_volume")
        volume_by_wf = volumes_raw if isinstance(volumes_raw, dict) else {}
        finding_workflows = sorted({
            str(f.get("workflow_file") or "")
            for f in _as_list(data.get("findings"))
            if isinstance(f, dict) and _is_workflow_file(str(f.get("workflow_file") or ""))
        })
        positive_volume_workflows = sorted(
            str(wf) for wf, volume in volume_by_wf.items()
            if (isinstance(volume, int) and not isinstance(volume, bool) and volume > 0))
        expected_unknown_volume = sorted(
            str(wf) for wf, volume in volume_by_wf.items()
            if (not (isinstance(volume, int) and not isinstance(volume, bool))
                or volume < 0))
        expected_omitted = sorted(set(positive_volume_workflows) - row_workflows)
        expected_row_workflow_count = len(set(positive_volume_workflows) & row_workflows)
        if str(workflow_coverage.get("scope") or "") != "positive_30d_workflows_in_play":
            problems.append("workflow_coverage.scope is not positive_30d_workflows_in_play")
        if not isinstance(volumes_raw, dict):
            problems.append("per_workflow_monthly_volume is required for workflow coverage")
        missing_finding_workflows = sorted(
            wf for wf in finding_workflows if wf not in volume_by_wf)
        if missing_finding_workflows:
            problems.append(
                "per_workflow_monthly_volume missing finding-backed workflow")
        if any(wf not in volume_by_wf for wf in row_workflows):
            problems.append("runner_minute_spine row workflow missing from per_workflow_monthly_volume")
        if (workflow_count is None or workflow_count < 0 or
                int(workflow_count) != workflow_count):
            problems.append("workflow_coverage.workflow_count must be a non-negative integer")
        elif int(workflow_count) != len(positive_volume_workflows):
            problems.append("workflow_coverage.workflow_count does not match per_workflow_monthly_volume")
        valid_row_workflow_count = True
        if (row_workflow_count is None or row_workflow_count < 0 or
                int(row_workflow_count) != row_workflow_count):
            problems.append("workflow_coverage.row_workflow_count must be a non-negative integer")
            valid_row_workflow_count = False
        elif row_workflow_count > len(row_workflows):
            problems.append("workflow_coverage.row_workflow_count exceeds row workflows")
        elif int(row_workflow_count) != expected_row_workflow_count:
            problems.append("workflow_coverage.row_workflow_count does not match row workflows")
        if (fetch_failures is None or fetch_failures < 0 or
                int(fetch_failures) != fetch_failures):
            problems.append("workflow_coverage.job_fetch_failures must be a non-negative integer")
        if not has_ds_fetch_failures:
            problems.append("data_sources.cost_spine_job_fetch_failures is required")
        elif fetch_failures is not None:
            if (ds_fetch_failures is None or ds_fetch_failures < 0 or
                    int(ds_fetch_failures) != ds_fetch_failures):
                problems.append("data_sources.cost_spine_job_fetch_failures must be a non-negative integer")
            elif int(ds_fetch_failures) != int(fetch_failures):
                problems.append(
                    "workflow_coverage.job_fetch_failures does not match data_sources")
        if sorted(omitted_workflows) != expected_omitted:
            problems.append("workflow_coverage.omitted_workflows does not match rows and volume")
        if sorted(unknown_volume_workflows) != expected_unknown_volume:
            problems.append("workflow_coverage.unknown_volume_workflows does not match volume gaps")
        if any(wf in row_workflows for wf in omitted_workflows):
            problems.append("workflow_coverage.omitted_workflows contains workflow with rows")
        if (workflow_count is not None and row_workflow_count is not None and
                valid_row_workflow_count and int(workflow_count) <
                int(row_workflow_count)):
            problems.append("workflow_coverage.workflow_count is less than row_workflow_count")
        if (workflow_count is not None and row_workflow_count is not None and
                valid_row_workflow_count and int(workflow_count) !=
                int(row_workflow_count) + len(omitted_workflows)):
            problems.append("workflow_coverage counts do not reconcile with omitted_workflows")
        expected_complete = (
            workflow_count is not None and row_workflow_count is not None and
            fetch_failures is not None and not omitted_workflows and
            not unknown_volume_workflows and int(fetch_failures) == 0 and
            int(row_workflow_count) == int(workflow_count) and
            int(workflow_count) == len(positive_volume_workflows) and
            int(row_workflow_count) == expected_row_workflow_count)
        if not isinstance(complete_flag, bool):
            problems.append("complete_repo_coverage must be boolean")
        elif complete_flag != expected_complete:
            problems.append("complete_repo_coverage does not match workflow_coverage")
        if any(wf not in row_workflows for wf in triaged_included):
            problems.append("triaged_workflows_included contains workflow without rows")
    if complete_flag is True and render_ready is not True:
        problems.append("complete_repo_coverage requires render_ready true")
    if render_ready is True and complete_flag is not True:
        problems.append("render_ready true requires complete_repo_coverage")
    share_denominator = _num(totals.get("billable_equiv_min_per_month")) or 0.0
    for idx, row in enumerate(rows, 1):
        billed = _num(_as_dict(row).get("billable_equiv_min_per_month"))
        expected_share = (
            billed / share_denominator
            if share_denominator > 0 and billed is not None else 0.0)
        if not _close(_as_dict(row).get("share_of_all_row_total"),
                      round(expected_share, 3), tol=0.0005):
            problems.append(f"row {idx}: share_of_all_row_total does not rederive")
    if rendered and render_ready is True:
        if render_parse_error:
            problems.append(render_parse_error)
        else:
            rendered_seen: set[tuple[str, str, str, str, str, str, str]] = set()
            rendered_keys: list[tuple[str, str, str, str, str, str, str]] = []
            for idx, rendered_row in enumerate(rendered_rows, 1):
                key = _runner_spine_key_from_row(rendered_row)
                if key in rendered_seen:
                    problems.append(f"rendered row {idx}: duplicate identity {key!r}")
                    continue
                rendered_seen.add(key)
                rendered_keys.append(key)
                source_row = source_rows_by_key.get(key)
                if source_row is None:
                    problems.append(f"rendered row {idx}: no matching source row {key!r}")
                    continue
                comparisons = [
                    ("raw min/mo", rendered_row.get("raw_compute_runner_min_per_month"),
                     source_row.get("raw_compute_runner_min_per_month"), 0.011),
                    ("billable min/mo", rendered_row.get("billable_equiv_min_per_month"),
                     source_row.get("billable_equiv_min_per_month"), 0.011),
                    ("share", rendered_row.get("share_of_all_row_total"),
                     source_row.get("share_of_all_row_total"), 0.0005),
                ]
                for label, got, want, tol in comparisons:
                    problem = _rendered_number_problem(f"rendered row {idx}: {label}", got, want,
                                                       tol=tol,
                                                       percent_allowed=(label == "share"))
                    if problem:
                        problems.append(problem)
            expected_visible_keys = [
                _runner_spine_key_from_row(_as_dict(row))
                for row in sorted(rows, key=_runner_spine_visible_sort_key)[
                    :min(len(rows), _VR_RUNNER_MINUTE_SPINE_CAP)]
            ]
            if rendered_keys != expected_visible_keys:
                problems.append("rendered cost-spine rows do not match the sorted visible source rows")
            hidden = len(rows) - len(rendered_seen)
            if hidden < 0:
                problems.append("rendered cost-spine table has more rows than the source block")
            elif hidden > 0:
                if rendered_hidden_count != hidden:
                    problems.append(
                        f"rendered cost-spine table hides {hidden} row(s) without disclosure")
            elif rendered_hidden_count is not None:
                problems.append("rendered cost-spine table discloses hidden rows but none are hidden")
            if rendered_total is None:
                problems.append("rendered cost-spine table missing Total row")
            else:
                total_comparisons = [
                    ("total raw min/mo", rendered_total.get("raw_compute_runner_min_per_month"),
                     totals.get("raw_compute_runner_min_per_month"), 0.011),
                    ("total billable min/mo", rendered_total.get("billable_equiv_min_per_month"),
                     totals.get("billable_equiv_min_per_month"), 0.011),
                    ("total share", rendered_total.get("share_of_all_row_total"), 1.0, 0.0005),
                ]
                for label, got, want, tol in total_comparisons:
                    problem = _rendered_number_problem(label, got, want, tol=tol,
                                                       percent_allowed=(label == "total share"))
                    if problem:
                        problems.append(problem)
    if problems:
        return Check(name, False, "; ".join(problems[:12]))
    return Check(name, True, f"runner_minute_spine rows re-derived ({len(rows)} row(s))")


def _text_from_json(v: object) -> str:
    if isinstance(v, dict):
        return " ".join(_text_from_json(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(_text_from_json(x) for x in v)
    return str(v or "")


def _post_completion_waste_corroborated(f: dict) -> bool:
    cert = _as_dict(f.get("tier2_neutrality"))
    me = _as_dict(f.get("measured_evidence"))
    table = _as_dict(me.get("table"))
    signal = str(f.get("measured_signal") or "").lower()
    ref = str(cert.get("ref") or "").lower()
    evidence_text = _text_from_json(me).lower()
    headers = [str(h).lower() for h in _as_list(table.get("headers"))]
    rows = _as_list(table.get("rows"))
    pat = str(f.get("pattern") or "")
    if pat == "OPT46":
        return (
            "superseded" in signal
            # The credited figure is the cancellable REMAINDER, not the whole run
            # (issue #89); the signal AND the rendered evidence must disclose that
            # basis, or the whole-run over-charge is back on the surface users see.
            and "remainder" in signal
            and "same head_branch" in ref
            and "newer run started before" in ref
            and "timestamp overlap" in ref
            and any("overlapping" in h and "run" in h for h in headers)
            and bool(rows)
            and "remainder" in evidence_text
            and "cancellation cause" in evidence_text
            and "inference" in evidence_text)
    if pat == "OPT35":
        return (
            "fail-fast:false" in signal
            and "post-failure" in signal
            and "first failed shard" in ref
            and "sibling shard" in ref
            and any("post-failure" in h and "min" in h for h in headers)
            and bool(rows)
            and "diagnostic matrices" in evidence_text
            and "first failed" in evidence_text)
    if pat == "OPT64":
        return (
            "run_attempt>1" in signal
            and "filter=all" in signal
            and "filter=latest" in signal
            and "dominant failing job" in signal
            and "run_attempt>1" in ref
            and "filter=all" in ref
            and "filter=latest" in ref
            and "dominant failing job" in ref
            and any("prior attempt" in h and "min" in h for h in headers)
            and bool(rows)
            and "prior attempt" in evidence_text
            and "latest attempt" in evidence_text
            and "ambiguous ties" in evidence_text)
    if pat == "OPT57":
        # Numeric OPT57 evidence is re-derived by the caller before this
        # prose/table corroboration runs.
        return (
            "near-default timeout burn" in signal
            and "successful p99" in signal
            and "failed/timed-out" in ref
            and "successful p99" in ref
            and any("default-timeout" in h and "burn" in h for h in headers)
            and bool(rows)
            and "360 minute default" in evidence_text
            and "timeout-minutes" in evidence_text
            and "p99" in evidence_text)
    return False


def _non_pr_event_corroborated(f: dict, data: dict) -> bool:
    cert = _as_dict(f.get("tier2_neutrality"))
    subset = {str(e) for e in _as_list(f.get("tier2_run_subset_events")) if str(e)}
    if not subset or subset & _VR_DEVELOPER_EVENTS:
        return False
    wf = str(f.get("workflow_file") or "")
    wf_events = set(str(e) for e in _as_list(_as_dict(data.get("events_by_wf")).get(wf)) if str(e))
    if not wf_events:
        wf_events = set(str(e) for e in _as_list(
            _as_dict(_as_dict(data.get("per_workflow_timing")).get(wf)).get("events")) if str(e))
    if not wf_events or not subset.issubset(wf_events):
        return False
    signal = str(f.get("measured_signal") or "").lower()
    ref = str(cert.get("ref") or "").lower()
    me = _as_dict(f.get("measured_evidence"))
    evidence_text = _text_from_json(me).lower()
    table = _as_dict(me.get("table"))
    rows = _as_list(table.get("rows"))
    # Currently only OPT36 emits a measured non_pr_event certificate. Pin the
    # schedule/same-head-sha evidence shape so a future detector cannot promote a
    # generic "non-PR" claim without its own corroboration.
    return (
        str(f.get("pattern")) == "OPT36"
        and subset == {"schedule"}
        and "event=schedule" in signal
        and "same-head_sha" in signal
        and "event=schedule" in ref
        and "schedule" in ref
        and "same-head_sha" in evidence_text
        and bool(rows))


def _tier2_id(f: dict, fallback: int) -> str:
    raw = str(f.get("id") or f"{f.get('pattern', 'tier2')}-{fallback}")
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw)


def _tier2_markers(report: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _TIER2_MARKER_RE.finditer(report)]


def _tier2_duplicate_marker_ids(markers: list[tuple[str, str]]) -> list[str]:
    counts: dict[str, int] = {}
    for fid, _pat in markers:
        counts[fid] = counts.get(fid, 0) + 1
    return sorted(fid for fid, count in counts.items() if count > 1)


def _tier2_marker_sections(report: str) -> list[tuple[str, str, str]]:
    matches = list(_TIER2_MARKER_RE.finditer(report))
    out: list[tuple[str, str, str]] = []
    for idx, match in enumerate(matches):
        if idx + 1 < len(matches):
            end = matches[idx + 1].start()
        else:
            # The last R-row's body runs to the next section heading. Each row's
            # OWN `## 🟢 Runner saving N:` header sits AFTER its marker (format
            # parity with the Long poles), so it must not terminate the body -
            # skip it and stop at the first `## ` that is not a Runner-saving
            # header (e.g. `## 🧹 Also noticed`).
            tail = re.search(r"\n## (?!🟢 Runner saving \d)", report[match.end():])
            end = match.end() + tail.start() if tail else len(report)
        out.append((match.group(1), match.group(2), report[match.start():end]))
    return out


def _tier2_body_for_marker(report: str, fid: str) -> str:
    bodies = [body for marker_id, _pat, body in _tier2_marker_sections(report)
              if marker_id == fid]
    return bodies[0] if len(bodies) == 1 else ""


def _fmt_runner_min(value: float | None) -> str:
    # PR-Z: mirrors the renderer's unified positive-label convention (the
    # signed "-N min/mo" appendix form is retired; PR-38's rule everywhere).
    if value is None or value == 0:
        return "-"
    return _fmt_tier2_saved_min(value)


def _fmt_tier2_saved_min(value: float | None) -> str:
    # Twin of scripts/blocking_path.py:_fmt_tier2_saved_min (identical nonzero
    # output). The zero/None "-" here is a skip-sentinel for the row-binding
    # check, NOT a mirror of the renderer's pre-strip "—" (_strip_emdashes
    # normalizes that to "-" anyway); zero rows cannot render because
    # admission requires a positive saving.
    if value is None or value == 0:
        return "-"
    v = abs(value)
    if v < 0.95:
        # Sub-minute savings keep one decimal (PR-Z): a real 0.2 min/mo row
        # must never render "0 min/mo" — display precision, not an admission
        # floor (D3 untouched). Detector savings round to 0.1, so the smallest
        # positive value is 0.1, never "0.0".
        return f"{v:.1f} min/mo"
    return f"{v:,.0f} min/mo"


def _flatten_cell(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


def _strict_job_p50(job: str, job_p50: dict) -> float | None:
    if job in job_p50:
        return _num(job_p50.get(job))
    prefix = job + " ("
    legs = [str(k) for k in job_p50 if str(k).startswith(prefix)]
    if not legs:
        return None
    best = min(legs, key=lambda n: (-(_num(job_p50.get(n)) or 0.0), n))
    return _num(job_p50.get(best))


def _below_floor_margin(f: dict, data: dict) -> float | None:
    wf = str(f.get("workflow_file") or "")
    crit = _as_dict(_as_dict(data.get("per_workflow_timing")).get(wf))
    floor = _num(crit.get("floor_p50"))
    job_p50 = _as_dict(crit.get("job_p50"))
    jobs = [str(j) for j in _as_list(f.get("affected_jobs")) if str(j)]
    if floor is None or floor <= 0 or not jobs:
        return None
    vals = [_strict_job_p50(j, job_p50) for j in jobs]
    vals = [v for v in vals if v is not None and v > 0]
    if not vals:
        return None
    own = max(vals)
    if own >= floor:
        return None
    return round(floor - own, 1)


def _rounding_waste_min(durations: list[float]) -> int:
    vals = [float(d) for d in durations if isinstance(d, (int, float)) and d > 0]
    if len(vals) < 2:
        return 0
    return max(0, int(sum(math.ceil(d / 60.0) for d in vals)
                      - math.ceil(sum(vals) / 60.0)))


_VR_OPT57_DEFAULT_TIMEOUT_MIN = 360.0
_VR_OPT57_NEAR_DEFAULT_TIMEOUT_S = 0.95 * _VR_OPT57_DEFAULT_TIMEOUT_MIN * 60.0
_VR_OPT57_MIN_TIMEOUT_S = 15.0 * 60.0
_VR_OPT57_TIMEOUT_BUFFER_S = 10.0 * 60.0
_VR_OPT57_TIMEOUT_MULTIPLIER = 1.5
# Keep in sync with collect_runs._MIN_TIMED_RUNS. The verifier is intentionally
# runnable as a standalone report checker, so it does not import detector code.
_VR_OPT57_MIN_SUCCESS_SAMPLES = 3


def _opt57_recommended_timeout_s(p99_s: float) -> float:
    target = max(
        _VR_OPT57_MIN_TIMEOUT_S,
        p99_s + _VR_OPT57_TIMEOUT_BUFFER_S,
        p99_s * _VR_OPT57_TIMEOUT_MULTIPLIER,
    )
    return float(math.ceil(target / 60.0) * 60)


def _opt57_timeout_rederived(f: dict) -> list[str]:
    burn = _as_dict(f.get("timeout_default_burn"))
    problems: list[str] = []
    if burn.get("kind") != "opt57_timeout_default_burn":
        return ["missing opt57_timeout_default_burn evidence"]
    samples = [_as_dict(s) for s in _as_list(burn.get("samples"))]
    job_key = str(burn.get("job_key") or "").strip()
    affected = [str(j).strip() for j in _as_list(f.get("affected_jobs")) if str(j).strip()]
    monthly = _num(burn.get("monthly_volume"))
    denom = _num(burn.get("sample_denominator"))
    p99_s = _num(burn.get("successful_duration_p99_s"))
    success_n = _num(burn.get("successful_duration_samples"))
    rec_min = _num(burn.get("recommended_timeout_minutes"))
    default_min = _num(burn.get("default_timeout_minutes"))
    if not samples:
        problems.append("no timeout samples")
    if not job_key:
        problems.append("timeout_default_burn.job_key is missing")
    elif affected != [job_key]:
        problems.append(f"affected_jobs {affected!r} != timeout_default_burn.job_key {job_key!r}")
    if monthly is None or monthly <= 0:
        problems.append(f"monthly_volume={burn.get('monthly_volume')!r}")
    if denom is None or denom <= 0:
        problems.append(f"sample_denominator={burn.get('sample_denominator')!r}")
    if default_min != _VR_OPT57_DEFAULT_TIMEOUT_MIN:
        problems.append(f"default_timeout_minutes {default_min!r} != 360")
    if success_n is None or success_n < _VR_OPT57_MIN_SUCCESS_SAMPLES:
        problems.append(f"successful_duration_samples={burn.get('successful_duration_samples')!r}")
    if p99_s is None or p99_s <= 0:
        problems.append(f"successful_duration_p99_s={burn.get('successful_duration_p99_s')!r}")
    if rec_min is None or rec_min <= 0:
        problems.append(f"recommended_timeout_minutes={burn.get('recommended_timeout_minutes')!r}")
    rec_s = (rec_min or 0.0) * 60.0
    if p99_s is not None and rec_min is not None and rec_min > 0:
        expected_rec_s = _opt57_recommended_timeout_s(p99_s)
        if abs(rec_s - expected_rec_s) > 0.01:
            problems.append(
                f"recommended_timeout_minutes {rec_min!r} != p99-backed "
                f"{expected_rec_s / 60.0:.0f}")
        if rec_s <= p99_s:
            problems.append("recommended timeout is not above successful p99")
        if rec_s >= _VR_OPT57_NEAR_DEFAULT_TIMEOUT_S:
            problems.append("recommended timeout approaches the default timeout")

    recomputed_waste_s = 0.0
    sample_run_ids: list[str] = []
    for i, sample in enumerate(samples, 1):
        conclusion = str(sample.get("conclusion") or "").lower()
        if conclusion not in {"failure", "timed_out"}:
            problems.append(f"sample {i}: conclusion {conclusion!r} is not failed/timed_out")
        dur = _num(sample.get("duration_s"))
        if dur is None or dur < _VR_OPT57_NEAR_DEFAULT_TIMEOUT_S:
            problems.append(f"sample {i}: duration {dur!r} below near-default threshold")
            continue
        waste = max(0.0, dur - rec_s)
        got_waste = _num(sample.get("waste_s"))
        if got_waste is None or abs(got_waste - waste) > 0.11:
            problems.append(f"sample {i}: waste_s {got_waste!r} != {round(waste, 3)}")
        recomputed_waste_s += waste
        rid = str(sample.get("run_id") or "").strip()
        if rid:
            sample_run_ids.append(rid)

    occurrence_n = _num(burn.get("sampled_timeout_occurrences"))
    if occurrence_n is None or int(occurrence_n) != len(samples):
        problems.append(f"sampled_timeout_occurrences {occurrence_n!r} != {len(samples)}")
    sampled_burn_min = round(recomputed_waste_s / 60.0, 1)
    claimed_burn_min = _num(burn.get("sampled_timeout_burn_min"))
    if claimed_burn_min is None or abs(claimed_burn_min - sampled_burn_min) > 0.11:
        problems.append(f"sampled_timeout_burn_min {claimed_burn_min!r} != {sampled_burn_min}")
    claimed_run_ids = sorted(str(r) for r in _as_list(burn.get("run_ids")) if str(r))
    if claimed_run_ids != sorted(set(sample_run_ids)):
        problems.append("timeout_default_burn.run_ids do not match sample run IDs")
    if monthly is not None and denom is not None and denom > 0:
        expected_scale = monthly / denom
        scale = _num(burn.get("scale"))
        if scale is None or abs(scale - expected_scale) > 0.00001:
            problems.append(f"scale {scale!r} != {expected_scale:.6f}")
        expected_rm = round(sampled_burn_min * expected_scale, 1)
        raw_top = (_num(f.get("runner_min_saving")) or 0.0) + (
            _num(f.get("runner_min_overlap_s")) or 0.0)
        burn_rm = _num(burn.get("runner_min_saving"))
        if abs(raw_top - expected_rm) > 0.11:
            problems.append(
                f"runner_min_saving+overlap {round(raw_top, 3)!r} != re-derived {expected_rm}")
        if burn_rm is None or abs(burn_rm - expected_rm) > 0.11:
            problems.append(
                f"timeout_default_burn.runner_min_saving {burn_rm!r} != re-derived {expected_rm}")
    return problems


def _opt65_rounding_rederived(f: dict, data: dict) -> tuple[float | None, list[str]]:
    rw = _as_dict(f.get("rounding_waste"))
    problems: list[str] = []
    if rw.get("kind") != "opt65_billing_rounding":
        return None, ["missing opt65_billing_rounding evidence"]
    samples = _as_list(rw.get("samples"))
    monthly = _num(rw.get("monthly_volume"))
    denom = _num(rw.get("sampled_successful_run_count"))
    sampled_waste_claim = _num(rw.get("sampled_waste_min"))
    if not samples:
        problems.append("no rounding samples")
    if monthly is None or monthly <= 0:
        problems.append(f"monthly_volume={rw.get('monthly_volume')!r}")
    if denom is None or denom <= 0:
        problems.append(f"sampled_successful_run_count={rw.get('sampled_successful_run_count')!r}")
    recomputed_waste = 0
    credited_jobs: set[str] = set()
    sample_jobs: list[list[str]] = []
    for i, sample in enumerate(samples, 1):
        s = _as_dict(sample)
        durations = [_num(d) for d in _as_list(s.get("durations_s"))]
        if any(d is None or d <= 0 for d in durations):
            problems.append(f"sample {i}: invalid durations")
            continue
        dur_vals = [float(d) for d in durations if d is not None]
        jobs = [str(j) for j in _as_list(s.get("jobs")) if str(j)]
        if len(jobs) != len(dur_vals) or len(jobs) < 3:
            problems.append(f"sample {i}: jobs/durations shape mismatch")
            continue
        waste = _rounding_waste_min(dur_vals)
        got_waste = _num(s.get("waste_min"))
        if got_waste is None or abs(got_waste - waste) > 0.01:
            problems.append(f"sample {i}: waste {got_waste!r} != {waste}")
        recomputed_waste += waste
        credited_jobs.update(jobs)
        sample_jobs.append(jobs)
    if sampled_waste_claim is None or abs(sampled_waste_claim - recomputed_waste) > 0.01:
        problems.append(f"sampled_waste_min {sampled_waste_claim!r} != {recomputed_waste}")
    if monthly is not None and denom is not None and denom > 0:
        expected_rm = round(recomputed_waste * monthly / denom, 1)
        rm = _num(f.get("runner_min_saving"))
        rw_rm = _num(rw.get("runner_min_saving"))
        if rm is None or abs(rm - expected_rm) > 0.11:
            problems.append(f"runner_min_saving {rm!r} != re-derived {expected_rm}")
        if rw_rm is None or abs(rw_rm - expected_rm) > 0.11:
            problems.append(f"rounding_waste.runner_min_saving {rw_rm!r} != re-derived {expected_rm}")
    if sorted(credited_jobs) != sorted(str(j) for j in _as_list(f.get("affected_jobs")) if str(j)):
        problems.append("affected_jobs do not match credited rounding jobs")
    if sorted(credited_jobs) != sorted(str(j) for j in _as_list(rw.get("credited_jobs")) if str(j)):
        problems.append("rounding_waste.credited_jobs do not match credited sample jobs")

    wf = str(f.get("workflow_file") or "")
    crit = _as_dict(_as_dict(data.get("per_workflow_timing")).get(wf))
    floor = _num(crit.get("floor_p50"))
    job_p50 = _as_dict(crit.get("job_p50"))
    if floor is None or floor <= 0:
        problems.append("missing floor_p50")
        return None, problems
    max_combined = 0.0
    for jobs in sample_jobs:
        vals = [_strict_job_p50(j, job_p50) for j in jobs]
        if any(v is None or v <= 0 for v in vals):
            problems.append("credited sample job lacks strict p50")
            continue
        max_combined = max(max_combined, sum(float(v) for v in vals if v is not None))
    if max_combined <= 0 or max_combined >= floor:
        problems.append(f"combined credited leg p50 {round(max_combined, 1)!r} is not below floor {floor!r}")
        return None, problems
    claimed_combined = _num(rw.get("max_combined_leg_p50_s"))
    if claimed_combined is None or abs(claimed_combined - round(max_combined, 1)) > 0.11:
        problems.append(f"max_combined_leg_p50_s {claimed_combined!r} != {round(max_combined, 1)}")
    return round(floor - max_combined, 1), problems


def _tier2_skip_or_data(findings_path: Path | None, name: str,
                        report: str | None = None) -> tuple[Check | None, dict]:
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True), {}
    markers = _tier2_markers(report or "")
    if not _has_tier2_stamp_surface(data):
        if markers:
            return Check(
                name, False,
                "rendered Tier-2 R-rows but findings JSON has no per-finding "
                "Tier-2 stamp surface; fail closed instead of compat SKIP"), data
        if _has_tier2_top_level_surface(data) and _has_positive_runner_min_candidate(data):
            return Check(
                name, False,
                "findings JSON has Tier-2 top-level stamps plus runner-minute findings "
                "but no per-finding Tier-2 stamp surface; fail closed instead of compat SKIP"), data
        return Check(name, True, "findings JSON has no Tier-2 stamps; compat SKIP", skipped=True), {}
    return None, data


def check_tier2_neutrality_derived(report: str, findings_path: Path | None,
                                   report_path: Path | None) -> Check:
    name = "Tier-2 R-rows carry re-derived wall-clock-neutral certificates"
    early, data = _tier2_skip_or_data(findings_path, name, report)
    if early:
        return early
    ranked = _tier2_ranked(data)
    visible = ranked[:_VR_TIER2_CAP]
    expected = {_tier2_id(f, i): f for i, f in enumerate(visible, 1)}
    markers = _tier2_markers(report)
    dupes = _tier2_duplicate_marker_ids(markers)
    if dupes:
        return Check(name, False, f"duplicate Tier-2 marker id(s): {dupes}")
    rendered = {fid: pat for fid, pat in markers}
    if not ranked and not rendered:
        return Check(name, True, "Tier-2 stamps present but no measured+certified finding to promote",
                     skipped=True)
    if set(rendered) != set(expected):
        return Check(name, False,
                     f"rendered R-row ids {sorted(rendered)} do not match visible eligible ids "
                     f"{sorted(expected)} (cap {_VR_TIER2_CAP}; total eligible {len(ranked)})")
    manifest = _load_claims(report_path)
    cert_claims = []
    if manifest and "runner_minutes" in _as_list(manifest.get("families_migrated")):
        cert_claims = [c for c in _as_list(manifest.get("claims"))
                       if isinstance(c, dict) and c.get("kind") == "tier2_neutrality_line"]
    claim_by_subject = {str(c.get("subject")): c for c in cert_claims}
    rendered_poles = {_cmp_name(check) for _wf, check, _body in _pole_header_sections(report)}
    bad: list[str] = []
    for idx, f in enumerate(ranked, 1):
        fid = _tier2_id(f, idx)
        visible_row = fid in expected
        cert = _as_dict(f.get("tier2_neutrality"))
        proof = str(cert.get("proof") or "")
        wc = f.get("wall_clock_p50_s")
        if wc not in (0, 0.0, None):
            bad.append(f"{fid}: wall_clock_p50_s={wc!r}")
        jobs = {_cmp_name(str(j)) for j in _as_list(f.get("affected_jobs")) if str(j)}
        if rendered_poles and jobs & rendered_poles:
            bad.append(f"{fid}: affected job is also rendered as a Long pole")
        if proof == "below_cluster_floor":
            got = _num(cert.get("margin_s"))
            if str(f.get("pattern") or "") == "OPT65":
                want, opt65_bad = _opt65_rounding_rederived(f, data)
                bad.extend(f"{fid}: {msg}" for msg in opt65_bad)
            else:
                want = _below_floor_margin(f, data)
            if got is None or want is None or abs(got - want) > 0.11:
                bad.append(f"{fid}: below-floor margin {got!r} != re-derived {want!r}")
        elif proof == "post_completion_waste":
            if str(f.get("pattern") or "") not in {"OPT35", "OPT46", "OPT57", "OPT64"}:
                bad.append(
                    f"{fid}: unsupported post_completion_waste pattern "
                    f"{str(f.get('pattern') or '')!r} - add detector-specific "
                    "corroboration in _post_completion_waste_corroborated")
            elif str(f.get("pattern") or "") == "OPT57":
                opt57_bad = _opt57_timeout_rederived(f)
                if opt57_bad:
                    bad.extend(f"{fid}: {msg}" for msg in opt57_bad)
                elif not _post_completion_waste_corroborated(f):
                    bad.append(f"{fid}: post_completion_waste lacks stamped post-completion evidence")
            elif not _post_completion_waste_corroborated(f):
                bad.append(f"{fid}: post_completion_waste lacks stamped post-completion evidence")
        elif proof == "non_pr_event":
            if not _non_pr_event_corroborated(f, data):
                bad.append(f"{fid}: non_pr_event lacks stamped event-subset evidence")
        else:
            bad.append(f"{fid}: unsupported proof {proof!r}")
        if visible_row:
            body = _tier2_body_for_marker(report, fid)
            # Rank<->header binding (Long-pole format parity): the row rendered
            # under this marker must carry its own `## 🟢 Runner saving {idx}:`
            # header, where idx is the re-derived rank - the header number, the
            # `r-{idx}` anchor, and the ranked order can never disagree. The dot
            # is constant green by design (OD12-rev1): admission requires the
            # neutrality certificate, so every rendered row is merge-safe.
            if f"## 🟢 Runner saving {idx}: " not in body:
                bad.append(f"{fid}: rendered row lacks its '## 🟢 Runner saving "
                           f"{idx}:' header at re-derived rank {idx}")
            if "machine-derived proof" not in body:
                bad.append(f"{fid}: rendered row lacks the neutrality proof line")
            claim = claim_by_subject.get(fid)
            if not claim:
                bad.append(f"{fid}: missing tier2_neutrality_line claim")
            elif str(claim.get("rendered") or "") not in body:
                bad.append(f"{fid}: neutrality claim not bound to its R-row")
    return Check(name, not bad, f"{len(ranked)} Tier-2 certificate(s) re-derived "
                 f"({len(expected)} visible R-row(s))" if not bad
                 else "; ".join(bad[:6]))


def check_tier2_measured_basis(report: str, findings_path: Path | None) -> Check:
    name = "Tier-2 R-rows use measured sizing basis"
    early, data = _tier2_skip_or_data(findings_path, name, report)
    if early:
        return early
    ranked = _tier2_ranked(data)
    by_id = {_tier2_id(f, i): f for i, f in enumerate(ranked[:_VR_TIER2_CAP], 1)}
    markers = _tier2_markers(report)
    if not markers:
        return Check(name, True, "no rendered Tier-2 R-rows", skipped=True)
    bad = []
    for idx, f in enumerate(ranked, 1):
        fid = _tier2_id(f, idx)
        if f.get("sizing_basis") != "measured":
            bad.append(f"{fid}: sizing_basis={f.get('sizing_basis')!r}")
        if not str(f.get("measured_signal") or "").strip():
            bad.append(f"{fid}: missing measured_signal")
    for fid, _pat in markers:
        f = by_id.get(fid)
        if not f:
            bad.append(f"{fid}: no eligible finding")
            continue
    return Check(name, not bad, f"{len(ranked)} Tier-2 finding(s) measured with signals "
                 f"({len(markers)} visible R-row(s))" if not bad
                 else "; ".join(bad[:6]))


# Verbatim copy of blocking_path._PER_POLE_STRUCTURAL_PATTERNS (verify_report is
# standalone by design — no skill imports); kept honest by the Stream-1 coupling
# test in test_verify_report_self.py, exactly like the other engine constants.
_VR_PER_POLE_STRUCTURAL = frozenset({"OPT70", "OPT71", "OPT72", "OPT74", "OPT75"})


def _vr_dedupe_findings(data: dict) -> dict:
    """Return `data` with its `findings` list de-overlapped the same way the renderer's
    `blocking_path._dedupe_findings` does — collapse EXACT-duplicate occurrences (same
    source/pattern/workflow_file/line/evidence), preserving order. verify_report stays
    import-free from the engine, so this is the transformation copied verbatim (a DATA
    step, read the same way findings.json is), not an import.

    Load-bearing for the Tier-2 section-lead re-derivation (issue #4): the renderer
    builds BOTH the Tier-2 section and the Also-noticed appendix from
    `_dedupe_findings(doc["findings"])`, so the section lead's "not promoted: N …" tail
    counts the DEDUPED collection its rows render from. Re-deriving that tail from the
    RAW `findings` list double-counts an occurrence the renderer collapsed (qdrant OPT17
    line 186 → 17 vs 18; grafana OPT35 line 0 → 11 vs 12) — a false FAIL against a
    correct report. Mirroring the dedupe here makes the re-derivation read the same
    single source the rows do, instead of a parallel raw-list count. Non-dict entries
    pass through untouched (the accounting loop already skips them)."""
    seen: set[tuple] = set()
    out: list = []
    for f in _as_list(data.get("findings")):
        if not isinstance(f, dict):
            out.append(f)
            continue
        key = (f.get("source", "ci-speedup"), f.get("pattern", ""),
               f.get("workflow_file", ""), f.get("line", 0),
               (f.get("evidence") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return {**data, "findings": out}


def check_tier2_total_deoverlapped(report: str, findings_path: Path | None,
                                   report_path: Path | None) -> Check:
    name = "Tier-2 section-lead totals match de-overlapped findings"
    early, data = _tier2_skip_or_data(findings_path, name, report)
    if early:
        return early
    # De-overlap first, exactly like the renderer (`_dedupe_findings`): the promoted set
    # AND the not-promoted accounting tail both derive from the deduped population, so an
    # exact-duplicate the renderer collapsed can't inflate the re-derived count (issue #4).
    data = _vr_dedupe_findings(data)
    ranked = _tier2_ranked(data)
    if not ranked:
        return Check(name, True, "no eligible Tier-2 findings", skipped=True)
    manifest = _load_claims(report_path)
    if not manifest or "runner_minutes" not in _as_list(manifest.get("families_migrated")):
        return Check(name, False, "claims manifest lacks runner_minutes family")
    leads = [c for c in _as_list(manifest.get("claims"))
             if isinstance(c, dict) and c.get("kind") == "tier2_section_lead"]
    if len(leads) != 1:
        return Check(name, False, f"expected exactly one tier2_section_lead claim, got {len(leads)}")
    fields = _as_dict(leads[0].get("fields"))
    raw = round(sum(_num(f.get("runner_min_saving")) or 0.0 for f in ranked), 3)
    naive = round(raw + sum(_num(f.get("runner_min_overlap_s")) or 0.0
                            for f in ranked), 3)
    count = len(ranked)
    problems = []
    if abs((_num(fields.get("raw_min")) or 0.0) - raw) > 0.01:
        problems.append(f"raw_min {fields.get('raw_min')!r} != {raw}")
    if abs((_num(fields.get("naive_min")) or 0.0) - naive) > 0.01:
        problems.append(f"naive_min {fields.get('naive_min')!r} != {naive}")
    if int(fields.get("count") or -1) != count:
        problems.append(f"count {fields.get('count')!r} != {count}")
    rendered = str(leads[0].get("rendered") or "")
    if report.count(rendered) != 1:
        problems.append("section-lead claim is not bound exactly once to the report")
    section = _section(report, "Runner-minute reductions")
    raw_s = _fmt_tier2_saved_min(raw)
    if not section:
        problems.append("Tier-2 section missing from report")
    elif f"{raw_s} credited after de-overlap" not in section:
        problems.append(f"rendered section lead missing raw credited total {raw_s!r}")
    # Dollars are excised (2026-07-20): the section lead is minutes-only and must
    # carry no rate-derived price surface.
    if section and "at published rates (as of" in section:
        problems.append("section lead renders a retired dollar total")
    if section and f"{count} neutral finding" not in section:
        problems.append(f"rendered section lead missing neutral finding count {count}")
    # The first-class Contents entry (owner-requested TOC fix): when it
    # renders, its leading total and its enumerated row count must equal the
    # re-derived values — the TOC and the section can never tell two stories.
    toc_m = re.search(r"\*\*💸 Runner-minute reductions\*\* - ~(.+?) of measured", report)
    if toc_m:
        if toc_m.group(1) != raw_s:
            problems.append(f"Contents total {toc_m.group(1)!r} != re-derived {raw_s!r}")
        # Row links cap at the section's display cap (12 = _TIER2_CAP, pinned
        # here — the section emits anchors only for shown rows); past it the
        # overflow tail must disclose the rest.
        toc_rows = re.findall(
            r"^(\d+)\. 🟢 \[.+?\]\(#r-\1\) - ([\d.,]+ min/mo)(?:\s*\(([^)]*)\))?",
            report, re.MULTILINE)
        expected_rows = min(count, 12)
        if len(toc_rows) != expected_rows:
            problems.append(f"Contents enumerates {len(toc_rows)} R-row link(s), "
                            f"expected {expected_rows}")
        if count > 12 and "+%d more" % (count - 12) not in report.replace("… ", ""):
            problems.append(f"Contents lacks the '+{count - 12} more' overflow tail")
        # Per-row minutes re-derive. Dollars are excised (2026-07-20): a TOC row
        # carries a minutes tail only, and never a rate-derived price paren.
        for pos, (num_s, min_s, paren) in enumerate(toc_rows):
            if pos >= len(ranked):
                break
            f = ranked[pos]
            want_min = _fmt_tier2_saved_min(_num(f.get("runner_min_saving")) or 0.0)
            if min_s != want_min:
                problems.append(f"Contents row R{num_s} shows {min_s!r}, "
                                f"re-derived {want_min!r}")
            if paren and "$" in paren:
                problems.append(f"Contents row R{num_s} renders a retired price paren: ({paren})")
    elif "Runner-minute reductions (wall-clock-neutral)" in report \
            and "## 📋 Contents" in report:
        problems.append("Tier-2 section renders but the Contents lacks the "
                        "first-class 💸 entry")

    # PR-P1: the lead's "not promoted:" tail must account for 100% of positive-saving
    # findings by basis and reason (§5 exit criterion item 1; D3-rev1's condition).
    # Re-derived per finding — decision table:
    #   promoted (in `ranked`)                    -> the K count above
    #   measured + tier2_neutrality, unpromoted   -> no_source     (source gate demotion)
    #   measured, no certificate                  -> cert_deferred
    #   modeled                                   -> modeled
    #   OPT73 (the one structural credited saver) -> structural
    #   anything else                             -> other: FAIL — an unaccountable
    #                                                finding is a silent drop
    promoted_ids = {id(f) for f in ranked}
    acct = {"no_source": 0, "cert_deferred": 0, "modeled": 0, "structural": 0, "other": 0}
    unaccountable = []
    for f in _as_list(data.get("findings")):
        if not isinstance(f, dict):
            continue
        pat = str(f.get("pattern") or "")
        if (f.get("advisory") or str(f.get("tier2_superseded_by") or "").strip()
                or pat == "OPT43" or pat in _VR_PER_POLE_STRUCTURAL
                or (_num(f.get("runner_min_saving")) or 0.0) <= 0
                or id(f) in promoted_ids):
            continue
        basis = f.get("sizing_basis")
        cert = f.get("tier2_neutrality")
        # Non-empty dict, mirroring the renderer's `_is_tier2_finding`: an EMPTY
        # `tier2_neutrality: {}` is no certificate, so it is certificate-deferred
        # in both derivations (greptile P1 on this PR caught the divergence).
        if basis == "measured" and isinstance(cert, dict) and cert:
            acct["no_source"] += 1
        elif basis == "measured":
            acct["cert_deferred"] += 1
        elif basis == "modeled":
            acct["modeled"] += 1
        elif pat == "OPT73":
            acct["structural"] += 1
        else:
            acct["other"] += 1
            unaccountable.append(str(f.get("id") or pat))
    field_map = {"no_source": "not_promoted_measured_no_source",
                 "cert_deferred": "not_promoted_measured_cert_deferred",
                 "modeled": "not_promoted_modeled",
                 "structural": "not_promoted_structural",
                 "other": "not_promoted_other"}
    for key, fname in field_map.items():
        got = fields.get(fname)
        if int(got if got is not None else -1) != acct[key]:
            problems.append(f"{fname} {got!r} != re-derived {acct[key]}")
    if unaccountable:
        problems.append(
            f"unaccountable positive-saving finding(s) {unaccountable}: no bucket in "
            "the accounting decision table covers them — extend the table, never drop")
    # The rendered tail must be the EXACT string these counts produce. Substring
    # matching per segment lets a digit-prefix tamper slip ("1 modeled" matches
    # inside "11 modeled" — adversarial review F5); rebuild the whole tail
    # (mirror of the renderer's `_tier2_unpromoted_tail`) and require it verbatim.
    segs = []
    measured_total = acct["no_source"] + acct["cert_deferred"]
    if measured_total:
        reasons = []
        if acct["no_source"]:
            reasons.append(f"{acct['no_source']} without source rows")
        if acct["cert_deferred"]:
            reasons.append(f"{acct['cert_deferred']} certificate-deferred")
        segs.append(f"{measured_total} measured item(s) ({', '.join(reasons)})")
    if acct["modeled"]:
        segs.append(f"{acct['modeled']} modeled item(s)")
    if acct["structural"]:
        segs.append(f"{acct['structural']} structural shared-step item(s)")
    if acct["other"]:
        segs.append(f"{acct['other']} other item(s)")
    if section and segs:
        tail = "; not promoted: " + " · ".join(segs) + "; see Also noticed"
        if tail not in section:
            problems.append(f"lead tail missing/differs: expected {tail!r}")
        if "unmeasured item(s) remain" in section:
            problems.append("lead still renders the retired 'unmeasured item(s)' counter")
    return Check(name, not problems, f"{count} finding(s), {raw:.3f} min/mo"
                 if not problems else "; ".join(problems))


def check_no_timing_endpoint_citation(report: str, report_path: Path | None) -> Check:
    """No claim or prose may cite GitHub's closing-down `/timing` endpoints (§4.5).

    `GET .../runs/{id}/timing` and `.../workflows/{id}/timing` are officially "in the
    process of closing down" and never covered what the report derives (public-repo
    hosted jobs, post-rounding). Every rendered number is derived from jobs-API
    timestamps; a `/timing` citation would assert a data source the pipeline does not
    (and soon cannot) use. Scans the report AND its claims sidecar (a claim field
    citing it is as wrong as prose). Content-only, so it applies to every artifact —
    pre-stamp reports included (L-compat: a text scan cannot false-fail them)."""
    name = "no claim or prose cites the closing-down /timing endpoints"
    # Endpoint-SHAPED citations only ("runs/{id}/timing", "workflows/{id}/timing").
    # A bare "/timing\b" ban false-fails legitimate user content: a monorepo job
    # named `test (packages/timing)` renders that substring in every report
    # (adversarial review F1) — the guard polices the citation, not the word.
    _TIMING_RE = re.compile(r"\b(?:runs|workflows)/[^\s/`]+/timing\b")
    hits = []
    for line in report.splitlines():
        if _TIMING_RE.search(line):
            hits.append(line.strip()[:100])
    manifest_path = report_path.parent / (report_path.name + ".claims.json") \
        if report_path else None
    if manifest_path and manifest_path.exists():
        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        if _TIMING_RE.search(raw):
            hits.append(f"claims sidecar {manifest_path.name} cites /timing")
    if hits:
        return Check(name, False,
                     "closing-down /timing endpoint cited: " + " | ".join(hits[:3]))
    return Check(name, True, "no /timing citation in report or claims sidecar")


def check_tier2_claims_derivation_basis(report: str, findings_path: Path | None,
                                        report_path: Path | None) -> Check:
    """Every Tier-2 claim records its derivation basis as a FIELD (§5.3's last
    bullet, G8): `jobs_api_timestamps` — machine-readable, not just the rendered
    "derived from jobs-API timestamps" prose. Compat: skips on artifacts with no
    manifest or no Tier-2 claims (pre-stamp worked examples)."""
    name = "Tier-2 claims carry the jobs-API derivation-basis field"
    manifest = _load_claims(report_path)
    if not manifest:
        return Check(name, True, "no claims manifest (pre-manifest artifact)", skipped=True)
    tier2 = [c for c in _as_list(manifest.get("claims"))
             if isinstance(c, dict) and str(c.get("kind") or "").startswith("tier2_")]
    if not tier2:
        return Check(name, True, "no Tier-2 claims in manifest", skipped=True)
    bad = [f"{c.get('kind')}/{c.get('subject')}" for c in tier2
           if _as_dict(c.get("fields")).get("derivation_basis") != "jobs_api_timestamps"]
    if bad:
        return Check(name, False,
                     f"{len(bad)} Tier-2 claim(s) lack derivation_basis="
                     f"'jobs_api_timestamps': {', '.join(bad[:4])}")
    return Check(name, True, f"{len(tier2)} Tier-2 claim(s) carry the derivation basis")


def check_tier2_headline_matches_stamp(report: str, findings_path: Path | None,
                                       report_path: Path | None) -> Check:
    name = "Tier-2 headline claim names the top stamped finding"
    early, data = _tier2_skip_or_data(findings_path, name, report)
    if early:
        return early
    ranked = _tier2_ranked(data)
    manifest = _load_claims(report_path)
    if not manifest or "runner_minutes" not in _as_list(manifest.get("families_migrated")):
        return Check(name, False, "claims manifest lacks runner_minutes family")
    heads = [c for c in _as_list(manifest.get("claims"))
             if isinstance(c, dict) and c.get("kind") == "tier2_headline"]
    if not ranked:
        fallback = [c for c in heads if c.get("subject") == "unpromoted-modeled"]
        if _tier2_has_modeled_value(data):
            if len(fallback) != 1:
                return Check(name, False,
                             f"expected one modeled fallback tier2_headline claim, got "
                             f"{[c.get('subject') for c in heads]}")
            rendered = str(fallback[0].get("rendered") or "")
            if report.count(rendered) != 1:
                return Check(name, False,
                             "modeled fallback Tier-2 headline claim is not bound exactly once")
            return Check(name, True, "no promoted findings; modeled fallback claim is bound")
        if heads:
            return Check(name, False,
                         f"unexpected Tier-2 headline claim(s) with no promoted or modeled value: "
                         f"{[c.get('subject') for c in heads]}")
        return Check(name, True, "no promoted findings and no modeled fallback claim", skipped=True)
    expected = _tier2_id(ranked[0], 1)
    matching = [c for c in heads if c.get("subject") == expected]
    if len(matching) != 1:
        return Check(name, False,
                     f"expected one tier2_headline claim for top finding {expected}, got "
                     f"{[c.get('subject') for c in heads]}")
    rendered = str(matching[0].get("rendered") or "")
    if report.count(rendered) != 1:
        return Check(name, False, "Tier-2 headline claim is not bound exactly once to the report")
    return Check(name, True, f"Tier-2 headline subject {expected} matches top ranked finding")


def _tier2_source_job_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _tier2_matrix_base_raw(value: object) -> str | None:
    name = _tier2_source_job_name(value)
    m = re.match(r"^(.*\S)\s*\((?:[^()]|\([^()]*\))*\)\s*$", name)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _tier2_source_rows_matching_jobs(rows: list[dict], jobs: list[str]) -> list[dict]:
    matched: list[dict] = []
    seen: set[int] = set()
    for job in jobs:
        affected = _tier2_source_job_name(job)
        if not affected:
            continue
        exact = [
            row for row in rows
            if _tier2_source_job_name(row.get("job_name")) == affected
        ]
        candidates = exact or [
            row for row in rows
            if _tier2_matrix_base_raw(_tier2_source_job_name(row.get("job_name"))) == affected
        ]
        for row in candidates:
            ident = id(row)
            if ident not in seen:
                seen.add(ident)
                matched.append(row)
    return matched


def _tier2_source_job_candidates(f: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        name = _tier2_source_job_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    for job in _as_list(f.get("affected_jobs")):
        add(job)
    add(f.get("rerun_dominant_job"))
    burn = _as_dict(f.get("timeout_default_burn"))
    add(burn.get("job_template"))
    add(burn.get("job_key"))
    for sample in _as_list(burn.get("samples")):
        add(_as_dict(sample).get("job_name"))
    return out


def _tier2_required_source_filters(f: dict) -> dict[str, str] | None:
    filters: dict[str, str] = {
        "status_filter": "success",
        "attempt_filter": "latest",
        "volume_filter": "all-status",
    }
    pat = str(f.get("pattern") or "")
    if pat == "OPT64" or f.get("rerun_dominant_job"):
        filters.update({
            "status_filter": "all-status",
            "attempt_filter": "prior",
            "volume_filter": "all-status",
        })
    explicit = _as_dict(f.get("runner_minute_source_filter"))
    for key in ("event_scope", "status_filter", "attempt_filter", "volume_filter"):
        value = str(explicit.get(key) or f.get(key) or "").strip()
        if value:
            if key in filters and filters[key] != value:
                return None
            filters[key] = value
    return filters


def _tier2_row_matches_required_filters(row: dict, filters: dict[str, str]) -> bool:
    return all(str(row.get(key) or "").strip() == expected
               for key, expected in filters.items())


def _tier2_spine_source_rows(f: dict, data: dict) -> list[dict]:
    spine = _as_dict(data.get("runner_minute_spine"))
    if spine.get("render_ready") is not True:
        return []
    rows_src = _as_list(spine.get("rows"))
    if not rows_src or not isinstance(spine.get("totals"), dict):
        return []
    wf = str(f.get("workflow_file") or "").strip()
    if not wf:
        return []
    wide_opt64 = str(f.get("pattern") or "") == "OPT64"
    rows = [
        _as_dict(row)
        for row in rows_src
        if str(_as_dict(row).get("workflow_file") or "").strip() == wf
    ]
    filters = _tier2_required_source_filters(f)
    if filters is None:
        return []
    if filters:
        rows = [row for row in rows if _tier2_row_matches_required_filters(row, filters)]
    jobs = _tier2_source_job_candidates(f)
    if wide_opt64:
        # R1 (WIDE): an OPT64 finding credits the whole retried run's
        # prior-attempt compute across every runner, so it binds to every
        # prior-attempt row of its workflow — not narrowed by job name.
        # The DOMINANT FAILING JOB itself (`rerun_dominant_job`,
        # never a looser candidate like a stale affected_jobs entry) must be
        # visible among those rows — its retries are what caused the
        # credited runs — else no binding at all. The sibling
        # no-double-count guard is _tier2_opt64_group_cover_ok.
        dominant = _tier2_source_job_name(f.get("rerun_dominant_job"))
        if not dominant or not _tier2_source_rows_matching_jobs(rows, [dominant]):
            return []
        return sorted(rows, key=_runner_spine_visible_sort_key)
    if jobs:
        rows = _tier2_source_rows_matching_jobs(rows, jobs)
    return sorted(rows, key=_runner_spine_visible_sort_key)


def _tier2_opt64_group_cover_ok(f: dict, data: dict) -> bool:
    """R1's no-double-count guard: sibling OPT64 findings on one workflow all
    bind to the SAME wide prior-attempt row set, so covering each sibling
    individually is not enough — their combined claim must fit the shared
    cover once. If it does not, NONE of the siblings is source-backed
    (order-independent fail-close: no sibling is cherry-picked)."""
    if str(f.get("pattern") or "") != "OPT64":
        return True
    rows = _tier2_spine_source_rows(f, data)
    if not rows:
        return False
    wf = str(f.get("workflow_file") or "").strip()
    sibs = [g for g in _as_list(data.get("findings"))
            if isinstance(g, dict)
            and str(g.get("pattern") or "") == "OPT64"
            and str(g.get("workflow_file") or "").strip() == wf
            and not g.get("advisory")
            and g.get("sizing_basis") == "measured"
            and isinstance(g.get("tier2_neutrality"), dict)
            and bool(g.get("tier2_neutrality"))
            and (_num(g.get("runner_min_saving")) or 0.0) > 0]
    claimed = sum(_num(g.get("runner_min_saving")) or 0.0 for g in sibs)
    # Each sibling's saving is rounded to 0.1 by the detector (≤0.05 error).
    tol = 0.011 + 0.05 * len(sibs)
    raw_total = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0
                    for row in rows)
    if claimed > raw_total + tol:
        return False
    return True


def _tier2_source_rows_cover_saving(f: dict, rows: list[dict]) -> bool:
    saving = _num(f.get("runner_min_saving")) or 0.0
    if saving <= 0 or not rows:
        return False
    if str(f.get("pattern") or "") == "OPT65":
        source_billable = sum(
            _num(row.get("billable_equiv_min_per_month")) or 0.0 for row in rows)
        if saving > source_billable + 0.011:
            return False
    else:
        source_raw = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0 for row in rows)
        if saving > source_raw + 0.011:
            return False
    return True


def _tier2_expected_source_line(f: dict, data: dict) -> str | None:
    rows = _tier2_spine_source_rows(f, data)
    if not rows:
        return None
    raw = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0 for row in rows)
    billable = sum(_num(row.get("billable_equiv_min_per_month")) or 0.0 for row in rows)
    plural = "s" if len(rows) != 1 else ""
    wf = str(f.get("workflow_file") or "").strip() or "unknown workflow"
    if str(f.get("pattern") or "") == "OPT64":
        # R1 (WIDE): the binding spans every prior-attempt row of the workflow
        # across runners, so the line names the attempt population.
        head = (f"matched {len(rows)} prior-attempt row{plural} for "
                f"`{_flatten_cell(wf)}`")
    else:
        head = (f"matched {len(rows)} row{plural} for `{_flatten_cell(wf)}`")
    return (f"**Source block:** `runner_minute_spine` {head}; "
            "current measured cost spine for those rows is "
            f"{raw:.3f} raw min/mo, {billable:.3f} billable min/mo.")


def _tier2_rendered_source_lines(body: str) -> list[str]:
    # The R-row renders the source line as a labeled bullet (`- **Source
    # block:** ...`, Long-pole format parity); compare it label-onward so the
    # expected-line derivation stays independent of the list markup.
    out: list[str] = []
    for line in str(body).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if stripped.startswith("**Source block:**"):
            out.append(stripped)
    return out


def check_tier2_savings_rows_backed_by_cost_spine(report: str,
                                                  findings_path: Path | None) -> Check:
    name = "Tier-2 savings rows are backed by runner-minute cost spine"
    early, data = _tier2_skip_or_data(findings_path, name, report)
    if early:
        return early
    ranked = _tier2_ranked(data)
    markers = _tier2_markers(report)
    dupes = _tier2_duplicate_marker_ids(markers)
    if dupes:
        return Check(name, False, f"duplicate Tier-2 marker id(s): {dupes}")
    if markers and not ranked:
        return Check(name, False, "rendered Tier-2 R-rows but no source-backed eligible findings")
    if not markers:
        return Check(name, True, "no rendered Tier-2 R-rows", skipped=True)
    visible = {_tier2_id(f, idx): f for idx, f in enumerate(ranked[:_VR_TIER2_CAP], 1)}
    spine = _as_dict(data.get("runner_minute_spine"))
    bad: list[str] = []
    if spine.get("render_ready") is not True:
        bad.append("rendered Tier-2 savings rows require render-ready runner_minute_spine")
    section_by_id = {fid: body for fid, _pat, body in _tier2_marker_sections(report)}
    for fid, _pat in markers:
        f = visible.get(fid)
        if not f:
            bad.append(f"{fid}: no eligible visible finding")
            continue
        expected = _tier2_expected_source_line(f, data)
        body = section_by_id.get(fid, "")
        source_lines = _tier2_rendered_source_lines(body)
        rows = _tier2_spine_source_rows(f, data)
        if expected is None:
            bad.append(
                f"{fid}: no matching render-ready runner_minute_spine rows for "
                f"{str(f.get('workflow_file') or '')!r}")
        elif len(source_lines) != 1:
            bad.append(f"{fid}: expected exactly one Source block line, got {len(source_lines)}")
        elif source_lines[0] != expected:
            bad.append(f"{fid}: rendered source-block line does not match cost spine")
        saving = _num(f.get("runner_min_saving")) or 0.0
        if str(f.get("pattern") or "") == "OPT65":
            source_billable = sum(_num(row.get("billable_equiv_min_per_month")) or 0.0
                                  for row in rows)
            if rows and saving > source_billable + 0.011:
                bad.append(f"{fid}: runner_min_saving {saving:.3f} exceeds matched "
                           f"source billable minutes {source_billable:.3f}")
        else:
            source_raw = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0
                             for row in rows)
            if rows and saving > source_raw + 0.011:
                bad.append(f"{fid}: runner_min_saving {saving:.3f} exceeds matched "
                           f"source raw minutes {source_raw:.3f}")
        saving_label = _fmt_tier2_saved_min(saving)
        if saving_label != "-" and saving_label not in body:
            bad.append(f"{fid}: rendered R-row body missing savings label {saving_label!r}")
    return Check(name, not bad, f"{len(markers)} visible R-row source binding(s) re-derived"
                 if not bad else "; ".join(bad[:6]))


# --- #4: addressable ceiling vs the co-occurrence floor (Phase 0, Class A) -----------------
# The report's headline win ("the biggest single measured win is **~Xm** off the slowest fixable
# check, `pole`") and each pole's "**What a change here can buy (wall-clock):** up to **~Xm**"
# note are both `pole_p50 − binding_floor`, where the binding floor is the slowest concurrent
# check a TYPICAL gating PR also waits on. The engine USED to pick that floor by GLOBAL presence
# (a check counts only if present on ≥ 0.8 × the most-present check ANYWHERE), which demotes a
# genuinely co-occurring 2nd-slowest sibling that merely runs on slightly fewer PRs than some
# trivial universal check — so a FASTER check becomes the named floor and the ceiling overstates
# (Infisical: `Run integration test` rendered "~28m" when `Run BDD tests` — on 13/13 of the pole's
# OWN gating PRs but demoted under the global cutoff — caps the real win at ~14.8m).
# This invariant re-derives the floor the RIGHT way — relative to the POLE's own gating PRs (the
# PRs where it is the slowest check) — straight from `populations`, and asserts the rendered
# ceiling does not exceed what that floor physically allows. It is an UPPER BOUND, not an
# exact-match: the renderer's exact aggregation (bimodal slow-mode, present-weighting) is
# deliberately NOT reproduced (that would be circular) — asserting `rendered ≤ pole_p50 − floor`
# catches the overstatement DIRECTION while tolerating benign aggregation differences. Floor-check
# SELECTION (which sibling is the floor) is the dimension the renderer got wrong, so we re-derive it
# independently here from `populations` rather than trusting the report; the floor's VALUE is the
# stamped `checks[].p50_s` (a field read, never a re-rank), so pole and floor are compared on the
# SAME metric (mixing a global-p50 pole with a gating-median floor manufactured a false negative on
# stripe-go during corpus validation — fixed by this). One tolerated difference is explicitly the
# BIMODAL dimension: the engine floors a bimodal-flagged sibling at its slow mode (`_eff_floor_s` =
# max(p50, bimodal_high)), while we use the bare `p50_s` — a LOWER floor, hence a LOOSER (larger)
# allowed ceiling, so a bimodal-floor regression where the engine drops to p50 is intentionally NOT
# caught by this check (false-positive-free is the priority; the engine's own tests pin bimodal).
_CEILING_FLOOR_NOTE_RE = re.compile(
    r"What a change here can buy \(wall-clock\):\*\*\s*up to\s*\*\*~([0-9hms :]+?)\*\*\s*-\s*"
    r"(?:it gates until it drops to the next concurrent check"
    r"|a concurrent check with no workflow file)")
_CEILING_HEADLINE_RE = re.compile(
    r"biggest single measured win is \*\*~([0-9hms :]+?)\*\* off the slowest fixable check, "
    r"`([^`]+)`")
# The per-pole MATRIX two-number floor note ("speeding **this one leg** … drops the whole matrix
# toward … for up to **~Y**") floors against NON-leg checks, so its ceiling can't be bounded by the
# simple-form re-derivation here (that would false-positive on the legitimate shared-config number).
# It is NOT silently dropped: matched notes are COUNTED and surfaced in the result detail as a known
# coverage limit (the matrix HEADLINE pole is still bounded via `_CEILING_HEADLINE_RE` — fivetran).
# Bounding the matrix per-leg form (re-deriving the non-leg floor from `populations`) is follow-up.
_CEILING_MATRIX_MARKER = "this job's matrix legs run in parallel"
# A pole gating fewer than this many sampled PRs has too little co-occurrence signal to re-derive
# a stable floor from; below it the check SKIPS that claim rather than risk a thin-sample false
# positive. ANALOGOUS to the engine's `_RARE_PRESENCE_MIN_PR` (6) stability floor but NOT the same
# value and NOT the same quantity — that guards the total sampled-PR population (`npop`), this
# guards THIS pole's own gating-PR count — so the two are intentionally distinct; don't "sync" them.
_CEILING_MIN_GATING_PR = 5
# Tolerance on the upper bound (relative + absolute) — absorbs benign aggregation differences
# while still catching a ~2× overstatement. Validated against all corpus reports: the three true
# offenders (Infisical, fivetran, lightdash) clear it; nothing legitimate trips it.
_CEILING_REL_TOL = 0.15
_CEILING_ABS_TOL_S = 30.0


def _parse_clock_to_s(text: str) -> float | None:
    """Seconds from a rendered `_clock` string ("6m 53s", "45s", "28m 02s"). None when no h/m/s
    token is present, so a malformed capture SKIPS rather than reading as 0 (which would false-pass)."""
    parts = re.findall(r"(\d+)\s*([hms])", text)
    if not parts:
        return None
    return float(sum(int(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts))


def _fmt_clock(seconds: float) -> str:
    """`blocking_path._clock` mirrored locally (verify_report is standalone — no skill import) for
    readable detail strings only; not load-bearing."""
    s = int(round(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def _populations_per_pr(cp: dict) -> list[list[tuple[str, float]]]:
    """Each sampled PR's positive-duration checks, slowest-first — the per-PR ground truth the
    floor is re-derived from (re-derive, never proxy the renderer). [] when no populations."""
    out: list[list[tuple[str, float]]] = []
    for entry in _as_list(cp.get("populations")):
        try:
            _share, cks = entry
            pos = [(str(nm), float(p)) for nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            continue
        if pos:
            pos.sort(key=lambda kv: -kv[1])
            out.append(pos)
    return out


def _cooccurrence_floor_s(per_pr: list[list[tuple[str, float]]], pole_name: str,
                          p50_by: dict[str, float]) -> tuple[float | None, str | None, int]:
    """The binding wall-clock floor for `pole_name`, re-derived RELATIVE to the pole's own gating
    PRs (where it is the slowest check) — the fix's definition, not global presence. Returns
    (floor_seconds, floor_check_name, n_gating). The floor is the MAX stamped `checks[].p50_s`
    among non-pole checks that co-occur with the pole on a strict MAJORITY of its gating PRs.
    (None, None, n) when the pole gates too few PRs for a stable floor; (0.0, None, n) when nothing
    qualifies (no near-universal concurrent floor → the whole pole is addressable)."""
    pole_cmp = _cmp_name(pole_name)
    gating = [pr for pr in per_pr if _cmp_name(pr[0][0]) == pole_cmp]
    if len(gating) < _CEILING_MIN_GATING_PR:
        return None, None, len(gating)
    co: dict[str, int] = {}
    for pr in gating:
        for nm in {_cmp_name(n) for n, _p in pr[1:]}:   # de-dupe within a PR
            co[nm] = co.get(nm, 0) + 1
    floor_s, floor_name = 0.0, None
    majority = len(gating) * 0.5
    for nm, count in co.items():
        if count <= majority:               # strict majority of the pole's OWN gating PRs
            continue
        v = p50_by.get(nm) or 0.0
        if v > floor_s:
            floor_s, floor_name = v, nm
    return floor_s, floor_name, len(gating)


# --- #6: a drilled pole's binding floor (a heavy concurrent check) must be DISCLOSED on the spine
# The report's banner promises "slow checks that run on a minority of PRs are shown as a footnote."
# A heavy MINORITY check (present on <50% of PRs) used to fall through BOTH the typical-PR chart and
# the `minority_slow` footnote (which only footnoted checks slower than the TOP gate), so lightdash
# `E2E: API (Vitest)` (13m — the real binding floor of `Deploy Preview`) was disclosed NOWHERE on the
# spine and the ceiling overstated. This re-derives each drilled pole's binding floor from
# `populations` (the SAME co-occurrence floor as the #4 ceiling check) and asserts it is disclosed in
# a SPINE context — a drilled pole header, the floor a pole's note NAMES, or the "Also slower"
# footnote — NOT merely mentioned incidentally in an unrelated hygiene finding (an OPT43 queue-time
# `**Where:**` line names dozens of jobs; that incidental mention is exactly the false "clean" the
# old `check_dropped_check_not_framed_on_path` coverage hole let through). Engine fix: the floor pool
# is the full concurrent set, so the binding floor is named in the pole's note (Class A #6).
def _matrix_base(norm_name: str) -> str:
    """A check's matrix BASE — its name with a trailing `(variant)` stripped — so two legs of one
    matrix job (`integration-test (3.1.1, 3.12)` / `(… 3.13)`) share a base. Input is already
    `_cmp_name`-normalized. Used to tell a sibling-leg floor (same base) from a distinct check."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", norm_name).strip()


_SPINE_FLOOR_NAME_RE = re.compile(
    r"(?:next concurrent check|no workflow file to speed up here|the next leg|toward the next check"
    # The frequency-gate pole role line ("**The check most PRs gate on.** … the slowest concurrent
    # check is `X`") NAMES the pole's floor on the spine, but its phrasing differs from the floor-note
    # forms above — without it a legitimately-disclosed floor read as a silent spine drop (a
    # wording-coupled false FAIL on the frequency-gate shape; caught on out-of-sample dogfood repos).
    r"|slowest concurrent check is"
    # A BELOW-gate drilled pole's role line ("Runs concurrently behind `X` (…); it becomes the gate
    # only once every slower concurrent check drops below …") NAMES the check `X` this pole runs
    # behind — the slowest concurrent check above it, which the renderer selects by effective floor
    # (`_eff_floor_s`), i.e. the pole's binding floor. Without this the floor a pole names ONLY here
    # read as a silent spine drop (the sibling-family shape: a below-gate pole's floor is a matrix leg
    # of an already-drilled family, named nowhere else — a live-run false FAIL).
    r"|Runs concurrently behind)"
    r",?\s*`([^`]+)`")


def _spine_disclosed_names(report: str) -> set[str]:
    """Check names DISCLOSED in a SPINE context — drilled pole headers ∪ the floor each pole's
    'what a change here can buy' note NAMES ∪ the 'Also slower' minority footnote. Deliberately
    excludes incidental mentions in the off-path hygiene appendix (the false-clean source)."""
    names = {_cmp_name(check) for _wf, check, _body in _pole_header_sections(report)}
    for m in _SPINE_FLOOR_NAME_RE.finditer(report):
        names.add(_cmp_name(m.group(1)))
    for ln in report.splitlines():
        if "Also slower on" in ln:        # the minority-check footnote line (backtick-named checks)
            for nm in re.findall(r"`([^`]+)`", ln):
                names.add(_cmp_name(nm))
    names.discard("")
    return names


# --- #7: the TOC "Also noticed" pointer's on/off-path label must match the appendix -------------
# The Contents pointer summarizes the off-path appendix. When the appendix actually contains a
# credited wall-clock lever that sits ON the critical path (flagged inline), the pointer must NOT
# blanket-label the whole section "off-path hygiene … (~0 wall-clock), below the critical path" —
# that contradicts the on-path row (getlago). The appendix BLURB already qualifies this; the TOC
# pointer must too. Report-internal: if the appendix carries `_ON_PATH_SENTENCE`, the pointer line
# may not carry the blanket-off-path claim.
def check_toc_also_noticed_label_honest(report: str) -> Check:
    """**The TOC on/off-path label class (Phase 0, Class A #7).** The Contents "🧹 Also noticed"
    pointer must agree with the appendix on whether everything below is off-path: if the appendix
    contains an on-path wall-clock lever (`_ON_PATH_SENTENCE`), the pointer may not claim the whole
    section is "off-path … below the critical path" (the getlago mislabel). Report-internal."""
    name = "TOC 'Also noticed' pointer's on/off-path label matches the appendix"
    m = re.search(r"^\*\*🧹 Also noticed\*\*\s*[-—].*$", report, re.MULTILINE)
    if not m:
        return Check(name, True, "no TOC 'Also noticed' pointer", skipped=True)
    pointer = m.group(0)
    appendix = _section(report, "Also noticed")
    appendix_on_path = _ON_PATH_SENTENCE in appendix
    pointer_blanket_off = "below the critical path" in pointer or "~0 wall-clock" in pointer
    pointer_claims_on = "DO sit on the critical path" in pointer
    # Symmetric: the pointer's on/off-path label must MATCH the appendix in BOTH directions.
    if appendix_on_path and pointer_blanket_off:
        return Check(name, False,
                     "the 'Also noticed' appendix contains a finding that sits ON the merge-gating "
                     "critical path, but the TOC pointer labels the whole section off-path / below "
                     "the critical path / ~0 wall-clock — a contradiction: " + pointer.strip())
    if pointer_claims_on and not appendix_on_path:
        return Check(name, False,
                     "the TOC pointer claims one or more 'Also noticed' findings sit ON the critical "
                     "path, but the appendix carries no on-path lever (reverse mislabel): " + pointer.strip())
    if not appendix_on_path:
        return Check(name, True, "appendix carries no on-path lever; pointer doesn't claim one", skipped=True)
    return Check(name, True, "TOC pointer acknowledges the on-path lever in the appendix")


# --- #5: a drilled pole's job must not ALSO be framed as an "Also noticed" minor-cleanup finding ---
# The headline names a pole job as the single biggest lever; re-listing a co-located catalog finding
# on that SAME job in the off-path appendix as "minor cleanup, ~0 wall-clock" contradicts it
# (mindee/doctr: `pytest-torch` headlined the 25m pole AND appeared as an OPT24 "no merge-wait win").
# Report-internal property. The contradiction is NARROW: a VALUELESS finding (rendered "no bill
# saving" AND NOT flagged as a credited on-path wall-clock lever) whose Where jobs are ALL drilled
# poles. A credited lever on a pole job (an OPT73 bill saving showing "N min/mo", or a wall-clock
# lever carrying the on-path note) is a legitimate appendix entry on a different axis and must NOT be
# flagged — mirroring the engine's narrow `_also_noticed_block` exclusion. The appendix renders each
# location as `wf:line` (job), so the `:line` suffix is stripped before matching the pole's bare
# `wf`. Engine fix: `_also_noticed_block` drops exactly these valueless-all-pole findings.
_APPENDIX_WHERE_PAIR_RE = re.compile(r"`([^`]+)`\s*\((.*?)\)(?=\s*,\s*(?:`|\+)|\s*$)")


def _appendix_wf_job_key(wf: str, job: str) -> tuple[str, str]:
    """`(wf_base, matrix-base job)` for an appendix `**Where:**` segment. The rendered wf carries a
    `:line` suffix (`ci.yml:149`) the pole header lacks, so strip it before taking the basename."""
    return (Path(_strip_line_suffix(wf)).name, _matrix_base(_cmp_name(job)))


# Mirror of `blocking_path._WALL_CLOCK_LONG_POLE_FLOOR_S` (a credited wall-clock lever sits at/above
# this; OPT24's sub-second saving is below it). Standalone copy — keep in sync with the engine.
_WALL_CLOCK_LONG_POLE_FLOOR_S = 30.0


def check_pole_not_reframed_as_hygiene(report: str, findings_path: Path | None) -> Check:
    """**The pole double-frame class (Phase 0, Class A #5).** A VALUELESS finding (no bill saving AND
    not a credited wall-clock lever) whose jobs are ALL drilled long poles must not appear in the
    "Also noticed" off-path appendix — the pole already headlines that job as the biggest lever, so an
    "~0-wall-clock minor cleanup" row on it contradicts the headline (mindee/doctr: `pytest-torch`
    headlined the 25m pole AND appeared as an OPT24 "no merge-wait win"). The valueless + all-pole
    criterion is read from `findings.json` (`runner_min_saving`, `wall_clock_p50_s`/`off_spine`,
    `affected_jobs`) — NOT the rendered `**Where:**` line, which collapses a multi-job finding to one
    segment and would mis-read a finding that also touches a NON-pole job (kept by the engine) as
    all-pole. A credited bill/wall-clock lever on a pole job is a legitimate different-axis entry and
    is NOT flagged. Mirrors the engine's narrow `_also_noticed_block` exclusion exactly."""
    name = "no drilled pole's job is also framed as an 'Also noticed' minor-cleanup finding"
    pole_jobs = {(Path(wf).name, _matrix_base(_cmp_name(check)))
                 for wf, check, _body in _pole_header_sections(report)}
    if not pole_jobs or not findings_path:
        return Check(name, True, "no drilled poles / no findings", skipped=True)
    appendix = _section(report, "Also noticed")
    if not appendix:
        return Check(name, True, "no 'Also noticed' appendix", skipped=True)
    try:
        findings = _as_list(_as_dict(json.loads(findings_path.read_text(encoding="utf-8"))).get("findings"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    # Per appendix pattern block, the (wf_base, job_base) keys its `**Where:**` line discloses.
    block_keys: dict[str, set[tuple[str, str]]] = {}
    for block in appendix.split("<details>"):
        mp = re.search(r"<summary><strong>(OPT\d+)", block)
        if not mp:
            continue
        mw = re.search(r"^\*\*Where:\*\*\s*(.+)$", block, re.MULTILINE)
        keys = {_appendix_wf_job_key(wf, job)
                for wf, job in (_APPENDIX_WHERE_PAIR_RE.findall(mw.group(1)) if mw else [])}
        block_keys.setdefault(mp.group(1), set()).update(keys)
    offenders = []
    for f in findings:
        if not isinstance(f, dict) or f.get("advisory"):
            continue
        pat = str(f.get("pattern", ""))
        if pat not in block_keys:
            continue   # this pattern isn't rendered in the appendix
        rm = f.get("runner_min_saving")
        rm = float(rm) if isinstance(rm, (int, float)) else 0.0
        wc = f.get("wall_clock_p50_s")
        wc = float(wc) if isinstance(wc, (int, float)) else 0.0
        credited_wc = (not f.get("off_spine")) and wc >= _WALL_CLOCK_LONG_POLE_FLOOR_S
        if rm > 0 or credited_wc:
            continue   # a real saving on either axis — a legitimate appendix entry, not a contradiction
        # `_as_list`: a truthy non-iterable `affected_jobs` (a number/bool) must not crash the `for j in
        # jobs` below — coerce a wrong-type value to [] so it falls back to the single `job`.
        jobs = _as_list(f.get("affected_jobs")) or ([f.get("job")] if f.get("job") else [])
        if not jobs:
            continue
        wfb = Path(str(f.get("workflow_file") or "")).name
        fkeys = {(wfb, _matrix_base(_cmp_name(str(j)))) for j in jobs}
        if not (fkeys <= pole_jobs):
            continue   # touches a non-pole job → engine keeps it for that job's disclosure
        shown = fkeys & block_keys[pat]
        if shown:      # the valueless all-pole finding IS rendered (its pole job appears in its block)
            offenders.append(f"{pat} on " + ", ".join(f"`{w}` ({j})" for w, j in sorted(shown)))
    if offenders:
        return Check(name, False,
                     "a job rendered AS a drilled long pole (the headline's biggest lever) is ALSO "
                     "framed in 'Also noticed' as valueless off-path minor cleanup — a "
                     "self-contradiction: " + "; ".join(sorted(set(offenders))))
    return Check(name, True, "no valueless all-pole-job finding framed as off-path cleanup")


# --- #2: rendered per-workflow gate frequency must match the populations re-derivation -----------
# The TOC + per-pole headers render "`<workflow>.yml` gates N/M PRs" — how often that workflow holds
# the per-PR critical-path pole. The engine summed it over the REPRESENTATIVE poles only, dropping a
# non-representative matrix sibling leg's pole_count (razorpay/blade `blade-validate` rendered 4/20
# vs the true 5/20). Re-derive each workflow's gate frequency straight from `populations` — sum, over
# all of the workflow's checks, how often each is the per-PR slowest — and assert the rendered N/M
# matches EXACTLY (a deterministic integer count, no aggregation fuzziness). M must be npop.
_GATE_FREQ_RE = re.compile(r"`([^`]+\.ya?ml)`\s+gates\s+(\d+)/(\d+)\s+(?:sampled\s+)?PRs")


def _populations_pole_counts(cp: dict) -> tuple[dict[str, int], int, int]:
    """(`{RAW check name: times it is the per-PR slowest}`, npop, dropped) re-derived from
    `populations`, mirroring the engine's `_gate_counts` winner logic EXACTLY — `max` over each PR's
    positive checks in populations order (so a tie credits exactly one check, no drift) AND keyed by
    the **raw** check name, NOT `_cmp_name`. The engine keys `pole_count`/`wf_gate` by raw name, so
    `_cmp_name` here (which strips an `@scope/` prefix) would COLLAPSE two distinct monorepo checks
    (`@a/pkg build` + `@b/pkg build`) into one bucket and double-count it against the engine — a false
    positive on the very scoped-name repos the skill targets. The workflow join is by basename (which
    carries no scope), so raw names reconcile cleanly. `dropped` counts unparseable entries so a
    shrinking denominator is surfaced, not silent."""
    counts: dict[str, int] = {}
    n = dropped = 0
    for entry in _as_list(cp.get("populations")):
        try:
            _share, cks = entry
            pos = [(str(nm), float(p)) for nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not pos:
            continue
        n += 1
        winner = max(pos, key=lambda kv: kv[1])[0]
        counts[winner] = counts.get(winner, 0) + 1
    return counts, n, dropped


def check_workflow_gate_frequency_matches(report: str, findings_path: Path | None) -> Check:
    """**The per-workflow gate-frequency class (Phase 0, Class A #2).** Each rendered
    "`<workflow>` gates N/M PRs" claim must equal the populations re-derivation: N = how often ANY
    check in that workflow is the per-PR slowest (summed over the workflow's checks — only one check
    is slowest per PR, so no double-count), M = npop. Catches the matrix-sibling undercount where the
    engine summed over representative poles only. Standalone; keys on `populations` + `checks[]`."""
    name = "rendered per-workflow gate frequency matches the populations re-derivation"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no gate-frequency claim", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    pole_count, npop, dropped = _populations_pole_counts(cp)
    if not npop:
        return Check(name, True, "no per-PR populations to re-derive gate frequency from", skipped=True)
    wf_freq: dict[str, int] = {}
    for c in _as_list(cp.get("checks")):
        if not isinstance(c, dict):
            continue
        wf = Path(str(c.get("workflow_file") or "")).name
        if wf.endswith((".yml", ".yaml")):
            wf_freq[wf] = wf_freq.get(wf, 0) + pole_count.get(
                str(c.get("name") or c.get("check") or ""), 0)
    claims = _GATE_FREQ_RE.findall(report)
    if not claims:
        return Check(name, True, "no rendered '`wf` gates N/M PRs' claim", skipped=True)
    bad: list[str] = []
    checked = 0
    unjoinable = 0
    for wf_base, n_str, m_str in claims:
        true_n = wf_freq.get(wf_base)
        if true_n is None:
            unjoinable += 1   # the named workflow isn't in checks[] — can't re-derive, surfaced below
            continue
        checked += 1
        n, m = int(n_str), int(m_str)
        if n != true_n or m != npop:
            bad.append(f"`{wf_base}` rendered \"gates {n}/{m}\" but populations give {true_n}/{npop}")
    if bad:
        return Check(name, False,
                     "rendered per-workflow gate frequency disagrees with the populations "
                     "re-derivation (a matrix sibling leg's pole_count was dropped): " + "; ".join(bad))
    # Coverage disclosure (no bare "clean"): surface unjoinable claims + any unparseable populations
    # entries (a shrinking denominator must be visible, not silently excluded from M).
    notes = []
    if unjoinable:
        notes.append(f"{unjoinable} claim(s) named a workflow absent from checks[]")
    if dropped:
        notes.append(f"{dropped} populations entr(y/ies) unparseable, excluded from M")
    tail = f"; not fully verified: {', '.join(notes)}" if notes else ""
    if checked == 0:
        return Check(name, True, f"no re-derivable gate-frequency claim{tail}", skipped=True)
    return Check(name, True, f"all {checked} gate-frequency claim(s) match populations{tail}")


def check_spine_heavy_check_disclosed(report: str, findings_path: Path | None) -> Check:
    """**The spine-drop disclosure class (Phase 0, Class A #6).** Every drilled pole's BINDING floor
    — the slowest concurrent check that co-occurs with it on a MAJORITY of its own gating PRs,
    re-derived from `populations` — must be DISCLOSED on the spine (a drilled pole, the floor its note
    names, or the 'Also slower' footnote), never silently dropped. Catches a heavy minority check that
    materially caps the merge wait but is shown nowhere — the lightdash `E2E: API (Vitest)` class.
    Standalone; reuses the #4 `_cooccurrence_floor_s`. Disclosure is matched by EXACT normalized name
    against the spine contexts only, so an incidental hygiene-appendix mention can't launder it.

    Floor SELECTION uses the EFFECTIVE floor (`max(p50, bimodal high_p50_s)`) — the same notion the
    renderer's `_eff_floor_s` names the floor by — so a bimodal slow-mode that reorders which sibling
    is slowest can't make this name a different check than the report does (a false FAIL).

    SCOPE (documented limits, NOT silent): it verifies the single BINDING floor per pole — the check
    that actually CAPS the merge wait. A co-occurring check strictly BELOW that floor does not cap the
    wait (the floor gates first), so it is out of this property's scope; the #4 ceiling check guards
    the magnitude. A majority-co-occurring check absent from `checks[]` (no measured p50) can't be
    ranked and is invisible to the re-derivation (a rare artifact mismatch). A matrix SIBLING-LEG floor
    (same matrix base AND same workflow file — mirroring the engine's `_same_matrix`) is excluded: the
    pole already represents its own matrix and the matrix two-number note handles leg flooring. Every
    skip path is COUNTED and surfaced in the detail (no bare 'clean')."""
    name = "every drilled pole's binding floor is disclosed on the spine (no silent heavy-check drop)"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no spine to check", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    per_pr = _populations_per_pr(cp)
    pole_secs = _pole_header_sections(report)
    if not per_pr or not pole_secs:
        return Check(name, True, "no populations / no drilled poles", skipped=True)
    # Effective-floor value map (bimodal-aware, mirroring `_eff_floor_s`) + each check's workflow file
    # (for the sibling-leg corroboration). A floor is selected/valued by `eff`, not the bare p50.
    eff_by: dict[str, float] = {}
    wf_by: dict[str, str] = {}
    for c in _as_list(cp.get("checks")):
        if not isinstance(c, dict):
            continue
        nm = _cmp_name(str(c.get("name") or c.get("check") or ""))
        if not nm:
            continue
        p = c.get("p50_s")
        p = float(p) if isinstance(p, (int, float)) else 0.0
        bi = c.get("bimodal")
        hi = bi.get("high_p50_s") if isinstance(bi, dict) else None
        eff_by[nm] = max(p, float(hi) if isinstance(hi, (int, float)) else 0.0)
        wf_by[nm] = Path(str(c.get("workflow_file") or "")).name
    disclosed = _spine_disclosed_names(report)
    missing: list[str] = []
    checked = thin = no_floor = sibling = 0
    for wf, pole, _body in pole_secs:
        floor_s, floor_name, n_gating = _cooccurrence_floor_s(per_pr, pole, eff_by)
        if floor_s is None:
            thin += 1            # gates < _CEILING_MIN_GATING_PR — couldn't re-derive a stable floor
            continue
        if not floor_name or floor_s <= 0:
            no_floor += 1        # nothing heavy co-occurs on a majority — genuinely nothing to disclose
            continue
        # Sibling-leg exclusion: same matrix base AND same workflow file (mirrors the engine's
        # `_same_matrix`, name+workflow_file), so a DISTINCT job merely sharing a paren-stripped stem
        # in a different workflow is NOT silently excluded. A real matrix leg shares both.
        same_wf = (not wf_by.get(floor_name)) or Path(wf).name == wf_by.get(floor_name)
        if _matrix_base(floor_name) == _matrix_base(_cmp_name(pole)) and same_wf:
            sibling += 1
            continue
        checked += 1
        if floor_name not in disclosed:
            missing.append(f"pole `{pole}`'s binding floor `{floor_name}` "
                           f"(~{_fmt_clock(floor_s)} on a majority of {n_gating} gating PRs)")
    if missing:
        return Check(name, False,
                     "a heavy concurrent check that caps a pole's merge wait is disclosed NOWHERE on "
                     "the spine (not drilled, not named as the floor, not in the 'Also slower' "
                     "footnote — a silent spine drop the report's own footnote promise forbids): "
                     + "; ".join(missing))
    # Coverage disclosure (no bare "clean"): surface the poles we did NOT bound and why.
    skips = []
    if thin:
        skips.append(f"{thin} thin-sample (<{_CEILING_MIN_GATING_PR} gating PRs)")
    if no_floor:
        skips.append(f"{no_floor} no heavy co-occurring floor")
    if sibling:
        skips.append(f"{sibling} matrix sibling-leg floor")
    tail = f"; not checked: {', '.join(skips)}" if skips else ""
    if checked == 0:
        return Check(name, True, f"no drilled pole has a distinct re-derivable binding floor{tail}",
                     skipped=True)
    return Check(name, True,
                 f"all {checked} drilled-pole binding floor(s) disclosed on the spine{tail}")


def check_pole_ceiling_within_cooccurrence(report: str, findings_path: Path | None) -> Check:
    """**The addressable-ceiling class (Phase 0).** No rendered "biggest single measured win" /
    per-pole "what a change here can buy" ceiling may exceed `pole_p50 − binding_floor`, where the
    binding floor is re-derived from `populations` as the slowest concurrent check present on a
    MAJORITY of the pole's OWN gating PRs (not global presence). A quantified PROPERTY over the
    artifact + the rendered report: parse each rendered ceiling, re-derive the floor independently,
    assert `rendered ≤ pole_p50 − floor + tolerance` (an upper bound — catches the overstatement
    direction, robust to the renderer's exact bimodal/present-weighted aggregation). Catches the
    class across repos — every future repo whose ceiling demotes a co-occurring 2nd-slowest sibling
    under the old global-presence cutoff — for the two bound forms: the headline "biggest single
    measured win" (matrix poles INCLUDED — fivetran) and the per-pole SIMPLE floor note. The per-pole
    MATRIX two-number note floors against non-leg checks, so it is not bounded here (would
    false-positive on the legit shared-config number); such notes are COUNTED and disclosed in the
    detail, never silently dropped (bounding them is follow-up). Standalone; keys on `populations` +
    `checks[].p50_s` (present even in stampless bundles)."""
    name = "pole addressable ceiling within the co-occurrence floor"
    if not findings_path or _is_static_only(report):
        return Check(name, True, "no findings / static-only — no spine ceiling to bound", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    per_pr = _populations_per_pr(cp)
    p50_by: dict[str, float] = {}
    for c in _as_list(cp.get("checks")):
        if not isinstance(c, dict):
            continue
        nm = _cmp_name(str(c.get("name") or c.get("check") or ""))
        v = c.get("p50_s")
        if nm and isinstance(v, (int, float)):
            p50_by[nm] = float(v)
    # Each rendered ceiling claim: (human label, the pole/check it credits, rendered seconds).
    # Parsed BEFORE the per-PR-populations guard so a short-sample SKIP is LOUD and NARROW
    # (#45, L8): a rendered ceiling with NO populations to bound it is a "couldn't check", not
    # a clean pass — it must name the unbounded claims, never read green. (The engine-side fix
    # clamps `chain_win_s` to the population floor so the overstated ceiling never renders in
    # the first place; this is the defense-in-depth guard that no longer fails open silently.)
    claims: list[tuple[str, str, float]] = []
    mh = _CEILING_HEADLINE_RE.search(report)
    if mh:
        secs = _parse_clock_to_s(mh.group(1))
        if secs is not None:
            claims.append(("headline win", mh.group(2), secs))
    matrix_unbounded = 0   # per-pole matrix-form notes we can't bound here — disclosed, not dropped
    for wf, check, body in _pole_header_sections(report):
        cm = _CEILING_FLOOR_NOTE_RE.search(body)   # the simple-form note (matrix two-number form is skipped)
        if not cm:
            if _CEILING_MATRIX_MARKER in body:
                matrix_unbounded += 1
            continue
        secs = _parse_clock_to_s(cm.group(1))
        if secs is not None:
            claims.append((f"pole `{wf}` ▸ {check}", check, secs))
    if not per_pr:
        # NARROW: only a coverage gap when there IS a rendered ceiling to bound. LOUD: name how
        # many ceilings went unbounded and why, so the skip surfaces in grader_seeds as a
        # coverage gap (#45) instead of a silent clean pass. No rendered ceiling → a genuine
        # nothing-to-do skip.
        if claims:
            labels = ", ".join(lbl for lbl, _p, _s in claims[:3])
            return Check(name, True,
                         f"{len(claims)} rendered addressable-ceiling claim(s) could NOT be "
                         f"bounded — no per-PR populations to re-derive the co-occurrence floor "
                         f"from (short/single-PR sample): {labels}. Coverage gap, not a clean "
                         "pass (the engine clamps chain_win_s to the population floor so the "
                         "ceiling should already be grounded).", skipped=True)
        return Check(name, True, "no rendered addressable ceiling to bound", skipped=True)
    overs: list[str] = []
    checked = 0
    thin = 0          # claims skipped for too-few gating PRs (a "couldn't check", not "clean")
    unjoinable = 0    # claims whose pole label didn't reconcile to a `checks[]` entry
    for label, pole_name, rendered in claims:
        floor_s, floor_name, n_gating = _cooccurrence_floor_s(per_pr, pole_name, p50_by)
        if floor_s is None:
            thin += 1   # pole gates too few PRs — not enough co-occurrence signal, SKIP this claim
            continue
        pole_p50 = p50_by.get(_cmp_name(pole_name))
        if pole_p50 is None:
            unjoinable += 1   # pole isn't in the data layer's checks — nothing to bound against, SKIP
            continue
        checked += 1
        allowed = max(pole_p50 - floor_s, 0.0)
        ceiling = allowed * (1 + _CEILING_REL_TOL) + _CEILING_ABS_TOL_S
        if rendered > ceiling:
            overs.append(
                f"{label}: claims ~{_fmt_clock(rendered)} but a concurrent floor `{floor_name}` "
                f"({_fmt_clock(floor_s)}) on a majority of {n_gating} gating PRs caps the win at "
                f"~{_fmt_clock(allowed)} (pole p50 {_fmt_clock(pole_p50)})")
    if overs:
        return Check(name, False,
                     "rendered addressable ceiling overstates what the co-occurrence floor allows — "
                     "a faster floor was named because a near-universal concurrent check was demoted "
                     "by GLOBAL presence instead of presence on the pole's OWN gating PRs: "
                     + "; ".join(overs))
    # Coverage disclosure: a claim skipped for a thin sample / unjoinable label is "couldn't check",
    # NOT "clean" — surface those counts (and the unbounded matrix-form notes) instead of hiding them.
    skips = []
    if thin:
        skips.append(f"{thin} thin-sample (<{_CEILING_MIN_GATING_PR} gating PRs)")
    if unjoinable:
        skips.append(f"{unjoinable} pole label(s) not in checks[]")
    if matrix_unbounded:
        skips.append(f"{matrix_unbounded} matrix-form note(s) not bounded (known limit)")
    tail = f"; not bounded: {', '.join(skips)}" if skips else ""
    if checked == 0:
        return Check(name, True,
                     f"no bounded ceiling claim{tail or ' (thin samples / no parseable note)'}",
                     skipped=True)
    return Check(name, True,
                 f"all {checked} bounded ceiling(s) within the co-occurrence floor{tail}")


# ── Config-era boundary + recoverable-within-wait (issue #66) ────────────────────────────────────
# Literals COUPLED to the renderer (`blocking_path._CONFIG_ERA_DISCLOSED_MARKER` /
# `_RECOVERABLE_RECONCILE_MARKER`). verify_report imports no same-skill module, so the marker text is
# mirrored here; a reword on either side breaks the #66 fixtures in `test_config_era_boundary.py`
# (which drive the real renderer and assert the marker), so the two can't silently drift (L7).
_CONFIG_ERA_DISCLOSED_MARKER = "measures the previous configuration"
_CONFIG_ERA_NARROWED_MARKER = "narrowed to the current configuration"
# The post_only_thin provisional marker (issue #74) — mirrors blocking_path._CONFIG_ERA_THIN_MARKER.
_CONFIG_ERA_THIN_MARKER = "treat these numbers as provisional"
_RECOVERABLE_RECONCILE_MARKER = "slow-mode/worst-case figure on the PRs where"
# A recoverable ceiling within this margin of the typical wait is coherent read alone (clock
# rounding + benign aggregation) — only a ceiling clearly ABOVE the wait needs the reconciliation.
_RECOVERABLE_WAIT_TOL_S = 30.0


def check_recoverable_within_wait(report: str, findings_path: Path | None,
                                  report_path: Path | None = None) -> Check:
    """**Recoverable-within-wait coherence (issue #66 fix 2).** A rendered recoverable "up to
    ~X" ceiling — the headline "biggest single measured win" or a per-pole "what a change here
    can buy" note — that EXCEEDS the headline typical merge wait is incoherent read alone: a fix
    cannot give back more than a TYPICAL PR waits. When it does, the report MUST co-render the
    slow-mode/worst-case reconciliation (the excess is the pole's conditional figure on the PRs
    where it IS the pole, not the median). A bounds-family sibling (#24/#25/#30): the typical
    wait is re-derived from the SAME rendered headline figure `check_headline_wait_within_makespan`
    bounds (`_vr_headline_merge_dur_s`, L4 — mirror the engine's metric), each ceiling is parsed
    from the rendered report, and a ceiling above the wait whose context lacks the reconciliation
    marker FAILs. The headline win's reconciliation renders inline in the Bottom-line line (marker
    searched report-wide, it is globally unique to this reconciliation); a per-pole note's must
    appear in that pole's own section body. The matrix two-number note is not parsed here (same
    known limit as `check_pole_ceiling_within_cooccurrence`). SKIPs static-only / no headline wall
    / no rendered ceiling. Standalone; report-keyed."""
    name = "recoverable ceiling above the typical wait carries a worst-case reconciliation"
    if _is_static_only(report):
        return Check(name, True, "static-only — no headline wall or recoverable ceiling to bound",
                     skipped=True)
    wait_s, prov = _vr_headline_merge_dur_s(report, report_path)
    if wait_s is None:
        return Check(name, True, f"no rendered typical-wait figure ({prov}) — no wall to bound a "
                     "ceiling against", skipped=True)
    overs: list[str] = []
    checked = 0
    mh = _CEILING_HEADLINE_RE.search(report)
    if mh:
        secs = _parse_clock_to_s(mh.group(1))
        if secs is not None:
            checked += 1
            if secs > wait_s + _RECOVERABLE_WAIT_TOL_S and _RECOVERABLE_RECONCILE_MARKER not in report:
                overs.append(f"headline win ~{_fmt_clock(secs)} (`{mh.group(2)}`) exceeds the "
                             f"~{_fmt_clock(wait_s)} typical wait with no worst-case reconciliation")
    for wf, check, body in _pole_header_sections(report):
        cm = _CEILING_FLOOR_NOTE_RE.search(body)
        if not cm:
            continue
        secs = _parse_clock_to_s(cm.group(1))
        if secs is None:
            continue
        checked += 1
        if secs > wait_s + _RECOVERABLE_WAIT_TOL_S and _RECOVERABLE_RECONCILE_MARKER not in body:
            overs.append(f"pole `{wf}` ▸ {check}: ceiling ~{_fmt_clock(secs)} exceeds the "
                         f"~{_fmt_clock(wait_s)} typical wait with no worst-case reconciliation")
    if overs:
        return Check(name, False,
                     "a recoverable ceiling above the typical merge wait is rendered without the "
                     "slow-mode/worst-case reconciliation — the reader sees a fix that gives back "
                     "more than a typical PR waits: " + "; ".join(overs))
    if checked == 0:
        return Check(name, True, "no rendered recoverable ceiling to bound", skipped=True)
    return Check(name, True,
                 f"all {checked} rendered recoverable ceiling(s) within the typical wait or reconciled")


def check_config_era_boundary(report: str, findings_path: Path | None,
                              report_path: Path | None = None) -> Check:
    """**Config-era boundary (issue #66 fix 1).** When a workflow's sample straddled its
    last-change commit, the engine kept ONE era for that workflow's spine + drill contributions
    (never both — the fabricated "runs twice, once whole and once sharded" cross-era synthesis is
    then structurally impossible) and stamped the fact in `pr_critical_path.config_eras`
    (boundary, kept_era, rule, pre/post counts). A drilled pole bound to a workflow whose kept era
    is the PRE-change one measures the PREVIOUS configuration — its runs predate the boundary — so
    the report MUST carry the era disclosure. Re-derives from the stamped `config_eras` +
    `poles[].workflow_file` + the disclosure marker: FAILs a pre-era measurement (drilled pole or
    the spine) in a report without the disclosure. A LOUD NARROW SKIP on a legacy artifact with no
    `config_eras` key (nothing to bind). Standalone; keys on `pr_critical_path`."""
    name = "no drilled pole measures a pre-change config era without the era disclosure"
    if not findings_path:
        return Check(name, True, "no findings — no config-era stamp to bind", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    if "config_eras" not in cp:
        return Check(name, True, "no config_eras stamped (pre-#66 artifact) — nothing to bind",
                     skipped=True)
    eras = [e for e in _as_list(cp.get("config_eras"))
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return Check(name, True, "no workflow sample straddled a config change — no era to bind",
                     skipped=True)
    # Issue #116: a pre-era straddle owes the LOUD disclosure only when it can touch the headline.
    # A push/cron-only or check-neutral straddle (0 PR-gating spine checks) legitimately suppresses
    # its global caveat — the renderer gates on the same `_era_fact_spine_relevant`, so excluding it
    # here keeps the two sides in lockstep (the bill-scope note in Data sources still covers it).
    pre_wfs = {_cmp_name(str(e.get("workflow_file"))) for e in eras
               if e.get("kept_era") == "pre" and _era_fact_spine_relevant(e)}
    pre_wfs.discard("")
    if not pre_wfs:
        # All straddles measure the CURRENT config (post_only or post_only_thin). No pre-era
        # measurement, so the LOUD pre disclosure isn't owed — but each kept-post rule owes its OWN
        # caveat, else a shortened/thin sample looks like a full one. A stamped rule whose marker was
        # dropped is a silent drop → FAIL (symmetric with the pre branch).
        #   * post_only      → the "narrowed to the current configuration" note (window shortened).
        #   * post_only_thin → the "treat these numbers as provisional" note (issue #74: the kept pre
        #     era had no gate check, so we measured the NEW config on a thin post sample).
        # Issue #116 lockstep: the renderer suppresses the LOUD thin marker for a spine-irrelevant
        # (push/cron-only or check-neutral) straddle — the same gate `_era_fact_spine_relevant` — so
        # the demand here must be filtered identically, else the guard FAILs an honestly-suppressed
        # report (the exact renderer-vs-guard fight this issue removes for the pre side). The lighter
        # `post_only` narrowed note below is NOT spine-gated in the renderer, so it stays un-gated too.
        thin = sum(1 for e in eras
                   if e.get("rule") == "post_only_thin" and _era_fact_spine_relevant(e))
        if thin and _CONFIG_ERA_THIN_MARKER not in report:
            return Check(name, False,
                         f"{thin} straddling workflow(s) measure the current config on a THIN "
                         "post-change sample (post_only_thin) but the report omits the provisional "
                         "note — the reader can't tell the numbers are from a couple of post-change "
                         "runs (issue #74)")
        narrowed = sum(1 for e in eras if e.get("rule") == "post_only")
        if narrowed and _CONFIG_ERA_NARROWED_MARKER not in report:
            return Check(name, False,
                         f"{narrowed} straddling workflow(s) were narrowed to the current config "
                         "(post_only) but the report omits the narrowed-window note — the reader "
                         "can't tell earlier runs were excluded (a silently shortened window)")
        return Check(name, True, f"{narrowed} narrowed + {thin} thin straddling workflow(s) measure "
                     "the CURRENT config; their notes are present")
    disclosed = _CONFIG_ERA_DISCLOSED_MARKER in report
    offenders: list[str] = []
    for p in _as_list(cp.get("poles")):
        if not isinstance(p, dict):
            continue
        wf = _cmp_name(str(p.get("workflow_file") or ""))
        if wf and wf in pre_wfs:
            offenders.append(str(p.get("check") or p.get("job") or wf))
    if offenders and not disclosed:
        return Check(name, False,
                     "a drilled pole measures a PRE-change config era (its runs predate the "
                     "workflow's stamped last-change boundary) but the report omits the era "
                     "disclosure — the reader can't tell the numbers reflect the previous "
                     "configuration: poles " + ", ".join(f"`{o}`" for o in offenders[:5]))
    if not disclosed:
        # Pre-era straddle stamped but no drilled pole binds to it — the SPINE still reflects the
        # previous config, so the disclosure is owed regardless. (The engine always renders it when
        # a disclosed_pre era is stamped; a missing marker is a silent drop.)
        return Check(name, False,
                     "a workflow's sample measures the PRE-change config era (stamped "
                     "kept_era=pre) but the report omits the era disclosure: "
                     + ", ".join(f"`{w}`" for w in sorted(pre_wfs)[:5]))
    return Check(name, True,
                 f"{len(offenders)} pre-era drilled pole(s) + the spine carry the era disclosure"
                 if offenders else "pre-era config measurement carries the era disclosure")


def _era_rendered_check_names(cp: dict) -> set[str]:
    """Every check NAME a report enumerates from `pr_critical_path` — the Contents critical-path
    list / poles / populations all re-derive from these. Normalized with `_cmp_name` for a scope-tolerant compare
    against a straddle fact's stamped `other_era_checks`. The union of: the `checks` list, each
    `poles[]` check/job, and every per-PR `populations` member — so a post-era-only check can't
    hide in any one of the three enumeration surfaces."""
    names: set[str] = set()
    for c in _as_list(cp.get("checks")):
        if isinstance(c, dict) and c.get("name"):
            names.add(_cmp_name(str(c.get("name"))))
    for p in _as_list(cp.get("poles")):
        if isinstance(p, dict):
            for k in ("check", "job"):
                if p.get(k):
                    names.add(_cmp_name(str(p.get(k))))
    for pop in _as_list(cp.get("populations")):
        # populations entry: [share, [[name, p50], ...]]
        if isinstance(pop, list) and len(pop) == 2 and isinstance(pop[1], list):
            for member in pop[1]:
                if isinstance(member, list) and member and member[0]:
                    names.add(_cmp_name(str(member[0])))
    names.discard("")
    return names


def _era_fact_spine_relevant(e: dict) -> bool:
    """Mirror of `blocking_path._era_fact_spine_relevant` (issue #116): may this straddle fact carry
    the GLOBAL "the headline reflects the old/thin config" caveat — does it touch the PR-gating spine?

    Trust a stamped `spine_relevant` bool (the engine's dev-event AND spine-check decision); else
    re-derive from the bound enumeration sets already on the fact — both empty ⇒ spine-irrelevant ⇒
    the caveat must be suppressed. A truly pre-enumeration fact (neither set stamped) is not
    re-derivable, so default relevant (matches the renderer and this guard's own narrow skip)."""
    sr = e.get("spine_relevant")
    if isinstance(sr, bool):
        return sr
    if "kept_checks" in e or "other_era_checks" in e:
        return bool(_as_list(e.get("kept_checks")) or _as_list(e.get("other_era_checks")))
    return True


def check_era_enumeration_bound(report: str, findings_path: Path | None,
                                report_path: Path | None = None) -> Check:
    """**Config-era enumeration binding (issue #69).** #66/#68 bound the SPINE TIMING to one config
    era, but the enumerated CHECK SET was still drawn from the raw PR-gate sample — so a
    `disclosed_pre` report could render the NEW config's checks (`guard shard 1/4..4/4`, carried by
    a couple of post-change PRs) as poles / Level-1 bars / populations beside the pre-era `test`
    timing: a configuration that never ran, and the seed of a fabricated cross-era redundancy.

    `collect_runs._era_scope_enumeration` now drops every check absent from the kept era and stamps
    it on the straddle fact as `other_era_checks` (the other configuration's adds/removes), keeping
    `kept_checks` alongside. This guard re-derives the bind: NO check the report enumerates (from
    `pr_critical_path.checks` / `poles` / `populations`) may be a member of any era's
    `other_era_checks` — i.e. no rendered pole/check is absent from the kept era's stamped run set.
    FAILs loudly and names the leak. A LOUD NARROW SKIP on a straddle artifact that predates the
    #69 enumeration stamps (a stamped straddle but no `other_era_checks`/`kept_checks` key anywhere
    — nothing to re-derive against). Standalone; keys on `pr_critical_path`."""
    name = "check enumeration is bound to the kept config era (no other-config check leaks in)"
    if not findings_path:
        return Check(name, True, "no findings — no config-era stamp to bind", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    if "config_eras" not in cp:
        return Check(name, True, "no config_eras stamped (pre-#66 artifact) — nothing to bind",
                     skipped=True)
    eras = [e for e in _as_list(cp.get("config_eras"))
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return Check(name, True, "no workflow sample straddled a config change — no era to bind",
                     skipped=True)
    # LOUD NARROW SKIP: a straddle is stamped but none carries the #69 enumeration stamps — a
    # pre-#69 artifact whose enumeration was never era-scoped. Nothing to re-derive against (not a
    # clean pass: the report may well leak, we just can't prove it from this artifact).
    stamped = [e for e in eras if "other_era_checks" in e or "kept_checks" in e]
    if not stamped:
        return Check(name, True, "straddle stamped but no per-era enumeration sets "
                     "(other_era_checks/kept_checks) — pre-#69 artifact, enumeration bind not "
                     "re-derivable. Coverage gap, not a clean pass.", skipped=True)
    other_era: set[str] = set()
    for e in stamped:
        for n in _as_list(e.get("other_era_checks")):
            cn = _cmp_name(str(n))
            if cn:
                other_era.add(cn)
    if not other_era:
        return Check(name, True, "the other configuration adds/removes no checks — every stamped "
                     f"straddle enumerates a single era ({len(stamped)} straddle(s) checked)")
    rendered = _era_rendered_check_names(cp)
    leaks = sorted(rendered & other_era)
    if leaks:
        return Check(name, False,
                     "the report enumerates check(s) that belong to the OTHER configuration (absent "
                     "from the kept era's runs) — a config that never ran is rendered as poles/bars: "
                     + ", ".join(f"`{lk}`" for lk in leaks[:6])
                     + ". The enumeration must be bound to the kept era (issue #69).")
    return Check(name, True,
                 f"no other-era check leaks into the enumeration ({len(other_era)} other-config "
                 f"check(s) named in the era note, none rendered as a pole/bar/population)")


def check_era_disclosure_matches_enumeration(report: str, findings_path: Path | None,
                                             report_path: Path | None = None) -> Check:
    """**Era disclosure ⟺ enumeration (issue #74).** The direct contradiction guard: a report whose
    RENDERED era disclosure claims it "measures the previous configuration" (pre-only) must not, at
    the same time, enumerate a check that never ran in the kept PRE era. The live #74 lie: the sole
    gate-bearing PRs were all POST-change, so the disclosed_pre run-count cut emptied the enumeration,
    the never-empties fallback skipped the cut whole (clearing the stamps `check_era_enumeration_bound`
    re-derives from — going blind), and the report rendered the NEW config's `test`/`guard shard N/4`
    beside a "reflect the configuration BEFORE it" disclosure. Nothing pre-era was measured at all.

    Re-derivable now that the engine's pre-drill flip + spine re-drill (`_era_resolve_thin_flip`,
    issue #74) makes stamps ALWAYS survive — the state space is total {post_only, post_only_thin,
    disclosed_pre}, so a
    disclosed_pre fact must carry a NON-empty `kept_checks` (it really measured a pre-era check) and
    a rendered pre-only disclosure must correspond to one. This guard FAILs, keyed independently on
    the RENDERED disclosure marker rather than on the enumeration stamps alone, when:
      * the report renders the pre-only disclosure but NO stamped fact is `disclosed_pre` (the
        disclosure is rendered over an all-post / narrowed measurement); or
      * a stamped `disclosed_pre` fact has an EMPTY `kept_checks` while its `other_era_checks` is
        non-empty (the #74 shape — the pre era measured nothing, everything enumerated is post-era); or
      * the report enumerates (from `checks`/`poles`/`populations`) a check named in a `disclosed_pre`
        fact's `other_era_checks` (a post-only check under a pre-only disclosure); or
      * (issue #116) the report renders a GLOBAL era caveat (pre-only OR the thin-sample marker) for a
        straddle that touches NO PR-gating spine check — a push/cron-only or check-neutral workflow
        (`spine_relevant` stamped False, or both enumeration sets empty). Such a straddle changed only
        its runner-minute layout (the Data-sources bill-scope note covers that); globalizing "the
        headline reflects the old/thin config" over a spine it never gates is the overreach. A
        companion arm FAILs a `spine_relevant` stamp incoherent with its basis (developer_event AND a
        kept/other spine check). NOTE: a disclosed_pre may now be spine-IRRELEVANT (empty kept AND
        empty other) — a valid suppressed state, distinct from the #74 hollow shape (empty kept,
        NON-empty other) that still FAILs.
    A LOUD NARROW SKIP on a straddle artifact without the enumeration stamps (pre-#69/#74 — the
    cleared-stamp shape the OLD guard went blind on; not re-derivable, a coverage gap not a clean
    pass). Standalone; keys on `pr_critical_path` + the rendered disclosure marker.

    **Timing-provenance leg, basis-aware (issue #77).** A POST-claiming straddle whose pole drilled a
    PRE-era run FAILs — but "pre-era" is judged by the pole's stamped classification BASIS, closing
    the timestamp blind spot the #76 guard shared. With basis "content" the pole's era comes from the
    CONTENT the run executed, so a `repr_run_created_at` before the boundary is not itself a
    contradiction; the check reads the era from the fact-level `content_era_by_sha` map and FAILs when
    that map places the pole's run "pre" under a post claim. It ALSO flags the pole's own
    `repr_run_era` disagreeing with the map — but note the pole stamp is COPIED from that same map by
    `_stamp_pole_repr_run_era`, so this coherence arm only catches post-stamping MUTATION
    (serialization / hand-edit drift), NOT a classification bug: a `_resolve_content_eras` mislabel the
    pole faithfully copies is common-mode and not caught here (verify holds no independent content
    source — it never re-fetches a blob). The genuinely independent legs are the map-says-"pre" arm and,
    with basis "timestamp"/absent, the pre-#77 `created_at` < boundary rule."""
    name = "the rendered era disclosure matches the enumerated config (no pre-only caveat over post-era checks)"
    if not findings_path:
        return Check(name, True, "no findings — no config-era stamp to bind", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    if "config_eras" not in cp:
        return Check(name, True, "no config_eras stamped (pre-#66 artifact) — nothing to bind",
                     skipped=True)
    eras = [e for e in _as_list(cp.get("config_eras"))
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return Check(name, True, "no workflow sample straddled a config change — no era to bind",
                     skipped=True)
    stamped = [e for e in eras if "other_era_checks" in e or "kept_checks" in e]
    if not stamped:
        return Check(name, True, "straddle stamped but no per-era enumeration sets "
                     "(other_era_checks/kept_checks) — pre-#69/#74 artifact, the disclosure/enumeration "
                     "bind isn't re-derivable. Coverage gap, not a clean pass.", skipped=True)
    disclosed_pre = [e for e in stamped if e.get("rule") == "disclosed_pre"]
    # (1) A disclosed_pre fact that measured NOTHING pre-era (empty kept, non-empty other) — the #74
    #     shape. This is the lie whether or not the marker parsed cleanly, so check the stamps first.
    hollow = [e for e in disclosed_pre
              if not _as_list(e.get("kept_checks")) and _as_list(e.get("other_era_checks"))]
    if hollow:
        wfs = ", ".join(f"`{_cmp_name(str(e.get('workflow_file')))}`" for e in hollow[:5])
        return Check(name, False,
                     "a disclosed_pre straddle claims to measure the PREVIOUS configuration but its "
                     "kept (pre) era enumerates NO check — every enumerated check belongs to the new "
                     f"config (issue #74; should be post_only_thin): {wfs}")
    marker = _CONFIG_ERA_DISCLOSED_MARKER in report
    # (2) The pre-only disclosure is rendered but nothing pre-era was kept.
    if marker and not disclosed_pre:
        rules = sorted({str(e.get("rule")) for e in stamped})
        return Check(name, False,
                     "the report renders the pre-only era disclosure ('measures the previous "
                     "configuration') but no stamped straddle is disclosed_pre — the caveat is "
                     f"rendered over an all-post/narrowed measurement (stamped rules: {rules})")
    # (3) A post-only check enumerated under the pre-only disclosure (independent of enum_bound, keyed
    #     on the RENDERED marker: even a report that dropped the enum stamps but kept the disclosure).
    if marker:
        other_era: set[str] = set()
        for e in disclosed_pre:
            for n in _as_list(e.get("other_era_checks")):
                cn = _cmp_name(str(n))
                if cn:
                    other_era.add(cn)
        leaks = sorted(_era_rendered_check_names(cp) & other_era)
        if leaks:
            return Check(name, False,
                         "the report renders the pre-only era disclosure yet enumerates check(s) "
                         "absent from the kept PRE era (they belong to the new config): "
                         + ", ".join(f"`{lk}`" for lk in leaks[:6]) + " (issue #74).")
    # (3b) Issue #116: a GLOBAL era caveat rendered for a straddle that CANNOT touch the headline —
    #      a push/cron-only or check-neutral workflow (0 PR-gating spine checks). Such a straddle
    #      globalizes "the headline reflects the old/thin config" over a spine it never gates; its
    #      staleness belongs only to the bill-scope note. Re-derived from the SAME stamp/basis the
    #      renderer gates on (`_era_fact_spine_relevant`), keyed on the RENDERED marker + the
    #      workflow file on its caveat line, so the guard and renderer can't drift. The stamp-
    #      integrity arm re-derives `spine_relevant` from its basis (developer_event AND a kept/other
    #      spine check) and FAILs a stamp that lies about its own inputs.
    report_lines = report.splitlines()
    integrity: list[str] = []
    overreach: list[str] = []
    for e in stamped:
        if isinstance(e.get("spine_relevant"), bool) and isinstance(e.get("developer_event"), bool):
            expected = bool(e.get("developer_event")) and bool(
                _as_list(e.get("kept_checks")) or _as_list(e.get("other_era_checks")))
            if bool(e.get("spine_relevant")) != expected:
                integrity.append(_cmp_name(str(e.get("workflow_file"))))
        rule = str(e.get("rule"))
        if rule not in ("disclosed_pre", "post_only_thin") or _era_fact_spine_relevant(e):
            continue
        wf_raw = str(e.get("workflow_file") or "")
        mk = _CONFIG_ERA_DISCLOSED_MARKER if rule == "disclosed_pre" else _CONFIG_ERA_THIN_MARKER
        if wf_raw and any(mk in ln and wf_raw in ln for ln in report_lines):
            overreach.append(_cmp_name(wf_raw))
    if integrity:
        return Check(name, False,
                     "a config-era straddle's `spine_relevant` stamp disagrees with its basis "
                     "(developer_event AND a kept/other spine check) — the stamp is incoherent "
                     "(issue #116): " + ", ".join(f"`{w}`" for w in integrity[:5]))
    if overreach:
        return Check(name, False,
                     "a GLOBAL era caveat is rendered for a straddle that touches NO PR-gating "
                     "spine check (push/cron-only or check-neutral) — it globalizes 'the headline "
                     "reflects the old/thin config' over a spine that workflow never gates "
                     "(issue #116): " + ", ".join(f"`{w}`" for w in overreach[:5]))
    # (4) Timing-provenance leg: a POST-claiming straddle (post_only / post_only_thin) whose pole's
    #     drilled runs are PRE-era is a pre-era TIMING under a "measures the new config" claim — the
    #     exact defect the reviewer caught (pole p50/drill/links from pre-era runs while the disclosure
    #     claims post). How the pole's era is re-derived depends on the stamped classification BASIS
    #     (issue #77 — the timestamp blind spot the #76 guard shared):
    #       * basis "content" — the era is derived from the workflow-file CONTENT the pole's repr run
    #         actually executed, so a `repr_run_created_at` BEFORE the boundary is NOT a contradiction
    #         (a fix-PR ran the new config early). This leg does not re-fetch blobs offline. The
    #         LOAD-BEARING signal is the fact-level `content_era_by_sha[repr_run_head_sha]`: FAIL when
    #         that authoritative map places the run "pre" under a post claim. It also FLAGS the pole's
    #         own `repr_run_era` disagreeing with the map — but that arm is NOT an independent check:
    #         `_stamp_pole_repr_run_era` COPIES `repr_run_era` verbatim from this same map, so the two
    #         agree by construction on any engine output and the disagreement can only arise from
    #         post-stamping MUTATION (serialization / hand-edit drift). A `_resolve_content_eras`
    #         mislabel the pole faithfully copies (a genuinely-pre run stamped "post" in the map) is
    #         common-mode and NOT caught — verify has no independent content source. A "content" basis
    #         with no backing map entry is unsupported → it falls through to the timestamp check below
    #         rather than being trusted blind.
    #       * basis "timestamp" / absent (legacy) — the pre-#77 rule: `repr_run_created_at` < boundary
    #         is the contradiction.
    post_claim_facts = {_cmp_name(str(e.get("workflow_file"))): e
                        for e in stamped
                        if e.get("rule") in ("post_only", "post_only_thin") and e.get("boundary")}
    pre_era_poles: list[str] = []
    incoherent: list[str] = []
    for p in _as_list(cp.get("poles")):
        if not isinstance(p, dict):
            continue
        wf = _cmp_name(str(p.get("workflow_file") or ""))
        fact = post_claim_facts.get(wf)
        if not wf or fact is None:
            continue
        boundary = str(fact.get("boundary") or "")
        pole_name = str(p.get("check") or p.get("job") or wf)
        basis = str(p.get("repr_run_era_basis") or "")
        cmap = fact.get("content_era_by_sha")
        cmap = cmap if isinstance(cmap, dict) else {}
        head = str(p.get("repr_run_head_sha") or "")
        stamped_era = str(p.get("repr_run_era") or "")
        if basis == "content" and head and head in cmap:
            authoritative = str(cmap.get(head))          # the fact map — the authoritative era for this head
            if stamped_era and stamped_era != authoritative:
                # The pole's `repr_run_era` is copied from this same map, so a disagreement is
                # post-stamping mutation (serialization / hand-edit), not a classification-bug signal.
                incoherent.append(pole_name)
            elif authoritative == "pre":
                pre_era_poles.append(pole_name)           # a pre-config pole under a post claim
            continue
        # basis "timestamp" / absent / an unsupported "content" stamp → the created_at-vs-boundary rule.
        created = str(p.get("repr_run_created_at") or "")
        if boundary and created and created < boundary:
            pre_era_poles.append(pole_name)
    if incoherent:
        return Check(name, False,
                     "a POST-claiming straddle carries a pole whose content-basis era stamp "
                     "(`repr_run_era`) DISAGREES with the fact's `content_era_by_sha` map for the "
                     "same head sha — the two era stamps are incoherent (issue #77): poles "
                     + ", ".join(f"`{o}`" for o in incoherent[:5]))
    if pre_era_poles:
        return Check(name, False,
                     "a POST-claiming era disclosure (post_only/post_only_thin) carries a pole whose "
                     "drilled runs are PRE-era (they predate the workflow's config boundary by content "
                     "or timestamp) — the pole's timing/drill derives from the OLD config while the "
                     "disclosure claims the new one (issue #74 direction (a); the spine must re-drill "
                     "from post-era runs): poles "
                     + ", ".join(f"`{o}`" for o in pre_era_poles[:5]))
    kept_ok = sum(1 for e in disclosed_pre if _as_list(e.get("kept_checks")))
    return Check(name, True,
                 f"disclosure matches enumeration ({len(disclosed_pre)} disclosed_pre straddle(s), "
                 f"{kept_ok} with a real pre-era measurement; no pre-only caveat over post-era checks; "
                 "no post-claiming pole drilled from a pre-era run)")


def check_era_chain_spine_bound_to_kept_era(report: str, findings_path: Path | None,
                                            report_path: Path | None = None) -> Check:
    """**Per-PR chain/makespan spine bound to the kept era (issue #80).** #66/#68 scoped the spine
    RUNS and #69 the enumerated CHECK SET, but the PER-PR layer — the sample that feeds
    `chain_facts → chain_summary → makespan_p50_s` (the "typical PR waits N" headline and the #24
    physical-bound cap), the populations, and the presence denominators — was filtered only by check
    NAME. A check name survives a config change, so a DROPPED-era PR's `test` interval flowed into the
    makespan under a disclosure claiming the KEPT era (live: a 166s post-era makespan cap on a
    538s-gate pre era). `collect_runs._era_scope_pr_spine_sample` now scopes that sample to the kept
    side. This guard re-derives the bind offline from the committed artifact, in three legs:

      1. **n-bound.** `chain_summary.n` (the number of per-PR chain facts feeding the makespan) must
         not EXCEED `sampled_pr_count` (the kept-side count post the door). A chain layer that counts
         more PRs than the kept-side sample is un-scoped though the count was — a partial-revert seam.

      2. **Content sha-provenance (disclosed_pre).** For a `disclosed_pre` straddle whose
         `content_era_by_sha` map places a sampled head "post" (the DROPPED side), no `chain_facts`
         entry for that head may carry a chain MEMBER named in the fact's `kept_checks` — a kept
         (pre) era check whose timing was measured on a dropped (post) PR is exactly the blend. Sound
         and false-positive-free: `content_era_by_sha` is the fact's own authoritative classification,
         and a post-side PR that legitimately SURVIVES the surgical door does so only on non-straddling
         (era-neutral) checks, which are never in `kept_checks`.

      3. **Makespan physical floor (disclosed_pre).** Under a pre claim, if a kept gate check G is
         present on EVERY kept sampled PR (`present_on == sampled_pr_count > 0`), the median per-PR
         makespan cannot fall BELOW G's own p50 — every such PR ran G, and a PR's makespan (max end −
         min start, span-capped) is >= any single check's span, so the median makespan >= median(G) =
         p50_G. `chain_summary.makespan_p50_s < p50_G` means the makespan was measured on faster,
         dropped-era PRs. Restricted to UNANIMOUS presence so it can never false-positive on a
         legitimately fast minority PR; a non-unanimous kept gate is NOT re-derivable this way offline
         (the median PR may not have run it) and is left to leg 2 + the engine-side door.

    Standalone; keys on `pr_critical_path`. A LOUD NARROW SKIP on a straddle artifact predating the
    #69/#74 enumeration stamps (nothing to re-derive against). Pure SKIP when nothing straddled."""
    name = "the per-PR chain/makespan spine is bound to the kept config era (no dropped-era PR blends in)"
    if not findings_path:
        return Check(name, True, "no findings — no config-era stamp to bind", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    cp = _as_dict(_as_dict(data).get("pr_critical_path"))
    if "config_eras" not in cp:
        return Check(name, True, "no config_eras stamped (pre-#66 artifact) — nothing to bind",
                     skipped=True)
    eras = [e for e in _as_list(cp.get("config_eras"))
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return Check(name, True, "no workflow sample straddled a config change — no spine to bind",
                     skipped=True)
    stamped = [e for e in eras if "other_era_checks" in e or "kept_checks" in e]
    if not stamped:
        return Check(name, True, "straddle stamped but no per-era enumeration sets — pre-#69/#74 "
                     "artifact, the spine bind isn't re-derivable. Coverage gap, not a clean pass.",
                     skipped=True)
    cs = _as_dict(cp.get("chain_summary"))
    chain_facts = [c for c in _as_list(cp.get("chain_facts")) if isinstance(c, dict)]
    sampled = _num(cp.get("sampled_pr_count"))
    # Leg 1 — n-bound: the chain layer must not count more PRs than the kept-side sample.
    cs_n = _num(cs.get("n"))
    if cs_n is not None and sampled is not None and cs_n > sampled:
        return Check(name, False,
                     f"chain_summary.n ({int(cs_n)}) exceeds the kept-side sampled_pr_count "
                     f"({int(sampled)}) — the per-PR chain/makespan spine counts more PRs than the "
                     "era door kept, so dropped-era PRs still feed the makespan (issue #80).")
    disclosed_pre = [e for e in stamped if e.get("rule") == "disclosed_pre"]
    # Leg 2 — content sha-provenance: a dropped(post)-side head must not carry a kept-era chain member.
    for e in disclosed_pre:
        cmap = e.get("content_era_by_sha")
        cmap = cmap if isinstance(cmap, dict) else {}
        kept = {_cmp_name(str(n)) for n in _as_list(e.get("kept_checks"))}
        if not cmap or not kept:
            continue
        dropped_heads = {sha for sha, era in cmap.items() if str(era) == "post"}
        for cf in chain_facts:
            if str(cf.get("sha") or "") not in dropped_heads:
                continue
            members = {_cmp_name(str(m)) for m in _as_list(cf.get("chain"))}
            leak = sorted(members & kept)
            if leak:
                return Check(name, False,
                             "a disclosed_pre straddle carries a chain fact for a DROPPED (post) head "
                             f"`{cf.get('sha')}` whose chain includes kept (pre) era check(s) "
                             + ", ".join(f"`{lk}`" for lk in leak[:5])
                             + " — post-era timing feeds the pre-claiming makespan (issue #80).")
    # Leg 3 — makespan physical floor: a pre-claiming makespan can't sit below a unanimous kept gate.
    makespan = _num(cs.get("makespan_p50_s"))
    if makespan is not None and makespan > 0 and disclosed_pre and sampled and sampled > 0:
        kept_gate = set()
        for e in disclosed_pre:
            for n in _as_list(e.get("kept_checks")):
                kept_gate.add(_cmp_name(str(n)))
        for c in _as_list(cp.get("checks")):
            if not isinstance(c, dict):
                continue
            cn = _cmp_name(str(c.get("name") or ""))
            if cn not in kept_gate:
                continue
            present = _num(c.get("present_on"))
            p50 = _num(c.get("p50_s"))
            if present is not None and p50 is not None and present == sampled and makespan < p50:
                return Check(name, False,
                             f"the makespan p50 ({makespan:g}s) is BELOW the kept (pre) era gate "
                             f"`{c.get('name')}` (p50 {p50:g}s), which ran on every one of the "
                             f"{int(sampled)} kept sampled PR(s) — a makespan below a unanimous gate "
                             "is physically impossible in the kept era; it was measured on faster "
                             "dropped-era PRs (issue #80).")
    n_disp = int(cs_n) if cs_n is not None else 0
    return Check(name, True,
                 f"the per-PR spine is bound to the kept era ({len(disclosed_pre)} disclosed_pre "
                 f"straddle(s); {n_disp} chain fact(s) within the kept-side sample; no dropped-era "
                 "head feeds a kept-era chain member; no makespan below a unanimous kept gate)")


# The measured sizing DOOR invariants (issues #43/#44/#45). Every rendered runner-minute saving must
# derive from — or be clamped by — the measured cost-spine rows for its affected jobs, stamped with a
# recognized `runner_min_basis`; and a cluster-floor lever's wall-clock ceiling must NOT be capped by
# one of its own matrix sibling legs (which descend with the fix).
_RM_BASIS_OK = frozenset({
    "measured_spine_billable",   # derived from / confirmed within the affected jobs' measured billable
    "measured_spine_clamped",    # modeled figure clamped DOWN to the measured billable (#43)
    "not_spine_derivable",       # explicit, reasoned whitelist (measured run-elimination / modeled hygiene)
    "unmeasured_no_spine",       # a spine-bound pattern with no render-ready spine to bind against
})


def check_saving_carries_measured_basis(report: str, findings_path: Path | None) -> Check:
    """**The sizing-door teeth (#43/#44/#45, D-i).** Under a render-ready cost spine, EVERY finding
    that credits a positive `runner_min_saving` MUST carry a recognized `runner_min_basis` stamp — the
    measured sizing door (`collect_runs._reground_runner_minute_savings`) can't be silently bypassed. A
    positive-saving finding with NO basis (a pattern that skipped the door), or the
    `UNCLASSIFIED_door_policy` sentinel (a rm-crediting pattern with no declared door policy), or any
    unrecognized basis, FAILs. SKIPs without a render-ready spine (no measured basis to bind against —
    the same gate as `check_saving_within_measured_compute`), so a legacy pre-door artifact never trips
    it. Standalone; keys on `runner_minute_spine.render_ready` + `findings[].runner_min_saving`/
    `runner_min_basis`."""
    name = "every credited runner-minute saving carries a measured-basis stamp (sizing door)"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    spine = _as_dict(data.get("runner_minute_spine"))
    if _as_list(spine.get("rows")) == [] or spine.get("render_ready") is not True:
        return Check(name, True, "no render-ready runner-minute cost spine — the sizing door does not "
                     "run, so there is no measured basis to require", skipped=True)
    missing: list[str] = []
    unclassified: list[str] = []
    unknown: list[str] = []
    checked = 0
    for f in _as_list(data.get("findings")):
        f = _as_dict(f)
        saving = _num(f.get("runner_min_saving"))
        if saving is None or saving <= 0:
            continue
        checked += 1
        basis = f.get("runner_min_basis")
        pat = str(f.get("pattern", "?"))
        if basis == "UNCLASSIFIED_door_policy":
            unclassified.append(pat)
        elif not basis:
            missing.append(f"{pat}({', '.join(str(j) for j in _as_list(f.get('affected_jobs'))[:2])})")
        elif basis not in _RM_BASIS_OK:
            unknown.append(f"{pat}={basis}")
    if unclassified:
        return Check(name, False,
                     f"pattern(s) {sorted(set(unclassified))} credit a runner-minute saving but have no "
                     "declared sizing-door policy (stamped UNCLASSIFIED_door_policy) — classify them in "
                     "collect_runs._RM_DOOR_OVERRIDES/_SIZING so their saving derives from or is clamped "
                     "to measured compute; an unmeasured sizing path must never ship")
    if missing:
        return Check(name, False,
                     f"{len(missing)} finding(s) credit a runner-minute saving under a render-ready cost "
                     f"spine but carry NO runner_min_basis stamp — they bypassed the sizing door: "
                     f"{', '.join(missing[:4])}")
    if unknown:
        return Check(name, False,
                     f"{len(unknown)} finding(s) carry an unrecognized runner_min_basis "
                     f"(not one of {sorted(_RM_BASIS_OK)}): {', '.join(unknown[:4])}")
    if checked == 0:
        return Check(name, True, "no credited runner-minute savings to bind", skipped=True)
    return Check(name, True, f"{checked} credited saving(s) carry a measured-basis stamp")


_BACKTICK_NAME_RE = re.compile(r"`([^`]+)`")


def check_cluster_lever_ceiling_escapes_sibling(report: str, findings_path: Path | None) -> Check:
    """**Cluster-floor lever escapes its sibling cap (#44, D-iii).** A cluster-floor lever (OPT73) fixes
    a step shared across concurrent matrix sibling legs — cutting it lowers ALL of them at once, so a
    SIBLING leg can never be the floor that caps its wall-clock (the sibling descends WITH the fix). Any
    OPT73 finding whose `wall_clock_derivation` shows the measured-critical-path bound capping it at a
    floor check that IS one of its own `affected_jobs` (matrix base matched) FAILs — that is the ~15x
    mastodon understatement (`Run bin/flatware rspec` capped at ~40s by a sibling rspec leg instead of
    floored to the ~639s ceiling the 202s non-sibling Elastic Search check allows — 40s vs 639s is the
    ~15x). Re-derived from the committed finding's own
    derivation + affected-jobs list. SKIPs when no OPT73 finding carries a measured-critical-path
    derivation entry. Standalone; keys on `findings[].pattern`/`affected_jobs`/`wall_clock_derivation`."""
    name = "cluster-floor lever's ceiling floors at a NON-sibling check (not its own matrix leg)"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)

    def _base(n: str) -> str:
        return _cmp_name(re.sub(r"\s*\([^()]*\)\s*$", "", str(n or "")).strip())

    offenders: list[str] = []
    unparseable: list[str] = []       # a cap EXISTS but couldn't be bounded (no floor name / missing spans)
    checked = 0
    opt73_seen = 0                    # OPT73 cluster levers present at all (bounded or not)
    for f in _as_list(data.get("findings")):
        f = _as_dict(f)
        if str(f.get("pattern")) != "OPT73":
            continue
        opt73_seen += 1
        sibs = {_base(j) for j in _as_list(f.get("affected_jobs")) if str(j).strip()}
        if not sibs:
            continue
        for d in _as_list(f.get("wall_clock_derivation")):
            d = _as_dict(d)
            if d.get("bound") != "measured-critical-path":
                continue
            # The measured floor cap names the gating floor check in backticks, and records the
            # from_s/to_s spans it clamped between. If ANY of those is absent we bounded NOTHING —
            # count it as uncheckable (a loud coverage gap), never a silent non-offender PASS.
            floor_names = {_base(m) for m in _BACKTICK_NAME_RE.findall(str(d.get("reason") or ""))}
            from_s, to_s = _num(d.get("from_s")), _num(d.get("to_s"))
            if not floor_names or from_s is None or to_s is None:
                unparseable.append(f"OPT73 `{f.get('workflow_file', '?')}` "
                                   f"(reason={str(d.get('reason') or '')[:60]!r})")
                continue
            checked += 1
            # If the gating floor is one of the finding's OWN sibling legs, and the cap actually
            # LOWERED the win (to_s < from_s), a sibling capped the cluster lever (#44).
            hit = floor_names & sibs
            if hit and to_s < from_s:
                offenders.append(
                    f"OPT73 `{f.get('workflow_file', '?')}`: wall-clock capped from "
                    f"{from_s}s to {to_s}s by sibling leg "
                    f"`{sorted(hit)[0]}` (a matrix leg that descends WITH the fix)")
    if offenders:
        return Check(name, False,
                     "a cluster-floor lever's wall-clock was capped by one of its OWN concurrent matrix "
                     "sibling legs — but the shared-step fix lowers every sibling in lockstep, so the "
                     "ceiling must floor at the slowest NON-sibling check instead (#44): "
                     + "; ".join(offenders[:3]))
    if unparseable and checked == 0:
        # A measured-critical-path cap exists on an OPT73 lever but its floor check / spans could not
        # be recovered — a "couldn't check," not a clean pass (L8: never read green on an unbounded cap).
        return Check(name, True,
                     f"{len(unparseable)} OPT73 cluster-lever cap(s) could NOT be bounded — no floor "
                     f"check name or missing from/to spans in the derivation: "
                     + "; ".join(unparseable[:3]) + ". Coverage gap, not a clean pass.", skipped=True)
    if checked == 0:
        # L8 (#49): a plain "nothing to check" here read CLEAN on every real report even when
        # OPT73 cluster levers WERE present — the exact fail-open the third SKIP-reads-clean
        # instance this week traces to. When OPT73 levers exist but none carried a bounded
        # measured-critical-path cap, say so LOUDLY and NARROWLY (name the count + what could not
        # be checked); only a run with NO cluster lever at all is a quiet skip.
        if opt73_seen:
            return Check(name, True,
                         f"{opt73_seen} OPT73 cluster lever(s) present but none carried a bounded "
                         "measured-critical-path cap to check — cannot confirm the ceiling escaped its "
                         "own sibling leg. Coverage gap, not a clean pass.", skipped=True)
        return Check(name, True, "no OPT73 cluster-floor lever in this run", skipped=True)
    if unparseable:
        # Some caps were checked AND some could not be bounded — the checked ones passed, but the
        # unbounded ones are a coverage gap that must NOT vanish into a clean PASS (L8: an unchecked
        # cap never reads green just because a sibling cap was checkable).
        return Check(name, True,
                     f"{checked} cluster-floor lever cap(s) floor at a non-sibling check, but "
                     f"{len(unparseable)} could NOT be bounded (no floor check name / missing spans): "
                     + "; ".join(unparseable[:3]) + ". Partial coverage, not a clean pass.", skipped=True)
    return Check(name, True, f"{checked} cluster-floor lever cap(s) floor at a non-sibling check")


_HEADLINE_WIN_RE = re.compile(r"\*\*~\s*([0-9hms  ]+?)\*\*")


def check_headline_consumes_stamped_cluster_ceiling(report: str, findings_path: Path | None) -> Check:
    """**Bottom line leads with the stamped cluster ceiling — no burial (#49, the burial invariant).**
    A credited cluster-floor lever (OPT73 with `cluster_floor_lever` stamped AND a positive
    `wall_clock_p50_s`) cuts a step shared across sibling legs — its fix saves its FULL stamped
    ceiling of merge wait (a concurrent matrix cluster drops in lockstep; a `needs:`-chained
    sequential cluster compounds per stage). The per-pole sibling-capped arithmetic that drives the
    Bottom line can't reach that (it floors at the next
    sibling leg), so the lever gets buried in "Also noticed" while the headline shows a tiny per-leg
    number (mastodon: ~36s over a stamped 627s; electron: ~5m37s over 2635s — the ~17x/~8x
    understatements this issue names). Re-derives the max credited cluster ceiling from findings.json
    and FAILs when the rendered Bottom-line lever is STRICTLY smaller than it — the ONE invariant that
    catches BOTH live reports. LOUD narrow SKIP when OPT73 findings exist but NONE carries the persisted
    `cluster_floor_lever` marker (a legacy artifact predating the #49 stamp — burial can't be verified),
    so it can never read clean on a real cluster report. Standalone; keys on
    `findings[].cluster_floor_lever` / `wall_clock_p50_s` and the rendered `~<clock>`."""
    name = "Bottom-line headline leads with the stamped cluster-floor ceiling (no burial)"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    findings = [_as_dict(f) for f in _as_list(data.get("findings"))]
    opt73 = [f for f in findings if str(f.get("pattern")) == "OPT73"]
    flagged = [f for f in findings if f.get("cluster_floor_lever") is True]
    if not opt73 and not flagged:
        return Check(name, True, "no cluster-floor lever (OPT73) in this run", skipped=True)
    if not flagged:
        # OPT73 present but the persisted marker is absent — a legacy artifact predating the #49
        # stamp. We can't tell which OPT73 is a CREDITED cluster lever, so we can't confirm the
        # headline consumed it. LOUD narrow skip (L8: never read clean on an unverifiable burial).
        return Check(name, True,
                     f"{len(opt73)} OPT73 cluster lever(s) present but NONE carries the persisted "
                     "`cluster_floor_lever` marker — a legacy artifact predating the #49 stamp; the "
                     "burial invariant cannot be verified. Coverage gap, not a clean pass.",
                     skipped=True)
    # Issue #114: an OFF-SPINE cluster (jobs dropped from the merge-gating spine) is NOT a headline
    # ceiling — it SHOULD be demoted to Also-noticed/bill, so the burial invariant (which forbids
    # UNDER-crediting a real ceiling) must not apply to it, or it would contradict the on-spine crown
    # fix (the renderer's `_is_credited_cluster_lever` excludes off_spine identically). `off_spine`
    # is a stamped producer fact, not a renderer proxy.
    credited = [(f, _num(f.get("wall_clock_p50_s")) or 0.0) for f in flagged
                if (_num(f.get("wall_clock_p50_s")) or 0.0) > 0.0 and not f.get("off_spine")]
    if not credited:
        return Check(name, True,
                     f"{len(flagged)} stamped cluster lever(s), none credited on-spine developer "
                     "wall-clock (all bill-only/off-path/off-spine) — nothing to headline")
    best_f, best_s = max(credited, key=lambda t: t[1])
    wf = best_f.get("workflow_file", "?")
    # The rendered Bottom line names the merge wait in bold WITHOUT a tilde and the addressable win in
    # bold WITH a leading `~`. Parse the first `~<clock>` in the Bottom-line paragraph.
    bl = next((ln for ln in report.splitlines() if "**Bottom line.**" in ln), "")
    m = _HEADLINE_WIN_RE.search(bl)
    rendered_s = _parse_clock_to_s(m.group(1)) if m else None
    if rendered_s is None:
        # The Bottom line renders NO addressable `~<clock>` win at all — a deliberate
        # no-single-win framing (competing-path chain / PR-floor / tiny-lever fallback),
        # NOT the "Also noticed" burial this guard targets (the live defect rendered a
        # SMALLER `~<clock>`, caught below). A MAX credited cluster ceiling would have
        # fired the render's cluster branch and emitted its clock, so a None here means
        # this lever is not the dominant win — a FAIL would be spurious. Can't confirm the
        # burial invariant either way → LOUD SKIP (L8: never read clean), never a spurious
        # gate red on a legitimate no-headline report.
        return Check(name, True,
                     f"a credited cluster-floor lever (OPT73 `{wf}`, ~{_fmt_clock(best_s)}) exists but "
                     "the Bottom line renders no addressable `~<clock>` win (a no-single-win framing) — "
                     "cannot confirm it leads with the stamped ceiling. Coverage gap, not a clean pass.",
                     skipped=True)
    # Rounding tolerance: `_clock` renders to whole seconds, so the parse is exact to ~1s; allow 2s.
    if rendered_s + 2.0 < best_s:
        return Check(name, False,
                     f"the rendered Bottom-line lever (~{_fmt_clock(rendered_s)}) is smaller than the "
                     f"stamped credited cluster ceiling (~{_fmt_clock(best_s)}, OPT73 `{wf}`) — the "
                     "cluster-floor lever is BURIED; the headline must consume the stamped per-finding "
                     "ceiling, not a sibling-capped per-leg headroom (#49)")
    return Check(name, True,
                 f"Bottom line leads with ~{_fmt_clock(rendered_s)} >= the stamped cluster ceiling "
                 f"~{_fmt_clock(best_s)} (OPT73 `{wf}`)")


def check_headline_lever_is_presence_eligible(report: str, findings_path: Path | None) -> Check:
    """**The crowned headline lever is presence-eligible — the CONVERSE of the burial guard (#56).**
    The burial guard above forbids UNDER-crediting a real cluster ceiling; this forbids the opposite
    error — a MINORITY-present cluster crowning the typical-PR bottom line. When the Bottom line leads
    with a credited cluster-floor lever (OPT73), that lever's WORKFLOW must actually gate the merge on
    a MAJORITY of sampled PRs. Re-derives the crowned lever's workflow gate frequency the SAME way the
    renderer's `_toc_block` does — summing `checks[].pole_n` over every check in that workflow (one
    check is the per-PR slowest, so the sum is the workflow's gate count) — and FAILs when it gates
    only a MINORITY (`wf_pole <= n * _VR_RARE_PRESENCE_FRAC`, `n >= _VR_RARE_PRESENCE_MIN_PR`) while a
    typical-PR ceiling (pole 1's own win) is the honest headline. This is the check that would have
    caught the playwright sample: `tests_secondary.yml` gates 2/20 yet its OPT73 crowned the bottom
    line at ~3m 15s and self-labeled 'sits ON the merge-gating critical path'. Standalone; re-derives
    from `pr_critical_path.checks[].pole_n` / `workflow_file` (never a renderer proxy) and the rendered
    `~<clock>`. A required check in the workflow gates by definition and EXEMPTS it (never a false FAIL
    on a required gate). LOUD narrow SKIP when the crown can't be re-derived (no stamped `pole_n`, or a
    below-floor sample), so it never reads clean on an unverifiable crown."""
    name = "Bottom-line crowned cluster lever is presence-eligible (no minority-workflow crown)"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    findings = [_as_dict(f) for f in _as_list(data.get("findings"))]
    opt73 = [f for f in findings if str(f.get("pattern")) == "OPT73"]
    flagged = [f for f in findings if f.get("cluster_floor_lever") is True]
    if not opt73 and not flagged:
        return Check(name, True, "no cluster-floor lever (OPT73) in this run", skipped=True)
    # Issue #114: an off-spine cluster is demoted by construction, so it is never the crown this
    # presence guard judges — exclude it (the dedicated `check_headline_cluster_lever_on_spine` owns
    # the off-spine crown). `off_spine` is a stamped producer fact, mirroring the renderer.
    credited = [(f, _num(f.get("wall_clock_p50_s")) or 0.0) for f in flagged
                if (_num(f.get("wall_clock_p50_s")) or 0.0) > 0.0 and not f.get("off_spine")]
    if not credited:
        # No on-spine wall-clock-credited cluster lever, so there is no crown to presence-check — the
        # presence-weighting demotion already floored any minority cluster to bill-only. Nothing
        # was verified here, so this must NOT read as a clean PASS: LOUD narrow SKIP naming what
        # couldn't be checked, matching the sibling burial guard's coverage-gap skips (L8: a check
        # that asserts nothing never reads clean).
        return Check(name, True,
                     f"{len(flagged)} stamped cluster lever(s), none credited on-spine developer "
                     "wall-clock (all bill-only/off-path/off-spine) — no crowned lever to "
                     "presence-check (any minority/off-spine cluster is already demoted to "
                     "bill-only). Coverage gap, not a clean pass.",
                     skipped=True)
    best_f, best_s = max(credited, key=lambda t: t[1])
    wf = str(best_f.get("workflow_file", "") or "")
    # Confirm the Bottom line actually LEADS with this lever (a smaller rendered win means it was
    # buried — the sibling burial guard's target, not this one).
    bl = next((ln for ln in report.splitlines() if "**Bottom line.**" in ln), "")
    m = _HEADLINE_WIN_RE.search(bl)
    rendered_s = _parse_clock_to_s(m.group(1)) if m else None
    if rendered_s is None or rendered_s + 2.0 < best_s:
        return Check(name, True,
                     f"the credited cluster lever (OPT73 `{wf}`, ~{_fmt_clock(best_s)}) does not lead "
                     "the Bottom line (buried or no `~<clock>` win) — the crown to presence-check is "
                     "not this lever. Coverage gap, not a clean pass.", skipped=True)
    # Re-derive the crowned lever's WORKFLOW gate frequency from the stamped per-check `pole_n`
    # (mirror of `blocking_path._toc_block`'s `wf_gate`): sum over every check in this workflow.
    cp = _as_dict(data.get("pr_critical_path"))
    checks = [c for c in _as_list(cp.get("checks")) if isinstance(c, dict)]
    wf_checks = [c for c in checks if str(c.get("workflow_file", "") or "") == wf]
    if not wf_checks or any(c.get("pole_n") is None for c in wf_checks):
        # No stamped pole_n for this workflow's checks — a legacy artifact predating the stamp, or a
        # fileless-only crown. Can't re-derive the gate frequency → LOUD narrow skip (never clean).
        return Check(name, True,
                     f"crowned cluster lever (OPT73 `{wf}`, ~{_fmt_clock(best_s)}) but its workflow's "
                     "checks carry no stamped `pole_n` — the gate frequency cannot be re-derived. "
                     "Coverage gap, not a clean pass.", skipped=True)
    nfloor = int(cp.get("check_present_n_pr") or 0)
    if nfloor < _VR_RARE_PRESENCE_MIN_PR:
        return Check(name, True,
                     f"crowned cluster lever (OPT73 `{wf}`, ~{_fmt_clock(best_s)}) but only "
                     f"{nfloor} sampled PRs (< {_VR_RARE_PRESENCE_MIN_PR}) — the gate fraction is "
                     "noise, so minority-crown can't be judged. Coverage gap, not a clean pass.",
                     skipped=True)
    required = {str(c) for c in _as_list(data.get("required_checks"))}
    if any(str(c.get("name", "")) in required for c in wf_checks):
        return Check(name, True,
                     f"crowned cluster lever (OPT73 `{wf}`) has a REQUIRED check — the workflow gates "
                     "by definition, so its wall-clock crown is honest")
    wf_pole = sum(int(c.get("pole_n") or 0) for c in wf_checks)
    if wf_pole <= nfloor * _VR_RARE_PRESENCE_FRAC:
        return Check(name, False,
                     f"the Bottom line crowns a MINORITY-present cluster lever: OPT73 `{wf}` gates "
                     f"only {wf_pole}/{nfloor} sampled PRs (a typical PR never waits on it) yet its "
                     f"~{_fmt_clock(rendered_s)} crowns the typical-PR headline. A minority-present "
                     "cluster's wall-clock saving must be demoted to bill-only (the honest ceiling is "
                     "pole 1's own win); the crowned lever must be presence-eligible (#56)")
    return Check(name, True,
                 f"crowned cluster lever OPT73 `{wf}` gates {wf_pole}/{nfloor} sampled PRs (majority) "
                 f"— presence-eligible to crown ~{_fmt_clock(rendered_s)}")


def check_headline_cluster_lever_on_spine(report: str, findings_path: Path | None) -> Check:
    """**The crowned cluster lever is ON the merge-gating spine (#114) — the gating-path converse of
    the presence-eligible guard.** `check_headline_lever_is_presence_eligible` re-derives the crown's
    WORKFLOW gate frequency and exempts any workflow hosting a required check — but a workflow can host
    a required gate (home-assistant/core `ci.yaml`: `Check hassfest`) while the CROWNED cluster's own
    jobs (the 10-leg `Run pytest` matrix) were DROPPED from the required-scoped spine. Workflow-level
    required-hosting is NOT the cluster's own presence on the gating path, so that guard's exemption
    reads the off-spine pytest crown as honest. This guard closes it: a credited cluster-floor lever
    (OPT73, `cluster_floor_lever` + positive `wall_clock_p50_s`) that the Bottom line LEADS with must
    NOT be stamped `off_spine=True` (the producer fact `_stamp_off_spine_findings` sets from the spine's
    dropped/kept check sets). The stamped off-spine cluster can be the workflow's heaviest step yet
    never gate the merge — it is bill/throughput, not merge-wait — so crowning it as the typical-PR
    "biggest single measured win" (~9m08s over a 1m56s wall, 4.7x the whole wait) is the defect. Keys
    on `findings[].cluster_floor_lever` / `off_spine` / `wall_clock_p50_s` and the rendered first
    `~<clock>`. LOUD narrow SKIP when the max credited cluster does not lead (correctly demoted / no
    tilde-win), so it never reads clean on an unverifiable crown."""
    name = "Bottom-line crowned cluster lever is on the merge-gating spine (not off-spine)"
    if not findings_path:
        return Check(name, True, "no findings path", skipped=True)
    data, err = _load_findings_doc(findings_path)
    if err:
        return Check(name, True, err, skipped=True)
    findings = [_as_dict(f) for f in _as_list(data.get("findings"))]
    opt73 = [f for f in findings if str(f.get("pattern")) == "OPT73"]
    flagged = [f for f in findings if f.get("cluster_floor_lever") is True]
    if not opt73 and not flagged:
        return Check(name, True, "no cluster-floor lever (OPT73) in this run", skipped=True)
    # Re-derive the crown WITHOUT the off_spine exclusion (max credited cluster by wall-clock), so an
    # off-spine cluster that the OLD renderer crowned is caught, while the fixed renderer (which drops
    # off_spine before crowning) leaves a smaller/absent win here and this guard skips.
    credited = [(f, _num(f.get("wall_clock_p50_s")) or 0.0) for f in flagged
                if (_num(f.get("wall_clock_p50_s")) or 0.0) > 0.0]
    if not credited:
        return Check(name, True,
                     f"{len(flagged)} stamped cluster lever(s), none credited developer wall-clock "
                     "(all bill-only/off-path) — no crowned lever to spine-check. Coverage gap, not a "
                     "clean pass.", skipped=True)
    best_f, best_s = max(credited, key=lambda t: t[1])
    wf = str(best_f.get("workflow_file", "") or "")
    bl = next((ln for ln in report.splitlines() if "**Bottom line.**" in ln), "")
    m = _HEADLINE_WIN_RE.search(bl)
    rendered_s = _parse_clock_to_s(m.group(1)) if m else None
    if rendered_s is None or rendered_s + 2.0 < best_s:
        # The max credited cluster does not LEAD the Bottom line (buried / demoted / no tilde-win) —
        # the fixed renderer's off-spine drop, or a genuine burial (the sibling guard's target). Not
        # this guard's crown to judge → LOUD narrow skip (L8: never read clean on an unverifiable crown).
        return Check(name, True,
                     f"the max credited cluster lever (OPT73 `{wf}`, ~{_fmt_clock(best_s)}) does not "
                     "lead the Bottom line (demoted/buried or no `~<clock>` win) — no off-spine crown "
                     "to catch. Coverage gap, not a clean pass.", skipped=True)
    if best_f.get("off_spine") is True:
        return Check(name, False,
                     f"the Bottom line crowns an OFF-SPINE cluster lever: OPT73 `{wf}` "
                     f"(~{_fmt_clock(rendered_s)}) is stamped `off_spine=True` — its jobs were dropped "
                     "from the merge-gating spine (the workflow gates via a DIFFERENT required check), "
                     "so its wall-clock is bill/throughput, not merge-wait. An off-spine cluster must "
                     "never crown the typical-PR headline; workflow-level required-hosting is not the "
                     "cluster's own presence on the gating path (#114, the home-assistant/core shape)")
    return Check(name, True,
                 f"crowned cluster lever OPT73 `{wf}` (~{_fmt_clock(rendered_s)}) is on the gating "
                 "spine (not stamped off_spine)")


# Structural speed-up patterns — the "decompose/shard this measured step" route. The class invariant
# below (the OPT75-fabrication class, mixpanel `Validate PR title`) generalizes the admission gate the
# engine's own collect_runs comment states: a FILE-BACKED check that routes one of these MUST carry a
# measured dominant step. Inferring a structural lever from the check NAME alone (no sampled step) is the
# infer-root-cause-from-a-bare-name anti-pattern OPT49/OPT51 were cut for.
_STRUCTURAL_PATTERNS = frozenset({"OPT70", "OPT72", "OPT75"})
# File-backed ⇔ workflow_file names a real in-repo workflow (a `.yml`/`.yaml` path). A GENUINELY-FILELESS
# check (a default setup / external app / bot) carries a bare check/app name there ("CodeQL", "Analyze")
# — there is no in-repo file to decompose, so a name-routed qualitative hand-off is honest and EXEMPT.
def _wf_is_file_backed(wf) -> bool:
    """File-backed ⇔ `workflow_file` names a real in-repo workflow (a `.yml`/`.yaml` path). MUST stay
    textually in sync with the engine-side drop in `collect_runs._detect_structural_candidates`
    (`_wf.lower().endswith((".yml", ".yaml"))`) — the producer that DROPS the fabrication and this
    checker that FLAGS it must agree on file-backedness, or a freshly-generated report could fail its
    own invariant. A genuinely-fileless check carries a bare app/check name here (CodeQL, Analyze)."""
    return str(wf or "").strip().lower().endswith((".yml", ".yaml"))


def check_structural_pole_has_measured_step(findings_path: Path | None) -> Check:
    """**The OPT75-fabrication class (Phase 0).** A FILE-BACKED structural lever (OPT70/72/75) must
    carry a MEASURED dominant step — `wall_clock_p50_s` is not None. A file-backed check whose steps
    weren't sampled (triaged-fast) has NO measured step, so routing a structural decomposition lever
    for it infers the cost category from the check NAME alone — the admission-gate violation the
    engine's own collect_runs sibling-branch refuses (and OPT49/OPT51 were cut for). Genuinely-fileless
    checks (workflow_file is a bare app/check name, not a `.yml` path) are exempt: there is no in-repo
    file to decompose, so a name-routed qualitative hand-off is honest, not a fabricated file-backed
    lever. Quantified PROPERTY over the findings JSON: file-backedness is re-derived from `workflow_file`
    (not a renderer proxy); the "has a measured step" half reads the engine-emitted `decomposition` /
    `measured_evidence` fields (a builder signal, not a re-derivation from the pole's sampled-job
    timings — a known limit: a regression that attached a bogus `decomposition` would be exempted). So a
    new repo whose triaged-fast pole would fabricate an OPT75 is
    caught on every future report — the class-wide guard, not a point-fix on `Validate PR title`."""
    name = "every file-backed structural lever carries a measured dominant step (no name-inferred OPT75)"
    if not findings_path:
        return Check(name, True, "no --findings to inspect", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    findings = _as_list(_as_dict(data).get("findings"))
    structural = [f for f in findings   # str(): an unhashable (list/dict) `pattern` must not crash the `in` test
                  if isinstance(f, dict) and str(f.get("pattern")) in _STRUCTURAL_PATTERNS]
    if not structural:
        return Check(name, True, "no structural (OPT70/72/75) findings in this run", skipped=True)
    # Offender ⇔ file-backed AND unsized (wall_clock_p50_s is None) AND NO measured step. The
    # measured-step signal is `decomposition` or `measured_evidence`: a file-backed structural lever
    # WITH a measured step but wall_clock None is a LEGITIMATE bill-only finding (its addressable
    # wall-clock floored to ~0 because a slower concurrent check gates) — NOT a name-inferred lever,
    # so it is exempt. Only the name-inferred ones (no decomposition, no measured evidence) violate
    # the admission gate. (Corpus: catches mixpanel `Validate PR title`, apple `Analyze PR for
    # labeling`, rootly `Docs Drift`; exempts rootly `Build`, apple `merge-build`/`pr-build`.)
    offenders = [
        f"{f.get('pattern')} `{_strip_scope(str(f.get('title') or f.get('check') or f.get('workflow_file') or ''))}` "
        f"(file `{f.get('workflow_file')}`, no measured step)"
        for f in structural
        if (_wf_is_file_backed(f.get("workflow_file"))
            and f.get("wall_clock_p50_s") is None
            and not f.get("decomposition") and not f.get("measured_evidence"))
    ]
    return Check(name, not offenders,
                 f"all {len(structural)} structural lever(s) file-backed-with-measured-step or fileless"
                 if not offenders else
                 "file-backed structural lever(s) with NO measured dominant step — a name-inferred OPT75 "
                 "the admission gate forbids (file-backed + steps not sampled ⇒ no structural lever): "
                 + "; ".join(offenders))


# --- The payload-binned-as-build class (nrwl/nx `Run Checks/Lint/Test/Build`) --------
# A COMBINED step whose name carries a PAYLOAD token (test/lint/spec/e2e/…) is the work
# the job exists for, even when the name ALSO carries the broad `build` token. Binning it
# as `build` (a setup/redundant category) inflates the redundant-work ratio (setup+build ÷
# payload) and misroutes the pole onto OPT72 "warm the build cache" when the step's time is
# test execution. `_VR_BUILD_CATEGORY` / `_VR_PAYLOAD_TOKEN_RE` are standalone `_VR_*` copies
# that keep verify_report import-free; the coupling test in test_verify_report_self.py pins
# them to the engine's `_SETUP_BUILD_CATEGORIES` membership + `_STEP_CATEGORY_RES` payload
# tokens so a category rename / token drop breaks a test, not the check silently.
_VR_BUILD_CATEGORY = "build"
# High-confidence PAYLOAD tokens: a genuine build step never carries these. Deliberately a
# CONSERVATIVE subset of the engine's `test`/`scan` regexes (mirrors the strongest literals,
# L7) so re-deriving "this build-labelled step clearly runs tests/lint" stays false-positive-
# free. `unittest` is a bare substring (glued token, no `\b`), mirroring the engine.
_VR_PAYLOAD_TOKEN_RE = re.compile(
    r"\btest\b|\btests\b|\bspec\b|\be2e\b|unittest|pytest|jest|vitest|"
    r"playwright|cypress|nextest|rspec|phpunit|\blint\b|eslint",
    re.IGNORECASE)


def check_structural_step_category_not_payload_binned_as_build(findings_path: Path | None) -> Check:
    """**The payload-binned-as-build class (nrwl/nx).** A structural finding's `decomposition`
    must not crown a `build`-category dominant step whose NAME clearly runs PAYLOAD work
    (test/lint/spec/e2e/…). The engine's fine-grained classifier bins each step into ONE
    category; when a single combined step (`nx affected` → "Run Checks/Lint/Test/Build", with
    `playwright test` inside) is binned as `build`, its whole duration lands in the redundant-
    work numerator (setup+build), inflating the ratio past 2.0 and routing the pole to OPT72
    ("warm the build cache") even though the step's time is test execution — the pole's own
    guardrail then steers at the wrong lever. RE-DERIVED PROPERTY over the findings JSON: the
    dominant step's category is read from the engine-emitted `decomposition.dominant_category`;
    the "this is really payload" half is re-derived INDEPENDENTLY from `decomposition.dominant_
    step` via `_VR_PAYLOAD_TOKEN_RE` (a standalone token set, NOT a call into the engine's
    classifier — so a classifier regression is caught, not masked). Scoped to `build` (not the
    whole setup/build set): `checkout`/`install` carry their own more-specific leading signals
    that legitimately win over a trailing payload word, and `build` is the exact category the
    OPT72 redundant-work route keys on. So any future report that bins a payload-bearing step as
    `build` is caught — the class-wide guard, not a point-fix on the nx step name."""
    name = ("no payload-bearing step is binned as `build` (redundant-work inflation → OPT72 "
            "misroute)")
    if not findings_path:
        return Check(name, True, "no --findings to inspect", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check(name, True, "findings unreadable", skipped=True)
    findings = _as_list(_as_dict(data).get("findings"))
    checked = 0
    offenders = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        decomp = f.get("decomposition")
        if not isinstance(decomp, dict):
            continue
        dom_cat = decomp.get("dominant_category")
        dom_step = decomp.get("dominant_step")
        if not isinstance(dom_cat, str) or not isinstance(dom_step, str):
            continue
        checked += 1
        if dom_cat == _VR_BUILD_CATEGORY and _VR_PAYLOAD_TOKEN_RE.search(dom_step):
            offenders.append(
                f"{f.get('pattern')} `{_strip_scope(str(f.get('title') or f.get('check') or ''))}` "
                f"dominant step `{dom_step}` binned `build` but its name runs payload work")
    if checked == 0:
        return Check(name, True, "no decomposition-bearing structural finding in this run",
                     skipped=True)
    return Check(name, not offenders,
                 f"all {checked} decomposition(s) classify a payload-bearing dominant step as payload"
                 if not offenders else
                 "a payload-bearing dominant step is binned as `build` — its duration inflates the "
                 "redundant-work ratio (setup+build ÷ payload) and misroutes the pole onto OPT72 "
                 "'warm the build cache' when the step is running tests: " + "; ".join(offenders))


# --- The off-category leaf-hijack class (issue #16) ---------------------------------
# A whole-log `_parse_log` leaf (eslint-no-cache, …) fires on a tool marker ANYWHERE in the
# joined job log with no dominant-step check, and would crown a pole's MEASURED CAUSE +
# claim its full wall-clock ceiling even when the tool's category is a MINORITY of the
# measured time (nrwl/nx: an eslint `scan` leaf crowning a `test`-dominant combined
# `Run Checks/Lint/Test/Build` step). The engine now DEMOTES such a leaf; this invariant is
# the independent net. RE-DERIVATION BASIS: the crowned leaf's `fix_key` is read from the
# per-pole `<!-- ci-speedup:leaf-crown fix_key=… -->` marker in the RENDERED report (L1: the
# claim under test lives in the text); its category is re-derived from the fix_key via the
# standalone `_VR_LEAF_STEP_CATEGORY` mirror (never a trusted stamped category enum); the
# GROUND TRUTH `dominant_category` is sourced from findings.json's pole decomposition
# (`pr_critical_path.poles`, keyed by (wf_base, cmp check) exactly as the cache class maps
# poles — L3). A crowned leaf whose category ≠ the pole's `dominant_category` FAILS: the
# ceiling it claims (the pole's dominant_p50) is credited to a category that is not the
# dominant measured work. The `_VR_LEAF_STEP_CATEGORY` mirror is pinned to the engine map by
# test_verify_report_self (L7).
_VR_LEAF_STEP_CATEGORY: dict[str, str] = {
    "prisma-migrate-once": "test",
    "vitest-v8-coverage": "test",
    "vitest-isolate-pool": "test",
    "playwright-parallel": "test",
    "pytest-no-xdist": "test",
    "cargo-test-shard": "test",
    "benchmark-serial-reruns": "test",
    "android-emulator-shard": "test",
    "gradle-test-parallelism": "test",
    "eslint-no-cache": "scan",
    "turbo-remote-cache": "build",
    "turbo-partial-cache": "build",
    "buildx-no-cache": "build",
    "install-lifecycle-build": "install",
}
_VR_LEAF_CROWN_RE = re.compile(r"<!--\s*ci-speedup:leaf-crown\s+fix_key=(\S+)\s*-->")


def _pole_dominant_categories(findings_path: Path | None) -> dict[tuple[str, str], str] | None:
    """`{(wf_base, cmp check): dominant_category}` for every pole in findings that carries a
    decomposition. None when findings are unavailable/unreadable (the check SKIPs)."""
    if not findings_path:
        return None
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    out: dict[tuple[str, str], str] = {}
    for p in _as_list(_as_dict(data.get("pr_critical_path")).get("poles")):
        if not isinstance(p, dict):
            continue
        dom_cat = p.get("dominant_category")
        if isinstance(dom_cat, str) and dom_cat:
            out[(_wf_base(str(p.get("workflow_file") or "")),
                 _cmp_name(str(p.get("check") or "")))] = dom_cat
    return out


def check_detector_leaf_agrees_with_dominant_category(report: str,
                                                      findings_path: Path | None) -> Check:
    """**The off-category leaf-hijack class (issue #16).** A rendered pole's crowned MEASURED
    CAUSE (a `_parse_log` leaf) must address the pole's DOMINANT measured step category, so
    its credited wall-clock ceiling isn't pinned on a minority of the measured time. For each
    pole section carrying a `<!-- ci-speedup:leaf-crown fix_key=… -->` marker, the leaf's
    category is re-derived from the fix_key (`_VR_LEAF_STEP_CATEGORY`) and compared to the
    pole's `dominant_category` read from findings.json — a mismatch (e.g. an `eslint-no-cache`
    `scan` leaf crowning a `test`-dominant combined step) FAILS. Under the engine's demote
    design a crowned leaf always agrees, so a mismatch means the demotion regressed and the
    ceiling is credited to non-dominant work."""
    name = ("crowned detector leaf agrees with the pole's dominant measured category "
            "(no off-category ceiling)")
    if findings_path is None:
        return Check(name, True, "no --findings to cross-check leaf category", skipped=True)
    dom_by_pole = _pole_dominant_categories(findings_path)
    if dom_by_pole is None:
        return Check(name, True, "findings unreadable", skipped=True)
    bad: list[str] = []
    checked = 0
    unknown_cat: list[str] = []
    unmapped: list[str] = []
    for wf, check, body in _pole_header_sections(report):
        m = _VR_LEAF_CROWN_RE.search(body)
        if not m:
            continue
        fk = m.group(1)
        leaf_cat = _VR_LEAF_STEP_CATEGORY.get(fk)
        if leaf_cat is None:
            unknown_cat.append(f"`{wf}` ▸ {check} (fix_key {fk})")
            continue
        dom_cat = dom_by_pole.get((_wf_base(_strip_line_suffix(wf)), _cmp_name(check)))
        if dom_cat is None:
            unmapped.append(f"`{wf}` ▸ {check}")
            continue
        checked += 1
        if leaf_cat != dom_cat:
            bad.append(f"`{wf}` ▸ {check}: crowned `{fk}` is a `{leaf_cat}` fix but the pole's "
                       f"measured dominant category is `{dom_cat}` — the ceiling is credited to "
                       "non-dominant work")
    # L8: surface every coverage skip in the detail rather than reading clean.
    skips = ""
    if unknown_cat:
        skips += f"; {len(unknown_cat)} crown marker(s) with an unmapped fix_key: " \
                 + "; ".join(unknown_cat[:3])
    if unmapped:
        skips += f"; {len(unmapped)} crowned pole(s) not matched to a findings decomposition: " \
                 + "; ".join(unmapped[:3])
    if bad:
        return Check(name, False,
                     "a crowned detector leaf contradicts the pole's dominant measured "
                     "category (off-category ceiling — issue #16): " + "; ".join(bad[:3]) + skips)
    if checked == 0:
        return Check(name, True,
                     "no crowned detector-leaf pole to cross-check" + (skips or ""), skipped=True)
    return Check(name, True,
                 f"all {checked} crowned detector leaf(s) agree with their pole's dominant "
                 "category" + skips)


# --- The cache-hit-rate class -------------------------------------------------------
# A cache-miss finding (turbo cold/partial, buildx cold, install-lifecycle build) is born
# from ONE drilled run's log. The engine now stamps a cross-run, event-split, fork-aware
# miss-rate distribution (`poles[*].cache_dist`) with a re-derivable `verdict`, and the
# renderer demotes a mostly-warm cache off its "BIGGEST LEVER" / "cache-key churn" framing.
# These two checks re-derive the verdict from the RAW stored values (never the stamped
# enum — L1) and fail a report whose framing contradicts the measured hit rate. Standalone
# `_VR_*` copies keep verify_report import-free; a coupling test pins them to the engine.
_VR_CACHE_CONTEXT_MARKER = "<!-- ci-speedup:cache-context -->"
_VR_PARTIAL_MISS_FLOOR_PCT = 40.0
_VR_CACHE_TAIL_MIN_FRAC = 0.25
_VR_CACHE_COLD_MISS_PCT = 99.5
_VR_BIGGEST_LEVER = "BIGGEST LEVER"
# A rendered pole-section line that ASSERTS a cache-miss / churn root cause — the claim
# whose framing must match the measured distribution. Each literal is pinned to a
# blocking_path render string by the coupling test.
_VR_CACHE_CLAIM_RES = [re.compile(p) for p in (
    r"cache-key churn",
    r"rebuilt despite caching ON",
    r"packages rebuilt from scratch \(cache-miss\)",
    r"Remote caching DISABLED",
    r"rebuilds \d+ layers from scratch",
    r"build work runs during dependency install",
)]


def _vr_cache_verdict(cache_dist: dict) -> str:
    """Re-derive a cache pole's verdict from its stamped RAW distribution — the exact
    mirror of `collect_runs._cache_verdict` (L4/L5: same miss metric = per-run
    `cache_state.miss_pct`, fork-excluded upstream median; same cold/tail rules). Used to
    catch a report whose framing (or its own stamped `verdict`) contradicts the values."""
    cd = _as_dict(cache_dist)
    pr_values = _as_list(_as_dict(cd.get("pr")).get("values"))
    tail = _as_dict(cd.get("tail"))

    def _miss(v: dict) -> float | None:
        st = v.get("cache_state") if isinstance(v.get("cache_state"), dict) else {}
        m = st.get("miss_pct")
        return float(m) if isinstance(m, (int, float)) else None

    upstream = [v for v in pr_values
                if isinstance(v, dict) and not v.get("fork") and _miss(v) is not None]
    if len(upstream) < 2:
        return "insufficient"

    def _is_cold(v: dict) -> bool:
        st = v.get("cache_state") if isinstance(v.get("cache_state"), dict) else {}
        return bool(st.get("remote_off")) or bool(st.get("cold")) \
            or (_miss(v) or 0.0) >= _VR_CACHE_COLD_MISS_PCT

    if all(_is_cold(v) for v in upstream):
        return "cold"
    med = statistics.median([_miss(v) for v in upstream])
    if med >= _VR_PARTIAL_MISS_FLOOR_PCT:
        return "churn"
    prev = tail.get("prevalence_max")
    if isinstance(prev, (int, float)) and prev >= _VR_CACHE_TAIL_MIN_FRAC:
        return "miss-tail"
    return "mostly-warm"


def _pole_cache_dists(findings_path: Path | None) -> tuple[dict[tuple[str, str], dict] | None, bool]:
    """`{(wf_base, cmp check): cache_dist}` for every pole carrying one, plus whether the
    findings carry the `data_sources.cache_dist_probe` schema stamp. `(None, False)` when
    findings are unavailable/unreadable — so the checks SKIP rather than crash."""
    if not findings_path:
        return None, False
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None, False
    stamped = "cache_dist_probe" in _as_dict(data.get("data_sources"))
    out: dict[tuple[str, str], dict] = {}
    for p in _as_list(_as_dict(data.get("pr_critical_path")).get("poles")):
        if isinstance(p, dict) and isinstance(p.get("cache_dist"), dict):
            out[(_wf_base(str(p.get("workflow_file") or "")),
                 _cmp_name(str(p.get("check") or "")))] = p["cache_dist"]
    return out, stamped


def check_cache_claim_backed_by_distribution(report: str, findings_path: Path | None) -> Check:
    """Every rendered cache root-cause CLAIM (a pole section asserting a cache miss / churn /
    cold-rebuild) must map to a pole carrying a cross-run `cache_dist` — so the framing rests
    on a distribution, not the single drilled run. SKIPs on pre-`cache_dist` findings (no
    `data_sources.cache_dist_probe`) so committed worked examples stay green until regenerated."""
    name = "every rendered cache claim carries a cross-run cache distribution"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    dists, stamped = _pole_cache_dists(findings_path)
    if dists is None:
        return Check(name, True, "findings unreadable", skipped=True)
    if not stamped:
        return Check(name, True, "findings predate the cache-distribution probe "
                     "(no data_sources.cache_dist_probe) — distribution guard not applicable",
                     skipped=True)
    missing, n = [], 0
    for wf, check, body in _pole_header_sections(report):
        if not any(rx.search(body) for rx in _VR_CACHE_CLAIM_RES):
            continue
        n += 1
        if (_wf_base(_strip_line_suffix(wf)), _cmp_name(check)) not in dists:
            missing.append(f"`{wf}` ▸ {check}")
    if missing:
        return Check(name, False,
                     "cache root-cause claim(s) rendered with no cross-run cache distribution "
                     "(cache_dist) to back them: " + "; ".join(missing))
    if n == 0:
        return Check(name, True, "no rendered cache root-cause claim in this report", skipped=True)
    return Check(name, True, f"all {n} rendered cache claim(s) carry a cross-run distribution")


def check_cache_framing_matches_distribution(report: str, findings_path: Path | None) -> Check:
    """A cache pole's rendered framing must match its MEASURED hit-rate distribution. For each
    pole carrying `cache_dist`: (1) the stamped `verdict` must equal the verdict RE-DERIVED
    from its own raw values — a stamped `cold` over warm values fails here, so the exemption
    can't be widened by a buggy/adversarial engine; (2) a re-derived miss-tail / mostly-warm /
    insufficient pole must NOT still label a cache claim `BIGGEST LEVER` or frame it as
    `cache-key churn` — miss-tail/mostly-warm additionally MUST carry the cache-context marker
    (they have a measured distribution to size against), while `insufficient` (fewer than 2
    upstream runs exposed a summary) has no distribution and instead discloses a single-run
    basis in the note, so it is exempt from the marker but NOT from the over-claim ban. The
    broad claim is exempt ONLY when the re-derived verdict is `cold`/`churn` (L2: only the
    framing/caveat contradiction is flagged — the drill, evidence, magnitude, and prompt still
    render)."""
    name = "cache finding's framing matches its measured hit-rate distribution"
    if findings_path is None:
        return Check(name, True, "no --findings to inspect", skipped=True)
    dists, stamped = _pole_cache_dists(findings_path)
    if dists is None:
        return Check(name, True, "findings unreadable", skipped=True)
    if not stamped:
        return Check(name, True, "findings predate the cache-distribution probe "
                     "(no data_sources.cache_dist_probe) — framing guard not applicable",
                     skipped=True)
    bad, checked = [], 0
    for wf, check, body in _pole_header_sections(report):
        cd = dists.get((_wf_base(_strip_line_suffix(wf)), _cmp_name(check)))
        if cd is None:
            continue
        checked += 1
        stamped_v = cd.get("verdict")
        rederived = _vr_cache_verdict(cd)
        if stamped_v != rederived:
            bad.append(f"`{wf}` ▸ {check}: engine stamped verdict {stamped_v!r} but its own "
                       f"values re-derive {rederived!r}")
            continue
        if rederived in ("miss-tail", "mostly-warm", "insufficient"):
            problems = []
            if any(_VR_BIGGEST_LEVER in ln and any(rx.search(ln) for rx in _VR_CACHE_CLAIM_RES)
                   for ln in body.splitlines()):
                problems.append("still labels a cache claim BIGGEST LEVER")
            if re.search(r"cache-key churn", body):
                problems.append("still frames it as cache-key churn")
            # miss-tail / mostly-warm carry a measured distribution, so they MUST show the
            # cache-context marker. `insufficient` has no cross-run distribution to size against
            # (fewer than 2 upstream runs) — it discloses via a single-run-basis caveat in the
            # note instead, so it is exempt from the marker but still banned from the over-claim.
            if rederived != "insufficient" and _VR_CACHE_CONTEXT_MARKER not in body:
                problems.append("missing the cache-context caveat marker")
            if problems:
                bad.append(f"`{wf}` ▸ {check} (measured {rederived}): " + ", ".join(problems))
    if bad:
        return Check(name, False,
                     "cache pole framing contradicts its measured distribution: " + "; ".join(bad))
    if checked == 0:
        return Check(name, True, "no cache pole with a distribution in this report", skipped=True)
    return Check(name, True, f"all {checked} cache pole(s) framed to match their distribution")


# --- The phantom-gate class (headline ranks a check that never actually gates) ------------
# The expo/expo bug: the spine ranker demoted every check present on <= 50% of PRs and crowned
# the slowest ALWAYS-present check — a lightweight `check-packages` that, by the findings' own
# `populations`, was the actual slowest job (the merge gate) on ZERO of 20 sampled PRs, burying
# the genuine 20-minute native/CLI suites that gate a minority of PRs. This invariant re-derives
# the per-PR pole (the slowest check each PR ran) from `populations` and fails a headline
# `critical_path_check` that is the actual gate on fewer than the recurrence floor of PRs — the
# class-detector that would have caught it from the data alone. Mirrors collect_runs'
# `_POLE_RECUR_FLOOR` / `_RARE_PRESENCE_MIN_PR` (kept coupled by a self-test). These constants are
# defined ONCE, near `_VR_RARE_PRESENCE_FRAC` above (a second copy here would let a threshold edit
# silently no-op) — this class reads that single definition.


def _vr_pole_frequencies(per_pr: list[list[tuple[str, float]]],
                         candidates: "set[str] | None" = None) -> dict[str, int]:
    """How many sampled PRs each check is the ACTUAL critical path (slowest) on, re-derived from
    `populations` (`per_pr` is already slowest-first per PR). The honest "is it the merge gate?"
    signal — the exact mirror of `collect_runs._pole_frequencies`: restricted to `candidates` (the
    ranked check set the engine argmaxes over) so verifier and engine argmax the SAME set, and
    TIES credit every co-slowest check (not just `pr[0]`) so a co-equal gate isn't starved of
    credit. `populations` magnitudes are already job-p50-capped, matching the engine's capped
    ranking basis — so the two never disagree on which check is the per-PR pole."""
    freq: dict[str, int] = {}
    for pr in per_pr:
        scoped = [(nm, d) for nm, d in pr if candidates is None or nm in candidates]
        if not scoped:
            continue
        mx = max(d for _, d in scoped)
        for nm, d in scoped:
            if d == mx:
                freq[nm] = freq.get(nm, 0) + 1
    return freq


def check_headline_pole_actually_gates(report: str, findings_path: Path | None) -> Check:
    """The rendered headline `critical_path_check` must ACTUALLY gate the merge — be the slowest
    job a PR waits on — on some sampled PRs. A headline that is the pole on FEWER than the
    recurrence floor of PRs while a genuine recurring gate (pole on >= floor) exists is a phantom
    gate (the expo/expo class: a lightweight always-present check crowned over the real 20-minute
    suites). Re-derived from `populations` — job-p50-capped, the same basis the engine ranks on.
    Required checks gate by definition (exempt). SKIPs on a thin sample / no populations.

    Why FAIL only when a real gate was PASSED OVER (not simply `hf < floor`): on a fragmented
    repo where NO check is the pole on >= floor PRs, the engine legitimately crowns the slowest
    overall — there is no better recurring gate to prefer, so failing that would be a false
    positive. The phantom-gate bug is specifically 'a genuine recurring gate existed and the
    engine crowned something that never gates instead'."""
    name = "headline critical-path check is an actual recurring gate"
    if findings_path is None:
        return Check(name, True, "no --findings to re-derive the gate from", skipped=True)
    try:
        data = _as_dict(json.loads(findings_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        return Check(name, True, f"findings unreadable: {e}", skipped=True)
    cp = _as_dict(data.get("pr_critical_path"))
    headline = cp.get("critical_path_check")
    if not isinstance(headline, str) or not headline:
        # None (static-only / no spine) OR a wrong-type value → nothing to re-derive against;
        # degrade to the same "no headline" SKIP as empty findings (never a false FAIL/crash).
        return Check(name, True, "no headline critical_path_check (static-only / no spine)",
                     skipped=True)
    per_pr = _populations_per_pr(cp)
    if len(per_pr) < _VR_RARE_PRESENCE_MIN_PR:
        return Check(name, True, f"only {len(per_pr)} PR population(s) (< {_VR_RARE_PRESENCE_MIN_PR}) "
                     "— pole frequency is noise on this sample", skipped=True)
    # Required checks gate by definition (branch protection), even if never the slowest — exempt.
    # Match on the RAW name (as the engine's `req_names` does), NOT a scope-normalized key: a
    # `_cmp_name` collapse would let a required `@teamA/build` exempt an unrelated phantom
    # `@teamB/build` (both normalize to `build`).
    required = {str(c) for c in _as_list(data.get("required_checks"))}
    if str(headline) in required:
        return Check(name, True, f"headline `{headline}` is a required check (gates by definition)")
    # Restrict the pole re-derivation to the ranked check set (the engine argmaxes over exactly
    # these — `set(pr_check_p50)`), keyed by RAW name to match `critical_path_check` and avoid the
    # monorepo scope-collision that a normalized key would introduce.
    ranked = {str(c.get("name") or c.get("check")) for c in _as_list(cp.get("checks"))
              if isinstance(c, dict)}
    freq = _vr_pole_frequencies(per_pr, ranked or None)
    hf = freq.get(str(headline), 0)
    real_gates = sorted((n for n, c in freq.items() if c >= _VR_POLE_RECUR_FLOOR),
                        key=lambda n: -freq[n])
    if hf < _VR_POLE_RECUR_FLOOR and real_gates:
        top = [(n, freq[n]) for n in real_gates[:3]]
        return Check(name, False,
                     f"headline `{headline}` is the actual slowest job (the merge gate) on only "
                     f"{hf}/{len(per_pr)} sampled PRs, yet genuine recurring gate(s) exist and were "
                     f"passed over: {', '.join(f'{n} (gates {c})' for n, c in top)}. The headline is "
                     "not the gate — this is the phantom-gate class.")
    if hf >= _VR_POLE_RECUR_FLOOR:
        return Check(name, True,
                     f"headline `{headline}` is the actual gate on {hf}/{len(per_pr)} sampled PRs "
                     f"(>= floor {_VR_POLE_RECUR_FLOOR})")
    return Check(name, True,
                 f"headline `{headline}` gates {hf}/{len(per_pr)} PRs; no check reaches the "
                 f"recurrence floor ({_VR_POLE_RECUR_FLOOR}) on this fragmented sample, so the "
                 "slowest-overall headline is the honest best available")


# Priced-dollar surface excised 2026-07-20 (the pre-public development archive
# preserves it, #98/#100).
# A rate-derived `$N` amount, a "USD" currency token, or a spelled-out
# "N dollars"/"N cents" figure on a MINUTES surface — the cost-spine table, an
# R-row, the section lead, the bottom line, or the TOC — would resurrect the
# retired pricing story. `$` inside ``` code fences (agent prompts, the
# workflow/job/step glossary's shell echoes, `${{ matrix }}` templates) is
# legitimate, so the sweep skips fenced blocks — but it scans indented lines too,
# because a `$`-figure hidden on a 4-space-indented line would otherwise slip past
# the visible-lines filter (which drops indentation as code-block). The one
# sanctioned pricing sentence ("multiply by your runner's per-minute rate to get
# dollars") legitimately ends in "dollars", so its exact phrase is stripped before
# matching — any OTHER dollars/cents/$ token on that same line still fails.
_RATE_DERIVED_DOLLAR_RE = re.compile(
    r"\$\s?-?\d|\busd\b|\bdollars?\b|\bcents?\b", re.IGNORECASE)
_SANCTIONED_PRICING_PHRASE = "multiply by your runner's per-minute rate to get dollars"
# Repo-derived identity tokens the report renders inside backticks (check / job /
# step / workflow / runner names, log excerpts). A user's job legitimately named
# `usd-integration-test` or `build-$4-shards` must not fail a minutes-only report
# — the check hunts SKILL-EMITTED dollar figures, and the skill never emits its
# own prose inside backticks (#104 review). Strip backtick spans before matching.
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")


def check_no_rate_derived_dollars(report: str, findings_path: Path | None = None) -> Check:
    name = "no rate-derived dollars on the minutes surfaces"
    hits: list[str] = []
    for line in _nonfence_markdown_lines(report):
        scrubbed = _BACKTICK_SPAN_RE.sub("", line)
        scrubbed = scrubbed.replace(_SANCTIONED_PRICING_PHRASE, "")
        if _RATE_DERIVED_DOLLAR_RE.search(scrubbed):
            hits.append(line.strip()[:100])
    if hits:
        return Check(name, False,
                     "rate-derived dollar/USD token(s) on a minutes surface: "
                     + " | ".join(hits[:4]))
    return Check(name, True, "no rate-derived dollar tokens on the minutes surfaces")


def run_checks(report, report_path, findings_path, skill_repo, clone=None):
    return [
        check_primary_section_present(report, findings_path),
        check_static_only_banner_matches_ci_shape(report, findings_path),
        check_headline_names_wall_clock(report),
        check_pole_anchors_resolve(report),
        check_rca_hands_off_never_prescribes(report),
        check_measured_cause_matches_rendered_timeline(report),
        check_skip_family_prompts_carry_pending_caveat(report),
        check_also_noticed_count_honest(report),
        check_coverage_disclosed(report, findings_path),
        check_run_list_gaps_named(report, findings_path),
        check_prefetch_plan_consumed(report, findings_path),
        check_cost_spine_shallow_disclosed(report, findings_path),
        check_date_matches_filename(report, report_path),
        check_no_typographic_dashes(report),
        check_fences_balanced(report),
        check_no_domain_leakage(report),
        check_skill_commit_provenance(report, skill_repo),
        check_rendered_patterns_exist(report, findings_path),
        check_data_driven_have_signal(findings_path),
        check_speed_poles_complete(report, findings_path),
        check_aggregation_gate_poles_never_prescribe(report, findings_path),
        check_pole_drill_belongs_to_its_job(report, findings_path),
        check_gap_fill_evidence_grounded(report, findings_path),
        check_dropped_check_not_framed_on_path(report, findings_path),
        check_demoted_pole_not_framed_typical_gate(report, findings_path),
        check_headline_slowest_matches_stamp(report, findings_path, report_path),
        check_headline_floor_presence_reconciled(report, findings_path),
        check_headline_presence_causal_only_when_minority(report, findings_path),
        check_headline_chain_matches_stamp(report, findings_path, report_path),
        check_aggregate_total_ge_largest_member(report, findings_path, report_path),
        check_headline_wait_within_makespan(report, findings_path, report_path),
        check_headline_basis_excludes_fileless(report, findings_path, report_path),
        check_saving_within_measured_compute(report, findings_path),
        check_saving_carries_measured_basis(report, findings_path),
        check_claims_cover_framing_vocabulary(report, report_path),
        check_tier2_neutrality_derived(report, findings_path, report_path),
        check_tier2_measured_basis(report, findings_path),
        check_tier2_total_deoverlapped(report, findings_path, report_path),
        check_no_timing_endpoint_citation(report, report_path),
        check_tier2_claims_derivation_basis(report, findings_path, report_path),
        check_tier2_headline_matches_stamp(report, findings_path, report_path),
        check_tier2_savings_rows_backed_by_cost_spine(report, findings_path),
        check_runner_minute_spine_contract(report, findings_path),
        check_no_rate_derived_dollars(report, findings_path),
        check_structural_pole_has_measured_step(findings_path),
        check_structural_step_category_not_payload_binned_as_build(findings_path),
        check_detector_leaf_agrees_with_dominant_category(report, findings_path),
        check_pole_ceiling_within_cooccurrence(report, findings_path),
        check_recoverable_within_wait(report, findings_path, report_path),        # issue #66 fix 2
        check_config_era_boundary(report, findings_path, report_path),            # issue #66 fix 1
        check_era_enumeration_bound(report, findings_path, report_path),           # issue #69
        check_era_disclosure_matches_enumeration(report, findings_path, report_path),  # issue #74
        check_era_chain_spine_bound_to_kept_era(report, findings_path, report_path),  # issue #80
        check_cluster_lever_ceiling_escapes_sibling(report, findings_path),
        check_headline_consumes_stamped_cluster_ceiling(report, findings_path),
        check_headline_lever_is_presence_eligible(report, findings_path),
        check_headline_cluster_lever_on_spine(report, findings_path),               # issue #114
        check_headline_wait_is_divergence_correct(report, findings_path, report_path),  # issue #115
        check_spine_heavy_check_disclosed(report, findings_path),
        check_workflow_gate_frequency_matches(report, findings_path),
        check_pole_not_reframed_as_hygiene(report, findings_path),
        check_toc_also_noticed_label_honest(report),
        check_cache_claim_backed_by_distribution(report, findings_path),
        check_cache_framing_matches_distribution(report, findings_path),
        check_headline_pole_actually_gates(report, findings_path),
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify a ci-speedup report's invariants.")
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--findings", type=Path, help="findings JSON the report came from")
    p.add_argument("--skill-repo", type=Path, help="skill git checkout, to check commit provenance")
    p.add_argument("--clone", type=Path, help="(accepted for compatibility; unused by the RCA report)")
    args = p.parse_args(argv)

    report = args.report.read_text(encoding="utf-8")
    checks = run_checks(report, args.report, args.findings, args.skill_repo, args.clone)

    failed = 0
    for c in checks:
        tag = "SKIP" if c.skipped else ("PASS" if c.ok else "FAIL")
        if not c.skipped and not c.ok:
            failed += 1
        print(f"{tag}  {c.name}" + (f"  - {c.detail}" if c.detail else ""))
    print()
    if failed:
        print(f"❌ {failed} check(s) failed")
        return 1
    print("✅ all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
