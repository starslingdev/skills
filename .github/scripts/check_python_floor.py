#!/usr/bin/env python3
"""Import every shipped module on the OLDEST Python this repo supports.

`pyproject.toml` declares `requires-python = ">=3.9"` and the README promises
"python3 (3.9 or newer)". These skills are publicly installed and run against
whatever `python3` a user happens to have — while every workflow here that
installs Python pins 3.12. That asymmetry means CI cannot see a floor break:
the version that fails is the one version CI never runs.

There is already an AST-based guard (`skills/ci-secure/tests/test_python_compat.py`)
and it is not enough, for two reasons. It reads only ci-secure — the other
skills have no static floor coverage at all. And it catches what 3.9 cannot
PARSE, which leaves out the code that parses fine on 3.9 and then fails when
the module body EXECUTES:

    re.compile(r"(?P<c>-)(?P=c)++")   # possessive quantifier: re.error below 3.11
    X: TypeAlias = int | str          # TypeAlias is 3.10+; the union is 3.10+ too

Both parse cleanly under a 3.9 grammar check and both raise on a 3.9
interpreter. Only importing the module for real finds them, which is what this
does. The AST guard keeps its own job: it needs no 3.9 installed, so it still
answers on a laptop that only has a current Python.

Each module is imported in its OWN subprocess. One that dies must not take the
run down with it — the point is to report every broken module in one pass, not
the first one alphabetically.

Note that running the repository's pytest suite on 3.9 is not an alternative:
the suite itself uses newer syntax in places. Importing the shipped modules is
the part that has to happen on the floor.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# What ships. `skills/<name>/scripts/` is the engine of each skill;
# `skills/<name>/scaffold/` is code copied into an adopter's repository, which
# is the code with the LEAST say over its interpreter — it runs on whatever
# `python3` the adopter has, in a repo we will never see.
#
# Recursive (`**`) on purpose. A skill is free to organise its engine into
# subdirectories, and the whole of `skills/<name>/` is copied to a user's
# machine, so a module one level deeper reaches them exactly the same way. A
# one-level pattern would walk past it and still report a clean run.
SHIPPED_GLOBS = (
    "skills/*/scripts/**/*.py",
    "skills/*/scaffold/**/*.py",
    # `verify_report.py` sits under a skill's `tests/` but is not a test. Each
    # skill's own instructions hand it to the reader as a step to run against
    # their report, so it executes on a user's interpreter like anything under
    # `scripts/` does.
    "skills/*/tests/verify_report.py",
)

# `maintainers/` is out of scope: it never leaves this repository, and neither
# do the repo's own top-level `tests/`. The pytest modules under a skill's
# `tests/` DO travel to a user — the install copies each skill directory whole
# — but running them means having pytest and choosing to invoke it, which is a
# maintainer's workflow and not a user's. Adding them here would install a test
# framework on the floor to check code no reader runs.


# Written by the probe as its final act, and looked for by the parent. See
# `import_in_subprocess`.
_COMPLETED = "__floor_probe_reached_the_end__"


def shipped_modules(root: Path = REPO) -> list[Path]:
    found: list[Path] = []
    for pattern in SHIPPED_GLOBS:
        found.extend(root.glob(pattern))
    return sorted(p for p in found if p.name != "__init__.py")


def import_in_subprocess(
    module: Path, timeout: float = 120.0
) -> tuple[bool, str, str]:
    """Import one module by file location.

    Returns (ok, headline, full output). The headline is what goes on the
    GitHub annotation; the full output is printed beneath it so a red job can
    be read without reproducing it.
    """
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('_floor_probe', {str(module)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        # Registered in sys.modules BEFORE executing, which is what the real
        # import system does and what several stdlib features assume. Without
        # it, `@dataclass` fails with a bare `AttributeError: 'NoneType' object
        # has no attribute '__dict__'` — dataclasses resolves a field's type by
        # looking the defining module up as `sys.modules[cls.__module__]`, and
        # an unregistered module is None. That is a bug in the PROBE that reads
        # exactly like a bug in the module under test, and it fired on three
        # healthy modules the first time this ran.
        "sys.modules[spec.name] = mod\n"
        # The module's own directory on sys.path: these scripts import their
        # siblings (`from config import ...`), exactly as they do when a user
        # runs them from an installed skill.
        f"sys.path.insert(0, {str(module.parent)!r})\n"
        "spec.loader.exec_module(mod)\n"
        # The verdict is "the module body ran to completion", not merely "the
        # process did not return nonzero". A module that calls sys.exit(0) at
        # import — one edit away from the `sys.exit(1)` several of these use
        # when PyYAML is absent — would otherwise be reported ok while every
        # line below the exit went unread.
        f"sys.stdout.write({_COMPLETED!r})\n"
    )
    # A module that hangs at import — waiting on stdin, or reaching for the
    # network at module scope — is as broken on the floor as one that raises,
    # and it is caught here rather than allowed to escape. Letting
    # TimeoutExpired propagate would abandon the results already collected and
    # print a traceback where the per-file annotation belongs; the whole point
    # of a subprocess per module is that one bad module reports alongside the
    # others instead of ending the run.
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return (False,
                f"still had not finished importing after {timeout:g}s", "")
    if proc.returncode == 0 and _COMPLETED in proc.stdout:
        return True, "", ""
    if proc.returncode == 0:
        return (False,
                "stopped part-way through its own import without failing",
                proc.stdout.replace(_COMPLETED, "") + proc.stderr)
    tail = (proc.stderr or proc.stdout).strip().splitlines()
    message = tail[-1] if tail else f"exited {proc.returncode}"
    if message.startswith(("ModuleNotFoundError", "ImportError")):
        # Worth separating out. This job installs one third-party package, so a
        # module that reaches for a second one fails here looking exactly like
        # a floor break while being nothing of the kind, and the contributor
        # goes hunting through 3.9 release notes for an answer that is "add it
        # to the workflow".
        message += " (a missing dependency rather than a floor break, if this "
        message += "module needs a package the job does not install)"
    # The headline is the last line, which for a chained exception is the
    # outermost one — often the least informative. The full output goes
    # alongside it so the root cause does not have to be reproduced locally.
    detail = "".join(part for part in (proc.stdout, proc.stderr) if part)
    return False, message, detail.replace(_COMPLETED, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # `--root` exists so this guard can be pointed at a fixture tree and shown
    # to FAIL. A guard nobody has watched go red is a guard nobody knows works.
    parser.add_argument("--root", type=Path, default=REPO)
    # Lowering the limit is how the hang path gets exercised without a test
    # that takes two minutes to make its point.
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds to allow each module's import")
    args = parser.parse_args(argv)

    # Resolved so a root given as a relative path, or through a symlink, still
    # matches the discovered paths when the per-file annotation is built below.
    root = args.root.resolve()
    version = ".".join(str(n) for n in sys.version_info[:3])
    modules = shipped_modules(root)
    if not modules:
        print("::error::found no shipped modules to check - this guard is "
              "checking nothing, which is worse than not having it")
        return 1

    print(f"importing {len(modules)} shipped module(s) on Python {version}")
    failures = []
    for module in modules:
        rel = module.relative_to(root)
        ok, message, detail = import_in_subprocess(module, timeout=args.timeout)
        if ok:
            print(f"  ok    {rel}")
        else:
            print(f"  FAIL  {rel}: {message}")
            for line in detail.strip().splitlines():
                print(f"        | {line}")
            failures.append((rel, message))

    if failures:
        for rel, message in failures:
            print(f"::error file={rel}::does not import on Python {version}, "
                  f"the documented floor: {message}")
        print(f"::error::{len(failures)} shipped module(s) do not import on "
              f"Python {version}. Every other check here runs a newer "
              "version, so none of them can see this - and a user on the "
              "floor gets an import error, not a degraded feature.")
        return 1

    print(f"all {len(modules)} shipped module(s) import on Python {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
