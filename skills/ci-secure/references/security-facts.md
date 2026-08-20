# The security config facts — machine-only inputs, and why each is disjoint from ci-score

## Contents

- [The disjointness census](#the-disjointness-census)
- [What each fact asks, exactly](#what-each-fact-asks-exactly)

---

ci-secure computes nine deterministic, pass/fail configuration facts as
MACHINE-ONLY inputs for a future blended score (the ci-advisor door, not yet
public). They are aggregated `100 × passed / scored` with no weights and no
partial credit (registered 2026-08-03) — but **that aggregate is never a number
the user sees**, and it is NOT ci-score's CI Score (a separate skill's grade).
ci-secure ships no security score anywhere a reader looks. The ten attack
vectors this skill scans for are **findings, never score input** — several
vector detectors are lexical rather than confirmed exploit proofs, and a public
number must not grade a stranger's repo down on an unconfirmed match.

**The denominator is token-dependent, so the aggregate is not comparable
across scans run under different auth.** Two of the nine facts read the GitHub
API. In a repository where every fact applies, a scan with no repository or no
token measures seven of nine and scores `100 × passed / 7`; the same repository
scanned with a token scores over nine. A fact that does not apply to the
repository at all leaves the denominator on top of that (see below), so both
numbers drop by one in a repository whose workflows run no suite.
The unmeasured ids are named in the block's `unmeasured` list and the caveat, so
a consumer — ci-advisor above all — can tell the two apart, but only if it
checks: comparing the raw numbers across differently-authenticated scans
compares different measurements.

**The aggregate is machine-only.** ci-secure's own report and close render
these facts as a pass/fail table and **no number** by design: a hygiene
aggregate labelled "Security score" overclaims what nine config observations
can say, and printed beside a ten-vector scan it read as a contradiction. The
`security_score` block in the findings JSON keeps its shape — same keys, same `fact_id`s, same outcomes, same aggregate; only prose the
report prints was reworded so it stops naming a score the reader never sees.
Consumers bind to the ids, not to the sentences. ci-advisor blends from that
block, and quantification belongs there, where the blend context carries the
denominators.

A fact that cannot be measured — an unscannable workflow file, or, for the two
API-gated facts, no repository and no token — is **unmeasured, never a silent
pass**: it scores nothing, stays in the applicable count as a
named coverage gap, and the block says so.

A fact that **does not apply** is a different state and is treated differently.
"This could not be checked" is a coverage gap; "there is nothing here to check"
is not, and counting it as one would report a gap that does not exist. A
not-applicable fact scores nothing and leaves the applicable count entirely —
the same treatment ci-score gives a check it rules not applicable — while its
row still renders, because a row that quietly vanishes reads as a pass. Its id
is listed in the block's `not_applicable` list, separate from `unmeasured`, so
a consumer never has to guess which of the two it is looking at. Only
`sec.gate.test-failure-fatal` can reach that state today, and only in a
repository whose workflows run no test or lint suite at all.

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
| `sec.codeowners.workflows` | — none | CODEOWNERS is not workflow YAML; no ci-score check reads it. |
| `sec.trigger.fork-code-uncleared` | `ci.trigger.*` (concurrency, cancel, path-filter) | Those checks read `concurrency:` and `paths:` blocks; this fact reads the trigger list plus checkout `ref:`/`repository:` values. No shared YAML key. Also tiered against ci-secure's own P14.9 finding: a bare untrusted trigger passes (the true-of-84% defect, deliberately removed), trigger + attacker-head checkout fails this FACT, and only the full trigger + checkout + execution chain is the P14.9 finding — so the fact and the finding cannot fire on the same edit either. |
| `sec.secrets.no-blanket-inherit` | — none | No ci-score check reads `secrets:` on reusable-workflow calls. |
| `sec.required-checks.skippable` | — none | Branch protection is not workflow YAML; no ci-score check reads the API. The workflow-side edit this fact asks for (add an always-running verdict job that `needs:` the conditional ones) touches no key any ci-score check reads. |
| `sec.fork-approval.effective` | — none | A repository Actions setting, not workflow YAML at all; no ci-score check reads it, and no YAML edit can move it. |
| `sec.gate.test-failure-fatal` | `ci.parallel.test-sharding` | Both look at the jobs that run the suite, and neither edit moves the other: sharding is a `strategy.matrix` on the job, this fact is the `run:` line's exit code and the step's `continue-on-error`. A sharded suite can still swallow every shard's failure, and an unsharded one can be perfectly fatal. Also bounded against the OTHER engine: `continue-on-error` on an upload, report, or notification step is a speed and reliability question that ci-speedup owns, and it does not reach this fact, because only a step whose own `run:` command the allowlist recognises as a suite is in scope — a `uses:` upload action has no `run:` line at all. Residual, disclosed: a reporting step whose command *does* match the allowlist is in scope here too, as is a suite step carrying `continue-on-error: true` that has been red for every run (ci-speedup's OPT68); the bound is on the command, not on the step's stated purpose. |
| `sec.checkout.credentials-scoped` | `ci.checkout.shallow-clone` | Both read checkout steps, but different keys: `fetch-depth` there, `persist-credentials` here — two `with:` entries, two edits. This fact also applies only on untrusted-trigger workflows (excluding the payload-less `fork`/`watch` notification events); shallow-clone applies on PR-gating ones. |

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
  global owner; an extension rule like `*.go` does not cover it). Without one,
  workflow changes merge with the same approvals as any other change.
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
  checks, reusable-workflow jobs, and templated job names all land there — and
  a PASS is a statement about EVERY required check, so ANY context that could
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
  `poetry run pytest`) and leading path (`./mvnw test`). Two things follow,
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
  fact's business either.

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
