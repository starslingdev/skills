---
pattern: OPT71
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: expensive-non-required-check
title_template: 'Expensive non-required critical-path check (de-scope, gate, or speed up)'
category: 14
---

# OPT71 — Expensive Non-Required Check on the Critical Path

**TL;DR**: One of the slowest checks holding up your PRs isn't even required to merge. If it's just advisory (a comment or preview), stop running it on every PR; if it's a real test, speed it up instead — don't turn it off.

**Anti-pattern (the de-scope case)**: A workflow that runs on every `pull_request` activity type (including `synchronize` — every push) and sits at/near the top of the measured critical path, is absent from the required-status-check list, AND whose output is genuinely advisory (a size-diff comment, a preview deploy, a non-blocking lint annotation) - so the developer's wait on it is pure friction. **This case alone is safe to de-scope.** A non-required check that actually runs tests or validates a build is NOT this anti-pattern: it is load-bearing developer signal even when branch protection doesn't list it, and the lever is to make it faster, not to stop running it.

**Detection heuristic** (routed):

1. The check is on the measured critical path (high p50 in the PR check-runs).
2. Cross-reference the repo's required checks: `gh api repos/{owner}/{repo}/rulesets` and `gh api repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks`. The check name is **not** in that set. (When the required-status data is missing OR only partially readable — common when auditing a repo you don't own, branch protection returns 404 — required-status is **unknown**; the router does NOT assert "non-required" and does NOT emit OPT71. It surfaces "required status unknown" rather than recommend de-scoping a check that might gate the merge.)

The router stops at (1)-(2). Consumer enumeration is a mandatory **reasoning-step precondition**, not part of detection:

3. *(reasoning step, not auto-detected)* **Enumerate the result's consumers** before recommending anything: does anything `needs:` this job, does a later step read its output, is there a downstream comment/label/deploy? A check that *looks* advisory but feeds a required aggregator is required-in-effect.

**Fix recipe**: Once consumers are enumerated and the check is genuinely advisory, the options in increasing aggressiveness are: (a) **narrow the trigger** — drop `synchronize` so it runs once per PR open/ready, not on every push; (b) **gate it** behind a `paths:` filter or a label so it only runs when relevant; (c) **make it advisory-async** — move it to run post-merge / on a schedule and post its result without blocking the PR. Pick the least aggressive option that removes the wait.

**Risk**: **MEDIUM** — reversible, but narrowing a trigger can drop a signal developers actually use (a preview URL they click, a size comment they read). Confirm with the consumers enumeration; if anyone relies on it per-push, prefer making it async over removing it.

**Guardrail**: Never de-scope a check whose required-status is **unknown** (branch-protection 404). Keep the advisory output reachable (async comment) rather than deleting it outright.

**Rollout**: Change the trigger on a branch, watch one week of PRs, confirm no one re-requests the dropped signal, then keep.

**Reporting contract — the Δ wall-clock is a DE-SCOPE CEILING.** The sized
`wall_clock_p50_s` is the wait removed *only if the check is dropped from the PR
path* — the maximum, realized only in the (safe) advisory case. Because the
detector can't classify advisory-vs-real, the report treats this saving as a
**ceiling by default**: the finding still RANKS by it (it IS the bottleneck, so
it leads the report), but the Δ wall-clock renders as a ceiling and is **NOT
counted in the saving total**. The fix step sets **`descope_recommended: true`**
on the finding ONLY when it confirms the check is genuinely advisory and
recommends de-scoping — that promotes the ceiling to a credited win. For a real
gate ("speed it up instead"), leave it unset: the realized saving is the
speed-up amount, which OPT72 / the cache family size separately, not this
de-scope ceiling.

---
