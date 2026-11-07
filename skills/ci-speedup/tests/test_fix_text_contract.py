from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills" / "ci-speedup" / "scripts" / "blocking_path.py"
PATTERNS = ROOT / "skills" / "ci-speedup" / "references" / "patterns"


def _load():
    spec = importlib.util.spec_from_file_location("fix_text_contract_bp", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pattern(number: int) -> str:
    path = next(PATTERNS.glob(f"opt{number}-*.md"))
    return path.read_text(encoding="utf-8")


def test_pytest_and_eslint_fixes_name_the_required_safe_configuration():
    bp = _load()
    pytest_text = " ".join(str(value) for value in bp._FIX_META["pytest-no-xdist"].values())
    eslint_text = " ".join(str(value) for value in bp._FIX_META["eslint-no-cache"].values())

    assert "-n logical" in pytest_text
    assert "worker count that actually started" in pytest_text
    assert "deadline-bound" in pytest_text and "serial pre-step" in pytest_text
    assert "require `--cache-strategy content`" in eslint_text


def test_multi_job_cache_and_concurrency_guidance_carry_their_conditions():
    bp = _load()
    opt61 = _pattern(61)
    assert "use `shared-key`" not in opt61
    assert "compilation mode" in opt61
    assert "save-if" in opt61
    assert "verify that they cannot interfere through a shared external server" in (
        bp._CONCURRENCY_ORDER_LINE)


def test_opt88_lists_the_common_worker_count_traps():
    text = _pattern(88)
    for marker in (
        "go test -p", "doctest", "gtest", "analysis tool", "--dist load",
        "worksteal", "maxWorkers", "regrtest -j", "meson test --num-processes",
        "go test -parallel",
    ):
        assert marker in text


def test_selection_fixes_require_a_measurable_safety_bar():
    for number in (70, 90):
        text = _pattern(number)
        for marker in (
            "last N merged PRs", "selectable share", "plant a defect",
            "known red commits", "changed tests", "data-file edges",
            "test neighbours", "not a speedup",
        ):
            assert marker in text, (number, marker)
