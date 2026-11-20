"""OPT13's log parser keeps GitHub step boundaries and rejects near misses.

`collect_runs._opt13_build_in_quality_log` reads one job log. Only a step
whose own `run:` starts with a lint/test/check/type-check package script, and
only command lines echoed inside that same step, can prove a build hidden in a
quality script; a log that loads compiled output anywhere fails closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import collect_runs as cr  # noqa: E402


def _log(*lines: str) -> str:
    return "".join(f"2026-01-28T00:00:{i % 60:02d}.0000000Z {line}\n"
                   for i, line in enumerate(lines))


def _quality_step(run: str, *commands: str) -> str:
    return _log(f"##[group]Run {run}", "##[endgroup]", *commands)


def test_build_echoed_inside_the_quality_step_fires():
    log = _quality_step("pnpm test", "> project@1.0.0 test",
                        "> vite build", "✓ built in 120.00s", "> vitest run",
                        "Tests 48 passed")
    assert cr._opt13_build_in_quality_log(log) == {
        "build_command": "vite build", "build_s": 120.0}


def test_separate_build_and_test_steps_stay_quiet():
    log = _log("##[group]Run pnpm build", "##[endgroup]", "> vite build",
               "✓ built in 120.00s",
               "##[group]Run pnpm test", "##[endgroup]", "> vitest run",
               "Tests 48 passed")
    assert cr._opt13_build_in_quality_log(log) is None


def test_build_in_the_step_run_line_is_not_hidden_in_a_script():
    log = _quality_step("pnpm build && pnpm test", "> vite build",
                        "✓ built in 120.00s", "> vitest run")
    assert cr._opt13_build_in_quality_log(log) is None


def test_log_without_step_headers_stays_quiet():
    log = _log("> pnpm test", "> vite build", "✓ built in 120.00s", "> vitest run")
    assert cr._opt13_build_in_quality_log(log) is None


@pytest.mark.parametrize("loader", [
    "> node --enable-source-maps dist/test.js",
    "> node --test dist/a.test.js",
    "> bun run dist/check.js",
])
def test_compiled_output_loaded_anywhere_in_the_log_stays_quiet(loader):
    log = _quality_step("pnpm test", "> vite build", "✓ built in 120.00s",
                        "> vitest run", loader)
    assert cr._opt13_build_in_quality_log(log) is None


@pytest.mark.parametrize("step_run, command", [
    # `--declaration false` and `--noEmit` emit nothing: a type check, not a build.
    ("pnpm typecheck", "> tsc -p . --declaration false"),
    ("pnpm typecheck", "> tsc --noEmit --declaration"),
    # A bundler named only as a config-file argument is not a bundle build.
    ("pnpm test", "> vitest run --config webpack.config.test.js"),
    # A script whose own name says it builds hides nothing.
    ("npm run test-build", "> vite build"),
    ("pnpm run lint:build", "> tsup"),
])
def test_near_miss_commands_stay_quiet(step_run, command):
    log = _quality_step(step_run, command, "✓ built in 120.00s", "> vitest run")
    assert cr._opt13_build_in_quality_log(log) is None


def test_tsc_type_check_is_not_a_validation_build():
    docs = {"ci.yml": {"jobs": {
        "test": {"steps": [{"run": "pnpm test"}]},
        "types": {"steps": [{"run": "tsc --noEmit --declaration"}]},
    }}}
    assert cr._opt13_validation_build_job(docs, "ci.yml", "test") is None


@pytest.mark.parametrize("source", [
    "const r = require(path.resolve('dist/runtime.js'));\n",
    "const r = await import(`../dist/runtime.js`);\n",
    "const r = require(\n  '../dist/runtime.js'\n);\n",
    "import '../dist/register.js';\n",
    "const p = path.join(__dirname, 'dist', 'worker.js');\nawait import(p);\n",
    "const file = new URL('../build/cli.mjs', import.meta.url);\nawait import(file);\n",
    "const target = resolveOutput('lib/index.js');\nloadPlugin(target);\n",
])
def test_source_guard_withholds_any_load_like_mention_of_build_output(tmp_path, source):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text(source, encoding="utf-8")
    guard = cr._opt13_source_output_guard(tmp_path)
    assert guard["safe"] is False


def test_source_guard_stays_safe_for_source_that_never_names_build_output(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        "import { join } from 'node:path';\n"
        "import { greet } from './greet';\n"
        "export const here = join(process.cwd(), 'src');\n",
        encoding="utf-8")
    assert cr._opt13_source_output_guard(tmp_path)["safe"] is True


def test_workflow_default_working_directory_binds_quality_and_validation():
    docs = {"ci.yml": {
        "defaults": {"run": {"working-directory": "packages/app"}},
        "jobs": {
            "test": {"steps": [{"run": "pnpm test"}]},
            "build": {"steps": [{"run": "pnpm build",
                                 "working-directory": "packages/app"}]},
        },
    }}
    assert cr._opt13_validation_build_job(docs, "ci.yml", "test") == {
        "workflow_file": "ci.yml", "job": "build"}


def test_workflow_default_does_not_override_a_job_default():
    docs = {"ci.yml": {
        "defaults": {"run": {"working-directory": "packages/app"}},
        "jobs": {
            "test": {"defaults": {"run": {"working-directory": "packages/web"}},
                     "steps": [{"run": "pnpm test"}]},
            "build": {"steps": [{"run": "pnpm build"}]},
        },
    }}
    assert cr._opt13_validation_build_job(docs, "ci.yml", "test") is None


@pytest.mark.parametrize("source", [
    "export default { build: { outDir: 'dist' } };\n",
    "export default [{ ignores: ['dist/', 'build/'] }];\n",
])
def test_source_guard_classifies_plain_build_config_values_as_inert(tmp_path, source):
    (tmp_path / "vite.config.ts").write_text(source, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
    assert cr._opt13_source_output_guard(tmp_path)["safe"] is True
