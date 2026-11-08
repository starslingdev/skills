"""Static detection for compilation inside foreign-architecture containers."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


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


def test_run_on_arch_test_only_is_silent(tmp_path: Path):
    hits = _opt89(tmp_path, """name: emulated tests
on: pull_request
jobs:
  arm:
    runs-on: ubuntu-latest
    steps:
      - uses: uraimo/run-on-arch-action@v2
        with:
          arch: aarch64
          run: pytest -q tests
""")

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
