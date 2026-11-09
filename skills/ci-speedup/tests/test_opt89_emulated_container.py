"""Static detection for compilation inside foreign-architecture containers."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"


def _scan(root: Path, workflow: str) -> list[dict]:
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text(workflow, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["findings"]


def _opt89(root: Path, workflow: str) -> list[dict]:
    return [row for row in _scan(root, workflow) if row["pattern"] == "OPT89"]


def test_run_on_arch_compile_fires(tmp_path: Path):
    hits = _opt89(tmp_path, """name: emulated build
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          distro: ubuntu22.04
          run: |
            ./configure
            make -j2
""")

    assert len(hits) == 1
    assert hits[0]["affected_jobs"] == ["arm"]
    assert "aarch64" in hits[0]["evidence"]


@pytest.mark.parametrize("workspace", ["$GITHUB_WORKSPACE", "${{ github.workspace }}"])
def test_run_on_arch_compile_keeps_shell_variables_opaque(
        tmp_path: Path, workspace: str):
    hits = _opt89(tmp_path, """name: variable action
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: cd WORKSPACE && make -j2
""".replace("WORKSPACE", workspace))

    assert len(hits) == 1
    assert "make" in hits[0]["evidence"]


@pytest.mark.parametrize("workspace", ["$PWD", "${{ github.workspace }}"])
def test_docker_compile_keeps_volume_variables_opaque(
        tmp_path: Path, workspace: str):
    hits = _opt89(tmp_path, """name: variable docker
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform linux/arm64 -v WORKSPACE:/src build-image make -j2
""".replace("WORKSPACE", workspace))

    assert len(hits) == 1
    assert "make" in hits[0]["evidence"]


def test_variable_using_docker_test_command_stays_silent(tmp_path: Path):
    hits = _opt89(tmp_path, """name: variable docker tests
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform linux/arm64 -v $PWD:/src test-image pytest -q $TESTS
""")

    assert hits == []


def test_raw_buildx_with_variable_image_remains_opt102(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.24 AS build\nRUN go build ./...\n", encoding="utf-8")
    findings = _scan(tmp_path, """name: variable buildx
on: pull_request
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - run: docker buildx build --platform linux/arm64 -t $IMAGE .
""")

    assert "OPT102" in {row["pattern"] for row in findings}


def test_run_on_arch_compile_is_silent_on_native_arm_runner(tmp_path: Path):
    hits = _opt89(tmp_path, """name: native build
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-24.04-arm
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: ./configure && make -j2
""")

    assert hits == []


@pytest.mark.parametrize("command", [
    "pytest -q tests",
    "make test",
    "make -j2 check",
    "echo ready && make test",
    "ninja test",
    "cmake --version",
    "gcc --version",
])
def test_run_on_arch_test_only_is_silent(tmp_path: Path, command: str):
    hits = _opt89(tmp_path, """name: emulated tests
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: COMMAND
""".replace("COMMAND", command))

    assert hits == []


def test_qemu_backed_foreign_docker_run_compile_fires(tmp_path: Path):
    hits = _opt89(tmp_path, """name: emulated docker build
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform linux/arm64 build-image make -j2
""")

    assert len(hits) == 1
    assert "QEMU-backed docker run" in hits[0]["evidence"]


def test_multiline_shell_wrapped_foreign_docker_compile_fires(tmp_path: Path):
    hits = _opt89(tmp_path, """name: emulated docker build
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
        with:
          platforms: arm64
      - run: |
          docker run \\
            --platform linux/arm64 \\
            build-image sh -lc 'make -j2'
""")

    assert len(hits) == 1
    assert "make" in hits[0]["evidence"]


def test_foreign_job_container_compile_fires(tmp_path: Path):
    hits = _opt89(tmp_path, """name: emulated job container
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    container:
      image: arm64v8/ubuntu:24.04
    steps:
      - run: ./configure && make -j2
""")

    assert len(hits) == 1
    assert "foreign-architecture job container" in hits[0]["evidence"]


def test_foreign_docker_run_without_qemu_setup_is_silent(tmp_path: Path):
    hits = _opt89(tmp_path, """name: unresolved docker platform
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - run: docker run --platform linux/arm64 build-image make -j2
""")

    assert hits == []


def test_foreign_docker_run_without_matching_qemu_platform_is_silent(
        tmp_path: Path):
    hits = _opt89(tmp_path, """name: unsupported emulation
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
        with:
          platforms: amd64
      - run: docker run --platform linux/arm64 build-image make -j2
""")

    assert hits == []


def test_host_command_after_docker_run_is_not_attributed_to_container(
        tmp_path: Path):
    hits = _opt89(tmp_path, """name: host build after emulated tests
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform linux/arm64 test-image pytest -q && make -j2
""")

    assert hits == []


def test_run_on_arch_resolves_matrix_architecture(tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix action
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        arch: [aarch64]
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: ${{ matrix.arch }}
          run: make -j2
""")

    assert len(hits) == 1
    assert "aarch64" in hits[0]["evidence"]


def test_run_on_arch_resolves_matrix_test_target_before_classifying(
        tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix action target
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        target: [test]
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: make ${{ matrix.target }}
""")

    assert hits == []


@pytest.mark.parametrize("scope", ["job", "step"])
def test_run_on_arch_resolves_env_test_target_before_classifying(
        tmp_path: Path, scope: str):
    job_env = "    env:\n      MAKE_TARGET: check\n" if scope == "job" else ""
    step_env = "        env:\n          MAKE_TARGET: check\n" if scope == "step" else ""
    hits = _opt89(tmp_path, f"""name: environment action target
on: pull_request
jobs:
  arm:
{job_env}    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
{step_env}        with:
          arch: aarch64
          run: make $MAKE_TARGET
""")

    assert hits == []


def test_run_on_arch_resolves_env_compile_target(tmp_path: Path):
    hits = _opt89(tmp_path, """name: environment action target
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    env:
      MAKE_TARGET: all
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: make ${MAKE_TARGET}
""")

    assert len(hits) == 1


def test_run_on_arch_unresolved_target_stays_silent(tmp_path: Path):
    hits = _opt89(tmp_path, """name: unknown action target
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: make $MAKE_TARGET
""")

    assert hits == []


def test_job_container_resolves_matrix_compile_command(tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix container command
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        command: [make -j2]
    runs-on: ubuntu-latest
    container: arm64v8/ubuntu:24.04
    steps:
      - run: ${{ matrix.command }}
""")

    assert len(hits) == 1
    assert "make" in hits[0]["evidence"]


def test_docker_run_resolves_matrix_platform(tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix docker
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        platform: [linux/arm64]
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform ${{ matrix.platform }} build-image make -j2
""")

    assert len(hits) == 1
    assert "linux/arm64" in hits[0]["evidence"]


def test_docker_run_resolves_matrix_platform_beside_opaque_volume(
        tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix docker with variable volume
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        platform: [linux/arm64]
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform ${{ matrix.platform }} -v $PWD:/src build-image make -j2
""")

    assert len(hits) == 1
    assert "linux/arm64" in hits[0]["evidence"]


def test_docker_run_keeps_formatted_mount_expression_opaque(tmp_path: Path):
    hits = _opt89(tmp_path, """name: formatted docker volume
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-qemu-action@v3
      - run: docker run --platform linux/arm64 -v ${{ format('{0}', github.workspace) }}:/src build-image make -j2
""")

    assert len(hits) == 1
    assert "make" in hits[0]["evidence"]


def test_job_container_resolves_matrix_image(tmp_path: Path):
    hits = _opt89(tmp_path, """name: matrix container
on: pull_request
jobs:
  arm:
    strategy:
      matrix:
        image: [arm64v8/ubuntu:24.04]
    runs-on: ubuntu-latest
    container:
      image: ${{ matrix.image }}
    steps:
      - run: make -j2
""")

    assert len(hits) == 1
    assert "arm64v8" in hits[0]["evidence"]


def test_repeated_action_finding_cites_the_compile_step(tmp_path: Path):
    hits = _opt89(tmp_path, """name: repeated actions
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: pytest -q
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: make -j2
""")

    assert len(hits) == 1
    assert "make -j2" in hits[0]["evidence_snippet"]
    assert "pytest" not in hits[0]["evidence_snippet"]
