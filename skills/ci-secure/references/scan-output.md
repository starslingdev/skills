# The scan output JSON — shape of `$FINDINGS`

`scripts/run.py --out "$FINDINGS"` writes this file, and it is the
orchestrator's **source of truth** for every later phase of
[SKILL.md](../SKILL.md). Never re-parse the markdown report to make a
decision the JSON can answer.

## Contents

- [One finding object](#one-finding-object)
- [The attacker_scenario merged in at Phase 2.5](#the-attacker_scenario-merged-in-at-phase-25)
- [Top-level blocks](#top-level-blocks)

---

## One finding object

`findings` is a list, one object per occurrence (occurrences are grouped by
`pattern` for rendering and dispatch, not stored grouped).

```json
{
  "id": "f1",
  "pattern": "P14.9",
  "severity": "HIGH",
  "title": "Fork code executed with privileges in bench.yml",
  "workflow_file": ".github/workflows/bench.yml",
  "line": 8,
  "affected_jobs": ["bench"],
  "workflow_activity": {"runs_30d": 218, "last_run": "...", "dormant": false},
  "evidence": "   8: job `bench` on `pull_request_target` checks out `${{ github.event.pull_request.head.sha }}` then executes from the tree <-- here",
  "fix_strategy": "switch-to-pull-request-or-drop-head-checkout",
  "fix_recipe_anchor": "p149--fork-code-executed-with-privileges"
}
```

`workflow_activity` has **three** shapes, and they are not interchangeable.
The one above is the enriched success case. `{}` means no enrichment was
attempted — `--repo` was not passed. `{"status": "unavailable", "reason": ...}`
means enrichment WAS attempted and failed. The last one must never be read as
"active": the dormancy flag is simply absent, so a failed check looks identical
to a live workflow unless the distinction is kept. Dormancy drives a mandatory
note in the report and the `all` fix selection, so read the report's rendered
dormancy state rather than inferring one from a missing flag.

## The attacker_scenario merged in at Phase 2.5

The one non-scripted field. Written once per pattern group and merged onto
**every member** of that group:

```json
{
  ...,
  "attacker_scenario": "Any GitHub user can open a fork PR — no prior access to the repo. Because bench.yml runs on pull_request_target and checks out the fork's code, the attacker's install scripts execute holding the repo's write token and secrets: they can push commits, mint releases, or exfiltrate credentials."
}
```

## Top-level blocks

- `gh_checks` — per-network-gated-check status, `P14.11` above all: `ran` /
  `partial` / `skipped` plus the detail (pin counts, or the reason). An
  ABSENT `P14.11` key is the "not recorded" state — a coverage hole, never a
  pass.
- `gh_check_details` — the per-check detail a `partial` status refers to,
  including the unresolved-pin list. `gh_checks` gives the status word; this
  is where the specifics live.
- `scan_incomplete` — workflow files skipped or unreadable, with a reason
  each. Non-empty means coverage is PARTIAL.
- `dropped_matches` and `coverage_notes` — the other two coverage channels.
  **Coverage is `complete` only when all THREE of `scan_incomplete`,
  `dropped_matches` and `coverage_notes` are empty** (`report.py`'s
  `_coverage_is_complete`). Reading `scan_incomplete` alone will call a
  degraded run complete, which is the one thing the coverage rule forbids.
  The rendered report already applies all three, which is why Phase 3 copies
  its `Coverage:` line rather than recomputing one.
- `suppressed_findings` — matches the engine deliberately withheld, with the
  reason each was withheld. Not a coverage gap, but not nothing either.
- `security_score` — present in the JSON and **never rendered**. Phase 3
  forbids a score, ratio or `N/100` anywhere the user sees. It is listed here
  so it is recognized as out of bounds rather than mistaken for a summary
  worth surfacing.
- `timings` — script-owned spans. As `run.py --out` writes them at Phase 2:
  `run_start_epoch`, `activity_enrich_s`, `scan_total_s`, `scripted_end_epoch`,
  `scripted_total_s`. **`total_run_s` is NOT there yet** — `report.py` computes
  and writes it back at Phase 3, which is why Phase 6 composes its `Timing:`
  line from THIS block only after the render has happened. There is no
  `Timing:` line in the rendered report to copy; the only such string in the
  scripts is a stderr debug log. `record_timing.py` appends the
  orchestrator-measured `fixes_s`.
- Provenance, also top-level: `commit_sha`, `repo_tree_dirty`,
  `skill_commit_sha`, `skill_tree_dirty` and `catalog_patterns_evaluated`.
