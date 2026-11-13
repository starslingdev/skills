"""Concurrency guidance for isolated serial tests and shard-adding prompts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import blocking_path as bp
import scan
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


def _pytest_verbose_log(*, startup_s: int = 0) -> str:
    """GitHub runner timestamps around pytest's real ``-v`` progress lines."""
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    lines = [f"{_stamp(at)} pytest -v tests/test_accounts.py"]
    at += timedelta(seconds=startup_s)
    for index in range(1, 5):
        at += timedelta(seconds=60)
        lines.append(
            f"{_stamp(at - timedelta(seconds=1))} schema_name=schema_test_{index}"
        )
        lines.append(
            f"{_stamp(at)} tests/test_accounts.py::test_account_{index} "
            f"PASSED [{index * 25}%]"
        )
    lines.append(f"{_stamp(at)} 4 passed in 240.00s")
    return "\n".join(lines)


def _pytest_verbose_capture_log() -> str:
    """The message shape captured from ``pytest -v -s`` on 2026-11-09.

    The runner timestamps are applied to the captured physical output lines;
    pytest writes the node id, the test's print, and ``PASSED`` separately.
    """
    at = datetime(2026, 11, 9, tzinfo=timezone.utc)
    rows = [
        (0, "python3 -m pytest -v -s tests/test_accounts.py"),
        (0, "collecting ... collected 4 items"),
    ]
    for index in range(1, 5):
        rows.extend((
            ((index - 1) * 60,
             f"tests/test_accounts.py::test_account[{index - 1}] "
             f"schema_name=schema_test_{index}"),
            (index * 60, "PASSED"),
        ))
    rows.append((240, "4 passed in 240.00s"))
    return "\n".join(
        f"{_stamp(at + timedelta(seconds=offset))} {message}"
        for offset, message in rows
    )


def _pytest_mixed_verbose_capture_log() -> str:
    """A ``-v -s`` run where only alternating tests print to stdout."""
    at = datetime(2026, 11, 9, tzinfo=timezone.utc)
    rows = [(0, "python3 -m pytest -v -s tests/test_accounts.py"),
            (0, "collecting ... collected 4 items")]
    for index in range(1, 5):
        if index % 2:
            rows.extend((
                ((index - 1) * 60,
                 f"tests/test_accounts.py::test_account[{index - 1}] "
                 f"schema_name=schema_test_{index}"),
                (index * 60, "PASSED"),
            ))
        else:
            rows.extend((
                ((index - 1) * 60, f"schema_name=schema_test_{index}"),
                (index * 60,
                 f"tests/test_accounts.py::test_account[{index - 1}] PASSED "
                 f"[{index * 25}%]"),
            ))
    rows.append((240, "4 passed in 240.00s"))
    return "\n".join(
        f"{_stamp(at + timedelta(seconds=offset))} {message}"
        for offset, message in rows
    )


def _vitest_verbose_log() -> str:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    durations = (68_100, 68_400, 67_900, 68_200, 21_000, 24_000, 28_000,
                 31_000, 35_000, 41_000, 46_000)
    lines = [f"{_stamp(at)} RUN v3.2.4 /repo"]
    for index, duration_ms in enumerate(durations, 1):
        at += timedelta(milliseconds=duration_ms)
        lines.append(
            f"{_stamp(at)} ✓ tests/accounts.test.ts > accounts > case {index} "
            f"{duration_ms}ms"
        )
    lines.append(f"{_stamp(at)} Tests  11 passed (11)")
    return "\n".join(lines)


def _vitest_source_facts() -> dict:
    return {
        "isolated_test_files": {
            "tests/accounts.test.ts": {
                f"case {index}": f"schema_test_{index}"
                for index in range(1, 12)
            },
        },
    }


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


def test_pytest_verbose_runner_timestamps_get_the_concurrency_leaf() -> None:
    leaf = bp._parse_log(_pytest_verbose_log())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_pytest_verbose_s_capture_gets_the_concurrency_leaf() -> None:
    leaf = bp._parse_log(_pytest_verbose_capture_log())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_pytest_verbose_s_mixed_printed_output_gets_the_concurrency_leaf() -> None:
    leaf = bp._parse_log(_pytest_mixed_verbose_capture_log())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_pytest_verbose_s_does_not_trust_a_printed_pass_before_failure() -> None:
    log = _pytest_verbose_capture_log().replace(
        "2026-11-09T00:01:00.0000000Z PASSED",
        "2026-11-09T00:00:59.0000000Z PASSED\n"
        "2026-11-09T00:01:00.0000000Z FAILED [25%]",
        1,
    ).replace("4 passed in 240.00s", "1 failed, 3 passed in 240.00s")
    assert bp._parse_log(log) is None


def test_pytest_verbose_s_does_not_treat_printed_ok_as_an_outcome() -> None:
    log = _pytest_verbose_capture_log().replace(
        "2026-11-09T00:01:00.0000000Z PASSED",
        "2026-11-09T00:01:00.0000000Z OK",
        1,
    )
    assert bp._parse_log(log) is None


def test_pytest_verbose_requires_isolation_inside_each_test_interval() -> None:
    log = _pytest_verbose_log().replace(
        "2026-11-06T00:00:59.0000000Z schema_name=schema_test_1",
        "2026-11-06T00:01:01.0000000Z schema_name=schema_test_1",
    )
    assert bp._parse_log(log) is None


def test_pytest_verbose_ignores_startup_before_the_first_test() -> None:
    leaf = bp._parse_log(_pytest_verbose_log(startup_s=10))
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_vitest_verbose_durations_use_source_proven_isolation() -> None:
    leaf = bp._parse_log(_vitest_verbose_log(), _vitest_source_facts())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"
    assert leaf["test_count"] == 11


def _vitest_verbose_log_after_another_file(
        prior_ms: int, *, prior_has_duration: bool = True) -> str:
    """One Vitest invocation whose serial candidate is one file in a larger run."""
    lines = _vitest_verbose_log().splitlines()
    started = datetime(2026, 11, 6, tzinfo=timezone.utc)
    prior_done = started + timedelta(milliseconds=prior_ms)
    prior = f"{_stamp(prior_done)} ✓ tests/health.test.ts > health > responds"
    if prior_has_duration:
        prior += f" {prior_ms}ms"
    shifted = [lines[0], prior]
    last_when = prior_done
    for line in lines[1:-1]:
        stamp, message = line.split(" ", 1)
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        last_when = when + timedelta(milliseconds=prior_ms)
        shifted.append(f"{_stamp(last_when)} {message}")
    shifted.append(f"{_stamp(last_when)} Tests  12 passed (12)")
    return "\n".join(shifted)


def test_vitest_file_count_is_scoped_to_the_serial_candidate() -> None:
    leaf = bp._parse_log(
        _vitest_verbose_log_after_another_file(
            100, prior_has_duration=False),
        _vitest_source_facts(),
    )
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"
    assert leaf["test_count"] == 11


def test_vitest_file_span_starts_when_that_file_begins() -> None:
    leaf = bp._parse_log(
        _vitest_verbose_log_after_another_file(180_000), _vitest_source_facts())
    assert leaf is not None
    assert leaf["fix_key"] == "test-isolated-serial"


def test_vitest_verbose_rejects_overlapping_duration_sum() -> None:
    lines = _vitest_verbose_log().splitlines()
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    for index in range(1, 12):
        message = lines[index].split(" ", 1)[1]
        lines[index] = f"{_stamp(at + timedelta(seconds=index))} {message}"
    assert bp._parse_log("\n".join(lines), _vitest_source_facts()) is None


def test_vitest_verbose_rejects_overlapping_intervals_with_equal_total_span() -> None:
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    completions = (180, 200, 220, 240)
    lines = [f"{_stamp(at)} RUN v3.2.4 /repo"]
    for index, completed_s in enumerate(completions, 1):
        lines.append(
            f"{_stamp(at + timedelta(seconds=completed_s))} "
            f"✓ tests/accounts.test.ts > accounts > case {index} 60000ms"
        )
    lines.append(
        f"{_stamp(at + timedelta(seconds=240))} Tests  4 passed (4)")
    facts = {
        "isolated_test_files": {
            "tests/accounts.test.ts": {
                f"case {index}": f"schema_test_{index}" for index in range(1, 5)
            },
        },
    }

    assert bp._parse_log("\n".join(lines), facts) is None


def test_vitest_verbose_binds_each_logged_test_to_its_source_fact() -> None:
    unrelated = {
        "isolated_test_files": {
            "tests/accounts.test.ts": {
                f"other case {index}": f"schema_test_{index}"
                for index in range(1, 12)
            },
        },
    }
    assert bp._parse_log(_vitest_verbose_log(), unrelated) is None


def test_timestamped_vitest_test_count_and_prompt_name_safe_concurrency() -> None:
    leaf = bp._parse_log(_vitest_verbose_log(), _vitest_source_facts())
    assert leaf is not None
    prompt = bp._build_agent_prompt(
        leaf, _pole(), [], None, "example/repo", None, 1, 1,
    ).lower()
    assert "describe.concurrent" in prompt
    assert "maxconcurrency" in prompt
    assert "5" in prompt
    assert "source isolation evidence" in prompt


def test_vitest_source_reader_requires_a_unique_literal_per_test(tmp_path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({ test: {} })\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    source = "\n".join(
        f"test('case {index}', async () => {{\n"
        f"  await createSchema('schema_test_{index}')\n"
        "})"
        for index in range(1, 12)
    )
    (tests / "accounts.test.ts").write_text(source, encoding="utf-8")

    facts = scan._read_test_runner_isolation(tmp_path)

    assert facts["isolated_test_files"]["tests/accounts.test.ts"] == {
        f"case {index}": f"schema_test_{index}" for index in range(1, 12)
    }


def test_vitest_source_reader_rejects_unused_namespace_variables(tmp_path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({ test: {} })\n",
        encoding="utf-8",
    )
    source = "\n".join(
        f"test('case {index}', async () => {{\n"
        f"  const schemaName = 'schema_test_{index}'\n"
        "  await runAgainstSharedDatabase()\n"
        "})"
        for index in range(1, 5)
    )
    (tmp_path / "accounts.test.ts").write_text(source, encoding="utf-8")

    facts = scan._read_test_runner_isolation(tmp_path)

    assert facts["isolated_test_files"] == {}


def test_vitest_source_reader_ignores_commented_namespace_calls(tmp_path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({ test: {} })\n",
        encoding="utf-8",
    )
    source = "\n".join(
        f"test('case {index}', async () => {{\n"
        f"  // await createSchema('schema_test_{index}')\n"
        "  await runAgainstSharedDatabase()\n"
        "})"
        for index in range(1, 5)
    )
    (tmp_path / "accounts.test.ts").write_text(source, encoding="utf-8")

    facts = scan._read_test_runner_isolation(tmp_path)

    assert facts["isolated_test_files"] == {}


def test_vitest_source_reader_rejects_test_symlinks_outside_root(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({ test: {} })\n",
        encoding="utf-8",
    )
    external = tmp_path / "external.test.ts"
    external.write_text(
        "\n".join(
            f"test('case {index}', async () => {{\n"
            f"  await createSchema('schema_test_{index}')\n"
            "})"
            for index in range(1, 5)
        ),
        encoding="utf-8",
    )
    (root / "linked.test.ts").symlink_to(external)

    facts = scan._read_test_runner_isolation(root)

    assert facts["isolated_test_files"] == {}


def test_non_vitest_repo_does_not_walk_test_sources(tmp_path, monkeypatch) -> None:
    def unexpected(_root):
        raise AssertionError("non-Vitest repositories must not scan test sources")

    monkeypatch.setattr(scan, "_isolated_test_source_facts", unexpected)

    facts = scan._read_test_runner_isolation(tmp_path)

    assert facts["runner"] is None
    assert facts["isolated_test_files"] == {}


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
