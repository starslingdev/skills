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


def _scan(root: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["findings"]


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


def test_opt102_raw_buildx_resolves_env_platform(tmp_path: Path):
    wf = tmp_path / ".github/workflows/build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("""on: push
jobs:
  image:
    runs-on: ubuntu-24.04
    env:
      TARGET_PLATFORMS: linux/arm64
    steps:
      - run: docker buildx build --platform $TARGET_PLATFORMS .
""", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n", encoding="utf-8")
    assert any(f["pattern"] == "OPT102" for f in _scan(tmp_path))


def test_opt102_raw_build_resolves_earlier_step_output(tmp_path: Path):
    wf = tmp_path / ".github/workflows/build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("""on: push
jobs:
  image:
    runs-on: ubuntu-24.04
    steps:
      - id: choose
        run: echo "platform=linux/arm64" >> "$GITHUB_OUTPUT"
      - run: docker build --platform ${{ steps.choose.outputs.platform }} .
""", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n", encoding="utf-8")
    assert any(f["pattern"] == "OPT102" for f in _scan(tmp_path))


def test_opt102_matrix_resolves_platform_and_runner_per_leg(tmp_path: Path):
    wf = tmp_path / ".github/workflows/build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("""on: push
jobs:
  image:
    strategy:
      matrix:
        include:
          - platform: linux/arm/v7
            runner: ubuntu-24.04
          - platform: linux/arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    steps:
      - run: docker buildx build --platform ${{ matrix.platform }} .
""", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n", encoding="utf-8")
    hits = [f for f in _scan(tmp_path) if f["pattern"] == "OPT102"]
    assert len(hits) == 1
    assert "linux/arm/v7" in hits[0]["evidence"]


def test_opt102_reusable_caller_resolves_dockerfile_input(tmp_path: Path):
    wd = tmp_path / ".github/workflows"
    wd.mkdir(parents=True)
    (wd / "caller.yml").write_text("""on: push
jobs:
  image:
    uses: ./.github/workflows/image.yml
    with:
      dockerfile: docker/release.Dockerfile
""", encoding="utf-8")
    (wd / "image.yml").write_text("""on:
  workflow_call:
    inputs:
      dockerfile:
        type: string
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          file: ${{ inputs.dockerfile }}
          platforms: linux/arm64
""", encoding="utf-8")
    dockerfile = tmp_path / "docker/release.Dockerfile"
    dockerfile.parent.mkdir()
    dockerfile.write_text("FROM golang:1.23\nRUN go build ./...\n", encoding="utf-8")
    assert any(f["pattern"] == "OPT102" for f in _scan(tmp_path))


def test_opt102_heredoc_ninja_and_native_npm_install(tmp_path: Path):
    patterns = _write_build(tmp_path, """FROM node:22
ENV npm_config_build_from_source=true
RUN <<'BUILD'
npm ci
cmake -GNinja -S . -B build
ninja -C build
BUILD
""", platforms="linux/arm64")
    assert "OPT102" in patterns


def test_opt102_unresolved_output_and_plain_install_stay_silent(tmp_path: Path):
    wf = tmp_path / ".github/workflows/build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("""on: push
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - id: choose
        run: echo dynamic
      - uses: docker/build-push-action@v6
        with:
          file: ${{ steps.choose.outputs.file }}
          platforms: linux/arm64
""", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM node:22\nRUN apk add git\nRUN npm ci\n", encoding="utf-8")
    assert not any(f["pattern"] == "OPT102" for f in _scan(tmp_path))


def test_opt102_native_platform_and_install_only_dockerfile_stay_silent(tmp_path: Path):
    patterns = _write_build(tmp_path, """FROM node:22
RUN apk add git
RUN npm ci
""", platforms="linux/amd64")
    assert "OPT102" not in patterns


def test_opt102_uses_hosted_x64_fallback_from_runs_on_expression(tmp_path: Path):
    wf = tmp_path / ".github/workflows/build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("""on: push
jobs:
  image:
    strategy:
      matrix:
        arm: [false]
    runs-on: ${{ matrix.arm && 'self-hosted-arm' || 'ubuntu-24.04' }}
    steps:
      - run: docker buildx build --platform linux/arm64 .
""", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang:1.23\nRUN go build -o /server ./cmd/server\n", encoding="utf-8")
    hit = next(f for f in _scan(tmp_path) if f["pattern"] == "OPT102")
    assert "hosted x64 fallback" in hit["evidence"]


def test_buildx_log_sums_chef_and_foreign_only_compile():
    log = """#10 [linux/arm64 2/4] RUN cargo chef cook --release
#10 DONE 120.0s
#11 [linux/arm64 3/4] RUN cargo build --release
#11 DONE 180.0s
"""
    leaf = bp._parse_log(log)
    assert leaf is not None and leaf["fix_key"] == "emulated-image-compile"
    assert leaf["magnitude"]["value"] == 300.0


def test_buildx_log_names_source_install_as_emulated_install():
    log = """#12 [linux/arm64 2/2] RUN npm_config_build_from_source=true npm ci
#12 DONE 75.0s
"""
    leaf = bp._parse_log(log)
    rows = leaf["deeper"][0]["rows"] if leaf is not None else []
    assert leaf is not None and leaf["magnitude"]["value"] == 75.0
    assert any(row[0] == "foreign-architecture emulated install" for row in rows)
