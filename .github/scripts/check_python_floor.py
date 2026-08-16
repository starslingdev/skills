#!/usr/bin/env python3
"""Import every shipped module on the OLDEST Python this repo supports.

`pyproject.toml` declares `requires-python = ">=3.9"` and the README promises
"python3 (3.9 or newer)". These skills are publicly installed and run against
whatever `python3` a user happens to have — while every workflow here pins
3.12. That asymmetry means CI cannot see a floor break: the version that fails
is the one version CI never runs.

There is already an AST-based guard (`skills/ci-secure/tests/test_python_compat.py`)
and it is not enough. It catches what 3.9 cannot PARSE, and PEP 604 in
annotations. It cannot catch code that parses fine on 3.9 and then fails when
the module body EXECUTES — which is most of the interesting cases:

    re.compile(r"(?P=x)++")   # possessive quantifiers: 3.11+, re.error on 3.9
    X: TypeAlias = int | str  # evaluated for real, TypeError on 3.9
    dict1 | dict2             # at module scope, TypeError on 3.8

Three real breaks were found by hand on 2026-08-15 and every one of them was
of that second kind. Only actually importing the module on a 3.9 interpreter
finds them, which is what this does.

Each module is imported in its OWN subprocess. One that dies must not take the
run down with it — the point is to report every broken module in one pass, not
the first one alphabetically.
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
SHIPPED_GLOBS = ("skills/*/scripts/*.py", "skills/*/scaffold/*.py")

# `tests/` and `maintainers/` are deliberately out of scope: they run here,
# under the version this repo pins, and never reach a user.


def shipped_modules(root: Path = REPO) -> list[Path]:
    found: list[Path] = []
    for pattern in SHIPPED_GLOBS:
        found.extend(root.glob(pattern))
    return sorted(p for p in found if p.name != "__init__.py")


def import_in_subprocess(module: Path) -> tuple[bool, str]:
    """Import one module by file location. Returns (ok, message)."""
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
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or proc.stdout).strip().splitlines()
    return False, tail[-1] if tail else f"exited {proc.returncode}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # `--root` exists so this guard can be pointed at a fixture tree and shown
    # to FAIL. A guard nobody has watched go red is a guard nobody knows works.
    parser.add_argument("--root", type=Path, default=REPO)
    args = parser.parse_args(argv)

    version = ".".join(str(n) for n in sys.version_info[:3])
    modules = shipped_modules(args.root)
    if not modules:
        print("::error::found no shipped modules to check - this guard is "
              "checking nothing, which is worse than not having it")
        return 1

    print(f"importing {len(modules)} shipped module(s) on Python {version}")
    failures = []
    for module in modules:
        rel = module.relative_to(args.root)
        ok, message = import_in_subprocess(module)
        if ok:
            print(f"  ok    {rel}")
        else:
            print(f"  FAIL  {rel}: {message}")
            failures.append((rel, message))

    if failures:
        for rel, message in failures:
            print(f"::error file={rel}::does not import on Python {version}, "
                  f"the documented floor: {message}")
        print(f"::error::{len(failures)} shipped module(s) do not import on "
              f"Python {version}. This repo pins 3.12 everywhere else, so no "
              "other check can see this - and a user on the floor gets an "
              "import error, not a degraded feature.")
        return 1

    print(f"all {len(modules)} shipped module(s) import on Python {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
