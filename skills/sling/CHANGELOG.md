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

### Notes

- **The install command is described, not printed.** `sling` installs by
  fetching and running a shell script, and a literal of that shape in shipped
  skill text is what two registry security scanners rate CRITICAL — the reason
  the repo carries a guard against it. The preflight points at the
  installation page instead of pasting the command.
- **No Windows build exists** (Apple Silicon macOS and x64 glibc Linux only),
  so the preflight says so plainly and falls back to the `gh` read path there.
