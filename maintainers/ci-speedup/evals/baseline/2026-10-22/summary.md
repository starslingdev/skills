# ci-speedup eval baseline, 2026-10-22

What the upcoming catalog split is measured against: the five runnable
entries of `skills/ci-speedup/evals/evals.json`, five runs each, on today's
single-file catalog (`references/optimization-patterns.md`, 5,360 lines,
393 KB).

- **Skill and harness:** branch `ci-speedup/eval-reading-baseline`, based on
  `main` at `a2249fd`. The catalog and `SKILL.md` are byte-identical to that
  commit; only maintainer tooling and `evals/` differ.
- **Model:** `claude-opus-5-5` (`--model claude-opus-5-5`), Claude Code
  2.1.307, run locally on macOS.
- **Command, per entry:**
  `python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --id <id> --runs 5 --model claude-opus-5-5`.
  Entries 10, 11 and 13 ran one after another (their sessions pick scratch
  paths under `/tmp` and could collide); 6 and 12 ran beside them.
- **Files:** one results JSON per invocation, as the runner wrote it, with one
  hand correction: in entry 11 run 5, `catalog_entries_read` listed `OPT81`
  twice (a counting bug fixed in the same change, with a test); the list was
  deduplicated and that run's check detail changed from `=2` to `=1`.
- **Measured with** `maintainers/scripts/eval_reading.py` as of commit
  `b976489`. Review then fixed several attribution gaps, so the figures here
  carry these known biases: reads through a shell variable (`$D/references/x`),
  `./references/x`, or a relative `references/` directory grep counted zero
  (undercount); a `sed` slice or grep that showed an entry's body without its
  heading recorded no entry (entry 12 run 3 has none), while a heading-only
  grep recorded one; a remote copy (`curl`, `gh api`) piped into a reader
  counted as the local file. Still true after the fixes: a compound command
  charges all of its output to the reference files it names, unprefixed output
  over several files splits evenly, and the catalog is the file
  `optimization-patterns.md`, so a split catalog must map its per-pattern
  files to "catalog" before the two are compared.
- **Check change after the baseline:** entry 11's answer check now also needs
  a runner or label word, accepts `isn't available` / `aren't offered`, and
  rejects `none`; its five baseline passes were graded on the older pattern.
- **Discarded:** a first five-run pass of entry 12. Its prompt did not name
  the skill, and in one run the agent skipped the Skill tool and read an older,
  globally installed copy of ci-speedup in the maintainer's home directory
  instead of the mounted one. The prompt now names the skill, the entry checks
  that the skill was invoked, and reading accounting ignores absolute paths
  outside the mounted copy.

Input tokens are the whole session, subagents included (the last result
event's `modelUsage`, recorded as `reading.session.input_tokens`). For
sessions that start background subagents (entry 11, runs 1, 3 and 5) the
runner's own `input_tokens` field covers only the final resumed turn and reads
60k to 67k. Wall time is the harness clock around the session. "Reference
files" counts files under `references/` with at least one byte read. Values
are median (min to max) over the five runs; the table is the output of
`python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py maintainers/ci-speedup/evals/baseline/2026-10-22`.

| id | case | runs | passed | not run | reference bytes | of which catalog | reference files | catalog opened | input tokens | wall time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 6 | `security-ask-routes-to-ci-secure` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 212,809 (168,328 to 246,086) | 38 (29 to 54) |
| 10 | `full-run-synthetic` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 303,642 (268,264 to 359,475) | 35 (30 to 45) |
| 11 | `quality-review-opt81` | 5 | 5 | 0 | 53,015 (15,240 to 92,283) | 41,800 (13,303 to 46,487) | 2 (1 to 2) | 5/5 | 848,381 (314,676 to 935,507) | 111 (42 to 142) |
| 12 | `gap-fill-coverage-lookup` | 5 | 5 | 0 | 13,811 (6,549 to 20,705) | 10,023 (3,668 to 13,462) | 2 (2 to 2) | 5/5 | 137,654 (134,358 to 178,684) | 23 (20 to 32) |
| 13 | `no-catalog-read-needed` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 171,067 (170,618 to 213,292) | 30 (21 to 43) |

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
  more than 47 KB of the 393 KB catalog. Runs that verified in the main
  session (2, 4) read 13 to 15 KB of it (`sed -n` / `awk` over the OPT81
  range). Runs that delegated to two review subagents (1, 3, 5) read 42 to
  46 KB, because each subagent locates and reads the entry again (runs 1, 3,
  4 and 5 also read the review rubric). The spread comes from delegation, not from the
  catalog's shape.
- **gap-fill-coverage-lookup** finds OPT86 in three or four tool calls: a
  keyword grep over the catalog and `gap-fill.md`, then a `sed -n` of the
  entry. 3.7 to 13.5 KB of catalog per run, most of it grep output.

## Discrimination proof

Entry 11 run once with the OPT81 section cut from the session's catalog copy
and the published copy blocked
(`python3 maintainers/ci-speedup/scripts/eval_withhold_entry.py OPT81 --id 11 --model claude-opus-5-5`):

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
