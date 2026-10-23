---
pattern: OPT59
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: runtime-only-env-vars-in-turbo-globalenv
title_template: 'Runtime-Only Env Vars in Turbo globalEnv'
category: 11
---

# OPT59 — Runtime-Only Env Vars in Turbo globalEnv

**Anti-pattern**: Environment variables listed in Turbo's `globalEnv` or task-level `env` that are only used at runtime (read from `process.env` at execution time), not at compile time (inlined by a bundler like webpack DefinePlugin, Vite's `import.meta.env`, or Next.js automatic `NEXT_PUBLIC_*` inlining). When these vars are in `globalEnv`, changing them (e.g., rotating an API key) invalidates the cache for every task in the repo.

**Distinct from OPT53**: OPT53 covers **unstable** env vars whose values change between runs (e.g., `GITHUB_RUN_ID`, `BUILD_NUMBER`). OPT59 covers **stable but build-irrelevant** env vars — API keys and secrets that are constant across runs but don't affect compiled output. The detection heuristic is different: OPT53 matches known-unstable variable names; OPT59 requires checking whether the variable is consumed at compile time.

**Detection heuristic**:

```bash
# Extract all env vars from globalEnv AND task-level env arrays
cat /tmp/turbo.json | jq -r '
  (.globalEnv // [])[] ,
  ((.tasks // .pipeline // {}) | to_entries[] | (.value.env // [])[] )
' 2>/dev/null | sort -u | while read var; do
  # Skip known compile-time vars
  echo "$var" | grep -qE '^(NEXT_PUBLIC_|VITE_|REACT_APP_)' && continue
  # Flag API keys and secrets as likely runtime-only
  echo "$var" | grep -qiE '(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)' && \
    echo "OPT59 candidate: $var — likely runtime-only (API key pattern)"
done
```

To confirm: search the source code for compile-time usage of the variable (webpack `DefinePlugin`, Vite `define`, Next.js automatic inlining via `NEXT_PUBLIC_*` prefix). If the variable is NOT used at compile time, it belongs in `globalPassThroughEnv` (turbo v1.10+), not `globalEnv`.

**Fix**: Remove runtime-only env vars from `globalEnv`/`env`. If the var must be available to tasks at runtime but should not affect caching, use `globalPassThroughEnv` instead. Verify the var is not inlined by a bundler before removing.

**Real-world example (mastra)**: PR #14432 removed `RAPID_API_KEY` and `ANTHROPIC_API_KEY` from turbo's `globalEnv`. Both were runtime string literals — not compile-time dependencies. Secret rotation was busting the cache for 60+ packages.

---
