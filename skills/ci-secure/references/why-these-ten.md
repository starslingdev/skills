# Why these ten — the selection criterion behind the critical-only catalog

ci-secure deliberately detects **ten** attack vectors, not the 25 patterns
its catalog once held — the ten kept, and the 15 the rejection record below
names and accounts for. This document is the reasoning, shipped with the skill
so every finding can answer "why is this one of only ten?" — and so every
future "shouldn't we add pattern X?" is tested against a written criterion
instead of instinct. A census test binds the list below to the scanner's
active pattern set: they cannot drift apart.

## Contents

- [The filter](#the-filter)
- [The ten, with their incident grounding](#the-ten-with-their-incident-grounding)
- [The rejection record — what the filter removed, and why](#the-rejection-record--what-the-filter-removed-and-why)
- [What "critical" means here](#what-critical-means-here)
- [Platform mitigations — dated, and scoped to what they actually close](#platform-mitigations--dated-and-scoped-to-what-they-actually-close)
- [Adding a pattern — the mechanical checklist](#adding-a-pattern--the-mechanical-checklist)

## The filter

A pattern is in the catalog **only if it describes a complete outsider →
compromise chain**, judged by three tests, all required:

1. **The outsider-chain test.** Someone with **no access to the repo** — an
   account that can open a fork PR, craft a branch/title, or publish an
   upstream artifact — can reach a concrete compromise: code execution
   holding a write token, secret theft, or poisoning of what the repo ships.
   Not "this makes a breach worse if one happens" (blast radius); the chain
   itself must start at outsider and end at compromise.
2. **The incident test.** The vector class has actually happened in public —
   each entry below names its incidents. Nothing on the list is theoretical.
3. **The same-day-fix test.** A maintainer can close the finding the day
   they read it — change a trigger, remove an interpolation, re-pin a SHA,
   drop a permission line. Nothing that requires a hardening program.

Notably, the filter is NOT "keep everything labeled HIGH": three catalog-HIGH
patterns failed it, and two catalog-MEDIUM patterns passed it (see the
rejection record).

## The ten, with their incident grounding

| # | Pattern | Chain | Public incidents |
|---|---------|-------|------------------|
| 1 | P14.10 | Template injection in `run:` — attacker text (PR title, branch name) pasted into a shell | nx / s1ngularity (2025); elementary-data (2026); Ultralytics (2024) |
| 2 | P14.9 | Fork code executed with privileges — untrusted trigger + head checkout + execution | GitHub Security Lab "pwn request" writeups; Trivy round 1 (2026) |
| 3 | P14.7 | Fork-writable shared cache — poisoned cache consumed by trusted runs | TanStack (2026); Ultralytics (2024) |
| 4 | P14.11 | Impostor / unreachable action SHA — a pin the canonical repo never contained | tj-actions/changed-files (2025); Chainguard "imposter commits" research |
| 5 | P14.14 | Whole-context secrets dump — `toJSON(secrets)` into logs/env | tj-actions payload behavior (memory scraping, 2025) |
| 6 | P14.15 | Attacker-controlled `$GITHUB_ENV` / `$GITHUB_PATH` write — hijacks later steps | GitHub Security Lab environment-injection writeups |
| 7 | P14.18 | `pull-requests: write` on an untrusted trigger — outsider's event holds a write token | elementary-data (2026) — forged release via default-write token |
| 8 | P14.19 | Credential files in caches/artifacts — keys fetchable by other jobs or the public | Trivy round 2 (2026) — attacker swept the runner for exactly these credential files (SSH keys, cloud creds, Kubernetes tokens); caching or uploading them extends that exposure |
| 9 | P14.24 | Unverified remote code execution — a piped installer, or a git tree fetched at a mutable ref and executed | Codecov bash-uploader breach (2021) — the PIPED-INSTALLER arm only; the mutable-fetch arm has no named breach of its own (see below) |
| 10 | P14.25 | Dependency install scripts executing in a privileged job — a compromised upstream package runs code where the secrets are | nx / s1ngularity install-script payload (2025); Miasma / `@redhat-cloud-services` (2026); GitHub's July 28 2026 supply-chain post |

Full incident citations live in the catalog's
[Reference incidents](security-patterns.md#reference-incidents) section.

### P14.24's two arms, and which one the incident evidences

P14.24 detects two shapes, and only one of them is carried by the incident
cited beside it. Saying so is the point of the incident test — an entry that
lets a second shape ride quietly on the first shape's breach has not passed it.

- **The piped installer** (`curl … | bash` and friends) is evidenced directly.
  The Codecov bash-uploader breach (2021) IS this shape: an altered script
  served from the vendor's own host, piped into a shell by every job that used
  it, exfiltrating the CI environment. That is the citation's whole scope.
- **The mutable fetch** (a git tree cloned at a branch, tag, `HEAD`, or an
  abbreviated commit, and then executed out of) is **not** evidenced by
  Codecov, and this document does not claim a breach for it. It is in the
  catalog on the strength of the SHARED TRUST MODEL rather than a named
  incident: both shapes execute code the repo never pinned, re-resolved live on
  every run, at full job privilege — and a branch or tag is *designed* to move,
  so it needs no host compromise at all, only push access on the other side.
  The mitigation is identical (pin to a full 40-character commit), which is why
  it is one vector with two arms rather than two entries.

If a public incident of the mutable-fetch shape is later cited, it belongs in
the catalog's Reference incidents section and in this row — until then, the
asymmetry stays stated rather than smoothed over.

### The tenth vector, evaluated against the three tests

P14.25 (dependency install scripts executed in a privileged job) was admitted
2026-08-06. Its evaluation, stated in full so the admission is auditable:

1. **Outsider-chain test — passes.** The outsider capability the filter names
   explicitly is "publish an upstream artifact". An attacker who takes over a
   maintainer's npm account, lands a typosquat, or compromises a transitive
   dependency publishes a version whose `preinstall`/`install`/`postinstall`
   script runs automatically the next time CI installs dependencies. No repo
   access is needed at any point. The chain ends at compromise **only when
   the job the install runs in holds something to steal** — repo secrets or a
   write-scoped token — which is why the detector requires that payoff rather
   than flagging every install (see below).
2. **Incident test — passes.** The s1ngularity-class payloads (2025) executed
   from install scripts and harvested credentials from the machines that ran
   them; the June 2026 Miasma compromise of the `@redhat-cloud-services`
   namespace, and its `binding.gyp`-based follow-up, used the same execution
   point. GitHub's [July 28 2026 post](https://github.blog/security/supply-chain-security/disrupting-supply-chain-attacks-on-npm-and-github-actions/)
   describes the class as the one it changed npm's defaults to disrupt.
3. **Same-day-fix test — passes.** Adding `--ignore-scripts` to the install
   command is a one-line edit a maintainer can make the day they read the
   finding. Where a legitimate build step depends on a lifecycle script, the
   same-day move is to re-enable it explicitly for the packages that need it
   (npm v12's approval list) or to run the script-bearing install in a
   separate job that carries no secrets — still a workflow edit, not a
   hardening program.

The severity is MEDIUM for the same documented reason as P14.24: the vector's
potency depends on a live condition outside the repo (whether a dependency in
the tree is or becomes malicious), not on an in-repo defect. Criticality is
membership, so it renders as a finding all the same.

## The rejection record — what the filter removed, and why

The old catalog's other patterns fell into two classes:

**Blast-radius patterns (including three catalog-HIGHs).** Real weaknesses
that make a breach worse *if an attacker is already in*, with no chain that
starts at outsider: workflow-scoped OIDC tokens (P14.8), long-lived cloud
credentials where OIDC exists (P14.12), cache steps in release jobs (P8.3).
These are exactly the findings a maintainer reads and cannot act on that
day — the noise the descope removed. ONE of them is already a pass/fail fact
in the CI Score: workflow-scoped OIDC tokens (P14.8) is ci-score's
`ci.security.scoped-id-token`. The other two are scored nowhere.

**Presence-shaped hygiene observations.** A defense being absent is not an
attack being possible: missing `permissions:` blocks (P5.5), unpinned
versions (P5.1), workflow-level permission scoping (P14.3), no CODEOWNERS on
workflows (P14.20), no scanner installed (P14.5), tag-pin audit hygiene
(P14.2), release-environment gating (P8.4), checkout credential persistence
(P14.16), `secrets: inherit` (P14.17), broad artifact upload (P14.22),
malformed `if:` (P14.23), and the manual-review checklist entries (P14.6).
P14.23's rejection still stands, and the scanner naming a dead
`github.event.*` field beside an existing finding does not reconsider it:
that raises no finding and changes no count — it tells the reader whether
the gate they can already see, on a vector already in the ten, is capable
of restricting anything.
These either became scored config facts — where a one-line
pass/fail is the honest weight for a presence fact — or were dropped.

Two catalog-MEDIUM patterns **passed** the filter and stayed: the fork-code
trust chain (P14.9 — its severity was raised to HIGH with the rebuilt
detector) and unverified remote code execution (P14.24 — the Codecov chain,
which is the piped-installer arm, is real; the mutable-fetch arm shares that
chain's trust model rather than a breach of its own. Either way the potency
depends on a live condition, which is why its catalog severity stays MEDIUM
while it remains a critical finding by membership).

The removed patterns are not shipped with the skill; re-admitting any entry
means passing this document's three tests and updating the census.

## What "critical" means here

**Criticality is membership in this list.** The catalog's `severity` field
still records each unfixed attack's potency (HIGH/MEDIUM), but the report
does not tier, top up, or truncate: every finding from these ten renders,
every one carries its attacker scenario, and zero findings is a first-class
result. One of the ten (P14.11) needs the GitHub API; when it cannot run,
the report says so explicitly — a skipped check is never a silent pass.

## Platform mitigations — dated, and scoped to what they actually close

Several of these vectors were partly mitigated by GitHub platform changes in
mid-2026. The catalog records each change with its date and the residuals
GitHub itself enumerates, on the entry it affects (P14.7, P14.9, P14.18,
P14.25). A platform default that closes the classic entry on github.com does
not retire the vector — Enterprise Server, third-party backends, opt-outs and
pinned action versions keep it live — so the detectors and severities are
unchanged, and the finding says which of those residuals applies to the
reader.

## Adding a pattern — the mechanical checklist

A candidate that passes all three tests above is a deliberate catalog change,
not a drift; the census test (`tests/test_census_why_these_ten.py`) fails any
catalog/doc mismatch. Mechanically, in ONE change:

- append a `### Pxx.y` section to `security-patterns.md` with a METADATA
  block (schema in the catalog's `## METADATA schema` section) and the five
  prose markers — `**TL;DR.**`, `**What an attacker can do.**`,
  `**Anti-pattern**:`, `**Fix recipe**`, `**Risk of the change.**`.
  `tests/test_census_why_these_ten.py` pins all five, and `**Anti-pattern**:`
  is pinned WITH its trailing colon;
- add a fixture the detector fires on, at
  `tests/fixtures/dot-github/workflows/pXX_Y_*.yml.fixture` — the
  `.yml.fixture` suffix and `dot-github/` directory keep fixtures out of the
  scanner's own workflow scans and off registry scanners — and register its
  hash in `tests/fixtures/cloak-manifest.json`, or the cloak-prune step drops
  it;
- update the list and rejection record in THIS document in the same change.
