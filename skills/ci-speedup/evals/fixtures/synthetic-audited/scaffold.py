#!/usr/bin/env python3
"""Scaffold the `synthetic-audited` eval fixture: the `synthetic` fixture plus
the finished audit a post-fix verification resumes from.

    scaffold.py <empty sandbox dir> <aux dir>

Builds the `synthetic` checkout and replay corpus, then runs the data pass
offline against that corpus and writes its findings to `<aux>/audit/`, the
session's own scratch. Prints the session's environment as KEY=VALUE lines,
including CI_SPEEDUP_AUDIT_FINDINGS, the path the eval prompt names.
Maintainer harness only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[3]


def _synthetic_main():
    path = Path(__file__).resolve().parents[1] / "synthetic" / "scaffold.py"
    spec = importlib.util.spec_from_file_location("synthetic_scaffold", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def main(sandbox: Path, aux: Path) -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        _synthetic_main()(sandbox, aux)
    env_lines = out.getvalue().splitlines()
    env = dict(l.split("=", 1) for l in env_lines if "=" in l)
    findings = aux / "audit" / "findings.json"
    findings.parent.mkdir()
    run_env = dict(os.environ, CI_SPEEDUP_GH_FIXTURES=env["CI_SPEEDUP_GH_FIXTURES"])
    run_env.pop("CI_SPEEDUP_GH_RECORD", None)  # never record over the corpus
    r = subprocess.run(
        [sys.executable, str(_SKILL / "scripts" / "run.py"), "--root", str(sandbox),
         "--out", str(findings), "--repo", env["EVAL_GH_SHIM_REPO"]],
        capture_output=True, text=True, env=run_env, timeout=240)
    if r.returncode != 0 or not findings.is_file():
        sys.exit(f"scaffold: the audit data pass failed: {r.stderr.strip()[-800:]}")
    for line in env_lines:
        print(line)
    print(f"CI_SPEEDUP_AUDIT_FINDINGS={findings}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
