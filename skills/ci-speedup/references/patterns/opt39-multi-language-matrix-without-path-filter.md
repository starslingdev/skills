---
pattern: OPT39
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: multi-language-matrix-without-path-filter
title_template: 'Multi-Language Matrix Without Path Filter'
category: 7
---

# OPT39 — Multi-Language Matrix Without Path Filter

**Anti-pattern**: A pull-request workflow runs language-specific work when the change touches none of that language's inputs. This includes a security scanner's language matrix and separate Go, Rust, or Python contract/proof jobs. The unused lane still installs its toolchain and runs its checks, consuming runner time for no signal.

**Detection heuristic**:

- Workflow declares a matrix with `language: [javascript, python, ...]` (or similar)
- Uses `github/codeql-action/init` (or `snyk/actions`, `returntocorp/semgrep-action`, etc.) with `languages: ${{ matrix.language }}`
- Or a job installs a Go, Rust, or Python toolchain with `setup-go`, `setup-python`, a Rust toolchain action, or `rustup toolchain install`
- The workflow has NO preceding `dorny/paths-filter` job or per-leg `if:` checking changed file extensions
- **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the legs then don't run on a normal PR. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)
- **Measured admission**: for a per-language job, require an explicit complete input map (for example, a reused scheduled-change action's `paths:` contract), then compare the exact base and head revisions recorded on each sampled run. Credit only the affected job's measured billed-equivalent minutes when every changed path is inside the workflow's declared input universe and outside that language's inputs. A missing map, stale run identity, unknown input, incomplete comparison, failed lookup, or unreadable job time receives no saving credit.

**Fix**: Derive each lane's complete input set from the repository, then select jobs from the exact pull-request comparison. Run every language job when the lookup is missing, incomplete, capped, malformed, or contains an input whose owner is unknown. A paths-filter pre-job is sufficient for simple repositories; a conservative owner-selection script is safer where languages share generated files or fixtures. Example:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      js: ${{ steps.filter.outputs.js }}
      python: ${{ steps.filter.outputs.python }}
    steps:
      - uses: actions/checkout@<sha>
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            js: ['**/*.js', '**/*.ts', '**/*.tsx', 'package.json', 'package-lock.json']
            python: ['**/*.py', 'pyproject.toml', 'requirements*.txt']

  codeql:
    needs: changes
    strategy:
      matrix:
        language: [javascript, python]
    if: |
      (matrix.language == 'javascript' && needs.changes.outputs.js == 'true') ||
      (matrix.language == 'python'     && needs.changes.outputs.python == 'true')
```

Caveat: GitHub renders skipped matrix legs and jobs as "skipped" (not "passed"). Keep an unchanged required aggregate check, or deliberately update branch protection so a language-specific required check is not left expected forever. Document the chosen approach in the workflow.

**Public evidence**: [OpenClaw PR #193865](https://github.com/openclaw/openclaw/pull/193865) adds fail-open pull-request owner selection for independent TypeScript and Rust contract proofs. Exact, complete comparisons may omit an unaffected owner; deleted, unknown, malformed, capped, or failed comparisons retain both jobs.

**Risk**: MEDIUM. If a polyglot file (a Python/JS bridge, or a config that affects both) changes, both legs should still run. The `paths-filter` rules need to be inclusive enough to catch shared config files.

---
