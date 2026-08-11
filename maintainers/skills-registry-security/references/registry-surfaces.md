# Registry surfaces reference

Exact URL shapes, response schemas, and finding-code classes behind
`scripts/registry_audit.py`. Read this when the script's parsing breaks — a
registry redesign changes page markup without notice — or when adding a
provider.

## Contents

- Surface A: the JSON audit API
- Surface B: the install-path audit endpoint
- Surface C: the rendered skill page
- Surface D: the per-provider detail page
- Surface E: the stored snapshot (the scan input)
- Finding-code classes
- Provider slugs and display names
- Why the surfaces disagree
- Verifying a literal
- Comparing against a project's own scan

## Surface A: the JSON audit API

```
GET https://www.skills.sh/api/v1/skills/audit/{owner}/{repo}/{skill}
```

Returns one entry per provider:

```json
{
  "id": "owner/repo/skill",
  "audits": [
    {"provider": "Gen Agent Trust Hub", "slug": "agent-trust-hub",
     "status": "pass", "riskLevel": "SAFE",
     "auditedAt": "2026-08-11T19:44:35.075Z", "summary": "...",
     "categories": ["PROMPT_INJECTION", "..."]}
  ]
}
```

`status` is one of `pass`, `fail`, `warn`. `summary` is prose plus an issue
count — **it never contains finding codes**, which is why Surface D exists.

A skill that has never been audited returns a not-found body stating that
audits are generated after a skill is installed for the first time. That is the
only documented trigger, and it fires on first indexing, not on later installs.

Requesting `/{provider}` as a fourth path segment returns HTTP 400; the API
accepts only the three-segment form. A `?provider=` query parameter is accepted
but ignored — the full provider list comes back regardless.

## Surface B: the install-path audit endpoint

```
GET https://add-skill.vercel.sh/audit?source={owner}/{repo}&skills={slug}
```

This is what the CLI reads at install time, and it is a **different cache** from
Surface A. Response is keyed by skill slug, with abbreviated provider keys:

```json
{"ci-secure": {
  "ath":    {"risk": "safe",     "analyzedAt": "..."},
  "socket": {"risk": "medium", "alerts": 0, "score": 79, "analyzedAt": "..."},
  "snyk":   {"risk": "critical", "analyzedAt": "..."}}}
```

Note `analyzedAt` here versus `auditedAt` on Surface A, and `ath` versus the
`agent-trust-hub` slug. The timestamps are the reliable join key.

This endpoint has served results a full day older than Surface A. Since it
drives what users see, treat it — not the API — as the answer to "what does a
user see today?".

## Surface C: the rendered skill page

```
GET https://www.skills.sh/{owner}/{repo}/{skill}
```

HTML. Carries a `Security Audits` block with a `Pass`/`Fail`/`Warn` badge per
provider, plus `Installs` and `First Seen`. Independently cached again, so it
can disagree with both A and B.

Flatten it by stripping `<script>`/`<style>`, then all tags, then collapsing
whitespace. The badges appear as `<provider name> <verdict>` pairs between the
literal strings `Security Audits` and `Browse All`.

## Surface D: the per-provider detail page

```
GET https://www.skills.sh/{owner}/{repo}/{skill}/security/{provider-slug}
```

**The only place finding codes and their prose live.** Structure after
flattening:

```
... {skill} {Pass|Fail|Warn} Audited by {Provider} on {date}
Risk Level: {LEVEL}
Full Analysis
  {SEVERITY} {CODE}: {title}. {body prose, which quotes offending literals}
  ...
Issues ( {n} ) {CODE} {SEVERITY} {title} ...
Audit Metadata Risk Level {LEVEL} Analyzed {timestamp} Issues {n}
```

Parse between `Full Analysis` and `Audit Metadata`. Finding codes match
`[EW]\d{3}`. Literals the scanner objected to appear inline in the prose,
usually parenthesized — extract URLs with a permissive pattern and drop any
pointing at the registry's own domain.

The page carries **no file paths or line numbers**. A finding is therefore
identified only by its code plus the literal it quotes, which is exactly why
grepping that literal is the decisive test.

## Surface E: the stored snapshot (the scan input)

```
GET https://www.skills.sh/api/download/{owner}/{repo}/{skill}
```

The single most useful surface, and the least obvious. Returns the pre-built
snapshot the registry keeps for fast installs — and the same content the
scanners read:

```json
{"files": [{"path": "SKILL.md", "contents": "..."}, ...],
 "hash": "a2f6d76e…"}
```

`hash` is the `skillsComputedHash`. Note the bare `skills.sh` host may fail to
connect from some networks; use `www.skills.sh`.

Because this returns **actual file contents**, a finding's quoted literal can be
searched directly in the bytes the scanner saw. That converts "the string is
gone from our repository, so the scan must be stale" — an inference — into "the
string is still in your snapshot, here is its hash and the four files that
contain it", which a maintainer can verify against their own service without
access to the repository.

Dating a snapshot is easy when the skill keeps a dated changelog: read
`CHANGELOG.md` out of the snapshot and compare its newest entry against the
repository's. A snapshot whose newest entry predates the fix commit is
conclusive.

Installing does not necessarily consume this snapshot — the CLI may clone the
default branch instead — so a clean installed tree and a stale snapshot coexist
happily. Do not treat the install as evidence about the audit.

## Finding-code classes

Snyk Agent Scan splits its catalog into two classes, and conflating them causes
bad triage:

| Class | Meaning | How to treat it |
|-------|---------|-----------------|
| `E###` | Critical. Drives the overall risk level. | Gate on these. One is enough to turn a skill red. |
| `W###` | Warning. | Usually an accurate description of inherent behavior. Accept with a rationale, or fix if cheap. |

Two that recur for repository-auditing skills:

- **`E005` — suspicious download URL in skill instructions.** Fires on a literal
  http(s) URL whose *path* ends in `.sh`, `.ps1`, or `.bash`, whether or not it
  is executed. Teaching material showing a `curl … | bash` example trips it. A
  reserved or example host does not clear it: the rule keys on the shape, not
  the domain. Replacing the address with a placeholder does clear it.
- **`W011` — third-party content exposure.** Fires when a skill ingests
  outsider-authored text at runtime. For a skill that audits other people's
  repositories this is simply true, and it fires even when the ingested text is
  already wrapped in explicit untrusted-content delimiters.

The catalog changes without any commit of yours, so a skill can go red with no
local change. A scheduled re-run of your own scanner is what notices.

## Provider slugs and display names

| Display name | Surface A slug | Surface B key | Detail-page slug |
|---|---|---|---|
| Gen Agent Trust Hub | `agent-trust-hub` | `ath` | `agent-trust-hub` |
| Socket | `socket` | `socket` | `socket` |
| Snyk | `snyk` | `snyk` | `snyk` |

Gen's verdicts are model-generated prose rather than a fixed rule catalog, so it
can reverse itself between scans on unchanged content. Socket reports an alert
count and a score. Snyk reports the coded findings above.

## Why the surfaces disagree

Each surface caches independently, and none of them keys on a commit SHA. The
practical consequences:

- Pushing a fix invalidates nothing.
- A refresh can reach one surface hours or a day before another.
- A re-audit may re-run the scanner against the **stored snapshot** rather than
  the current default branch, producing a new `auditedAt` on a finding derived
  from deleted content. This is the single most confusing behavior in the whole
  system, and the reason a fresh timestamp proves nothing on its own.

Re-indexing (refreshing the stored snapshot) and re-auditing (re-running
scanners over whatever is stored) are separate operations. A stale-content
finding needs the former.

## Verifying a literal

For each literal a finding quotes, check two corpora:

1. **The git ref** — `git grep -I -l -F "<literal>" origin/main`. Covers every
   tracked file, so it answers "is this in the published default branch?".
2. **The freshly installed tree** — `grep -rIlF "<literal>" <install-dir>`.
   Covers exactly the bytes the registry ships, which can differ from the
   repository if the install excludes paths.

Absent from both ⇒ the finding cannot describe current content. Present in
either ⇒ treat the finding as real and decide whether to fix or accept it.

Search for the literal string, not a regex built from it, and search
case-sensitively. Matching a hand-built pattern instead of the quoted literal
reintroduces exactly the false-positive class this test exists to eliminate.

## Comparing against a project's own scan

If the project runs the same scanner in CI, that run is the strongest available
counter-evidence, because it removes any argument about scanner behavior and
isolates the input. Useful things to extract:

- Whether the gate distinguishes critical from warning codes; a green build
  usually means "no critical codes", not "no findings".
- Whether the workflow proves its gate can fail before trusting a pass. Without
  that, a green result may be vacuous.
- The run's timestamp and head SHA, so it can be quoted against the registry's
  `auditedAt`.

Agreement on warning codes combined with disagreement on a critical code is a
strong signature of stale input: the finding tied to live behavior reproduces,
while the one tied to deleted content does not.
