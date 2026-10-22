# ci-speedup catalog split: before and after, 2026-10-22

Both arms ran in one batch on one machine: same model (`claude-opus-5-5`),
same Claude Code (2.1.308), same harness and reading module, five runs per
entry. BEFORE is `main` at `d6f8c77` (one 393 KB catalog file); AFTER is the
split branch at `7490804` (one file per pattern plus `INDEX.md`). How each
arm ran: `before/summary.md`, `after/summary.md`. Values are median (min to
max) over the five runs; "catalog" is the single file in BEFORE and any
`references/patterns/` file in AFTER.

| id | case | arm | passed | reference bytes | catalog bytes | compiled file opened | input tokens | wall (s) |
|---|---|---|---|---|---|---|---|---|
| 10 | `full-run-synthetic` | before | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 324,355 (276,704 to 384,584) | 34 (31 to 41) |
| 10 | `full-run-synthetic` | after | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 320,152 (269,177 to 334,992) | 41 (30 to 45) |
| 11 | `quality-review-opt81` | before | 4/5 | 69,403 (0 to 88,917) | 44,245 (0 to 59,448) | 4/5 | 587,815 (365,696 to 1,008,650) | 95 (62 to 126) |
| 11 | `quality-review-opt81` | after | 5/5 | 22,290 (10,497 to 96,821) | 14,766 (10,497 to 52,241) | 0/5 | 406,378 (348,675 to 797,361) | 58 (50 to 126) |
| 12 | `gap-fill-coverage-lookup` | before | 5/5 | 17,822 (10,382 to 20,027) | 8,911 (5,191 to 20,027) | 5/5 | 142,081 (135,190 to 148,266) | 20 (19 to 24) |
| 12 | `gap-fill-coverage-lookup` | after | 5/5 | 9,060 (6,869 to 33,843) | 9,060 (6,869 to 32,807) | 0/5 | 138,004 (133,856 to 154,439) | 19 (18 to 22) |
| 13 | `no-catalog-read-needed` | before | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 213,269 (170,960 to 221,978) | 27 (21 to 31) |
| 13 | `no-catalog-read-needed` | after | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 171,911 (171,639 to 176,381) | 29 (20 to 38) |

Per-check pass counts (before / after): every check of entries 10, 12 and
13 passed 5/5 in both arms. Entry 11: `ci-speedup-skill-invoked`,
`data-pass-ran` and `names-the-catalog-only-fork-caveat` 5/5 in both;
`read-some-reference-material` and `read-the-opt81-catalog-entry` 4/5
before (run 4 answered without opening any reference file), 5/5 after.

## What each arm read, per run

| id | arm | catalog files read, per run |
|---|---|---|
| 11 | before | single file 44,245 / 54,439 / 26,866 / 0 / 59,448 B (runs 1, 2 and 5 delegated to two review subagents, each of which searched the file again) |
| 11 | after | `opt81-same-job-faster-on-another-runner.md` (10,497 B on disk) in every run: 14,766 / 10,497 / 51,149 / 17,866 / 14,760 B; run 3 (two review subagents) also charged 12 B to each of the other 91 `patterns/` files, see below |
| 12 | before | single file 5,191 / 20,027 / 10,073 / 8,526 / 8,911 B, by keyword grep plus a slice |
| 12 | after | `opt86-use-prebuilt-dependency-packages.md` in every run (3,591 to 6,220 B) plus `INDEX.md`: a search of it (649 to 2,840 B) in three runs, the whole file (26,661 and 26,587 B) in two |

## Gates (spec 7.2)

| gate | result |
|---|---|
| Outcome: after >= before on every check, zero regressions | **pass** (entry 11 went 4/5 to 5/5; everything else equal) |
| Compiled file opened in zero after-arm runs | **pass** (0/20) |
| Entry 11: after median catalog bytes below the before minimum | **fail as written** (before minimum is 0, from the run that read nothing and failed). Against the four passing before runs: after median 14,766 B is below their minimum 26,866 B |
| Entry 12: after median catalog bytes below the before minimum | **fail** (after median 9,060 B; before minimum 5,191 B, before median 8,911 B). Two after runs read the whole 26 KB `INDEX.md` |
| Entry 11: every after run reads `patterns/opt81-*.md` and no other pattern file | **fail as measured, in one run of five**: run 3 read `opt81-*.md` 5 times (51 KB) and its accounting also charges 12 B to each other `patterns/` file, the even split of about 1.1 KB of unprefixed output from a reading command that named the directory. No other pattern file's content is evidenced; the session was not kept, so the command is not recorded |

Entries 10 and 13 read zero reference bytes in both arms, as the 2026-10-22
reference baseline predicted: the split cannot change them, and nothing
regressed there.
