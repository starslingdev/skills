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

**Anti-pattern**: A `docker/build-push-action` step on an x64 hosted runner builds
a foreign Linux architecture from a repository-local Dockerfile whose compile stage
does not run on the build platform (`FROM --platform=$BUILDPLATFORM`). Buildx then
runs the foreign compile through emulation, which is often far slower than native
compilation. Target flags such as `GOARCH`, `GOOS` or `--target` inside a
target-platform stage do not avoid this: the compiler itself still runs emulated.

**Detection heuristic**:

- require a foreign Linux platform (anything other than `linux/amd64`) on a job
  whose `runs-on` is not an arm runner or an unresolved expression
- read only a repository-local build context and Dockerfile
- require a compile command in a Dockerfile stage the built target reaches
- stay quiet when that stage's `FROM` uses `--platform=$BUILDPLATFORM`, directly or
  through the stage it is based on
- confirm the opportunity from buildx per-platform `RUN` and `DONE` lines; size it
  only when foreign-architecture compile time is at least half of the measured total

**Fix**: Build platform-independent output once in a stage based on
`FROM --platform=$BUILDPLATFORM`, passing the target architecture to a supported
cross-compiler, then copy the artifact into each target image. If the toolchain
cannot cross-compile safely, build on native runners per architecture and merge the
manifests. The first approach removes most emulated compile work and should reduce
both wall time and runner minutes; native per-architecture jobs reduce wall time but
usually keep runner minutes level or increase them because they add jobs and a
manifest merge. Preserve every platform, verify the produced binary architecture,
test each image natively, and compare total runner minutes as well as wall time.
