---
pattern: OPT69
impact: LOW
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: dead-workflow-env-vars-config
title_template: 'Dead Workflow Env Vars / Config'
category: 13
---

# OPT69 — Dead Workflow Env Vars / Config

**Anti-pattern**: A workflow declares an environment variable, secret, or config option that is not consumed by any code in the repo. Common causes: a feature was removed but the env var wasn't cleaned up, a test was rewritten and the skip-flag was forgotten, a stale env var was inherited from a template repo.

**Detection heuristic**:

1. Enumerate every `env:` block in workflow YAML — both workflow-level and step-level.
2. For each env var name (e.g., `SKIP_DB_TESTS`, `MOCK_PAYMENTS`, `DISABLE_TELEMETRY`, `LEGACY_AUTH`), grep the repo for case-sensitive use:
   - Code: `os.environ.get('SKIP_DB_TESTS')`, `process.env.SKIP_DB_TESTS`, `std::env::var("SKIP_DB_TESTS")`
   - Config: `${SKIP_DB_TESTS}` in `.env*`, `docker-compose*.yml`, `Makefile`, shell scripts
   - Tests: `pytest.mark.skipif(os.getenv('SKIP_DB_TESTS'))`
3. If grep returns zero hits AND the var is set to a non-secret literal (so it's not a deploy-time config), flag as dead.
4. Skip secrets passed via `${{ secrets.X }}` — those may be consumed by external services and aren't grep-able locally.

**Fix**: Delete the env var. Add a follow-up audit suggestion if the deletion uncovers further dead config (e.g., the workflow step that sets the var is now itself dead).

**Saving math**: Per-deletion saving is small (~0ms), but the cumulative readability benefit + reduced confusion for future contributors makes it worth flagging at LOW severity. Aggregate across all dead vars in the report (e.g., "5 dead env vars across 3 workflows — delete in one PR").

**Real-world example (blen-starter-kit deep-scan)**: An env var like `SKIP_DB_TESTS: "true"` was found in `ci.yml` but no Python or shell code in `apps/api/` reads it — the test runner uses a different mechanism. Safe to delete.

**Risk**: LOW. The grep should be case-sensitive and include both `${VAR}` and `$VAR` syntax variants. Verify the var isn't used as a deploy-time secret being passed to a downstream system (Vercel env, CloudWatch dashboard variable, etc.) — those uses won't grep locally.

---
