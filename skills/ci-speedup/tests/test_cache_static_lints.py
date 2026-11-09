"""Static cache shapes that cannot produce a useful warm run."""

from __future__ import annotations

from pathlib import Path

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
          path: .cache/tool
          key: tool-${{ runner.os }}-${{ github.sha }}
          restore-keys: other-${{ runner.os }}-
      - run: tool build
""")
    assert "OPT61" in _patterns(rows)
    assert any("restore" in row["evidence"].lower() for row in rows
               if row["pattern"] == "OPT61")


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
    rows = _scan(tmp_path, """      - uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"/root/.cache": "build-cache"}'
      - uses: docker/build-push-action@v6
        with:
          context: .
          cache-from: type=gha,scope=app
          cache-to: type=gha,scope=app,mode=max
""")

    assert "OPT4" not in _patterns(rows)


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
