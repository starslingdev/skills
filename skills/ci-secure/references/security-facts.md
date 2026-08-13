# The security config facts — machine-only inputs, and why each is disjoint from ci-score

ci-secure computes six deterministic, pass/fail configuration facts as
MACHINE-ONLY inputs for a future blended score (the ci-advisor door, not yet
public). They are aggregated `100 × passed / scored` with no weights and no
partial credit (registered 2026-08-03) — but **that aggregate is never a number
the user sees**, and it is NOT ci-score's CI Score (a separate skill's grade).
ci-secure ships no security score anywhere a reader looks. The ten attack
vectors this skill scans for are **findings, never score input** — several
vector detectors are lexical rather than confirmed exploit proofs, and a public
number must not grade a stranger's repo down on an unconfirmed match.

**The aggregate is machine-only.** ci-secure's own report and close render
these six facts as a pass/fail table and **no number** by design: a hygiene
aggregate labelled "Security score" overclaims what six config observations can
say, and printed beside a ten-vector scan it read as a contradiction. The
`security_score` block in the findings JSON keeps its shape — same keys, same `fact_id`s, same outcomes, same aggregate; only prose the
report prints was reworded so it stops naming a score the reader never sees.
Consumers bind to the ids, not to the sentences. ci-advisor blends from that
block, and quantification belongs there, where the blend context carries the
denominators.

A fact that cannot be measured (an unscannable workflow file) is **unmeasured,
never a silent pass**: it scores nothing, stays in the applicable count as a
named coverage gap, and the block says so.

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
- **`sec.checkout.credentials-scoped`** — on untrusted-trigger workflows,
  every `actions/checkout` sets `persist-credentials: false`. GitHub's default
  persists the token into `.git/config`, where attacker-influenced later steps
  can read it. Trusted-trigger workflows are not this fact's business — nor are
  the payload-less `fork`/`watch` notification events, which carry no
  attacker-influenced execution that could read a persisted token.
