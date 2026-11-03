---
pattern: OPT100
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: infra-fault-retries
title_template: 'Infrastructure Faults Fail the Whole Run'
category: 10
---

# OPT100 — Infrastructure Faults Fail the Whole Run

**TL;DR**: Infrastructure failures can make an otherwise valid change pay for another complete CI attempt. Classify the first failing step before considering a bounded retry.

**Anti-pattern**: A lost runner, exhausted memory, crashed browser, or install/fetch network fault fails a merge-gating job and causes the whole workflow to run again.

**Detection heuristic**:

- Inspect only timestamp-bounded lines from the first failed step of each sampled failed job. Descriptive signatures live in `blocking_path.py`; assertion diagnostics never count. Network signatures require an install or fetch step. Missing logs or step timing do not imply a class.
- Emit when at least five failures match and they represent at least 20% of the job's sampled failures. The denominator includes failures with unreadable logs; disclose those gaps. A readable required-check set must establish that the job gates merge.
- Show counts by class. A runner receiving at least five and 80% of the matched failures is named for investigation when its labels explicitly identify it as self-hosted.
- Retry compute comes only from subsequent sampled attempts of the same run, with exactly one failed job in the preceding attempt. Ambiguous multi-failure retries receive no credit. Each attempt counts once; OPT64 does not claim runs already claimed here.
- A same-run, same-commit fail-then-pass without an infrastructure signature adds a pointer to OPT48's quarantine recipe, not another finding.

**Fix recipe**:

1. Investigate the first failing step with `sling`. For confirmed infrastructure classes only, evaluate a maximum of two attempts and short backoff at the step boundary. A retry wrapper or the tool's own bounded retry may help; never retry test assertions indiscriminately.
2. When failures cluster on one unhealthy self-hosted machine, stop routing work to that machine while degraded.
3. Quarantine confirmed flakes using OPT48's owner, expiry and default-branch guardrails.

**Guardrail**: A retry must not hide a product failure. Retry only classified infrastructure faults, preserve required checks, and count every retry so a rising failure rate remains visible.

**Sizing**: Runner-minutes retain the observed subsequent-attempt compute and separately scale the monthly credit by all-status monthly volume divided by sampled run count, matching the report's existing monthly contract. Wall-clock is median observed retry delay multiplied by the job's infrastructure failure rate, subject to the existing workflow and cross-workflow floors. Missing retries imply zero measured savings, not estimated retries. OPT48 retains the general reliability signal without savings; OPT100 isolates the infrastructure share. OPT64 covers repeated attempts outside the claimed infrastructure runs.
