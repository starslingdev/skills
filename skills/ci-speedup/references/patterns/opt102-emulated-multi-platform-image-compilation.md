---
pattern: OPT102
impact: HIGH
class: static
detector: bespoke
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,Dockerfile*'
fix_strategy: emulated-multi-platform-image-compilation
title_template: 'Foreign-Architecture Image Compilation Runs Under Emulation'
category: 3
---

# OPT102 — Foreign-Architecture Image Compilation Runs Under Emulation

**Anti-pattern**: A Docker Buildx or `docker build` step on one inspected builder
node builds a Linux architecture foreign to that node's native platform from a
repository-local Dockerfile whose compile
or source-install stage does not run on the build platform
(`FROM --platform=$BUILDPLATFORM`). Buildx then runs that work through emulation,
which is often far slower than native compilation. Target flags such as `GOARCH`,
`GOOS` or `--target` inside a target-platform stage do not avoid this: the compiler
itself still runs emulated.

**Detection heuristic**:

- require a foreign Linux platform on a job whose `runs-on` resolves to a
  different native platform; resolve literal environment,
  matrix, reusable-workflow input, and earlier-step output values, while leaving
  unknown values silent
- read only a repository-local build context and Dockerfile
- require a compile command in a Dockerfile stage the built target reaches; recognize
  common Go, Rust, CMake, Ninja, Meson, Node native-addon, Python wheel, Maven, and
  related build commands, including shell heredocs
- count `npm ci` or `npm install` only when the stage explicitly requests source
  builds with a skip-prebuilt or `npm_config_build_from_source` flag
- stay quiet when that stage's `FROM` uses `--platform=$BUILDPLATFORM`, directly or
  through the stage it is based on
- confirm the log diagnosis only when `docker buildx inspect --bootstrap` shows
  exactly one builder node; skip user-configured platforms marked with `*` and use
  the first detected platform as that node's native platform, failing closed when
  the inspect block is missing, lists multiple nodes, or contains no detected platform
- confirm the opportunity from buildx per-platform `RUN` and `DONE` lines; report
  source installs separately and size the opportunity only when foreign-architecture
  compile and source-install time is at least half of all measured cold image-build
  layer time and, when native compilation was measured, each measured foreign
  architecture takes at least twice that native compile time; include shared-stage
  layers without a platform prefix in the cold-layer total, and let a foreign-only
  build qualify against the cold-layer share alone

**Fix**: Build platform-independent output once in a stage based on
`FROM --platform=$BUILDPLATFORM`, passing the target architecture to a supported
cross-compiler, then copy the artifact into each target image. Native
cross-compilation still costs about one native compile per extra architecture, so
size the saving as the emulated seconds minus the native seconds for each foreign
architecture, and retain cache import/export, image export, and push time. cgo with
`CGO_ENABLED=1`, Rust `-sys` crates, and native Node add-ons can require
`tonistiigi/xx` or `gcc-<arch>-linux-gnu`. If the toolchain cannot cross-compile
safely, build on native runners per architecture and merge the manifests; check
each runner's cores and memory against link-time optimisation and parallel links,
and build only the binaries the image ships. Preserve every platform, verify the
produced binary architecture, test each image natively, and compare total runner
minutes as well as wall time.
