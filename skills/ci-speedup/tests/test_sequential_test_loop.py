"""Detection and runner-advisory handling for sequential shell test loops."""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import blocking_path as bp  # noqa: E402


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _loop_log(count: int = 12, *, spacing_s: int = 70) -> str:
    start = datetime(2026, 11, 5, tzinfo=timezone.utc)
    lines = []
    for i in range(count):
        at = start + timedelta(seconds=i * spacing_s)
        path = f"tests/unit_{i}.sh"
        lines.append(f"{_stamp(at)} + ./run-test.sh {path}")
        lines.append(f"{_stamp(at + timedelta(seconds=70))} done {path}")
    return "\n".join(lines)


def test_serial_test_loop_leaf_requires_ten_non_overlapping_file_invocations():
    leaf = bp._parse_log(_loop_log())
    assert leaf is not None and leaf["fix_key"] == "test-loop-serial"


def test_serial_test_loop_leaf_rejects_overlapping_invocations():
    assert bp._parse_log(_loop_log(spacing_s=35)) is None


def test_serial_test_loop_leaf_rejects_only_eight_invocations():
    assert bp._parse_log(_loop_log(count=8)) is None


def test_serial_test_loop_leaf_rejects_short_loop_with_long_tail():
    log = _loop_log() + "\n" + (
        "2026-11-05T00:33:00.0000000Z other test work completed")
    assert bp._parse_log(log) is None


def test_dependency_cache_leaf_keeps_precedence_over_serial_test_loop():
    log = _loop_log() + "\nCache not found for input keys: deps-linux"
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "dependency-cache"


def test_cache_hit_does_not_hide_a_serial_test_loop():
    leaf = bp._parse_log(_loop_log() + "\nCache restored from key: deps-linux")
    assert leaf is not None and leaf["fix_key"] == "test-loop-serial"


def test_cache_hit_alone_remains_a_dependency_cache_observation():
    leaf = bp._parse_log("Cache restored from key: deps-linux")
    assert leaf is not None and leaf["fix_key"] == "dependency-cache"


_EXPECTED_PARSE_LOG_LEAF_ORDER = (
    "cache-effective-path-mismatch",
    "prisma-migrate-once",
    "vitest-v8-coverage",
    "vitest-isolate-pool",
    "vitest-import-bound",
    "playwright-parallel",
    "install-lifecycle-build",
    "turbo-remote-cache",
    "turbo-partial-cache",
    # Deliberate exception: a dominant emulated compile beats generic BuildKit
    # cache advice, even when the same log also contains a cache miss.
    "emulated-image-compile",
    "buildx-no-cache",
    "pytest-no-xdist",
    "eslint-no-cache",
    "cargo-test-shard",
    "benchmark-serial-reruns",
    "android-emulator-shard",
    "gradle-test-parallelism",
    "dependency-cache",
    "test-isolated-serial",
    "test-loop-serial",
)


_LEAF_HELPER_KEYS = {
    "_cache_path_leaf": "cache-effective-path-mismatch",
    "_emulated_image_compile_leaf": "emulated-image-compile",
}


def _parse_log_leaf_order(source: str) -> tuple[str, ...]:
    """Read every possible leaf identity in its source-precedence slot."""
    tree = ast.parse(textwrap.dedent(source))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_log")
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "fix_key"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    found.append((node.lineno, node.col_offset, value.value))
        elif (isinstance(node, (ast.Assign, ast.AnnAssign))
              and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)):
            targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
            if any(isinstance(target, ast.Name) and target.id == "fix_key"
                   for target in targets):
                found.append((node.lineno, node.col_offset, node.value.value))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id.endswith("_leaf")):
            found.append((
                node.lineno,
                node.col_offset,
                _LEAF_HELPER_KEYS.get(node.func.id, f"unknown-helper:{node.func.id}"),
            ))

    ordered: list[str] = []
    for _line, _column, key in sorted(found):
        if key not in ordered:
            ordered.append(key)
    return tuple(ordered)


def _assert_parse_log_leaf_order(source: str) -> None:
    assert _parse_log_leaf_order(source) == _EXPECTED_PARSE_LOG_LEAF_ORDER


def test_parse_log_leaf_order_is_closed_world():
    _assert_parse_log_leaf_order(inspect.getsource(bp._parse_log))


def test_parse_log_leaf_order_guard_rejects_moving_an_existing_leaf():
    source = inspect.getsource(bp._parse_log)
    mutant = source.replace(
        '"fix_key": "prisma-migrate-once"',
        '"fix_key": "__move_leaf__"',
        1,
    ).replace(
        '"fix_key": "vitest-v8-coverage"',
        '"fix_key": "prisma-migrate-once"',
        1,
    ).replace(
        '"fix_key": "__move_leaf__"',
        '"fix_key": "vitest-v8-coverage"',
        1,
    )

    with pytest.raises(AssertionError):
        _assert_parse_log_leaf_order(mutant)


def test_parse_log_leaf_order_guard_rejects_an_inserted_leaf():
    source = inspect.getsource(bp._parse_log)
    marker = "    # --- A: per-DB-engine test files"
    mutant = source.replace(
        marker,
        '    if False:\n        return {"fix_key": "new-leaf"}\n\n' + marker,
        1,
    )

    with pytest.raises(AssertionError):
        _assert_parse_log_leaf_order(mutant)


def test_runner_advisory_explains_why_a_serial_loop_holds_it_back():
    finding = {
        "id": "f9",
        "pattern": "OPT81",
        "workflow_file": ".github/workflows/ci.yml",
        "faster_runner": {"half": "A2", "job": "test"},
        "fix_recipe_anchor": "opt81--runner-class-upgrade",
    }
    card = "\n".join(bp._opt81_card(
        finding, "https://example.invalid/catalog", held_back_by="test-loop-serial"))
    assert ("a bigger runner will not help a sequential step: it runs on one core; "
            "run the work in parallel first") in card.lower()
    assert "OPT88" in card
    assert "https://example.invalid/catalog#opt88--test-concurrency-is-a-guess-not-a-measurement" in card
    assert "#opt81--runner-class-upgrade#" not in card
