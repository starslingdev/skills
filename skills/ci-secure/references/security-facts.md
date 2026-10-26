# The security config facts — machine-only inputs, and why each is disjoint from ci-score

## Contents

- [The disjointness census](#the-disjointness-census)
- [What each fact asks, exactly](#what-each-fact-asks-exactly)

---

ci-secure computes thirteen deterministic, pass/fail configuration facts as
MACHINE-ONLY inputs for a future blended score (the ci-advisor door, not yet
public). They are aggregated `100 × passed / scored` with no weights and no
partial credit (registered 2026-08-03) — but **that aggregate is never a number
the user sees**, and it is NOT ci-score's CI Score (a separate skill's grade).
ci-secure ships no security score anywhere a reader looks. The ten attack
vectors this skill scans for are **findings, never score input** — several
vector detectors are lexical rather than confirmed exploit proofs, and a public
number must not grade a stranger's repo down on an unconfirmed match.

**The denominator is token-dependent, so the aggregate is not comparable
across scans run under different auth.** Up to six of the thirteen facts read
the GitHub API (the CODEOWNERS fact only once a CODEOWNERS rule covers the
workflows). In a repository where every fact applies and that rule exists, a
scan with no repository or no token measures seven of thirteen and scores
`100 × passed / 7`; the same repository scanned with a token that can read
every source scores over thirteen. Between the two sits the ordinary
reader's token: the two environment facts are measured from environment
settings, but any verdict that rests on classic branch protection rules or a
ruleset's bypass list (both admin-only) stays unmeasured, and so does the
environment-scoped secrets fact (secret lists need admin rights or the
secrets permission). A fact that does not apply to the
repository at all leaves the denominator on top of that (see below), so both
numbers drop by one for each such fact — in a repository whose workflows run
no suite, in one that configures no dependency update tool, and, for the two
environment facts, in one with no environments or no required reviewers.
The unmeasured ids are named in the block's `unmeasured` list and the caveat, so
a consumer — ci-advisor above all — can tell the two apart, but only if it
checks: comparing the raw numbers across differently-authenticated scans
compares different measurements.

**The aggregate is machine-only.** ci-secure's own report and close render
these facts as a pass/fail table and **no number** by design: a hygiene
aggregate labelled "Security score" overclaims what thirteen config observations
can say, and printed beside a ten-vector scan it read as a contradiction. The
`security_score` block in the findings JSON keeps its shape — same keys, same `fact_id`s, same outcomes, same aggregate; only prose the
report prints was reworded so it stops naming a score the reader never sees.
Consumers bind to the ids, not to the sentences. ci-advisor blends from that
block, and quantification belongs there, where the blend context carries the
denominators.

A fact that cannot be measured — an unscannable workflow file; for the six
API-gated facts, no repository and no token; or, for
`sec.deps.update-cooldown`, an update config this scan cannot parse — is
**unmeasured, never a silent pass**: it scores nothing, stays in the applicable count as a
named coverage gap, and the block says so.

A fact that **does not apply** is a different state and is treated differently.
"This could not be checked" is a coverage gap; "there is nothing here to check"
is not, and counting it as one would report a gap that does not exist. A
not-applicable fact scores nothing and leaves the applicable count entirely —
the same treatment ci-score gives a check it rules not applicable — while its
row still renders, because a row that quietly vanishes reads as a pass. Its id
is listed in the block's `not_applicable` list, separate from `unmeasured`, so
a consumer never has to guess which of the two it is looking at. Four facts
can reach that state today: `sec.gate.test-failure-fatal`, only in a
repository whose workflows run no test or lint suite at all;
`sec.deps.update-cooldown`, only in a repository that configures neither
Dependabot version updates nor Renovate; `sec.environments.branch-restricted`,
only in a repository with no environments; and `sec.environments.approval-holds`, only when no
environment requires a reviewer.

## The disjointness census

Every fact is checked against ci-score's shipped 11-check registry before
registration. The bar: **no single YAML edit may move a ci-score check and a
fact here at the same time.** ci-score's registry is frozen (its number must
not change meaning); this table is pinned by a census test against a manifest
of its check ids, so a future addition on either side that collides goes red
instead of shipping.

| fact | nearest ci-score check | why one edit cannot move both |
| --- | --- | --- |
| `sec.permissions.workflow-declares` | `ci.security.scoped-id-token` | Adding a `permissions:` block is a different edit from relocating an `id-token: write` grant. A workflow can declare permissions and still fail scoped-id-token, and vice versa. |
| `sec.permissions.write-scoped` | `ci.security.scoped-id-token` | **Disjoint by construction:** `id-token` is excluded from this fact's scope set, so the one edit scoped-id-token asks for (move `id-token: write` to a job) cannot change this fact. `write-all` fails here on its own; unwinding it into per-job grants is not the id-token relocation. |
| `sec.codeowners.workflows` | — none | CODEOWNERS is not workflow YAML, and the branch rule that enforces it (require review from Code Owners) is read over the API, not from YAML; no ci-score check reads either. API-gated once a rule covers workflows. |
| `sec.trigger.fork-code-uncleared` | `ci.trigger.*` (concurrency, cancel, path-filter) | Those checks read `concurrency:` and `paths:` blocks; this fact reads the trigger list plus checkout `ref:`/`repository:` values. No shared YAML key. Also tiered against ci-secure's own P14.9 finding: a bare untrusted trigger passes (the true-of-84% defect, deliberately removed), trigger + attacker-head checkout fails this FACT, and only the full trigger + checkout + execution chain is the P14.9 finding — so the fact and the finding cannot fire on the same edit either. |
| `sec.secrets.no-blanket-inherit` | — none | No ci-score check reads `secrets:` on reusable-workflow calls. |
| `sec.required-checks.skippable` | — none | Branch protection is not workflow YAML; no ci-score check reads the API. The workflow-side edit this fact asks for (add an always-running verdict job that `needs:` the conditional ones) touches no key any ci-score check reads. |
| `sec.fork-approval.effective` | — none | A repository Actions setting, not workflow YAML at all; no ci-score check reads it, and no YAML edit can move it. |
| `sec.gate.test-failure-fatal` | `ci.parallel.test-sharding` | Both look at the jobs that run the suite, and neither edit moves the other: sharding is a `strategy.matrix` on the job, this fact is the `run:` line's exit code and the step's `continue-on-error`. A sharded suite can still swallow every shard's failure, and an unsharded one can be perfectly fatal. Also bounded against the OTHER engine: `continue-on-error` on an upload, report, or notification step is a speed and reliability question that ci-speedup owns, and it does not reach this fact, because only a step whose own `run:` command the allowlist recognises as a suite is in scope — a `uses:` upload action has no `run:` line at all. Residual, disclosed: a reporting step whose command *does* match the allowlist is in scope here too, as is a suite step carrying `continue-on-error: true` that has been red for every run (ci-speedup's OPT68); the bound is on the command, not on the step's stated purpose. |
| `sec.checkout.credentials-scoped` | `ci.checkout.shallow-clone` | Both read checkout steps, but different keys: `fetch-depth` there, `persist-credentials` here — two `with:` entries, two edits. This fact also applies only on untrusted-trigger workflows (excluding the payload-less `fork`/`watch` notification events); shallow-clone applies on PR-gating ones. |
| `sec.environments.branch-restricted` | — none | Deployment environment settings, rulesets and classic branch protection are read over the API, not from workflow YAML; ci-score reads no environment, ruleset or secret API, and no YAML edit moves this fact (an `environment:` key in a workflow is not what it reads). |
| `sec.environments.approval-holds` | — none | An environment's required-reviewer setting, read over the API; no ci-score check reads it and no YAML edit can move it. |
| `sec.secrets.environment-scoped` | — none | The repository and organisation secret lists are read over the API; ci-score reads no secret API, and no workflow YAML edit moves this fact (moving a secret into an environment is a settings change). |
| `sec.deps.update-cooldown` | — none | Dependabot and Renovate config files are not workflow YAML, and no ci-score check reads `dependabot.yml`, a Renovate config, `cooldown`, or `minimumReleaseAge` (ci-score's registry and methodology were searched for all four). The nearest neighbour, `ci.security.pinned-action-shas`, reads `uses:` refs in workflow files; adding a cooldown cannot move it, and pinning an action cannot move this. |

**Residual correlation, disclosed:** a repo with careless
workflow hygiene will tend to fail checks in both tools — that is the
configuration-vs-consequence pairing the CI Score's axes are built on. It is
disclosed in the census table above, which names every near-neighbour check in
ci-score's registry alongside the fact here that resembles it, and says what
separates the two. What the census rules out is the sharper defect: one edit,
two moved numbers.

## What each fact asks, exactly

- **`sec.permissions.workflow-declares`** — every workflow file declares
  `permissions:`, at top level or on every job. An undeclared workflow runs
  with the repository default, which on older repos is read-write everything.
- **`sec.permissions.write-scoped`** — no workflow-level `write` for any scope
  other than `id-token` (which belongs to ci-score). Write grants belong on
  the jobs that need them; `permissions: write-all` fails.
- **`sec.codeowners.workflows`** — a CODEOWNERS entry covers
  `.github/workflows/` (`.github/workflows/`, `.github/**`, or a bare `*`
  global owner; an extension rule like `*.go` does not cover it) AND the
  default branch enforces it. Without a covering entry, workflow changes merge
  with the same approvals as any other change — a fail decided from the files
  alone, with no API call. With one, CODEOWNERS only REQUESTS an owner's
  review: it binds only when the branch requires review from Code Owners, in a
  ruleset's pull request rule (organisation rulesets included) or in classic
  branch protection. Either source requiring it passes; both readable and
  neither requiring it fails, because any approver can then merge a workflow
  change. **To fix that fail**: turn on "Require review from Code Owners" in
  the branch's ruleset or branch protection. API-gated once covered: with no
  repository or no token, or when the answer cannot be completed (classic
  protection is admin-only, so a ruleset that does not require it plus an
  unreadable classic endpoint establishes nothing), the fact is unmeasured and
  says why. Two answers settle classic protection without admin access: a 404
  "Branch not protected" from its endpoint, and `protected: false` on the
  public branch endpoint (that flag is set by rulesets as well as classic
  protection, so `false` means neither exists; `true` settles nothing).
- **`sec.trigger.fork-code-uncleared`** — no workflow with an untrusted-event
  trigger (`pull_request_target`, `workflow_run`, `issue_comment`, …) checks
  out the attacker's head ref. Bare untrusted triggers pass.
- **`sec.secrets.no-blanket-inherit`** — no reusable-workflow call passes
  `secrets: inherit`; secrets are passed by name so a called workflow's blast
  radius is visible in the caller.
- **`sec.required-checks.skippable`** — every status check the default branch
  REQUIRES is produced by a job that always runs. GitHub counts a skipped
  required check as a pass, so a check only a conditional job reports can be
  satisfied by never running it. The pass shape is the always-running verdict
  job that `needs:` the conditional suites and asserts their results. Required
  contexts no workflow job produces are named, not judged — external app
  checks, reusable-workflow jobs, and templated job names whose rendering this
  scan will not guess all land there, each with the reason it was refused —
  and a PASS is a statement about EVERY
  required check, so ANY context that could
  not be traced leaves the fact unmeasured rather than green — the ordinary
  shape of a mature repository is a dozen required contexts with most coming
  from external apps, and a green earned off the one traceable check would be
  a clean bill for the others. A `needs:` target
  that is not a job in the file, and a `needs:` cycle, are unknowns for the
  same reason. Whether a workflow's jobs can report on a pull request at all is
  three-valued, and each value is treated differently. A `pull_request`
  workflow, a plain `push`, or a push over every branch (`branches: ['**']`)
  CAN report, so an always-running job there certifies the check. A push
  restricted to TAGS provably cannot — no pull-request branch push matches it —
  so a same-named job in a tag-only release workflow is not a producer at all.
  A matrix job's contexts come from the combinations the matrix can actually
  run: a literal `name:` takes GitHub's appended `(value, …)` suffix, and a
  `name:` templated over the matrix (`build shard ${{ matrix.shard }}/4` over
  `shard: [1, 2, 3, 4]`) renders its legs directly. Legs are read the way
  GitHub substitutes them, so a `true` leg is `true` and a `null` leg is
  nothing at all. Both readings enumerate the
  same combinations, so both refuse the same unknowables — `fromJSON()` or any
  other computed value, `include:`, a nested or non-list shape, a placeholder
  that is not a plain `matrix.<axis>` reference, an axis a name references but
  the matrix does not declare, and an `exclude:` this scan cannot read (a key
  naming no declared axis, a computed value, or a value that is not a scalar)
  — and an unenumerable matrix leaves the check
  untraced rather than guessed. Further shapes are refused even when they
  do enumerate:

  - **A name with no literal LETTER to anchor it** — a bare
    `${{ matrix.target }}`, but equally `${{ matrix.a }}/${{ matrix.b }}`,
    `${{ matrix.s }} (${{ matrix.l }})` or `${{ matrix.a }}_${{ matrix.b }}`.
    Punctuation, underscores and digits anchor nothing, and those render an
    external app's `security/snyk`, `Analyze (javascript)` and `ubuntu_x64`
    names on a coincidence. One literal letter is enough: `v${{ matrix.n }}`
    binds.
  - **An EMPTY leg**, which renders exactly what GitHub renders and that is the
    problem: `${{ matrix.p }}lint` over `p: ['']` collapses to the bare `lint`
    another job owns, so the anchoring the name was admitted on is gone from
    the rendering.
  - **Interior whitespace** in the `name:` or in a leg. Renderings are
    whitespace-normalised so they can be compared and GitHub normalises none,
    so the compared string is one GitHub never emits.
  - **A rendering a genuine reusable caller in these files ALSO claims**, as
    its own check or as the `<caller> / <child>` leaf of its invocation. Where
    the placeholder sits in the template has nothing to do with it; the
    competing claim is the whole rule. A caller whose own `name:` is templated
    is rendered the same way, and one this scan cannot render competes for the
    names its literal text could still produce — not, as it once did, for
    every name in the repository.
  A push filtered to specific branches or paths is UNKNOWN: it may or may not
  run on a given pull request's head branch, so its jobs still count against
  the check but can never be the evidence that one always reports. Unknown is
  never treated as absent — dropping the only producer would make the fact
  unmeasured, and an unmeasured fact scores nothing while a fail scores zero.

  None of that makes filtering a BYPASS, which is a separate question and the
  opposite of what GitHub does: a workflow those filters skip never reports its
  check, so the required check stays PENDING and the pull request cannot merge.
  Only a skipped JOB reports Success. (`pull_request.branches` filters the base
  branch — the branch whose protection is being read — so every pull request
  this fact gates is inside that filter anyway.)
  **To fix a fail**: add one job that carries the required check's name, runs
  `if: always()` (or `!cancelled()`), `needs:` the
  conditional suites, and fails unless the suite that should have run passed —
  then point branch protection at that job's name instead of the suites'.
  `success() || failure()` is deliberately NOT accepted on a job with
  `needs:`. It reads like `always()` and is not: when a dependency is SKIPPED
  neither predicate holds, so GitHub skips the verdict job too — and a skipped
  required check is exactly what it reports as passed. Accepting it would
  certify a gate with the same hole the fact exists to find.

  A branch that requires NO status check passes this fact — there is no
  required check to bypass — which is a true statement about a narrow question
  and not a statement that the branch is protected. Whether a branch should
  require a check is a different fact's business.
  API-gated: with no repository or no token the fact is unmeasured and says so.

  *Source*: GitHub documents this under "Handling skipped but required checks"
  — a skipped required check is treated as successful, so branch protection
  does not hold the pull request. This repository shipped that bypass and
  closed it with the verdict-job pattern above.
- **`sec.fork-approval.effective`** — the repository's fork-PR approval policy
  gates more than accounts new to GitHub. `all_external_contributors` and
  `first_time_contributors` (the default, and a legitimate trust judgment) both
  pass; only `first_time_contributors_new_to_github` fails, because any outside
  account old enough clears it and the gate then gates nobody real. Hygiene,
  not an exploit chain — fork runs still carry no secrets and a read-only
  token, so what an unapproved run buys is compute under the repository's name
  and quiet iteration. **To fix a fail**: this is a repository setting, not a
  YAML edit — Settings → Actions → General → "Approval for running fork pull
  request workflows" → require approval from first-time contributors (or from
  all outside collaborators). No workflow change closes it. API-gated, and a
  policy value outside GitHub's documented enum is disclosed rather than
  judged.
- **`sec.checkout.credentials-scoped`** — on untrusted-trigger workflows,
  every `actions/checkout` sets `persist-credentials: false`. GitHub's default
  persists the token into `.git/config`, where attacker-influenced later steps
  can read it. Trusted-trigger workflows are not this fact's business — nor are
  the payload-less `fork`/`watch` notification events, which carry no
  attacker-influenced execution that could read a persisted token.
- **`sec.gate.test-failure-fatal`** — no job that runs the test or lint suite
  swallows its own exit code. This is the sibling of
  `sec.required-checks.skippable`, and it belongs here for the same reason that
  one does: that fact is "a required check that was SKIPPED reports green"; this
  one is "a check that RAN reports green regardless of what the suite did."
  The failure mode is identical — the merge gate is decorative, and the repo
  believes it is gated — the reader is the same person, and so is the fix:
  let the failure reach the job's exit status. It is a pass/fail configuration
  fact, not an attack vector: nothing here is an exploit chain, no finding is
  raised, and it is not a candidate for the ten (`why-these-ten.md` governs
  those, and this is not one of them).

  The failing shapes are `|| true`, `|| :`, `|| echo …`, a trailing `exit 0`,
  `set +e` with no later re-check of the status it stopped enforcing, and
  `continue-on-error: true` on the suite's own step (or on the job that runs
  it). A quoted `'true'` counts; an expression like
  `continue-on-error: ${{ matrix.experimental }}` does not — it is true on some
  matrix legs and false on others, and the YAML cannot say which.

  The trailing-`exit 0` shape is judged against the step's **effective
  shell**, resolved the way GitHub resolves it — the step's `shell:`, then
  the job's `defaults.run.shell`, then the workflow's, then the runner
  default. Where errexit applies — the platform default `bash -e {0}` on
  Linux and macOS, `sh -e`, an explicit `shell: bash` (invoked with
  `-eo pipefail`) — a failing suite aborts the step before a trailing
  `exit 0` is ever reached, so the step already fails and flagging it would
  be a false accusation against a fatally wired job. The arm therefore fires
  only where the discard is real: `pwsh`, `powershell`, or `cmd`, declared
  at any of those levels or implied by a `windows-*` runner's `pwsh`
  default. A shell expression or a template this list does not recognise
  resolves to errexit-bearing, for the same miss-beats-accusation reason.
  The other shapes need no such gate: `|| true` and `|| :` succeed under
  any shell, and `set +e` is itself the act of turning errexit off.

  **Scope, deliberately narrow.** Only a workflow that can report a check on a
  pull request is judged at all — a `pull_request`, `pull_request_target`,
  `push` or `merge_group` trigger. A nightly `schedule` job or a
  `workflow_dispatch`-only smoke run reports on no pull request, so a tolerant
  `|| true` there is not the decorative merge gate this fact describes, and
  failing it would be the false accusation the rest of this paragraph rules
  out. An `on:` block that cannot be read stays in scope: dropping it would
  turn an unreadable trigger into a silent pass.

  Within those workflows, only a `run:` line whose COMMAND a shipped allowlist
  recognises as a test, lint, or build-verification suite can fail this fact.
  The command, not the line: `docker pull ghcr.io/org/mypy:latest || true`,
  `pkill -f karma || true`, `tar czf out.tgz .tox || true` and
  `gh pr comment --body "eslint found 3 issues" || true` each carry an
  allowlisted name in an image tag, a process pattern, a directory and a
  message body, and none of them runs a suite. The allowlist is therefore
  anchored to the head of the command, after any environment assignment,
  privilege or timing wrapper, interpreter runner (`python -m pytest`,
  `poetry run pytest`), leading path (`./mvnw test`) and shell keyword. The
  keyword matters as much as the wrappers do: `if ! pytest -q; then` and
  `for m in $MODULES; do pytest -q; done` run the suite exactly as a bare line
  does, and a suite the anchor cannot see does not merely go unjudged — it
  takes the whole repository out of this fact's denominator as not applicable,
  which is a silent loss of coverage rather than a miss on one line.
  Two things follow,
  both intended. A `run:` block the allowlist does not
  recognise — `./scripts/ci.sh || true` — is never failed: it may be swallowing
  a suite or a cleanup, the YAML cannot say which, and a false accusation
  against a stranger's repository costs more than a miss in a fact that feeds a
  published score. And `continue-on-error: true` on an **upload, report, or
  notification** step — codecov, `upload-artifact`, `upload-sarif`, a Slack
  webhook — does not reach this fact, because a step is judged only by its own
  `run:` line: a `uses:` action has no `run:` line at all, and a webhook `curl`
  matches nothing on the allowlist. Making a reporting step non-fatal is a
  deliberate, usually correct choice about speed and reliability, and it is
  already ci-speedup's business; billing one configuration to two engines would
  be a defect, not thoroughness. **Residual, disclosed:** that exemption is a
  property of the COMMAND, not of the step's name or intent, so a reporting
  step whose `run:` line does match the allowlist — `npm run
  test:upload-coverage`, `make check-links || true` — is in scope here and can
  be billed by both engines. A `|| true` on a utility line sitting beside the
  suite (`grep … || true`) swallows the grep, not the tests, and is not this
  fact's business either. Neither is a **here-doc body**: the lines between
  `cat > run.sh <<'EOF'` and its closing delimiter are text on its way to a
  file, so a step that WRITES a tolerant wrapper script is not a step that
  runs one, and judging that text as shell would fail a job whose own suite is
  fatal.

  **A repository with nothing to check is NOT APPLICABLE, not a pass.** There
  is nothing to swallow, so there is nothing to certify: a green here would
  read as "this repo's tests are wired to fail the build" about a repo with no
  tests. It leaves the denominator, which is the shape ci-score settled on for
  the same question (its test-sharding check is not applicable when no
  test-like job exists).

  **"Nothing to check" is a stronger claim than "nothing recognised", and the
  fact does not confuse the two.** Not-applicable requires BOTH that no
  recognised suite ran AND that no step threw an exit status away. Where the
  allowlist recognised nothing but something *did* discard a status —
  `bash ci/test.sh || true`, the canonical unknown-purpose shape — the two
  readings are opposite (a swallowed suite, or a tolerated cleanup) and the
  YAML settles neither. That is UNMEASURED, with the steps named: a coverage
  gap that stays in the denominator, because "there is nothing here to check"
  would assert no gap exists on the one shape where one demonstrably does.
  **To fix a fail**: drop the swallow so the suite's exit status reaches the
  job — and if the intent was to see the failure without blocking, keep the
  reporting and re-raise the status (`… || { echo "suite failed"; exit 1; }`,
  or capture `$?` under `set +e` and `exit` it at the end).
- **`sec.deps.update-cooldown`** — every configured dependency update tool
  waits at least **3 days** after a release is published before proposing it.
  This is the prevention half of P14.25: a hijacked maintainer account or a
  typosquat usually ships a malicious version that is reported and pulled
  within days, so a tool that waits rarely hands that version to the CI
  install that runs its lifecycle scripts. Three days is the threshold — long
  enough that most malicious releases are gone first, short enough to be
  uncontroversial — and it lives in one named constant.

  - **Dependabot** (`.github/dependabot.yml` or `.yaml` with an `updates:`
    list): every entry is judged. An entry's `cooldown.default-days` must be
    3 or more, and so must every `semver-major-days` / `semver-minor-days` /
    `semver-patch-days` it sets — a long default beside a one-day patch window
    still lets patch releases through in a day. GitHub documents the semver
    keys as applying only to package managers that support SemVer, and names
    `docker`, `github-actions` and `terraform` as not; on those only
    `default-days` is judged. An entry that sets only the
    semver keys passes when each one present is 3 or more, and the evidence
    says so (unlisted classes fall back to the default). An entry with **no**
    `cooldown` (or one that sets no day keys) is judged at Dependabot's own default: since July 14 2026
    Dependabot waits three days on github.com with no configuration (GitHub
    Enterprise Server from 3.23), which meets the threshold, so the entry
    passes and the evidence names it as relying on that default rather than
    claiming the repository set one. An entry whose `cooldown.exclude`
    lists `*` lets every package skip the wait and **fails**; a narrower
    `exclude` passes and the evidence names the exempted packages. `include`
    lists are not judged.
  - **Renovate** (the first of `renovate.json`, `renovate.jsonc`,
    `renovate.json5`, the same three under `.github/`, `.renovaterc`,
    `.renovaterc.json`, `.renovaterc.jsonc`, `.renovaterc.json5`, or a
    `renovate` key in `package.json` — Renovate's own search order): a
    top-level `minimumReleaseAge` of 3 days or more (`"3 days"`, `"1 week"`,
    `"72 hours"`, `"1 month"`; Renovate reads `M` as months and `m` as
    minutes). A value set only inside `packageRules` covers only the packages
    those rules match and does not pass; neither does a preset in `extends`
    this scan cannot see into, and the fail says so when `extends` is set.
    A `packageRules` entry that lowers `minimumReleaseAge` below 3 days (or
    sets it to `null`) for **every** package — no `match*` / `exclude*`
    filter, or only match-all ones such as `"*"` or `"/.*/"` — undoes the
    top-level wait and **fails**. A rule that lowers it for a filtered set of
    packages (your own scope, say) passes, and the evidence names it.

  Both tools configured: both must pass. Neither configured: **not
  applicable**, the same treatment `sec.gate.test-failure-fatal` gets — a
  repository with no update tool has nothing here to delay. A config this scan
  cannot read — a Dependabot file the YAML loader rejects, a Renovate config
  that is not plain JSON (comments, trailing commas), or an age it cannot
  convert to days — is **unmeasured**, never a pass and never a fail; the row
  says to check the value by hand.

  **Security updates are not delayed.** GitHub documents that a Dependabot
  cooldown applies to version updates only, and Renovate documents that
  security updates bypass `minimumReleaseAge`; a fix for a known
  vulnerability still opens immediately. **To fix a fail**: `cooldown: { default-days: 7 }`
  on each Dependabot `updates` entry named, raising (or removing) any
  `semver-*-days` key under 3 on that entry and keeping its other `cooldown`
  keys, or `"minimumReleaseAge": "7 days"` at the top level of the Renovate
  config.
- **`sec.environments.branch-restricted`** — every deployment environment
  either accepts only refs a leaked non-admin push credential can neither
  create nor move ("closed"), or requires at least one reviewer. A credential that can
  push workflow files can otherwise create a throwaway branch, add a
  workflow that binds to the environment, read its secrets and delete the
  branch; nothing of it reaches the scanned checkout, and protecting
  workflow files on the default branch does not stop it. **Every
  environment is in scope**, secrets or not: a cloud role that trusts the
  environment's name over OIDC makes the name itself the credential. The
  GitHub-managed `github-pages` and `copilot` are in scope only when they
  hold a secret, and are listed as "can't tell" (never passed or failed)
  when their secrets cannot be read.

  A pattern is **closed** when every ref it can match is covered by an
  active ruleset (organisation rulesets included; `evaluate` and
  `disabled` count for nothing) or a classic rule that blocks creating it
  AND blocks pushing to it, with nobody short of an admin on the bypass
  list. A pull request rule blocks pushing only when it needs at least one
  approval AND that approval is dismissed or re-asked after a new push
  (otherwise a credential pushes a workflow onto an already-approved pull
  request and merges it); one that does not say is unmeasured. GitHub
  applies one classic rule to a branch, never a combination: a rule naming
  it exactly outranks every wildcard, and among wildcards the oldest wins.
  So an exact-name rule that leaves an accepted branch open opens the
  pattern, and two wildcard rules that disagree over it leave it
  unmeasured (their order is not read). An exact name that already exists needs pushes blocked plus
  deletion or creation blocked (otherwise it can be deleted and pushed
  again); one that does not exist yet needs creation blocked. A branch
  matching an accepted wildcard that already exists with no protection
  proves the pattern open on its own. Tag patterns need a closed tag
  ruleset. **"Protected branches only" is decided by classic branch
  protection rules alone** (measured 2026-10-24: a ruleset-protected branch
  is not admitted). With no classic rule in the repository at all, GitHub
  lets every branch and tag deploy to such an environment, which fails like
  no restriction; with at least one, each classic pattern must block
  creation and pushes.

  A repository with **no environments** is not applicable here, whatever
  its secrets are. Readable repository and organisation secrets are
  `sec.secrets.environment-scoped`'s question, and failing both checks on
  one setup would bill one gap twice. (Until 2026-10-24 this case failed
  when a secret was readable; that rule was retired when the secrets fact
  was scored.)

  API-gated, with F7's contract: no repository or no token is unmeasured;
  an unreadable source makes only the verdicts that depend on it
  unmeasured, while an environment that accepts any branch still fails; no
  retries. Classic branch protection rules and ruleset bypass lists are
  readable only by an admin (anyone else is answered with an empty classic
  list, which is never read as "none"), so on a reader's token a
  "protected branches only" environment, or one closed only by a ruleset,
  is unmeasured. **To fix a fail**: set the environment to "Selected
  branches and tags" naming exact protected branches whose rules block
  creating and pushing, or "Protected branches only" with a classic rule
  that blocks creation on every protected pattern, or add a required
  reviewer.
- **`sec.environments.approval-holds`** — every environment that has
  required reviewers also has "Prevent self-review" on. Otherwise a leaked
  reviewer's credential starts a run and approves it itself. One bad
  setting costs one check: reviewers satisfy the branch fact above, and
  only this fact fails on self-review. "Admins can bypass" is shown as an
  information line, never part of either verdict, because only an admin
  credential can use it. Not applicable when no environment has reviewers.
  A GitHub-managed environment whose secrets cannot be read and that allows
  self-review keeps this fact from passing (unmeasured), as it does the
  branch fact.
  **To fix a fail**: turn on "Prevent self-review" in each named
  environment's required-reviewers rule.
- **`sec.secrets.environment-scoped`** — no repository- or
  organisation-level Actions secret is readable by this repository: every
  secret it uses lives in an environment. Any secret outside one is
  readable from any branch a push credential creates, however well the
  environments are locked. **Passes** when neither list holds a secret,
  including a repository with no secrets at all. **Fails** when either
  does; the evidence names each one, repository and organisation
  separately, with a short deterministic hint (`PRIVATE_KEY`, `APP_ID`,
  `PYPI`, `NPM_TOKEN`, …) putting the likely high-value ones first — a
  sort order, not a severity. **Unmeasured** when a list cannot be read
  (recorded: both answer a non-admin reader 403, although GitHub's docs
  say collaborator access; an entry with no name counts as unread too),
  never a pass; when
  one list is unreadable and the other already holds a secret, the fail
  stands, because nothing in the unread list can undo it, and the
  evidence says the list may be incomplete. Scored by owner decision
  (2026-10-24), knowing it fails many repositories: it is the one setting
  that decides whether locking environments protects anything. **To fix a
  fail**: pick or create an environment that passes
  `sec.environments.branch-restricted`, re-enter each named secret's value
  there (GitHub never shows a stored value, so this step is the user's),
  point the jobs that use it at that environment, then delete the
  repository or organisation copy. What that can break: a job on a pull
  request, or on a branch the environment does not accept, can no longer
  read the secret; give it an environment with a required reviewer or stop
  it needing the secret. For an organisation secret, narrowing
  its repository access to the repositories that need it is the
  alternative. The names (never values) also render under the row as the
  secret list. The report states, every run in which this layer completes,
  that a pushed workflow can
  ask for its own write-scoped token and an OIDC login token, and that a
  credential which cannot push workflow files, but can change code the
  repository's own workflows run, is not checked yet. Which credentials
  can push workflow files is cited from GitHub's docs: the `workflow`
  scope for classic tokens and OAuth apps
  (https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps),
  the "Workflows" permission for GitHub Apps
  (https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
  and for fine-grained tokens
  (https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
