"""Every test module across the suite's test paths has a unique basename.

The test directories ship no `__init__.py` and pytest runs in its default
import mode, so two files called `test_parallel_steps.py` under different
skills import as ONE module name and collection aborts with "import file
mismatch" before a single test runs. That is how main went red on 2026-10-10:
ci-score (#120) and ci-secure (#121) each added a `test_parallel_steps.py`,
each PR was green alone, and the merge of the second stopped the whole suite.
A CI that cannot collect is a CI that verifies nothing, so this pins the
invariant at the repo root, where a cross-skill read is allowed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


_TESTPATHS_RE = re.compile(r'^testpaths\s*=\s*\[(.*?)\]', re.M | re.S)


def _test_paths() -> list[Path]:
    # Read without tomllib: the suite's Python floor predates it.
    match = _TESTPATHS_RE.search((_ROOT / "pyproject.toml").read_text())
    assert match, "pyproject.toml has no pytest testpaths line"
    return [_ROOT / p for p in re.findall(r'"([^"]+)"', match.group(1))]


def test_every_test_module_basename_is_unique_across_test_paths():
    seen: dict[str, list[str]] = defaultdict(list)
    for base in _test_paths():
        if not base.is_dir():
            continue
        for path in base.rglob("test_*.py"):
            if "__pycache__" in path.parts or "fixtures" in path.parts:
                continue
            seen[path.name].append(str(path.relative_to(_ROOT)))
    clashes = {name: paths for name, paths in seen.items() if len(paths) > 1}
    assert not clashes, (
        "two test modules share one basename, so pytest imports one and refuses "
        f"to collect the other; rename one of each pair: {clashes}")


def test_the_guard_reads_the_real_test_paths():
    paths = _test_paths()
    assert len(paths) >= 5 and all(re.search(r"tests?$", str(p)) for p in paths), paths
