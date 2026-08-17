# ci-secure — maintainer notes

**Maintainers only.** This file is a sibling of the installable
`skills/ci-secure/` tree; the `skills` CLI never copies `maintainers/` into an
end-user install. `tests/test_ci_secure_install_surface.py` makes that boundary
a PASS/FAIL invariant, and it already expects maintainer material to land here
rather than under the skill.

## Contents

- [Changing the catalog](#changing-the-catalog): the admission test, and what
  must move with a pattern
- [Removing a pattern](#removing-a-pattern)
- [Third-party findings and disclosure](#third-party-findings-and-disclosure):
  the rule for scans of repos we do not own
- [Choosing a public example target](#choosing-a-public-example-target)
- [Tests that govern this skill](#tests-that-govern-this-skill)

## Changing the catalog

The catalog is a **closed set of ten**, and that number is a promise the skill
makes to every reader: `references/why-these-ten.md` ships with the skill so any
finding can answer "why is this one of only ten?". Adding an eleventh is a
product decision, not a code change.

A candidate pattern is admitted **only if it passes all three tests** in
`references/why-these-ten.md`:

1. **The outsider-chain test.** Someone with no access to the repo reaches a
   concrete compromise: code execution holding a write token, secret theft, or
   poisoning of what the repo ships. Blast radius does not qualify. "This makes
   a breach worse" is a different class and belongs in the rejection record.
2. **The incident test.** The vector class has actually happened in public, and
   the entry names its incidents. Nothing theoretical.
3. **The same-day-fix test.** A maintainer can close it the day they read it.
   Anything needing a hardening program fails.

Severity is not the filter and never has been. Three catalog-HIGH patterns
failed these tests; two catalog-MEDIUM patterns passed. Reaching for "but it's
HIGH" as an argument means the case has not been made.

**A pattern never travels alone.** Admitting or changing one touches, at
minimum:

- `skills/ci-secure/references/security-patterns.md` — the entry, its
  `METADATA` block, and the five prose sections every entry must carry:
  `**TL;DR.**`, `**What an attacker can do.**`, `**Anti-pattern**:`,
  `**Fix recipe**`, `**Risk of the change.**`
- `skills/ci-secure/references/why-these-ten.md` — the entry in the ten, its
  incident grounding, and the census arithmetic in the rejection record
- `skills/ci-secure/scripts/scan.py` — the detector
- `skills/ci-secure/tests/` — detector tests, and the census tests below
- `skills/ci-secure/CHANGELOG.md` — a dated entry, same PR

The census tests bind these together on purpose: the catalog, the doc, the
scanner's active pattern set, and the report manifest cannot drift apart. If a
census test fails, the fix is almost never to relax the test.

## Removing a pattern

Removal has the same weight as admission. The rejection record in
`references/why-these-ten.md` must account for it, and
`test_the_rejection_record_accounts_for_the_catalog_size_it_claims` checks that
the arithmetic still adds up. `test_no_shipped_doc_cites_a_removed_pattern`
catches the dangling references a removal leaves behind.

Fifteen patterns have already been removed, in two classes the record names
and both worth reading before proposing a sixteenth: the blast-radius patterns
(a real weakness that only makes an existing breach worse), and the
presence-shaped hygiene observations, which either became scored config facts
in ci-score or were dropped.

## Third-party findings and disclosure

**A ci-secure run against a repository we do not own can surface a live,
undisclosed vulnerability in someone else's code.** That output is sensitive
until its owner has had a chance to fix it, and we are the ones who generated
it.

The rule:

- **Never publish a finding against a third-party repo that its owner has not
  already disclosed or fixed.** Not in `examples/`, not in a report committed
  to this repo, not in a PR body, an issue, a screenshot, a blog post, or a
  social post. A committed file is worse than a post: it is permanent and
  indexed.
- **Report it privately first.** Use the project's own security policy or
  bug-bounty program. Give them the file, the line, and the attacker scenario
  the skill already produced.
- **Scope the scan when the target is a demonstration.** If the point is to show
  one already-public bug, run against that one workflow file rather than the
  whole repository, so an unrelated live finding cannot end up in the artifact.
- **A scan of our own repositories has no such constraint**, and dogfooding
  ci-secure on this repo is encouraged.

This applies to anything derived from a run, including a report a loop or an
agent generated automatically. Read what you are about to commit.

## Choosing a public example target

`examples/` ships no ci-secure worked example yet. When one is added, its
target has to be a repository whose findings are safe to publish permanently.
In order of preference:

1. **A repository we own.** No disclosure question at all.
2. **A historical commit of a third-party repo whose vulnerability is already
   public and already fixed**, cited to the published disclosure. The bug is
   public knowledge, the current code is not affected, and the before/after pair
   across the owner's own fix is the most persuasive artifact the skill can
   produce: it shows the detector is specific, not merely noisy.
3. **A current third-party repo.** Only with a clean scan, and only after
   confirming nothing undisclosed is in the output.

Scope option 2 to the affected file. A whole-repo scan of the same commit can
surface unrelated findings that are still live today, which is exactly the case
the rule above exists for.

## Tests that govern this skill

From the repo root, `python3 -m pytest -q` runs everything. The suites that
specifically constrain catalog changes:

- `skills/ci-secure/tests/test_census_why_these_ten.py` — binds the catalog, the
  doc, the scanner's pattern set, the report manifest, anchors, severities,
  platform notes, and the rejection-record arithmetic to each other
- `skills/ci-secure/tests/test_scan.py` — per-detector positive and negative
  cases
- `skills/ci-secure/tests/verify_report.py` (the checker) and
  `skills/ci-secure/tests/test_verify_report.py` (its tests) — the report's own
  invariants, including that a source fence quotes only source and that the
  rendered vector-status table covers all ten
- `tests/test_ci_secure_install_surface.py` — fails if maintainer-only material
  appears under `skills/ci-secure/`

A guard that cannot fail is not a guard: several of these carry positive-control
tests asserting the detector actually fires. Keep that property when adding one.
