# CI speedup mutation-prune report

This report records the no-deletion audit that precedes any test pruning. The
audit is maintainer-only and runs without network access. It checks out a
disposable worktree, changes one engine behavior there, runs the complete test
suite, and records the failing test nodes from JUnit output. An interrupted
run cannot alter the source tree that launched the audit.

The machine-readable evidence is in `mutation-prune-report.json`. Reproduce it
from the repository root with:

```bash
python3 maintainers/ci-speedup/scripts/mutation_prune.py \
  --starting-test-count 11064 \
  --starting-local-suite-wall-seconds 130.859 \
  --ci-suite-wall-seconds 130
```

The starting tree collected 11,064 tests. The audited tree collects 11,067,
including all three audit-harness tests, and its audit baseline took 128.394
seconds. The starting tree's local four-worker suite took
130.859 seconds wall time and reported 11,030 passed, 33 skipped, one expected
failure, and 155 subtests. The latest successful main CI test step took 130
seconds. Runtime varies by machine; the mutation IDs, killer sets, overlap
candidates, and phrase clusters are the reproducible result.

## Mutant kill table

| Mutant | Module | Failing tests | End-to-end set kills it |
|---|---|---:|---|
| Path-filter measurement removed | `collect_runs.py` | 8 | yes |
| Wall-clock bound direction reversed | `wall_clock.py` | 46 | yes |
| Credited long-pole floor raised | `blocking_path.py` | 41 | yes |
| OPT21 detector dispatch disabled | `scan.py` | 101 | yes |

Every mutant is killed. The JSON report lists every failing test node for each
mutant and 139 non-protected tests whose observed kill set is a subset of the
end-to-end set's kill set. That broad overlap list is evidence for review, not
a deletion instruction. The scanner mutant disables one detector while
preserving the catalog and every other detector, so the listed failures have a
bounded interpretation without making their detailed assertions interchangeable.

## Wording-only cluster and proposed deletion

The strict phrase pass keeps only tests with exactly one assertion of the form
`"natural-language phrase" in output` (or `not in`) and separates present from
absent expectations. It found one same-direction cluster for
`No measured critical path`:

- Keep `test_render_no_poles_no_findings_still_dead_ends_gracefully`.
- Candidate for deletion: `test_render_no_poles_is_graceful`.

Neither test kills any registered mutant, but their inputs are distinct: the
candidate covers missing fields while the kept test covers explicit empty
fields. A later prune may remove the standalone candidate only after folding
both input shapes into the kept test. No test is deleted in this change.
