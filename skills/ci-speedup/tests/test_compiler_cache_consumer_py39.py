"""The compiler-cache consumer pattern must compile on Python 3.9.

`_detect_opt61` builds `compiler_cache_consumer_re` each time it inspects a
job that declares a compiler cache. It once used possessive quantifiers
(`[ \\t]++`), which only exist from Python 3.11; on 3.9/3.10 (macOS ships 3.9)
the scan died with `re.error: multiple repeat` and no report was produced.
The CI interpreter is newer, so only running this on 3.9 shows the break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scan

_CATALOG = Path(__file__).resolve().parents[1] / "references" / "patterns"


def _opt61_fires(tmp_path: Path, command: str) -> bool:
    """True when `command` counts as a compiler-cache consumer.

    The job saves sccache at a path that differs from SCCACHE_DIR, so OPT61
    fires exactly when the step command is recognised as a consumer.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(f"""name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      SCCACHE_DIR: /var/cache/sccache
      RUSTC_WRAPPER: sccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/sccache
          key: sccache-${{{{ github.sha }}}}
      - run: {command}
""", encoding="utf-8")
    rows = scan.scan(tmp_path, _CATALOG)["findings"]
    return "OPT61" in {str(row["pattern"]) for row in rows}


@pytest.mark.parametrize("command", [
    "just build", "task build", "pnpm install", "yarn build", "bun test",
    "rustc main.rs", "rustc --edition 2021 main.rs",
])
def test_consumer_commands_are_recognised(tmp_path: Path, command: str) -> None:
    assert _opt61_fires(tmp_path, command), command


@pytest.mark.parametrize("command", [
    "just --list", "pnpm --version", "pnpm  -v", "pnpm help", "yarn cache clean",
    "rustc --version", "rustc -Vv", "rustc  --print cfg",
])
def test_non_consumer_commands_are_not(tmp_path: Path, command: str) -> None:
    assert not _opt61_fires(tmp_path, command), command
