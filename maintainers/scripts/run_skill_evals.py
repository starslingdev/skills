#!/usr/bin/env python3
"""Run a skill's runnable `evals/evals.json` entries as real agent sessions.

    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup
    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --id 10 --runs 3
    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --all --id 11

An entry is runnable when it carries `checks`. Its optional `tier` is `gate`
(the default: the per-PR regression check) or `measure` (kept for a
before/after measurement or the baseline); only `gate` entries run unless
`--tier measure` or `--all` is passed, and every entry left out prints a
SKIPPED (tier) line. Runs go `--jobs` at a time (default: every run, at most
five; env SKILL_EVALS_JOBS), each in its own temp dir, sandbox, skill copy and
TMPDIR; runs of entries marked `serial` (their sessions write fixed `/tmp`
paths) take turns. A PASS / FAIL / NOT RUN block prints as each run finishes.
For each run:

  1. scaffold - `evals/fixtures/<fixture>/scaffold.py` builds the target in an
                empty temp dir and prints KEY=VALUE lines for the session env;
  2. run      - one headless `claude -p` session against that dir, with a copy
                of the skill (minus its answer key) mounted by `--plugin-dir`;
  3. collect  - tool calls, transcript, last message, and the files the
                session wrote (in the sandbox, its TMPDIR, or at an absolute
                `.md` path a Bash command named or a Write/Edit targeted);
  4. check    - each check's regex against the record it names.

Exit 0 every run passed, 1 a run happened and failed a check, 2 the harness
could not run (nothing selected, no claude, scaffold failed, no real `gh` for
the shim, no session or no result event, turn cap, timeout, a `files` check
with nothing collected or an empty report, or an unexpected crash). A crash is
that run's NOT RUN; the other runs still finish and grade. If any run
failed a check the exit is 1 even when other runs could not run, a crash
included: a session that happened and failed is the stronger signal. A session
that never happened must not read as a behaviour change. Tokens and duration
per run go to `skill-eval-results/`, rewritten (atomically, by the one main
thread) after every run so a crash or a killed job keeps what already ran.
Each entry's `expected_output` is judge-only: no check reads it, so it is
printed and recorded as UNSCORED.
Maintainer-only: it spends real tokens and is never part of pytest.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_reading  # noqa: E402  (reading accounting lives in its own module)

_REPO = Path(__file__).resolve().parents[2]
_RESULTS = _REPO / "skill-eval-results"
_LOGS = _REPO / "skill-eval-logs"
_SHIM = Path(__file__).resolve().parent / "eval_gh_shim.py"
_CHECK_TYPES = ("tool_used", "trace", "files", "last_message", "reading")
_MAX_FILE = 4 * 1024 * 1024
_MAX_JOBS = 5  # sessions at once by default; each is one `claude -p` process
# `gate` entries run by default (the per-PR regression check); `measure`
# entries exist to measure (a before/after, the baseline) and run on request.
_TIERS = ("gate", "measure")
_SERIAL = threading.Lock()  # held by a `serial` entry's run for its whole session
# The session loads no user-level settings, so a laptop with the skill installed
# globally cannot answer from that copy instead of the one under test.
_ISOLATION = ("--setting-sources", "project,local")


class HarnessError(Exception):
    """The harness could not produce a session worth checking (exit 2)."""


def plugin_ignore(directory: str, names: list[str]) -> list[str]:
    """What the skill copy withholds: the answer key. `evals/` holds every
    expected string, `tests/test_*.py` and `tests/fixtures/` the oracle and
    corpus, `CHANGELOG.md` narrates the checks. `tests/verify_report.py` stays:
    the skill runs it."""
    d = Path(directory)
    out = []
    for n in names:
        if d.name == "tests" and (n.startswith("test_") or n in ("fixtures", "conftest.py")):
            out.append(n)
        elif n in ("evals", "CHANGELOG.md", "__pycache__") and (d / "SKILL.md").exists():
            out.append(n)
        elif n == "__pycache__":
            out.append(n)
    return out


def _env(extra: dict | None = None) -> dict:
    keep = ("PATH", "HOME", "USER", "TERM", "LANG", "LC_ALL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL", "IS_SANDBOX", "CLAUDE_CODE_BUBBLEWRAP", "NODE_PATH",
            "NPM_CONFIG_PREFIX", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST",
            "GH_CONFIG_DIR", "XDG_CONFIG_HOME")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["GIT_CONFIG_NOSYSTEM"] = "1"  # and no inherited GIT_DIR / GIT_WORK_TREE
    env.update(extra or {})
    return env


def scaffold(skill_dir: Path, fixture: str | None, sandbox: Path, aux: Path) -> dict:
    """Build the fixture into `sandbox`; return the env lines it printed."""
    if any(sandbox.iterdir()):
        raise HarnessError(f"refusing to scaffold into a non-empty dir: {sandbox}")
    if not fixture:
        return {}
    script = skill_dir / "evals" / "fixtures" / fixture / "scaffold.py"
    if not script.is_file():
        raise HarnessError(f"no scaffold for fixture {fixture!r}: {script}")
    try:
        r = subprocess.run([sys.executable, str(script), str(sandbox), str(aux)],
                           capture_output=True, text=True, timeout=300, env=_env())
    except subprocess.TimeoutExpired:
        raise HarnessError(f"scaffold {fixture!r} exceeded 300s")
    except OSError as exc:
        raise HarnessError(f"scaffold {fixture!r} could not start: {exc}")
    if r.returncode != 0:
        raise HarnessError(f"scaffold {fixture!r} failed: {r.stderr.strip()[-800:]}")
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


def events(stream: str) -> list[dict]:
    out = []
    for line in stream.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [e for e in out if isinstance(e, dict)]


def _content(ev: dict) -> list:
    c = (ev.get("message") or {}).get("content")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def tool_calls(evs: list[dict]) -> list[str]:
    """`<ToolName> <json input>` for every tool call, in order."""
    return [f"{c.get('name', '')} {json.dumps(c.get('input', {}))}"
            for ev in evs for c in _content(ev) if c.get("type") == "tool_use"]


def transcript(evs: list[dict]) -> str:
    """The agent's prose and its tools' output, decoded (never the prompt)."""
    parts = []
    for ev in evs:
        for c in _content(ev):
            if c.get("type") == "text" and ev.get("type") == "assistant":
                parts.append(str(c.get("text", "")))
            elif c.get("type") == "tool_result":
                body = c.get("content")
                parts.extend([body] if isinstance(body, str) else
                             [str(b.get("text", "")) for b in body or []
                              if isinstance(b, dict) and b.get("type") == "text"])
    return "\n".join(parts)


def last_message(evs: list[dict]) -> str:
    texts = [""]
    for ev in evs:
        turn = [str(c.get("text", "")) for c in _content(ev)
                if ev.get("type") == "assistant" and c.get("type") == "text"]
        if turn:
            texts = turn
    return "\n".join(texts)


def _files(roots: list[Path]) -> list[Path]:
    return [p for r in roots if r.is_dir() for p in sorted(r.rglob("*"))
            if p.is_file() and not p.is_symlink() and ".git" not in p.relative_to(r).parts]


def snapshot(roots: list[Path]) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _files(roots)}


def files_written(roots: list[Path], before: dict, calls: list[str], since: float) -> str:
    """Every file the session created or changed under `roots`, plus any `.md`
    at an absolute path one of its Bash commands names, or a Write / Edit /
    MultiEdit call targets, that was written after `since` (the skill leaves
    its scratch dir to the agent, which may pick `/tmp/...` or a `mktemp -d`
    that ignores TMPDIR)."""
    paths = [p for p in _files(roots)
             if before.get(str(p)) != hashlib.sha256(p.read_bytes()).hexdigest()]
    named = []
    for call in calls:
        tool, _, raw = call.partition(" ")
        if tool == "Bash":
            cmd = str(json.loads(raw).get("command", ""))
            named += [m.group(1) for m in re.finditer(r"(/[^\s'\"`;|&<>()]+\.md)\b", cmd)]
        elif tool in ("Write", "Edit", "MultiEdit"):
            fp = str(json.loads(raw).get("file_path", ""))
            named += [fp] if fp.startswith("/") and fp.endswith(".md") else []
    for p in map(Path, named):
        if p not in paths and p.is_file() and not p.is_symlink() and p.stat().st_mtime >= since:
            paths.append(p)
    out = []
    for p in paths:
        raw = p.read_bytes()[:_MAX_FILE]
        out.append(f"=== {p.name} ===\n{raw.decode('utf-8', 'replace')}")
    return "\n".join(out)


def required_files(files: str, files_require: str) -> list[tuple[str, str]]:
    """(header, body) of each collected file whose name `files_require` matches
    in full; every other file (scratch notes, the findings JSON) is left out."""
    parts = re.split(r"(?m)^=== (.+) ===$\n?", files)
    return [(f"=== {n} ===", b) for n, b in zip(parts[1::2], parts[2::2])
            if re.fullmatch(f"(?:{files_require})", n)]


def apply_checks(checks: list[dict], rec: dict,
                 files_require: str | None = None) -> list[tuple[str, bool, str]]:
    """(name, passed, detail) per check. `rec` holds the four records.
    `files_require` (an entry field) names, as a regex, the file a `files`
    check reads, and the check then reads only that file: when none is
    collected, or one is empty, a `files` check is a harness error, so scratch
    files can neither stand in for a missing report nor satisfy a check the
    report fails, and an empty report cannot pass a `not_contains`."""
    results = []
    for c in checks:
        kind, name, pat = c.get("type"), c.get("name", "?"), c.get("pattern", "")
        if kind not in _CHECK_TYPES:
            raise HarnessError(f"check {name!r}: unknown type {kind!r}")
        if kind == "tool_used":
            n = sum(1 for call in rec["tool_used"] if re.search(pat, call))
            lo, hi = c.get("min", 1), c.get("max")
            results.append((name, n >= lo and (hi is None or n <= hi), f"count={n}"))
            continue
        if kind == "reading":
            try:
                results.append((name, *eval_reading.check(c, rec["reading"])))
            except ValueError as exc:  # a malformed check never passes silently
                raise HarnessError(str(exc)) from exc
            continue
        if kind == "files" and not rec["files"]:
            raise HarnessError(f"check {name!r} reads `files`, but the session wrote "
                               "nothing the harness could collect")
        text = rec[kind]
        if kind == "files" and files_require:
            wanted = required_files(rec["files"], files_require)
            if not wanted:
                raise HarnessError(f"check {name!r} reads `files`, but no collected file "
                                   f"matches {files_require!r}")
            if any(not body.strip() for _, body in wanted):
                raise HarnessError(f"check {name!r} reads `files`, but the collected "
                                   f"{files_require!r} file is empty")
            text = "\n".join(f"{h}\n{b}" for h, b in wanted)
        found = re.search(pat, text) is not None
        if c.get("match", "contains") == "not_contains":
            results.append((name, not found, "absent" if not found else "PRESENT"))
        else:
            results.append((name, found, "found" if found else "missing"))
    return results


def run_session(prompt: str, cwd: Path, plugins: list[Path], env: dict,
                max_turns: int, timeout: int, model: str | None) -> tuple[str, list[dict]]:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-turns", str(max_turns), "--permission-mode", "bypassPermissions",
           *_ISOLATION, *[a for p in plugins for a in ("--plugin-dir", str(p))]]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise HarnessError(f"session exceeded {timeout}s")
    evs = events(r.stdout)
    why = None
    if not any(e.get("type") == "system" and e.get("subtype") == "init" for e in evs):
        why = "the stream carries no session"
    elif not tool_calls(evs):
        why = "the session called no tool"
    elif not any(e.get("type") == "result" for e in evs):  # truncated: max-0 checks pass vacuously
        why = "the stream has no result event"
    if why and r.returncode != 0:
        why = f"claude exited {r.returncode}: {why}"
    result = next((e for e in reversed(evs) if e.get("type") == "result"), {})
    if result.get("subtype") == "error_max_turns":
        why = f"the session hit its {max_turns}-turn cap"
    elif result.get("is_error") and not why:
        why = "the session ended in an error result"
    if why:
        _LOGS.mkdir(exist_ok=True)  # mkstemp: two runs failing in one second keep both
        fd, _ = tempfile.mkstemp(prefix=f"harness-error-{int(time.time())}-",
                                 suffix=".jsonl", dir=_LOGS)
        with os.fdopen(fd, "w") as fh:
            fh.write(r.stdout)
        raise HarnessError(f"{why}; stderr: {r.stderr.strip()[-1500:] or '(empty)'}")
    return r.stdout, evs


def run_entry(skill_dir: Path, entry: dict, run: int, args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="skill-eval-"))
    sandbox, aux, tmpdir, mounts = (tmp / n for n in ("repo", "aux", "tmp", "plugins"))
    for d in (sandbox, aux, tmpdir, mounts):
        d.mkdir()
    try:
        extra = scaffold(skill_dir, entry.get("fixture"), sandbox, aux)
        plugins = []
        for name in [skill_dir.name, *entry.get("plugins", [])]:
            src = skill_dir.parent / name
            plugins.append(Path(shutil.copytree(src, mounts / name, ignore=plugin_ignore)))
        extra["TMPDIR"] = str(tmpdir)
        if extra.get("EVAL_GH_SHIM_REPO"):
            real = shutil.which("gh", path=_env()["PATH"])
            if not real:
                raise HarnessError("no real `gh` on PATH for the shim to delegate to")
            shim_dir = aux / "shim"
            shim_dir.mkdir()
            (shim_dir / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_SHIM}" "$@"\n')
            (shim_dir / "gh").chmod(0o755)
            extra.update(EVAL_GH_REAL=real, EVAL_GH_SCRIPTS=str(plugins[0] / "scripts"),
                         PATH=f"{shim_dir}{os.pathsep}{_env()['PATH']}")
        before, since = snapshot([sandbox, tmpdir]), time.time() - 1
        stream, evs = run_session(entry["prompt"], sandbox, plugins, _env(extra),
                                  entry.get("max_turns", args.max_turns),
                                  entry.get("timeout", args.timeout),
                                  args.model)
        (tmp / "session.jsonl").write_text(stream)  # read with --keep
        calls = tool_calls(evs)
        rec = {"tool_used": calls, "trace": transcript(evs),
               "last_message": last_message(evs),
               "files": files_written([sandbox, tmpdir], before, calls, since),
               "reading": eval_reading.summarize(evs, skill_dir.name,
                                                 eval_reading.reference_names(skill_dir),
                                                 time.time() - since - 1, str(plugins[0]))}
        stem = _LOGS / f"{skill_dir.name}-{entry.get('id')}-run{run}"

        def keep_session() -> None:
            _LOGS.mkdir(exist_ok=True)
            Path(f"{stem}.jsonl").write_text(stream)
            Path(f"{stem}.files.txt").write_text(rec["files"])
        try:
            checks = apply_checks(entry["checks"], rec, entry.get("files_require"))
        except HarnessError:
            keep_session()  # nothing collected: the transcript says why
            raise
        result =next((e for e in reversed(evs) if e.get("type") == "result"), {})
        usage = result.get("usage") or {}
        out = {"id": entry.get("id"), "name": entry.get("name"), "run": run,
               "passed": all(ok for _, ok, _ in checks), "tool_calls": len(calls),
               "input_tokens": sum(usage.get(k) or 0 for k in (
                   "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
               "output_tokens": usage.get("output_tokens") or 0,
               "duration_ms": result.get("duration_ms"),
               "cost_usd": result.get("total_cost_usd"), "reading": rec["reading"],
               "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in checks]}
        if not out["passed"]:
            keep_session()
            out["session_log"] = f"{stem}.jsonl"
        return out
    finally:
        if args.keep:
            print(f"    [#{entry.get('id')}] kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def default_jobs(n_runs: int) -> int:
    """Sessions at once: `SKILL_EVALS_JOBS` if set, else every run, capped."""
    env = os.environ.get("SKILL_EVALS_JOBS", "").strip()
    return int(env) if env else max(1, min(n_runs, _MAX_JOBS))


def _attempt(skill_dir: Path, entry: dict, run: int, args,
             stop: threading.Event) -> tuple[str, object]:
    """One run on a worker thread. Any Exception becomes that run's NOT RUN, so
    a crash in one session never takes the others down with it. A `serial`
    entry's sessions write fixed `/tmp/...` paths (the agent picks them, not
    $TMPDIR), so those runs take turns while the others run beside them. A run
    already waiting its turn when the job is stopped starts no session."""
    try:
        with _SERIAL if entry.get("serial") else contextlib.nullcontext():
            if stop.is_set():
                return "not_run", "the job was stopped before this session started"
            return "ok", run_entry(skill_dir, entry, run, args)
    except HarnessError as exc:
        return "not_run", str(exc)
    except Exception as exc:  # a harness bug in this run only: the others still grade
        return "not_run", f"unhandled {type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skill", required=True)
    ap.add_argument("--id", type=int, help="run one evals.json entry by id")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--model")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=1500, help="seconds per session")
    ap.add_argument("--keep", action="store_true", help="keep the temp dirs")
    ap.add_argument("--jobs", type=int, help="sessions at once (default: every run, at most "
                    f"{_MAX_JOBS}; env SKILL_EVALS_JOBS)")
    ap.add_argument("--tier", choices=_TIERS, default="gate",
                    help="run only entries of this tier (default gate)")
    ap.add_argument("--all", action="store_true", help="run every tier")
    args = ap.parse_args(argv)
    skill_dir = _REPO / "skills" / args.skill
    spec = json.loads((skill_dir / "evals" / "evals.json").read_text(encoding="utf-8"))
    selected = [e for e in spec["evals"] if e.get("checks")
                and (args.id is None or e.get("id") == args.id)]
    bad = [e.get("id") for e in selected if e.get("tier", "gate") not in _TIERS]
    if bad:
        print(f"harness: unknown `tier` on entries {bad}; use one of {_TIERS}", file=sys.stderr)
        return 2
    entries = [e for e in selected if args.all or e.get("tier", "gate") == args.tier]
    skipped = [e for e in selected if e not in entries]
    for e in skipped:  # never silently absent: the line says how to run it
        print(f"SKIPPED (tier)  {args.skill} #{e.get('id')} {e.get('name', '')}  tier "
              f"{e.get('tier', 'gate')}; run with --tier {e.get('tier', 'gate')} or --all")
    if not entries or args.runs < 1:
        why = (f"every selected entry is outside tier {args.tier}" if skipped
               else "no entry with `checks` selected")
        print(f"harness: nothing to run ({why})", file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        print("harness: `claude` is not on PATH", file=sys.stderr)
        return 2
    _RESULTS.mkdir(exist_ok=True)
    out = _RESULTS / f"{args.skill}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    results = [{"id": e.get("id"), "skipped": f"tier {e.get('tier', 'gate')}"} for e in skipped]
    harness_errors = 0

    def record(item: dict) -> None:  # main thread only; on disk after every run, atomically
        results.append(item)
        part = out.with_name(f".{out.name}.part")
        part.write_text(json.dumps(results, indent=2))
        os.replace(part, out)

    tasks = [(e, run) for e in entries for run in range(1, args.runs + 1)]
    jobs = args.jobs if args.jobs is not None else default_jobs(len(tasks))
    if jobs < 1:
        print("harness: --jobs / SKILL_EVALS_JOBS must be at least 1", file=sys.stderr)
        return 2
    for entry in entries:
        if entry.get("expected_output"):
            print(f"\nUNSCORED  {args.skill} #{entry.get('id')} {entry.get('name', '')}  "
                  "expected_output is judge-only; no check reads it")
    print(f"\nrunning {len(tasks)} session(s), {min(jobs, len(tasks))} at a time", flush=True)
    started = time.monotonic()
    pool, stop = ThreadPoolExecutor(max_workers=jobs), threading.Event()
    try:
        futures = {pool.submit(_attempt, skill_dir, e, run, args, stop): (e, run)
                   for e, run in tasks}
        for fut in as_completed(futures):  # lines print as runs finish, one block per run
            entry, run = futures[fut]
            eid = entry.get("id")
            unscored = {"unscored": "expected_output"} if entry.get("expected_output") else {}
            tag = f"{args.skill} #{eid} {entry.get('name', '')} (run {run}/{args.runs})"
            kind, r = fut.result()
            if kind == "not_run":
                harness_errors += 1
                print(f"\nNOT RUN  {tag}\n    [#{eid}] harness: {r}", flush=True)
                record({"id": eid, "run": run, "harness_error": r, **unscored})
                continue
            record({**r, **unscored})
            secs = (r["duration_ms"] or 0) / 1000
            lines = [f"\n{'PASS' if r['passed'] else 'FAIL'}  {tag}  {r['tool_calls']} tool "
                     f"calls, {r['input_tokens']} in / {r['output_tokens']} out tokens, "
                     f"{secs:.0f}s, ${r['cost_usd'] or 0:.2f}, {eval_reading.figure(r['reading'])}"]
            lines += [f"    [#{eid}] {'ok  ' if c['passed'] else 'FAIL'} {c['name']}  "
                      f"({c['detail']})" for c in r["checks"]]
            if r.get("session_log"):
                lines.append(f"    [#{eid}] session recorded: {r['session_log']}")
            print("\n".join(lines), flush=True)
    finally:  # a killed job (KeyboardInterrupt) starts nothing new; the file holds what ran
        stop.set()  # runs already queued on the serial lock see it once they get the lock
        pool.shutdown(wait=False, cancel_futures=True)
    print(f"\nresults: {out}\ntotal wall: {time.monotonic() - started:.0f}s")
    # Precedence: a run that happened and failed (1) outranks runs that could
    # not run or a crash (2), which outrank a clean pass (0).
    if any(r.get("passed") is False for r in results):
        return 1
    return 2 if harness_errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # an unexpected crash is the harness failing, not the skill
        print(f"harness: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
