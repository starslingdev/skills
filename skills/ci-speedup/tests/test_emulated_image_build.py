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


def _write_build(root: Path, dockerfile: str, *, context: str = ".",
                 file: str | None = None, target: str | None = None,
                 platforms: str = "linux/amd64,linux/arm64",
                 runs_on: str = "ubuntu-latest") -> set[str]:
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    inputs = f"          context: {context}\n          platforms: {platforms}\n"
    if file:
        inputs += f"          file: {file}\n"
    if target:
        inputs += f"          target: {target}\n"
    (workflow_dir / "build.yml").write_text(
        """name: image
on: push
jobs:
  image:
    runs-on: RUNS_ON
    steps:
      - uses: docker/build-push-action@v6
        with:
INPUTS""".replace("INPUTS", inputs.rstrip()).replace("RUNS_ON", runs_on),
        encoding="utf-8",
    )
    docker_path = root / (file or "Dockerfile")
    docker_path.parent.mkdir(parents=True, exist_ok=True)
    docker_path.write_text(dockerfile, encoding="utf-8")
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


def test_static_hit_carries_its_release_lane(tmp_path: Path):
    _write_build(
        tmp_path,
        """FROM golang:1.23 AS build
RUN go build -o /out/server ./cmd/server
""",
    )
    workflow = tmp_path / ".github" / "workflows" / "build.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace("on: push", "on: release"),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, check=True)
    finding = next(
        row for row in json.loads(result.stdout)["findings"]
        if row["pattern"] == "OPT102")
    assert finding["workflow_lanes"] == ["release"]


def test_static_hit_distinguishes_unscoped_and_tag_only_pushes(tmp_path: Path):
    _write_build(
        tmp_path,
        """FROM golang:1.23 AS build
RUN go build -o /out/server ./cmd/server
""",
    )
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, check=True)
    finding = next(
        row for row in json.loads(result.stdout)["findings"]
        if row["pattern"] == "OPT102")
    assert finding["workflow_push_scopes"] == ["unscoped"]

    workflow = tmp_path / ".github" / "workflows" / "build.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "on: push", "on:\n  push:\n    tags: ['v*']"),
        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, check=True)
    finding = next(
        row for row in json.loads(result.stdout)["findings"]
        if row["pattern"] == "OPT102")
    assert finding["workflow_push_scopes"] == ["tag_only"]


def test_reusable_static_hit_inherits_its_callers_lane(tmp_path: Path):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "release.yml").write_text(
        """on: release
jobs:
  image:
    uses: ./.github/workflows/image.yml
""",
        encoding="utf-8")
    (workflow_dir / "image.yml").write_text(
        """on: workflow_call
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
""",
        encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.23 AS build\nRUN go build -o /out/server ./cmd/server\n",
        encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, check=True)
    finding = next(
        row for row in json.loads(result.stdout)["findings"]
        if row["pattern"] == "OPT102")
    assert finding["workflow_file"].endswith("image.yml")
    assert finding["workflow_lanes"] == ["release"]


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


def test_opt102_resolves_explicit_file_from_workspace(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    patterns = _write_build(
        tmp_path,
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n",
        context="./app",
        file="./app/Dockerfile",
    )
    assert "OPT102" in patterns


def test_opt102_accepts_braced_buildplatform(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM --platform=${BUILDPLATFORM} node:22 AS build
RUN npm run build
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
""",
    )
    assert "OPT102" not in patterns


def test_opt102_ignores_unreachable_compile_stage(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM golang:1.23 AS unused
RUN go build -o /server ./cmd/server
FROM alpine:3.20 AS release
COPY dist/server /server
""",
        target="release",
    )
    assert "OPT102" not in patterns


def test_opt102_ignores_compile_words_in_comments(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM alpine:3.20
# go build runs outside Docker
COPY dist/server /server
""",
    )
    assert "OPT102" not in patterns


def test_opt102_does_not_accept_cross_signal_only_in_comment(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM golang:1.23
# This stage should use $BUILDPLATFORM later.
RUN go build -o /server ./cmd/server
""",
    )
    assert "OPT102" in patterns


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


def test_opt102_target_os_flags_alone_do_not_avoid_emulation(tmp_path: Path):
    # Without FROM --platform=$BUILDPLATFORM the stage runs on the target platform,
    # so GOOS / GOARCH / --target flags still compile under emulation.
    patterns = _write_build(
        tmp_path,
        """FROM golang:1.23 AS build
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/server ./cmd/server
FROM scratch
COPY --from=build /out/server /server
""",
    )
    assert "OPT102" in patterns


def test_opt102_accepts_buildplatform_inherited_through_stage_alias(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        """FROM --platform=$BUILDPLATFORM golang:1.23 AS toolchain
FROM toolchain AS build
ARG TARGETARCH
RUN GOARCH=$TARGETARCH go build -o /out/server ./cmd/server
FROM scratch
COPY --from=build /out/server /server
""",
    )
    assert "OPT102" not in patterns


def test_opt102_detects_foreign_only_platform_on_x64_runner(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n",
        platforms="linux/arm64",
    )
    assert "OPT102" in patterns


def test_opt102_stays_silent_on_arm_runner_building_arm_only(tmp_path: Path):
    patterns = _write_build(
        tmp_path,
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n",
        platforms="linux/arm64",
        runs_on="ubuntu-24.04-arm",
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


def test_cache_hit_does_not_hide_emulated_compile():
    log = """#10 [linux/amd64 4/8] RUN go build -o /out/server ./cmd/server
#10 DONE 10.0s
#15 [linux/arm64 4/8] RUN go build -o /out/server ./cmd/server
#15 DONE 90.0s
Cache restored from key: deps-linux
"""
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "emulated-image-compile"


def test_buildx_log_correlates_reused_operation_ids_in_order():
    log = """#1 [linux/amd64 3/3] RUN go build -o /out/server ./cmd/server
#1 DONE 10.0s
#1 [linux/arm64 3/3] RUN go build -o /out/server ./cmd/server
#1 DONE 90.0s
"""
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["magnitude"]["value"] == 90.0
