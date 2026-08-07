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


def load_scan() -> ModuleType:
    saved = {n: sys.modules.get(n) for n in ("config", "gh_utils")}
    try:
        _load("config", _SCRIPTS / "config.py")
        _load("gh_utils", _SCRIPTS / "gh_utils.py")
        return _load("ci_secure_scan", _SCRIPTS / "scan.py")
    finally:
        for n, mod in saved.items():
            if mod is not None:
                sys.modules[n] = mod
            else:
                sys.modules.pop(n, None)
