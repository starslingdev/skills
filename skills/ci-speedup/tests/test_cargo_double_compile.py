"""Detection of the same local Cargo crates compiled in two job steps."""

from __future__ import annotations

from pathlib import Path

import blocking_path as bp


def _step(command: str, crate: str, profile: str, seconds: int,
          *, path: str | None = None) -> str:
    profile_line = (
        f"Finished `test` profile [unoptimized + debuginfo] target(s) in {seconds}s"
        if profile == "test"
        else f"Finished release [optimized] target(s) in {seconds}s"
    )
    return "\n".join((
        f"##[group]Run {command}",
        command,
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        f"   Compiling {crate} v0.1.0 ({path or f'/repo/crates/{crate}'})",
        f"    {profile_line}",
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
        "label": "second Cargo step wall (upper bound)",
        "value": 175.0,
        "unit": "s",
    }


def test_different_crates_and_different_profiles_stay_silent() -> None:
    assert bp._parse_log(_double_log(second="ts_goport")) is None
    assert bp._parse_log(_double_log(second_profile="release")) is None


def test_unique_local_compile_in_second_step_stays_silent() -> None:
    first = _step("cargo test -p one", "shared", "test", 181)
    later = "\n".join((
        "##[group]Run cargo test -p two",
        "cargo test -p two",
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        "   Compiling shared v0.1.0 (/repo/crates/shared)",
        "   Compiling unique v0.1.0 (/repo/crates/unique)",
        "    Finished `test` profile [unoptimized + debuginfo] target(s) in 175s",
    ))
    assert bp._parse_log(f"{first}\n{later}") is None


def test_later_selected_crate_is_its_own_test_harness_not_a_repeat() -> None:
    first = _step("cargo test -p app", "core", "test", 181)
    later = _step("cargo test --package core", "core", "test", 175)
    assert bp._parse_log(f"{first}\n{later}") is None


def test_later_selected_crate_in_build_command_is_a_repeat() -> None:
    first = _step("cargo build --lib -p app", "core", "release", 181)
    later = _step("cargo build --lib -p core", "core", "release", 175)
    leaf = bp._parse_log(f"{first}\n{later}")
    assert leaf is not None
    assert leaf["fix_key"] == "cargo-double-compile"


def test_shared_dependency_rebuilt_beside_selected_crate_is_a_repeat() -> None:
    first = _step("cargo test -p app", "shared", "test", 181)
    later = "\n".join((
        "##[group]Run cargo test -p core",
        "cargo test -p core",
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        "   Compiling shared v0.1.0 (/repo/crates/shared)",
        "   Compiling core v0.1.0 (/repo/crates/core)",
        "    Finished `test` profile [unoptimized + debuginfo] target(s) in 175s",
    ))
    leaf = bp._parse_log(f"{first}\n{later}")
    assert leaf is not None
    assert leaf["fix_key"] == "cargo-double-compile"
    assert "shared" in "\n".join(leaf["evidence"])
    assert "Compiling core" not in "\n".join(leaf["evidence"])


def test_later_unscoped_cargo_command_stays_silent() -> None:
    first = _step("cargo test -p app", "shared", "test", 181)
    later = _step("cargo test", "shared", "test", 175)
    assert bp._parse_log(f"{first}\n{later}") is None


def test_crate_selected_by_both_commands_can_still_be_a_repeat() -> None:
    first = _step("cargo test -p core", "core", "test", 181)
    later = _step("cargo test --package core", "core", "test", 175)
    leaf = bp._parse_log(f"{first}\n{later}")
    assert leaf is not None
    assert leaf["fix_key"] == "cargo-double-compile"


def test_glob_selected_package_harness_is_not_a_repeat() -> None:
    first = _step("cargo test -p app", "core-utils", "test", 181)
    later = _step("cargo test -p 'core-*'", "core-utils", "test", 175)
    assert bp._parse_log(f"{first}\n{later}") is None


def test_package_like_harness_arguments_do_not_hide_a_shared_repeat() -> None:
    first = _step("cargo test -p app", "shared", "test", 181)
    later = "\n".join((
        "##[group]Run cargo test -p core -- -p shared",
        "cargo test -p core -- -p shared",
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        "   Compiling shared v0.1.0 (/repo/crates/shared)",
        "   Compiling core v0.1.0 (/repo/crates/core)",
        "    Finished `test` profile [unoptimized + debuginfo] target(s) in 175s",
    ))
    leaf = bp._parse_log(f"{first}\n{later}")
    assert leaf is not None
    assert leaf["fix_key"] == "cargo-double-compile"
    assert "shared" in "\n".join(leaf["evidence"])


def test_attached_short_package_selector_is_scoped() -> None:
    first = _step("cargo test -p app", "core", "test", 181)
    later = _step("cargo test -pcore", "core", "test", 175)
    assert bp._parse_log(f"{first}\n{later}") is None


def test_different_artifact_contexts_stay_silent() -> None:
    first = _step("cargo test -p goport_core", "goport_core", "test", 181)
    variants = (
        "cargo test -p ts_goport --target aarch64-unknown-linux-gnu",
        "cargo test -p ts_goport --features serde",
        "cargo test -p ts_goport --target-dir target/isolated",
        "cargo +nightly test -p ts_goport",
        "RUSTFLAGS=-Ctarget-cpu=native cargo test -p ts_goport",
        "cargo check -p ts_goport",
    )
    for command in variants:
        later = _step(command, "goport_core", "test", 175)
        assert bp._parse_log(f"{first}\n{later}") is None, command


def test_arbitrary_machine_local_path_is_not_called_in_repo() -> None:
    first = _step("cargo test -p one", "shared", "test", 181,
                  path="/opt/shared/shared")
    later = _step("cargo test -p two", "shared", "test", 175,
                  path="/opt/shared/shared")
    assert bp._parse_log(f"{first}\n{later}") is None


def test_double_compile_can_crown_build_scan_and_test_poles() -> None:
    leaf = bp._parse_log(_double_log())
    assert leaf is not None
    for category in ("build", "scan", "test"):
        assert not bp._offcategory_leaf(leaf, {"dominant_category": category})
    assert bp._offcategory_leaf(leaf, {"dominant_category": "install"})


def test_double_compile_prompt_aligns_package_and_feature_selection() -> None:
    leaf = bp._parse_log(_double_log())
    assert leaf is not None
    prompt = bp._build_agent_prompt(
        leaf,
        {"workflow_file": ".github/workflows/ci.yml", "check": "test", "job": "test"},
        [], None, "example/repo", None, 1, 1,
    )
    assert "combine" in prompt.lower()
    assert "-p" in prompt or "--package" in prompt
    assert "--features" in prompt
    assert "every original test target" in prompt.lower()


def test_opt16_catalog_names_duplicate_cargo_compilation() -> None:
    path = (Path(__file__).resolve().parents[1] / "references" / "patterns"
            / "opt16-within-job-duplicate-commands.md")
    text = path.read_text(encoding="utf-8").lower()
    assert "compiling <crate>" in text
    assert "observable profile" in text
    assert "upper bound" in text
