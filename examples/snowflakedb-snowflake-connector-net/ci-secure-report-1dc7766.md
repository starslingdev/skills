# snowflake-connector-net — any critical attack vectors in your CI?

| Repository | `/local/snowflake-connector-net` (local checkout — no linked GitHub remote) |
| :--- | :--- |
| **Audited commit** | `1dc7766` — file & line references are anchored to this tree |
| **Workflows scanned** | 1 workflow file(s) under `.github/workflows/` |
| **Catalog** | ten critical attack vectors (critical-only — not a comprehensive audit) |
| **Coverage** | ⚠️ **PARTIAL** — not every workflow was fully scanned; see the Incomplete-coverage warning below |
| **Scanned** | 2026-08-17 (UTC) |
| **Scanner** | ci-secure (skill commit `553baad`) — 0 finding(s) |

```
CI Secure   0 critical findings  ▏0 of 10 vectors hit▕  1 workflow · impostor check SKIPPED
```

> Critical exploit-chain checks only — this is not a comprehensive audit.

> [!WARNING]
> **A network-gated check did not fully run. Its absence from the findings below is NOT a pass.**
>
> - **P14.11 impostor-SHA check: SKIPPED — disabled via --gh-impostor=off (network-gated check did NOT run); this is NOT a pass.**
>
> Re-run once the check can complete to close this gap.

> [!WARNING]
> **Incomplete coverage — 7 workflow file(s) could not be statically scanned.** This is **not** a clean result — fix the cause and re-run before relying on this report.
>
> _Static scan could not read/parse:_
> - **.github/workflows/changelog.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/cla_bot.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/jira_close.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/jira_comment.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/linter.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/main.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass
> - **.github/workflows/semgrep.yml**: present in the audited commit but absent from the scanned working tree (partial or sparse checkout) — never read, so its absence from the findings is NOT a pass

## Critical findings: **0** — no vector matched

**No critical attack vectors detected.**

No pattern matched the workflow files that could be scanned — see the incomplete-coverage warning above; this is NOT a clean result.

Critical exploit-chain checks only — this is not a comprehensive audit.

## 🔗 Vector map — all ten

Every vector ci-secure checks, and what it found. A ✅ row was evaluated and came back clean; a ⚠️ row did **not** run and is not a pass. What each vector actually checks is in the appendix.

| | Vector | Evidence |
|---|---|---|
| ✅ | `P14.10` — [Template Injection in `run:` Blocks](#chain-p14-10) | no match in 1 workflow |
| ✅ | `P14.9` — [Fork Code Executed With Privileges](#chain-p14-9) | no match in 1 workflow |
| ✅ | `P14.7` — [`pull_request_target` Jobs That Write the Shared Cache](#chain-p14-7) | no match in 1 workflow |
| ⚠️ | `P14.11` — Impostor / Unreachable SHA on Action Reference | SKIPPED — disabled via --gh-impostor=off (network-gated check did NOT run); NOT a pass |
| ✅ | `P14.14` — [Whole-Context Dump via `toJSON(secrets\|github\|env)`](#chain-p14-14) | no match in 1 workflow |
| ✅ | `P14.15` — [Attacker-Controlled Write to `$GITHUB_ENV` or `$GITHUB_PATH`](#chain-p14-15) | no match in 1 workflow |
| ✅ | `P14.18` — [`pull-requests: write` Granted to Workflow With Untrusted Trigger](#chain-p14-18) | no match in 1 workflow |
| ✅ | `P14.19` — [Cache or Artifact `path:` Includes Known Credential Files](#chain-p14-19) | no match in 1 workflow |
| ✅ | `P14.24` — [Unverified Remote Code Execution (`curl \| bash` and mutable fetch-and-run)](#chain-p14-24) | no match in 1 workflow |
| ✅ | `P14.25` — [Dependency Install Scripts Executed in a Privileged Job](#chain-p14-25) | no match in 1 workflow |

## 🧰 Config hygiene checks — pass/fail

Hygiene and armor observations about how these workflows are configured. They are **not attack vectors** and they are **not scored, graded, or totalled anywhere in this report** — each row is an independently fixable fact, and a failing row does not make the vector scan above less clean (or a passing row make it safer).

7 check(s) could not be measured (sec.permissions.workflow-declares, sec.permissions.write-scoped, sec.trigger.fork-code-uncleared, sec.secrets.no-blanket-inherit, sec.required-checks.skippable, sec.fork-approval.effective, sec.checkout.credentials-scoped) — a coverage gap, not a pass.

| | Check | Evidence |
|---|---|---|
| ⚠️ unmeasured | every workflow declares `permissions:` (top level, or on every job) | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |
| ⚠️ unmeasured | no workflow-level write permission other than id-token (writes belong on the jobs that need them) | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |
| ❌ fail | a CODEOWNERS entry covers `.github/workflows/` | no CODEOWNERS file at .github/CODEOWNERS, CODEOWNERS, or docs/CODEOWNERS, so workflow changes merge with the same approvals as any other change |
| ⚠️ unmeasured | no untrusted-trigger workflow checks out the attacker's head ref (a bare untrusted trigger passes; the full trigger, checkout and execute chain is reported separately as a fork-code-execution finding, not as a hygiene check here) | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |
| ⚠️ unmeasured | no reusable-workflow call passes `secrets: inherit` | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |
| ⚠️ unmeasured | every required status check is produced by a job that always runs (GitHub counts a SKIPPED required check as a pass, so a check only a conditional job reports can be satisfied without running) | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |
| ⚠️ unmeasured | fork-PR workflow approval gates more than accounts new to GitHub (the weakest setting lets any aged outside account start CI unapproved; requiring approval from first-time contributors to this repo passes) | unmeasured: the fork-PR approval policy is a repository setting read over the API, and this scan had no repository to read it from — that needs `gh` authenticated (`gh auth login`) and a GitHub remote to derive `owner/name` from |
| ⚠️ unmeasured | on untrusted-trigger workflows, every checkout sets persist-credentials: false (GitHub's default persists the token into .git/config where later steps can read it) | unmeasured: 7 workflow file(s) could not be scanned (.github/workflows/changelog.yml, .github/workflows/cla_bot.yml, .github/workflows/jira_close.yml — and 4 more), and this fact is a claim about every workflow |

---

## 📖 What each vector checks

One line per vector, taken from the catalog entry each detector is built from.

- <a id="chain-p14-10"></a>**`P14.10` — Template Injection in `run:` Blocks.** A `run:` step contains `${{ github.event.* }}`, `${{ github.event.inputs.* }}`, `${{ github.head_ref }}`, or `${{ github.event.client_payload.* }}` — a template expression sourced from attacker-controllable text.
- <a id="chain-p14-9"></a>**`P14.9` — Fork Code Executed With Privileges.** A workflow runs on a privileged untrusted-event trigger (`pull_request_target`, `workflow_run`, `issue_comment`, …), checks out the **attacker's code** (`actions/checkout` with `ref:` pointing at the PR head — `github.event.pull_request.head.sha`, `github.head_ref`, `github.event.workflow_run.head_*`), and then **executes in that tree** (a `run:` step or a local `./action`).
- <a id="chain-p14-7"></a>**`P14.7` — `pull_request_target` Jobs That Write the Shared Cache.** A `pull_request_target` workflow writes to the shared cache — via `actions/cache@*` (including `/save` and `/restore`), `actions/setup-{node,python,go,java,ruby,dotnet}` with a `cache:` input, `pnpm/action-setup`, or `gradle/actions/setup-gradle`.
- <a id="chain-p14-11"></a>**`P14.11` — Impostor / Unreachable SHA on Action Reference.** An action reference `uses: owner/repo@<40-char-sha>` resolves to a commit that is NOT part of the canonical repo.
- <a id="chain-p14-14"></a>**`P14.14` — Whole-Context Dump via `toJSON(secrets|github|env)`.** A step contains `${{ toJSON(secrets) }}`, `${{ toJSON(github) }}`, or `${{ toJSON(env) }}` — usually as an env var value, sometimes echoed to a log line.
- <a id="chain-p14-15"></a>**`P14.15` — Attacker-Controlled Write to `$GITHUB_ENV` or `$GITHUB_PATH`.** A `run:` step appends attacker-controlled text to `$GITHUB_ENV` or `$GITHUB_PATH` — typically `echo "KEY=${{ github.event.X }}" >> "$GITHUB_ENV"`.
- <a id="chain-p14-18"></a>**`P14.18` — `pull-requests: write` Granted to Workflow With Untrusted Trigger.** A workflow declares `pull-requests: write` anywhere in the document **and** is triggered by a member of the untrusted-event family — commonly `pull_request_target`, `issue_comment`, `workflow_run`, `pull_request_review`, or `pull_request_review_comment` (the full 11-trigger set is `_UNTRUSTED_TRIGGERS` in `scan.py`).
- <a id="chain-p14-19"></a>**`P14.19` — Cache or Artifact `path:` Includes Known Credential Files.** A cache step (`actions/cache@*`, `actions/cache/save@*`) or upload step (`actions/upload-artifact@*`, `actions/upload-pages-artifact@*`) has a `path:` that matches a known credential file: `~/.docker/config.json`, `~/.aws/credentials`, `~/.npmrc`, `~/.netrc`, `~/.kube/config`, `~/.ssh/`, `**/.env`, `**/*.pem`, `**/*.key`, and similar.
- <a id="chain-p14-24"></a>**`P14.24` — Unverified Remote Code Execution (`curl | bash` and mutable fetch-and-run).** A job executes remotely-fetched code that nothing pins — a script piped straight into a shell, or another repository fetched at a mutable ref (by `git clone`, `git fetch`, or `actions/checkout`) and then run out of.
- <a id="chain-p14-25"></a>**`P14.25` — Dependency Install Scripts Executed in a Privileged Job.** A job runs a package-manager install of the whole DEPENDENCY TREE that can execute dependency lifecycle scripts (`npm ci`, `npm install`, `pnpm install`, `yarn install` — without `--ignore-scripts`) **and** that same job holds something worth stealing: a `secrets.*` reference beyond `github.token`, or a write-scoped `permissions:` grant.

---

## ⚙️ Methodology

| Term | Definition |
| --- | --- |
| **Scope** | Critical exploit-chain checks only — this is not a comprehensive audit. ci-secure checks a deliberately small set of critical exploit chains — the ones that turn a workflow into remote code execution or credential theft. A clean ci-secure report does **not** mean the repository is secure. |
| **What is not scanned** | Workflow YAML only. A composite action the workflow calls (`uses: ./.github/actions/…`, or any third-party action) is a separate file that this scan does not open, so an install command, a secret dump, or an untrusted-input expansion living inside one is invisible here — the workflow line that calls it looks clean. Steps generated at runtime (a `run:` block that writes and then executes a script) are outside the scan for the same reason. |
| **Provenance path** | The Repository row names the local checkout the scan read, so the file:line references below can be tied to a tree. That is deliberate — it is the audited path, and on your own run it is your own — with `$HOME` abbreviated to `~`. |
| **ci-secure pattern catalog** | Public catalog at [`security-patterns.md`](https://github.com/starslingdev/skills/blob/main/skills/ci-secure/references/security-patterns.md) — the critical exploit-chain patterns, authored from public CI/CD supply-chain incidents (tj-actions, Ultralytics, nx/s1ngularity, Trivy, TanStack, elementary-data). Each pattern carries a TL;DR, an attacker-capability statement, the anti-pattern definition, and a fix recipe. |
| **Network-gated checks** | Some patterns need the GitHub API to decide (e.g. whether an action pin points at a commit reachable from the canonical repo). When `gh` is unavailable they do not run, and the report says so loudly under the header — a skipped check is never a pass. |
| **Severity** | Criticality is membership in the ten-vector catalog: every finding here is a complete outsider → compromise chain, and every one renders. The HIGH / MEDIUM label records the unfixed attack's potency — it never tiers, truncates, or reorders what you see. |
| **Finding grouping** | Every occurrence of the same underlying rule (same catalog pattern) collapses into one `## Finding N` entry — and every group renders, always. The definition list summarizes the group; the `#### Evidence` section points at the findings JSON, which holds the file:line + matched-lines detail for every occurrence. |
| **Workflow activity** | When `--repo owner/repo` is passed, the GitHub API supplies per-workflow run counts. The row summarizes them as `X of Y active in last 30d · Z runs (cap 50/wf) · K dormant`. The per-workflow cap means `Z` is a floor when any workflow hit it (a `+` marker is added to make that explicit). A workflow with zero runs in the last 90 days is **dormant**; a group is dormant only when *every* one of its occurrences is on a dormant workflow. A **reusable** workflow (`on: workflow_call` only) is never called dormant on an empty run history: GitHub attributes its runs to the calling workflow, so that history is empty however often the file executes — its activity is reported as unknown. |
| **`N of 10 vectors hit`** | The denominator is always 10 — the whole catalog — never "the vectors that ran". A vector that could not be evaluated (a network-gated check with no `gh`) shows ⚠️ in the vector map and is counted as neither hit nor clean; shrinking the denominator instead would quietly convert a check that did not run into one that passed. |
| **Workflows scanned** | Counts every workflow FILE read, including one that is entirely commented out. Such a file defines no jobs and cannot produce a finding, but it is still part of the tree that was examined, and excluding it would make the denominator depend on file contents. |

---

## 🗄️ Data sources

| Source | Coverage | Used for |
| --- | --- | --- |
| ci-secure scanner at commit `553baad` | Every `.github/workflows/` file ending `.yml` or `.yaml`, dot-prefixed names included, under the audited tree (1dc7766) | Critical exploit-chain pattern detection (see the catalog) |
| GitHub API — run activity | not queried (no `--repo`) | Pass `--repo owner/repo` to enrich findings with workflow activity |

**Data freshness.** Scanner ran at `2026-08-17T22:46:27Z`. Workflow YAML is read from the audited tree at commit `1dc7766`. Activity counts (when `--repo` is supplied) reflect a rolling 30-day window at scan time.

---

Generated by [StarSling](https://starsling.dev) 💫
