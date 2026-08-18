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

Exit 0 when every selected case passes every mechanical grader, 1 otherwise, 2
when the harness itself could not run (no `claude`, no PyYAML, scaffold failure)
— a distinction that matters, because a harness that could not run is not a
suite that passed.

`llm` graders are REPORTED, NOT SCORED. They need a judge model; scoring them
here without one would be inventing a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[3] / "skills" / "ci-secure"
_EVALS = _SKILL / "evals"


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
        ["bash", str(script)], cwd=sandbox, capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        _die(f"{case.name}: scaffold failed: {r.stderr.strip()[-400:]}")


def _run_agent(prompt: str, sandbox: Path, max_turns: int, timeout: int) -> str:
    """One headless session against the sandbox. Returns the raw stream."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        "--plugin-dir", str(_SKILL),
    ]
    try:
        r = subprocess.run(
            cmd, cwd=sandbox, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        _die(f"agent run exceeded {timeout}s", 2)
    if "early access" in r.stdout[:400] or "early access" in r.stderr[:400]:
        _die("claude refused the run (early access gate)")
    return r.stdout


def _tool_calls(stream: str) -> list[tuple[str, str]]:
    """(tool_name, json-encoded input) for every tool_use in the stream."""
    out: list[tuple[str, str]] = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                out.append((c.get("name", ""), json.dumps(c.get("input", {}))))
    return out


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
            unscored.append((name, f"unknown grader type {kind!r}"))
    return scored, unscored


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--case", help="run one case by directory name")
    ap.add_argument("--runs", type=int, default=1, help="runs per case (default 1)")
    ap.add_argument("--keep", action="store_true", help="keep sandboxes for debugging")
    args = ap.parse_args()

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
            try:
                _scaffold(case, sandbox)
                stream = _run_agent(
                    prompt, sandbox,
                    ex.get("max_turns", 60), ex.get("timeout_seconds", 900),
                )
                tools = _tool_calls(stream)
                scored, unscored = _grade(spec.get("graders", []), stream, tools)

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
            finally:
                if not args.keep:
                    shutil.rmtree(sandbox, ignore_errors=True)
                else:
                    print(f"    sandbox kept: {sandbox}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
