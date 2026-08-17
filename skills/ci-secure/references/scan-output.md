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
- `scan_incomplete` — workflow files skipped or unreadable, with a reason
  each. Non-empty means coverage is PARTIAL.
- `timings` — script-owned spans, `total_run_s` leading; `record_timing.py`
  appends the orchestrator-measured `fixes_s`.
