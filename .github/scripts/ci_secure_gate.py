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

Two trees, and the difference between them is the whole security argument. The
SCAN ROOT is the repository under examination, taken from GITHUB_WORKSPACE; on
a fork pull request an attacker writes every byte of it. The ENGINE and the
RULE are the code this gate executes, and they are resolved relative to THIS
FILE - never from the scan root. A gate that loaded its engine from the tree it
was auditing would be running the code it is supposed to be judging, and would
print whatever verdict that code returned.

CI_SECURE_ENGINE is the one deliberate redirect, for a repository that vendors
ci-secure into a directory of its own instead of checking out this whole tree.
It is safe only while the workflow that sets it is pinned to the base
repository's definition - a fork that can edit the workflow `env:` can point it
back into the workspace and reopen the hole this resolution closes.

`config.py` follows the ENGINE, not this file: it is loaded from the directory
of whichever engine was resolved, falling back to this tree's copy. One rule
serves both layouts - here the gate sits at `.github/scripts/` and the engine
under `skills/ci-secure/scripts/`, while a vendored install puts engine and
config in one directory - and the redirect travels with CI_SECURE_ENGINE, so
the rule is always in the same trust class as the engine it configures rather
than a third thing that can be aimed somewhere else on its own.
"""
import importlib.util
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

# The tree being AUDITED. Attacker-writable on a fork PR; only ever scan input.
REPO_ROOT = Path(os.environ.get("GITHUB_WORKSPACE")
                 or Path(__file__).resolve().parents[2])

# The tree the gate EXECUTES from, relative to this file: `.github/scripts/` ->
# repo root -> the in-tree skill. Deliberately not REPO_ROOT-relative.
_GATE_TREE = Path(__file__).resolve().parents[2]
_ENGINE_DEFAULT = _GATE_TREE / "skills" / "ci-secure" / "scripts" / "scan.py"
_CONFIG_FALLBACK = _ENGINE_DEFAULT.parent / "config.py"

ENGINE = Path(os.environ.get("CI_SECURE_ENGINE") or _ENGINE_DEFAULT)


def load_config():
    """Load `config.py` from beside the resolved engine, else from this tree.

    By file location, through importlib, rather than by package import: a
    vendored install carries the engine and this gate and nothing else - no
    test helpers, no conftest, no `skills.ci_secure` package on sys.path.
    Anything that works here only because the repository's test tree happens
    to be importable would fail on every adopter.
    """
    for candidate in (ENGINE.resolve().parent / "config.py", _CONFIG_FALLBACK):
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "ci_secure_gate_config", candidate)
        if spec is None or spec.loader is None:      # pragma: no cover - defensive
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, candidate
    return None, None


# Loading the rule EXECUTES code - `spec.loader.exec_module` - and in a vendored
# layout that code sits beside the engine rather than in this tree, which makes
# this the likeliest line in the file to raise, not the least. It runs at module
# scope, OUTSIDE main()'s handler, so a config.py with a syntax error or an
# import-time raise used to exit non-zero with a bare traceback and no
# `::error::` - the "unexplained red" this file argues against everywhere else.
# The failure is captured here and re-reported as a stated verdict in main().
#
# `BaseException`, not `Exception`, and that is the load-bearing part rather
# than defensive habit: `SystemExit` is not an `Exception`, so a config.py whose
# module body called `sys.exit(0)` ENDED THIS PROCESS RIGHT HERE with status 0
# and no output - a green gate over a scan that never ran. Narrowing this clause
# is a natural-looking tidy-up that brings that forged pass straight back, which
# is why `test_a_rule_that_exits_cleanly_on_load_cannot_forge_a_pass` pins it.
#
# Deliberately unannotated: a PEP 604 `BaseException | None` here would be
# evaluated at module scope, and this file is vendored into adopters' repos
# where python3 is whatever they have. That is the same break this PR fixed in
# the skill's own config.py.
try:
    CONFIG, CONFIG_PATH = load_config()
    CONFIG_ERROR = None
except BaseException as _exc:                                 # noqa: BLE001
    CONFIG, CONFIG_PATH, CONFIG_ERROR = None, None, _exc

# The job's own timeout-minutes would eventually catch a hung engine, but only
# as an unexplained cancellation; failing here names the cause on the check run.
# It must therefore fire FIRST: at parity with the job's 10 minutes this path was
# unreachable, because checkout and pip had already spent a minute of that clock.
#
# Overridable by env so the timeout can be exercised for real in a test rather
# than trusted. A test that has to sleep 420s does not get written, and an
# untested timeout is how this constant sat at parity with the job clock -
# unreachable - in the first place.
#
# The override can only ever SHORTEN the wait, and the clamp is what makes that
# true rather than a comment hoping it is. Without it a workflow could set the
# variable high enough to put the timeout back out of reach, which is exactly
# the regression this constant exists to prevent - and the invariant test reads
# the source literal, so it would not notice. A zero, a negative or a
# non-numeric falls back to the default, since "no timeout at all" is the
# failure being prevented, not a way to ask for one.
_TIMEOUT_CEILING = 420
try:
    ENGINE_TIMEOUT_S = int(os.environ.get("CI_SECURE_ENGINE_TIMEOUT_S", "")
                           or _TIMEOUT_CEILING)
except ValueError:
    ENGINE_TIMEOUT_S = _TIMEOUT_CEILING
if not 0 < ENGINE_TIMEOUT_S <= _TIMEOUT_CEILING:
    ENGINE_TIMEOUT_S = _TIMEOUT_CEILING

# P14.11 (impostor action SHA) is the one detector that needs network and a
# GitHub token, so whether it runs is a property of the JOB, not of this script:
# a job holding a read-only token turns it on, a job running fork-authored code
# does not get one. CI_SECURE_GH_IMPOSTOR carries that decision.
#
# The value is always passed through explicitly and `auto` is not accepted.
# `auto` (and simply omitting the flag, which lands on it) runs the check iff an
# authenticated `gh` happens to be present, which makes "did this security check
# run?" a property of the runner image - one rebuild away from silently
# stopping. A typo is red for the same reason: a check that quietly turned
# itself off looks exactly like a check that passed.
# No default. Defaulting to "off" made UNSET the one value that skipped the
# refusal below - so deleting the `env:` block from a scan job would quietly
# turn the network-gated check off, which is exactly the outcome this variable
# exists to make impossible, and the refusal message right below claimed was
# already impossible.
IMPOSTOR = os.environ.get("CI_SECURE_GH_IMPOSTOR", "").strip().lower()
ENGINE_ARGS = ["--gh-impostor", IMPOSTOR]

# Anything short of a completed network check is disclosed everywhere; whether
# it also BLOCKS depends on the run. A pull request must not be held hostage to
# someone else's rate limit, so there it warns. The scheduled run against the
# default branch has no deadline and nothing to race, so there it is red - and
# it is the run that catches a pin that rots after merge.
#
# Unset is allowed and means lax, because the fork job deliberately does not set
# it and lax still DISCLOSES on both surfaces - the dial changes severity, not
# coverage. An unrecognised value is refused, though: `true`, `yes` and
# `schedule` all read as "strict is on" to a human editing the YAML and all
# silently meant lax, which would have muted the one run whose whole purpose is
# catching a pin that rots after merge.
STRICT_RAW = os.environ.get("CI_SECURE_GH_STRICT", "0").strip()
STRICT_GH = STRICT_RAW == "1"

# The engine's own vocabulary for a completed network check. `partial:` (some
# pins unverified) and `skipped:` (never ran) are the two it uses for anything
# less, and both are held to be short of "ran".
GH_CHECK_COMPLETE_PREFIX = "ran:"


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
    """Neutralize untrusted text for the Markdown job summary.

    Same attacker-controlled source as `esc`, different sink: a newline here does
    not run a command, it breaks out of the list item and lets a crafted filename
    render its own Markdown - a summary that reads as a clean pass. A backtick
    does the same thing without a newline, by closing one of the inline code
    spans this summary wraps ids in, and a pipe splits a table row.

    The rule comes from the skill's config.py rather than being spelled out
    here, because the engine's report renderer neutralizes the same strings for
    the same reason: two copies would drift, and the weaker one is the surface
    an attacker aims at.
    """
    return CONFIG.flatten_scanned(value)


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

    # No rule, no verdict. The gate could fall back to a hardcoded copy of the
    # outcome tables, and that is exactly the failure this indirection exists to
    # prevent: a second definition of "which outcomes block" that drifts from
    # the engine's and is never noticed, because a build that finds it goes
    # green either way.
    if CONFIG_ERROR is not None:
        quote("".join(traceback.format_exception(
            type(CONFIG_ERROR), CONFIG_ERROR, CONFIG_ERROR.__traceback__)))
        return fail(
            f"ci-secure rule (config.py) could not be loaded ({CONFIG_ERROR!r}) "
            "- the gate cannot decide what blocks without one")
    if CONFIG is None:
        return fail(
            f"ci-secure rule (config.py) not found beside the engine at "
            f"{ENGINE.parent} nor at {_CONFIG_FALLBACK} - the gate cannot decide "
            "what blocks without one")
    # `coverage_is_complete` is in this list because it is CALLED below: left
    # out, a vendored rule missing only that name reached the crash handler and
    # reported an AttributeError instead of the specific disagreement.
    missing = [name for name in
               ("BLOCKING_OUTCOMES", "KNOWN_OUTCOMES", "OUTCOME_MARKS",
                "flatten_scanned", "coverage_is_complete")
               if not hasattr(CONFIG, name)]
    if missing:
        return fail(f"ci-secure rule at {CONFIG_PATH} defines no {', '.join(missing)} "
                    "- engine and gate disagree about what an outcome means")
    if not CONFIG.coverage_is_complete():
        return fail(f"ci-secure rule at {CONFIG_PATH} is incoherent: an outcome that "
                    "blocks or is recognised has no display mark")

    if IMPOSTOR not in ("on", "off"):
        return fail(
            f"CI_SECURE_GH_IMPOSTOR must be 'on' or 'off', not {IMPOSTOR!r} - "
            "'auto' and an unset value are refused on purpose, because they make "
            "whether the network-gated check runs depend on the runner image "
            "rather than on this job")

    if STRICT_RAW not in ("0", "1"):
        return fail(
            f"CI_SECURE_GH_STRICT must be '0' or '1', not {STRICT_RAW!r} - a "
            "value like 'true' reads as strict and meant lax, which would mute "
            "the run whose purpose is catching a pin that rots after merge")

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
    unknown = sorted(str(o) for o in
                     {f["outcome"] for f in facts} - set(CONFIG.KNOWN_OUTCOMES))
    if unknown:
        degraded.append(f"unrecognised fact outcome(s) {unknown} - this gate cannot "
                        "tell whether they are failures")

    # Which outcomes block is the rule's call, not a string literal here: a
    # hardcoded "fail" filter means a newly-added failure outcome is neither
    # blocked nor unrecognised, and ships green.
    failed = [f for f in facts if f["outcome"] in CONFIG.BLOCKING_OUTCOMES]
    unmeasured = [f for f in facts if f["outcome"] == "unmeasured"]

    # Counts, never the bare aggregate. `config_facts.py` registers the score as
    # machine-only - it exists so several engines' scores can be blended, and the
    # report renderer is forbidden from showing it, because one number invites a
    # reader to manage the number rather than the findings. This is a second
    # reader-facing surface and it was quietly exempt.
    #
    # `applicable_count` is in the headline, not just `scored_count`: when a fact
    # cannot be measured the engine drops it from BOTH sides of the ratio, so a
    # repo with one measurable fact and eleven unmeasurable ones still reads
    # "1/1 facts pass". True, and rhetorically false - the denominator shrank to
    # hide the gap. The engine states the gap in `caveat`, rendered below.
    #
    # Every interpolation here is flattened. These fields come out of the
    # engine's JSON, and on a fork pull request the engine is attacker code: a
    # newline in any of them would start a new line in `body`, which is printed
    # to stdout where Actions parses `::workflow commands::`.
    lines = ["## ci-secure", "",
             f"{flat(score.get('passed'))}/{flat(score.get('scored_count'))} facts pass"
             f" of {flat(score.get('applicable_count'))} applicable, "
             f"{flat(scan.get('scanned_workflows'))} workflow file(s) scanned", ""]
    if score.get("caveat"):
        lines += [f"> {flat(score['caveat'])}", ""]

    # A network-gated check that did not complete is disclosed HERE, in the
    # summary's opening lines, not only in the detector section far below. A
    # reader who stops after the headline must not come away with a coverage
    # claim the run cannot support: "no findings from P14.11" and "P14.11 never
    # ran" are different statements, and the reassuring one is false.
    incomplete_gh = {k: v for k, v in gh_checks.items()
                     if not str(v).startswith(GH_CHECK_COMPLETE_PREFIX)}
    if incomplete_gh:
        lines += [f"> **Network-gated check(s) did NOT run to completion:** "
                  f"{', '.join(flat(k) for k in sorted(incomplete_gh))}. "
                  "This scan does not cover what they would have checked.", ""]

    # An unrecognised outcome renders as itself rather than folding into FAIL:
    # the summary a human reads must never disagree with the exit code.
    for f in facts:
        mark = CONFIG.OUTCOME_MARKS.get(
            f["outcome"], f"**{flat(f['outcome']).upper()}**")
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
    # not `.get(... ) or {}`: this gate always passes `--gh-impostor` explicitly,
    # on or off, so the engine always has a status to report, and an absent key
    # is schema drift - which would make those two cases look identical again.
    lines += ["", "### Network-gated detectors", ""]
    lines += [f"- `{flat(k)}`: {flat(v)}" for k, v in sorted(gh_checks.items())]
    # Individually unmeasured facts do not block - several have honest causes
    # that would otherwise leave the gate permanently red - but they are never
    # silent, and a run where NOTHING was measured is caught by `degraded` above.
    for f in unmeasured:
        print(f"::warning::ci-secure fact unmeasured: {esc(f['fact_id'])} - {esc(f['evidence'])}")
    # The second surface for the same disclosure. On a strict run this is
    # promoted to a verdict below rather than repeated as a warning.
    if not STRICT_GH:
        for name, status in sorted(incomplete_gh.items()):
            print(f"::warning::ci-secure network-gated check {esc(name)} did not "
                  f"complete: {esc(status)} - this scan does not cover it")
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
    if STRICT_GH:
        for name, status in sorted(incomplete_gh.items()):
            fail(f"ci-secure network-gated check {name} did not complete: {status} "
                 "- this run requires complete coverage, and a check that could "
                 "not finish is not a check that passed")
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
