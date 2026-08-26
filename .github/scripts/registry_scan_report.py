#!/usr/bin/env python3
"""Surface every registry-scan finding, without letting a warning block the build.

`.github/workflows/registry-scan.yml` blocks on every risk 0.6.0 reports except the
ones named in `registry_scan_contract.NON_BLOCKING_RISKS`. Those exempt risks are real
findings we want a human or a review agent to look at, but they do not fail CI. A
finding nobody can see is the failure this whole gate exists to prevent, so "does not
block" must not decay into "does not appear".

This script reads the scanner's `--json` output and writes it to three surfaces, each
for a different consumer:

  1. `::warning::` workflow commands — render on the checks tab and the Files view, and
     come back through the checks API as annotations, which is what an automated review
     agent can actually query.
  2. A markdown table on `$GITHUB_STEP_SUMMARY` — the human-glanceable surface.
  3. The raw JSON, uploaded as a run artifact by the workflow — a deterministic feed for
     tooling, so nothing has to scrape the log.

It also prints a plain-text table to the job log, because the scanner's own rich report
is not produced in JSON mode and this is the only pass that sees every finding: the
gating pass runs with the exemption list, and `--ignore-risks` nulls those risks out of
the response before it is printed as well as before the exit status is computed.

Exit status: 0 whatever the findings are — gating is the next step's job, and exempt
risks must never block. It exits 1 only if the scan output cannot be read: either it
does not parse, or it parses into a shape this build does not recognise. Both are a
broken pipeline, and silence there looks exactly like "nothing found".

Usage:  python3 .github/scripts/registry_scan_report.py <findings.json>
"""
from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from registry_scan_contract import (  # noqa: E402
    STALE_PIN_HINT,
    UnrecognisedPayload,
    iter_findings,
    unknown_risks,
)

import json
import os
import sys
from pathlib import Path

def load_findings(path: Path) -> dict:
    """Parse the scanner's JSON, tolerating a banner printed before the document.

    The scanner prints a version line on stdout before the JSON document in some
    versions, so slicing from the first brace is more durable than assuming the file
    is pure JSON.
    """
    raw = path.read_text(encoding="utf-8")
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in {path}; the scan produced no parseable output")
    return json.loads(raw[start:])


def collect(findings: dict) -> list[dict]:
    """Every risk in the payload, via the shared contract.

    This read the 0.5.x shape — a mapping of scanned path to a record carrying
    `issues` — until 0.6.0 replaced it with `{"scan_path_responses": [...]}`, whose one
    top-level value is a LIST. The old loop skipped anything that was not a dict, so it
    returned empty unconditionally and every summary read "No findings." regardless of
    content. It parsed cleanly, so the FINDINGS UNREADABLE guard never fired either.
    Silent, and indistinguishable from a clean scan.
    """
    parsed = iter_findings(findings)
    # A risk name outside the pinned vocabulary already blocks (it is not exempt), but
    # nothing told anyone the scanner's catalog had moved. Saying so is what makes the
    # ten-name list in the contract load-bearing rather than decorative.
    unrecognised = unknown_risks(
        row["risk"] for row in parsed if not row["risk"].startswith("scan_error:"))
    rows = []
    for row in parsed:
        rows.append(
            {
                "code": row["risk"],
                "severity": "blocking" if row["blocking"] else "warning",
                "skill": row["skill"],
                "message": row["evidence"],
                "critical": row["blocking"],
                "score": row["score"],
                "unknown": row["risk"] in unrecognised,
            }
        )
    # iter_findings already orders blocking-first, then by risk name, then skill.
    return rows


def emit_annotations(rows: list[dict]) -> None:
    """One annotation per warning finding.

    Deliberately `::warning::` and not `::error::`: an error annotation does not by
    itself fail a job, but it renders as a failure to a reader, and the owner's rule is
    that warnings are visible without ever reading as blocking. Criticals are left to
    the gating step, which fails the job and says so itself.
    """
    for row in rows:
        if row["unknown"]:
            print(
                f"::warning title=UNKNOWN RISK NAME {row['code']} in {row['skill']}::"
                f"The scanner reported a risk this build's vocabulary does not carry, so "
                f"registry_scan_contract.py is out of date with the scanner's catalog. It "
                f"blocks the gate either way. {row['message']}"
            )
            continue
        if row["critical"]:
            continue
        print(
            f"::warning title={row['code']} ({row['severity']}) in {row['skill']}"
            f"::{row['message']}"
        )


def render_table(rows: list[dict]) -> str:
    lines = [
        "| Class | Risk | Score | Skill | Finding |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        klass = "**blocks the build**" if row["critical"] else "does not block"
        name = f"`{row['code']}`" + (" **(UNKNOWN to this build)**" if row["unknown"] else "")
        score = "—" if row["score"] is None else f"{row['score']}/1000"
        lines.append(
            f"| {klass} | {name} | {score} | `{row['skill']}` | {row['message']} |"
        )
    return "\n".join(lines)


def write_summary(rows: list[dict]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    body = ["## Registry scan findings", ""]
    body.append(
        "Non-blocking risks do not fail the build; every other risk does. Every finding below is "
        "real — warnings are surfaced for a human or a review agent to judge, not "
        "suppressed. The full machine-readable finding set is attached to this run as "
        "the `registry-scan-findings` artifact."
    )
    body.append("")
    if rows:
        criticals = sum(1 for row in rows if row["critical"])
        body.append(
            f"{len(rows)} finding(s): {criticals} critical, {len(rows) - criticals} warning."
        )
        body.append("")
        body.append(render_table(rows))
    else:
        body.append("No findings.")
    body.append("")

    text = "\n".join(body)
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <findings.json>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        findings = load_findings(path)
    except (OSError, ValueError) as exc:
        # Not a finding — a broken pipeline. Silence here would be indistinguishable
        # from a clean scan, which is the exact confusion this gate exists to remove.
        print(
            "::error title=REGISTRY SCAN FINDINGS UNREADABLE::"
            f"Could not read the scanner's JSON output ({exc}). Findings were not "
            f"surfaced; do not read this run as 'no warnings'. {STALE_PIN_HINT}",
            file=sys.stderr,
        )
        return 1

    try:
        rows = collect(findings)
    except UnrecognisedPayload as exc:
        # Valid JSON in a shape we cannot read is the 0.5.x failure exactly: it parsed,
        # it yielded nothing, and "No findings." was printed over every run for a week.
        print(
            "::error title=REGISTRY SCAN FINDINGS UNREADABLE::"
            f"The scanner's JSON is not a shape this build recognises ({exc}). Findings "
            f"were not surfaced; do not read this run as 'no warnings'. {STALE_PIN_HINT}",
            file=sys.stderr,
        )
        return 1
    emit_annotations(rows)
    write_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
