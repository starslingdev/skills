---
pattern: OPT36
impact: LOW
class: static
detector: yaml-on-trigger
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cron-schedule-too-frequent
title_template: 'Cron Schedule Too Frequent'
category: 7
---

# OPT36 — Cron Schedule Too Frequent

**TL;DR**: A scheduled (cron) workflow runs more often than it needs to, piling up runs.

**Anti-pattern**: Cron-triggered workflows running more frequently than necessary for their purpose. Common examples: cleanup/triage jobs running every 5 minutes when every 15-60 minutes would suffice, or scheduled builds running hourly when daily is adequate.

**Detection heuristic**:

```bash
# Find cron schedules and classify frequency
grep -rn 'cron:' .github/workflows/ | while read line; do
  file=$(echo "$line" | cut -d: -f1)
  cron=$(echo "$line" | grep -oP "'[^']+'" | tr -d "'")
  min_field=$(echo "$cron" | awk '{print $1}')
  # Flag schedules running more than 4x/hour
  if echo "$min_field" | grep -qP '^\*/[1-9]$|^\*/1[0-4]$'; then
    echo "OPT36 hit: $file runs every $(echo $min_field | tr -d '*/')min — verify frequency is justified"
  fi
done
```

**Fix**: Increase the cron interval to match the actual operational need. For issue cleanup/triage bots: `*/15` or `*/30` is typically sufficient. For scheduled builds: daily or every 6 hours. Document the rationale for the chosen frequency.

**Real-world example (mastra)**: PR #14432 changed spam issue cleanup from every 5 minutes (`*/5 * * * *`) to every 15 minutes — the job rarely finds new spam within a 5-minute window.

**Tier-2 render note**: When run history shows consecutive `event=schedule`
runs on the same `head_sha`, OPT36 can promote as measured schedule burn. The
finding must size only the schedule-event subset, carry `sizing_basis=measured`,
price from successful schedule-event job timings only, stamp
`tier2_run_subset_events: ["schedule"]`, and carry a `non_pr_event`
certificate. Static cron-frequency hits without same-`head_sha` run evidence
remain modeled residual findings in "Also noticed"; a matching static row on a
measured workflow is superseded by the measured finding.

---
