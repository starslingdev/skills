# ci-secure before/after — a publicly disclosed, already-fixed template injection

Two real, unedited-except-for-sanitization `ci-secure` reports produced by the
shipped scanner over the **same workflow file at two commits** of
[`snowflakedb/snowflake-connector-net`](https://github.com/snowflakedb/snowflake-connector-net):
the commit that carried the bug, and the commit that fixed it. Nothing here is
hand-authored — it is exactly what the pipeline emitted (only local filesystem
paths were stripped, replaced with `/local/…`).

**Why this repo.** A security report names live holes, so we do not publish one
against a third party's current code. The finding here is the opposite case: it
was found by Wiz Research, reported to Snowflake through HackerOne, fixed by
Snowflake, and written up publicly at
[wiz.io/blog/red-agent-snowflake-copilot-cicd-bug](https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug).
Both commits are public history, and the vulnerable one has been superseded for
months. The scan is **scoped to the single workflow file that write-up concerns**
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
  [`a43d237`](https://github.com/starslingdev/skills/commit/a43d237), run from a
  clean checkout of that commit. The reports stamp the same SHA.
- **Generated:** 2026-08-17 (UTC).
- **Coverage caveat, stated in both reports:** the network-gated impostor-SHA
  check (P14.11) was disabled (`--gh-impostor off`) so the run needed no GitHub
  API access to a third party's repository. Both reports mark it `SKIPPED — NOT
  a pass`, and the two API-read config hygiene facts render as `unmeasured`
  rather than as passes, which is exactly how the scanner is meant to behave
  when it cannot see something.

Reproduce it:

```bash
npx skills add starslingdev/skills --skill ci-secure

git init snowflake-connector-net && cd snowflake-connector-net
git remote add origin https://github.com/snowflakedb/snowflake-connector-net.git
git sparse-checkout init --no-cone
git sparse-checkout set '.github/workflows/jira_issue.yml'
git fetch --depth 1 origin 4a1b8cecd65b899540e4324715557d6b080ddeb5
git checkout 4a1b8cecd65b899540e4324715557d6b080ddeb5
# then, in your coding agent: "/ci-secure"
```
