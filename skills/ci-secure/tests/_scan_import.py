"""Import ci-secure's scan module safely under the repo-wide pytest run.

The root pyproject puts several skills' `scripts/` dirs on pythonpath, and
more than one ships a `scan.py` (as well as `run.py` / `record_timing.py`) —
so a bare `import scan` can resolve to whichever module won the path race
(a sibling skill's, which is listed first). The older
ci-secure tests dodge this by running scan.py as a subprocess; tests
that need direct access to scan's internals (mocking, generators) import
through THIS shim instead: it loads ci-secure's own config/gh_utils/scan
under the right names, then restores whatever the rest of the suite had.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def assert_is_ci_secure(mod: ModuleType) -> ModuleType:
    """Fail loudly if `mod` is a SIBLING skill's file of the same name.

    A bare `import record_timing` (or `scan`, or `run`) binds whichever
    same-named module won the path race. When the sibling's copy is already in
    `sys.modules` — the normal state under the repo-wide run — the import is a
    no-op and the tests below happily assert against the WRONG skill's code,
    passing while ci-secure's own script is never executed. So every module
    load states which file won.
    """
    src = Path(mod.__file__).resolve()          # type: ignore[arg-type]
    assert src.parents[1].name == "ci-secure", (
        f"{mod.__name__} resolved to {src} — that is a sibling skill's file, "
        "not ci-secure's; these assertions would be testing the wrong code")
    return mod


def load_script(module_name: str, filename: str) -> ModuleType:
    """Load one of ci-secure's scripts by file location, under a unique name.

    No `sys.path` mutation and no reliance on import order, so nothing global
    is left changed for whatever test module runs next.
    """
    if module_name in sys.modules:
        return assert_is_ci_secure(sys.modules[module_name])
    return assert_is_ci_secure(_load(module_name, _SCRIPTS / filename))


def load_scan() -> ModuleType:
    saved = {n: sys.modules.get(n) for n in ("config", "gh_utils")}
    try:
        _load("config", _SCRIPTS / "config.py")
        _load("gh_utils", _SCRIPTS / "gh_utils.py")
        return assert_is_ci_secure(
            _load("ci_secure_scan", _SCRIPTS / "scan.py"))
    finally:
        for n, mod in saved.items():
            if mod is not None:
                sys.modules[n] = mod
            else:
                sys.modules.pop(n, None)
