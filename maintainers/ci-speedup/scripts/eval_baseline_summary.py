#!/usr/bin/env python3
"""Summarise a directory of skill-eval results into the baseline table.

    python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py DIR

Reads every `*.json` results list the shared runner wrote into DIR and prints a
Markdown table per case: runs, passes, not-run sessions, the median and min to
max of reference bytes read, reference files read, whole-session input tokens
(subagents included) and wall time, how many runs opened the catalog, and the
pass count of every check.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

CATALOG = "optimization-patterns.md"


def _span(vals: list[float], fmt=lambda v: f"{v:,.0f}") -> str:
    if not vals:
        return "n/a"
    return f"{fmt(statistics.median(vals))} ({fmt(min(vals))} to {fmt(max(vals))})"


def summarize(rows: list[dict]) -> str:
    """The baseline tables. A row is a session that ran when it carries
    `passed`; a `harness_error` row is not run. Reading figures come from the
    rows that recorded `reading`, each check counts over the rows that carry
    it, and a missing wall time is left out of the medians (never a zero)."""
    cases: dict[int, list[dict]] = {}
    for r in rows:
        cases.setdefault(r.get("id"), []).append(r)
    head = ("| id | case | runs | passed | not run | reference bytes | of which catalog | "
            "reference files | catalog opened | input tokens | wall time (s) |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|")
    lines, checks = [head], []
    for cid in sorted(cases, key=lambda i: (i is None, i)):
        rs = cases[cid]
        ran = [r for r in rs if "passed" in r and "harness_error" not in r]
        name = next((r.get("name") for r in ran if r.get("name")), "?")
        rd = [r["reading"] for r in ran if isinstance(r.get("reading"), dict)]
        sess = [x.get("session") or {} for x in rd]
        lines.append("| {} | `{}` | {} | {} | {} | {} | {} | {} | {}/{} | {} | {} |".format(
            cid, name, len(rs), sum(1 for r in ran if r.get("passed")), len(rs) - len(ran),
            _span([x["reference_bytes"] for x in rd]),
            _span([sum(f.get("bytes", 0) for f in x["reference_files"]
                       if f.get("path", "").endswith(CATALOG)) for x in rd]),
            _span([len(x["reference_files"]) for x in rd]),
            sum(1 for x in rd if x.get("catalog_opened")), len(rd),
            _span([s["input_tokens"] for s in sess if s.get("input_tokens") is not None]),
            _span([s["wall_s"] for s in sess if s.get("wall_s") is not None])))
        names = list(dict.fromkeys(c["name"] for r in ran for c in r.get("checks", [])))
        for n in names:
            got = [c["passed"] for r in ran for c in r.get("checks", []) if c["name"] == n]
            checks.append(f"| {cid} | `{n}` | {sum(1 for v in got if v)}/{len(got)} |")
    return ("\n".join(lines) + "\n\nPass count per check:\n\n| id | check | passed |\n|---|---|---|\n"
            + "\n".join(checks) + "\n")


def main(argv: list[str]) -> int:
    d = Path(argv[0])
    rows = [r for p in sorted(d.glob("*.json")) for r in json.loads(p.read_text(encoding="utf-8"))]
    print(summarize(rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
