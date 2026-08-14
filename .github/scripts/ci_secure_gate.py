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
import traceback
from pathlib import Path

REPO_ROOT = Path(os.environ.get("GITHUB_WORKSPACE")
                 or Path(__file__).resolve().parents[2])
ENGINE = Path(os.environ.get("CI_SECURE_ENGINE")
              or REPO_ROOT / "skills" / "ci-secure" / "scripts" / "scan.py")

# The job's own timeout-minutes would eventually catch a hung engine, but only
# as an unexplained cancellation; failing here names the cause on the check run.
# It must therefore fire FIRST: at parity with the job's 10 minutes this path was
# unreachable, because checkout and pip had already spent a minute of that clock.
ENGINE_TIMEOUT_S = 420

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


# GitHub rejects a step summary over 1 MiB, so an oversized one is not a big
# summary - it is no summary. The budget is in BYTES because that is what GitHub
# counts: a character budget passes a scan whose evidence strings are non-ASCII
# (em dashes are 3 bytes, and on a fork PR the filenames in them are attacker
# chosen) while the upload is several times over the real limit. Headroom is left
# for whatever another step in the same job already appended to the same file.
SUMMARY_LIMIT_BYTES = 900_000


def esc(value: object, *, prop: bool = False) -> str:
    """Escape untrusted text before it goes into a ::workflow command::.

    Everything this gate reports about is read out of scanned workflow files, and
    on a fork pull request an attacker writes those files - including their NAMES,
    which the engine reports verbatim. GitHub parses `::command::` sequences out
    of a step's stdout line by line, so an unescaped newline in a filename lets
    that filename emit commands of its own. `::stop-commands::` is the one that
    matters: it switches off command parsing for the rest of the step, which
    would silence the `::error::` annotations this gate emits for real failures.
    The exit code is computed in Python and cannot be touched this way, so a
    build still goes red - but "the finding does not block" must not decay into
    "the finding does not appear", which is this gate's whole premise.

    Escapes are the ones GitHub decodes: %25 first (or it would double-encode the
    others), then CR and LF. Property values additionally escape `:` and `,`,
    which terminate the property list.
    """
    text = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def flat(value: object) -> str:
    """Collapse untrusted text to one line for the Markdown job summary.

    Same attacker-controlled source as `esc`, different sink: a newline here does
    not run a command, it breaks out of the list item and lets a crafted filename
    render its own Markdown - a summary that reads as a clean pass.
    """
    return " ".join(str(value).split())


def budget(body: str) -> str:
    """Trim the summary to GitHub's byte limit, cutting on a line boundary."""
    if len(body.encode("utf-8")) <= SUMMARY_LIMIT_BYTES:
        return body
    clipped = body.encode("utf-8")[:SUMMARY_LIMIT_BYTES].decode("utf-8", "ignore")
    # Cut back to the last complete line so the final row is not half-rendered.
    head, sep, _ = clipped.rpartition("\n")
    return (head if sep else clipped) + "\n\n_(summary truncated; see the step log)_"


def quote(text: str) -> None:
    """Echo untrusted engine output to the log, readably, without running it.

    Percent-encoding this would be wrong: GitHub decodes `%0A` only inside a
    workflow command, never in an ordinary log line, so escaping an engine
    traceback turns the thing a maintainer reads when the gate goes red for a
    NON-security reason into one unreadable blob. Prefixing each line keeps it
    legible while guaranteeing no line can begin with `::`, which is the only
    property that matters here.
    """
    for line in text.splitlines():
        print(f"engine| {line}", file=sys.stderr)


def fail(message: str) -> int:
    """Annotate the check run with a red reason. Returns 1, the gate's exit code."""
    print(f"::error::{esc(message)}")
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
            capture_output=True, text=True, errors="replace",
            timeout=ENGINE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return fail(f"ci-secure engine did not finish within {ENGINE_TIMEOUT_S}s - "
                    "the gate cannot pass without a verdict")

    if result.returncode != 0:
        quote(result.stderr[:4000])
        return fail("ci-secure engine failed to run - the gate cannot pass without "
                    "a verdict (a scan that did not run is not a scan that passed)")

    # Every key read below is one the engine always emits. Indexing rather than
    # .get()-with-a-default is deliberate: if the schema ever drops `findings` or
    # `scan_incomplete`, the gate must go red on the KeyError, not quietly report
    # zero coverage gaps - which is the exact false negative it exists to prevent.
    # (Fields the gate only renders, never decides on, are read with .get().)
    try:
        scan = json.loads(result.stdout)
        score = scan["security_score"]
        facts = score["facts"]
        findings = scan["findings"]
        incomplete = scan["scan_incomplete"]
        # A match the engine FOUND and then could not anchor to a line, so did not
        # report. The engine ranks this the same as an unreadable file, and its own
        # coverage predicate requires both to be empty - so a gate that blocks on
        # `scan_incomplete` and ignores this one is inconsistent about the same
        # class of hole. A finding produced and discarded is a false negative.
        dropped = scan["dropped_matches"]
        gh_checks = scan["gh_checks"]
        if not isinstance(facts, list) or not all(isinstance(f, dict) for f in facts):
            raise TypeError(f"facts is not a list of objects: {type(facts).__name__}")
        for f in facts:
            # Read now, inside the guarded block, so a malformed fact renders as
            # "no readable verdict" instead of an unannotated traceback later.
            f["outcome"], f["fact_id"], f["evidence"]
    except (ValueError, KeyError, TypeError) as exc:
        quote(result.stdout[:2000])
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
    if not gh_checks:
        degraded.append("the engine reported no network-gated detector status")
    unknown = sorted(str(o) for o in {f["outcome"] for f in facts} - KNOWN_OUTCOMES)
    if unknown:
        degraded.append(f"unrecognised fact outcome(s) {unknown} - this gate cannot "
                        "tell whether they are failures")

    failed = [f for f in facts if f["outcome"] == "fail"]
    unmeasured = [f for f in facts if f["outcome"] == "unmeasured"]

    # `applicable_count` is in the headline, not just `scored_count`: when a fact
    # cannot be measured the engine drops it from BOTH sides of the ratio, so a
    # repo with one measurable fact and eleven unmeasurable ones still reads
    # "1/1 facts pass" at a score of 100. True, and rhetorically false - the
    # denominator shrank to hide the gap. The engine states the gap in `caveat`,
    # which is rendered below.
    #
    # Every interpolation here is flattened. These four fields come out of the
    # engine's JSON, and on a fork pull request the engine is attacker code: a
    # newline in any of them would start a new line in `body`, which is printed
    # to stdout where Actions parses `::workflow commands::`.
    lines = ["## ci-secure", "",
             f"score: **{flat(score.get('score'))}** "
             f"({flat(score.get('passed'))}/{flat(score.get('scored_count'))} facts pass"
             f" of {flat(score.get('applicable_count'))} applicable, "
             f"{flat(scan.get('scanned_workflows'))} workflow file(s) scanned)", ""]
    if score.get("caveat"):
        lines += [f"> {flat(score['caveat'])}", ""]

    # An unrecognised outcome renders as itself rather than folding into FAIL:
    # the summary a human reads must never disagree with the exit code.
    marks = {"pass": "PASS", "fail": "**FAIL**", "unmeasured": "UNMEASURED"}
    for f in facts:
        mark = marks.get(f["outcome"], f"**{flat(f['outcome']).upper()}**")
        lines.append(f"- {mark} `{flat(f['fact_id'])}` - {flat(f['evidence'])}")
    if findings:
        lines += ["", "### Findings (surfaced, non-blocking)", ""]
        for f in findings:
            loc = f"{flat(f.get('workflow_file', '?'))}:{flat(f.get('line', '?'))}"
            lines.append(f"- {flat(f.get('severity', '?'))} `{flat(f.get('pattern', '?'))}` "
                         f"{flat(f.get('title', ''))} ({loc})")
            props = [f"file={esc(f.get('workflow_file', ''), prop=True)}"]
            if f.get("line") is not None:
                props.append(f"line={esc(f['line'], prop=True)}")
            print(f"::warning {','.join(props)}::"
                  f"ci-secure {esc(f.get('severity', '?'))} {esc(f.get('pattern', '?'))}: "
                  f"{esc(f.get('title', ''))}")
    # A detector that did not run is reported, not omitted: "no findings from
    # P14.11" and "P14.11 never ran" must not look the same to a reader. Indexed,
    # not `.get(... ) or {}`: this gate always passes `--gh-impostor off`, so the
    # engine always has a status to report, and an absent key is schema drift -
    # which would make those two cases look identical again.
    lines += ["", "### Network-gated detectors", ""]
    lines += [f"- `{flat(k)}`: {flat(v)}" for k, v in sorted(gh_checks.items())]
    # Individually unmeasured facts do not block - several have honest causes
    # that would otherwise leave the gate permanently red - but they are never
    # silent, and a run where NOTHING was measured is caught by `degraded` above.
    for f in unmeasured:
        print(f"::warning::ci-secure fact unmeasured: {esc(f['fact_id'])} - {esc(f['evidence'])}")
    if incomplete:
        lines += ["", f"### Coverage gaps: {flat(incomplete)}"]

    body = "\n".join(lines)
    print(body)

    # The verdict is emitted BEFORE the summary is written. The summary is a
    # cosmetic sink on a file GitHub provisions, and an OSError writing it used to
    # take the whole failure list down with it - the operator got a traceback
    # about a summary file instead of the security failure that caused the red.
    red = False
    for reason in degraded:
        fail(f"ci-secure produced no usable verdict: {reason}")
        red = True
    if incomplete:
        fail(f"ci-secure scan incomplete: {incomplete} - a skipped workflow shown "
             "as clean is a false negative")
        red = True
    if dropped:
        fail(f"ci-secure dropped {len(dropped)} match(es) it could not anchor to a "
             f"line: {dropped} - a finding the detector produced and discarded is a "
             "false negative")
        red = True
    for f in failed:
        fail(f"ci-secure fact failed: {f['fact_id']} - {f['evidence']}")
        red = True

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(budget(body) + "\n")
        except OSError as exc:
            # Never fatal: the verdict above is already on the check run.
            print(f"::warning::could not write the job summary ({exc!r}); the "
                  "findings above are in the step log")
    return 1 if red else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:                              # noqa: BLE001
        # Fail closed AND fail legibly. An uncaught traceback already exits
        # non-zero, so the build was never at risk - but it reaches the check run
        # as a red with no stated cause, which is the same "unexplained red" this
        # gate argues against everywhere else.
        traceback.print_exc()
        sys.exit(fail(f"ci-secure gate crashed ({exc!r}) - the gate cannot pass "
                      "without a verdict"))
