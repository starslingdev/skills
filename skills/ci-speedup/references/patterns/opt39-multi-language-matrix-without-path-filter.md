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

**Anti-pattern**: A security/static-analysis workflow uses a matrix to run language-specific scanners (CodeQL, Snyk, Semgrep, dependency-scan) but never gates the matrix legs on whether files of THAT language changed. CodeQL Python runs on JS-only PRs, CodeQL JavaScript runs on Python-only PRs — both consume runner time + analysis time for zero signal.

**Detection heuristic**:

- Workflow declares a matrix with `language: [javascript, python, ...]` (or similar)
- Uses `github/codeql-action/init` (or `snyk/actions`, `returntocorp/semgrep-action`, etc.) with `languages: ${{ matrix.language }}`
- The workflow has NO preceding `dorny/paths-filter` job or per-leg `if:` checking changed file extensions
- **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the legs then don't run on a normal PR. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)

**Fix**: Add a pre-job that uses `dorny/paths-filter@v3` to detect which languages changed, then gate each matrix leg with `if: needs.changes.outputs.<lang> == 'true'`. Example:

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

Caveat: GitHub renders skipped matrix legs as "skipped" (not "passed"). If you have a required-status-check rule on `codeql (javascript)`, change it to `codeql` without the matrix-leg suffix, or make the leg's terminal step a no-op success rather than `if:`-skipping the whole leg. Document the chosen approach in the workflow.

**Real-world example (blen-starter-kit deep-scan)**: `security.yml` runs CodeQL Python and JavaScript on every PR regardless of which app changed. JS-only PRs paid Python init+analyze (~30-60s) for nothing, and vice versa.

**Risk**: MEDIUM. If a polyglot file (a Python/JS bridge, or a config that affects both) changes, both legs should still run. The `paths-filter` rules need to be inclusive enough to catch shared config files.

---
