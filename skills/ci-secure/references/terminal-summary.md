# Composing the terminal summary

Mechanics for [SKILL.md](../SKILL.md) Phase 3. SKILL.md holds the *rules*
(what must be said, and what must never be dressed up as a pass); this file
holds the *provenance* of each line and the exact receipt format. Read it
before printing the summary.

## Contents

- [The three-line header block](#the-three-line-header-block)
- [Where each line comes from](#where-each-line-comes-from)
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
| `Impostor-SHA check (P14.11): …` | **Assembled** from the **banner's** own impostor-check word (`ran` / `partial` / `SKIPPED` / `not recorded` — the impostor word ENDS the pre-drawn line, and it is two words in the `not recorded` state, so read to the end of the line rather than taking the last token) plus the `gh_checks["P14.11"]` status/detail in `$FINDINGS` (the pin counts — "14 unique pins verified, 0 flagged", or the UNVERIFIED count on a partial — live there, NOT in any report row). On a run that did NOT fully complete, add the reason from the report's `> [!WARNING]` gh-checks blockquote (or the ⚠️ `P14.11` vector-map row). Do NOT read the pin counts off the vector-map row: when the check ran clean that row is a generic ✅ "no match" like every other clean vector and carries none of them. Always say `PARTIAL … NOT a pass` / `SKIPPED … this check did NOT run` verbatim when it did not run. |
| `Coverage: …` | **Assembled** from the **Coverage** ROW of the provenance table at the top of the report — NOT from any sentence under the banner, where nothing of the kind is rendered. The row reads `✅ complete — every workflow file was scanned` or `⚠️ **PARTIAL** — not every workflow was fully scanned`, and on PARTIAL it does **not** say what was missed: that lives in the separate `> [!WARNING] **Incomplete coverage — …**` blockquote further down, and your line must carry it. |

The extraction commands are in SKILL.md Phase 3. Run them as written; do not
re-derive them here. The one to be careful with is the P14.11 vector-map row:
the id renders in backticks with **no space before it**, so a pattern
expecting `| ` immediately ahead of the id matches nothing and silently drops
the line.

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
