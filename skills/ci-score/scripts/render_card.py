"""CI Score card renderer (extracted from ci-speedup's blocking_path.py,
2026-07-16).

`_render_score_card(doc)` renders the CI Score card from a findings.json's
`ci_score` stamp ONLY — the single-source rule: every number on the card IS the
stamp; nothing here recomputes. No stamp -> no card. A recorded scoring failure
(`data_sources.ci_score_error`) renders as one honest line.

`_flatten_cell` is copied verbatim from ci-speedup (cross-skill imports are
forbidden — a skill must install standalone).
"""
from __future__ import annotations

import re
from typing import Any

# The score gauge is a 25-block scale. `filled` is round-half-up(value·25/100)
# expressed in integer math ((value·25 + 50)//100) so 0.5 always rounds up
# (Python's round() bankers-rounds and would send 12.5→12). verify_report.py
# re-derives this exact count from the rendered bar — the gauge cannot drift
# from its number without going red.
_GAUGE_BLOCKS = 25


def _gauge_line(value: int, passed: Any, applicable: Any, na: int) -> str:
    """The score gauge — the card's first line. `filled` blocks are
    round-half-up(value × 25 / 100); the `· N n/a` tail appears only when the
    not-applicable count is positive."""
    filled = (value * _GAUGE_BLOCKS + 50) // 100
    bar = "█" * filled + "░" * (_GAUGE_BLOCKS - filled)
    tail = f"{passed} of {applicable} checks pass"
    if na > 0:
        tail += f" · {na} n/a"
    return f"CI Score  {value}/100  ▏{bar}▕  {tail}"


def _anchor(label: str) -> str:
    """GitHub-style heading anchor for the report's "What each check means"
    subsections (the in-document link targets the card points at)."""
    return re.sub(r"[^a-z0-9 -]", "", label.lower()).replace(" ", "-")


def _flatten_cell(text: str) -> str:
    """A markdown table cell can't contain a raw newline or an unescaped pipe."""
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


def _render_score_card(doc: dict[str, Any]) -> list[str]:
    """The CI Score card — rendered ONLY from the `ci_score` stamp (the
    single-source rule: every number on the card IS the stamp; nothing here
    recomputes). No stamp -> no card (pre-score documents render exactly as
    before). A recorded scoring failure renders as one honest line — an
    unstamped-with-error doc must not look like a pre-scorer doc."""
    stamp = doc.get("ci_score")
    if not isinstance(stamp, dict):
        # collect_config records a scoring failure as a STRING
        # (`f"{type(exc).__name__}: {exc}"`); older/synthetic docs may carry a
        # {"error": ...} dict. Honour both shapes so a recorded failure is
        # never silently dropped from the report (an unstamped-with-error doc
        # must not render like a pre-scorer doc).
        err = (doc.get("data_sources") or {}).get("ci_score_error")
        msg = err.get("error") if isinstance(err, dict) else (
            err if isinstance(err, str) and err else None)
        if msg:
            return ["> **CI Score unavailable** — scoring failed on this run "
                    f"(`{msg}`). The audit below is unaffected.", ""]
        return []
    out: list[str] = []
    checks = stamp.get("checks")
    checks_list = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    refusal = stamp.get("refusal")
    if isinstance(refusal, dict):
        # human_reason strings begin "No score: ..." OR "Not scored: ..." —
        # strip either prefix here so the heading doesn't stutter ("no score:
        # No score: ..." / "no score: Not scored: ..."). OD-CS20's
        # automation_only reason uses the "Not scored:" form.
        reason = str(refusal.get("human_reason", ""))
        low = reason.lower()
        for prefix in ("no score:", "not scored:"):
            if low.startswith(prefix):
                reason = reason[len(prefix):].strip()
                break
        out += [f"## CI Score — no score: {reason}", ""]
    else:
        # The gauge is the card's first line — a monospace terminal visual, so
        # it's fenced to survive markdown (runs of spaces and the box-drawing
        # caps keep their width). No gauge on refusal/error cards (handled in
        # the branches that return before here). Guarded on an int value so a
        # malformed stamp still renders best-effort instead of raising.
        value = stamp.get("value")
        if isinstance(value, int):
            na = sum(1 for c in checks_list if c.get("state") == "not_applicable")
            out += ["```", _gauge_line(value, stamp.get("checks_passed"),
                                       stamp.get("checks_applicable"), na), "```", ""]
        # Number-only presentation (owner, 2026-07-28): the letter band stays
        # in the stamp/registry but is not rendered — 8-12 checks cannot
        # distinguish adjacent values, and the numeric form is softer on a
        # public page than a report-card letter. This line stays as the card's
        # second line beneath the gauge (the gauge adds a visual, it does not
        # replace the machine-checkable headline verify_report.py pins).
        out += [f"## CI Score: **{stamp.get('value')}/100** — "
                f"{stamp.get('checks_passed')} of {stamp.get('checks_applicable')} "
                "applicable checks", ""]
    out += [f"> {stamp.get('scope_statement', '')}", ""]
    marks = {"pass": "✅", "fail": "❌", "not_applicable": "n/a"}
    out += ["| | Check | Evidence |", "|---|---|---|"]
    for chk in checks_list:  # already filtered to dicts — the card never dies
        mark = marks.get(str(chk.get("state")), "?")
        evidence = str(chk.get("evidence") or "")
        note = chk.get("measured_note")
        if note:
            evidence += f" — {note}"
        # Cells route through the module's escaper like every other table: a
        # pipe or newline in an evidence string must not collapse the row.
        raw_label = str(chk.get("label") or chk.get("check_id"))
        label = _flatten_cell(raw_label)
        # Every check name links its "What each check means" subsection in the
        # SAME document — an in-document anchor, never a filesystem path (owner,
        # 2026-07-28: absolute-path/methodology-file links broke in common
        # viewers, which treat `path.md#anchor` as a literal filename). The
        # report renders that appendix; the anchor is GitHub's slug of the
        # subsection heading, which _anchor() reproduces.
        label = f"[{label}](#{_anchor(raw_label)})"
        out.append(f"| {mark} | {label} | {_flatten_cell(evidence)} |")
    out.append("")
    return out
