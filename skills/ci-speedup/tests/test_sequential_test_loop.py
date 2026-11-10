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
    "cargo-double-compile",
    "cargo-test-shard",
    "benchmark-serial-reruns",
    "android-emulator-shard",
    "gradle-test-parallelism",
    "lighthouse-serial-audits",
    "dependency-cache",
    "per-invocation-startup",
    "test-isolated-serial",
    "test-loop-serial",
    "vendored-lib-build",
    "dependency-cache",
)


def _dict_fix_keys(
        node: ast.Dict,
        string_values: dict[str, list[tuple[int, str]]],
        before_line: int) -> list[str]:
    for key, value in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and key.value == "fix_key"):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return [value.value]
        if isinstance(value, ast.Name):
            return [text for line, text in string_values.get(value.id, ())
                    if line < before_line]
    return []


def _helper_fix_keys(name: str) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(bp, name))))
    string_values: dict[str, list[tuple[int, str]]] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    string_values.setdefault(target.id, []).append(
                        (node.lineno, node.value.value))
    leaf_values: dict[str, list[tuple[int, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
        if not isinstance(node.value, ast.Dict):
            continue
        keys = _dict_fix_keys(node.value, string_values, node.lineno)
        for target in targets:
            if isinstance(target, ast.Name):
                leaf_values.setdefault(target.id, []).extend(
                    (node.lineno, key) for key in keys)
    keys: list[str] = []
    for node in sorted(
            (item for item in ast.walk(tree) if isinstance(item, ast.Return)),
            key=lambda item: (item.lineno, item.col_offset)):
        if isinstance(node.value, ast.Dict):
            keys.extend(_dict_fix_keys(node.value, string_values, node.lineno))
        elif isinstance(node.value, ast.Name):
            eligible = [(line, key) for line, key in leaf_values.get(node.value.id, ())
                        if line < node.lineno]
            if eligible:
                latest = max(line for line, _key in eligible)
                keys.extend(key for line, key in eligible if line == latest)
    # Alternate return shapes from one helper retain one precedence slot when
    # they route to the same leaf identity.
    return list(dict.fromkeys(keys))


def _parse_log_leaf_order(source: str) -> tuple[str, ...]:
    """Read every leaf identity at the return site that sets its precedence."""
    tree = ast.parse(textwrap.dedent(source))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_log")
    string_values: dict[str, list[tuple[int, str]]] = {}
    for node in ast.walk(function):
        if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
            if any(isinstance(target, ast.Name) and target.id == "fix_key"
                   for target in targets):
                string_values.setdefault("fix_key", []).append(
                    (node.lineno, node.value.value))

    leaf_values: dict[str, list[tuple[int, str]]] = {}
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if isinstance(node.value, ast.Dict):
            keys = _dict_fix_keys(node.value, string_values, node.lineno)
        elif (isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name)
              and node.value.func.id.endswith("_leaf")):
            keys = _helper_fix_keys(node.value.func.id)
        else:
            keys = []
        for name in names:
            leaf_values.setdefault(name, []).extend(
                (node.lineno, key) for key in keys)

    found: list[tuple[int, int, str]] = []
    for node in sorted(
            (item for item in ast.walk(function) if isinstance(item, ast.Return)),
            key=lambda item: (item.lineno, item.col_offset)):
        if isinstance(node.value, ast.Dict):
            keys = _dict_fix_keys(node.value, string_values, node.lineno)
        elif isinstance(node.value, ast.Name):
            eligible = [(line, key) for line, key in leaf_values.get(node.value.id, ())
                        if line < node.lineno]
            latest = max((line for line, _key in eligible), default=-1)
            keys = [key for line, key in eligible if line == latest]
        else:
            keys = []
        found.extend((node.lineno, node.col_offset, key) for key in keys)
    return tuple(key for _line, _column, key in found)


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


def test_parse_log_leaf_order_guard_rejects_moving_an_assigned_leaf_return():
    source = inspect.getsource(bp._parse_log)
    final_return = "    return dependency_hit_leaf\n"
    assert source.endswith(final_return)
    marker = "    # --- L: isolated tests in one file"
    mutant = source[:-len(final_return)].replace(
        marker,
        "    if dependency_hit_leaf is not None:\n"
        "        return dependency_hit_leaf\n"
        + marker,
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
