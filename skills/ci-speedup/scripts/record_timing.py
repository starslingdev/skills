#!/usr/bin/env python3
"""Merge an orchestrator-measured phase duration into the findings JSON.

Most of the pipeline self-times: ``scan.py`` writes ``timings.static_scan_s``
and ``collect_runs.py`` writes ``timings.gh_timing_s``. But ``total_run_s`` and
any orchestrator-driven phases (the render hand-off, the user's own fix work)
are not self-timed by a single script, so their wall-clock never reaches the
durable ``timings`` block on their own. That makes the block misleading: a
reader looking at where the time went sees only the cheap scripted phases.

This helper closes that gap. The orchestrator wraps each agentic phase in
wall-clock and records it here, so the findings JSON alone answers "where did
the time go?" for the whole run:

    _t=$(date +%s)
    # ... agentic catalog walk ...
    ./scripts/record_timing.py --findings "$FINDINGS" \
        --phase agentic_walk_s --seconds $(( $(date +%s) - _t ))

Merges (never clobbers) the existing ``timings`` keys, so calling it
repeatedly across phases just adds one key each time.
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
        help="Timing key to set, e.g. risk_scenario_s or fixes_s.",
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
    # Atomic write: render to `.partial` then rename. A mid-write crash
    # leaves the prior good file intact; without this, the orchestrator
    # could lose every previously-recorded phase duration on a kill.
    partial = args.findings.with_name(args.findings.name + ".partial")
    try:
        partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        partial.replace(args.findings)
    except OSError as e:
        print(f"ERROR: cannot write findings JSON {args.findings}: {e}", file=sys.stderr)
        # Clean up the partial if it landed but rename failed.
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    # This directory first, so the sibling import below resolves even under
    # PYTHONSAFEPATH / `python -P` (the script's directory is not on sys.path).
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from log_level import configure_logging  # STARSLING_LOG_LEVEL opt-in
    configure_logging()
    sys.exit(main())
