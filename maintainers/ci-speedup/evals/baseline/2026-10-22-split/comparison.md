# ci-speedup catalog split: before and after, 2026-10-22

Both arms ran in one batch on one machine: same model (`claude-opus-5-5`),
same Claude Code (2.1.308), same harness and reading module, five runs per
entry. BEFORE is `main` at `d6f8c77` (one 393 KB catalog file); AFTER is the
split branch at `7490804` (one file per pattern plus `INDEX.md`). Entry 12's
after arm was re-run after the index slim, at `4d67e5b` (`INDEX.md` 25.9 KB
to 10.4 KB); its first after-arm batch is kept as the "superseded" row. How
each arm ran: `before/summary.md`, `after/summary.md`,
`after-superseded/summary.md`. Values are median (min to
max) over the five runs; "catalog" is the single file in BEFORE and any
`references/patterns/` file in AFTER.

| id | case | arm | passed | reference bytes | catalog bytes | compiled file opened | input tokens | wall (s) |
|---|---|---|---|---|---|---|---|---|
| 10 | `full-run-synthetic` | before | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 324,355 (276,704 to 384,584) | 34 (31 to 41) |
| 10 | `full-run-synthetic` | after | 5/5 | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 320,152 (269,177 to 334,992) | 41 (30 to 45) |
| 11 | `quality-review-opt81` | before | 4/5 | 69,403 (0 to 88,917) | 44,245 (0 to 59,448) | 4/5 | 587,815 (365,696 to 1,008,650) | 95 (62 to 126) |
| 11 | `quality-review-opt81` | after | 5/5 | 22,290 (10,497 to 96,821) | 14,766 (10,497 to 52,241) | 0/5 | 406,378 (348,675 to 797,361) | 58 (50 to 126) |
| 12 | `gap-fill-coverage-lookup` | before | 5/5 | 17,822 (10,382 to 20,027) | 8,911 (5,191 to 20,027) | 5/5 | 142,081 (135,190 to 148,266) | 20 (19 to 24) |
| 12 | `gap-fill-coverage-lookup` | after, superseded (25.9 KB index) | 5/5 | 9,060 (6,869 to 33,843) | 9,060 (6,869 to 32,807) | 0/5 | 138,004 (133,856 to 154,439) | 19 (18 to 22) |
| 12 | `gap-fill-coverage-lookup` | after (10.4 KB index) | 5/5 | 17,410 (9,686 to 19,054) | 11,815 (4,843 to 13,817) | 0/5 | 142,106 (133,641 to 142,430) | 24 (19 to 26) |
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
| 12 | after, superseded | `opt86-use-prebuilt-dependency-packages.md` in every run (3,591 to 6,220 B) plus `INDEX.md`: a search of it (649 to 2,840 B) in three runs, the whole file (26,661 and 26,587 B) in two |
| 12 | after | `opt86-use-prebuilt-dependency-packages.md` in every run (3,110 to 6,220 B) plus `INDEX.md` 10,707 / 582 / 5,595 / 5,278 / 5,800 B (run 1 read it whole). In runs 3 to 5 one search spanned `INDEX.md` and `gap-fill.md` and the unprefixed part of its output is split evenly between them, so the two files show equal or near-equal bytes |

## Gates (spec 7.2)

| gate | result |
|---|---|
| Outcome: after >= before on every check, zero regressions | **pass** (entry 11 went 4/5 to 5/5; everything else equal) |
| Compiled file opened in zero after-arm runs | **pass** (0/20) |
| Entry 11: after median catalog bytes below the before minimum, over the before arm's passing runs | **pass**: after median 14,766 B against a passing-run minimum of 26,866 B. The before arm's one failed run (run 4) read 0 bytes and is not a minimum the catalog could be compared with: it never looked up the entry and failed the check that it did |
| Entry 12: after median catalog bytes below the before minimum | **fail**: after median 11,815 B (re-run with the 10.4 KB index) against a before minimum of 5,191 B (before median 8,911 B). The superseded batch with the 25.9 KB index had a 9,060 B median; slimming the index did not bring the lookup below the single-file arm's narrowest grep |
| Entry 11: every after run reads `patterns/opt81-*.md` and no other pattern file | **pass**: every run read `opt81-*.md`. An even split of unattributed output is not a read: in run 3, about 1.1 KB of unprefixed output from a command that named the `patterns/` directory is spread at 12 B over each of the other 91 files, and no other pattern file's content is evidenced. Follow-up: `eval_reading.py` should report such bytes as unattributed rather than splitting them |

Entries 10 and 13 read zero reference bytes in both arms, as the 2026-10-22
reference baseline predicted: the split cannot change them, and nothing
regressed there.
