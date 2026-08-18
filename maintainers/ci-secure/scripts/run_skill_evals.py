#!/usr/bin/env python3
"""Execute ci-secure's behavioral eval cases against a real agent session.

`claude plugin eval` is the runner these cases were written for, and it is
gated behind early access. This is the same four steps built on shipped
primitives instead:

  1. scaffold  - the case's `scaffold.sh` materializes fixture workflows into
                 an empty temp directory and makes it a git repo;
  2. run       - `claude -p ... --plugin-dir <skill> --output-format stream-json`
                 drives one headless session against that directory;
  3. collect   - every tool call and message is read back out of the stream;
  4. grade     - the case's own graders are applied to that record.

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
  - `target` (`trace` / `last_message` / `files`) and regex `flags` are ignored:
    every regex grader is matched against the whole decoded transcript.

Each of those makes a pass weaker than the case author asked for. None of them
makes a failure wrong.
"""

from __future__ import annotations

import argparse
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
_PLUGIN_EXCLUDE = ("evals", "tests")

# Where a failing session is written, relative to the working directory, so the
# run that failed can be read rather than reproduced.
_LOGS = Path("skill-eval-logs")


def _env() -> dict:
    """A deliberately small environment for every subprocess.

    Inherited `GIT_DIR` / `GIT_WORK_TREE` are the hazard: the scaffold's own
    guard checks that the working directory is empty, which an empty temp dir
    always is, so a git repository-selection variable exported by a hook or a
    `git worktree` would send its `git init` / `git add` / `git commit` at a
    real repository — and what it commits is intentionally vulnerable workflow
    YAML. `_scaffold_common.sh` documents the minimal environment it is built
    against; this is that environment.
    """
    keep = ("PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "IS_SANDBOX",
            "CLAUDE_CODE_BUBBLEWRAP", "NODE_PATH", "NPM_CONFIG_PREFIX")
    env = {k: v for k, v in os.environ.items() if k in keep and v is not None}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
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
               timeout: int) -> str:
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
            env=_env(),
        )
    except subprocess.TimeoutExpired:
        _die(f"agent run exceeded {timeout}s", 2)
    if "early access" in r.stdout or "early access" in r.stderr:
        _die("claude refused the run (early access gate)")
    if r.returncode != 0:
        _die(f"claude exited {r.returncode} — the session never ran. "
             f"claude said: {(r.stderr.strip() or r.stdout.strip())[-600:]}")
    events = _events(r.stdout)
    if not any(e.get("type") == "system" and e.get("subtype") == "init"
               for e in events):
        _die("claude exited 0 but the stream carries no session — nothing to "
             f"grade. stderr: {r.stderr.strip()[-600:] or '(empty)'}")
    for e in events:
        if e.get("type") != "result":
            continue
        if e.get("subtype") == "error_max_turns":
            _die(f"the session hit its {max_turns}-turn cap; a truncated run is "
                 "not a graded run")
        if e.get("is_error"):
            _die(f"the session ended in an error: "
                 f"{str(e.get('result', ''))[:400]}")
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


def _grade(graders: list[dict], stream: str, tools: list[tuple[str, str]]) -> tuple[list, list]:
    """(scored results, unscored llm graders)."""
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
            found = bool(re.search(pat, stream))
            if mode == "not_contains":
                scored.append((name, not found, "absent" if not found else "PRESENT"))
            elif mode.startswith("count:"):
                want = int(mode.split(":", 1)[1])
                n = len(re.findall(pat, stream))
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
            try:
                _scaffold(case, sandbox)
                stream = _run_agent(
                    prompt, sandbox, _plugin_dir(plugin_root),
                    ex.get("max_turns", 60), ex.get("timeout_seconds", 900),
                )
                tools = _tool_calls(stream)
                scored, unscored = _grade(
                    spec.get("graders", []), _transcript(stream), tools)
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
            finally:
                shutil.rmtree(plugin_root, ignore_errors=True)
                if not args.keep:
                    shutil.rmtree(sandbox, ignore_errors=True)
                else:
                    print(f"    sandbox kept: {sandbox}")

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
