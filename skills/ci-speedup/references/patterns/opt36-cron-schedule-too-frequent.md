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

**TL;DR**: A scheduled (cron) workflow runs more often than it needs to, or reruns scanner categories whose explicitly defined inputs have not changed.

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

For a multi-category scanner, keep a separate last-successful commit for each category. Before the next scheduled scan, compare that category's current input paths with its own baseline and skip it only when the complete comparison contains no relevant change. If the baseline, compare result, exclusion syntax, or changed-file list is unavailable or incomplete, run the scan. Keep pull-request and manual scans unchanged. This fail-open recipe preserves coverage while avoiding work whose inputs are proven unchanged.

**Public evidence**: openclaw/openclaw#191805 consolidated scheduled CodeQL admission while preserving a separate successful baseline per category. With seven categories sharing unchanged inputs and a valid baseline, its public evidence reports run, job, and comparison requests falling from 21 to 3; unknown syntax, incomplete comparisons, unavailable blobs, and relevant changes retain the scans.

**Tier-2 render note**: When run history shows consecutive `event=schedule`
runs on the same `head_sha`, or a complete compare between consecutive scheduled
runs touches only the scanner's explicit inline `paths-ignore`, OPT36 can promote
as measured schedule burn. The
finding must size only the schedule-event subset, carry `sizing_basis=measured`,
price from successful schedule-event job timings only, stamp
`tier2_run_subset_events: ["schedule"]`, and carry a `non_pr_event`
certificate. The measured finding states a signed runner-minute saving and scales
one sampled schedule-run population to the schedule's 30-day volume. A changed
relevant path, an unreadable/truncated compare, or exclusions the audit cannot
classify retains the scan. Static cron-frequency hits without measured redundant
run evidence remain modeled residual findings in "Also noticed"; a matching
static row on a measured workflow is superseded by the measured finding.

---
