---
pattern: OPT110
impact: MEDIUM
class: static
detector: yaml-cross-workflow
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: remove-or-reuse-unused-artifact
title_template: 'Uploaded artifact has no repository consumer (unverified externally)'
category: 2
---

# OPT110 — Uploaded Artifact Has No Repository Consumer

**Anti-pattern**: A job packs and uploads a build output or compiled worker that
no job downloads and no release or deploy step consumes. The upload time,
retention storage and repeated packaging are pure cost when no human or
external system uses the artifact either.

**Detection heuristic**: Index each literal `actions/upload-artifact` name
across the repository. Match it against literal `actions/download-artifact`
names and patterns, `gh run download` selectors, and uploaded paths referenced
by release or deploy steps. A download without a selector consumes all names.
Withhold the finding when a download selector is dynamic or unreadable. The
repository scan cannot see a human download or an external API consumer, so a
remaining finding is explicitly unverified outside the repository.

**Fix recipe (safest first)**:

1. Search workflow history, release automation and owner documentation for the
   artifact name and path. Confirm whether a person or external system still
   depends on it and whether its retention is required for diagnostics.
2. If there is no consumer, remove the upload and any packaging done only for
   it. Preserve every test, build, diagnostic and release step.
3. If another job rebuilds the same output, keep one producer and reuse its
   artifact there instead (OPT14). Prove the consumer validates the same source,
   dependency and toolchain inputs before deleting its local build.

**Sizing**: Runner-minute effect is a **saving**: the upload step's measured
billed compute, plus packaging time performed only for that upload. Do not
state a number until those steps are timed. Storage reduction is real but is a
separate retention quantity, not runner-minute saving. Removing an off-path
upload normally does not shorten pull-request merge wait.

**Public evidence**: [OpenClaw pull request #154095](https://github.com/openclaw/openclaw/pull/154095)
removed two build-archive packing and upload paths after confirming no
repository job consumed them, while retaining diagnostic, proof and release
artifacts that still had consumers.

**Risk**: MEDIUM. A repository-local search cannot disprove human downloads or
external API use. Removing a diagnostic or release artifact can break incident
response or delivery without making CI red, so confirm those consumers first.
