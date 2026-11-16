---
pattern: OPT104
impact: MEDIUM
class: structural
detector: critical-path
risk: LOW
affected_files: 'Cargo.toml,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: rust-coverage-profile-override
title_template: 'Rust coverage loads unoptimized debug artifacts'
category: 12
---

# OPT104 — Rust Coverage Loads Unoptimized Debug Artifacts

**TL;DR**: A measured long-pole step runs Rust coverage while Cargo's effective
test profile keeps the default full debug information and no optimization.
Coverage tests can repeatedly load a larger, slower library or plugin artifact.
Build only that loaded artifact without debug information and with optimization,
then keep the change only when test time improves and covered-line count stays
unchanged.

**Detection heuristic**: route from a measured critical-path job, never a flat
repository grep. The job's dominant measured step must be the workflow step that
directly runs `cargo llvm-cov` or `cargo tarpaulin`. The repository `Cargo.toml`
must leave the effective test profile at its debug/unoptimized defaults. Stay
quiet when `[profile.test]` (including values inherited from `[profile.dev]`)
disables debug information or enables optimization, and fail closed when the
manifest cannot be read.

**Fix recipe**: identify the library or plugin artifact loaded by the coverage
tests and use a package-scoped Cargo profile override (or equivalent harness
flags) to set `debug = 0` / `debuginfo = 0` and an appropriate `opt-level` for
that artifact. Do not blindly switch the whole command to `--release`:
`cargo llvm-cov` normally reads its coverage-instrumented target directory.
Make the cache key include the profile settings.

**Mandatory guardrail**: on the same commit and runner class, record both the
coverage test's wall time and its covered-line count before and after. Keep the
change only when the covered-line count is unchanged. A broad profile change can
affect unrelated artifacts, so prefer a package-scoped override.

**Sizing: deliberately uncredited**. The dominant step's measured p50 is a
ceiling, not a forecast. No wall-clock or runner-minute saving is credited until
the before/after benchmark exists.

**Conservative rollout**: land one profile override at a time, confirm the cache
key changes, and sample subsequent coverage runs for time and coverage drift.

