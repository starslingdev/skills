# ci-secure before/after — a publicly disclosed, already-fixed template injection

Two real, unedited-except-for-sanitization `ci-secure` reports produced by the
shipped scanner over the **same workflow file at two commits** of
[`snowflakedb/snowflake-connector-net`](https://github.com/snowflakedb/snowflake-connector-net):
the commit that carried the bug, and the commit that fixed it. Nothing here is
hand-authored — it is what the pipeline emitted, with local filesystem paths
stripped and replaced with `/local/…`, and one field carried forward from an
earlier run of the same skill over the same findings (both noted precisely under
**Provenance**).

**Why this repo.** A security report names live holes, so we do not publish one
against a third party's current code. The finding here is the opposite case: it
was found by Wiz Research, reported to Snowflake through HackerOne, fixed by
Snowflake, and written up publicly at
[wiz.io/blog/red-agent-snowflake-copilot-cicd-bug](https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug).
Both commits are public history, and the vulnerable one was superseded in June
2026, the day the report reached Snowflake. The scan is **scoped to the single workflow file that write-up concerns**
(`.github/workflows/jira_issue.yml`, via a sparse checkout) so the example stays
inside the already-public, already-fixed material and reports nothing about the
rest of the repository.

## Result

| | Vulnerable | Fixed |
| :--- | :--- | :--- |
| Commit | [`4a1b8ce`](https://github.com/snowflakedb/snowflake-connector-net/commit/4a1b8cecd65b899540e4324715557d6b080ddeb5) | [`1dc7766`](https://github.com/snowflakedb/snowflake-connector-net/commit/1dc7766c5aa4b07da3cf3416e501364d3bc827a0) |
| Critical findings | **3 HIGH** (P14.10, template injection) at `jira_issue.yml` lines 24, 25, 42 | **0** |
| Vectors hit | 1 of 10 | 0 of 10 |
| Report | [`ci-secure-report-4a1b8ce.md`](./ci-secure-report-4a1b8ce.md) | [`ci-secure-report-1dc7766.md`](./ci-secure-report-1dc7766.md) |
| Raw findings | [`ci-secure-findings-4a1b8ce.json`](./ci-secure-findings-4a1b8ce.json) | [`ci-secure-findings-1dc7766.json`](./ci-secure-findings-1dc7766.json) |

The `Jira creation` workflow pasted an issue's title and body straight into a
`run:` block through `${{ }}`, in a job holding the Jira service-account
credentials. `${{ }}` substitution happens before the shell parses the line, so
the `sed` quote-escaping on lines 24–25 could not stop it. Snowflake's fix moves
the values into `env:` vars — the same fix `ci-secure` recommends — and the
scanner returns zero findings on it.

That is the point of shipping the pair: the tool is specific, not just noisy.
The same catalog, the same file, the same command — 3 findings before, 0 after.

## Provenance

- **Scanned repo:** `snowflakedb/snowflake-connector-net`, sparse checkout of
  `.github/workflows/jira_issue.yml` only, at the two commits above (clean tree
  in both cases — the reports' `Audited commit` rows carry no dirty marker).
- **Scanner:** `ci-secure` at `starslingdev/skills` commit
  [`3545d1b`](https://github.com/starslingdev/skills/commit/3545d1b), run from a
  clean checkout of that commit on `main`. The reports stamp the same SHA, and
  both findings JSONs record `skill_tree_dirty: false`.
- **One spliced field.** Each finding's `attacker_scenario` is the prose the
  skill's own Phase 2.5 wrote for these exact findings on an earlier run at
  commit `a43d237`, carried forward each time the reports were re-rendered
  (first against the partial-checkout coverage fix, then against the on-`main`
  scanner commit above). Every other field, including all three file:line
  locations and their quoted evidence, is byte-identical across those runs;
  nothing was hand-authored, and no number or finding was edited.
- **Generated:** 2026-08-18 (UTC).
- **Coverage caveats, stated loudly in both reports.** Neither report claims a
  clean repository, and that is the second thing this pair demonstrates:

  - **The scan is partial, and says so.** Scanning one file out of the eight in
    that commit means seven were never read, so both reports carry
    `Coverage ⚠️ PARTIAL`, an `Incomplete coverage` banner naming every
    unscanned file, and config hygiene facts that render `unmeasured` instead
    of passing. A finding here is a real finding; the *absence* of findings in
    a file nobody opened is not a pass, and the report refuses to let it read
    as one.
  - **The network-gated impostor-SHA check (P14.11) was disabled**
    (`--gh-impostor off`) so the run needed no GitHub API access to a third
    party's repository. Both reports mark it `SKIPPED — NOT a pass`.

  So the `1dc7766` report says "zero findings in the one file that was
  scanned", never "this repository is secure". That distinction is the whole
  reason the coverage machinery exists.

- **What the fix actually changed:** [`jira_issue.yml` between the two
  commits](https://github.com/snowflakedb/snowflake-connector-net/compare/4a1b8cecd65b899540e4324715557d6b080ddeb5...1dc7766c5aa4b07da3cf3416e501364d3bc827a0)
  — the values move out of `${{ }}` in `run:` and into `env:`. Linked so the
  before/after claim can be checked against the source rather than taken on
  trust, since a zero-finding report necessarily names no file.

Reproduce it. The sparse checkout and the missing `origin` remote are both
load-bearing: they are what make the reports scan one file and render
`local checkout — no linked GitHub remote`, so adding a remote or a full
checkout will not reproduce the committed bytes.

```bash
npx skills add starslingdev/skills --skill ci-secure

git init snowflake-connector-net && cd snowflake-connector-net
git sparse-checkout init --no-cone
git sparse-checkout set '.github/workflows/jira_issue.yml'
git fetch --depth 1 https://github.com/snowflakedb/snowflake-connector-net.git \
  4a1b8cecd65b899540e4324715557d6b080ddeb5
git checkout FETCH_HEAD
ls .github/workflows/   # must list jira_issue.yml and nothing else
# then, in your coding agent:
#   "/ci-secure, with the impostor-SHA check disabled (--gh-impostor off)"
```

Check that `ls` before scanning. If `git sparse-checkout init` fails on your
git version the fetch still succeeds and you get the **whole** repository,
which scans a third party's workflows far beyond the disclosed one.
