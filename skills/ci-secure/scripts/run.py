#!/usr/bin/env python3
"""One-shot driver for ci-secure's deterministic phase.

Since the critical-only descope there is ONE scripted phase before the
orchestrator's prose pass: the scan. This driver runs it, validates the
output, and atomically publishes ONE findings JSON. (The old three-step
pipeline — scan → fix-complexity risk → render-plan — shrank with the
contract: risk scoring left with the comprehensive catalog, and the render
plan is trivial now that EVERY group renders.)

It also keeps end-to-end timing reliable WITHOUT an orchestrator-managed
stamp file: `scan.py` records the run's start epoch into the findings
`timings` block; this driver adds its scripted-phase span; `report.py`
(always the last step) reads the start epoch back and computes
`total_run_s`. The scripts own the timing, not the agent's memory.

Usage:
    run.py --root REPO --out FINDINGS.json [--repo owner/repo]
           [--gh-impostor auto|on|off] [--catalog PATH]
    # writes FINDINGS.json; prints a PRESENCE LIST of the pattern ids
    # present, sorted by id and UNORDERED with respect to the report —
    # ["P14.10", ...]. Every one needs an attacker_scenario, because every
    # one renders. It is NOT the render plan: for the report's own order and
    # per-group dormancy use `report.py --render-plan --in FINDINGS.json`,
    # which numbers groups exactly as the report does.
    # scan stderr flows straight through, so coverage warnings are visible.

The output path is cleared before the scan runs, so a failed run never
leaves a previous run's findings at the fixed path SKILL.md reuses.

Exit codes, all of them:
  0  ok.
  2  scan.py's own invalid-argument code, propagated unchanged. scan.py
     exits 2 for a malformed `--repo` AND for `--gh-impostor on` when gh is
     not authenticated (the flag demands the network-gated check, so a skip
     would be a silent downgrade). Either way the argument, not the repo,
     is what needs fixing.
  1  any other failure — scan non-zero (including no .github/workflows dir)
     or unparseable/empty findings output. A coverage failure the
     orchestrator must surface and stop on, never render over.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ci-secure's scan and write one findings JSON.",
    )
    parser.add_argument("--root", required=True, help="Repo root to scan.")
    parser.add_argument("--out", required=True, type=Path, help="Findings JSON path.")
    parser.add_argument("--repo", default=None, help="owner/repo for dormancy activity lookup.")
    parser.add_argument(
        "--gh-impostor", choices=["auto", "on", "off"], default="auto",
        help="Pass-through to scan.py's network-gated impostor-SHA check.",
    )
    parser.add_argument("--catalog", default=None, help="Override catalog path.")
    args = parser.parse_args(argv)

    run_start = time.time()

    # Clear the output path BEFORE scanning. SKILL.md uses a fixed literal
    # findings path across runs, so a failed run that left the previous run's
    # file in place would hand the orchestrator stale findings exactly where
    # it promises none exist — the report would render a prior repo's chains
    # as this one's. Absence after a failure is the contract.
    try:
        args.out.unlink(missing_ok=True)
    except OSError as e:
        print(f"ERROR: cannot clear {args.out}: {e}", file=sys.stderr)
        return 1

    # Scan. stdout = findings JSON; stderr flows through. A non-zero exit is a
    # coverage failure — propagate it, never write a partial findings file.
    scan_cmd = [
        sys.executable, str(_DIR / "scan.py"),
        "--root", args.root, "--gh-impostor", args.gh_impostor,
    ]
    if args.repo:
        scan_cmd += ["--repo", args.repo]
    if args.catalog:
        scan_cmd += ["--catalog", args.catalog]
    scan = subprocess.run(scan_cmd, stdout=subprocess.PIPE, text=True)
    if scan.returncode == 2:
        # Exit 2 is scan.py's invalid-ARGUMENT code (malformed --repo, or
        # `--gh-impostor on` with gh unauthenticated) — NOT a coverage
        # failure. The repo was never scanned; the flag is what needs fixing.
        print(
            f"ERROR: scan.py exited 2 — invalid argument, not a coverage "
            f"failure. Fix the flag reported in the stderr above and re-run.",
            file=sys.stderr,
        )
        return scan.returncode
    if scan.returncode != 0:
        print(
            f"ERROR: scan.py exited {scan.returncode} — coverage failure, not a "
            f"clean repo. Surface the stderr above and stop; do not render.",
            file=sys.stderr,
        )
        return scan.returncode
    try:
        data = json.loads(scan.stdout or "")
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            raise ValueError("missing `findings` array")
    except (json.JSONDecodeError, ValueError) as e:
        print(
            f"ERROR: scan.py exited 0 but produced unparseable/empty findings "
            f"({e}) — coverage failure, not a clean repo. Stop; do not render.",
            file=sys.stderr,
        )
        return 1

    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    timings["scripted_end_epoch"] = round(time.time(), 3)
    timings["scripted_total_s"] = round(time.time() - run_start, 2)
    data["timings"] = timings

    # Atomic publish: write a sibling temp file, then rename.
    tmp = args.out.with_name(args.out.name + ".partial")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(args.out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    # Presence list, unordered: which patterns are present, sorted by id.
    # Every one needs an attacker_scenario (every group renders). Render
    # ORDER and dormancy come from `report.py --render-plan`, which shares
    # the renderer's own grouping code — this driver does not render.
    groups = sorted({f.get("pattern", "?") for f in data["findings"]})
    sys.stdout.write(json.dumps(groups) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
