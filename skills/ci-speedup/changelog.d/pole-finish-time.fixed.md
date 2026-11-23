- **2026-11-23** — The long-pole ranking and the report's "finishes last on N/M PRs" counts now
  follow each sampled PR's real finish order: a check counts at its measured job time (start to
  finish, no queue) plus the time it started after the PR's first check, instead of the longest
  span capped at the job's p50. Only a run above that check's own P95 is clipped, and a re-run
  that started after every other check had finished sits out that PR. Every merge-gating check
  competes, so a PR that a check the report cannot confirm as merge-blocking finishes last on is
  counted for that check, not the next one. A job that waits on a slow setup job, or on whatever
  a short collecting job waits on, now leads the long poles when it is what finishes last. The
  findings carry the per-PR basis as `pr_critical_path.pole_basis`, and `verify_report`
  re-derives every count from it (#491).
