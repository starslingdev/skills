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

**Anti-pattern**: A Docker Buildx or `docker build` step on an x64 hosted runner
builds a foreign Linux architecture from a repository-local Dockerfile whose compile
or source-install stage does not run on the build platform
(`FROM --platform=$BUILDPLATFORM`). Buildx then runs that work through emulation,
which is often far slower than native compilation. Target flags such as `GOARCH`,
`GOOS` or `--target` inside a target-platform stage do not avoid this: the compiler
itself still runs emulated.

**Detection heuristic**:

- require a foreign Linux platform (anything other than `linux/amd64`) on a job
  whose `runs-on` resolves to a hosted x64 runner; resolve literal environment,
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
- confirm the opportunity from buildx per-platform `RUN` and `DONE` lines; report
  source installs separately and size the opportunity only when foreign-architecture
  compile and source-install time is at least half of the measured total, or when a
  foreign-only build supplies all measured work

**Fix**: Build platform-independent output once in a stage based on
`FROM --platform=$BUILDPLATFORM`, passing the target architecture to a supported
cross-compiler, then copy the artifact into each target image. If the toolchain
cannot cross-compile safely, build on native runners per architecture and merge the
manifests. The first approach removes most emulated compile work and should reduce
both wall time and runner minutes; native per-architecture jobs reduce wall time but
usually keep runner minutes level or increase them because they add jobs and a
manifest merge. Preserve every platform, verify the produced binary architecture,
test each image natively, and compare total runner minutes as well as wall time.
