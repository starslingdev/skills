#!/usr/bin/env python3
"""Summarise a directory of skill-eval results into the baseline table.

    python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py DIR

Reads every `*.json` results list the shared runner wrote into DIR and prints a
Markdown table per case: runs, passes, not-run sessions, the median and min to
max of reference bytes read, reference files read, whole-session input tokens
(subagents included) and wall time, how many runs opened the catalog, and the
pass count of every check. "Of which catalog" counts the catalog in either
shape: the single `optimization-patterns.md` or the split `patterns/` files.
When any run read a split-catalog file, a further table lists, per case, how
many runs opened the compiled single file and which `patterns/` files were
read (runs that read each, and the bytes).
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

CATALOG = "optimization-patterns.md"
PATTERNS = "patterns/"


def _is_catalog(path: str) -> bool:
    return path.endswith(CATALOG) or path.startswith(PATTERNS)


def _span(vals: list[float], fmt=lambda v: f"{v:,.0f}") -> str:
    if not vals:
        return "n/a"
    return f"{fmt(statistics.median(vals))} ({fmt(min(vals))} to {fmt(max(vals))})"


def summarize(rows: list[dict]) -> str:
    """The baseline tables. A row is a session that ran when it carries
    `passed`; a `harness_error` row is not run; a `skipped` row (an entry its
    tier left out) was never selected, so it is no row at all. Reading figures
    come from the rows that recorded `reading`, each check counts over the rows
    that carry it, and a missing wall time is left out of the medians (never a
    zero)."""
    cases: dict[int, list[dict]] = {}
    for r in (r for r in rows if "skipped" not in r):
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
                       if _is_catalog(f.get("path", ""))) for x in rd]),
            _span([len(x["reference_files"]) for x in rd]),
            sum(1 for x in rd if x.get("catalog_opened")), len(rd),
            _span([s["input_tokens"] for s in sess if s.get("input_tokens") is not None]),
            _span([s["wall_s"] for s in sess if s.get("wall_s") is not None])))
        names = list(dict.fromkeys(c["name"] for r in ran for c in r.get("checks", [])))
        for n in names:
            got = [c["passed"] for r in ran for c in r.get("checks", []) if c["name"] == n]
            checks.append(f"| {cid} | `{n}` | {sum(1 for v in got if v)}/{len(got)} |")
    return ("\n".join(lines) + "\n\nPass count per check:\n\n| id | check | passed |\n|---|---|---|\n"
            + "\n".join(checks) + "\n" + _split_files(cases))


def _split_files(cases: dict[int, list[dict]]) -> str:
    """Per case: runs that opened the compiled file, and each `patterns/` file
    read (runs, median and min to max bytes over the runs that read it).
    Empty when no run read a split-catalog file."""
    rows = []
    for cid in sorted(cases, key=lambda i: (i is None, i)):
        rd = [r["reading"] for r in cases[cid] if "passed" in r and "harness_error" not in r
              and isinstance(r.get("reading"), dict)]
        compiled = sum(1 for x in rd if any(f.get("path", "").endswith(CATALOG) for f in x["reference_files"]))
        per: dict[str, list[int]] = {}
        for x in rd:
            for f in x["reference_files"]:
                if f.get("path", "").startswith(PATTERNS):
                    per.setdefault(f["path"], []).append(f.get("bytes", 0))
        read = "; ".join(f"`{p}` {len(v)}/{len(rd)} runs, {_span(v)} bytes" for p, v in sorted(per.items()))
        rows.append((cid, compiled, len(rd), read or "none"))
    if not any(r[3] != "none" for r in rows):
        return ""
    return ("\nSplit catalog files read:\n\n| id | compiled file opened | `patterns/` files read |\n"
            "|---|---|---|\n" + "\n".join(f"| {c} | {o}/{n} | {r} |" for c, o, n, r in rows) + "\n")


def main(argv: list[str]) -> int:
    d = Path(argv[0])
    rows = [r for p in sorted(d.glob("*.json")) for r in json.loads(p.read_text(encoding="utf-8"))]
    print(summarize(rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
