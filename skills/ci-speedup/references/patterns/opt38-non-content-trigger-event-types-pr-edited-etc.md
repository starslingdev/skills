---
pattern: OPT38
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: non-content-trigger-event-types-pr-edited-etc
title_template: 'Non-Content Trigger Event Types (PR `edited`, etc.)'
category: 7
---

# OPT38 — Non-Content Trigger Event Types (PR `edited`, etc.)

**Anti-pattern**: A workflow's `on.pull_request.types` includes events that don't change the code under test, causing the full workflow to re-run on metadata-only changes. The most common offenders:

- `edited` — fires when the PR title, description, or base branch is edited. Title typo fix → full CI re-run.
- `labeled` / `unlabeled` — fires when any label is added/removed. Most workflows don't gate on labels and re-run for nothing.
- `assigned` / `unassigned`, `review_requested`, `review_request_removed` — same story.
- `ready_for_review` — legitimate (draft → ready transition); KEEP this one if you want CI to start when a draft becomes ready.

The default `pull_request` types are `[opened, synchronize, reopened]` — these are the content-change events. Anything beyond that needs justification.

**Detection heuristic**:

```bash
# Find workflows that override pull_request.types
grep -rA3 'pull_request:' .github/workflows/ | grep -E 'types:.*edited|types:.*labeled'
```

For each hit:

1. Confirm the `types:` array includes one of the non-content events.
2. Cross-reference the workflow body for any `if: github.event.action == 'labeled'` (or similar) — if present, the trigger is intentional.
3. Inspect what the workflow reads before calling a re-run a no-op. List every `uses:` target and every script the jobs run, and look for a dependency on the PR title, description, labels, base branch, or other PR metadata, with or without an `if:` guard. A PR-title validator such as `amannn/action-semantic-pull-request`, a label-driven check, or a description linter reads that metadata internally, so its job needs the matching event type to run again. Keep every event type needed to rerun those checks.
4. Only when that inspection finds no metadata dependency, the metadata events trigger a full no-op re-run.
5. Cost = full workflow P50 × estimated frequency of metadata edits (typically 5-20% of PR activity).

**Fix**: Restrict `types:` to content-change events:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
```

Or simply omit `types:` to use the default (which already excludes `edited`).

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` and several other workflows would re-run on PR title/body edits because of an inherited `types:` array including `edited`. Fix is a one-line YAML change.

**Risk**: LOW once step 3 finds no metadata dependency; not LOW before it. An `if:` guard is not the only sign a workflow needs the event. Two cases need it with no guard at all:

- An action or script that reads PR metadata internally (a PR-title validator, a label-driven check, a description linter). Dropping `edited` or `labeled` leaves a corrected title failing, or an invalid one passing, on a stale result.
- `edited` also fires when the pull request's base branch changes. Dropping it means CI does not run again after a retarget, so a PR can show green against its old base.

---
