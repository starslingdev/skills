#!/usr/bin/env python3
"""The ci-secure CI gate: red on any failed security fact, loud on findings.

Runs the ci-secure engine against the repository root, then applies the gate:

  facts    -> deterministic pass/fail security checks; ANY fail exits 1.
  findings -> severity-rated pattern matches; they never exit non-zero, but
              each one becomes a ::warning:: annotation and a summary row,
              so accepting one is a visible decision, not silence.

Stdlib only, like the engine itself. Portable: point ENGINE at any checkout
of starslingdev/skills (this repo uses its in-tree copy).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPO_ROOT / "skills" / "ci-secure" / "scripts" / "scan.py"


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(ENGINE), "--root", str(REPO_ROOT)],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        print("::error::ci-secure engine failed to run - the gate cannot pass "
              "without a verdict (a scan that did not run is not a scan that "
              "passed)")
        return 1
    scan = json.loads(result.stdout)

    facts = scan["security_score"]["facts"]
    failed = [f for f in facts if f["outcome"] == "fail"]
    findings = scan.get("findings", [])
    incomplete = scan.get("scan_incomplete", [])

    lines = ["## ci-secure", "",
             f"score: **{scan['security_score']['score']}** "
             f"({scan['security_score']['passed']}/{scan['security_score']['scored_count']} facts pass, "
             f"{scan['scanned_workflows']} workflow file(s) scanned)", ""]

    for f in facts:
        mark = "PASS" if f["outcome"] == "pass" else "**FAIL**"
        lines.append(f"- {mark} `{f['fact_id']}` - {f['evidence']}")
    if findings:
        lines += ["", "### Findings (surfaced, non-blocking)", ""]
        for f in findings:
            loc = f"{f.get('workflow_file', '?')}:{f.get('line', '?')}"
            lines.append(f"- {f.get('severity', '?')} `{f.get('pattern', '?')}` {f.get('title', '')} ({loc})")
            print(f"::warning file={f.get('workflow_file', '')},line={f.get('line', 1)}::"
                  f"ci-secure {f.get('severity', '?')} {f.get('pattern', '?')}: {f.get('title', '')}")
    if incomplete:
        lines += ["", f"### Coverage gaps: {incomplete}"]
        print(f"::error::ci-secure scan incomplete: {incomplete} - a skipped "
              "workflow shown as clean is a false negative")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    for f in failed:
        print(f"::error::ci-secure fact failed: {f['fact_id']} - {f['evidence']}")
    if failed or incomplete:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
