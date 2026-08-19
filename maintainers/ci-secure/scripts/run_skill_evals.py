#!/usr/bin/env python3
"""Execute ci-secure's behavioral eval cases against a real agent session.

`claude plugin eval` is the runner these cases were written for, and it is
gated behind early access. This is the same four steps built on shipped
primitives instead:

  1. scaffold  - the case's `scaffold.sh` materializes fixture workflows into
                 an empty temp directory and makes it a git repo;
  2. run       - `claude -p ... --plugin-dir <skill> --output-format stream-json`
                 drives one headless session against that directory;
  3. collect   - every tool call and message is read back out of the stream,
                 and every file the session WROTE is read off disk: the skill
                 renders its report to a file and prints a summary, so the
                 transcript alone is only half of what the run produced;
  4. grade     - the case's own graders are applied to that record, each regex
                 against the corpus its `target` names.

Maintainer-only, and deliberately outside `skills/`: it spends real tokens, so
it is never part of `pytest`. Run it before shipping a change to SKILL.md, the
reference files, or the scripts the contract tells the agent to invoke.

    python3 maintainers/ci-secure/scripts/run_skill_evals.py            # all cases
    python3 maintainers/ci-secure/scripts/run_skill_evals.py --case clean-repo
    python3 maintainers/ci-secure/scripts/run_skill_evals.py --runs 3

Exit 0 when every selected case passes every mechanical grader, 1 only when a
case really ran and failed one, and 2 whenever the harness could not run: no
`claude`, no PyYAML, a scaffold failure, an agent that refused to start, a
session that produced no events, a turn cap hit, or a grader type this harness
cannot evaluate. The split is the whole point. A session that never happened,
graded, fails every positive grader and passes every negative one — which reads
as "the agent behaved wrongly" when the truth is "the agent never ran". That is
the wrong diagnosis pointing at the wrong file, and it is exactly the honesty
rule the skill under test enforces on its own skipped checks.

`llm` graders are REPORTED, NOT SCORED. They need a judge model; scoring them
here without one would be inventing a verdict.

WHAT THIS HARNESS DOES NOT YET DO, and `claude plugin eval` does:

  - `execution.allowed_tools` is not applied. The session runs with the full
    tool set, so a pass means the behaviour is reachable — not that it is
    reachable under the tool grant the case declares.
  - The no-plugin ablation arm (`arm: both`) is not run, so no grader is
    checked against a session without the skill loaded.
  - `execution.model` and `runs: 3` in the case files are not honoured; the
    model is whatever the CLI defaults to and the run count comes from `--runs`.
  - regex `flags` are ignored; every pattern is matched case-sensitively.
  - a `target` given as `{source: file, path: ...}` is not implemented. The
    three word targets are: `trace` (the decoded transcript), `files` (what the
    session wrote — see `_files_corpus`), and `last_message`. An unimplemented
    target stops the harness rather than quietly grading another corpus.

Each of those makes a pass weaker than the case author asked for. None of them
makes a failure wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[3] / "skills" / "ci-secure"
_EVALS = _SKILL / "evals"

# The skill tree minus the parts that would hand the agent its own answer key.
# `--plugin-dir` mounts the whole directory, and `evals/` holds every grader's
# expected string while `tests/` holds rendered report fixtures containing them.
# An agent that greps the plugin directory would put those strings into the
# transcript, satisfying the graders without ever running a scan.
#
# `CHANGELOG.md` is here for the same reason and is the less obvious one: it
# narrates the graders themselves, so it quotes their expected banner strings
# verbatim -- `0 critical findings` sits in the entry explaining why that very
# pattern needs its `(?<![\d.])` guard. It is also of no use to an agent
# auditing a repository, so withholding it costs the run nothing.
# `test_no_file_the_plugin_copy_mounts_can_satisfy_a_grader` states the property
# this list exists to satisfy, so the next file added at the skill root cannot
# quietly reopen the hole.
_PLUGIN_EXCLUDE = ("evals", "tests", "CHANGELOG.md")

# Where a failing session is written, relative to the working directory, so the
# run that failed can be read rather than reproduced.
_LOGS = Path("skill-eval-logs")

# A ceiling on any single file entering the graded corpus. A report is tens of
# kilobytes; anything past this is a lockfile, a cache or a binary the agent
# happened to create, and reading it in would bloat every regex match.
_MAX_CORPUS_FILE = 4 * 1024 * 1024

# The artifact every `files` grader is anchored on. SKILL.md renders it as
# `$TMPDIR/ci-secure-report-<slug>.md` and the save option copies it to
# `./ci-secure-report.md`, so one glob covers both landing spots. `_grade` uses
# the header form to tell "the report was never collected" (a harness failure)
# apart from "the report says something else" (a real red).
_REPORT_GLOB = "ci-secure-report*.md"
_REPORT_HEADER = re.compile(r"^=== [^\n]*ci-secure-report[^\n]*\.md ===", re.M)


def _env(tmpdir: Path | None = None) -> dict:
    """A deliberately small environment for every subprocess.

    Inherited `GIT_DIR` / `GIT_WORK_TREE` are the hazard: the scaffold's own
    guard checks that the working directory is empty, which an empty temp dir
    always is, so a git repository-selection variable exported by a hook or a
    `git worktree` would send its `git init` / `git add` / `git commit` at a
    real repository — and what it commits is intentionally vulnerable workflow
    YAML. `_scaffold_common.sh` documents the minimal environment it is built
    against; this is that environment.

    `tmpdir` points the session's TMPDIR at a directory this harness owns.
    SKILL.md renders the report to `${TMPDIR:-/tmp}/ci-secure-report-<slug>.md`
    and the findings to the matching `ci-secure-findings-<slug>.json`, so
    setting it is what turns "collect what the run produced" into a contract
    rather than a guess at where the files landed.
    """
    keep = ("PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "IS_SANDBOX",
            "CLAUDE_CODE_BUBBLEWRAP", "NODE_PATH", "NPM_CONFIG_PREFIX")
    env = {k: v for k, v in os.environ.items() if k in keep and v is not None}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def _die(msg: str, code: int = 2) -> None:
    print(f"harness: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _cases(only: str | None) -> list[Path]:
    dirs = sorted(p.parent for p in _EVALS.glob("*/case.yaml"))
    if only:
        dirs = [d for d in dirs if d.name == only]
        if not dirs:
            _die(f"no case named {only!r} under {_EVALS}")
    if not dirs:
        _die(f"no cases found under {_EVALS}")
    return dirs


def _scaffold(case: Path, sandbox: Path) -> None:
    script = case / "scaffold.sh"
    if not script.is_file():
        _die(f"{case.name}: no scaffold.sh")
    r = subprocess.run(
        ["bash", str(script)], cwd=sandbox, capture_output=True, text=True,
        timeout=120, env=_env(),
    )
    if r.returncode != 0:
        _die(f"{case.name}: scaffold failed: {r.stderr.strip()[-400:]}")


def _events(stream: str) -> list[dict]:
    """Every decodable JSON event in the stream, in order."""
    out = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _plugin_dir(into: Path) -> Path:
    """A copy of the skill with the answer key removed, for `--plugin-dir`."""
    dest = into / "ci-secure"
    shutil.copytree(
        _SKILL, dest,
        ignore=lambda d, names: [n for n in names if n in _PLUGIN_EXCLUDE
                                 and Path(d) == _SKILL],
    )
    return dest


def _run_agent(prompt: str, sandbox: Path, plugin: Path, max_turns: int,
               timeout: int, tmpdir: Path | None = None) -> str:
    """One headless session against the sandbox. Returns the raw stream.

    Exits 2 rather than returning a stream no session produced. `claude` has
    several ways to decline before the first turn — an auth failure, a refusal
    to use `bypassPermissions` as root, an unknown flag — and every one of them
    yields an empty stdout that grades as a full behavioural regression.
    """
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        "--plugin-dir", str(plugin),
    ]
    try:
        r = subprocess.run(
            cmd, cwd=sandbox, capture_output=True, text=True, timeout=timeout,
            env=_env(tmpdir),
        )
    except subprocess.TimeoutExpired:
        _die(f"agent run exceeded {timeout}s", 2)

    def _no_session(reason: str) -> None:
        """Report a run that produced no session, verbatim.

        Nothing here is special-cased to a known message. The first CI run of
        this suite failed for a reason nobody could read, because the harness
        kept only a substring test for "early access" and discarded the rest of
        what `claude` said. Whatever the next non-start is, it lands in the log.
        """
        _die("\n".join([
            reason,
            f"  claude exit code: {r.returncode}",
            f"  claude stderr: {r.stderr.strip()[-2000:] or '(empty)'}",
            f"  claude stdout: {r.stdout.strip()[-2000:] or '(empty)'}",
        ]))

    if r.returncode != 0:
        _no_session("claude exited non-zero — the session never ran.")
    events = _events(r.stdout)
    if not any(e.get("type") == "system" and e.get("subtype") == "init"
               for e in events):
        _no_session("claude exited 0 but the stream carries no session.")
    if not _tool_calls(r.stdout):
        # Every case drives the agent to read files and execute the scanner. A
        # completed session that called no tool at all did not do the work; it
        # is a session that ended before it started, and grading it scores each
        # negative grader as a pass while blaming the contract for the rest.
        _no_session("the session completed without calling a single tool.")
    for e in events:
        if e.get("type") != "result":
            continue
        if e.get("subtype") == "error_max_turns":
            _die(f"the session hit its {max_turns}-turn cap; a truncated run is "
                 "not a graded run")
        if e.get("is_error"):
            _no_session("the session ended in an error result.")
    return r.stdout


def _tool_calls(stream: str) -> list[tuple[str, str]]:
    """(tool_name, json-encoded input) for every tool_use in the stream."""
    out: list[tuple[str, str]] = []
    for ev in _events(stream):
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                out.append((c.get("name", ""), json.dumps(c.get("input", {}))))
    return out


def _transcript(stream: str) -> str:
    r"""The decoded transcript the regex graders are written against.

    Grading the raw stream-json is not the same thing, and is wrong in both
    directions: inside a JSON string a newline is the two characters `\` and
    `n`, so `[^\n]` stops meaning "on one line". `ci\.yml[^\n]{0,30}\b11\b`
    would then match `ci.yml` on one line of output and a stray `11` on the
    next — a false pass on the very grader that pins a finding to its real file
    and line — while a `not_contains` guard fails on text that never shared a
    line. Decode first, then match.

    The corpus is what the session produced: the agent's own prose and the
    output of the tools it ran. The prompt is excluded, so a grader cannot be
    satisfied by the instructions the agent was handed.
    """
    parts: list[str] = []
    for ev in _events(stream):
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        is_assistant = ev.get("type") == "assistant"
        for c in content:
            if not isinstance(c, dict):
                continue
            kind = c.get("type")
            if kind == "text" and is_assistant:
                parts.append(str(c.get("text", "")))
            elif kind == "tool_result":
                body = c.get("content")
                if isinstance(body, str):
                    parts.append(body)
                elif isinstance(body, list):
                    parts.extend(str(b.get("text", "")) for b in body
                                 if isinstance(b, dict) and b.get("type") == "text")
    return "\n".join(parts)


def _last_message(stream: str) -> str:
    """The final assistant turn — what the session left on the reader's screen."""
    texts = []
    for ev in _events(stream):
        if ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        turn = [str(c.get("text", "")) for c in content
                if isinstance(c, dict) and c.get("type") == "text"]
        if turn:
            texts = turn
    return "\n".join(texts)


def _snapshot(roots: list[Path]) -> dict[str, str]:
    """path -> content digest, for every readable file under `roots`."""
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in _corpus_files(roots)}


def _corpus_files(roots: list[Path]) -> list[Path]:
    """Every regular, non-`.git`, non-symlink file under `roots`, sorted.

    Symlinks are skipped rather than followed: the sandbox is a directory an
    agent had write access to, and a link out of it would pull an arbitrary file
    into the graded corpus.
    """
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            if ".git" in p.relative_to(root).parts:
                continue
            out.append(p)
    return out


def _files_corpus(roots: list[Path], before: dict[str, str]) -> str:
    """The files the SESSION produced, as one greppable text.

    Not the whole sandbox. The scaffold hands the agent intentionally
    vulnerable workflow YAML naming the very files several graders assert on,
    so grading what the agent was handed would give those graders their answer
    for free — the same trap `_PLUGIN_EXCLUDE` closes for `evals/` and
    `tests/`. A file is in only if it did not exist before the session or its
    contents changed during it.

    Each file is introduced by a `=== <path> ===` header, relative to its root
    so the corpus does not carry this run's temp directory name.
    """
    parts: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in _corpus_files([root]):
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if before.get(str(p)) == hashlib.sha256(raw).hexdigest():
                continue  # handed to the session, not written by it
            rel = p.relative_to(root).as_posix()
            if len(raw) > _MAX_CORPUS_FILE:
                # Never drop a file silently — a grader that cannot see its
                # evidence must say so, not fail as though the run misbehaved.
                parts.append(f"=== {rel} (NOT READ: {len(raw)} bytes) ===")
                continue
            try:
                parts.append(f"=== {rel} ===\n{raw.decode('utf-8')}")
            except UnicodeDecodeError:
                parts.append(f"=== {rel} (NOT READ: not UTF-8 text) ===")
    return "\n".join(parts)


def _grade(graders: list[dict], stream: str, tools: list[tuple[str, str]],
           files: str = "", last_message: str = "") -> tuple[list, list]:
    """(scored results, unscored llm graders).

    `stream` is the decoded transcript; `files` is what the session wrote.
    A regex grader reads whichever its `target` names — the two corpora are
    kept apart deliberately. `files` exists because the graders that pin a
    finding to its real file and line are anchored on `report.py`'s evidence
    bullets, which the skill renders into a file and does not print; grading
    them against the transcript scored whether the agent happened to `cat` its
    own report.
    """
    corpora = {"trace": stream, "files": files, "last_message": last_message}
    scored, unscored = [], []
    for g in graders:
        kind, name = g.get("type"), g.get("name", "?")
        if kind == "tool_used":
            pat = g.get("input_match")
            n = sum(
                1 for t, i in tools
                if t == g.get("tool") and (pat is None or re.search(pat, i))
            )
            lo, hi = g.get("min", 1), g.get("max")
            scored.append((name, n >= lo and (hi is None or n <= hi), f"count={n}"))
        elif kind == "regex":
            pat, mode = g.get("pattern", ""), g.get("match", "contains")
            # `target` used to be ignored outright, so a grader that said
            # `files` was silently matched against the transcript — the wrong
            # corpus, reported as a verdict. An unimplemented target is a hole
            # in the suite, and a suite with a hole in it did not pass.
            target = g.get("target", "trace")
            if target not in corpora:
                _die(f"{name}: grader target {target!r} is not implemented — "
                     "this harness cannot score the case, so it must not "
                     "report one")
            if target == "files" and not _REPORT_HEADER.search(files):
                # Every `files` grader in this suite is anchored on the rendered
                # report, so a corpus without one scores every `contains` as a
                # failure and every `not_contains` as a pass: the session's own
                # behaviour, reported from evidence that was never collected.
                #
                # Testing `not files` was too weak. A session leaves scratch
                # notes and a fix branch, and any one of them keeps the corpus
                # non-empty while the report is still missing — which is what
                # happens the day `claude` stops honouring TMPDIR, a subprocess
                # resets it, or SKILL.md drifts to a literal path. Ask for the
                # artifact the graders actually read.
                _die(f"{name}: targets `files`, but the session wrote no "
                     f"{_REPORT_GLOB} the harness could collect — the graders "
                     "anchored on the report had no evidence to read, and that "
                     "is not a behavioural failure")
            found = bool(re.search(pat, corpora[target]))
            if mode == "not_contains":
                scored.append((name, not found, "absent" if not found else "PRESENT"))
            elif mode.startswith("count:"):
                # `corpora[target]`, not `stream`: counting always read the
                # transcript, so a `files` grader scored the agent's prose.
                want = int(mode.split(":", 1)[1])
                n = len(re.findall(pat, corpora[target]))
                scored.append((name, n == want, f"count={n}"))
            else:
                scored.append((name, found, "found" if found else "missing"))
        elif kind == "file_exists":
            unscored.append((name, "file_exists needs the harness's created-file diff"))
        elif kind in ("llm", "baseline"):
            unscored.append((name, f"{kind} grader needs a judge model — reported, not scored"))
        else:
            # Never demote an unevaluated grader to a dim line. A grader type
            # this harness does not implement is a hole in the suite, and a
            # suite with a hole in it did not pass.
            _die(f"{name}: grader type {kind!r} is not implemented — this "
                 "harness cannot score the case, so it must not report one")
    return scored, unscored


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--case", help="run one case by directory name")
    ap.add_argument("--runs", type=int, default=1, help="runs per case (default 1)")
    ap.add_argument("--keep", action="store_true", help="keep sandboxes for debugging")
    args = ap.parse_args()

    # `--runs 0` used to execute nothing and return 0, and the workflow's
    # verdict announced that the skill evals passed. A suite that ran no
    # session has no verdict to report.
    if args.runs < 1:
        _die(f"--runs {args.runs} would execute no session; that is not a pass")

    if shutil.which("claude") is None:
        _die("`claude` is not on PATH")
    try:
        import yaml
    except ImportError:
        _die("PyYAML is required")

    failed = False
    for case in _cases(args.case):
        spec = yaml.safe_load((case / "case.yaml").read_text(encoding="utf-8"))
        ex = spec.get("execution", {})
        prompt = ex.get("prompt")
        if not prompt:
            _die(f"{case.name}: no execution.prompt")

        for run in range(1, args.runs + 1):
            sandbox = Path(tempfile.mkdtemp(prefix=f"skill-eval-{case.name}-"))
            plugin_root = Path(tempfile.mkdtemp(prefix=f"skill-eval-plugin-{case.name}-"))
            # The session's TMPDIR, and the second half of the graded corpus:
            # SKILL.md renders the report and the findings JSON under it.
            artifacts = Path(tempfile.mkdtemp(prefix=f"skill-eval-out-{case.name}-"))
            try:
                _scaffold(case, sandbox)
                # AFTER the scaffold: what the fixture put there is what the
                # agent was handed, and must not count as what it produced.
                before = _snapshot([sandbox, artifacts])
                stream = _run_agent(
                    prompt, sandbox, _plugin_dir(plugin_root),
                    ex.get("max_turns", 60), ex.get("timeout_seconds", 900),
                    artifacts,
                )
                tools = _tool_calls(stream)
                scored, unscored = _grade(
                    spec.get("graders", []), _transcript(stream), tools,
                    files=_files_corpus([sandbox, artifacts], before),
                    last_message=_last_message(stream))
                # `all([])` is True. A case whose graders all fell through to
                # unscored would otherwise print PASS over nothing at all.
                if not scored:
                    _die(f"{case.name}: no grader could be scored; a case with "
                         "nothing to check does not pass")

                ok = all(p for _, p, _ in scored)
                failed |= not ok
                tag = f"{case.name} (run {run}/{args.runs})"
                print(f"\n{'PASS' if ok else 'FAIL'}  {tag}  "
                      f"— {sum(p for _, p, _ in scored)}/{len(scored)} scored graders, "
                      f"{len(tools)} tool calls")
                for name, passed, detail in scored:
                    print(f"    {'ok  ' if passed else 'FAIL'} {name}  ({detail})")
                for name, why in unscored:
                    print(f"    ----  {name}  ({why})")
                if not ok:
                    # "Read the job log" is what the workflow tells a reader to
                    # do, and grader names alone do not explain a failure. Keep
                    # the session next to the log so it can be read.
                    log = _LOGS / f"{case.name}-run{run}.jsonl"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(stream, encoding="utf-8")
                    print(f"    session recorded: {log}")
                    # A `files` grader failed on something the transcript does
                    # not contain, so the transcript alone cannot explain it.
                    corpus_log = _LOGS / f"{case.name}-run{run}.files.txt"
                    corpus_log.write_text(
                        _files_corpus([sandbox, artifacts], before),
                        encoding="utf-8")
                    print(f"    files the run produced: {corpus_log}")
            finally:
                shutil.rmtree(plugin_root, ignore_errors=True)
                if not args.keep:
                    shutil.rmtree(sandbox, ignore_errors=True)
                    shutil.rmtree(artifacts, ignore_errors=True)
                else:
                    print(f"    sandbox kept: {sandbox}")
                    print(f"    session output kept: {artifacts}")

    return 1 if failed else 0


def cli() -> int:
    """`main`, with every unexpected failure mapped onto the could-not-run code.

    Python exits 1 on an uncaught exception, and 1 is reserved here for "a case
    ran and failed". A hung scaffold, a malformed case.yaml or a bad grader
    pattern is the harness failing, and must not be read as the skill's
    behaviour changing.
    """
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the exit code is the point
        _die(f"unhandled harness error: {type(exc).__name__}: {exc}")
        raise  # unreachable; _die always raises


if __name__ == "__main__":
    raise SystemExit(cli())
