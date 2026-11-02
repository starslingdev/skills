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

**TL;DR**: Several jobs copy-paste the same setup steps. Pulling them into one shared action is easier to maintain — but note it does NOT speed anything up (each job still runs them).

**Anti-pattern**: Multiple jobs with identical preamble steps (checkout, setup-node, pnpm install, build) that could be extracted to a composite action or use artifact handoff.

**Detection heuristic**:

```bash
# Hash the first N steps of each job and compare
# Look for identical sequences of: checkout → setup-pnpm → setup-node → install → build
```

**Fix**: Extract shared setup into a composite action, or build once and pass artifacts to downstream jobs.

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
