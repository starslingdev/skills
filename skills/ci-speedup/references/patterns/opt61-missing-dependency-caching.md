---
pattern: OPT61
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: missing-dependency-caching
title_template: 'Missing Dependency Caching'
category: 12
---

# OPT61 — Missing Dependency Caching

**Anti-pattern**: Build jobs compile or install dependencies from scratch on every run without any caching action. This wastes minutes per job and multiplies across matrix shards.

**Savings estimation**: Build cache savings depend on cache hit rate — the fraction of runs where the cached task's inputs haven't changed. Do NOT assume 100% hit rate. Measure the actual hit rate from sampled job-duration bimodality (runs < 50% of baseline P50 = cache hits) or by parsing cache-restore / cache-miss lines from sampled job logs (`collect_runs.py --with-logs`). Cache typically only helps runs that don't change the ecosystem's source files: frontend-only PRs, dependabot PRs, re-runs, and CI config changes.

**Detection heuristic**:

1. Identify the ecosystem from workflow steps:
   - Rust: `cargo build`, `cargo test`, `cargo clippy` → check for `Swatinem/rust-cache`, `sccache`, or `actions/cache` targeting `target/` or `~/.cargo`
   - Python: `pip install`, `uv sync`, `poetry install` → check for `actions/cache` targeting pip/uv cache or venv
   - Go: `go build`, `go test` → check for `actions/setup-go` with `cache: true` or `actions/cache` targeting `GOMODCACHE`
   - Java: `mvn`, `gradle` → check for `actions/cache` targeting `~/.m2` or `~/.gradle`
   - JS: `npm ci`, `pnpm install`, `yarn install` → check for `actions/setup-node` with `cache:` or `actions/cache`
   - C++: `cmake`, `make`, `ninja` → check for `actions/cache` targeting build dir, or `ccache`/`sccache`
2. Flag if the ecosystem's build/install commands are present but no corresponding cache action exists.
3. Count total jobs affected — in sharded/matrix workflows, the waste multiplies.

```bash
# Check for any caching across all workflows
grep -rn 'actions/cache\|rust-cache\|sccache\|setup-node.*cache\|setup-go.*cache\|setup-python.*cache' /tmp/workflows/
# If empty, check what build tools are used
grep -rn 'cargo \|pip install\|uv sync\|go build\|mvn \|gradle\|cmake\|make ' /tmp/workflows/
```

**Fix**: Add the ecosystem-appropriate caching action. For multi-job workflows, use `shared-key` or equivalent to avoid N separate caches.

---
