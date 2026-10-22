# ci-speedup eval baseline, 2026-10-22

What the upcoming catalog split is measured against: the five runnable
entries of `skills/ci-speedup/evals/evals.json`, five runs each, on today's
single-file catalog (`references/optimization-patterns.md`, 5,360 lines,
393 KB).

- **Skill and harness:** branch `ci-speedup/eval-reading-baseline`. The
  catalog and `SKILL.md` are byte-identical to `main` at `a2249fd`; only
  maintainer tooling and `evals/` differ.
- **Model:** `claude-opus-5-5` (`--model claude-opus-5-5`), Claude Code
  2.1.308, run locally on macOS.
- **Command, per entry:**
  `python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --id <id> --runs 5 --model claude-opus-5-5`.
  Entries 10, 11 and 13 ran one after another (their sessions pick scratch
  paths under `/tmp` and could collide); 6 and 12 ran beside them.
- **Files:** one results JSON per invocation, exactly as the runner wrote it
  (no hand edits).
- **Measured with** `maintainers/scripts/eval_reading.py` after review fixes:
  entries 11 and 12 at commit `1291ec4`, entries 6, 10 and 13 at `ffac425`.
  The one difference between the two: at `ffac425` a `Bash` result flagged as
  an error (the command exited non-zero) counted nothing even when it printed
  reference text. That undercount was found in this batch (entry 11 run 2
  printed the OPT81 entry with `awk`, then a later `cat` failed, and the run
  recorded 0 bytes), fixed in `1291ec4`, and entries 11 and 12 were re-run;
  their first-pass results were discarded. Entries 6, 10 and 13 read zero
  reference bytes in every run and were not re-run, so an errored shell read
  in them would not show here.
- **Still true of the accounting:** a compound command charges all of the
  output of its reading commands to the reference files they name;
  unprefixed output over several files splits evenly; name-only listings
  (`grep -l`, `ls`, `Grep` without `content` mode) count nothing; and the
  catalog is the file `optimization-patterns.md`, so a split catalog must map
  its per-pattern files to "catalog" before the two are compared.
- **Replaces** an earlier batch on the same date measured with the tracker at
  `b976489`, before review fixed several attribution gaps (shell-variable
  paths, searches from the skill root, file listings counted as reads).

Input tokens are the whole session, subagents included (the last result
event's `modelUsage`, recorded as `reading.session.input_tokens`). For
sessions that start background subagents (entry 11, runs 1, 2, 3 and 5) the
runner's own `input_tokens` field covers only part of the session (65k to
558k against 804k to 947k whole-session). Wall time is the harness clock around the session. "Reference files"
counts files under `references/` with at least one byte read. Values are
median (min to max) over the five runs; the table is the output of
`python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py maintainers/ci-speedup/evals/baseline/2026-10-22`.

| id | case | runs | passed | not run | reference bytes | of which catalog | reference files | catalog opened | input tokens | wall time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 6 | `security-ask-routes-to-ci-secure` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 214,692 (170,031 to 255,541) | 35 (33 to 45) |
| 10 | `full-run-synthetic` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 325,681 (270,983 to 372,219) | 51 (31 to 69) |
| 11 | `quality-review-opt81` | 5 | 5 | 0 | 74,628 (30,540 to 112,567) | 42,540 (28,604 to 75,945) | 2 (1 to 2) | 5/5 | 809,986 (428,362 to 946,720) | 132 (47 to 153) |
| 12 | `gap-fill-coverage-lookup` | 5 | 5 | 0 | 19,666 (12,397 to 20,820) | 13,457 (9,316 to 17,978) | 2 (1 to 2) | 5/5 | 145,747 (142,137 to 178,192) | 24 (17 to 31) |
| 13 | `no-catalog-read-needed` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 171,706 (170,835 to 213,999) | 22 (20 to 26) |

Pass count per check:

| id | check | passed |
|---|---|---|
| 6 | `ci-secure-skill-invoked` | 5/5 |
| 6 | `ci-speedup-skill-not-invoked` | 5/5 |
| 6 | `ci-speedup-pipeline-not-run` | 5/5 |
| 10 | `ci-speedup-skill-invoked` | 5/5 |
| 10 | `data-pass-ran` | 5/5 |
| 10 | `report-drills-ci-test-as-long-pole-1` | 5/5 |
| 10 | `self-check-passed` | 5/5 |
| 10 | `nothing-committed-or-pushed` | 5/5 |
| 11 | `ci-speedup-skill-invoked` | 5/5 |
| 11 | `data-pass-ran` | 5/5 |
| 11 | `names-the-catalog-only-fork-caveat` | 5/5 |
| 11 | `read-some-reference-material` | 5/5 |
| 11 | `read-the-opt81-catalog-entry` | 5/5 |
| 12 | `ci-speedup-skill-invoked` | 5/5 |
| 12 | `names-opt86` | 5/5 |
| 12 | `read-some-reference-material` | 5/5 |
| 13 | `ci-speedup-skill-invoked` | 5/5 |
| 13 | `data-pass-ran` | 5/5 |
| 13 | `states-the-measured-wait` | 5/5 |
| 13 | `names-the-slowest-job` | 5/5 |

## What it shows

- **The agent opens the catalog only when the question needs it.** The full
  audit (10), the routing case (6) and the control question (13) read zero
  bytes of `references/` in all fifteen runs. The split cannot reduce reading
  there; in the candidate arm they show that nothing regressed.
- **quality-review-opt81 reads slices, never the whole file.** No run read
  more than 76 KB of the 393 KB catalog. The one run that verified in the
  main session (4) read 30 KB of it and nothing else. The four runs that
  delegated to two review subagents (1, 2, 3, 5) read 29 to 76 KB of catalog
  plus 10 to 43 KB of the review rubric, because each subagent locates and
  reads the entry and the rubric again. The spread comes from delegation, not
  from the catalog's shape.
- **gap-fill-coverage-lookup** finds OPT86 in three or four tool calls: a
  keyword grep over the catalog and `gap-fill.md`, then a slice of the entry.
  9 to 18 KB of catalog per run, most of it grep output.

## Discrimination proof

Not re-run with this batch. Entry 11 was run once with the OPT81 section cut
from the session's catalog copy and the published copy blocked
(`python3 maintainers/ci-speedup/scripts/eval_withhold_entry.py OPT81 --id 11 --model claude-opus-5-5`),
measured with the tracker and wrapper at `b976489` (before the wrapper
always guarded `gh` and stopped exempting the GitHub API host):

    FAIL  ci-speedup #11 quality-review-opt81 (run 1/1)  13 tool calls, 615932 in / 4883 out tokens, 67s, $0.55, refs 8.0 KB in 1 files, catalog opened; session 616k in, 67s wall
        ok   ci-speedup-skill-invoked  (count=1)
        ok   data-pass-ran  (count=1)
        FAIL names-the-catalog-only-fork-caveat  (missing)
        ok   read-some-reference-material  (reference_bytes=8228)
        FAIL read-the-opt81-catalog-entry  (catalog_entries_read=0)

The agent's answer: `MISSING-FROM-CARD: can't be determined: the OPT81
catalog entry and its fix recipe don't exist locally, and the upstream link
returns 404`. Before the published copy was blocked, two withheld runs passed
by fetching the entry from GitHub (once with `curl`, once with `gh api`).
