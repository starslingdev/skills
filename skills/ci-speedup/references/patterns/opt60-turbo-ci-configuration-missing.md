---
pattern: OPT60
impact: LOW
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-ci-configuration-missing
title_template: 'Turbo CI Configuration Missing'
category: 11
---

# OPT60 — Turbo CI Configuration Missing

**TL;DR**: Turbo is missing a couple of CI-tuning settings that cut log noise and rendering overhead. Minor.

**Anti-pattern**: Turbo used in CI without CI-specific configuration flags, causing unnecessary overhead or noisy logs.

Missing settings include:

- `"ui": "stream"` (root-level) — Avoids interactive TUI rendering overhead in non-interactive CI environments. Note: `"stream"` is the default in recent Turbo versions — check via Context7 whether the project's Turbo version already defaults to stream before flagging.
- `"outputLogs": "new-only"` (**task-level**, not root-level) — Suppresses replayed cache-hit logs, reducing log noise and storage. Valid values: `full` (default), `hash-only`, `new-only`, `errors-only`, `none`. Applied per-task in the `tasks` block.
- `"futureFlags": { "affectedUsingTaskInputs": true }` (root-level) — Enables more precise `--affected` filtering using task-level `inputs` rather than package-level change detection.

**Detection heuristic**:

```bash
# Check root turbo.json for CI-relevant settings
if [ -f /tmp/turbo.json ]; then
  ui=$(cat /tmp/turbo.json | jq -r '.ui // "not set"')
  futureFlags=$(cat /tmp/turbo.json | jq -r '.futureFlags // "not set"')
  [ "$ui" = "not set" ] && echo "OPT60: turbo.json missing ui (check if Turbo version defaults to stream)"
  [ "$futureFlags" = "not set" ] && echo "OPT60: turbo.json missing futureFlags (affectedUsingTaskInputs)"

  # Check tasks for outputLogs (task-level setting, not root-level)
  cat /tmp/turbo.json | jq -r '
    .tasks // .pipeline | to_entries[] |
    select(.value.cache != false) |
    select(.value.outputLogs == null) |
    .key
  ' | while read task; do
    echo "OPT60: task '$task' missing outputLogs (defaults to full — consider new-only for CI)"
  done
fi
```

**Fix**: Add root-level settings and per-task `outputLogs`:

```json
{
  "ui": "stream",
  "futureFlags": { "affectedUsingTaskInputs": true },
  "tasks": {
    "build": {
      "outputLogs": "new-only"
    }
  }
}
```

Note: `ui` can also be set via `TURBO_UI=stream` env var in CI workflows. `outputLogs` can be overridden per-run with `--output-logs` CLI flag. Verify `futureFlags` compatibility with the project's turbo version via Context7.

**Real-world example (mastra)**: PR #14432 added `ui: "stream"` and `futureFlags` at root level, and `outputLogs: "new-only"` on the `build` task.

