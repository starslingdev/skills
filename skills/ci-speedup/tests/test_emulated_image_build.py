"""Detection for multi-platform image builds that compile under emulation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import blocking_path as bp  # noqa: E402


def _write_build(root: Path, dockerfile: str, *, context: str = ".") -> set[str]:
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "build.yml").write_text(
        """name: image
on: push
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          context: CONTEXT
          platforms: linux/amd64,linux/arm64
""".replace("CONTEXT", context),
        encoding="utf-8",
    )
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {finding["pattern"] for finding in json.loads(result.stdout)["findings"]}


def test_opt102_detects_go_compile_for_native_and_arm64(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM golang:1.23 AS build
WORKDIR /src
COPY . .
RUN go build -o /out/server ./cmd/server
FROM scratch
COPY --from=build /out/server /server
""",
    )
    assert "OPT102" in patterns


def test_opt102_accepts_explicit_cross_compile(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM --platform=$BUILDPLATFORM golang:1.23 AS build
ARG TARGETARCH
RUN GOARCH=$TARGETARCH go build -o /out/server ./cmd/server
FROM scratch
COPY --from=build /out/server /server
""",
    )
    assert "OPT102" not in patterns


def test_opt102_ignores_copy_only_image_stage(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM alpine:3.20
COPY dist/server /server
ENTRYPOINT [\"/server\"]
""",
    )
    assert "OPT102" not in patterns


def test_opt102_ignores_remote_build_context(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM golang:1.23 AS build
RUN go build -o /out/server ./cmd/server
""",
        context="https://example.invalid/source.git",
    )
    assert "OPT102" not in patterns


def test_buildx_log_measures_emulated_compile_share():
    # Buildx header/DONE layout copied from a committed public Actions build log.
    log = """2024-12-11T04:49:34.9027906Z #10 [linux/amd64 4/8] RUN go build -o /out/server ./cmd/server
2024-12-11T04:51:49.9027906Z #10 DONE 135.0s
2024-12-11T05:17:20.3662178Z #15 [linux/arm64 4/8] RUN go build -o /out/server ./cmd/server
2024-12-11T05:38:49.3662178Z #15 DONE 1289.0s
"""
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "emulated-image-compile"
    assert leaf["magnitude"] == {
        "label": "emulated compile seconds", "unit": "s", "value": 1289.0}
