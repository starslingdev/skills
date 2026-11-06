"""Detection and runner-advisory handling for sequential shell test loops."""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


def test_serial_test_loop_is_last_literal_leaf_in_parser_order():
    tree = ast.parse(textwrap.dedent(inspect.getsource(bp._parse_log)))
    ordered = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if (isinstance(key, ast.Constant) and key.value == "fix_key"
                    and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                ordered.append((node.lineno, value.value))
    leaf_keys = [key for _line, key in sorted(ordered)]
    assert leaf_keys[-1] == "test-loop-serial"


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
