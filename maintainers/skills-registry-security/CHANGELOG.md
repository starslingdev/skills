# Changelog — skills-registry-security

Maintainer-only. Not part of any installable skill tree.

## Added

- **2026-08-11** — **The stored snapshot is now read directly, which turns the
  central claim from an inference into an artifact.** `GET
  /api/download/{owner}/{repo}/{skill}` returns the pre-built snapshot the
  registry keeps and the scanners read, as real file contents plus a
  `skillsComputedHash`. Searching a finding's quoted literal in those bytes
  answers "what was the scanner actually handed?" instead of inferring it from
  what the repository no longer contains. Literals are now classified three
  ways — `REAL` (still in current content, the finding stands), `STALE_INPUT`
  (gone from the repository but still in the snapshot, so the input needs
  refreshing), and `PHANTOM` (gone from both, so the scanner is likely serving
  its own cached result) — because those three cases have different remedies
  and asking for the wrong one wastes days. A `STALE_INPUT` verdict prints the
  snapshot hash and the offending paths, which is exactly what a maintainer
  needs to confirm the claim against their own service without access to the
  repository.

- **2026-08-11** — **The skill exists.** `scripts/registry_audit.py` answers
  "is this registry security failure real?" in one command by reading all four
  cached surfaces concurrently, running a throwaway install to capture the risk
  table users actually see, scraping the per-provider finding codes that the
  JSON API does not expose, and grepping every literal a finding quotes against
  both a git ref and the freshly installed tree. A literal absent from both is
  reported as `PHANTOM`, which is the signal that the scan input is stale rather
  than the code being wrong. Doing this by hand took an afternoon; the script
  takes about seven seconds with `--no-install` and about forty with it.

- **2026-08-11** — **The gotchas that cost the most time are written down.**
  SKILL.md records that installing never promptly triggers a re-scan, that the
  audit cache key carries no commit SHA so merging a fix invalidates nothing,
  that the JSON API carries no finding codes, that timestamps arrive in two
  formats on one payload, and — the expensive one — that a re-audit can re-run
  scanners against a stored snapshot, so a fresh `auditedAt` is not evidence
  that current code was read. `references/registry-surfaces.md` carries the URL
  shapes, response schemas, and the `E`/`W` finding-code split.

- **2026-08-11** — **The re-index versus re-audit distinction is load-bearing.**
  Asking for a plain re-audit of a stale snapshot reproduces the same finding,
  so the skill names re-indexing explicitly as the thing to request, and lists
  the three artifacts that make such a request verifiable without access to the
  repository: the finding code with its quoted literal, the commit that removed
  that literal, and an `auditedAt` proving the scan ran afterwards.
