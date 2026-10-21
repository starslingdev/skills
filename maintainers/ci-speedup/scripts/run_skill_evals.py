#!/usr/bin/env python3
"""Execute ci-speedup's behavioural eval cases against a real agent session.

A port of `maintainers/ci-secure/scripts/run_skill_evals.py` (read that file's
docstring first; every guard below exists for the reason it gives there). The
same four steps:

  1. scaffold  - the case's `scaffold.sh` materialises the synthetic repository
                 the offline replay corpus was recorded against into an empty
                 temp directory, as a git checkout whose `origin` names
                 `synthetic/repo`;
  2. run       - `claude -p ... --plugin-dir <skill> --output-format stream-json`
                 drives one headless session against that directory, with
                 `CI_SPEEDUP_GH_FIXTURES` pointing the skill's gh client at a
                 replay corpus, so every run-history call the skill makes is
                 answered from committed fixtures instead of the network;
  3. collect   - every tool call and message is read back out of the stream,
                 and every file the session WROTE is read off disk: the skill
                 renders `ci-speedup-findings-report.md` beside its scratch
                 `findings.json` and only summarises it in chat;
  4. grade     - the case's own mechanical graders are applied to that record.

Maintainer-only, and deliberately outside `skills/`: it spends real tokens, so
it is never part of `pytest`. Run it before shipping a change to SKILL.md, the
reference files, or the scripts the contract tells the agent to invoke.

    python3 maintainers/ci-speedup/scripts/run_skill_evals.py            # all cases
    python3 maintainers/ci-speedup/scripts/run_skill_evals.py --case full-run-synthetic
    python3 maintainers/ci-speedup/scripts/run_skill_evals.py --runs 3

Exit 0 when every selected case passes every mechanical grader, 1 only when a
case really ran and failed one, and 2 whenever the harness could not run: no
`claude`, no PyYAML, a scaffold failure, an agent that refused to start, a
session that produced no events, a turn cap hit, a report the graders are
anchored on that was never collected, or a grader type this harness cannot
evaluate. A session that never happened, graded, fails every positive grader
and passes every negative one; that is "the agent never ran", not "the agent
behaved wrongly", and the two must never share an exit code.

WHAT IS DIFFERENT FROM THE ci-secure HARNESS, and why:

  - The run-history data is REPLAYED. ci-speedup measures real GitHub Actions
    runs over `gh`; a live run would read whatever a real repository did this
    week and could never be graded mechanically. `collect_runs.GhClient` already
    has an offline seam (`CI_SPEEDUP_GH_FIXTURES`) used by the e2e test, and the
    replay directory is built by the same helper that test uses
    (`skills/ci-speedup/tests/fixtures/synthetic_corpus.py`), so the session sees
    exactly the corpus the e2e proves renders a report `verify_report.py`
    accepts. Nothing in SKILL.md is aware of the seam; the Bash tool inherits the
    variable and `run.py` picks it up.
  - The gh gate is REAL. SKILL.md's phase 1 stops unless `gh auth status`
    passes. The harness does not fake that: it passes the caller's GitHub auth
    through (`GH_TOKEN` / `GITHUB_TOKEN` in CI, `HOME`'s gh config on a laptop).
    With replay on, the token is never used for a data call; it only lets the
    gate pass honestly. No auth means the case fails its "pipeline ran" graders,
    which is the skill doing exactly what its contract says.
  - The plugin copy keeps `tests/verify_report.py`. SKILL.md's phase 5 tells the
    agent to run it, so `tests/` cannot be withheld wholesale the way ci-secure
    withholds it. Everything else under `tests/` (the oracle tests and every
    fixture, the replay corpus included) is still withheld: those are the
    answer key.
  - The report is found by NAME. SKILL.md leaves the scratch path to the agent,
    which usually lands it under TMPDIR (pointed at a directory this harness
    owns) but may pick a literal `/tmp/...`. So beside the sandbox and TMPDIR,
    the corpus also reads any `ci-speedup-findings-report.md` a Bash command of
    THIS session named, provided the file was written after the session started.

WHAT THIS HARNESS DOES NOT YET DO, and `claude plugin eval` does: identical to
the ci-secure harness. `execution.allowed_tools` is not applied, the no-plugin
ablation arm is not run, `execution.model` and `runs:` are not honoured, regex
`flags` are ignored, and a `{source: file}` target is not implemented. Each
makes a pass weaker than the case author asked for. None makes a failure wrong.
And the reverse gap: `claude plugin eval` has no way to set
`CI_SPEEDUP_GH_FIXTURES` (case `env` keys must match `^EVAL_`), so under it the
full-run case would audit a repository that does not exist on GitHub. Today,
this harness is the only runner that can execute that case.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL = _REPO_ROOT / "skills" / "ci-speedup"
_EVALS = _SKILL / "evals"
_CORPUS_HELPER = _SKILL / "tests" / "fixtures" / "synthetic_corpus.py"
_GH_SHIM = Path(__file__).resolve().parent / "eval_gh_shim.py"
# The repository the replay corpus answers for (every fixture name embeds it).
_REPLAYED_REPO = "synthetic/repo"

# The skill tree minus the parts that would hand the agent its own answer key.
# `--plugin-dir` mounts the whole directory: `evals/` holds every grader's
# expected string, the oracle tests under `tests/` assert the renderer's exact
# output, `tests/fixtures/` holds the replay corpus the session is graded
# against (job names, step names, the synthetic repo's YAML), and CHANGELOG.md
# narrates past graders and fixtures. An agent that greps the plugin directory
# would put those strings into the transcript without running anything.
#
# `tests/verify_report.py` is the one exception, and it is not optional:
# SKILL.md's phase 5 runs it on every audit. It imports nothing from `tests/`
# (only `scripts/untrusted_wrap.py`), which
# `test_the_plugin_copy_keeps_what_the_contract_runs` pins.
_PLUGIN_EXCLUDE_AT_ROOT = ("evals", "CHANGELOG.md")
_PLUGIN_KEEP_IN_TESTS = ("verify_report.py",)

# Where a failing session is written. Anchored on the repository root, never
# relative to the working directory: run from inside `skills/ci-speedup/` a
# relative path would drop a transcript into the installable skill, which the
# `skills` CLI ships to every user. Gitignored (`skill-eval-logs/`).
_LOGS = _REPO_ROOT / "skill-eval-logs"

_MAX_CORPUS_FILE = 4 * 1024 * 1024

# The artifact every `files` grader is anchored on. SKILL.md phase 4 renders it
# as `ci-speedup-findings-report.md` beside the scratch `findings.json`, and the
# opt-in save copies it to `./ci-speedup-findings-report.md`.
_REPORT_NAME = "ci-speedup-findings-report.md"
_REPORT_HEADER = re.compile(r"^=== [^\n]*ci-speedup-findings-report\.md ===", re.M)

# Load no USER settings: no user-installed skills, plugins, MCP servers or
# global CLAUDE.md. Without it, a maintainer laptop that has ci-speedup (or
# ci-secure) installed offers the session TWO ci-speedup skills, the installed
# one and the plugin copy under test, and a pass can come from the wrong one; a
# personal CLAUDE.md steers the session in ways CI never sees. Measured on
# claude 2.1.307: the session's `init` event lists 140 skills and 8 plugins
# without the flag and 20 skills (the plugin's `ci-speedup:ci-speedup` the only
# non-builtin) with it. Auth is unaffected. On the CI runner there are no user
# settings, so the flag changes nothing there; it makes a laptop run match CI.
_ISOLATION_FLAGS = ("--setting-sources", "project,local")

# Seconds of slack on top of the session's own budget when sizing the replay
# corpus's time-dependent fixtures (see `_replay_fixtures`).
_REPLAY_SLACK_S = 120


def _env(tmpdir: Path | None = None, fixtures: Path | None = None,
         shim_dir: Path | None = None) -> dict:
    """A deliberately small environment for the agent session.

    Same rationale as ci-secure's `_env`: no inherited `GIT_DIR` /
    `GIT_WORK_TREE`, no system git config, TMPDIR pointed at a directory the
    harness owns. Two additions, both load-bearing:

    * GitHub auth (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_HOST`, `GH_CONFIG_DIR`,
      `XDG_CONFIG_HOME`, `USER`). SKILL.md's phase-1 gate runs
      `gh auth status` and stops the audit when it fails. Passing auth through
      is how the gate passes honestly; with replay on, no data call uses it.
    * `CI_SPEEDUP_GH_FIXTURES`, the replay seam. `CI_SPEEDUP_GH_RECORD` is
      never kept: a session that recorded would overwrite the corpus it is
      graded against.
    * `shim_dir`, prepended to PATH: the `gh` there answers the replayed
      repository from the same corpus and delegates everything else, the
      `gh auth status` gate included, to the real `gh` (`eval_gh_shim.py`).
    """
    keep = ("PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL", "USER",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "IS_SANDBOX",
            "CLAUDE_CODE_BUBBLEWRAP", "NODE_PATH", "NPM_CONFIG_PREFIX",
            "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR",
            "XDG_CONFIG_HOME")
    env = {k: v for k, v in os.environ.items() if k in keep and v is not None}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    if fixtures is not None:
        env["CI_SPEEDUP_GH_FIXTURES"] = str(fixtures)
    if shim_dir is not None:
        env["CI_SPEEDUP_EVAL_REAL_GH"] = shutil.which("gh", path=env["PATH"]) or ""
        env["CI_SPEEDUP_EVAL_REPLAYED_REPO"] = _REPLAYED_REPO
        env["CI_SPEEDUP_EVAL_SCRIPTS"] = str(_SKILL / "scripts")
        env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    return env


def _gh_shim(into: Path) -> Path:
    """A directory holding one executable, `gh`, that runs `eval_gh_shim.py`.

    Why a shim at all, measured: of the first real full-run sessions, one ran
    the pipeline and passed, and the next ran `gh repo view` and
    `gh api repos/synthetic/repo` itself during phase 1, got real GitHub's 404
    for a repository that exists only in the corpus, and stopped to ask the user
    about their access. The skill's own scripts were already replaying; the CLI
    the agent calls directly was not. The shim closes that gap and nothing else.
    """
    into.mkdir(parents=True, exist_ok=True)
    gh = into / "gh"
    gh.write_text("#!/bin/sh\n"
                  f'exec python3 "{_GH_SHIM}" "$@"\n', encoding="utf-8")
    gh.chmod(0o755)
    return into


def _scaffold_env() -> dict:
    """The scaffold's environment: the minimal one, and no credentials at all.

    `_scaffold_common.sh` (ci-secure) documents the sandbox it is written
    against: PATH, HOME, TMPDIR, TERM, GIT_CONFIG_NOSYSTEM=1, no credentials.
    The scaffold only writes files and runs `git init`; it has no use for a
    token, so it never gets one.
    """
    keep = ("PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL")
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
        timeout=120, env=_scaffold_env(),
    )
    if r.returncode != 0:
        _die(f"{case.name}: scaffold failed: {r.stderr.strip()[-400:]}")


def _load_corpus_helper():
    spec = importlib.util.spec_from_file_location(
        "ci_speedup_synthetic_corpus", _CORPUS_HELPER)
    if spec is None or spec.loader is None:
        _die(f"cannot load the replay corpus helper at {_CORPUS_HELPER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _replay_fixtures(into: Path, span_s: int) -> Path:
    """The replay corpus for one session: the committed fixtures plus the one
    time-dependent family the e2e writes at run time.

    `_monthly_volume`'s endpoint embeds "now minus 30 days" to the second, and
    an agent reaches `run.py` minutes after this is written, not the seconds the
    e2e's subprocess takes. So the band is the session's whole budget plus
    slack: every second the session could call from is answered, and the
    collection stays a clean zero-error one rather than a partial-coverage
    report that tests the banner instead of the audit.
    """
    return _load_corpus_helper()._replay_dir(into, span_s=span_s)


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


def _plugin_ignore(directory: str, names: list[str]) -> list[str]:
    d = Path(directory)
    if d == _SKILL:
        return [n for n in names if n in _PLUGIN_EXCLUDE_AT_ROOT]
    if d == _SKILL / "tests":
        return [n for n in names if n not in _PLUGIN_KEEP_IN_TESTS]
    return [n for n in names if n == "__pycache__"]


def _plugin_dir(into: Path) -> Path:
    """A copy of the skill with the answer key removed, for `--plugin-dir`."""
    dest = into / "ci-speedup"
    shutil.copytree(_SKILL, dest, ignore=_plugin_ignore)
    return dest


# A SIBLING skill a case mounts beside ci-speedup (the routing case mounts
# ci-secure, so the question "which skill answers a security ask" has two real
# answers to choose between). Withheld for the reason ci-secure's own harness
# withholds them: its graders' expected strings and its rendered fixtures.
_SIBLING_EXCLUDE_AT_ROOT = ("evals", "tests", "CHANGELOG.md")


def _plugin_dirs(case: Path, spec: dict, into: Path) -> list[Path]:
    """One `--plugin-dir` per entry in the case's `plugins:` list.

    The first entry must be this skill; any further entry must be another skill
    under `skills/` (a directory with a SKILL.md), copied without its answer
    key. Anything else stops the harness: a case naming a plugin this harness
    would silently skip is a case that cannot be graded as written.
    """
    entries = spec.get("plugins") or ["../.."]
    out: list[Path] = []
    for i, entry in enumerate(entries):
        src = (case / entry).resolve()
        if i == 0:
            if src != _SKILL:
                _die(f"{case.name}: the first plugins entry must be the skill under "
                     f"test ({_SKILL}), got {src}")
            out.append(_plugin_dir(into))
            continue
        if src.parent != _SKILL.parent or not (src / "SKILL.md").is_file():
            _die(f"{case.name}: plugins entry {entry!r} is not a sibling skill "
                 f"under {_SKILL.parent}")
        dest = into / src.name
        shutil.copytree(
            src, dest,
            ignore=lambda d, names, _s=src: [
                n for n in names
                if (Path(d) == _s and n in _SIBLING_EXCLUDE_AT_ROOT)
                or n == "__pycache__"])
        out.append(dest)
    return out


def _run_agent(prompt: str, sandbox: Path, plugin: Path | list[Path], max_turns: int,
               timeout: int, tmpdir: Path | None = None,
               fixtures: Path | None = None, shim_dir: Path | None = None) -> str:
    """One headless session against the sandbox. Returns the raw stream.

    Exits 2 rather than returning a stream no session produced (see the
    ci-secure harness: an auth failure, a root refusal or an unknown flag all
    yield an empty stdout that would grade as a full behavioural regression).
    """
    plugins = plugin if isinstance(plugin, list) else [plugin]
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        *[a for p in plugins for a in ("--plugin-dir", str(p))],
        *_ISOLATION_FLAGS,
    ]
    try:
        r = subprocess.run(
            cmd, cwd=sandbox, capture_output=True, text=True, timeout=timeout,
            env=_env(tmpdir, fixtures, shim_dir),
        )
    except subprocess.TimeoutExpired:
        _die(f"agent run exceeded {timeout}s", 2)

    def _no_session(reason: str) -> None:
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
    r"""The decoded transcript the regex graders are written against: the
    agent's own prose and the output of the tools it ran, never the prompt.

    Decoded, not the raw stream-json: inside a JSON string a newline is the two
    characters `\` and `n`, so `[^\n]` would stop meaning "on one line".
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
    """Every regular, non-`.git`, non-symlink file under `roots`, sorted."""
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


_REPORT_PATH_IN_COMMAND = re.compile(
    r"""(?:^|[\s='"])(/[^\s'"`;|&<>]*/""" + re.escape(_REPORT_NAME) + r")")


def _named_reports(tools: list[tuple[str, str]], roots: list[Path],
                   since: float) -> list[Path]:
    """Report files a Bash command of this session named, outside `roots`.

    SKILL.md leaves the scratch directory to the agent. Under the harness's
    TMPDIR it is collected with the rest of the root; a literal `/tmp/...` is
    not, and grading without it would score "the report says something else"
    when the truth is "the harness looked in the wrong place".

    Only an absolute path ending in the report's own name, only a regular file
    (never a symlink), and only one modified after `since`: a stale report left
    by an earlier run at the same path must not be graded as this session's.
    """
    found: list[Path] = []
    for tool, raw in tools:
        if tool != "Bash":
            continue
        try:
            command = str(json.loads(raw).get("command", ""))
        except (json.JSONDecodeError, AttributeError):
            continue
        for m in _REPORT_PATH_IN_COMMAND.finditer(command):
            p = Path(m.group(1))
            if any(_is_under(p, r) for r in roots):
                continue
            try:
                if p.is_symlink() or not p.is_file() or p.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            if p not in found:
                found.append(p)
    return found


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _files_corpus(roots: list[Path], before: dict[str, str],
                  extra: list[Path] | None = None) -> str:
    """The files the SESSION produced, as one greppable text.

    Not the whole sandbox: the scaffold hands the agent the synthetic repo's
    workflow YAML, which names the very jobs the graders assert on. A file is
    in only if it did not exist before the session or its contents changed
    during it. `extra` are report files found by `_named_reports`, introduced
    by their absolute path.
    """
    parts: list[str] = []

    def _add(label: str, p: Path) -> None:
        try:
            raw = p.read_bytes()
        except OSError:
            return
        if len(raw) > _MAX_CORPUS_FILE:
            parts.append(f"=== {label} (NOT READ: {len(raw)} bytes) ===")
            return
        try:
            parts.append(f"=== {label} ===\n{raw.decode('utf-8')}")
        except UnicodeDecodeError:
            parts.append(f"=== {label} (NOT READ: not UTF-8 text) ===")

    for root in roots:
        if not root.is_dir():
            continue
        for p in _corpus_files([root]):
            try:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            if before.get(str(p)) == digest:
                continue  # handed to the session, not written by it
            _add(p.relative_to(root).as_posix(), p)
    for p in extra or []:
        _add(p.as_posix(), p)
    return "\n".join(parts)


def _grade(graders: list[dict], stream: str, tools: list[tuple[str, str]],
           files: str = "", last_message: str = "") -> tuple[list, list]:
    """(scored results, unscored llm graders). Identical semantics to the
    ci-secure harness; only the report a `files` grader is anchored on differs."""
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
            target = g.get("target", "trace")
            if target not in corpora:
                _die(f"{name}: grader target {target!r} is not implemented — "
                     "this harness cannot score the case, so it must not "
                     "report one")
            if target == "files" and not _REPORT_HEADER.search(files):
                _die(f"{name}: targets `files`, but the session wrote no "
                     f"{_REPORT_NAME} the harness could collect — the graders "
                     "anchored on the report had no evidence to read, and that "
                     "is not a behavioural failure")
            found = bool(re.search(pat, corpora[target]))
            if mode == "not_contains":
                scored.append((name, not found, "absent" if not found else "PRESENT"))
            elif mode.startswith("count:"):
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
            _die(f"{name}: grader type {kind!r} is not implemented — this "
                 "harness cannot score the case, so it must not report one")
    return scored, unscored


def _record(case: str, run: int, stream: str, corpus: str) -> None:
    """Keep a session and the files it produced, so a failure can be read
    rather than reproduced. The workflow uploads `skill-eval-logs/`."""
    log = _LOGS / f"ci-speedup-{case}-run{run}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(stream, encoding="utf-8")
    print(f"    session recorded: {log}")
    corpus_log = _LOGS / f"ci-speedup-{case}-run{run}.files.txt"
    corpus_log.write_text(corpus, encoding="utf-8")
    print(f"    files the run produced: {corpus_log}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--case", help="run one case by directory name")
    ap.add_argument("--runs", type=int, default=1, help="runs per case (default 1)")
    ap.add_argument("--keep", action="store_true", help="keep sandboxes and record every session, passing or not")
    ap.add_argument(
        "--fixtures-override", type=Path, default=None,
        help="replay from THIS directory instead of the built corpus. A grader "
             "check only: pointing it at an empty directory must turn the "
             "full-run case red, which is how a maintainer proves the graders "
             "discriminate. Never use it for a real verdict.")
    args = ap.parse_args(argv)

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
        timeout = ex.get("timeout_seconds", 900)

        for run in range(1, args.runs + 1):
            sandbox = Path(tempfile.mkdtemp(prefix=f"skill-eval-{case.name}-"))
            plugin_root = Path(tempfile.mkdtemp(prefix=f"skill-eval-plugin-{case.name}-"))
            artifacts = Path(tempfile.mkdtemp(prefix=f"skill-eval-out-{case.name}-"))
            # Outside both graded roots: the replay corpus is input, and its job
            # logs name the jobs the graders assert on.
            replay_root = Path(tempfile.mkdtemp(prefix=f"skill-eval-replay-{case.name}-"))
            try:
                _scaffold(case, sandbox)
                fixtures = (args.fixtures_override if args.fixtures_override
                            else _replay_fixtures(replay_root,
                                                  timeout + _REPLAY_SLACK_S))
                before = _snapshot([sandbox, artifacts])
                started = time.time()
                stream = _run_agent(
                    prompt, sandbox, _plugin_dirs(case, spec, plugin_root),
                    ex.get("max_turns", 60), timeout, artifacts, fixtures,
                    _gh_shim(replay_root / "bin"),
                )
                tools = _tool_calls(stream)
                roots = [sandbox, artifacts]
                extra = _named_reports(tools, roots, started)
                corpus = _files_corpus(roots, before, extra)
                try:
                    scored, unscored = _grade(
                        spec.get("graders", []), _transcript(stream), tools,
                        files=corpus, last_message=_last_message(stream))
                except SystemExit:
                    # A session that ran but could not be graded (its report was
                    # never collected, say) is exactly the one somebody has to
                    # read. Keep it before the exit-2 propagates.
                    _record(case.name, run, stream, corpus)
                    raise
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
                for p in extra:
                    print(f"    report collected from: {p}")
                if not ok or args.keep:
                    _record(case.name, run, stream, corpus)
            finally:
                shutil.rmtree(plugin_root, ignore_errors=True)
                shutil.rmtree(replay_root, ignore_errors=True)
                if not args.keep:
                    shutil.rmtree(sandbox, ignore_errors=True)
                    shutil.rmtree(artifacts, ignore_errors=True)
                else:
                    print(f"    sandbox kept: {sandbox}")
                    print(f"    session output kept: {artifacts}")

    return 1 if failed else 0


def cli() -> int:
    """`main`, with every unexpected failure mapped onto the could-not-run code
    (Python exits 1 on an uncaught exception, and 1 means "a case ran and
    failed")."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the exit code is the point
        _die(f"unhandled harness error: {type(exc).__name__}: {exc}")
        raise  # unreachable; _die always raises


if __name__ == "__main__":
    raise SystemExit(cli())
