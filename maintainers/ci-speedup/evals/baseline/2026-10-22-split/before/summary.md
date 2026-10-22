# ci-speedup catalog split: BEFORE arm (single-file catalog), 2026-10-22

The skill under test is `skills/ci-speedup/` of `main` at `d6f8c77`: the
single 5,360-line, 393 KB `references/optimization-patterns.md` is the only
catalog and `SKILL.md` routes to it.

- **Model:** `claude-opus-5-5` (`--model claude-opus-5-5`), Claude Code
  2.1.308, run locally on macOS on 2026-10-22, in the same batch as the
  other arm.
- **Harness:** `maintainers/scripts/run_skill_evals.py` from `main` at
  `d6f8c77`, byte-identical in both arms, with `maintainers/scripts/eval_reading.py`
  from this branch (the version that counts `references/patterns/` files as
  catalog) in both arms.
- **Command, per entry:**
  `python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --id <id> --runs 5 --model claude-opus-5-5`,
  run from an export of the tree named above. Entries 10, 11 and 13 ran one
  after another (scratch paths under `/tmp` could collide), and the two
  arms' 10/11/13 chains never overlapped; entry 12 ran beside them from its
  own export.
- **Files:** one results JSON per invocation, exactly as the runner wrote
  it, with one redaction: entry 11 run 4's `session_log` is cut to its file
  name (the runner recorded an absolute local path). The table is the output of
  `python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py maintainers/ci-speedup/evals/baseline/2026-10-22-split/before`.
- **One invocation lost and re-run:** the first entry-12 batch started in
  the same second as the entry-10 batch from the same export, both wrote
  the same results file name, and entry 10's later writes overwrote it.
  Entry 12 was re-run from a separate export; its console lines from the
  lost batch (all five passed, 3 to 23 KB of references) are not counted.

| id | case | runs | passed | not run | reference bytes | of which catalog | reference files | catalog opened | input tokens | wall time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 | `full-run-synthetic` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 324,355 (276,704 to 384,584) | 34 (31 to 41) |
| 11 | `quality-review-opt81` | 5 | 4 | 0 | 69,403 (0 to 88,917) | 44,245 (0 to 59,448) | 2 (0 to 2) | 4/5 | 587,815 (365,696 to 1,008,650) | 95 (62 to 126) |
| 12 | `gap-fill-coverage-lookup` | 5 | 5 | 0 | 17,822 (10,382 to 20,027) | 8,911 (5,191 to 20,027) | 2 (1 to 2) | 5/5 | 142,081 (135,190 to 148,266) | 20 (19 to 24) |
| 13 | `no-catalog-read-needed` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 213,269 (170,960 to 221,978) | 27 (21 to 31) |

Pass count per check:

| id | check | passed |
|---|---|---|
| 10 | `ci-speedup-skill-invoked` | 5/5 |
| 10 | `data-pass-ran` | 5/5 |
| 10 | `report-drills-ci-test-as-long-pole-1` | 5/5 |
| 10 | `self-check-passed` | 5/5 |
| 10 | `nothing-committed-or-pushed` | 5/5 |
| 11 | `ci-speedup-skill-invoked` | 5/5 |
| 11 | `data-pass-ran` | 5/5 |
| 11 | `names-the-catalog-only-fork-caveat` | 5/5 |
| 11 | `read-some-reference-material` | 4/5 |
| 11 | `read-the-opt81-catalog-entry` | 4/5 |
| 12 | `ci-speedup-skill-invoked` | 5/5 |
| 12 | `names-opt86` | 5/5 |
| 12 | `read-some-reference-material` | 5/5 |
| 13 | `ci-speedup-skill-invoked` | 5/5 |
| 13 | `data-pass-ran` | 5/5 |
| 13 | `states-the-measured-wait` | 5/5 |
| 13 | `names-the-slowest-job` | 5/5 |
