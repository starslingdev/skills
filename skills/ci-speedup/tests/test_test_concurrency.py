"""Concurrency guidance for isolated serial tests and shard-adding prompts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import blocking_path as bp
import verify_report as vr


_COST_PREFIX = "Runner-minute effect:"
_ORDER_PREFIX = "Concurrency order:"
_CHECK_NAME = "job-adding prompts state their runner-minute effect"


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _isolated_log(durations: tuple[int, ...] = (60, 60, 60, 60),
                  *, markers: bool = True) -> str:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    lines: list[str] = []
    for index, duration in enumerate(durations, 1):
        node = f"tests/test_accounts.py::test_account_{index}"
        lines.append(f"{_stamp(at)} START {node}")
        if markers:
            lines.append(
                f"{_stamp(at + timedelta(seconds=1))} "
                f"creating schema_test_{index}"
            )
        at += timedelta(seconds=duration)
        lines.append(f"{_stamp(at)} PASS {node}")
    return "\n".join(lines)


def _overlapping_timestamp_log() -> str:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    lines: list[str] = []
    for index in range(1, 5):
        started = at + timedelta(seconds=(index - 1) * 30)
        node = f"tests/test_accounts.py::test_account_{index}"
        lines += [
            f"{_stamp(started)} START {node}",
            f"{_stamp(started + timedelta(seconds=1))} creating schema_test_{index}",
            f"{_stamp(started + timedelta(seconds=60))} PASS {node}",
        ]
    return "\n".join(lines)


def _post_completion_marker_log() -> str:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    lines: list[str] = []
    for index in range(1, 5):
        node = f"tests/test_accounts.py::test_account_{index}"
        lines += [
            f"{_stamp(at)} START {node}",
            f"{_stamp(at + timedelta(seconds=60))} PASS {node}",
            f"{_stamp(at + timedelta(seconds=61))} creating schema_test_{index}",
        ]
        at += timedelta(seconds=62)
    return "\n".join(lines)


def _repeated_node_log() -> str:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    node = "tests/test_accounts.py::test_retried_account"
    lines: list[str] = []
    for index in range(1, 5):
        lines += [
            f"{_stamp(at)} START {node}",
            f"{_stamp(at + timedelta(seconds=1))} creating schema_test_{index}",
            f"{_stamp(at + timedelta(seconds=60))} PASS {node}",
        ]
        at += timedelta(seconds=60)
    return "\n".join(lines)


def _pole() -> dict:
    return {
        "workflow_file": ".github/workflows/ci.yml",
        "check": "isolated tests",
        "job": "isolated-tests",
        "p50_s": 240.0,
    }


def _pattern_prompt(pattern: str) -> str:
    member = {
        "pattern": pattern,
        "title": "Long tests",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["test"],
        "evidence": "one test job is the measured long pole",
        "wall_clock_p50_s": 240.0,
        "shard_family": "test",
    }
    return "\n".join(bp._hygiene_prompt(
        pattern, "Long tests", [member], "https://example.invalid/catalog",
        saves_wc=True,
    ))


def test_isolated_same_file_tests_get_the_concurrency_leaf() -> None:
    leaf = bp._parse_log(_isolated_log())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_isolated_test_leaf_rejects_varied_timers_and_missing_markers() -> None:
    assert bp._parse_log(_isolated_log((60, 72, 60, 48))) is None
    assert bp._parse_log(_isolated_log(markers=False)) is None
    assert bp._parse_log(_overlapping_timestamp_log()) is None
    assert bp._parse_log(_post_completion_marker_log()) is None
    assert bp._parse_log(_repeated_node_log()) is None


def test_isolated_test_prompt_names_safety_and_lower_runner_minutes() -> None:
    leaf = bp._parse_log(_isolated_log())
    assert leaf is not None
    prompt = bp._build_agent_prompt(
        leaf, _pole(), [], None, "example/repo", None, 1, 1,
    )
    lowered = prompt.lower()
    assert "run the isolated tests concurrently" in lowered
    assert "shared state" in lowered
    assert "runner minutes go down" in lowered


def test_shard_prompts_put_in_job_workers_before_added_shards() -> None:
    for pattern in ("OPT24", "OPT25"):
        prompt = _pattern_prompt(pattern)
        assert _ORDER_PREFIX in prompt
        assert "OPT88" in prompt
        assert _COST_PREFIX in prompt
        assert prompt.index(_ORDER_PREFIX) < prompt.lower().index("add shards")
        assert "repeat checkout and setup" in prompt
        assert "bill each job" in prompt


def test_machine_and_structural_prompts_state_runner_minute_effect() -> None:
    finding = {
        "id": "f1",
        "pattern": "OPT81",
        "title": "Runner class",
        "workflow_file": ".github/workflows/ci.yml",
        "faster_runner": {"half": "A2", "job": "test"},
    }
    opt81 = "\n".join(bp._opt81_prompt(finding, "https://example.invalid/catalog"))
    assert "Runner cost effect:" in opt81
    assert "runner minutes" in opt81

    structural = bp._build_generic_agent_prompt(
        _pole(), [], None, "example/repo", None, 1, 1, None,
        structural=[{"pattern": "OPT75"}],
    )
    assert _COST_PREFIX in structural
    assert "added jobs or machines" in structural


def test_verifier_covers_measured_runner_prompt_cost() -> None:
    finding = {
        "id": "f1",
        "pattern": "OPT81",
        "title": "Measured runner class",
        "workflow_file": ".github/workflows/ci.yml",
        "evidence": "the same job ran on two repository-owned labels",
        "faster_runner": {"half": "A1", "job": "test"},
    }
    prompt = "\n".join(bp._opt81_prompt(finding, "https://example.invalid/catalog"))
    without_cost = "\n".join(
        line for line in prompt.splitlines()
        if not line.startswith("Runner cost effect:")
    )
    broken = next(check for check in vr.run_checks(without_cost, None, None, None)
                  if check.name == _CHECK_NAME)
    assert not broken.ok


def test_runner_minute_prompt_check_is_registered() -> None:
    names = {check.name for check in vr.run_checks("# report\n", None, None, None)}
    assert _CHECK_NAME in names


def test_verifier_rejects_an_opt24_prompt_with_the_cost_line_removed() -> None:
    prompt = _pattern_prompt("OPT24")
    clean = next(check for check in vr.run_checks(prompt, None, None, None)
                 if check.name == _CHECK_NAME)
    assert clean.ok
    without_cost = "\n".join(
        line for line in prompt.splitlines() if not line.startswith(_COST_PREFIX)
    )
    broken = next(check for check in vr.run_checks(without_cost, None, None, None)
                  if check.name == _CHECK_NAME)
    assert not broken.ok


def test_verifier_rejects_shard_order_guidance_moved_after_its_cost() -> None:
    lines = _pattern_prompt("OPT24").splitlines()
    order = next(line for line in lines if line.startswith(_ORDER_PREFIX))
    lines.remove(order)
    lines.insert(lines.index("```"), order)
    moved = "\n".join(lines)
    broken = next(check for check in vr.run_checks(moved, None, None, None)
                  if check.name == _CHECK_NAME)
    assert not broken.ok


def test_verifier_covers_structural_context_beside_a_log_leaf() -> None:
    leaf = {
        "fix_key": "dependency-cache",
        "unit_label": "dependency cache misses",
        "evidence": [],
    }
    prompt = bp._build_agent_prompt(
        leaf, _pole(), [], None, "example/repo", None, 1, 1,
        structural=[{"pattern": "OPT75"}],
    )
    assert "Structural cost context:" in prompt
    without_cost = "\n".join(
        line for line in prompt.splitlines() if not line.startswith(_COST_PREFIX)
    )
    broken = next(check for check in vr.run_checks(without_cost, None, None, None)
                  if check.name == _CHECK_NAME)
    assert not broken.ok


def test_opt88_documents_common_idle_core_defaults_without_sizing() -> None:
    path = Path(__file__).resolve().parents[1] / "references" / "patterns" / \
        "opt88-measure-test-concurrency.md"
    text = path.read_text(encoding="utf-8")
    assert "| Configuration signal |" in text
    start = text.index("| Configuration signal |")
    end = text.index("\n\n", start)
    table = text[start:end]
    for expected in (
        "`workers: 1` under CI",
        "Playwright",
        "`pytest -n auto`",
        "Maven without `-T`",
        "CTest without `-j`",
    ):
        assert expected in table
    assert "seconds" not in table.lower()
    assert not any(token.endswith("s") and token[:-1].isdigit()
                   for token in table.replace("|", " ").split())
