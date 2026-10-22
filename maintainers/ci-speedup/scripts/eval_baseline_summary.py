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


def _span(vals: list[float], fmt=lambda v: f"{v:,.0f}") -> str:
    if not vals:
        return "n/a"
    return f"{fmt(statistics.median(vals))} ({fmt(min(vals))} to {fmt(max(vals))})"


def summarize(rows: list[dict]) -> str:
    cases: dict[int, list[dict]] = {}
    for r in rows:
        cases.setdefault(r.get("id"), []).append(r)
    head = ("| id | case | runs | passed | not run | reference bytes | reference files | "
            "catalog opened | input tokens | wall time (s) |\n|---|---|---|---|---|---|---|---|---|---|")
    lines, checks = [head], []
    for cid in sorted(cases, key=lambda i: (i is None, i)):
        rs = cases[cid]
        ran = [r for r in rs if "reading" in r]
        name = next((r.get("name") for r in ran if r.get("name")), "?")
        rd = [r["reading"] for r in ran]
        lines.append("| {} | `{}` | {} | {} | {} | {} | {} | {}/{} | {} | {} |".format(
            cid, name, len(rs), sum(1 for r in ran if r.get("passed")), len(rs) - len(ran),
            _span([x["reference_bytes"] for x in rd]),
            _span([len(x["reference_files"]) for x in rd]),
            sum(1 for x in rd if x["catalog_opened"]), len(rd),
            _span([x["session"]["input_tokens"] for x in rd]),
            _span([x["session"]["wall_s"] or 0 for x in rd])))
        names = [c["name"] for c in (ran[0]["checks"] if ran else [])]
        checks += [f"| {cid} | `{n}` | {sum(1 for r in ran for c in r['checks'] if c['name'] == n and c['passed'])}/{len(ran)} |"
                   for n in names]
    return ("\n".join(lines) + "\n\nPass count per check:\n\n| id | check | passed |\n|---|---|---|\n"
            + "\n".join(checks) + "\n")


def main(argv: list[str]) -> int:
    d = Path(argv[0])
    rows = [r for p in sorted(d.glob("*.json")) for r in json.loads(p.read_text(encoding="utf-8"))]
    print(summarize(rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
