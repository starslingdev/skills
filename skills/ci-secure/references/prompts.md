# ci-secure prompt templates

Verbatim prompt templates used by the orchestrator in SKILL.md. They
live here (not inline in SKILL.md) to keep the launchpad lean; the
phases that use them link here.

## Contents

- [Phase 5 — per-group fix subagent prompt](#phase-5--per-group-fix-subagent-prompt)

---

## Phase 5 — per-group fix subagent prompt

Launched once per selected finding group via the `Agent` tool
(`general-purpose` type, NO worktree isolation — changes go in the
user's working tree). Substitute `{pattern}`, `{severity}`, the
`{occurrences}` list, the per-occurrence `{evidence}`, and
`{fix_recipe_excerpt}` (the section between `### {pattern}` and the
next `### P` heading in `security-patterns.md` — or the next `## `
heading, whichever comes first).

```
You are fixing every occurrence of one security finding (one rule,
one fix recipe, applied across every affected workflow file).

Pattern: {pattern} ({severity})

Occurrences ({N}):
- {workflow_file_1}:{line_1} — jobs: {affected_jobs_1}
- {workflow_file_2}:{line_2} — jobs: {affected_jobs_2}
- ...

Evidence (per occurrence):
- {workflow_file_1}:{line_1}
{evidence_1}
- {workflow_file_2}:{line_2}
{evidence_2}
- ...

Fix recipe (from references/security-patterns.md#{fix_recipe_anchor}):
{fix_recipe_excerpt}

Rules:
- Apply the same recipe to every occurrence listed above. Modify ONLY
  those workflow files; do not touch any other file.
- Fix EVERY occurrence, or explicitly report the ones you did not and why.
  If an occurrence shouldn't get the mechanical fix (e.g. a P14.7 job whose
  cache write the build genuinely needs, so disabling it would break CI —
  namespace the key with a fork-scoped prefix instead), do NOT silently
  leave it — list it as skipped with the reason. A half-fixed group with no
  record is the failure mode to avoid.
- Do not widen the patch beyond what the fix recipe specifies. If you
  see a sibling problem in the same file (a different pattern), mention
  it in your return summary but do not fix it here — it has its own
  separate dispatch.
- Do not commit, push, or open a PR. Leave the changes in the working
  tree for the user to review.
- Preserve the workflow's INTENT, and prove it: after editing, re-read
  the changed block and state in your summary how the guarded behavior
  still triggers on the same conditions (e.g. the confirm gate still
  rejects a wrong string; the pinned installer resolves the same
  toolchain). If the workflow is a deploy/release/publish path or holds
  production credentials, say so and name the safe way to re-verify
  before trusting it (typically a `workflow_dispatch` dry-run) — the
  cost of a wrong edit here is a broken or weakened production path,
  which is worse than the unfixed finding.
- When done, your return summary MUST enumerate, per occurrence,
  `file:line — fixed` or `file:line — skipped: {reason}` (so the
  orchestrator can record a complete, honest `## Fixes applied` entry —
  including any occurrence left unfixed), then one line on what the shared
  fix was, how intent-preservation was checked, and any manual follow-up
  (e.g. re-run the publish workflow via a dry-run, or re-verify a
  re-pinned action SHA against the canonical repo).

Verify (the oracle — you are done only when it passes): re-run the
ci-secure scan over this repo —
`python3 <ci-secure>/scripts/run.py --root . --out /tmp/ci-secure-recheck.json`
— and confirm the chain no longer fires: no finding carrying
`"pattern": "{pattern}"` may remain in that JSON. If one does, occurrences
are still open — fix them and re-run. Report the re-scan result in your
summary; a fix you could not verify this way is reported as unverified,
never as done.

If the recipe is ambiguous, stop and ask. Do not guess.
```
