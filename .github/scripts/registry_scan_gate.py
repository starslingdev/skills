#!/usr/bin/env python3
"""Decide the registry-scan verdict from the scanner's JSON, without a second scan.

`.github/workflows/registry-scan.yml` used to ask the scanner twice about `skills/`:
once with `--json` and no exemption list (the only pass that can show an exempt
finding, because `--ignore-risks` nulls those risks out of the response before it is
printed) and once more under `--ci --ignore-risks` for the exit code. Every call is a
network request against the public tier of a third-party service, and that tier has a
daily allowance that is undocumented and, since 2026-09-14, small enough that a day of
pushes exhausts it. Each exhausted call was one more red check that said "DID NOT
COMPLETE" over a tree nobody had looked at.

The second call bought nothing the first had not already said. The scanner's own
`--ci` logic is a few lines over the same document — "any risk left after the
exemptions, or any error flagged `is_failure`" — so this script applies it here, to
the JSON the unfiltered pass wrote, and the exemption list is read from the shared
contract rather than passed to the scanner. That halves the calls this workflow spends
per run, and it removes a way the policy could go silently inert: the scanner drops an
exemption name it does not recognise with a yellow warning and exits 0, whereas this
script refuses to run under one.

It writes the same two `$GITHUB_OUTPUT` keys the "Report the coverage gap" step reads
(`scan_class`, `scan_exit`) and prints the same titled `::error` annotations the
in-workflow gate printed, so nothing downstream — the verdict step, or a review agent
reading the checks API — has to change. The classes, in the order they are decided:

  finding       a non-exempt risk is present — including a name outside the pinned
                vocabulary, which is not exempt and therefore blocks. Exit 1. This IS
                a security finding, and it wins over every other class: a quota error
                beside a real finding is still a real finding.
  quota         an analysis error carrying the scanner's daily-cap text (HTTP 429 on
                the public tier). Nothing was verified; it resets daily. Exit 1 —
                a scan that did not run must not be green. NOT a finding.
  operational   any other error the scanner flags `is_failure` — the X-codes its own
                exit line would have named. The scanner broke; nothing was fully
                checked. Exit 1. NOT a finding.
  indeterminate the document reads clean but the scanner did not exit 0, so this run
                cannot say the document is complete. Exit 1, without claiming to be a
                security result.
  clean         no non-exempt risk and no failure, from a scanner that exited 0. Exit 0.
  did-not-run   the document is missing, unparseable, or not a shape the contract
                reads. Exit 1. NOT a finding, and never clean.
  exemption-stale  the contract exempts a name the pinned vocabulary does not carry,
                so the policy is unknown. Exit 1 before reading anything.

Classification is structural — it reads the JSON's risk keys and error objects — never
a substring search over prose. The evidence text of a scanned skill is untrusted, and
these are CI-security skills whose own text talks about found risks as a matter of
course.

Usage:  python3 .github/scripts/registry_scan_gate.py <findings.json>
        SCANNER_EXIT=<n>   the unfiltered pass's exit code (default 0), published as
                           `scan_exit` and consulted only when the document is clean.
"""
from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from registry_scan_contract import (  # noqa: E402
    NON_BLOCKING_RISKS,
    SERVER_RISKS,
    SKILL_RISKS,
    STALE_PIN_HINT,
    UnrecognisedPayload,
    iter_findings,
    unknown_risks,
)

import json
import os
import sys
from pathlib import Path

NOT_A_FINDING = "This is a coverage gap, not a security finding."


def _output(key: str, value: str) -> None:
    """Append a `key=value` line to `$GITHUB_OUTPUT`, and echo it for the log."""
    line = f"{key}={value}"
    print(line)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _annotate(title: str, message: str) -> None:
    print(f"::error title={title}::{message}")


def load_document(path: Path) -> dict:
    """Parse the scanner's JSON, tolerating a banner printed before the document."""
    raw = path.read_text(encoding="utf-8")
    start = raw.find("{")
    if start == -1:
        raise ValueError("no JSON object in the scan output")
    document = json.loads(raw[start:])
    if not isinstance(document, dict):
        raise ValueError("the scan output is not a JSON object")
    return document


def classify(rows: list[dict], scanner_exit: int) -> tuple[str, list[dict]]:
    """The verdict for a parsed finding set, and the rows that decided it."""
    errors = [r for r in rows if r["risk"].startswith("scan_error:")]
    risks = [r for r in rows if not r["risk"].startswith("scan_error:")]

    blocking = [r for r in risks if r["blocking"]]
    if blocking:
        return "finding", blocking
    quota = [r for r in errors if r.get("quota")]
    if quota:
        return "quota", quota
    failures = [r for r in errors if r["blocking"]]
    if failures:
        return "operational", failures
    if scanner_exit != 0:
        return "indeterminate", []
    return "clean", risks


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <findings.json>", file=sys.stderr)
        return 2
    scan_path = os.environ.get("SCAN_PATH", "skills")
    try:
        scanner_exit = int(os.environ.get("SCANNER_EXIT") or 0)
    except ValueError:
        scanner_exit = -1
    _output("scan_exit", str(scanner_exit))

    # The policy must be checked BEFORE the document is read. A retired exemption name
    # is how the last outage hid — the scanner dropped it with a warning and scanned
    # under a policy nobody chose. Offline there is no scanner to warn, so the check
    # is ours.
    stale = set(NON_BLOCKING_RISKS) - set(SKILL_RISKS) - set(SERVER_RISKS)
    if stale:
        _output("scan_class", "exemption-stale")
        _annotate(
            "REGISTRY SCAN EXEMPTION IS STALE",
            f"NON_BLOCKING_RISKS in .github/scripts/registry_scan_contract.py names "
            f"{sorted(stale)}, which the pinned vocabulary does not carry, so the gating "
            f"rule is unknown and nothing under '{scan_path}' was gated. Update the "
            f"contract to the scanner's current catalog. {NOT_A_FINDING} {STALE_PIN_HINT}",
        )
        return 1

    path = Path(argv[1])
    try:
        rows = iter_findings(load_document(path))
    except (OSError, ValueError, UnrecognisedPayload) as exc:
        _output("scan_class", "did-not-run")
        how = (
            f"the scanner exited {scanner_exit} and "
            if scanner_exit != 0 else ""
        )
        _annotate(
            "REGISTRY SCAN DID NOT RUN",
            f"{how}its output could not be read ({exc}), so NOTHING under "
            f"'{scan_path}' was checked. A usage error here usually means the scanner "
            f"renamed a flag out from under us; a missing file means it never wrote "
            f"one. {NOT_A_FINDING} Do not read this run as clean. {STALE_PIN_HINT}",
        )
        return 1

    scan_class, decisive = classify(rows, scanner_exit)
    _output("scan_class", scan_class)

    if scan_class == "finding":
        unrecognised = unknown_risks(r["risk"] for r in decisive)
        names = ", ".join(
            f"{r['risk']} ({r['score']}/1000) in {r['skill']}" for r in decisive)
        moved = (
            f" The name(s) {sorted(unrecognised)} are outside this build's pinned "
            f"vocabulary, so the scanner's catalog has moved as well: update "
            f"registry_scan_contract.py once the finding itself is dealt with."
            if unrecognised else ""
        )
        _annotate(
            "REGISTRY SCAN FINDING",
            f"The scan reported a blocking risk under '{scan_path}': {names}. This IS "
            f"a security finding; its evidence is in this step's log below and in the "
            f"registry-scan-findings artifact.{moved}",
        )
        for row in decisive:
            print(f"  {row['risk']}  {row['score']}/1000  {row['skill']}: {row['evidence']}")
        print("Gate failed: a blocking risk is present.")
        return 1

    if scan_class == "quota":
        detail = decisive[0]["evidence"]
        _annotate(
            "REGISTRY SCAN QUOTA EXHAUSTED — NOT A FINDING",
            f"The scanner's public tier has a daily usage cap, and this run hit it: the "
            f"analysis endpoint answered HTTP 429 (the scanner's code X007), so nothing "
            f"under '{scan_path}' was verified. It resets daily; re-run this check "
            f"tomorrow, or push again after the reset. The allowance is undocumented "
            f"and shared by every run of this workflow that day. The cap is a property "
            f"of the public tier that SNYK_TOKEN authenticates against, so the unlock is "
            f"enabling Agent Scan on the Snyk tenant and running the scanner with a "
            f"tenant push key (or the contact link the scanner prints), not a different "
            f"token. {NOT_A_FINDING} Do not read it as a broken scanner either — the "
            f"scanner said: {detail}",
        )
        return 1

    if scan_class == "operational":
        codes = sorted({r["risk"].split(":", 1)[1] for r in decisive})
        _annotate(
            "REGISTRY SCAN DID NOT COMPLETE",
            f"The scanner reported a runtime failure (codes: {', '.join(codes)}), not a "
            f"finding. Nothing under '{scan_path}' was fully checked. {NOT_A_FINDING} "
            f"{STALE_PIN_HINT}",
        )
        for row in decisive:
            print(f"  {row['risk']}  {row['skill']}: {row['evidence']}")
        return 1

    if scan_class == "indeterminate":
        _annotate(
            "REGISTRY SCAN RESULT UNCLASSIFIED",
            f"The scanner's document reads clean but the scanner exited {scanner_exit}, "
            f"so this run cannot say whether the document is complete. Read the "
            f"unfiltered scan step's log before treating it as either a clean result or "
            f"a runtime failure. {NOT_A_FINDING} {STALE_PIN_HINT}",
        )
        return 1

    exempt = len(decisive)
    print(
        f"Gate passed: no blocking risk under '{scan_path}' "
        f"({exempt} exempt finding(s) surfaced by the report step, none blocking)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
