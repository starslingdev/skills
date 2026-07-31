"""CI Score v0.1-basic — the pure-function scorer (OD-CS15).

`compute_ci_score(doc, spec)` maps a findings.json to the `ci_score` stamp:
eleven pass/fail configuration facts, score = passed / applicable, one refusal
(no workflows to check). The facts themselves are computed by the scan
(`practice_facts` — things present or absent in the repo's own YAML); this
module only maps them onto the spec's registry and does the arithmetic, so a
maintainer can verify any state in under a minute and the scorer has almost
nothing to get wrong.

The v1-granular rubric (magnitude-gated checks, tiers, weights, nine refusals)
was punted by the owner before publication — decision record
`decision_log.OD-CS15` in the spec; its machinery survives in git history and
in the measured pipeline's stamps, which remain the report and may enrich a
check's evidence for DISPLAY only.

Purity: no network, no clock, no LLM, no input mutation; byte-identical output
for identical input. A scoring failure upstream (`_stamp_ci_score`) leaves the
document unstamped with a `data_sources.ci_score_error` marker — never a
partial stamp.
"""
from __future__ import annotations

import math
from typing import Any

_VALID_STATES = {"pass", "fail", "not_applicable"}


def _round_half_up(x: float) -> int:
    """The spec's pinned rounding. Python's round() is banker's rounding and
    gives 84 for 84.5 — a published-letter coin flip."""
    return int(math.floor(x + 0.5))


def _grade(value: int, spec: dict) -> str:
    for band in spec["bands"]:
        for suffix, span in (("-", band.get("minus")), ("", band.get("bare")),
                             ("+", band.get("plus"))):
            if span and span[0] <= value <= span[1]:
                return band["grade"] + ("" if band["grade"] == "F" else suffix)
    raise AssertionError(f"no band covers {value}")  # bands are tested contiguous


def _measured_note(check_id: str, doc: dict) -> str | None:
    """DISPLAY-ONLY enrichment: when the measured pipeline carries a sized
    finding for this practice, surface its cost next to the config fact.
    Never changes a state (formula.rules.evidence_display_only)."""
    slug_patterns = {
        "ci.cache.dependency-cache": ("OPT1", "OPT2", "OPT5", "OPT6", "OPT9", "OPT63"),
        "ci.cache.build-cache": ("OPT3", "OPT41", "OPT52", "OPT58", "OPT60"),
        "ci.checkout.shallow-clone": ("OPT28",),
        "ci.parallel.test-sharding": ("OPT24", "OPT25"),
        "ci.trigger.cancel-superseded": ("OPT46",),
    }
    patterns = slug_patterns.get(check_id)
    if not patterns:
        return None
    best = 0.0
    for f in doc.get("findings") or []:
        if isinstance(f, dict) and str(f.get("pattern")) in patterns:
            wc = f.get("wall_clock_p50_s")
            if isinstance(wc, (int, float)) and wc > best:
                best = float(wc)
    return f"measured cost on this repo: ~{best:.0f}s per run" if best > 0 else None


def compute_ci_score(doc: dict, spec: dict) -> dict:
    facts = doc.get("practice_facts")
    refusal = None

    def _refusal(reason_code: str) -> dict:
        # RAISE (never silently substitute another reason) if the code is
        # absent from this spec — a silent mislabel of the refusal reason is
        # worse than a loud failure. The caller turns the raise into an honest
        # ci_score_error marker, never a wrong-reasoned stamp.
        r = next(x for x in spec["refusals"] if x["reason_code"] == reason_code)
        return {"reason_code": r["reason_code"], "human_reason": r["human_reason"]}

    if doc.get("scanned_workflows") == 0:
        refusal = _refusal("no_workflow_yaml")
    elif doc.get("automation_only"):
        # OD-CS20: workflows exist but none do project build or test — only
        # automation (bots, releases, triage). A number here would be
        # technically honest but absurd and would corrode trust in every other
        # score; refuse instead. Precedes facts_unavailable: it is the more
        # specific, more useful reason.
        refusal = _refusal("automation_only")
    elif not isinstance(facts, dict) or not all(
            c["check_id"] in facts for c in spec["checks"]):
        # Absent OR incomplete: a partial stamp must never publish a score
        # computed from the subset it happens to carry.
        refusal = _refusal("facts_unavailable")

    checks: list[dict[str, Any]] = []
    passed = applicable = 0
    for check in spec["checks"]:
        cid = check["check_id"]
        fact = facts.get(cid) if isinstance(facts, dict) else None
        if refusal is not None or not isinstance(fact, dict):
            state, evidence = "not_applicable", "not evaluated (no facts stamped)"
            files: list[str] = []
        else:
            state = str(fact.get("state"))
            if state not in _VALID_STATES:
                state, evidence, files = ("not_applicable",
                                          f"unrecognized fact state {fact.get('state')!r}", [])
            else:
                evidence = str(fact.get("evidence") or "").strip() or "(no evidence string stamped)"
                files = [str(x) for x in fact.get("files") or []]
        if refusal is None:
            if state == "pass":
                passed += 1
                applicable += 1
            elif state == "fail":
                applicable += 1
        checks.append({"check_id": cid, "label": check.get("label") or cid,
                       "state": state, "evidence": evidence, "files": files,
                       "measured_note": _measured_note(cid, doc) if state == "fail" else None})

    value = grade = None
    if refusal is None:
        if applicable == 0:
            # Every check n/a (nothing was checkable): the honest output is
            # the refusal, never a vacuous 100 — the Scorecard anomaly again.
            # Same reason_code as facts_unavailable but a case-specific reason
            # (all-n/a, not vintage); the code is named, never positional.
            refusal = {"reason_code": "facts_unavailable",
                       "human_reason": "No score: none of the practice checks "
                                       "were applicable to this repository."}
        else:
            value = _round_half_up(100.0 * passed / applicable)
            grade = _grade(value, spec)

    return {
        "spec_version": spec["spec_version"],
        "scope_statement": spec["scope_statement"],
        "value": value,
        "grade": grade,
        "refusal": refusal,
        "checks": checks,
        "checks_passed": passed if refusal is None else None,
        "checks_applicable": applicable if refusal is None else None,
    }
