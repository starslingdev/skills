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
- Or an explicit Go, Rust, or Python lane, or a language-neutral contract/proof/conformance lane, installs that toolchain with `setup-go`, `setup-python`, a Rust toolchain action, or `rustup toolchain install`; a generic primary job using Python only to run repository tooling is not enough. The workflow must also run another language's lane (another job installs a different toolchain, such as `setup-node` or `setup-java`) or declare a complete input map; a single-language project's test job runs for every code change, and its non-code pull requests belong to OPT32
- The workflow has NO preceding `dorny/paths-filter` job or per-leg `if:` checking changed file extensions
- **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the legs then don't run on a normal PR. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)
- **Measured admission**: for a per-language job, require an explicit complete input map (for example, a reused scheduled-change action's `paths:` contract), then compare the exact base and head revisions recorded on each sampled run. Credit only the affected job's measured billed-equivalent minutes when every changed path is inside the workflow's declared input universe and outside that language's inputs. A missing map, stale run identity, unknown input, incomplete comparison, failed lookup, or unreadable job time receives no saving credit.

**Fix**: Derive each lane's complete input set from the repository, then select jobs from the exact pull-request comparison. Run every language job when the lookup is missing, incomplete, capped, malformed, or contains an input whose owner is unknown. A lane may be skipped only when the selector finished and proved that lane untouched. Example (fail-open selector, gated lanes, one aggregate required check):

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      # An empty output (selector failed or never set it) means "run".
      go: ${{ steps.select.outputs.go || 'true' }}
      rust: ${{ steps.select.outputs.rust || 'true' }}
    steps:
      - id: select
        continue-on-error: true
        uses: actions/github-script@<sha>
        with:
          script: |
            const owners = {
              go: [/\.go$/, /^go\.(mod|sum|work)$/],
              rust: [/\.rs$/, /^Cargo\.(toml|lock)$/, /^rust-toolchain/],
            };
            const shared = [/^testdata\//, /^\.github\/workflows\/ci\.yml$/];
            const inert = [/^docs\//, /\.md$/];  // proven to feed no lane
            const hit = (patterns, path) => patterns.some((re) => re.test(path));
            const run = { go: true, rust: true };   // default: run everything
            const files = await github.paginate(github.rest.pulls.listFiles, {
              ...context.repo, pull_number: context.payload.pull_request.number,
              per_page: 100,
            });
            const paths = files.flatMap((f) => [f.filename, f.previous_filename])
              .filter(Boolean);
            const known = paths.every((p) => hit(shared, p) || hit(inert, p)
              || Object.values(owners).some((patterns) => hit(patterns, p)));
            // listFiles stops at 3000 files: treat a capped or empty list as unknown.
            if (files.length > 0 && files.length < 3000 && known
                && !paths.some((p) => hit(shared, p))) {
              run.go = paths.some((p) => hit(owners.go, p));
              run.rust = paths.some((p) => hit(owners.rust, p));
            }
            core.setOutput('go', String(run.go));
            core.setOutput('rust', String(run.rust));

  go-contract:
    needs: changes
    # Skip only when the selector succeeded AND proved Go untouched.
    if: ${{ !cancelled() && (needs.changes.result != 'success' || needs.changes.outputs.go != 'false') }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
      - uses: actions/setup-go@<sha>
      - run: go test ./...

  rust-contract:
    needs: changes
    if: ${{ !cancelled() && (needs.changes.result != 'success' || needs.changes.outputs.rust != 'false') }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
      - uses: dtolnay/rust-toolchain@<sha>
      - run: cargo test --locked

  ci-ok:  # the required status check
    needs: [go-contract, rust-contract]
    if: ${{ always() }}
    runs-on: ubuntu-latest
    steps:
      - env:
          RESULTS: ${{ join(needs.*.result, ' ') }}
        run: |
          for result in $RESULTS; do
            case "$result" in success|skipped) ;; *) echo "lane: $result"; exit 1 ;; esac
          done
```

`dorny/paths-filter` can replace the script when its filters list every input of each lane; give its step `continue-on-error: true` and its job outputs the same `|| 'true'` default. For a scanner matrix, a job-level `if:` cannot read `matrix`: emit the language list from the selector (the full list on any failure) and use `matrix: { language: ${{ fromJSON(needs.changes.outputs.languages) }} }`.

Required checks: a job skipped by its own `if:` reports **Success** to branch protection under its own name, so a gated lane that stays a required check still lets an unrelated pull request merge. That is also why a plain `if: needs.changes.outputs.go == 'true'` is unsafe: when the selector fails, the lane is skipped and its required check reports Success with nothing validated, so the lane condition above runs on any selector result other than a proven `false`. A matrix leg left out of the matrix reports no check at all, so a required `codeql (python)` would stay expected forever; require the `ci-ok` aggregate (which fails on any failed or cancelled lane) instead of the per-leg names. Do not move this gating into a workflow-level `paths:` filter: a workflow that never starts leaves its required checks pending.

**Public evidence**: [OpenClaw PR #193865](https://github.com/openclaw/openclaw/pull/193865) adds fail-open pull-request owner selection for independent TypeScript and Rust contract proofs. Exact, complete comparisons may omit an unaffected owner; deleted, unknown, malformed, capped, or failed comparisons retain both jobs.

**Risk**: MEDIUM. If a polyglot file (a Python/JS bridge, or a config that affects both) changes, both legs should still run. The `paths-filter` rules need to be inclusive enough to catch shared config files.

---
