#!/usr/bin/env python3
"""Merge an orchestrator-measured phase duration into the findings JSON.

Most of the pipeline self-times: ``scan.py`` writes ``timings.scan_total_s``
and ``timings.activity_enrich_s`` into the findings JSON. But the
orchestrator-driven phases — Phase 2.5 (writing each group's
``attacker_scenario``, in one pass) and Phase 5 (per-group fixes) — are not
scripts, so their wall-clock never reached the durable ``timings`` block. That
made the block misleading: a reader (or a future optimizer following the
"target the largest *measured* cost" rule) couldn't see that the prose phase
dominates a run, because only the cheap scripted phases were recorded.

This helper closes that gap for the one phase that runs AFTER the last script:
Phase 5 per-group fixes. (Phase 2.5's span is closed automatically by
report.py, which runs after it and writes ``risk_scenario_s`` — a legacy key
name for the prose gap; no manual recording is needed there.) The orchestrator
wraps the post-render phase in wall-clock and records it here, so the findings
JSON alone answers "where did the time go?" for the whole run:

    _t=$(date +%s)
    # ... Phase 5 per-group fixes ...
    ./scripts/record_timing.py --findings "$FINDINGS" \
        --phase fixes_s --seconds $(( $(date +%s) - _t ))

Merges (never clobbers) the existing ``timings`` keys, mirroring how
``scan.py`` builds the block, so calling it repeatedly across phases just adds
one key each time.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record an orchestrator-measured phase duration into the "
        "findings JSON timings block."
    )
    parser.add_argument(
        "--findings", required=True, type=Path,
        help="Path to the findings JSON to update in place.",
    )
    parser.add_argument(
        "--phase", required=True,
        help="Timing key to set, e.g. fixes_s.",
    )
    parser.add_argument(
        "--seconds", required=True, type=float,
        help="Wall-clock seconds the orchestrator measured for the phase.",
    )
    args = parser.parse_args(argv)

    try:
        data = json.loads(args.findings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read findings JSON {args.findings}: {e}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"ERROR: findings JSON {args.findings} is not an object", file=sys.stderr)
        return 1

    timings = data.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    timings[args.phase] = round(args.seconds, 2)
    data["timings"] = timings
    args.findings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
