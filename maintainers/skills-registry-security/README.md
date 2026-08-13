# skills-registry-security

Maintainer-only. Deliberately outside `skills/`, so it never ships to end users.

Answers one question quickly: **a published skill shows a failing security
audit — is that real, or is the registry auditing stale content?**

The security analysis itself belongs to the registry's providers (Gen Agent
Trust Hub, Socket, Snyk). This tool triages *their verdict*: it decides whether
a finding describes the skill as it is today, and if not, produces the evidence
that says so.

## Running it

```bash
python3 maintainers/skills-registry-security/scripts/registry_audit.py \
    starslingdev/skills/ci-secure
```

About 6 seconds with a live install, ~1.4s with `--no-install`. Everything is
read-only: HTTP GETs, a throwaway install into a temp directory, and greps over
a local checkout. Nothing is written to the repository and nothing is POSTed to
the registry.

## What one run does, in order

1. **Fans out concurrently** across every cached surface: the JSON audit API,
   the install-path cache the CLI actually reads, the rendered page badges, and
   the three per-provider detail pages.
2. **Scrapes the finding codes** (`E005`, `W011`, …) from those detail pages.
   The JSON API returns only prose like `"CRITICAL · 2 issues"`, so the pages
   are the only place the actual findings exist.
3. **Runs a real `npx skills add`** in a temp directory and captures the
   "Security Risk Assessments" table verbatim — what a user actually sees.
4. **Fetches the stored snapshot** (`/api/download/...`), the content the
   scanners read, along with its hash.
5. **Searches every literal a finding quotes** against three corpora: the git
   ref, the freshly installed tree, and the snapshot.
6. **Classifies each literal and prints a verdict:**
   - `REAL` — still in current content. The finding stands; fix it or accept it
     with a written rationale.
   - `STALE_INPUT` — gone from the repository but still in the snapshot. Nothing
     to patch. Prints the snapshot hash and offending paths, which is what makes
     an escalation verifiable without access to your repository.
   - `LAGGING` — gone from both, and the audit predates the current snapshot.
     The ordinary post-fix state: the fix worked and the badge has not caught
     up. Wait ~a day; do not escalate.
   - `PHANTOM` — gone from both, and the audit has already read this snapshot.
     Only then is the scanner serving its own cached result, so a re-index
     alone may not clear it.
   - `PHANTOM_OR_LAGGING` — gone from both, with no `--snapshot-changed-at` to
     tell those two apart. It declines to guess rather than accuse the scanner.
   - `UNVERIFIED` — no corpus to compare against, so it declines to guess.

It also reports disagreement between surfaces, which is common and usually
resolves on its own as caches catch up.

## What ships with it

| Path | What it is |
|---|---|
| `SKILL.md` | The agent-facing contract, plus the gotchas that cost the most time |
| `scripts/registry_audit.py` | The engine. stdlib only |
| `references/audit-pipeline.md` | How the registry actually works: the twenty-second diagram, an annotated real timeline, the precondition check, observed timings, tested dead ends, an evidence log, and how to gate CI so this cannot recur |
| `references/registry-surfaces.md` | Exact URL shapes, response schemas, the `E`/`W` finding-code split |
| `evals/evals.json` | Four scenarios |
| `tests/` | Offline tests, wired into the repo suite |

## Why it exists

The investigation this came from took about six hours and produced four wrong
conclusions along the way — including "installs never trigger indexing," which
came from an experiment invalidated by a silently suppressed telemetry beacon.

The script reproduces the correct answer in seconds. The references record both
the working model and the mistakes, so the next person does not repeat them.
The most important single line in there is the precondition check: an install
whose beacon never fired looks exactly like a registry that ignores installs,
and confusing the two sends you hunting a bug that does not exist.
