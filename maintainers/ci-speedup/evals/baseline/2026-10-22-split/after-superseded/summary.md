# ci-speedup catalog split: SUPERSEDED after-arm entry 12, 2026-10-22

Entry 12 of the after arm as first measured, on branch
`ci-speedup/catalog-split` at `7490804`, when `patterns/INDEX.md` was
25.9 KB / 167 lines. Superseded by the re-run in `../after/` after the index
was slimmed to 10.4 KB / 110 lines; kept unchanged for the record. Model,
CLI and harness as in `../after/summary.md`. The table is the output of
`python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py maintainers/ci-speedup/evals/baseline/2026-10-22-split/after-superseded`.

| id | case | runs | passed | not run | reference bytes | of which catalog | reference files | catalog opened | input tokens | wall time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | `gap-fill-coverage-lookup` | 5 | 5 | 0 | 9,060 (6,869 to 33,843) | 9,060 (6,869 to 32,807) | 2 (2 to 3) | 5/5 | 138,004 (133,856 to 154,439) | 19 (18 to 22) |

Pass count per check:

| id | check | passed |
|---|---|---|
| 12 | `ci-speedup-skill-invoked` | 5/5 |
| 12 | `names-opt86` | 5/5 |
| 12 | `read-some-reference-material` | 5/5 |

Split catalog files read:

| id | compiled file opened | `patterns/` files read |
|---|---|---|
| 12 | 0/5 | `patterns/INDEX.md` 5/5 runs, 2,840 (649 to 26,661) bytes; `patterns/opt86-use-prebuilt-dependency-packages.md` 5/5 runs, 6,220 (3,591 to 6,220) bytes |
