---
pattern: OPT12
impact: MEDIUM
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: duplicated-setup-across-jobs
title_template: 'Duplicated Setup Across Jobs'
category: 2
---

# OPT12 — Duplicated Setup Across Jobs

**TL;DR**: Several jobs copy-paste the same setup steps, or repeat the same test suite before later work can run. A shared action only reduces maintenance; handing duplicate test work to the existing gate removes one suite run while preserving every check.

**Anti-pattern**: Multiple jobs with identical preamble steps (checkout, setup-node, pnpm install, build) that could be extracted to a composite action or use artifact handoff.

**Detection heuristic**:

```bash
# Hash the first N steps of each job and compare
# Look for identical sequences of: checkout → setup-pnpm → setup-node → install → build
```

**Fix**: Extract shared setup into a composite action, or build once and pass artifacts to downstream jobs.

## Duplicate test work on one lane

When two jobs on the same trigger and concrete runner platform execute the same
test command, and one copy sits on a later job's `needs:` chain, keep every gate
and execute the suite once. Within one workflow, make the later job depend on
the existing test job. Across workflows, trigger the consumer with
`workflow_run` and check the producer workflow's conclusion before continuing.
If the second run exists only to collect coverage, move that coverage floor onto
the existing test run.

Expand local composite actions before comparing commands. Matrix legs on
different operating systems or architectures are platform coverage and are not
duplicates. Removing a test job or weakening a required status is never the
fix.

Runner minutes fall by one measured suite run per execution. The cross-workflow
handoff waits for the producer workflow to complete, so its wall-clock credit is
`max(0, duplicate job duration - producer workflow completion after push)`.
A same-workflow `needs:` handoff receives zero wall-clock credit. Measure both
axes from sampled runs.

**GUARDRAIL — "downstream ignores producer's artifact" claims (REQUIRED before any cross-workflow handoff finding):**

If your finding crosses workflow boundaries and frames the fix as
"consumer ignores producer's artifact", "wire up the [producer]
artifact", or "downstream throws away the [producer] output", you
MUST `grep -nE 'actions/upload-artifact'` the producer workflow file
BEFORE accepting the framing. Two valid outcomes:

1. **Producer DOES upload an artifact** → finding is valid as written.
   Cite the upload step's file:line in `evidence`. Fix is "add
   `actions/download-artifact` in the consumer".
2. **Producer does NOT upload an artifact** → reframe the finding.
   The producer's "output" is whatever it actually publishes — most
   commonly a warm Turbo / build-tool remote cache (check for
   `TURBO_CACHE: remote:rw`, `sccache`, `ccache`, `nx affected`,
   etc.) or a PR status update. The fix is then "ADD
   `actions/upload-artifact` to the producer AND add
   `actions/download-artifact` to the consumer" — that is a NEW
   pipeline design, not a re-wiring. Re-size the savings: the
   redundancy is `pnpm install` / `cargo fetch` / equivalent
   install-step cost only, NOT "downstream rebuilds the full
   monorepo from scratch". Use measured P50/P95 of the build step
   to size the cold-tail recovery component; never assume 100% cold.

**Severity cap.** A finding that survives outcome (2) — i.e. the
producer doesn't upload — is capped at HIGH severity, never CRITICAL,
because the saving is bounded by install dedup + measured cold-tail,
not by full rebuild elimination.

**Required `evidence` text:** include one of these two literal lines:
- `Verified <producer-workflow> uploads <artifact-name> at L<N>; consumer <consumer-workflow> does NOT call actions/download-artifact. Wiring fix applies.`
- `Verified <producer-workflow> does NOT upload any artifact (no actions/upload-artifact step). Producer's output is <warm-cache | PR-status | other>. Fix is a NEW pipeline; saving re-sized to <X> min/mo from measured install + cold-tail, NOT 'downstream rebuilds from scratch'.`

A prebuild-dedupe audit that assumed "downstream rebuilds from scratch"
when the producer never actually uploaded an artifact is the empirical
motivation for this guardrail — verify the upload before sizing.

---
