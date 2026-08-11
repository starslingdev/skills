---
name: skills-registry-security
description: >-
  Checks the third-party security audit status of a skill published to a skills
  registry (skills.sh), across every cached surface at once, and decides whether
  a failing finding is real or points at content that was already removed. Runs
  a real install to capture what users actually see, reads the per-provider
  finding codes the JSON API does not expose, and greps each flagged literal
  against the repository and the shipped tree. Use when a published skill shows
  a FAIL or CRITICAL security audit, when an audit looks stale or predates a fix,
  when someone asks why a registry badge disagrees with a merged change, when
  deciding whether to request a re-index, or before or after publishing a skill
  that has been flagged. Also use when a Snyk, Socket, or Gen Agent Trust Hub
  verdict on a published skill needs to be explained or disputed.
---

# Registry security status for a published skill

A registry stores a skill's audit in several places that drift apart, and the
verdict a user sees at install time is not the one the JSON API returns. Worse,
a re-audit can run against a stored snapshot rather than the current default
branch, so a finding can survive long after the code it describes is deleted.
Answering "is this failure real?" therefore means reading four surfaces and then
proving, by grep, whether the flagged string still exists.

`scripts/registry_audit.py` does all of that concurrently. Run it first; read
the rest of this file only when the output needs interpreting.

## Quick start

```bash
python3 maintainers/skills-registry-security/scripts/registry_audit.py \
    starslingdev/skills/ci-secure
```

Roughly 40 seconds, most of it the install. Add `--no-install` for a ~3 second
check when only the cached verdicts matter. `--json` emits the same data
machine-readably; `--ref` picks the git ref to verify literals against
(default `origin/main`).

The script is read-only: HTTP GETs, a throwaway install into a temp directory,
and greps over a local checkout. It never POSTs to the registry and never
writes to the repository.

## What it reports, and how to read it

The output has five parts. Read them in this order, because each one narrows
the question:

1. **Provider status** — status, risk level, and `auditedAt` per provider from
   the JSON API. If every `auditedAt` predates the fix you are checking, the
   audit simply has not re-run yet and nothing else matters.
2. **Install-path cache** — the separate endpoint the CLI reads. This is what
   users see. It has lagged the API by a full day.
3. **Fresh install output** — the verbatim risk table the CLI prints.
4. **Findings** — the actual codes (`E005`, `W011`, …) scraped from the
   per-provider pages, plus every literal each finding quotes, each tagged
   `present` or `PHANTOM`.
5. **Verdict** — whether any flagged literal is absent from both the git ref and
   the freshly installed tree.

A literal tagged `PHANTOM` is the finding worth escalating: the scanner is
objecting to a string that exists in neither the default branch nor the shipped
skill, which can only happen if the scan input is stale.

## Deciding what to do

The output separates three cases that need very different responses. Getting
this wrong wastes days, so decide deliberately:

**The finding is real.** The literal is `present`. The scanner is describing
today's content. Fix it or consciously accept it — a re-scan will keep
reporting it, correctly. Warning-class codes are frequently accurate
descriptions of what a skill legitimately does, and accepting one with a
written rationale is a valid outcome.

**The scan input is stale.** A literal is `PHANTOM`. There is nothing to patch.
Requesting a plain re-audit is the trap here: re-auditing the same stored
snapshot reproduces the same finding. Ask for a **re-index** — refreshing the
stored snapshot — and say so explicitly, because the two are different
operations and only one of them helps.

**The surfaces disagree.** The reconciliation section fires. The underlying
audit may be fine while users still see a stale badge. This resolves on its own
as caches catch up; confirm by re-running rather than by acting.

To make a stale-input case airtight, gather the three pieces of evidence a
maintainer can verify without your repository: the finding code and the exact
literal it quotes, the commit that removed that literal with its timestamp, and
the `auditedAt` timestamp showing the scan ran *after* that commit. If the
project runs the same scanner in its own CI, a passing run on the current
default branch is the strongest single exhibit — same scanner, opposite verdict.

## Gotchas

These are the traps that make this task take hours instead of a minute. Most of
them look like a bug in your own reasoning when you hit them cold.

- **Installing never triggers a re-scan.** The CLI's audit call is a plain GET
  keyed on `owner/repo` plus the skill slug, with no commit SHA anywhere in it.
  Installing repeatedly, waiting, and re-installing accomplishes nothing.
  Polling on a timer is wasted effort — check once, then act.

- **Merging a fix does not invalidate anything.** Because the cache key carries
  no SHA, pushing the fix to the default branch leaves the audit untouched.
  Expect no change from a merge alone.

- **The JSON API does not carry finding codes.** It returns only a summary
  string like `Risk: CRITICAL · 2 issues`. The codes and the quoted literals
  live exclusively on the per-provider HTML pages. Any workflow that reads only
  the API cannot tell a real finding from a phantom one.

- **Timestamps come in two formats on the same payload** — some providers emit
  `...Z`, others `...+00:00`. Compare parsed datetimes, never strings.

- **A re-audit is not a re-index.** A refresh can re-run the scanner against the
  stored snapshot, producing a brand-new `auditedAt` on a finding derived from
  old content. A fresh timestamp is therefore not evidence that current code was
  read.

- **Warning-class and critical-class codes are different animals.** A single
  critical code drives the whole verdict; warning codes usually describe
  inherent behavior and often should be accepted rather than chased.

- **Check the sibling skills too.** Registry sweeps have re-audited the skills
  that did not change while skipping the one that did. Comparing timestamps
  across every skill in the same repository exposes that inversion, and it is
  strong evidence for an escalation.

- **A finding can be about a test fixture.** Vendored third-party workflows and
  deliberately malicious-looking fixtures get scanned like real code. These are
  usually false positives, but they are genuinely present in the tree, so the
  script reports them as `present` — that tag means "really there", not
  "really a problem".

- **A URL whose host ends in `.sh` is not a shell script.** Rules about download
  URLs key on the *path* ending in `.sh`, `.ps1`, or `.bash`. A documentation
  link to a `.sh` domain is a false alarm when triaging by hand.

- **Install counters do not reflect your installs.** They appear deduplicated,
  so a rising count is not confirmation that an install registered.

## Deeper reference

`references/registry-surfaces.md` documents each surface's exact URL shape and
response schema, and the finding-code classes. Read it when the script's parsing
breaks — for example after a registry redesign changes the page markup — or when
adding a provider.
