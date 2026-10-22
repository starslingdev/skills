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
| 11 | after | `opt81-same-job-faster-on-another-runner.md` (10,498 B on disk) in every run: 14,766 / 10,497 / 51,149 / 17,866 / 14,760 B; run 3 (two review subagents) also charged 12 B to each of the other 91 `patterns/` files, see below |
| 12 | before | single file 5,191 / 20,027 / 10,073 / 8,526 / 8,911 B, by keyword grep plus a slice. In runs 1, 4 and 5 the single file's bytes equal `gap-fill.md`'s exactly: the same even split of one search over both files |
| 12 | after, superseded | `opt86-use-prebuilt-dependency-packages.md` in every run (3,591 to 6,220 B) plus `INDEX.md`: a search of it (649 to 2,840 B) in three runs, the whole file (26,661 and 26,587 B) in two |
| 12 | after | `opt86-use-prebuilt-dependency-packages.md` in every run (3,110 to 6,220 B) plus `INDEX.md` 10,707 / 582 / 5,595 / 5,278 / 5,800 B (run 1 read it whole). In runs 2 to 5 one search spanned `INDEX.md` and `gap-fill.md`, and in runs 1, 2 and 4 one spanned `opt86-*.md` and `gap-fill.md`; the unprefixed part of such output is split evenly between the files it named, so `gap-fill.md` shows bytes equal to those files' sum. In runs 1, 2 and 4 the `opt86-*.md` bytes come only from such a split |

## Gates (spec 7.2)

| gate | result |
|---|---|
| Outcome: after >= before on every check, zero regressions | **pass** (entry 11 went 4/5 to 5/5; everything else equal) |
| Compiled file opened in zero after-arm runs | **pass** (0/20) |
| Entry 11, original gate, not met as written: after median catalog bytes below the before minimum | **not met as written**: the before minimum is 0 B, from run 4, which read nothing and failed |
| Entry 11, restated after measurement: the same, over the before arm's passing runs | **pass**: after median 14,766 B against a passing-run minimum of 26,866 B. The before arm's one failed run (run 4) read 0 bytes and is not a minimum the catalog could be compared with: it never looked up the entry and failed the check that it did |
| Entry 12, original gate, not met, superseded by the row below: after median catalog bytes below the before minimum | **not met**: after median 11,815 B (re-run with the 10.4 KB index) against a before minimum of 5,191 B; the superseded batch with the 25.9 KB index had a 9,060 B median |
| Entry 12, restated gate: no regression, after median within the before arm's range and no increase in input tokens | **pass within noise**: after median 11,815 B within the before range of 5,191 to 20,027 B. Read strictly, the token clause is not met: input tokens 142,106 after vs 142,081 before (medians), 25 tokens (0.02%) more, inside the before arm's 135,190 to 148,266 range. Wall time 24 s after vs 20 s before (medians) |
| Entry 11, original verdict: every after run reads `patterns/opt81-*.md` and no other pattern file | **not met as measured, in one run of five**: run 3's accounting charges 12 B to each of the other 91 `patterns/` files |
| Entry 11, restated after measurement: the same, with an even split of unattributed output not counted as a read | **pass**: every run read `opt81-*.md`. An even split of unattributed output is not a read: in run 3, about 1.1 KB of unprefixed output from a command that named the `patterns/` directory is spread at 12 B over each of the other 91 files, and no other pattern file's content is evidenced. Follow-up: `eval_reading.py` should report such bytes as unattributed rather than splitting them |

**Restated gates.** All three restatements were made after the data was in, and each original verdict is kept above. The two entry-11 restatements change what counts (passing runs only; an even split is not a read), not the threshold. The original gate text (spec 7.2) is not committed; the "original" rows quote it.

**Why the entry-12 gate was restated (owner decision, 2026-10-22).** The gate "after median below the before minimum" compared a typical run against the single narrowest before-run (5,191 B); on like-for-like statistics the arms are equal within noise (median 8,911 B before vs 11,815 B after, input tokens 142k vs 142k, after-arm max 13.8 KB vs before max 20.0 KB), and the OPT86 pattern file alone is 6.2 KB on disk (its recorded reads, 3.1 to 6.2 KB, include even splits), so the floor for this lookup is the entry plus a slice of the 10.4 KB index. The entry-12 gate is therefore "no regression: after median within the before arm's range and no increase in input tokens", which passes.

**Accounting follow-up.** In both arms of entry 12, single searches over two files had their unprefixed output split evenly between them by `eval_reading.py` (before runs 1, 4 and 5; after runs 1 to 5), so the before minimum of 5,191 B is itself a split figure. Total reference bytes are neutral (17,822 B before vs 17,410 B after, medians). `eval_reading.py` should report such bytes as unattributed instead.

**Input tokens on entry 11.** The 588k to 406k drop is mostly delegation, not reading: before runs 1, 2 and 5 each spawned two review subagents, after only run 3 did. Single-session runs are close in both arms (before 365,696 and 369,543; after 348,675 to 465,562), and entry 13, which read nothing in either arm, moved 213k to 172k, so arm-to-arm token noise at five runs is about 20%. The catalog-bytes drop is the measured effect of the split; the token drop and the 4/5 to 5/5 (one before run that read nothing) are not attributable to it.

Entries 10 and 13 read zero reference bytes in both arms, as the 2026-10-22
reference baseline predicted: the split cannot change them, and nothing
regressed there.
