# snowflake-connector-net — any critical attack vectors in your CI?

| Repository | `/local/snowflake-connector-net` (local checkout — no linked GitHub remote) |
| :--- | :--- |
| **Audited commit** | `4a1b8ce` — file & line references are anchored to this tree |
| **Workflows scanned** | 1 workflow file(s) under `.github/workflows/` |
| **Catalog** | ten critical attack vectors (critical-only — not a comprehensive audit) |
| **Severity breakdown (by occurrence)** | HIGH: 3 |
| **Coverage** | ⚠️ **PARTIAL** — not every workflow was fully scanned; see the Incomplete-coverage warning below |
| **Scanned** | 2026-08-18 (UTC) |
| **Scanner** | ci-secure (skill commit `3545d1b`) — 3 finding(s) |

```
CI Secure   3 critical findings  ▏1 of 10 vectors hit▕  1 workflow · impostor check SKIPPED
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

## Critical findings: **3** — 1 of 10 vectors hit

3 occurrence(s) of 1 distinct attack vector(s) (by vector: 1 HIGH) across 1 of the 1 workflow file(s) scanned. The header's severity breakdown counts occurrences, not vectors.

## 🔗 Vector map — all ten

Every vector ci-secure checks, and what it found. A ✅ row was evaluated and came back clean; a ⚠️ row did **not** run and is not a pass. What each vector actually checks is in the appendix.

| | Vector | Evidence |
|---|---|---|
| 🟥 | `P14.10` — [Template Injection in `run:` Blocks](#finding-1) | 3 sites across 1 workflow |
| ✅ | `P14.9` — [Fork Code Executed With Privileges](#chain-p14-9) | no match in 1 workflow |
| ✅ | `P14.7` — [`pull_request_target` Jobs That Write the Shared Cache](#chain-p14-7) | no match in 1 workflow |
| ⚠️ | `P14.11` — Impostor / Unreachable SHA on Action Reference | SKIPPED — disabled via --gh-impostor=off (network-gated check did NOT run); NOT a pass |
| ✅ | `P14.14` — [Whole-Context Dump via `toJSON(secrets\|github\|env)`](#chain-p14-14) | no match in 1 workflow |
| ✅ | `P14.15` — [Attacker-Controlled Write to `$GITHUB_ENV` or `$GITHUB_PATH`](#chain-p14-15) | no match in 1 workflow |
| ✅ | `P14.18` — [`pull-requests: write` Granted to Workflow With Untrusted Trigger](#chain-p14-18) | no match in 1 workflow |
| ✅ | `P14.19` — [Cache or Artifact `path:` Includes Known Credential Files](#chain-p14-19) | no match in 1 workflow |
| ✅ | `P14.24` — [Unverified Remote Code Execution (`curl \| bash` and mutable fetch-and-run)](#chain-p14-24) | no match in 1 workflow |
| ✅ | `P14.25` — [Dependency Install Scripts Executed in a Privileged Job](#chain-p14-25) | no match in 1 workflow |

Every hit vector renders in full below — no vector is trimmed, tiered, or topped up. Findings are grouped by underlying rule: every occurrence of the same catalog pattern collapses into one entry, ranked by severity. Within a group, a long list of occurrences shows a sample inline and says so on the spot; the complete list is always in the findings JSON.

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

<a id="finding-1"></a>

## 🟥 Finding 1: Template Injection in `run:` Blocks — 3 sites / 1 workflow

**Anyone with a GitHub account can open an issue on the repo — no prior access needed.**

- **Pattern:** [P14.10 — Template Injection in `run:` Blocks](https://github.com/starslingdev/skills/blob/main/skills/ci-secure/references/security-patterns.md#p1410--template-injection-in-run-blocks)
- **TL;DR:** A `run:` step contains `${{ github.event.* }}`, `${{ github.event.inputs.* }}`, `${{ github.head_ref }}`, or `${{ github.event.client_payload.* }}` — a template expression sourced from attacker-controllable text. The `${{ }}` substitution happens *before* the shell parses the line, so the substituted value becomes shell code at the same privilege as the workflow.
- **What an attacker could do:** Anyone with a GitHub account can open an issue on the repo — no prior access needed. The `create-issue` job substitutes the issue's title and body into its `run:` script before the shell parses it, so a title or body containing `$(...)` or backticks executes as commands in a job that holds `JIRA_BASE_URL`, `JIRA_USER_EMAIL` and `JIRA_API_TOKEN`; the `sed` quote-escaping on lines 24-25 runs after the substitution and cannot stop it. From there the attacker can print or exfiltrate the Jira service-account credentials and the job's `issues: write` token.
- **Severity:** **HIGH**
- **Workflow activity:** —
- **Occurrences:** 3 occurrences across 1 workflow
- **Fix strategy:** `env-var-indirection`

#### 🔍 Evidence

3 occurrences across 1 workflow.

- `.github/workflows/jira_issue.yml:24` — jobs: `create-issue`

  ```yaml
    23:           # Escape special characters in title and body
    24:           TITLE=$(echo '${{ github.event.issue.title }}' | sed 's/"/\\"/g' | sed "s/'/\\\'/g") <-- here
    25:           BODY=$(echo '${{ github.event.issue.body }}' | sed 's/"/\\"/g' | sed "s/'/\\\'/g")
  ```

  > **derived** — assembled by the scanner, not quoted source:
  > this job carries a gate condition: ((github.event_name == 'issue_comment' && github.event.comment.body == 'recreate jira' && github.event.comment.user.login == 'sfc-gh-mkeller') || (github.event_name == 'issues' && github.event.pull_request.user.login != 'whitesource-for-github-com[bot]')) — it reads `github.event.pull_request`, which no trigger this workflow declares (`issue_comment`, `issues`) ever populates, so that comparison is against an empty value and therefore always evaluates the same way, whoever triggered the workflow. What the gate as a whole then does — including whether a fixed term decides it outright — depends on the rest of the condition, which is not evaluated here — verify it

- `.github/workflows/jira_issue.yml:25` — jobs: `create-issue`

  ```yaml
    24:           TITLE=$(echo '${{ github.event.issue.title }}' | sed 's/"/\\"/g' | sed "s/'/\\\'/g")
    25:           BODY=$(echo '${{ github.event.issue.body }}' | sed 's/"/\\"/g' | sed "s/'/\\\'/g") <-- here
    26:
  ```

  > **derived** — assembled by the scanner, not quoted source:
  > this job carries a gate condition: ((github.event_name == 'issue_comment' && github.event.comment.body == 'recreate jira' && github.event.comment.user.login == 'sfc-gh-mkeller') || (github.event_name == 'issues' && github.event.pull_request.user.login != 'whitesource-for-github-com[bot]')) — it reads `github.event.pull_request`, which no trigger this workflow declares (`issue_comment`, `issues`) ever populates, so that comparison is against an empty value and therefore always evaluates the same way, whoever triggered the workflow. What the gate as a whole then does — including whether a fixed term decides it outright — depends on the rest of the condition, which is not evaluated here — verify it

- `.github/workflows/jira_issue.yml:42` — jobs: `create-issue`

  ```yaml
    41:                 "summary": "'"$TITLE"'",
    42:                 "description": "'"$BODY"' \\\\ \\\\ _Created from GitHub Action_ for ${{ github.event.issue.html_url }}", <-- here
    43:                 "customfield_11401": {"id": "14723"},
  ```

  > **derived** — assembled by the scanner, not quoted source:
  > this job carries a gate condition: ((github.event_name == 'issue_comment' && github.event.comment.body == 'recreate jira' && github.event.comment.user.login == 'sfc-gh-mkeller') || (github.event_name == 'issues' && github.event.pull_request.user.login != 'whitesource-for-github-com[bot]')) — it reads `github.event.pull_request`, which no trigger this workflow declares (`issue_comment`, `issues`) ever populates, so that comparison is against an empty value and therefore always evaluates the same way, whoever triggered the workflow. What the gate as a whole then does — including whether a fixed term decides it outright — depends on the rest of the condition, which is not evaluated here — verify it

#### 🛠️ Fix

**Do this:** Move untrusted `${{ }}` values into an `env:` var before using them in `run:`

Move the value out of `${{ }}` into an `env:` var, then reference the env var from the shell — shell quoting now applies and command substitution doesn't fire. See [catalog §P14.10](https://github.com/starslingdev/skills/blob/main/skills/ci-secure/references/security-patterns.md#p1410--template-injection-in-run-blocks) for the full recipe and cross-references.

**Risk of the change:** Moving the value into an `env:` var changes the shell's view of it — the text is no longer substituted before bash parses the line, so quoting semantics change: verify any comparison or gate built on that value still rejects a wrong value and still accepts the real one.

<details>
<summary>🤖 Prompt for your coding agent</summary>

````text
You are fixing every occurrence of ci-secure finding `P14.10` (HIGH) — Template Injection in `run:` Blocks — in this repository.

Context: A `run:` step contains `${{ github.event.* }}`, `${{ github.event.inputs.* }}`, `${{ github.head_ref }}`, or `${{ github.event.client_payload.* }}` — a template expression sourced from attacker-controllable text. The `${{ }}` substitution happens *before* the shell parses the line, so the substituted value becomes shell code at the same privilege as the workflow.

Occurrences (3):
- .github/workflows/jira_issue.yml:24 — jobs: create-issue
- .github/workflows/jira_issue.yml:25 — jobs: create-issue
- .github/workflows/jira_issue.yml:42 — jobs: create-issue

Catalog reference: https://github.com/starslingdev/skills/blob/main/skills/ci-secure/references/security-patterns.md#p1410--template-injection-in-run-blocks

Recipe (from the catalog):

```yaml
# WRONG — RCE if the PR title is a command substitution that fetches and runs a script
- run: echo "Building PR ${{ github.event.pull_request.title }}"

# RIGHT — the env var is bash-quoted, no expression substitution
- env:
    PR_TITLE: ${{ github.event.pull_request.title }}
  run: echo "Building PR $PR_TITLE"
```

Constraints:
- Risk of the change: Moving the value into an `env:` var changes the shell's view of it — the text is no longer substituted before bash parses the line, so quoting semantics change: verify any comparison or gate built on that value still rejects a wrong value and still accepts the real one.
- Modify ONLY the workflow files listed above. Do not touch any other file.
- Do not widen the patch beyond what the recipe specifies. If you spot a sibling issue in the same file, mention it in your summary but do not fix it here.
- Do not commit, push, or open a PR. Leave the changes in the working tree for human review.
- If the recipe is ambiguous for a specific file, stop and ask before guessing.

When done, print a 3-line summary: which files changed, what the shared fix was, and any follow-up the user should verify manually.

Verify (the oracle — you are done only when it passes): re-run the ci-secure scan over this repo — `python3 <ci-secure>/scripts/run.py --root . --out /local/out/vuln-recheck.json` — and confirm the chain no longer fires: no finding with `"pattern": "P14.10"` may remain in that JSON. If one does, occurrences are left — fix them and re-run. If you also edited the rendered ci-secure report, `python3 <ci-secure>/tests/verify_report.py --report <report.md> --findings <the ci-secure findings JSON from your run (the Phase 2 --out path)>` must still print `all checks passed`.
````

</details>

#### 📚 References

*[5,000 private repos were briefly made public](https://www.wiz.io/blog/s1ngularity-supply-chain-attack) · [Wiz write-up](https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug) · [`template-injection`](https://docs.zizmor.sh/audits/#template-injection)*

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
| ci-secure scanner at commit `3545d1b` | Every `.github/workflows/` file ending `.yml` or `.yaml`, dot-prefixed names included, under the audited tree (4a1b8ce) | Critical exploit-chain pattern detection (see the catalog) |
| GitHub API — run activity | not queried (no `--repo`) | Pass `--repo owner/repo` to enrich findings with workflow activity |

**Data freshness.** Scanner ran at `2026-08-18T00:37:37Z`. Workflow YAML is read from the audited tree at commit `4a1b8ce`. Activity counts (when `--repo` is supplied) reflect a rolling 30-day window at scan time.

---

Generated by [StarSling](https://starsling.dev) 💫
