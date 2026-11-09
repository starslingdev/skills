"""Detection of the same local Cargo crates compiled in two job steps."""

from __future__ import annotations

from pathlib import Path

import blocking_path as bp


def _step(command: str, crate: str, profile: str, seconds: int) -> str:
    profile_line = (
        f"Finished `test` profile [unoptimized + debuginfo] target(s) in {seconds}s"
        if profile == "test"
        else f"Finished release [optimized] target(s) in {seconds}s"
    )
    return "\n".join((
        f"##[group]Run {command}",
        f"   Compiling {crate} v0.1.0 (/repo/crates/{crate})",
        f"    {profile_line}",
        "##[endgroup]",
    ))


def _double_log(*, first: str = "goport_core", second: str = "goport_core",
                first_profile: str = "test", second_profile: str = "test") -> str:
    return "\n".join((
        _step("cargo test -p goport_core", first, first_profile, 181),
        _step("cargo test -p ts_goport", second, second_profile, 175),
    ))


def test_same_local_crate_compiled_twice_reports_second_compile_wall() -> None:
    leaf = bp._parse_log(_double_log())
    assert leaf is not None
    assert leaf["fix_key"] == "cargo-double-compile"
    assert leaf["magnitude"] == {
        "label": "redundant second Cargo compile wall",
        "value": 175.0,
        "unit": "s",
    }


def test_different_crates_and_different_profiles_stay_silent() -> None:
    assert bp._parse_log(_double_log(second="ts_goport")) is None
    assert bp._parse_log(_double_log(second_profile="release")) is None


def test_double_compile_prompt_aligns_package_and_feature_selection() -> None:
    leaf = bp._parse_log(_double_log())
    assert leaf is not None
    prompt = bp._build_agent_prompt(
        leaf,
        {"workflow_file": ".github/workflows/ci.yml", "check": "test", "job": "test"},
        [], None, "example/repo", None, 1, 1,
    )
    assert "-p" in prompt or "--package" in prompt
    assert "--features" in prompt
    assert "test target" in prompt.lower()


def test_opt16_catalog_names_duplicate_cargo_compilation() -> None:
    path = (Path(__file__).resolve().parents[1] / "references" / "patterns"
            / "opt16-within-job-duplicate-commands.md")
    text = path.read_text(encoding="utf-8").lower()
    assert "compiling <crate>" in text
    assert "same profile" in text
