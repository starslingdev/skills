# Security patterns catalog — the ten critical vectors

Canonical catalog of the CI/CD attack vectors `ci-secure` detects. **This is a
deliberately minimal, critical-only catalog**: every entry is a complete
outsider → compromise chain — someone with no access to the repo (a fork PR,
a crafted title, a poisoned upstream) reaching code execution with a write
token, secret theft, or shipped-artifact poisoning. Why exactly these ten,
what was rejected, and the test any future addition must pass:
[why-these-ten.md](why-these-ten.md). A census test binds this catalog's
entries to that document's list — they cannot drift apart.

Each pattern has an HTML `METADATA` block consumed by `scripts/scan.py` and a
fix recipe consumed by per-finding fix subagents.

## Contents

- [P14.10 — Template Injection in `run:` Blocks](#p1410--template-injection-in-run-blocks)
- [P14.9 — Fork Code Executed With Privileges](#p149--fork-code-executed-with-privileges)
- [P14.7 — `pull_request_target` Jobs That Write the Shared Cache](#p147--pull_request_target-jobs-that-write-the-shared-cache)
- [P14.11 — Impostor / Unreachable SHA on Action Reference](#p1411--impostor--unreachable-sha-on-action-reference)
- [P14.14 — Whole-Context Dump via `toJSON(secrets|github|env)`](#p1414--whole-context-dump-via-tojsonsecretsgithubenv)
- [P14.15 — Attacker-Controlled Write to `$GITHUB_ENV` or `$GITHUB_PATH`](#p1415--attacker-controlled-write-to-github_env-or-github_path)
- [P14.18 — `pull-requests: write` Granted to Workflow With Untrusted Trigger](#p1418--pull-requests-write-granted-to-workflow-with-untrusted-trigger)
- [P14.19 — Cache or Artifact `path:` Includes Known Credential Files](#p1419--cache-or-artifact-path-includes-known-credential-files)
- [P14.24 — Unverified Remote Code Execution (`curl | bash` and mutable fetch-and-run)](#p1424--unverified-remote-code-execution-curl--bash-and-mutable-fetch-and-run)
- [P14.25 — Dependency Install Scripts Executed in a Privileged Job](#p1425--dependency-install-scripts-executed-in-a-privileged-job)
- [Severity scale](#severity-scale)
- [METADATA schema](#metadata-schema)
- [Reference incidents](#reference-incidents)

---

## Severity scale

Catalog `severity` records the unfixed attack's potency (HIGH / MEDIUM).
**Criticality is keep-list membership**: every pattern in this catalog is a
critical finding by definition — the catalog IS the critical set. Two entries
carry catalog-severity MEDIUM, both for the same documented reason — their
potency depends on a live condition outside the repo rather than on an in-repo
defect: P14.24 (whether the remote code — the host serving the piped script, or
the repository behind the mutable ref — is or becomes malicious) and P14.25 (whether a dependency in the tree is or becomes
malicious). Both render as findings all the same. P14.9 was raised to HIGH
when its detector was rebuilt as a real chain detector.

Where a pattern's body carries a **Prioritization** note, that note is triage
guidance for the reader — which occurrences to attack first. It never
overrides the catalog `severity` the report renders.

---

## METADATA schema

`scripts/scan.py` parses one HTML metadata comment per pattern section. The
keys this catalog currently uses: `pattern`, `severity`, `detector`, `match`
(regex detectors), `correlation` (correlated detectors), `affected_files`,
`fix_strategy`, `title_template` — plus `fix-surface`, which is read out of the
same comment by `scripts/report.py` (it shapes the fix prompt, not the scan).
The loader also accepts four keys no
current entry needs — `yaml_path` and `yaml_value` (the `yaml-path` /
`yaml-path-absent` detectors), `trigger_keys` (`yaml-on-trigger`), and
`file_check` (`repo-file-check`).

The loader is strict: a section whose METADATA is missing, malformed, or
names an unknown `correlation` / `file_check` fails the whole load (exit 1).
Dropping the entry instead would delete a chain from the scan while the
report still read as clean.

Detector semantics (only the types this catalog uses):

- `regex` — applied to each file's content with `re.MULTILINE`. One finding per match.
- `yaml-run-injection` — a regex applied to `jobs.*.steps.*.run` scalars only, so template expressions that are harmless in `with:`/`env:` blocks don't false-positive.
- `yaml-job-correlated` / `yaml-workflow-correlated` — named correlation functions in `scan.py` that fire only when multiple predicates hold on the same job / workflow. The chain detectors.
- `gh-impostor-sha` — the one network-gated detector (see P14.11): runs when `gh` is authenticated, is explicitly recorded as skipped otherwise.

`fix-surface` is `yaml` when the fix is an edit to workflow YAML and
`non-yaml` when it is an org setting, registry configuration, or manual
review. It is declared, never inferred: the fix prompt used to guess from
whether the recipe carried a fenced ```yaml block, and told the agent that
P14.7's and P14.18's workflow restructures were "non-YAML org-level
settings" while restricting it to editing YAML files. All ten current
entries are `yaml`.

Required prose sections per pattern: `**TL;DR.**`, `**What an attacker can
do.**`, `**Anti-pattern**:`, `**Fix recipe**:` (the first ```yaml/```bash
block after it is the inline recipe), and `**Risk of the change.**` — one
honest sentence on what applying the fix could break. `scripts/report.py`
extracts and renders all five with each finding; the risk sentence also goes
to the fix agent as a constraint.

---

### P14.10 — Template Injection in `run:` Blocks

<!-- METADATA
pattern: P14.10
severity: HIGH
detector: yaml-run-injection
match: "\$\{\{\s*(github\.event\.(issue|comment|pull_request|discussion|workflow_run|head_commit|inputs|client_payload)\.[a-zA-Z_.]+|github\.event\.inputs\.[a-zA-Z_0-9]+|github\.head_ref)(\s*\|\|[^}]*?)?\s*\}\}"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: env-var-indirection
fix-surface: yaml
title_template: "Template injection surface in {basename}"
-->

**TL;DR.** A `run:` step contains `${{ github.event.* }}`, `${{ github.event.inputs.* }}`, `${{ github.head_ref }}`, or `${{ github.event.client_payload.* }}` — a template expression sourced from attacker-controllable text. The `${{ }}` substitution happens *before* the shell parses the line, so the substituted value becomes shell code at the same privilege as the workflow.

**What an attacker can do.** An attacker who controls a PR title, issue body, comment, or `repository_dispatch` payload embeds `$(...)`, backticks, or newlines that the shell executes at the workflow's privilege — exfiltrate every secret, push with `GITHUB_TOKEN`, or publish a forged release.

**Anti-pattern**: any `${{ <expr> }}` substitution where the expression resolves to attacker-controllable text, used inside a `run:` step. The `${{ }}` syntax is textual substitution that happens BEFORE the shell parses the script — so a value containing `$(...)`, backticks, `;`, or even a newline becomes shell code with the same privileges as the job. No YAML-layer quoting can fix this. Fields GitHub generates itself whose value shape cannot carry shell metacharacters are excluded: integers (`.number`, `.id`, `.commits` — the PR's commit COUNT, not the push payload's commit array), 40-hex object ids (`.sha`, `.head_sha`, `.merge_commit_sha`), booleans (`.fork`, `.merged`), the closed enum `.author_association`, whose only members are `OWNER`, `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `MANNEQUIN` and `NONE`, and the account `.login` (a GitHub username/org login — `.user.login`, `.sender.login`, `.owner.login`), whose charset GitHub enforces to alphanumerics and single non-leading/trailing hyphens (≤39 chars), so it cannot carry a shell metacharacter even when it is the attacker's own fork account — no member carries a shell metacharacter and no outsider can add one — while everything text-shaped (titles, bodies, branch names, labels, comments) stays in scope. The exclusion only ever applies to a fully-qualified `github.*` context path, so a `.sha` suffix on anything else suppresses nothing. The exclusion also turns on *who fills the field in*, not what it is called: under `github.event.client_payload.*` (the arbitrary JSON body of a `repository_dispatch`), `github.event.inputs.*` (the legacy `workflow_dispatch` input spelling) and `inputs.*` (the modern `workflow_dispatch` / `workflow_call` spelling), the caller chooses the value, so a field named `sha`, `id`, or `number` there carries no shape guarantee at all and is never excluded.

Attacker-controllable expression sources:

- `github.event.issue.title`, `.body`, `github.event.comment.body`
- `github.event.pull_request.title`, `.body`, `.head.ref`
- `github.event.discussion.title`, `.body`
- `github.event.head_commit.message` and any other commit metadata on `push` triggers
- `github.head_ref` (PR source branch name)
- `github.event.workflow_run.head_branch`, `.display_title`
- `workflow_dispatch` inputs that accept free-form strings, in their `github.event.inputs.*` spelling
- Anything from `repository_dispatch.client_payload`
- Output of an earlier step derived from any of the above

A **`||` fallback does not sanitize anything.** `${{ github.head_ref || github.ref_name }}` is matched, because `||` returns the FIRST operand whenever it is truthy — so on a pull request the branch name an attacker chose is exactly what lands in the shell, and the safe-looking right-hand side never evaluates. The match therefore tolerates ` || <anything>` after the context path, and the value-shape exclusion above is judged on the first operand only: `${{ github.event.pull_request.title || '' }}` stays a finding, while `${{ github.event.pull_request.number || 0 }}` remains excluded. (cal.com's `production-build-without-database.yml:72` interpolated `${{ github.head_ref || github.ref_name }}` into a `run:` block holding roughly a dozen secrets and went unreported for exactly this reason.)

The bare `inputs.*` context is deliberately **not** matched. It resolves to a
`workflow_call` input on a reusable workflow, and a caller who can set that
input already has write access to a workflow file in the repo — an insider,
not the outsider the catalog's admission test requires. (`workflow_dispatch`
inputs remain in scope via their `github.event.inputs.*` spelling, which is
what the trigger actually populates.)

The `yaml-run-injection` detector walks `jobs.*.steps.*.run` and only fires when the regex matches inside a `run:` scalar — the actual shell context. Expressions in `env:`, `with:`, or `name:` blocks are structurally excluded: those are not shell contexts for this sink. That is a statement about *this* sink only — an action that shells out its own inputs turns a `with:` value into shell code inside the action's body, which is a different, undetected class.

**Real incidents:**

- **nx / s1ngularity (Aug 2025)**: `pull_request_target` interpolated `${{ github.event.pull_request.title }}` into a shell step. A PR title containing a command substitution executed as the workflow with a publishing token in scope. Over [5,000 private repos were briefly made public](https://www.wiz.io/blog/s1ngularity-supply-chain-attack).
- **snowflakedb/snowflake-connector-net (Jun 2026)**: `jira_issue.yml` ran on `issues: opened` and built `TITLE=$(echo '${{ github.event.issue.title }}' | sed ...)` — escaping that runs *after* template expansion, so a single quote in the title closed the `echo` and ran the rest as shell. An issue title exfiltrated a Jira API token with read access across Snowflake's engineering, security-compliance and bug-bounty projects. The vulnerable line was introduced five days earlier by a commit co-authored by GitHub Copilot Autofix, which *removed* the repo's existing safe `env:` + `jq --arg` pattern — the fix recipe below, already in place, undone by an AI cleanup. The job's `if:` gate also compared `github.event.pull_request.user.login` against a bot account, under `issues` and `issue_comment` triggers where no `pull_request` object exists: the comparison read `null != '...'` and admitted everyone. The detector reports a dead `github.event.*` field in the gate alongside the finding, and calls the gate INERT when the whole condition is that one comparison. The real file's gate is a disjunction, so it gets the dead-field fact and no verdict: nothing here evaluates a compound condition. Found by an autonomous agent within five days of introduction; fixed same-day. [Wiz write-up](https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug).
- **elementary-data (Apr 2026)**: `issue_comment` workflow echoed `${{ github.event.comment.body }}` into bash. A 2-day-old account left a comment that closed the echo string and curled a stager. Within 10 minutes: forged `github-actions[bot]` commit, dispatched release workflow, malicious wheel on PyPI and image on GHCR.

**Prioritization**: every occurrence is a HIGH finding. Attack first the ones on an untrusted-event trigger (`pull_request_target`, `issue_comment`, `workflow_run`, …), where any stranger supplies the attacker text with no prior access. Occurrences on trusted triggers still execute attacker-influenced text as shell and are mechanical to fix.

**Fix recipe**: Move the value out of `${{ }}` into an `env:` var, then reference the env var from the shell — shell quoting now applies and command substitution doesn't fire:

```yaml
# WRONG — RCE if the PR title is a command substitution that fetches and runs a script
- run: echo "Building PR ${{ github.event.pull_request.title }}"

# RIGHT — the env var is bash-quoted, no expression substitution
- env:
    PR_TITLE: ${{ github.event.pull_request.title }}
  run: echo "Building PR $PR_TITLE"
```

For values that flow into a multi-line shell script, set them in `env:` once and reference them. Never use `${{ <user-controllable> }}` directly inside any `run:` step.

Zizmor's [`template-injection`](https://docs.zizmor.sh/audits/#template-injection) audit catches this with a more comprehensive expression model.

**Risk of the change.** Moving the value into an `env:` var changes the shell's view of it — the text is no longer substituted before bash parses the line, so quoting semantics change: verify any comparison or gate built on that value still rejects a wrong value and still accepts the real one.

**Check the job's `if:` gate against the workflow's own `on:` triggers before you claim you preserved its intent.** A gate reading a `github.event.*` object that none of the declared triggers populates — `github.event.pull_request` on an `issues` workflow — is comparing against an empty value and never restricted anything, so "it still behaves the same" is not the goal there. Fix the injection regardless: the occurrence is a finding on its own merits and a dead gate never makes it skippable. Then, in the same change, either repoint the gate at an object those triggers do populate (under `issues`, `github.event.issue.user.login`) or remove it, and say in your summary which you did and why.

---

### P14.9 — Fork Code Executed With Privileges

<!-- METADATA
pattern: P14.9
severity: HIGH
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: switch-to-pull-request-or-drop-head-checkout
fix-surface: yaml
title_template: "Fork code executed with privileges in {basename}"
correlation: untrusted-checkout-executes
-->

**TL;DR.** A workflow runs on a privileged untrusted-event trigger
(`pull_request_target`, `workflow_run`, `issue_comment`, …), checks out the
**attacker's code** (`actions/checkout` with `ref:` pointing at the PR head —
`github.event.pull_request.head.sha`, `github.head_ref`,
`github.event.workflow_run.head_*`), and then **executes in that tree** (a
`run:` step or a local `./action`). These triggers run in the BASE repo's
context — write `GITHUB_TOKEN`, repo secrets — so the attacker's code runs
with your credentials. This is the "pwn request" class: the single most
exploited GitHub Actions mistake in public incident history.

**What an attacker can do.** Open a fork PR — no prior access needed. When the
workflow fires, their code (an install script, a poisoned `Makefile`, a
modified test) executes holding your write token and every secret the
workflow can reach: push commits, mint releases, exfiltrate cloud
credentials.

**Anti-pattern**: the three-condition chain in ONE job — (1) an
untrusted-event trigger with elevated context, (2) `actions/checkout` whose
`ref:`/`repository:` resolves to attacker-controlled head code, (3) any
subsequent step that executes from the working tree (`run:` or `uses: ./…`).
Each condition alone can be legitimate; together they hand the runner to the
fork.

**Detection**: deterministic three-way correlation, per job. Condition (3) is
a deliberate over-approximation stated honestly: a post-checkout `run:` step
almost always executes tree-controlled content (package install scripts,
Makefiles, test suites), so the chain fires without proving which file the
step touches. A checkout of the base ref (no `ref:`, or a non-head ref) never
fires. The bare trigger without the head checkout is NOT a finding (that
presence fact belongs to the scored config checks, not this catalog).

**Platform change, June 18 2026** (actions/checkout changelog): checkout's
default now **refuses** fork head/merge checkouts under `pull_request_target`
and `workflow_run` — shipped in v7, backported to the supported majors on
July 20 2026. GitHub enumerates the residuals, and each one leaves this vector
live: a `run:` block that pulls an untrusted ref itself (`git fetch` /
`gh pr checkout`) never touches the action's default; other untrusted event
types (`issue_comment`, `pull_request_review`, …) are outside the change;
non-fork untrusted repositories are outside it too; and the
`allow-unsafe-pr-checkout` opt-out restores the old behavior wholesale. **The
adoption hole matters most in practice: a SHA-pinned, minor-pinned, or
patch-pinned `actions/checkout` does NOT receive the backport** — the
protection arrives only when the pin moves. Upgrade to v7 and re-pin.

A `ref:` naming `refs/pull/N/*` fires on **either** suffix. `refs/pull/N/head`
is the fork's commit outright; `refs/pull/N/merge` is that same commit merged
into the base branch — it still contains the fork's files, so the code that
executes is attacker-controlled either way. Treating `/merge` as safe is a
common and wrong reading, so the detector matches the `refs/pull/` prefix
without inspecting the suffix.

**Fix recipe**: Run fork code only under the `pull_request` trigger (fork PRs
get a read-only token and no secrets), and keep any privileged follow-up
(commenting, labeling) in a separate `workflow_run` workflow that never
checks out the head — and upgrade `actions/checkout` to v7 and re-pin, so
the action's own June 18 2026 default (which a SHA- or patch-pinned older
checkout never receives) becomes a second layer under the restructure:

```yaml
# .github/workflows/pr-ci.yml — runs the fork's code WITHOUT privileges
name: PR CI
on:
  pull_request:        # not pull_request_target
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>   # checks out the merge ref; no secrets here
      - run: npm ci && npm test
```

If the privileged trigger is genuinely required (e.g. labeling from a fork
PR), never combine it with a head checkout — operate on the API event payload
only, or gate the job on an environment with required reviewers.

**Also upgrade `actions/checkout` to v7 and re-pin.** Since June 18 2026 the
action's own default refuses fork head/merge checkouts on
`pull_request_target` and `workflow_run`, so the upgrade is a second layer
under the restructure above — but only if the pin actually moves: a SHA- or
patch-pinned older checkout keeps the old default even though the backport
exists. Re-pin to a v7 commit, then verify the workflow still checks out what
it is supposed to.

**Risk of the change.** `pull_request` runs fork PRs without secrets and with a read-only token, so any step in the job that legitimately needs them — a preview deploy, a coverage upload, a bot comment — stops working and must move to a separate workflow that never checks out fork code.

---

### P14.7 — `pull_request_target` Jobs That Write the Shared Cache

<!-- METADATA
pattern: P14.7
severity: HIGH
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: switch-pull-request-target-to-pull-request
fix-surface: yaml
title_template: "pull_request_target writes the shared cache in {basename}"
correlation: untrusted-trigger-writes-cache
-->

**TL;DR.** A `pull_request_target` workflow writes to the shared cache — via `actions/cache@*` (including `/save` and `/restore`), `actions/setup-{node,python,go,java,ruby,dotnet}` with a `cache:` input, `pnpm/action-setup`, or `gradle/actions/setup-gradle`. Cache writes use a runner-internal token, not `GITHUB_TOKEN`, so `permissions: contents: read` does NOT block them. **A composite action that caches inside its own body is invisible here**: this detector reads only the workflow YAML, so a job whose only cache step lives behind `uses: some-org/config/.github/setup@main` will not fire and needs manual review.

**What an attacker can do.** A fork PR plants malware in the shared cache from a `pull_request_target` run and waits for the release workflow to restore it — entry vector of TanStack 2026-05-11 (poisoned `pnpm-store` → OIDC theft → 84 malicious npm versions).

**Anti-pattern**: a `pull_request_target` workflow runs `actions/cache@*` (or `actions/cache/{save,restore}@*`), `actions/setup-{node,python,go,java,ruby,dotnet}` with `cache:`, `pnpm/action-setup@*`, `gradle/actions/setup-gradle@*`, or any composite that transitively does so — and therefore writes a cache entry to the base repo's cache scope. A fork PR controls the code that runs and can deliberately poison the cache under a key that production workflows later restore. This is the *writing* half of the cache bridge; P14.19 covers the opposite direction, credential material leaking *out* through the same shared store.

**This is the entry point of the TanStack 2026-05-11 compromise.** `bundle-size.yml` ran `pull_request_target` and called `pnpm install` from the fork's PR-merge ref; `actions/cache@v5`'s post-step persisted the poisoned `pnpm-store` to a key that `release.yml`'s next push-to-main restored. The poisoned store contained malware that extracted the runner's OIDC token from memory and authenticated directly to the npm registry.

> Setting `permissions: contents: read` does NOT block cache writes. `actions/cache` writes the cache using a runner-internal token, not `GITHUB_TOKEN`. Audits that conclude "permissions are tight, this workflow is safe" miss this entirely.

**Platform change, June 26 2026:** GitHub now issues read-only cache tokens for untrusted triggers (`pull_request_target`, `issue_comment`, fork-PR `workflow_run`) on github.com and GitHub with Data Residency, closing this vector's classic entry on github.com defaults. This finding remains live for: GitHub Enterprise Server (not named in the changelog's scope) and third-party cache backends (S3-backed, buildjet, Namespace, sccache). Confirm whether either applies to you before deciding this one is closed — the detector and its severity are unchanged, because neither residual is visible in workflow YAML. GitHub's change also leaves cache poisoning from a TRUSTED trigger that runs untrusted code untouched, but that shape is outside this detector, which fires only on `pull_request_target` — it is not what this finding is reporting.

**Prioritization**: every occurrence is a HIGH finding. Attack first in repos that also carry a privileged publish workflow (npm / PyPI / container release), where the poisoned cache has a trusted consumer to reach; without one the poisoned entry still waits for whatever restores that key next.

**Fix recipe** (in order of preference):

1. **Switch the workflow to `pull_request`.** Fork code stops running with base-repo credentials and stops writing to base-repo cache scope. Use job summaries for output. Durable fix.
2. **Disable cache writes from the trigger.** For `actions/cache@*`, set `lookup-only: true` (or use `actions/cache/restore@*` only). For setup actions, pass `cache: false`.
3. **Namespace the cache key** with a fork-scoped prefix: `key: pr-fork-${{ github.event.pull_request.head.repo.full_name }}-...` — requires a `repository_owner` guard so the base-repo path uses the normal key. Maintenance burden.
4. **Restructure into two workflows** per P14.9.

For composite / reusable actions whose body the audit can't read (e.g., `TanStack/config/.github/setup@main`), review by hand — as the TL;DR says, a transitive `actions/cache` step is invisible to YAML-only static analysis and this detector will not fire on it.

**Risk of the change.** Dropping the cache from the privileged job makes those runs slower (dependencies are re-fetched every time), and splitting into trusted/untrusted workflows changes which run reports the status check — update the branch-protection required checks in the same change or PRs will hang on a check that no longer exists.

---

### P14.11 — Impostor / Unreachable SHA on Action Reference

<!-- METADATA
pattern: P14.11
severity: HIGH
detector: gh-impostor-sha
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: repin-to-reachable-sha
fix-surface: yaml
title_template: "Unverifiable action SHA in {basename}"
-->

**TL;DR.** An action reference `uses: owner/repo@<40-char-sha>` resolves to a
commit that is NOT part of the canonical repo. GitHub stores a repository and
all its forks in one shared object pool, so a SHA that only exists on a
stranger's fork (never reviewed, never merged) is fetchable through the
parent's namespace as if the maintainer had blessed it — the
[Chainguard "imposter commits"](https://www.chainguard.dev/unchained/what-the-fork-imposter-commits-in-github-actions-and-ci-cd)
class. The malicious tj-actions commit was a
[dangling object belonging to no branch](https://www.stepsecurity.io/blog/the-github-warning-everyone-ignores-this-commit-does-not-belong-to-any-branch);
the runner executed it anyway. SHA-pinning alone proves nothing — only
membership in the upstream's history does.

**What an attacker can do.** An action runs attacker-controlled code while
your audit says you're SHA-pinned — the SHA is reachable through GitHub's
shared object pool from an unreviewed fork, fetchable as if blessed by the
action's maintainer.

**Anti-pattern**: any 40-hex `uses:` pin whose commit GitHub's API does not
recognize as belonging to the referenced repository.

**Detection**: first-party, **network-gated** — the one check in this catalog
that cannot be answered from the YAML alone (it is a reachability question
about the action repo's git history). When `gh` is authenticated, the scanner
collects every unique `owner/repo@sha` pin and asks the GitHub API whether
the commit exists in that repository (one cached call per unique pin); a
miss flags every occurrence. When `gh` is unavailable the check is SKIPPED
and the scan output + report say so explicitly — silence is never treated as
a pass. False-positive note: a commit reachable only from a tag (a release
commit) still exists in the repo and passes the API check; this detector
flags only commits the canonical repo does not contain at all.

**Detector limitation — the fork gap, stated because this pattern's own
attacker story is the part it does not fully cover.** Both endpoints the
scanner can use (`repos/{repo}/commits/{sha}` and `repos/{repo}/git/tags/{sha}`)
answer about the fork NETWORK, not about the repository. Measured, not
assumed: `repos/octocat/Hello-World/commits/c5a5e513…` returns 200 for a
commit that lives only in a fork and is reachable from no upstream branch —
re-confirmed on `github/gitignore`. So a pin to an object an attacker pushed
to a fork of the action's own repo reads CLEAN. What the check reliably
catches is the pin that resolves to nothing anywhere in the network: a
deleted, dangling or never-pushed object — the tj-actions shape, and the
reason the check earns its place. Closing the fork gap needs a
ref-reachability test the REST API does not offer; until then, read a clean
P14.11 as "not dangling", not as "provably the maintainer's commit".

**Fix recipe**: Re-pin to a SHA from the upstream's own history — typically
the commit the latest release tag points at:

```bash
gh api repos/OWNER/REPO/commits/BAD_SHA          # 404 → not in the canonical repo
gh api repos/OWNER/REPO/releases/latest --jq .tag_name
gh api repos/OWNER/REPO/git/ref/tags/TAG --jq .object.sha   # pin this instead
```

Verify the new pin the same way before committing it.

**Risk of the change.** Re-pinning points the workflow at different code: a pinned action or installer can drift from the toolchain the build expects, so read the diff between the old and new commit and re-run the workflow before trusting the new pin.

---

### P14.14 — Whole-Context Dump via `toJSON(secrets|github|env)`

<!-- METADATA
pattern: P14.14
severity: HIGH
detector: regex
match: "(?i)toJSON\s*\(\s*(secrets|github|env)\s*\)"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: scope-secret-per-step
fix-surface: yaml
title_template: "Whole-context dump via toJSON in {basename}"
-->

**TL;DR.** A step contains `${{ toJSON(secrets) }}`, `${{ toJSON(github) }}`, or `${{ toJSON(env) }}` — usually as an env var value, sometimes echoed to a log line. `toJSON(secrets)` serializes every secret the workflow can read into one string; `toJSON(github)` includes `GITHUB_TOKEN` and the attacker-controlled event payload.

**What an attacker can do.** Any code in the same step — a postinstall hook, a build script, a malicious test fixture — reads the dumped secret blob and POSTs it anywhere. `toJSON(github)` also dumps `GITHUB_TOKEN` and the attacker-controlled event payload.

**Anti-pattern**: a step assigns the entire `secrets`, `github`, or `env` context to an environment variable, a log line, or any serializable destination. Common shapes:

```yaml
env:
  SECRETS: ${{ toJson(secrets) }}        # hands every secret to whatever runs next
  CTX:     ${{ toJSON(github) }}         # includes GITHUB_TOKEN, event payload
- run: echo "${{ toJson(secrets) }}"     # logged → run-readable by anyone with read access
- run: echo "${{ toJson(env) }}"         # leaks every env var including masked secrets if masking lost
```

Any later code in the same step — intentional or injected — can read the dumped context. The leak surface is the *literal* token in the YAML, not a runtime substitution like P14.10. Detection is therefore purely lexical.

Also covers multi-key JSON literals that pack several secrets into one env value (`CREDS: '{"npm":"${{ secrets.NPM_TOKEN }}","pypi":"${{ secrets.PYPI_TOKEN }}"}'`) — same shape, same risk.

**Severity**: HIGH. A `toJSON(secrets)` dump in a workflow that ever runs untrusted code (test fixtures, dep install scripts, fork PRs) is a direct exfiltration vector — every secret in scope, in one line of YAML. `toJSON(github)` dumps include `GITHUB_TOKEN` and the attacker-controlled event payload.

**Fix recipe**: Pass only the specific secret each step needs as a named env var:

```yaml
# WRONG
- env:
    SECRETS: ${{ toJson(secrets) }}
  run: ./publish.sh

# RIGHT
- env:
    NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
  run: ./publish.sh
```

`toJSON(github)` has no legitimate workflow use; if it appears anywhere except a debug scratch branch, remove it before merge. To debug event payloads, log specific known-safe fields (`github.event.action`, `github.event_name`) one at a time.

**Sources**: Wiz "Secrets context exposure"; Salesforce "Logging and Telemetry"; StepSecurity "Secrets Management".

**Risk of the change.** Replacing a whole-context dump with named secrets removes variables the step may have been reading implicitly — enumerate what it actually consumes first, or the job will fail at runtime on an empty value rather than a missing-secret error.

---

### P14.15 — Attacker-Controlled Write to `$GITHUB_ENV` or `$GITHUB_PATH`

<!-- METADATA
pattern: P14.15
severity: HIGH
detector: regex
match: "(?m)\$\{\{[^}]*(github\.event\.|github\.head_ref|inputs\.|repository_dispatch\.client_payload)[^}]*\}\}[^\n]*>>\s*\"?\$?GITHUB_(ENV|PATH)\"?"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: sanitize-before-export
fix-surface: yaml
title_template: "Attacker-controlled $GITHUB_ENV/PATH write in {basename}"
-->

**TL;DR.** A `run:` step appends attacker-controlled text to `$GITHUB_ENV` or `$GITHUB_PATH` — typically `echo "KEY=${{ github.event.X }}" >> "$GITHUB_ENV"`. The poisoned value flows into *the next step's* environment or `$PATH`, so the attack doesn't fire at the line that looks dangerous; it fires when an otherwise-clean later step inherits the bad env.

**What an attacker can do.** An attacker sets `LD_PRELOAD`, `NODE_OPTIONS`, `PYTHONSTARTUP`, or `GIT_SSH_COMMAND` via the poisoned env, or prepends an attacker-controlled directory to `$PATH`; the next otherwise-clean step's `git`/`npm`/`docker` invocation hits a shimmed binary that exfiltrates secrets.

**Anti-pattern**: a `run:` step appends to `$GITHUB_ENV` or `$GITHUB_PATH` using a value sourced from an attacker-controllable context (same source list as P14.10: `github.event.*`, `github.head_ref`, `inputs.*`, `repository_dispatch.client_payload`, derived step outputs). The appended value flows into the **next** step's environment or `$PATH`, which means an attacker can:

- Override `LD_PRELOAD`, `NODE_OPTIONS`, `PYTHONSTARTUP`, `GIT_SSH_COMMAND`, or any other env var a downstream tool reads for code-injection vectors.
- Prepend an attacker-controlled directory to `$PATH` so subsequent `git`, `npm`, `python`, `docker`, etc. invocations hit a shimmed binary that exfiltrates secrets before forwarding to the real command.

P14.10 catches the *direct* template-injection sink (the substituted value becomes shell code immediately). P14.15 catches the *deferred* sink (the substituted value becomes a poisoned env or PATH that a later, otherwise-clean step inherits). Both can fire on the same workflow; both must be fixed independently.

**Prioritization**: every occurrence is a HIGH finding. Attack first the ones whose substituted expression comes from an untrusted context (`github.event.*`, `github.head_ref`, `repository_dispatch.client_payload`) — those are reachable by a stranger. The rest still hand a later step an environment somebody else wrote.

**Fix recipe**: Sanitize the value before writing, or run it through an `env:` shim first so shell quoting applies, then validate before the export:

```yaml
# WRONG — PR title becomes a poisoned $PATH for every later step
- run: |
    echo "PATH=${{ github.event.pull_request.title }}:$PATH" >> "$GITHUB_ENV"

# BETTER — env shim, validate, only export if safe
- env:
    PR_TITLE: ${{ github.event.pull_request.title }}
  run: |
    if [[ "$PR_TITLE" =~ ^[A-Za-z0-9._/-]+$ ]]; then
      echo "PR_TITLE_SAFE=$PR_TITLE" >> "$GITHUB_ENV"
    else
      echo "Refusing to export PR title with special characters" >&2; exit 1
    fi
```

**Detector limitation**: the regex requires the `${{ ... }}` interpolation and the `>> "$GITHUB_ENV"` / `>> "$GITHUB_PATH"` redirect on the **same line**. The two-step variant — assign attacker text to an intermediate shell variable, then export it on a later line — won't fire automatically. Catch it via the P14.10 finding on the assignment line and triage the surrounding `run:` block manually.

```yaml
# Caught by P14.15 (same-line redirect).
- run: echo "PATH=${{ github.event.pull_request.title }}:$PATH" >> "$GITHUB_ENV"

# NOT caught by P14.15 — P14.10 fires on line 1, manually inspect line 2.
- run: |
    VAL=${{ github.event.pull_request.title }}
    echo "PR_TITLE=$VAL" >> "$GITHUB_ENV"
```

**Pairs with P14.10**: when P14.15 fires on an untrusted context, check P14.10 for direct injection in the same workflow.

**Sources**: Wiz "GITHUB_ENV & GITHUB_PATH injection".

**Risk of the change.** Validation rejects values that used to flow through: a legitimate branch name, tag, or input containing characters your new allowlist excludes will now fail the step, so calibrate the pattern against real values from recent runs before enforcing it.

---

### P14.18 — `pull-requests: write` Granted to Workflow With Untrusted Trigger

<!-- METADATA
pattern: P14.18
severity: HIGH
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: split-trusted-untrusted-workflows
fix-surface: yaml
title_template: "pull-requests: write on an untrusted trigger in {basename}"
correlation: pr-write-and-untrusted-trigger
-->

**TL;DR.** A workflow declares `pull-requests: write` anywhere in the document **and** is triggered by a member of the untrusted-event family — commonly `pull_request_target`, `issue_comment`, `workflow_run`, `pull_request_review`, or `pull_request_review_comment` (the full 11-trigger set is `_UNTRUSTED_TRIGGERS` in `scan.py`).

**What an attacker can do.** A workflow compromise (injection, poisoned dependency, compromised action) self-approves and merges the malicious PR, bypassing branch protection's human-review requirement — elementary-data Apr 2026 chained this to PyPI + GHCR in 10 minutes.

**Anti-pattern**: a workflow declares `pull-requests: write` **and** is triggered by any member of the untrusted-event family — `pull_request_target`, `issue_comment`, `workflow_run`, `issues`, `pull_request_review`, `pull_request_review_comment`, `discussion`, `discussion_comment`, `fork`, `watch`, `repository_dispatch` (this is the whole set; `scan.py`'s `_UNTRUSTED_TRIGGERS` is its source of truth). A compromise of any step in that workflow can self-approve or merge the PR, bypassing branch protection that requires reviewer approval. The elementary-data 2026 incident chained this exact pivot (default-write token + injection → forged `github-actions[bot]` commit → dispatched release workflow → PyPI publish in 10 minutes).

**Prioritization**: every occurrence is a HIGH finding — a direct bypass of branch protection's human-review requirement. Attack first the workflows that also run attacker-supplied content (a template-injection sink, a fork checkout), since those hand the write scope to a stranger rather than requiring a separate compromise.

**Fix recipe**: Two structural options:

1. **Move `pull-requests: write` to a job that does NOT run on the untrusted trigger** — typically by splitting into two workflows (one on `pull_request_target` that uploads an artifact, one on `workflow_run` that consumes the artifact and posts the comment). Same shape as P14.9 fix.
2. **Use a GitHub App** for the comment / approve operation. Astral's `astral-sh-bot` pattern: credentials live outside the repo, the App listens on the same webhook events, and a workflow file compromise doesn't expose the App's installation token.

**Platform change, June 18 2026:** enterprise/org/repo policies for *who and what can trigger workflows* shipped, but they are **opt-in and evaluate-mode** — nothing changes on default configuration, so this vector is intact by default.

The org-level setting *"Allow GitHub Actions to create and approve pull requests"* should be **disabled** regardless; it is an org/repo setting, not a workflow fact, so no detector in this catalog can see it — verify it by hand. P14.18 catches the per-workflow opt-in even when the org-level setting allows it.

`detector: yaml-workflow-correlated` with `correlation: pr-write-and-untrusted-trigger` — fires once per workflow whose `on:` keys include any untrusted-event family member AND that declares `pull-requests: write` **at any scope in the document**. The detector walks the whole parsed document for the declaration rather than tying it to a particular job, because a write granted anywhere in a workflow that an outsider's event can start is reachable; it does not attempt to prove which job would use it.

**Sources**: StepSecurity "Prevent actions creating/approving PRs"; Wiz "Automated pull request creation/approval".

**Risk of the change.** Removing `pull-requests: write` disables any labelling, commenting, or auto-merge automation living in that workflow — those jobs have to move to a separate trusted workflow, and until they do, behaviour maintainers rely on for every PR disappears.

---

### P14.19 — Cache or Artifact `path:` Includes Known Credential Files

<!-- METADATA
pattern: P14.19
severity: HIGH
detector: yaml-job-correlated
correlation: credential-file-in-cache-or-artifact
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: move-credential-outside-cached-path
fix-surface: yaml
title_template: "Cache/artifact path includes credential files in {basename}"
-->

**TL;DR.** A cache step (`actions/cache@*`, `actions/cache/save@*`) or upload step (`actions/upload-artifact@*`, `actions/upload-pages-artifact@*`) has a `path:` that matches a known credential file: `~/.docker/config.json`, `~/.aws/credentials`, `~/.npmrc`, `~/.netrc`, `~/.kube/config`, `~/.ssh/`, `**/.env`, `**/*.pem`, `**/*.key`, and similar.

**What an attacker can do.** Anyone restoring the cache key downloads the credential file directly — across trust boundaries when the key is shared with a less-privileged context. Artifact uploads do the same for anyone with workflow-read access.

**Anti-pattern**: a cache step (`actions/cache@*`, `actions/cache/save@*`) or an upload step (`actions/upload-artifact@*`, `actions/upload-pages-artifact@*`) has a `path:` value matching a known credential-file location: `~/.docker/config.json`, `~/.aws/credentials`, `~/.aws/config`, `~/.npmrc`, `~/.netrc`, `~/.kube/config`, `~/.ssh/`, `**/.env`, `**/*.pem`, `**/*.key`, `**/credentials.json`, `**/service-account*.json`.

For caches: the file becomes restorable by any future workflow that restores the same cache key, including across trust boundaries when the cache key is shared with a less-privileged context (the P14.7 cache-poisoning bridge in reverse — the credential leaks *out* of the privileged context). For artifacts: the file becomes downloadable by anyone with read access to the workflow run, for the artifact's retention window.

**Prioritization**: every occurrence is a HIGH finding — direct credential exfiltration, where the file *is* the credential and the cache / artifact mechanism is the publish-to-attacker step. Attack first the ones whose cache key or artifact is reachable from a less-privileged context (a fork PR's workflow, a public artifact download).

**Fix recipe**: Move the credential file outside the cached / uploaded path. Concrete remediations by file type:

- `.npmrc` with auth: keep the registry config repo-local without the token; set the token from `env:` at job runtime only.
- `~/.docker/config.json`: emit the docker login fresh in each job rather than caching it; if caching docker layers, scope the cache path to `/tmp/.buildx-cache` which doesn't contain auth.
- `~/.aws/credentials`: switch to short-lived OIDC credentials minted at job runtime — there is then no file to leak.
- `~/.kube/config`: never cache; mint at job runtime via `aws eks update-kubeconfig` or equivalent.
- `~/.ssh/`: cache `~/.ssh/known_hosts` only if needed; never cache the private key directory.

**Pairs with P14.7**: that describes cache-poisoning *into* a privileged context. P14.19 describes credential-leak *out of* a privileged context. Same shared-state mechanism, opposite trust direction.

`detector: yaml-job-correlated` with `correlation: credential-file-in-cache-or-artifact` — walks `actions/cache@*` / `actions/cache/{save,restore}@*` / `actions/upload-artifact@*` / `actions/upload-pages-artifact@*` steps and matches each `path:` line against a credential catalog: a credential-bearing directory component (`.aws`, `.ssh`, `.kube`, `.docker`, `.gnupg`, `.azure`, `.gcloud`), a credential basename (`.npmrc`, `.netrc`, `.pypirc`, `credentials`, `credentials.json`, `.env`), or a key-file glob (`*.pem`, `*.key`, `id_rsa*`, `service-account*.json`, `*.p12`, `*.pfx`). It catches a path that *names* a credential. Two known limitations, stated rather than implied: an *over-broad* whole-workspace path (`path: .`, `~`, `**`) sweeps credentials in without naming one and **no pattern in this ten-vector catalog detects it** — check those by hand; and only string-valued `path:` is inspected (a multi-line block scalar counts, and fires if any line matches), so a YAML list-valued `path:` is not walked.

**Sources**: Salesforce "Cache/Artifact storage".

**Risk of the change.** Relocating a credential file changes where every tool in the job looks for it, so the matching config or env var (`DOCKER_CONFIG`, `AWS_SHARED_CREDENTIALS_FILE`, `NPM_CONFIG_USERCONFIG`, …) must move with it — and existing cache entries keep the old copy until the cache key is rotated.

---

### P14.24 — Unverified Remote Code Execution (`curl | bash` and mutable fetch-and-run)

<!-- METADATA
pattern: P14.24
severity: MEDIUM
detector: yaml-job-correlated
correlation: unverified-remote-code-execution
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: pin-and-verify-remote-script
fix-surface: yaml
title_template: "Unverified remote code execution in {basename}"
-->

**TL;DR.** A job executes remotely-fetched code that nothing pins — a script piped straight into a shell, or another repository fetched at a mutable ref (by `git clone`, `git fetch`, or `actions/checkout`) and then run out of. **The piped installer**: a `run:` step pipes a remotely-fetched script straight into a shell — `curl … | bash`, `wget … | sh`, `bash <(curl …)`, or `deno run <url>`. **The mutable fetch**: a job clones or fetches a git tree at a BRANCH, TAG, `HEAD`, or an abbreviated commit id — anything but a full 40-hex commit — and then executes a file out of that tree. Both run whatever the other side serves *at that moment*, with no integrity check, at full job privilege. A fetch pinned to a full 40-character commit id is immutable and is NOT a finding: that is the same trust model this catalog recommends for action pins.

**What an attacker can do.** Whoever controls the fetched code — the upstream host, a hijacked CDN, a maintainer account on the cloned repository — runs arbitrary code on your runner with the job's secrets and `GITHUB_TOKEN`. The mutable-fetch shape needs no host compromise at all: a branch or tag is *designed* to move, so anyone who can push to (or re-point a tag on) that repository changes what your next CI run executes, and nothing in your repo has to change for it to happen. Unlike a SHA-pinned action, both shapes re-resolve live on every run, so a one-time compromise hits every subsequent run until someone notices.

**Anti-pattern**: executing code the repo never pinned, in either of two shapes.

*Shape 1 — the piped installer*: a `run:` scalar that fetches and executes in one step without verifying integrity. The detector only inspects `run:` scalars (not comments or `env:`), and matches:

- `curl … | bash` / `curl … | sh` (with or without `sudo`)
- `wget … | bash` / `wget … | sh`
- `bash <(curl …)` / `sh <(curl …)` (process substitution)
- `deno run … https://…` (deno executes remote URLs directly)

```yaml
# WRONG — runs whatever the installer host serves at this moment
- run: curl -fsSL "<installer-url>" | bash
```

(`<installer-url>` stands in for the vendor address a real workflow writes
inline. For the `curl`/`wget` forms the detector matches either way, because
what makes those unsafe is the pipe into a shell, not the address being
fetched; the `deno run` form is the exception — it has no pipe, so that arm
does key on a literal URL.)

*Shape 2 — the mutable fetch*: within ONE job, a `git clone` / `git fetch` / `actions/checkout` of another repository at a mutable reference, followed by a command that executes out of the directory it landed in.

```yaml
# WRONG — `main` is whatever that branch points at when the job runs
- run: git clone --branch main "<tools-repo-url>" tools
- run: python3 tools/setup.py
```

Execution shapes matched: an interpreter running a fetched file (`python3`/`node`/`bash`/`sh`/`ruby`/`perl`/`pwsh` …), `source`ing a fetched script, `pip install <fetched-dir>` (including `-e`, but never a flag's value — `--target` names a destination and `-r` names a file pip reads), and a fetched path invoked directly (`./tools/install`). `cd` into the fetched directory is followed within the step that ran it, so `cd tools && ./install` connects — and, because `cd` dies with its step, a later step is read from the workspace root again.

**Detection**, and where it deliberately stops short. The pairing must be VISIBLE: the fetch's destination directory and the executed path have to connect inside the same job, which the scanner reads off the shell text. Its consequences, stated rather than implied:

- **A full 40-hex pin silences it — a short sha does not, and neither does a late one.** `git clone … && git -C dir checkout <40-hex>` is immutable and never reported, and the pin has to land BETWEEN the fetch and the execution: pinning is a claim about the code that ran, so a pin before the fetch pinned the tree as it stood and a pin after the execution pinned nothing that had already run. Every suppression is recorded — in the scan's `suppressed_findings`, which is informational and never counts against coverage. An abbreviated id (`a1b2c3d`) is re-resolved by git at fetch time and is treated as mutable.
- **A destination the scanner cannot see is not reported.** `git clone "$TOOLS_URL"` with no explicit target directory leaves the destination unknowable, so no connection is claimed and no finding is raised — a deliberate false negative in favour of never inventing a chain. It is recorded as a coverage note, so "no finding" is distinguishable from "nothing here". Shell the scanner cannot parse (an unbalanced quote) contributes nothing; it is recorded as a coverage note when the text it could not read mentions a fetch or an execution, and only an unreadable `cd` abandons the rest of the step, since that is the case that leaves the working directory stale.
- **`git fetch` fires only for a third-party remote** — a URL, a variable holding one, or a name this job just added with `git remote add <name> <url>` — that something then brings into the tree (`git checkout FETCH_HEAD`, or `reset` / `switch` / `merge` / `rebase` on it). `git fetch origin main` pulls the repository's own history and is not this vector. Unlike the clone arm, a `git fetch` lands in the job's working directory, so the destination is that directory and the finding renders it as `.`.
- **The shell arm reads `git clone`, `git fetch` and `git remote add`, and nothing else.** Everything below is the same trust model and is NOT reported — a clean P14.24 is not a statement that the repo has no mutable fetch-and-run, only that no `git`-spelled one is visible:
  - other fetchers: `gh repo clone`, `svn checkout`, `git submodule update --remote`, and package managers pointed at a git ref (`pip install "git+…@main"`, `go install …@latest`, an npm/cargo git dependency);
  - a tarball fetched with `curl`/`wget`, unpacked, and then run;
  - a fetch and its execution separated by a rename (`mv tools built && python3 built/setup.py`);
  - the mirror of that rename — a third-party tree whose bytes are OVERWRITTEN by the repo's own content before the named file runs (`rm -rf dir/*; cp -r "$GITHUB_WORKSPACE/mine" dir/`), so the fetch into `dir` is real but the executed file is self-owned; the finding names the fetch it can see, and whether a copy-in later replaced those bytes is not tracked;
  - a remote whose URL is set with `git config remote.<name>.url` rather than `git remote add`;
  - execution through an interpreter's INLINE program — `bash -c "…"`, `perl -e`, `php -r`, `pwsh -Command`, `powershell -EncodedCommand` — whose text is not read as commands;
  - an execution whose path BEGINS with a variable this scan cannot resolve (`"$TOOLS/setup.sh"`) — it could hold an absolute path that escapes the fetched tree, so it is recorded as a coverage note rather than resolved and reported; the runner's own absolute variables (`$GITHUB_WORKSPACE/…`) are the exception, treated as absolute, and a variable DEEPER in a path rooted at the fetched directory (`tools/$V/run.sh`) is inside the tree whatever it holds and still fires;
  - execution through a build driver rather than an interpreter (`make -C tools`), or through a versioned interpreter (`python3.11`);
  - a fetch whose execution happens in a different job (different runner, different tree).

  Review those by hand. Widening the shell arm to guess at them would cost the property the whole entry rests on: every reported chain is one a reader can see in their own YAML.
- **A clone of your OWN repository is not reported.** `${{ github.repository }}` is the scanned repo by definition, and a literal `github.com/<owner>/<repo>` is compared against the checkout's `origin` remote — including the authenticated spelling that carries a token in the URL's userinfo. No third party is involved in re-cloning yourself, and the fix this entry recommends (pin to a full commit id) is unactionable for a release workflow that must run at the branch head. In an exported tree with no `.git`, `origin` is unknown and the literal form is reported like any other clone.
- **`working-directory:` is followed** — on the step, and through `defaults.run.working-directory` on the job and on the workflow. A `working-directory:` computed at run time (`apps/${{ matrix.app }}`) is not knowable, so it becomes an opaque directory: a fetch and an execution both inside it still connect to each other, and nothing inside it can match a directory anywhere else.
- **`actions/checkout` of another `repository:` is covered too**, since it is how most workflows fetch a second repository. The same rules apply: a full 40-hex `ref:` is silent, your own repository is silent, and something in a LATER step of the same job has to execute out of the `path:` it landed in. With NO `path:` the checkout replaces the workspace, so anything the job runs afterwards counts. A `repository:`, `ref:` or `path:` computed at run time is recorded as a coverage gap rather than guessed at.

PowerShell `iex (New-Object Net.WebClient).DownloadString(...)` is the same class on Windows runners but is not matched here (casing / Windows-runner rarity); flag it in manual review if you run Windows jobs.

**Severity**: MEDIUM. Real RCE vector, but it depends on the remote code being (or becoming) malicious — a live condition rather than an in-repo defect. A SHA-pinned raw URL (`raw.githubusercontent.com/owner/repo/<sha>/…`) materially reduces the risk of the piped-installer arm; the detector still flags it, so treat a pinned-URL hit as low-priority.

**Fix recipe**: pin what you execute, or vendor it. A mutable fetch is pinned to a full 40-character commit id — the same immutability an action SHA-pin buys, re-pinned deliberately when you want the update. A piped installer becomes three steps: download, verify a known digest, then run the local copy. One block, both shapes — apply the half that matches the occurrence:

```yaml
# RIGHT (mutable fetch) — the tree is one immutable commit, verified by git
- run: |
    git clone "<tools-repo-url>" tools
    git -C tools checkout <full-40-hex-commit>
- run: python3 tools/setup.py

# RIGHT (piped installer) — fetch, verify a known-good digest, then run
- run: |
    curl -fsSL -o install.sh "<installer-url>"
    echo "<known-sha256>  install.sh" | sha256sum -c -
    bash install.sh
```

Prefer a package manager or a SHA-pinned action over an ad-hoc installer where one exists. For tools distributed only as a piped installer, or a repository you must build from source, vendor the code into the repo (so it is reviewed and pinned by git) and run the local copy.

**Sources**: poutine `unverified_script_exec` rule (boostsecurityio/poutine); OpenSSF "Mitigating attack vectors in GitHub workflows".

**Risk of the change.** Pinning freezes the version you execute: a pinned installer or commit can drift from the toolchain the build expects, so upgrades become a deliberate step and CI will fail — correctly, but loudly — the day you actually need the newer code.

---

### P14.25 — Dependency Install Scripts Executed in a Privileged Job

<!-- METADATA
pattern: P14.25
severity: MEDIUM
detector: yaml-job-correlated
correlation: install-scripts-in-privileged-job
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: ignore-install-scripts-in-privileged-job
fix-surface: yaml
title_template: "Dependency install scripts run in a privileged job in {basename}"
-->

**TL;DR.** A job runs a package-manager install of the whole DEPENDENCY TREE that can execute dependency lifecycle scripts (`npm ci`, `npm install`, `pnpm install`, `yarn install` — without `--ignore-scripts`) **and** that same job holds something worth stealing: a `secrets.*` reference beyond `github.token`, or a write-scoped `permissions:` grant. Install scripts (`preinstall`, `install`, `postinstall`, and npm's implicit `binding.gyp` rebuild) run automatically, as the job, before any test or build step — so a compromised package anywhere in the dependency tree executes attacker code exactly where the credentials are.

**What an attacker can do.** Take over a maintainer's npm account, land a typosquat, or compromise a transitive dependency — no access to your repo at any point — and publish a version whose install script runs on your next CI install. It executes with the job's environment: it reads every secret exported there, mints or reuses the write token, and exfiltrates both before a single test has run.

**Anti-pattern**: two conditions in ONE job — (1) a **dependency-tree** install command that can execute dependency lifecycle scripts, and (2) a live payoff in that job (any `secrets.*` reference other than `github.token` alone, or a write-scoped permission at job or workflow scope effective for that job). Neither half is a finding alone: an install in a job with no secrets and no write scope has nothing to hand the attacker, and a job full of secrets that never installs untrusted code is not this vector. The pairing is the vector.

**Two install shapes are outside this anti-pattern and are excluded.** The vector is a compromised package *somewhere in the resolved dependency tree* — code nobody in the repo chose — executing during a bulk install. So neither of these counts:

- a **global** install (`npm i -g corepack@0.31`, `npm install --global @github/copilot`): it resolves nothing from the repo's lockfile and installs a tool the author named;
- a **named single-package** install (`npm install @playwright/test@next`): what runs is exactly what the workflow author typed.

A named single-package install whose own lifecycle script is malicious is a real risk, but it is a DIFFERENT shape — it turns on that one package's identity and provenance, not on the size of a tree — and this pattern deliberately does not silently widen to cover it. Excluding rather than widening is the point: `vercel/next.js` alone reported seventeen `npm i -g corepack@0.31` tool bootstraps as this vector before the exclusion existed.

**Detection**: `detector: yaml-job-correlated` with `correlation: install-scripts-in-privileged-job`, per job. The install leg matches `npm ci`, `npm install`, `npm i`, `pnpm install`, `pnpm i`, `yarn install`, and a bare `yarn` **inside a `run:` scalar only** (a step `name:` or a shell comment that mentions an install is not shell and never matches), and it does **not** fire when the same command carries `--ignore-scripts`, a global flag, or a package spec. The quoted evidence is the first command that QUALIFIES, not the first line the regex touches — a job that bootstraps with `npm install -g npm@11` and then runs `pnpm install` is quoted at the `pnpm install`. The payoff leg walks the job's `env:`/`with:`/`run:` text for `secrets.<NAME>` (ignoring `secrets.GITHUB_TOKEN` / `secrets.github_token` alone, which every job has), plus `secrets: inherit` on a called workflow, plus the **workflow-level `env:` block**, which GitHub merges into every job's environment — and reads the job's own `permissions:` block, or the workflow-level block when the job declares none, for any `write` scope.

**Whether an install actually runs dependency lifecycle scripts is a per-manager question, and the defaults differ.** The finding never asserts that scripts are enabled — that is not knowable from workflow YAML. It states what is: the install executes lifecycle scripts *unless* the manager's version or configuration disables them, and names the condition for the manager it matched.

- **npm** runs dependency `preinstall`/`install`/`postinstall` scripts by default through npm 11. npm v12 (announced on the GitHub Changelog June 9 2026, shipped in v12.0.0 on July 8 2026) turns them off by default and requires an explicit approval entry to re-enable them — including for the implicit `binding.gyp` rebuild, which older npm ran as an implicit `install` lifecycle step with no explicit `package.json` script entry. That implicit rebuild IS suppressed by `--ignore-scripts` (npm treats it as an install lifecycle script — consistent with the node-gyp tradeoff noted below); what npm v12 changes is that it no longer runs it *by default*. When the job itself pins the major (`npm install -g npm@11`), the finding names that major instead of asking.
- **pnpm 10 and later block dependency lifecycle scripts by default** and run them only for packages allow-listed in `onlyBuiltDependencies` / `allowBuilds`; pnpm 9 and earlier run them. This corrects an earlier version of this entry, which claimed pnpm's and Yarn's "own defaults are unchanged" — pnpm's default is off, and saying otherwise made every pnpm finding overstate its own premise.
- **Yarn Classic (1.x)** runs them; **Yarn Berry (2+)** is governed by its `enableScripts` setting (`.yarnrc.yml` or `YARN_ENABLE_SCRIPTS`).

Where the repo pins the manager in `package.json`'s `packageManager` field, the finding quotes that pin rather than asking the reader which version their runner resolves.

**In-repo mitigation signals are read, and the two tiers are kept apart deliberately.** *Hard* evidence — a step in the SAME job, ordered before the install, that empties or falsifies the build allowlist (`yq '.allowBuilds[]=false' -i pnpm-workspace.yaml`), on a repo whose `packageManager` pin resolves pnpm 10 or later — means **no finding at all**; `vitejs/vite` writes exactly that above `pnpm install` in both its release and its publish workflow, and reporting them was a false positive twice over. *Partial* signals — a committed `pnpm-workspace.yaml` allowlist, or a pnpm ≥ 10 pin on its own — leave the finding standing with the mitigation **named in its evidence**, because a committed file is not proof of the file at install time (vite's own release jobs rewrite it mid-run) and a non-empty allowlist means scripts still run for the allow-listed packages (`vercel/next.js` allows `@ast-grep/cli`). Suppressing on a partial signal would be a silent false negative; asserting danger over one would be a false claim. Naming it is neither.

**Composite actions**: an install that happens inside `uses: some-org/action@…` is invisible to a YAML-only scan and will not fire.

```yaml
# WRONG — postinstall from any dependency runs holding NPM_TOKEN
- run: npm ci
  env:
    NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
```

**Platform change, June 9 2026** (GitHub Changelog; shipped in npm v12.0.0 on July 8 2026): npm no longer runs dependency `preinstall`/`install`/`postinstall` scripts by default, and git / remote-URL dependencies now need an explicit flag. **Read this against the install command quoted in this finding's evidence: this particular change is npm's alone.** It does not touch a `pnpm` or `yarn` match — but that is not the same as saying those managers run scripts: pnpm ≥ 10 already blocks them by default (see the catalog entry's per-manager list), and Yarn Berry has `enableScripts`. Nor does it touch a pinned older npm major or any package re-enabled through npm's approval list. For an `npm` match it closes the install leg only on runners that resolve npm 12 or later with no approval entries, which is not knowable from workflow YAML — so this finding still fires and names the condition rather than assuming it.

**Prioritization**: attack first the jobs whose secrets are publish credentials (npm/PyPI/registry tokens, cloud keys) or whose write scope reaches `contents` / `packages` — those turn one malicious dependency into a shipped artifact. A job holding only a narrow read-scoped secret is the same shape with a smaller prize.

**Fix recipe**: Stop executing dependency lifecycle scripts in the job that holds the credentials.

```yaml
# RIGHT — the privileged job installs without running dependency scripts
- run: npm ci --ignore-scripts
# pnpm
- run: pnpm install --frozen-lockfile --ignore-scripts
# Yarn Classic (1.x)
- run: yarn install --frozen-lockfile --ignore-scripts
# Yarn Berry (2+) has no --ignore-scripts flag; disable it for the step:
- run: yarn install --immutable
  env:
    YARN_ENABLE_SCRIPTS: "false"
```

Verify each flag against the manager you actually run: `--ignore-scripts` is the spelling for npm, pnpm and Yarn Classic; Yarn Berry uses the `enableScripts` setting (`.yarnrc.yml` or `YARN_ENABLE_SCRIPTS`); npm v12+ already defaults to off and re-enables per package through its approval list rather than a flag.

**The honest tradeoff**: some dependencies genuinely build at install time — `node-gyp` native modules, Playwright browser downloads, `husky` — and those builds stop happening. Two ways out, both same-day: re-enable scripts for the specific packages that need them (npm v12's approval entries; pnpm's `onlyBuiltDependencies`), or split the install — run the script-bearing install in a job that carries **no** secrets and no write scope, cache or upload the result, and let the privileged job consume it. Blanket `--ignore-scripts` on a build that needs a native module will fail loudly at the next step, not silently.

**Sources**: GitHub, ["Disrupting supply chain attacks on npm and GitHub Actions"](https://github.blog/security/supply-chain-security/disrupting-supply-chain-attacks-on-npm-and-github-actions/) (July 28 2026); npm v12 defaults (GitHub Changelog, June 9 2026; shipped July 8 2026).

**Risk of the change.** `--ignore-scripts` skips legitimate build steps as well as malicious ones: a project with a native module, a browser download, or a git-hook installer will fail at the step that needs the artifact — so run the workflow once after the change and re-enable the specific packages that turn out to need scripts rather than reverting the flag.

---

## Reference incidents

The catalog draws on these public postmortems. Every vector in
[why-these-ten.md](why-these-ten.md) cites its incidents from this list —
"Source for" names only patterns that are still in the catalog; incidents that
motivated removed patterns are not cited here.

- **TanStack npm supply-chain compromise (2026-05-11)** — [postmortem](https://tanstack.com/blog/npm-supply-chain-compromise-postmortem). Three-leg chain: untrusted trigger → cache poisoning via composite action → over-broad OIDC mint. Source for P14.7 and P14.9.
- **Nesbitt 2026-04-28 systemic incident survey** — ["GitHub Actions is the weakest link"](https://nesbitt.io/2026/04/28/github-actions-is-the-weakest-link.html). Traces eighteen months of public Actions supply-chain incidents to a small set of platform features. Source for P14.10 and P14.11.
- **tj-actions/changed-files (Mar 2025)** — [Wiz writeup](https://www.wiz.io/blog/github-action-tj-actions-changed-files-supply-chain-attack-cve-2025-30066), [CISA advisory](https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files-cve-2025-30066-and-reviewdogaction). A tag was remapped to a malicious dangling commit; the poisoned action ran a memory scraper that dumped runner context — secrets, tokens, keys — into build logs. The action was in use across ~23,000 repositories; how many ran the poisoned tag during the exposure window is not quantified by the cited sources. Source for P14.11 and P14.14.
- **Chainguard, "What the fork? Imposter commits in GitHub Actions and CI/CD"** — research naming the class: a fork-only commit is reachable through the upstream repo's URL space but was never in the canonical repo, so a 40-hex pin can look pinned and still be attacker-controlled. Source for P14.11's canonical-repo reachability test.
- **Trivy round 1 (Feb 2026)** — [Snyk writeup](https://snyk.io/articles/trivy-github-actions-supply-chain-compromise/). `pull_request_target` misconfiguration — fork code run with base-repo privileges. Source for P14.9.
- **Trivy round 2 (Mar 2026)** — [StepSecurity writeup](https://www.stepsecurity.io/blog/trivy-compromised-a-second-time---malicious-v0-69-4-release), [Wiz TeamPCP writeup](https://www.wiz.io/blog/trivy-compromised-teampcp-supply-chain-attack). Force-push of 75 of 76 `trivy-action` historical tags (plus 7 `setup-trivy` tags) after credential rotation lag; the malicious binary swept the runner **filesystem** for exactly the credential files this vector warns you not to cache or upload — SSH keys, cloud credentials (AWS/GCP/Azure), and Kubernetes tokens across 50+ sensitive paths — and also scraped runner memory and environment. (The writeup documents credential material harvested from the runner's filesystem/memory/env; it does **not** analyze an Actions-cache pivot.) Source for P14.19.
- **nx / s1ngularity (Aug 2025)** — [Wiz writeup](https://www.wiz.io/blog/s1ngularity-supply-chain-attack). `pull_request_target` + `${{ github.event.pull_request.title }}` in a `run:` block; the published malicious `nx` versions carried a `postinstall` payload that harvested credentials from every machine that installed them. Source for P14.10 and P14.25.
- **Miasma / `@redhat-cloud-services` (June 2026)** — [Microsoft Security Blog, "Preinstall to persistence: Inside the Red Hat npm Miasma credential-stealing campaign", June 2 2026](https://www.microsoft.com/en-us/security/blog/2026/06/02/preinstall-persistence-inside-red-hat-npm-miasma-credential-stealing-campaign/). 32 malicious packages across 90+ versions published to the `@redhat-cloud-services` npm namespace, each carrying a weaponized `preinstall` hook that ran a dropper automatically on `npm install` — through transitive dependencies as well as direct ones. The payload harvested GitHub tokens and Actions/org secrets, npm credentials and cloud credentials reachable from instance metadata, and scraped GitHub Actions runner memory for secrets before republishing poisoned packages downstream. Source for P14.25.
- **npm install-script defaults (2026)** — [GitHub Changelog, June 9 2026 announcement; npm v12.0.0 shipped July 8 2026](https://github.blog/security/supply-chain-security/disrupting-supply-chain-attacks-on-npm-and-github-actions/). Dependency `preinstall`/`install`/`postinstall` scripts — and the implicit `binding.gyp` rebuild — no longer run by default; git and remote-URL dependencies now require an explicit flag. The announcement follows the Miasma compromise above, which used the same install-time execution point. Source for P14.25.
- **GitHub, "Disrupting supply chain attacks on npm and GitHub Actions" (July 28 2026)** — [post](https://github.blog/security/supply-chain-security/disrupting-supply-chain-attacks-on-npm-and-github-actions/). The platform's own account of the 2026 changes: npm install-script defaults, read-only Actions cache tokens for untrusted triggers (June 26 2026), safer `actions/checkout` defaults under `pull_request_target` (June 18 2026), and opt-in workflow-trigger policies (June 18 2026). Source for P14.25 and for the dated platform notes on P14.7, P14.9 and P14.18.
- **snowflakedb/snowflake-connector-net (Jun 2026)** — [Wiz writeup](https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug). `issues` / `issue_comment` + template injection into a `run:` step holding a Jira API token; the vulnerable line was introduced by a Copilot Autofix commit that removed the safe `env:` pattern, and the job's trust gate read an event object neither trigger populates. Source for P14.10.
- **elementary-data (Apr 2026)** — [StepSecurity writeup](https://www.stepsecurity.io/blog/elementary-data-compromised-on-pypi-and-ghcr-forged-release-pushed-via-github-actions-script-injection). `issue_comment` + template injection + default-write `GITHUB_TOKEN` → forged commit → PyPI + GHCR in 10 minutes. Source for P14.10 and P14.18.
- **Ultralytics (Dec 2024)** — [Yossarian writeup](https://blog.yossarian.net/2024/12/06/zizmor-ultralytics-injection). `pull_request_target` + cache poisoning across a trust boundary. Source for P14.7 and P14.10.
- **Codecov bash-uploader breach (Apr 2021)** — [Codecov security update](https://about.codecov.io/security-update/). An altered upload script served from the vendor's own host exfiltrated environment variables — including CI secrets — from every job that piped it into a shell. Source for P14.24.
- **GitHub Security Lab, ["Preventing pwn requests"](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/) and ["Untrusted input"](https://securitylab.github.com/resources/github-actions-untrusted-input/)** — the platform-vendor writeups defining the fork-code-with-privileges chain and the attacker-controlled-write-to-`$GITHUB_ENV` step hijack. Source for P14.9 and P14.15.
- **Astral's "Open source security at Astral"** — [post](https://astral.sh/blog/open-source-security-at-astral). Catalog of mitigations Astral adopted after the above incidents. Source for the Category 14 pattern family and the zero-then-broaden permissions style.

The bridge structure — fork-trust crosses into base-repo cache scope, cache scope crosses into release-workflow runtime, release-workflow runtime crosses into registry write access — is generic. Detection logic looks for *bridges between trust boundaries*, not specific tools.
