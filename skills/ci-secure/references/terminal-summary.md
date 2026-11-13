# Composing the terminal summary

Mechanics for [SKILL.md](../SKILL.md) Phase 3. SKILL.md holds the *rules*
(what must be said, and what must never be dressed up as a pass); this file
holds the *provenance* of each line and the exact receipt format. Read it
before printing the summary.

## Contents

- [The three-line header block](#the-three-line-header-block)
- [Where each line comes from](#where-each-line-comes-from)
- [The push-credential rows](#the-push-credential-rows)
- [The vector-map receipt format](#the-vector-map-receipt-format)

---

## The three-line header block

```
CI Secure   3 critical findings  ▏2 of 10 vectors hit▕  12 workflows · impostor check ran
  Impostor-SHA check (P14.11): ran — 14 unique pins verified, 0 flagged
  Coverage: complete
```

The first line is the report's own banner, pre-drawn by `report.py` inside a
fenced block immediately under the provenance table. Copy it verbatim; never
redraw, re-count or reformat it. The other two are assembled. There is no
fourth HEADER line — SKILL.md Phase 3's mandatory contract lines (the hygiene
clause, the ten-row vector receipt, the dormancy note) follow this block and
are not optional.

## Where each line comes from

| Line | Where it comes from |
| --- | --- |
| `CI Secure …` | **Pre-drawn — copy, never compose.** `grep '^CI Secure' "$REPORT"`. Rendered inside a fenced block under the provenance table with its counts already computed. |
| `Impostor-SHA check (P14.11): …` | **Assembled** from the **banner's** own impostor-check word (`ran` / `partial` / `SKIPPED` / `not recorded` — the impostor word ENDS the pre-drawn line, and it is two words in the `not recorded` state, so read to the end of the line rather than taking the last token) plus the `gh_checks["P14.11"]` status/detail in `$FINDINGS` (the pin counts — "14 unique pins verified, 0 flagged", or the UNVERIFIED count on a partial — live there, NOT in any report row). On a run that did NOT fully complete, add the reason from the report's `> [!WARNING]` gh-checks blockquote (or the ⚠️ `P14.11` vector-map row). Do NOT read the pin counts off the vector-map row: when the check ran clean that row is a generic ✅ "no match" like every other clean vector and carries none of them. Always say `PARTIAL … NOT a pass` / `SKIPPED … this check did NOT run` verbatim when it did not run. Then read the `action_lockfile` dict the same `$FINDINGS` one-liner prints on its second line (the JSON, not the report, is the source): when `state` is `valid` and `covered_references` is above 0, append `; N action reference(s) in M workflow(s) locked by actions.lock are verified by GitHub at run time, not by this check` (N = `covered_references`, M = the length of `covered_workflows`); when `state` is `invalid`, append `; <path> is not a lockfile this scan can read, so no reference is counted as locked`; when `absent`, append nothing. |
| `Coverage: …` | **Assembled** from the **Coverage** ROW of the provenance table at the top of the report — NOT from any sentence under the banner, where nothing of the kind is rendered. The row reads `✅ complete — every workflow file was scanned` or `⚠️ **PARTIAL** — not every workflow was fully scanned`, and on PARTIAL it does **not** say what was missed: that lives in the separate `> [!WARNING] **Incomplete coverage — …**` blockquote further down, and your line must carry it. |

**P14.24's release-immutability lookup is not a header line.** It is
recorded in `gh_checks["P14.24"]` only when the scan reached a GitHub CLI
extension pinned to a tag and then run, and it can only remove a finding.
The Phase 3 `$FINDINGS` one-liner prints it on its third line (empty when
the key is absent). When that status is `skipped` or `partial`, say so in
one clause where the summary names the P14.24 finding(s):
`release-immutability lookup did not run (<reason>), so tag-pinned
extensions are reported as mutable`, adding `; sign in to gh and re-run to
check them` when the reason is gh sign-in or `--gh-impostor=off`. Never
call this a missing finding, and never word it as `NOT a pass`: every
tag-pinned finding is still listed.

The extraction commands are in SKILL.md Phase 3. Run them as written; do not
re-derive them here. The one to be careful with is the P14.11 vector-map row:
the id renders in backticks with **no space before it**, so a pattern
expecting `| ` immediately ahead of the id matches nothing and silently drops
the line.

## The push-credential rows

The report's hygiene section ends with a `### If a push credential leaks`
sub-heading holding three hygiene rows (`sec.environments.branch-restricted`,
`sec.environments.approval-holds`, `sec.secrets.environment-scoped`). They
are hygiene checks like the rest: name a failed one in the hygiene clause, in
one short clause each (`3 environments accept a branch any push credential
can make`, `3 environments let the requester approve their own run`,
`any workflow pushed to any branch can read 11 secrets`), and an unmeasured one
as unmeasured with its reason — never `0 secrets` over a list that could not
be read. Say what must be done in plain words, as the row does; there is no
prompt to hand to another agent. Never claim these rows would have stopped
an attack by an admin or org owner; they do not, and the report does not say
so either.

**When `sec.secrets.environment-scoped` fails, it leads the hygiene clause.**
The report then carries a `> [!CAUTION]` **Secrets reachable from any
branch** callout right under the headline; take the count from it and say
the consequence, not the bookkeeping: `Hygiene: any workflow pushed to any
branch can read 11 secrets, so one leaked push credential exposes them all
with no review and no merge; move them into an environment only protected
branches can use (one that passes the branch-restriction check).` When the
callout says "at least", say "at least 11" too. Name secrets only as the report names them (names, never values),
and never call this an attack-vector hit: the vector count is unchanged. No
callout means the row did not fail; an unmeasured row is still said as
unmeasured, never as this sentence.

## The vector-map receipt format

Derive each line from the report's "Vector map" rows with links and anchors
stripped: NUMBERED 1–10 in the report's row order, one per vector, catalog id
included, numbers **right-aligned** — pad rows 1–9 with a leading space so
the periods line up under `10.`:

```
Vector scan — 10 attack vectors checked, 1 hit:
  1. ✅ P14.10 Template Injection in run: Blocks — no match in 3 workflows
  2. 🟥 P14.9 Fork code executed with privileges — 2 sites across 2 workflows → Finding 1
 …
 10. ⚠️ P14.11 Impostor / unreachable SHA — SKIPPED (gh not authenticated); this check did NOT run
```

Icons: ✅ evaluated-clean; 🟥 a HIT whose group severity is HIGH; 🟧 a HIT
whose group severity is MEDIUM, each with its site count (`2 sites across 2
workflows`) and its `→ Finding N` tag; ⚠️ did-not-run, which keeps its reason
and is never promoted to ✅.

Plain text only, in the receipt and in the question text that carries it — no
`**bold**`, no headings, no code fences. The question UI supplies its own
emphasis, and bolding the whole block made a close unreadable.
