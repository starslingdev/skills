# Changelog

All notable changes to the `sling` skill. Unversioned; dated (UTC).

## [Unreleased]

### Added

- **2026-08-25** — **Initial skill.** Routes GitHub Actions questions between
  `sling` (StarSling's read-only, agent-first CLI) and `gh`, so an agent stops
  defaulting to whichever tool it already knows. Ships the routing table, the
  `sling doctor` / `sling login` preflight, the `--agent` machine-mode
  contract, the exit-code recovery table, and the handoff rule that sends
  repo-wide audits to `ci-score`, `ci-speedup` and `ci-secure` instead of
  looping `sling` over every run.
- **2026-08-25** — **A command reference read off the binary, not the docs**
  ([`references/command-reference.md`](references/command-reference.md)). Every
  flag list and JSON shape was captured by running `sling` v0.1.2 with
  `--agent` against a live control plane. Three places where the published
  documentation and the binary disagree are recorded with the binary as the
  answer, the largest being `sling logs`: the docs describe it as the one
  command that streams raw text in both modes, and under `--agent` it in fact
  returns the same structured JSON as everything else, so an agent can hand
  every command's stdout to one parser.
- **2026-08-25** — **A pinned capture of the command surface**
  ([`references/command-surface.json`](references/command-surface.json)) and
  the tests that hold the skill to it. The skill exists to stop an agent
  inventing a `sling` subcommand, so its own routing table is guarded against
  doing the same; the guard has teeth because a wrong command name really does
  survive being printed by the tool — `sling doctor` on v0.1.2 suggests
  `sling update` as its fix, and the binary rejects `sling update` as unknown.
  The reference says so rather than passing the suggestion along.

- **2026-08-25** — **A Gotchas section, an eval set, and a contents block**,
  from a pass against the official skill-authoring best practices and the
  internal authoring guide. The Gotchas section collects the places where
  `sling` behaves differently from what its own `--help`, its output, or its
  documentation implies — the highest-signal content in a skill about driving
  a CLI, and previously scattered through the body.
  [`evals/evals.json`](evals/evals.json) adds five behavioral scenarios,
  including the two the skill must DECLINE, so a later change cannot quietly
  turn the handoff rule into an approximation. The 300-line command reference
  gained a contents block, since long reference files get previewed with
  partial reads.

### Fixed

- **2026-08-25** — **The output-stream rule was wrong for the two exit codes
  that are not errors, and it broke the skill's own preflight.** The `--agent`
  section said a non-zero exit means stdout is empty and stderr carries the
  message. Verified against the binary: `sling doctor --agent` on an
  unreachable control plane exits `10` and writes every check to stdout with
  stderr empty. An agent following the old rule would have skipped that JSON
  and been unable to do what the preflight asks two paragraphs later — act on
  the check that is `ok: false` — and would have discarded every partial
  `why` / `time` result at exit `6`. The rule is now: parse stdout first
  whatever the exit code, and fall back to stderr only when stdout is empty.
- **2026-08-25** — **"Pass `--agent` on every invocation" made authentication
  unreachable.** `--agent` implies `--no-input`, so it turns a prompt into
  exit `2`. Following the auth recovery — "`sling login`, then retry once" —
  with `--agent` attached refuses the device-code prompt, lands on the usage
  row, and loops without ever signing in. `--agent` is now scoped to data
  commands, with the two interactive ones called out.
- **2026-08-25** — **Signing in is a human step, and the skill now says so
  everywhere.** `sling login` prints a device code, opens a browser approval
  page, and blocks until a person approves it. An agent cannot do that: as a
  subprocess the output arrives only once the command has finished or timed
  out, so the code is never visible while it is still usable, and `--agent`
  (which carries `--no-input`) refuses the prompt outright with exit `2`.
  The preflight now asks the user to run it in their own terminal and
  confirms with `doctor` afterwards; the exit-`4` recovery row and the
  command reference say the same, and no shipped example pairs `sling login`
  with `--agent`. Two tests pin it, because this is the failure that leaves a
  user signed out and the session hung.
- **2026-08-25** — **The invention guard missed invented subcommands under
  real commands, and never looked at the description.** It reported only the
  first word when a pair was unknown, so `sling bill export` and `sling logs
  tail` passed because `bill` and `logs` are real — the exact class it exists
  to catch. It also scanned only SKILL.md's body, and the frontmatter
  description was at that moment shipping `sling runs`, which is not a command
  (only `runs list` / `runs show` are). The detector now reads command
  mentions out of code spans rather than prose, and the description and every
  shipped reference are scanned alongside the body.
- **2026-08-25** — **`gh run delete` was in the fallback reference but missing
  from the routing table**, and the two mutating-verb guards disagreed about
  whether `trigger` counts as a state change. Both now derive from one list.
- **2026-08-25** — **`--org` was documented as both taking and not taking a
  value.** Because malformed flags are ignored rather than rejected, `sling
  usage --org --window 7d` exits `0` having eaten `--window` as the org slug
  and returns a wrongly-scoped cost answer that looks correct.

- **2026-08-25** — **The `sling update` gotcha is now dated to the release it
  applies to.** It is real on 0.1.2, which is what the installer serves, and
  already fixed on the CLI's main branch — where the same check emits the
  installer one-liner. Saying so keeps the note from outliving the bug and
  teaching users to distrust a `fix_command` that has become correct.

- **2026-08-25** — **The precondition the skill never checked: the StarSling
  GitHub App.** `sling` reports CI that the app collected, so a perfectly valid
  login sees nothing for an org the app was never installed on. It surfaces as
  exit `4` with `You don't have access to org "<name>"`, and the CLI's own
  message ends `Run \`sling login\`` — which cannot work, because the
  credential was never the problem. The skill's exit-`4` row said the same
  thing, so an agent following it would have looped on login exactly as the
  misleading message invites. It now reads the stderr before acting, and the
  preflight carries a **StarSling gate** in the shape ci-speedup's gh gate
  already uses: stop, say what is unavailable in the user's own terms, give the
  install path, and degrade only if they say so. Personal repositories are
  named as unsupported, since the app installs there but StarSling never picks
  up the jobs.
- **2026-08-25** — **An empty listing is a coverage hole, not a finding.**
  `sling runs list --repo <name>` returns `{"runs": []}` at exit `0` both for a
  repo with no runs in the window and for one StarSling is not watching, and
  the payload cannot distinguish them — so reporting "you have no CI runs"
  turns a missing installation into a false statement about the user's repo.
  Adopts ci-secure's rule for a check that could not run: an absent result is a
  coverage hole, never a pass.

- **2026-08-25** — **The `gh` half of the routing table gets the same gate the
  `sling` half has**, reusing ci-speedup's rather than a thinner re-derivation.
  It previously carried a one-line aside about `gh auth status`, which missed
  three things that pattern already encodes: whether `gh` is **installed** at
  all (a different failure with the same consequence), the concrete path
  (https://cli.github.com, then `gh auth login`), and the caution ci-speedup
  recorded from a live miss — a sandboxed agent shell cannot reach keyring
  credentials, so one failed probe is not proof and must never be reported as
  an expired login. The degraded path when `sling` is unavailable now runs that
  gate before promising `gh`, since with neither CLI there is nothing left to
  fall back to.

### Notes

- **The install command is described, not printed.** `sling` installs by
  fetching and running a shell script, and a literal of that shape in shipped
  skill text is what two registry security scanners rate CRITICAL — the reason
  the repo carries a guard against it. The preflight points at the
  installation page instead of pasting the command.
- **No Windows build exists** (Apple Silicon macOS and x64 glibc Linux only),
  so the preflight says so plainly and falls back to the `gh` read path there.
