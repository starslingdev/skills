#!/usr/bin/env python3
"""Surface every registry-scan finding, without letting a warning block the build.

`.github/workflows/registry-scan.yml` gates on Snyk Agent Scan's critical class only
— the E-codes. Warnings (W-codes) are real findings we want a human or a review agent
to look at, but they do not fail CI. A finding nobody can see is the failure this whole
gate exists to prevent, so "does not block" must not decay into "does not appear".

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
gating pass runs with the ignore list, and the scanner strips ignored findings from its
printed report as well as from its exit status.

Exit status: 0 whatever the findings are — gating is the next step's job, and warnings
must never block. It exits 1 only if the scan output cannot be parsed at all, because at
that point the surfacing is broken and silence would look exactly like "nothing found".

Usage:  python3 .github/scripts/registry_scan_report.py <findings.json>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Snyk Agent Scan issue codes carry their class in the prefix: E = critical class
# (what this gate fails on), W = warning class (surfaced, never blocking), X = a scan
# runtime failure. See https://github.com/snyk/agent-scan/blob/main/docs/issue-codes.md
CRITICAL_PREFIX = "E"


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


def skill_name(result: dict, issue: dict) -> str:
    """Best-effort name of the skill an issue fired on.

    `reference` is (server_index, entity_index); for a skills directory each skill is
    one "server" entry. Falls back to the scanned path when the shape is unfamiliar,
    which is a labelling detail only — the finding is reported either way.
    """
    reference = issue.get("reference")
    servers = result.get("servers") or []
    if isinstance(reference, (list, tuple)) and reference and isinstance(reference[0], int):
        index = reference[0]
        if 0 <= index < len(servers):
            name = (servers[index] or {}).get("name")
            if name:
                return str(name)
    return str(result.get("path") or "unknown")


def severity_of(issue: dict) -> str:
    extra = issue.get("extra_data") or {}
    return str(extra.get("severity") or "unknown")


def collect(findings: dict) -> list[dict]:
    rows = []
    for result in findings.values():
        if not isinstance(result, dict):
            continue
        for issue in result.get("issues") or []:
            code = str(issue.get("code") or "")
            rows.append(
                {
                    "code": code,
                    "severity": severity_of(issue),
                    "skill": skill_name(result, issue),
                    "message": " ".join(str(issue.get("message") or "").split()),
                    "critical": code.startswith(CRITICAL_PREFIX),
                }
            )
    # Critical first, then by code, so the thing that will fail the build reads first.
    rows.sort(key=lambda r: (not r["critical"], r["code"], r["skill"]))
    return rows


def emit_annotations(rows: list[dict]) -> None:
    """One annotation per warning finding.

    Deliberately `::warning::` and not `::error::`: an error annotation does not by
    itself fail a job, but it renders as a failure to a reader, and the owner's rule is
    that warnings are visible without ever reading as blocking. Criticals are left to
    the gating step, which fails the job and says so itself.
    """
    for row in rows:
        if row["critical"]:
            continue
        print(
            f"::warning title={row['code']} ({row['severity']}) in {row['skill']}"
            f"::{row['message']}"
        )


def render_table(rows: list[dict]) -> str:
    lines = [
        "| Class | Code | Severity | Skill | Finding |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        klass = "**critical — blocks**" if row["critical"] else "warning"
        lines.append(
            f"| {klass} | `{row['code']}` | {row['severity']} | `{row['skill']}` | "
            f"{row['message']} |"
        )
    return "\n".join(lines)


def write_summary(rows: list[dict]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    body = ["## Registry scan findings", ""]
    body.append(
        "Warnings do not block; critical (E-class) findings do. Every finding below is "
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
            "surfaced; do not read this run as 'no warnings'.",
            file=sys.stderr,
        )
        return 1

    rows = collect(findings)
    emit_annotations(rows)
    write_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
