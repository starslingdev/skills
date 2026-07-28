# Security

## Reporting a vulnerability

Please report suspected security vulnerabilities through GitHub's **private
vulnerability reporting**: go to the **Security** tab of this repository and
choose **Report a vulnerability**. This opens a private advisory visible only to
the maintainers — please do not open a public issue for a security report.

Include enough detail to reproduce (affected script/version, inputs, and observed
vs expected behavior). We'll acknowledge your report and keep you updated as we
investigate.

## Data-handling model

`ci-speedup` is designed to run **locally, under your own GitHub credentials**,
and to keep your data on your machine.

- **Runs with your own `gh` auth.** The skill reads the audited repository's
  GitHub Actions run/job/log data and workflow YAML through a fixed set of
  **read-only, enumerated `gh` API calls**, using the `gh` CLI you have already
  authenticated. It uses no credentials of its own.
- **Never modifies your repo's contents, and never commits or pushes.** The
  critical path and the findings are derived in-process and stored **locally**:
  a `findings.json` (plus a raw drill-log bundle) written to a scratch path
  outside your checkout. The one file it can create in your working directory is
  the sanitized report (`ci-speedup-findings-report.md`), and only when you pick
  "Save the full report"; it is an untracked, generated file you can gitignore
  or delete. If you ask the skill to implement a fix, you review the change
  before anything is committed.
- **No telemetry — nothing is sent to StarSling.** The skill reports no run data,
  finding, or metric anywhere. Data leaves your machine in exactly two ways, both
  of them yours: the read-only `gh` calls to GitHub, and — only when a drilled
  pole matches no catalog detector — the job-log excerpt your own agent reads to
  write the gap-fill analysis. Nothing else is transmitted.

### Log data handling

Job logs and workflow YAML are **third-party untrusted data**, and the report
quotes them verbatim as evidence — so three layers apply, in order:

- **GitHub's own secret masking** is the first layer: values registered as repo
  or org secrets arrive already masked in the log the skill reads.
- **Instruction level.** The skill's prompts (`SKILL.md` phase 4a,
  `references/gap-fill.md`) treat log content as data, never as instructions: it
  is quoted as evidence, and directives embedded in it are never followed.
- **Render boundary.** Every verbatim line passes through one sink
  (`blocking_path._fence_safe`) that defuses Markdown fence breakouts and
  deterministically masks credential-shaped strings — GitHub tokens, AWS access
  keys, Slack tokens, JWTs, Google API keys, private-key headers, and
  `key=value` credential assignments — as `[REDACTED:<kind>]`. This catches
  tokens that were echoed into a log without being registered as secrets.
