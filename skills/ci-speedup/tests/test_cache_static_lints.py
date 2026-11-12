"""Static cache shapes that cannot produce a useful warm run."""

from __future__ import annotations

from pathlib import Path

import collect_runs
import scan


_CATALOG = Path(__file__).resolve().parents[1] / "references" / "patterns"


def _scan(tmp_path: Path, steps: str, *, runs_on: str = "ubuntu-latest") -> list[dict]:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: pull_request\njobs:\n  build:\n"
        f"    runs-on: {runs_on}\n    steps:\n{steps}",
        encoding="utf-8",
    )
    return scan.scan(tmp_path, _CATALOG)["findings"]


def _patterns(rows: list[dict]) -> set[str]:
    return {str(row["pattern"]) for row in rows}


def _scan_workflow(tmp_path: Path, workflow: str) -> list[dict]:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(workflow, encoding="utf-8")
    return scan.scan(tmp_path, _CATALOG)["findings"]


def test_opt61_flags_restore_prefix_that_cannot_match_saved_key(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: .cache/pnpm
          key: pnpm-${{ runner.os }}-${{ github.sha }}
          restore-keys: other-${{ runner.os }}-
      - uses: actions/cache@v4
        with:
          path: .cache/ccache
          key: ccache-${{ runner.os }}
      - run: pnpm build
""")
    assert "OPT61" in _patterns(rows)
    restore_row = next(row for row in rows if row["pattern"] == "OPT61"
                       and "restore" in row["evidence"].lower())
    assert restore_row["cache_family"] == "pnpm"
    for index, prefix in enumerate(("v1", "cache-v2", "shared", "build", "deps", "linux")):
        prefixed = _scan(tmp_path / f"prefixed-{index}", f"""      - uses: actions/cache@v4
        with:
          path: .cache/tool
          key: {prefix}-${{{{ runner.os }}}}-tool-${{{{ github.sha }}}}
          restore-keys: other-${{{{ runner.os }}}}-
      - run: tool build
""")
        prefixed_row = next(row for row in prefixed if row["pattern"] == "OPT61"
                            and "restore" in row["evidence"].lower())
        status, _line = collect_runs._cache_status_from_log(
            f"Cache not found for input keys: {prefix}-Linux-pnpm-other",
            collect_runs._cache_keywords_for_finding(prefixed_row),
        )
        assert status != "miss"


def test_opt61_scopes_confirmation_to_the_saved_key_family(tmp_path) -> None:
    cases = (
        ("target", "cargo"),
        ("node_modules", "npm"),
        ("~/.m2/repository", "maven"),
    )
    for index, (path, family) in enumerate(cases):
        rows = _scan(tmp_path / f"key-family-{index}", f"""      - uses: actions/cache@v4
        with:
          path: {path}
          key: {family}-${{{{ runner.os }}}}-${{{{ github.sha }}}}
          restore-keys: other-${{{{ runner.os }}}}-
      - run: {family} build
""")
        row = next(item for item in rows if item["pattern"] == "OPT61"
                   and "restore" in item["evidence"].lower())
        status, _line = collect_runs._cache_status_from_log(
            f"Cache not found for input keys: {family}-Linux-deadbeef",
            collect_runs._cache_keywords_for_finding(row),
        )
        assert status == "miss"


def test_opt61_uses_the_specific_key_word_after_an_unlisted_prefix(tmp_path) -> None:
    for index, prefix in enumerate(("ubuntu", "ci")):
        rows = _scan(tmp_path / f"generic-prefix-{index}", f"""      - uses: actions/cache@v4
        with:
          path: ~/.cache
          key: {prefix}-${{{{ runner.os }}}}-tool-${{{{ github.sha }}}}
          restore-keys: other-${{{{ runner.os }}}}-
      - run: tool build
""")
        row = next(item for item in rows if item["pattern"] == "OPT61"
                   and "restore" in item["evidence"].lower())
        own_status, _line = collect_runs._cache_status_from_log(
            f"Cache not found for input keys: {prefix}-Linux-tool-deadbeef",
            collect_runs._cache_keywords_for_finding(row),
        )
        unrelated_status, _line = collect_runs._cache_status_from_log(
            f"Cache not found for input keys: {prefix}-Linux-pnpm-deadbeef",
            collect_runs._cache_keywords_for_finding(row),
        )
        assert own_status == "miss"
        assert unrelated_status == "unknown"


def test_opt61_accepts_a_matching_rolling_restore_prefix(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: .cache/tool
          key: tool-${{ runner.os }}-${{ github.sha }}
          restore-keys: tool-${{ runner.os }}-
      - run: tool build
""")
    assert "OPT61" not in _patterns(rows)


def test_opt61_accepts_equivalent_expression_spacing_in_restore_prefix(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: .cache/tool
          key: tool-${{runner.os}}-${{ github.sha }}
          restore-keys: tool-${{ runner.os }}-
      - run: tool build
""")
    assert "OPT61" not in _patterns(rows)


def test_opt61_accepts_restore_family_seeded_by_another_job(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: [push, pull_request]
jobs:
  seed:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-main-${{ github.sha }}
      - run: ccache make
  pr:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-pr-${{ github.sha }}
          restore-keys: ccache-main-
      - run: ccache make
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_accepts_restore_family_seeded_by_another_workflow(tmp_path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "seed.yml").write_text("""name: Seed
on:
  push:
    branches: [main]
jobs:
  seed:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-main-${{ github.sha }}
      - run: ccache make
""", encoding="utf-8")
    (workflows / "pr.yml").write_text("""name: PR
on: pull_request
jobs:
  pr:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-pr-${{ github.sha }}
          restore-keys: ccache-main-
      - run: ccache make
""", encoding="utf-8")

    rows = scan.scan(tmp_path, _CATALOG)["findings"]

    assert not any(
        row.get("pattern") == "OPT61"
        and row.get("workflow_file", "").endswith("pr.yml")
        for row in rows)


def test_opt61_flags_tiny_ccache_limits_but_accepts_500m(tmp_path) -> None:
    small = _scan(tmp_path, """      - uses: hendrikmuhs/ccache-action@v1
        with:
          max-size: 50M
      - run: cmake --build build
""")
    assert "OPT61" in _patterns(small)
    large = _scan(tmp_path / "large", """      - uses: hendrikmuhs/ccache-action@v1
        with:
          max-size: 500M
      - run: cmake --build build
""")
    assert "OPT61" not in _patterns(large)
    for index, command in enumerate(("CCACHE_MAXSIZE=64M ccache make",
                                     "ccache -M 32M && make")):
        assert "OPT61" in _patterns(
            _scan(tmp_path / f"command-{index}", f"      - run: {command}\n"))


def test_opt61_flags_unpersisted_compiler_and_analysis_caches(tmp_path) -> None:
    for index, command in enumerate((
            "CCACHE_DIR=$RUNNER_TEMP/ccache ccache cmake --build build",
            "STATICCHECK_CACHE=/tmp/staticcheck staticcheck ./...",
            "sccache --start-server && cargo build",
    )):
        rows = _scan(tmp_path / str(index), f"      - run: {command}\n")
        assert "OPT61" in _patterns(rows), command
    mixed = _scan(tmp_path / "mixed", """      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-${{ runner.os }}
      - run: STATICCHECK_CACHE=/tmp/staticcheck staticcheck ./...
""")
    temp_row = next(row for row in mixed if row["pattern"] == "OPT61"
                    and "temporary storage" in row["evidence"])
    assert temp_row["cache_family"] == "staticcheck"


def test_opt61_accepts_a_persisted_sccache_directory(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: ~/.cache/sccache
          key: sccache-${{ runner.os }}-${{ github.sha }}
          restore-keys: sccache-${{ runner.os }}-
      - run: SCCACHE_DIR=~/.cache/sccache sccache --start-server
      - run: cargo build
""")
    assert "OPT61" not in _patterns(rows)


def test_opt61_accepts_a_persisted_temporary_analysis_cache(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: /tmp/staticcheck
          key: staticcheck-${{ runner.os }}-${{ github.sha }}
          restore-keys: staticcheck-${{ runner.os }}-
      - run: STATICCHECK_CACHE=/tmp/staticcheck staticcheck ./...
""")
    assert "OPT61" not in _patterns(rows)


def test_opt61_accepts_compiler_caches_with_persistent_or_remote_backends(tmp_path) -> None:
    self_hosted = _scan(
        tmp_path / "self-hosted", "      - run: sccache --start-server && cargo build\n",
        runs_on="[self-hosted, linux]",
    )
    assert "OPT61" not in _patterns(self_hosted)

    remote = _scan(tmp_path / "remote", """      - run: sccache --start-server && cargo build
        env:
          SCCACHE_BUCKET: compiler-cache
""")
    assert "OPT61" not in _patterns(remote)

    inherited_remote = _scan_workflow(tmp_path / "inherited-remote", """name: CI
on: pull_request
env:
  CCACHE_REMOTE_STORAGE: redis://cache.internal
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - run: cmake --build build
""")
    assert "OPT61" not in _patterns(inherited_remote)


def test_opt61_reads_ccache_directory_and_launcher_from_job_env(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: ${{ runner.temp }}/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - run: cmake --build build
""")

    assert "OPT61" in _patterns(rows)


def test_opt61_matches_the_declared_ccache_directory_to_the_saved_path(tmp_path) -> None:
    mismatch = _scan_workflow(tmp_path / "mismatch", """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /tmp/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-${{ github.sha }}
      - run: cmake --build build
""")
    assert "OPT61" in _patterns(mismatch)

    matching = _scan_workflow(tmp_path / "matching", """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /tmp/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: /tmp/ccache
          key: ccache-${{ github.sha }}
      - run: cmake --build build
""")
    assert "OPT61" not in _patterns(matching)

    inline_mismatch = _scan(tmp_path / "inline-mismatch", """      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-${{ github.sha }}
      - run: CCACHE_DIR=/var/cache/compiler ccache make
""")
    assert "OPT61" in _patterns(inline_mismatch)

    expression_match = _scan_workflow(tmp_path / "expression-match", """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: ${{ runner.temp }}/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{runner.temp}}/ccache
          key: ccache-${{ github.sha }}
      - run: cmake --build build
""")
    assert "OPT61" not in _patterns(expression_match)


def test_opt61_resolves_job_env_in_a_saved_cache_path(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /tmp/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_keeps_an_unknown_cache_path_expression_silent(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: CCACHE_DIR=/tmp/ccache ccache cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_keeps_an_unknown_cache_path_suffix_silent(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - run: echo "CACHE_ROOT=/mnt/cache" >> "$GITHUB_ENV"
      - uses: actions/cache@v4
        with:
          path: ${{ env.CACHE_ROOT }}/ccache
          key: ccache-v1
      - run: cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_unknown_unrelated_path_does_not_hide_a_literal_mismatch(
        tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /tmp/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ccache
          key: ccache-v1
      - uses: actions/cache@v4
        with:
          path: ${{ env.NPM_CACHE_DIR }}
          key: npm-v1
      - run: cmake --build build
""")

    assert any(
        row.get("pattern") == "OPT61"
        and "compiler cache has no persistence step" in row.get("evidence", "")
        for row in rows)


def test_opt61_uses_job_env_instead_of_shadowed_workflow_env(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
env:
  CCACHE_DIR: /workflow/ccache
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_resolves_workflow_env_in_a_saved_cache_path(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
env:
  CCACHE_DIR: /workflow/ccache
  CMAKE_C_COMPILER_LAUNCHER: ccache
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_cache_step_env_overrides_job_env_for_the_saved_path(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        env:
          CCACHE_DIR: /cache-step/ccache
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: cmake --build build
""")

    assert "OPT61" in _patterns(rows)


def test_opt61_matches_cache_and_run_step_overrides(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        env:
          CCACHE_DIR: /step/ccache
        with:
          path: ${{ env.CCACHE_DIR }}
          key: ccache-v1
      - run: cmake --build build
        env:
          CCACHE_DIR: /step/ccache
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_applies_a_run_step_override_only_to_that_step(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: /step/ccache
          key: ccache-v1
      - run: cmake --build first
        env:
          CCACHE_DIR: /step/ccache
      - run: cmake --build second
""")

    assert "OPT61" in _patterns(rows)


def test_opt61_applies_an_inline_override_only_to_its_command(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: /inline/ccache
          key: ccache-v1
      - run: |
          CCACHE_DIR=/inline/ccache ccache make first
          ccache make second
""")

    assert "OPT61" in _patterns(rows)


def test_opt61_keeps_a_standalone_shell_assignment_for_later_commands(
        tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - uses: actions/cache@v4
        with:
          path: /shell/ccache
          key: ccache-v1
      - run: |
          CCACHE_DIR=/shell/ccache
          ccache make all
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_applies_github_env_writes_to_later_steps(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - run: echo "CCACHE_DIR=/later/ccache" >> "$GITHUB_ENV"
      - uses: actions/cache@v4
        with:
          path: /later/ccache
          key: ccache-v1
      - run: ccache make
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_ignores_inherited_directories_in_unrelated_run_steps(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /job/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
    steps:
      - run: echo setup
      - uses: actions/cache@v4
        with:
          path: /step/ccache
          key: ccache-v1
      - run: cmake --build build
        env:
          CCACHE_DIR: /step/ccache
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_splits_a_multiline_env_cache_path(tmp_path) -> None:
    rows = _scan_workflow(tmp_path, """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      CCACHE_DIR: /tmp/ccache
      CMAKE_C_COMPILER_LAUNCHER: ccache
      CACHE_PATHS: |-
        /tmp/ccache
        ~/.npm
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ env.CACHE_PATHS }}
          key: caches-v1
      - run: cmake --build build
""")

    assert "OPT61" not in _patterns(rows)


def test_opt61_marks_a_restored_then_unread_deleted_path_as_pure_cost(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: /tmp/staticcheck
          key: staticcheck-${{ runner.os }}-${{ github.sha }}
          restore-keys: staticcheck-${{ runner.os }}-
      - run: rm -rf /tmp/staticcheck
      - run: echo no analysis runs in this job
""")
    lint = next(row for row in rows
                if row.get("pattern") == "OPT61"
                and row.get("cache_path_consumed") is False)
    assert "deletes" in lint["evidence"]


def test_opt61_does_not_call_a_pre_restore_cleanup_a_later_deletion(tmp_path) -> None:
    rows = _scan(tmp_path, """      - run: rm -rf /tmp/staticcheck
      - uses: actions/cache@v4
        with:
          path: /tmp/staticcheck
          key: staticcheck-${{ runner.os }}-${{ github.sha }}
          restore-keys: staticcheck-${{ runner.os }}-
      - run: staticcheck ./...
""")
    assert not any(row.get("cache_path_consumed") is False for row in rows)
    assert not any("later step deletes" in row.get("evidence", "") for row in rows)


def test_opt62_flags_go_build_cache_clean_but_not_testcache(tmp_path) -> None:
    for index, command in enumerate(("go clean -cache", "go clean -i -cache",
                                     "go clean -modcache")):
        assert "OPT62" in _patterns(_scan(tmp_path / str(index),
                                                  f"      - run: {command}\n"))
    assert "OPT62" not in _patterns(
        _scan(tmp_path / "testcache", "      - run: go clean -testcache\n"))


def test_opt4_flags_run_scoped_gha_and_lossy_local_exports(tmp_path) -> None:
    gha = _scan(tmp_path / "gha", """      - run: >-
          docker buildx build .
          --cache-from type=gha,scope=${{ github.sha }}
          --cache-to type=gha,scope=${{ github.sha }}
""")
    assert "OPT4" in _patterns(gha)
    local = _scan(tmp_path / "local", """      - run: >-
          docker buildx build .
          --cache-from type=local,src=/tmp/build-cache
          --cache-to type=local,dest=/tmp/build-cache
""")
    assert "OPT4" in _patterns(local)


def test_opt4_flags_run_scoped_gha_on_build_push_action(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: docker/build-push-action@v6
        with:
          context: .
          cache-from: type=gha,scope=${{ github.sha }}
          cache-to: type=gha,scope=${{ github.sha }},mode=max
""")
    assert "OPT4" in _patterns(rows)


def test_opt4_flags_prefixed_commit_scope_and_action_local_overwrite(tmp_path) -> None:
    scoped = _scan(tmp_path / "scoped", """      - uses: docker/build-push-action@v6
        with:
          context: .
          cache-from: type=gha,scope=image-${{ github.sha }}
          cache-to: type=gha,scope=image-${{ github.sha }},mode=max
""")
    assert "OPT4" in _patterns(scoped)

    local = _scan(tmp_path / "local", """      - uses: docker/build-push-action@v6
        with:
          context: .
          cache-from: type=local,src=/tmp/build-cache
          cache-to: type=local,dest=/tmp/build-cache
""")
    assert "OPT4" in _patterns(local)


def test_opt4_accepts_stable_gha_scope_and_mode_max_local_rotation(tmp_path) -> None:
    gha = _scan(tmp_path / "gha", """      - run: >-
          docker buildx build .
          --cache-from type=gha,scope=app
          --cache-to type=gha,scope=app,mode=max
""")
    assert "OPT4" not in _patterns(gha)
    local = _scan(tmp_path / "local", """      - run: >-
          docker buildx build .
          --cache-from type=local,src=/tmp/build-cache
          --cache-to type=local,dest=/tmp/build-cache-new,mode=max
""")
    assert "OPT4" not in _patterns(local)


def test_opt4_does_not_pair_local_paths_from_separate_build_steps(tmp_path) -> None:
    rows = _scan(tmp_path, """      - uses: docker/build-push-action@v6
        with:
          context: service-a
          cache-to: type=local,dest=/tmp/build-cache
      - uses: docker/build-push-action@v6
        with:
          context: service-b
          cache-from: type=local,src=/tmp/build-cache
          cache-to: type=local,dest=/tmp/build-cache-next
""")

    assert "OPT4" not in _patterns(rows)


def test_opt4_flags_ephemeral_dockerfile_cache_mount_on_hosted_runner(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, "      - run: docker build .\n")
    assert "OPT4" in _patterns(rows)


def test_opt4_export_only_does_not_persist_a_buildkit_cache_mount(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - run: >-
          docker buildx build .
          --cache-from type=gha,scope=app
          --cache-to type=gha,scope=app,mode=max
""")
    assert "OPT4" in _patterns(rows)


def test_opt4_action_export_only_does_not_persist_a_cache_mount(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - uses: docker/build-push-action@v6
        with:
          context: .
          cache-from: type=gha,scope=app
          cache-to: type=gha,scope=app,mode=max
""")
    assert "OPT4" in _patterns(rows)


def test_opt4_accepts_buildkit_cache_dance_for_cache_mount(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: cache-mount
          key: cache-mount-${{ hashFiles('Dockerfile') }}
      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-dir: cache-mount
          dockerfile: Dockerfile
      - uses: docker/build-push-action@v6
        with:
          context: .
""")

    assert "OPT4" not in _patterns(rows)


def test_opt4_accepts_cache_dance_explicit_map_and_target_source(tmp_path) -> None:
    for name in ("explicit", "target-source"):
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "Dockerfile").write_text(
            "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
            encoding="utf-8",
        )
    explicit = _scan(tmp_path / "explicit", """      - uses: actions/cache@v4
        with:
          path: build-cache
          key: cache-mount-${{ hashFiles('Dockerfile') }}
      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"build-cache": {"target": "/root/.cache", "id": "compiler"}}'
      - run: docker build .
""")
    assert "OPT4" not in _patterns(explicit)

    target_source = _scan(tmp_path / "target-source", """      - uses: actions/cache@v4
        with:
          path: /root/.cache
          key: cache-mount-${{ hashFiles('Dockerfile') }}
      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          dockerfile: Dockerfile
      - run: docker build .
""")
    assert "OPT4" not in _patterns(target_source)


def test_opt4_cache_dance_requires_a_persisted_matching_source(tmp_path) -> None:
    for name in ("unrelated", "unpersisted"):
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "Dockerfile").write_text(
            "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
            encoding="utf-8",
        )
    unrelated = _scan(tmp_path / "unrelated", """      - uses: actions/cache@v4
        with:
          path: apt-cache
          key: apt-cache-${{ hashFiles('Dockerfile') }}
      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"apt-cache": "/var/cache/apt"}'
      - uses: docker/build-push-action@v6
        with:
          context: .
""")
    assert "OPT4" in _patterns(unrelated)

    unpersisted = _scan(tmp_path / "unpersisted", """      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"build-cache": "/root/.cache"}'
      - uses: docker/build-push-action@v6
        with:
          context: .
""")
    assert "OPT4" in _patterns(unpersisted)


def test_opt4_cache_dance_skip_extraction_does_not_seed_mount(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: build-cache
          key: build-cache-${{ hashFiles('Dockerfile') }}
      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"build-cache": "/root/.cache"}'
          skip-extraction: true
      - uses: docker/build-push-action@v6
        with:
          context: .
""")

    assert "OPT4" in _patterns(rows)


def test_opt4_accepts_cache_mount_on_persistent_remote_builder(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - uses: docker/setup-buildx-action@v3
        with:
          driver: remote
          endpoint: tcp://builder.internal:1234
      - uses: docker/build-push-action@v6
        with:
          context: .
""")
    assert "OPT4" not in _patterns(rows)


def test_opt4_unrelated_actions_cache_does_not_hide_ephemeral_docker_mount(tmp_path) -> None:
    (tmp_path / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nRUN --mount=type=cache,target=/root/.cache make all\n",
        encoding="utf-8",
    )
    rows = _scan(tmp_path, """      - uses: actions/cache@v4
        with:
          path: ~/.npm
          key: npm-${{ hashFiles('package-lock.json') }}
      - run: docker build .
""")
    assert "OPT4" in _patterns(rows)
