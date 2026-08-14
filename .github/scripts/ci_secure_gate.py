#!/usr/bin/env python3
"""The ci-secure CI gate: red on any failed security fact, loud on findings.

Runs the ci-secure engine against the repository root, then applies the gate:

  facts    -> deterministic pass/fail security checks; ANY fail exits 1.
  findings -> severity-rated pattern matches; they never exit non-zero, but
              each one becomes a ::warning:: annotation and a summary row,
              so accepting one is a visible decision, not silence.

Green here means a scan ran and returned a verdict. That is a stronger claim
than "the engine exited 0", and the difference is the whole job of this file:
the engine is deliberately crash-tolerant, so its facts layer degrades to an
empty `facts` list with a null score rather than taking the scan down, and an
empty list contains no failures. Left unchecked, "the scoring layer crashed"
would render as "0/0 facts pass" and go green - a scan that measured nothing,
reported as clean. Every degraded shape is therefore named and turned red
below. A check that did not run is not a check that passed.

This file is stdlib only. The engine it runs is NOT: `scan.py` imports PyYAML
and exits 1 with an install hint without it, so the job needs `pip install
pyyaml` (or an image that already has it) as well as a checkout and python3.

Portability. This file must live at `.github/scripts/` (it takes the scan root
from GITHUB_WORKSPACE, falling back to two directories up). The engine defaults
to an in-tree checkout of starslingdev/skills, which is what this repo has; any
repo that vendors the gate without vendoring the engine points CI_SECURE_ENGINE
at a fetched `scan.py` instead. A missing engine is a red build naming the path
it looked in, never a silent pass.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("GITHUB_WORKSPACE")
                 or Path(__file__).resolve().parents[2])
ENGINE = Path(os.environ.get("CI_SECURE_ENGINE")
              or REPO_ROOT / "skills" / "ci-secure" / "scripts" / "scan.py")

# The job's own timeout-minutes would eventually catch a hung engine, but only
# as an unexplained cancellation; failing here names the cause on the check run.
ENGINE_TIMEOUT_S = 600

# The outcomes the engine's config-facts layer is known to emit. Anything else
# is a contract change between engine and gate, and must not be quietly bucketed
# as "not a fail" - that is how a new failure state ships green.
KNOWN_OUTCOMES = {"pass", "fail", "unmeasured"}

# P14.11 (impostor action SHA) is the one detector that needs network + a GitHub
# token. Neither job here has one, so it is turned off explicitly rather than
# left on `auto`: otherwise whether a security check runs would depend on
# whichever runner image happened to ship an authenticated `gh`. The engine
# records the decision in `gh_checks`, which this gate renders.
ENGINE_ARGS = ["--gh-impostor", "off"]


def fail(message: str) -> int:
    """Annotate the check run with a red reason. Returns 1, the gate's exit code."""
    print(f"::error::{message}")
    return 1


def main() -> int:
    if not ENGINE.is_file():
        return fail(
            f"ci-secure engine not found at {ENGINE} - point CI_SECURE_ENGINE at a "
            "checkout's skills/ci-secure/scripts/scan.py. The gate cannot pass "
            "without a verdict (a scan that did not run is not a scan that passed)")

    try:
        result = subprocess.run(
            [sys.executable, str(ENGINE), "--root", str(REPO_ROOT), *ENGINE_ARGS],
            capture_output=True, text=True, timeout=ENGINE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return fail(f"ci-secure engine did not finish within {ENGINE_TIMEOUT_S}s - "
                    "the gate cannot pass without a verdict")

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return fail("ci-secure engine failed to run - the gate cannot pass without "
                    "a verdict (a scan that did not run is not a scan that passed)")

    # Every key read below is one the engine always emits. Indexing rather than
    # .get()-with-a-default is deliberate: if the schema ever drops `findings` or
    # `scan_incomplete`, the gate must go red on the KeyError, not quietly report
    # zero coverage gaps - which is the exact false negative it exists to prevent.
    try:
        scan = json.loads(result.stdout)
        score = scan["security_score"]
        facts = score["facts"]
        findings = scan["findings"]
        incomplete = scan["scan_incomplete"]
    except (ValueError, KeyError, TypeError) as exc:
        sys.stderr.write(result.stdout[:2000])
        return fail(f"ci-secure engine produced no readable verdict ({exc!r}) - "
                    "the gate cannot pass without one")

    degraded = []
    if not scan.get("scanned_workflows"):
        degraded.append("no workflow files were scanned")
    if not facts or not score.get("scored_count"):
        degraded.append("the config-facts layer evaluated nothing")
    if score.get("score") is None:
        degraded.append("the engine returned no score")
    if score.get("reason"):
        degraded.append(str(score["reason"]))
    unknown = sorted(str(o) for o in {f.get("outcome") for f in facts} - KNOWN_OUTCOMES)
    if unknown:
        degraded.append(f"unrecognised fact outcome(s) {unknown} - this gate cannot "
                        "tell whether they are failures")

    failed = [f for f in facts if f["outcome"] == "fail"]
    unmeasured = [f for f in facts if f["outcome"] == "unmeasured"]

    lines = ["## ci-secure", "",
             f"score: **{score.get('score')}** "
             f"({score.get('passed')}/{score.get('scored_count')} facts pass, "
             f"{scan.get('scanned_workflows')} workflow file(s) scanned)", ""]

    # An unrecognised outcome renders as itself rather than folding into FAIL:
    # the summary a human reads must never disagree with the exit code.
    marks = {"pass": "PASS", "fail": "**FAIL**", "unmeasured": "UNMEASURED"}
    for f in facts:
        mark = marks.get(f["outcome"], f"**{str(f['outcome']).upper()}**")
        lines.append(f"- {mark} `{f['fact_id']}` - {f['evidence']}")
    if findings:
        lines += ["", "### Findings (surfaced, non-blocking)", ""]
        for f in findings:
            loc = f"{f.get('workflow_file', '?')}:{f.get('line', '?')}"
            lines.append(f"- {f.get('severity', '?')} `{f.get('pattern', '?')}` {f.get('title', '')} ({loc})")
            print(f"::warning file={f.get('workflow_file', '')},line={f.get('line', 1)}::"
                  f"ci-secure {f.get('severity', '?')} {f.get('pattern', '?')}: {f.get('title', '')}")
    # A detector that did not run is reported, not omitted: "no findings from
    # P14.11" and "P14.11 never ran" must not look the same to a reader.
    gh_checks = scan.get("gh_checks") or {}
    if gh_checks:
        lines += ["", "### Network-gated detectors", ""]
        lines += [f"- `{k}`: {v}" for k, v in sorted(gh_checks.items())]
    # Individually unmeasured facts do not block - several have honest causes
    # that would otherwise leave the gate permanently red - but they are never
    # silent, and a run where NOTHING was measured is caught by `degraded` above.
    for f in unmeasured:
        print(f"::warning::ci-secure fact unmeasured: {f['fact_id']} - {f['evidence']}")
    if incomplete:
        lines += ["", f"### Coverage gaps: {incomplete}"]

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    red = False
    for reason in degraded:
        red = bool(fail(f"ci-secure produced no usable verdict: {reason}"))
    if incomplete:
        red = bool(fail(f"ci-secure scan incomplete: {incomplete} - a skipped "
                        "workflow shown as clean is a false negative"))
    for f in failed:
        red = bool(fail(f"ci-secure fact failed: {f['fact_id']} - {f['evidence']}"))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
