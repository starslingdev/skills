# Changelog — ci-score

All notable changes to this skill are recorded here (Keep-a-Changelog style;
the skill is unversioned; the CI Score registry it carries is versioned
separately as `ci-score-vX.Y.Z` inside `references/ci-score-spec.json`).

> **Note on PR/issue numbers.** Entries below reference the pull requests and
> issues of the skill's pre-public development archive, which is not part of
> this repository's history. The numbers are kept for the maintainers' audit
> trail; they are not links you can follow here.

## [Unreleased]

- **2026-07-31** — **Public launch.** Ported the finished skill from the
  pre-public development archive into the public `starslingdev/skills` repo at
  CI Score registry `ci-score-v0.1.3`. The port is the launch: it moves a
  completed, dogfooded skill (frozen scorer + rubric, ranked recommendations,
  close/kickoff protocol, offline controls) unchanged. The graded third-party
  calibration corpus and the internal build/launch specs deliberately stay in
  the development archive and are not part of this repository; a new
  `tests/test_ci_score_install_surface.py` invariant keeps that maintainer-only
  data out of the installable `skills/ci-score/` tree.

- **Close/kickoff protocol extracted to a reference doc** (2026-07-30,
  behavior-neutral). `SKILL.md`'s `## Close contract` section (banner rules,
  stake kinds, menu shapes, kickoff protocol, informed consent,
  ship/post-ship/post-merge rules, PR requests, refusal closes) moved verbatim
  to `references/close-contract.md`; `SKILL.md` dropped from 474 to 233 lines
  (230 for the bare extraction, +3 for the review clarification below), back
  under the 500-line soft ceiling with headroom. In its place, a compact
  `## Close contract — invariants` section keeps the non-negotiables at point
  of use — banner-verbatim/no-fabricated-score, close-text-then-question-same-turn,
  menu shapes, save-pick-only-writes-the-report, one-rec-per-approval /
  stop-at-the-diff — each with its live-miss anchor, plus an imperative
  instruction to read the full protocol before every close (text position is
  behavior here; burying these regressed live runs before). One additive rule
  while extracting (owner-sanctioned): the ask-before-network norm now also
  covers apply-time READS — any network read during an apply or its
  investigation (`git ls-remote` for SHAs, `gh api` reads like branch
  protection) is disclosed in the offer or at the moment of need (observed live
  2026-07-30: a branch-protection read during a path-filters investigation was
  protective but undisclosed). Coupled tests repointed to the reference
  (`tests/test_close_contract.py` honesty pin; the banner/delta-bar formula
  re-derivation in `tests/test_ci_score_contract.py`); no rubric or scorer
  behavior changed. Two review clarifications on top of the verbatim move:
  (1) step 4 of the Flow now points explicitly at
  `references/close-contract.md` as the first thing to read at the close (the
  invariants section is the point-of-use summary, not the whole protocol), and
  a new `test_close_contract.py` guard pins that entrypoint handoff — the link,
  the read instruction, and the invariants must stay in `SKILL.md` or the suite
  goes red, so the relocated protocol can't become present-but-undiscoverable
  (Greptile P2). (2) One move-introduced dangling pointer corrected inside the
  reference: "the interaction contract above" → "SKILL.md's interaction
  contract", since that contract stays in `SKILL.md` and nothing is "above" it
  in the new file (the sole deviation from a byte-verbatim body; the sentence's
  meaning is unchanged and its mechanics are restated inline).

- **A reported merge starts a fresh banner-led round** (owner dogfood
  2026-07-30): after the user merged the shipped PR, the session
  re-scored the merged base but buried the new score in prose and jumped
  straight to a fix menu — the fresh banner only appeared on manual
  re-invocation. Now: reporting the merge — or, post-merge, asking for a
  fresh score or what to do next — triggers a full first close on the
  merged base (fresh collector banner, updated recommendations,
  first-close menu, no ship option since nothing is applied yet).
  Review-loop scoping on the same ruling: an incidental post-merge
  question (a status check, recalling the earlier number, explaining a
  finding) is answered on its own terms, not hijacked into a surprise
  re-grade; and the banner-led rule governs a re-score of a real
  committed/merged base presented as the current standing — the in-loop
  post-apply re-score against the dirty working tree keeps its before →
  after delta bar and is never turned into a fresh banner.

- **Post-apply re-offer leads with "Commit this branch and open a PR"**
  (owner 2026-07-30, live apply run on the docs repo): after a fix was
  applied and re-scored, the menu dropped the user straight into the
  next recommendation — no visible path to review/land the work, which
  is not how a developer works. The stop-at-the-diff rule stands, but
  the explicit ask it requires is now a first-class menu option: slot 1
  post-apply = commit + push + open a PR (body per the PR-request rules:
  CI Score before → after from stamps, applied recs, caveats; never
  merge), then ONE next-fix slot, a different recommendation, save last.
  An unshipped session's close names the branch so the work is findable.
  Review-loop reconciliations on the same ruling: a ship pick ENDS the
  loop (close on the opened PR, no silently appending further commits to a
  branch whose PR is already open); the "stop-at-the-diff" gotcha no longer
  says "the user commits" flatly — the agent commits/pushes/opens the PR on
  the ship pick (still never merge), while the apply "yes" alone still
  authorizes nothing downstream; and the save-pick's terminal description
  now also names the branch when a fix was applied but not shipped.

- **Two owner rulings from the docs-repo dogfood (2026-07-30)**: (1) the
  close menu is now multi-slot, adopting ci-speedup's shape — up to two
  fix slots ranked best-first (a one-edit-closes-both bundle counts as
  one slot; each slot carries its own consent scope and risk note), then
  "a different recommendation", then the verbatim save option last (the
  platform question tool caps at 4 options). A live run improvised
  exactly this shape and it read well. (2) Ranking tie-break: among
  recommendations equal on impact AND risk, the stake kind now decides —
  security, then reliability, then speed/cost — instead of arbitrary
  registry position (the two high-impact/low-risk security checks tied
  with the concurrency/trigger checks and sorted below them purely by
  registry order — the `ci.hygiene.*` family never shares that tie group).
  Impact and risk still dominate;
  renderer-only (OD-L5), no registry change, verifier parity automatic
  (it recomputes with the same rank function).

- **Sandbox-aware gh probe in the clone path** (2026-07-30, mirroring
  ci-speedup's gh-gate fix): step 0's `gh repo view` access check can
  false-fail in restricted agent shells (Codex) where keyring credentials
  are unreachable — retry with host access before reading a failure as
  no-access. (A live Codex ci-speedup run told a logged-in user their
  auth had expired off exactly this probe shape.)

- **Codex question tool: attempt-first, mirroring ci-speedup** (owner
  2026-07-29): the interaction contract had talked itself out of the
  Codex question tool ("expect the plain-message branch to carry most
  Codex runs") — so a live Codex run never attempted the call and went
  straight to the printed list. Now worded exactly like ci-speedup's
  proven contract: name both identifier forms, check for the tool and
  attempt the call first, fall back to the plain-message option list
  only when the tool is genuinely absent or the call fails. Also
  corrected the provenance note: the whole interaction contract mirrors
  ci-speedup's (it was mislabeled as partly ci-score's own extension).

- **Close completion = the question call, never the text (regression fix
  #2, quasar dogfood 2026-07-29)**: a second live close ended with no
  question at all — its text referenced "the last option below" and the
  turn ended, leaving the user to type a pick freehand. Root cause: the
  contract itself scripted the agent to narrate the menu in prose (the
  report note said "the last option below saves it"), so a finished-
  sounding text read as a finished close. Restructured: step 4 is
  explicitly two parts in one turn (close text, then the question call);
  the options may appear ONLY inside the question; prose references to
  "the option below" are banned; and the close is defined as unfinished
  until the question is asked. Review-loop tightening on the same fix: the
  completion rule now explicitly covers the in-loop "next?" re-offers (not
  just the first close, or miss #2 recurs one loop later); the step-4 close
  text is stated to be a separate ordinary message, never packed into the
  question call; and the "end by asking" mandate is reworded so it forbids
  a message that leaves the choices unasked without forbidding the
  sanctioned no-tool plain-message fallback (that fallback message IS the
  question and does complete the close). Made explicitly Codex-safe (owner
  directive, parallel to ci-speedup's interaction contract): step 4(b) is
  platform-conditional with ONE definition — the structured-question tool
  where one exists (AskUserQuestion on Claude Code), and where none does
  (most Codex runs) the question IS the numbered option list printed as the
  message's FINAL block (same options, same order, save last and verbatim),
  the turn ending on it. The prose-menu ban is scoped to menu NARRATION
  ahead of the question, expressly NOT that final option list. Cites a
  third live miss: a Codex run that closed on a bare "Apply recommendation
  #4 (job timeouts) now?" with no options — the same optionless close on
  the tool-less platform.

- **Best-practices audit follow-ups (2026-07-29)**: contract prose
  standardized on "score" (the noun "grade" survives only in the
  description's user-utterance triggers and the stamp's literal `grade`
  field; the letter band was already never rendered); the methodology's
  inline link to the spec JSON replaced with plain text so no reference
  file links to another reference file (references stay one level deep
  from SKILL.md). From a /skills-best-practices audit of the branch;
  tests and evals pinning the old wording updated in the same pass.

- **Close-offer mandate front-loaded (regression fix, quasar dogfood
  2026-07-29)**: a live run delivered a contract-perfect close except it
  ended in a typed "Want me to apply #1?" — no structured options, no
  save choice. The ONE-structured-question mandate had been buried
  mid-sentence fifteen lines into the close paragraph by successive
  additions (instruction decay by burial — the same failure mode the
  review-loop skill documents for spawn prompts). The mandate is now its
  own bold rule at the point of action: ending the close on a bare,
  optionless offer is a violation even when the content is right — what is
  owed is the fixed options + the save choice, delivered through the
  structured-question tool where one exists, and otherwise through the
  contract's plain-message fallback carrying the same options in the same
  order (the fallback stays sanctioned — the ban is on dropping the
  options, not on prose per se).

- **Close recs name their stake KIND, not just size** (owner dogfood
  2026-07-29, quasar run): "high impact / low risk" reads identically for
  a security exposure and a speed win, so the close's per-recommendation
  line now leads with the stake kind in one plain word, derived
  mechanically from the check_id family (`ci.security.*` → security,
  `ci.hygiene.*` → reliability/cost, others → speed/cost — never invented
  per run). The stake kind stays qualitative — a category and its
  mechanism, never a saved-seconds/runner-minute/dollar figure — so it
  cannot drift into the measurement the Boundaries ban forbids. The
  report-exists note also now says the report explains why each practice
  matters. Contract + evals only.

- **Interaction contract: confirm the target first, structured questions at
  both ends** (owner 2026-07-29, adopting the ci-speedup convention): (0) a
  new Flow step 0 confirms the scoring target before the collector runs —
  default is the detected checkout (`<owner/repo>` or path), one structured
  question, "a different repo or path" as the alternative; load-bearing
  because the report file lands in the scored repo's root, so a wrong
  working directory writes a file into the wrong project. (4) The kickoff
  offer (and each later in-loop approval) is now ONE structured question via
  the platform's question tool where one exists (AskUserQuestion on Claude
  Code; Codex's user-input request tool; a single plain message with the
  same fixed options only when neither exists): apply the named
  recommendation (option carries the informed-consent scope + risk note) /
  a different recommendation / `None, just save the report (.md)` last and
  verbatim, no re-offer after a save pick. evals.json close-contract
  expectation updated to match. Contract-only — no script changes.
  The report itself is now **opt-in like ci-speedup's** (owner dogfood
  2026-07-29): rendered and verified in the scratch workdir, written into
  the target repo as `./ci-score-report.md` ONLY when the user picks the
  save option — no other pick writes it, and no repo path is shown before
  that pick. Side benefit: an unsaved report no longer leaves the
  untracked file that flagged the next run's provenance `-dirty`.
  Review-loop refinements folded into the same contract: the report is now
  re-rendered after **every** re-score (not only when the pass/fail stamp
  flips) so a save pick always copies the freshest report; an explicit
  out-of-band save request mid-loop is honored without ending the loop
  (only the structured "None, just save" pick is terminal); and step 0
  falls to a plain-message / name-the-target ask when the working directory
  is not a git checkout (no default to confirm).

- **Toolkit-review hardening round (2026-07-29)**: four-agent toolkit pass on
  the PR found no critical issues; four small fixes folded in. (1) The
  practice-page census test now carries the two post-freeze pages and asserts
  the coverage note accounts for all three unmapped pages by name — the exact
  rot OD-CS21 corrected can no longer recur silently. (2) A malformed stamp
  (value present, tallies missing) prints NO banner instead of a
  contradictory "0 pass · 0 fail" box. (3) The HEADER invariant now also runs
  on collection refusals that carry a commit (no_parseable_workflows renders
  a real header), red-proven. (4) SKILL.md banner phrasing corrected to
  `<owner/repo>`. Pushed back with rationale: a verify guard for the
  terminal banner (unit tests already re-derive its math; verify_report
  guards report artifacts, and the banner is collector stdout copied
  verbatim), and a stale pre-existing archive line in an old decision-log
  entry (point-in-time record, out of scope).

- **Registry footnote corrected in place (OD-CS21, owner 2026-07-29; no
  spec_version bump)**: the spec's `practice_coverage_note` claimed exactly
  one published best-practices page lacked a scored check; two pages shipped
  after it was written. Corrected to account for all three
  (keep-advisory-checks-non-blocking - no check, branch-protection
  semantics; replace-fixed-sleeps-with-polling - no check, step-content
  judgment; bound-job-timeouts - scored, null slug binding, renderer
  supplies the link). Metadata-only: zero computation, stamps, or
  calibration rows affected, which is why the version number does not move
  (rationale recorded in the spec's own decision_log). Surfaced by the
  an internal-dev-repo session working against the pinned vendored copy.

- **Banner is now pre-drawn by the collector, copied verbatim** (dogfood
  2026-07-29): a live session drew the close banner's 30-block bar freehand
  and mis-counted it (29 blocks, misaligned box), so the collector now
  prints the complete banner box to stdout (value line, 30-block bar via the
  same round-half-up integer math as the card gauge, pass/fail/n-a tallies,
  and `<repo> @ <7-hex-sha>` keeping any `-dirty` suffix) right after its
  summary line — the SKILL.md close now says to COPY that box verbatim and
  never redraw it by hand. Box borders are always equal width (grows to fit
  a long slug); no banner is printed for refusals or scoring errors. Tests
  pin bar length, round-half-up fill, equal-width borders, `-dirty`
  preservation, slug→basename fallback, and no-banner-on-refusal. A second
  dogfood round (a live dogfood repo's longer slug) caught the widened box
  padding its longest row flush against the right border — the width math
  now reserves a 2-space right margin for every content row, test-pinned.
  Also hardened in the same pass: `_repo_slug` now recognises `git://`,
  explicit-port, non-`git` scp-user, and mixed-case github.com remotes (and
  still rejects `github.com.evil.com` look-alikes), the no-remote header cell
  no longer falsely asserts "no GitHub remote detected" (an unrecognised URL
  also lands there), a slug-without-commit no longer emits a broken
  `/commit/?` link, and the HEADER invariant's SHA/dirty checks are scoped to
  the provenance header region (a 7-char SHA echoed lower in the report can't
  stand in for the table).

- **Report header: title + provenance table** (2026-07-29): the report now
  opens ci-speedup-style — `# <owner/repo> — how does your CI configuration
  score?` over a metadata table stating exactly what was scored: repository
  (GitHub-linked when a github.com origin exists, local path otherwise),
  scored commit (linked short SHA; a `-dirty` run is labelled "tree was
  dirty" in the table, not just the raw suffix), workflows scanned, rubric
  version + check count, and run date with the local-checkout-only note.
  The collector records a new display-only `repo_slug` field (parsed from
  the local `remote.origin.url` config — no network call; absent when the
  origin is missing or not GitHub). `verify_report.py` gains a HEADER
  invariant: the report must quote the document's own short SHA, must say
  the tree was dirty when `commit_sha` is `-dirty`, and must never claim
  dirt on a clean run — each provably red. No registry change.

- **Terminal score visuals — gauge, close banner, delta bar** (2026-07-28):
  three number-only visuals, every number read straight off the `ci_score`
  stamp. (1) The score card's first line is a 25-block **gauge**
  (`CI Score  38/100  ▏█…░▕  3 of 8 checks pass · 3 n/a`); filled =
  round-half-up(value × 25 / 100), and the `· N n/a` tail shows only when the
  not-applicable count is positive. The `## CI Score: **{value}/100**` line
  stays as the card's second line beneath the gauge (kept so verify_report's
  CARD-headline invariant is undisturbed). (2) The LLM close opens with a
  30-block **banner** carrying the score, pass/fail/n-a tallies, and
  `<owner/repo> @ <short-sha>` — keeping the `-dirty` suffix whenever the
  document's `commit_sha` carries one, so a dirty-tree run stays visibly
  dirty on the banner (dogfood 2026-07-29); refusals and scoring errors
  keep their plain-sentence closes (no banner). The banner example art
  uses deliberately illustrative numbers with an explicit
  read-from-the-stamp-only note, so an obeying agent can't look correct
  by parroting the example. (3) The kickoff confirmation shows a
  before → after **delta bar** (two 25-block gauges + numeric delta + the
  check that flipped) when an approved fix's re-score moves the value.
  `verify_report.py` gains a GAUGE↔STAMP invariant — the gauge's shown value
  must equal the stamp's, its filled-block count must equal
  round-half-up(value × 25 / 100), and its `N of M checks pass · K n/a` tail
  must equal the stamp's own tallies; provably red by doctoring a rendered
  bar, value, or tail. A doc-consistency test also re-derives the shipped
  SKILL.md banner/delta example art from the formula so the illustration
  can't silently drift. No registry change (frozen spec untouched —
  presentation only).

### Fixed

- **Report is now self-contained — broken methodology-file links removed
  (owner hit it live)** (2026-07-28): the score card linked each check name to
  the installed skill's methodology file (`/abs/path/….md#anchor`). Those links
  broke in common macOS viewers (which read `path.md#anchor` as a literal
  filename → "no such file") and were already dead on GitHub and other
  machines. The card's check-name links are now IN-DOCUMENT anchors, and the
  report renders a final **"What each check means"** appendix — one subsection
  per check (the check's fact and not-applicable condition verbatim from the
  registry, the why-it-matters line, the guide link, and the check's
  **Sources:** line), generated at render time, never hand-authored. No
  filesystem path appears in the report. `verify_report.py` gains an invariant
  that every check the card links has its appendix subsection in the same
  document. The methodology file keeps its explainer sections (it serves the
  website mirror and direct readers); only the report stops depending on it.
  The Sources links are single-sourced in `render_report._CHECK_SOURCES` and
  feed BOTH the appendix and the methodology explainers (a census test asserts
  they can't diverge); every URL re-verified live. This vindicates the earlier
  toolkit review that flagged the report should not depend on an external file.

- **Install-signal regex hardened — ReDoS removed + build-tool coverage (review
  round 7)** (2026-07-28): the dependency-install regex ran over an arbitrary
  (untrusted) target repo's `run:` text with a `(?:\s+-{1,2}\S+)*` group whose
  two-way dash split made it exponential — a crafted `run:` line (a manager
  token + ~20 dash-flags + no reachable verb) could hang the collector
  indefinitely with no timeout or error. De-ambiguated to a single `-\S+`
  decomposition per flag token (behaviour-preserving, now LINEAR), verified with
  a pathological-input test. Also closed a masking gap the same review found:
  build tools invoked with a project-path/goal target between the tool and the
  verb (`./gradlew :app:test`, `mvn -pl app -am install`, `dotnet tool install`)
  now match via a BOUNDED lazy window, and the `i` shorthand (`npm i`/`pnpm i`/
  `bun i`) is recognised. Added pinning tests for the by-design boundaries
  (root-only manifest probe → subdir-only manifest is n/a; install inside a
  local composite action counts) and a linear-time guard.

### Changed

- **Automation-only repositories are not scored (OD-CS20 — third refusal, part
  of the v0.1.3 bump)** (2026-07-28, owner-directed): a repository whose visible
  workflows do NO project build or test — only automation (bots, releases,
  triage) — is now REFUSED with reason `automation_only` ("Not scored: this
  repository's workflows show no build or test activity — what is visible is
  automation (bots, releases, triage), not the project's CI"), rather than given
  a technically-honest-but-absurd number that would corrode trust in every real
  score. The predicate is structural and conservative: refuse only when a repo
  has no test signal (a test-like job OR a test-runner command — `pytest`/`go
  test`/`npm test`/`cargo test`/`rspec`/… — so a real test-only repo whose job
  is named `ci` still scores) AND no build signal (build tool, a build-or-lint
  job or command, a container-build action) AND no dependency-install command in
  any workflow — any one scores it, so a small-but-real gate (lint-only job, a
  single build job) is never falsely refused; a setup-node/setup-bun action does
  not count (issue-triage bots use those to run their scripts), nor does manifest
  presence, and a repo delegating its CI to a cross-repo reusable workflow is
  never refused. `compute_ci_score` gains one refusal branch; the score
  arithmetic and the 11-check registry are unchanged; the card/report render it
  through the existing refusal path. Motivating case: anthropics/claude-code
  scored a real-looking number off workflows that are all issue-triage bots and
  release automation. Blast radius verified, not assumed: of the 31 calibration
  repos, only three have no test-like job (claude-code, coolify, leonardo); on
  re-collecting their workflows the predicate refuses exactly ONE —
  anthropics/claude-code (now "not scored") — while coolify keeps its value
  (real build-push jobs) and leonardo keeps its 0 (its `pull_request` ci runs a
  real `pnpm install`). One calibration row becomes "not scored"; every scoring
  row is unchanged.

- **Per-check external sources added to the methodology explainers**
  (2026-07-28, owner-directed, dogfood round 6: the score had no citations
  anywhere): every one of the eleven explainer sections gains a **Sources:**
  line with 1-3 authoritative external vendor-documentation links (GitHub
  Actions docs, actions/cache, actions/checkout, Turborepo, Nx, Gradle,
  Playwright, Jest, pytest-xdist) — never blogs. Every link was verified live
  (HTTP 200 after redirects) at commit time; the census test asserts the
  Sources line's presence and shape (no network in tests).

- **Scale-dependent advice conditioning restored** (2026-07-28, owner-directed,
  dogfood round 6): the three scale-dependent recommendations — test sharding,
  build caching, change-scoped builds — now state their payoff condition
  plainly in the ADVICE layer (impact_note + recipe caveat + methodology "Why
  it matters"), never presenting as unconditionally high-impact on a repo whose
  suite/build is evidently quick. Test sharding carries OPT24's original
  detection heuristic — the measured catalog's five-plus-minute test-job
  threshold — as a plain-English precondition. Provenance: the config port had
  dropped the source catalog's precondition; this restores it. Scoring is
  unchanged (advice layer only). SKILL.md's recommendations guidance gains the
  matching rule.

- **Masking hole closed: three-signal applicability + broadened ecosystems
  (bot + adversarial review)** (2026-07-28): a manifest+install-verb-only gate
  still MASKED any ecosystem outside JS/Python/Go/Rust — a .NET, Elixir, Scala,
  or JVM-monorepo repo that installs dependencies but has neither a listed root
  manifest nor a listed install verb fell to `not_applicable` and inflated its
  grade (a regression from v0.1.2, where it failed). Fixed three ways: (1) a
  **language setup action** (`actions/setup-node`/`setup-python`/`setup-java`/
  `setup-dotnet`/`setup-go`/`setup-ruby`/`erlef/setup-beam`/… — excluding
  non-runtime `setup-*` like `docker/setup-buildx`) is now a general
  applicability signal that does not depend on enumerating every ecosystem;
  (2) the manifest probe adds `pom.xml`, `build.gradle(.kts)`, `*.csproj`/
  `*.fsproj`/`*.vbproj`, `packages.config`, `mix.exs`/`mix.lock`, `build.sbt`,
  `*.gemspec`, `cabal.project`/`*.cabal`, `stack.yaml`, `Package.swift`; (3) the
  install-command regex adds `mvn`/`gradle`/`dotnet`/`mix`/`cabal`/`stack`
  build-and-resolve commands. The check is applicable when ANY of the three
  signals is present; `not_applicable` only when NONE (a genuinely
  dependency-free repo — the only disclosed residual is a truly exotic stack
  with no root manifest, no recognised install command, and no setup action).
  Also hardened the install regex to require the verb ADJACENT to the manager
  (so `npm run ci` / `yarn run add-x` no longer false-match) and to strip shell
  comments before matching (so a commented-out install can never cause a false
  FAIL — honouring the module's structure-not-substrings invariant). The v0.1.3
  `post_freeze_receipts` summary is corrected to record the two test-sharding
  row moves (it had wrongly said no row moved). Blast radius unchanged: the
  three-signal union is strictly more applicable, so it can only remove n/a
  cases, never add them — no calibration row moves.

- **Post-merge coherence + toolkit hardening riders** (2026-07-28): the two
  explainer sections whose not-applicable conditions v0.1.3 changed are
  regenerated verbatim from the new registry (the other nine verified
  already-verbatim); plus the second toolkit pass's five follow-ups: an
  anchor round-trip test (card _anchor == GitHub's slug for every registry
  label), a card link-emission test, a content pin on the cache recipe's
  manifest caveat, the methodology summary-table row aligned to the registry
  label ("Superseded runs cancelled"), and a stale "grade from the stamp"
  docstring refreshed to score wording.

- **CI Score registry bumped to `ci-score-v0.1.3` — two applicability gates
  added (OD-CS19)** (2026-07-28): two checks gain the applicability gate the
  rubric's own principle already demanded — *a mechanism this fact cannot see
  is never failed.*
  - **`ci.cache.dependency-cache` is `not_applicable` only when the repo shows
    NO dependency-install signal at all** — an install-signal gate, not a
    file-only one (see "Round-6 retarget" below). Applicable when ANY of three
    signals is present: (i) a dependency manifest at the repo root (probe over
    `package.json` + JS/TS lockfiles, `requirements*.txt`, `pyproject.toml`,
    `setup.py`/`setup.cfg`, `Pipfile(.lock)`, `poetry.lock`, `uv.lock`,
    `go.mod`/`go.sum`, `Cargo.toml`/`Cargo.lock`, `build.gradle(.kts)`,
    `pom.xml`, `*.csproj`/`*.fsproj`/`*.vbproj`, `packages.config`, `mix.exs`/
    `mix.lock`, `build.sbt`, `*.gemspec`, `cabal.project`/`*.cabal`,
    `stack.yaml`, `Package.swift`, `Gemfile(.lock)`,
    `composer.json`/`composer.lock`, `pubspec.yaml`/`pubspec.lock`), (ii) a
    dependency-install command in a step (`pip`/`npm`/`pnpm`/`yarn`/`bun`/
    `poetry`/`uv`/`composer`/`bundle`/`gem`/`conda` with an install-verb
    adjacent, plus `mvn`/`gradle`/`dotnet`/`mix`/`cabal`/`stack` build commands,
    `go mod download` / `cargo fetch`), or (iii) a language setup action
    (`setup-node`/`setup-python`/`setup-java`/`setup-dotnet`/…). Only a repo
    with NONE of the three is `not_applicable` — with nothing installed there is
    nothing to cache, so a missing cache is never a fail. A cache that IS
    configured still passes
    regardless of the signal; the existing reusable-delegation n/a is unchanged
    and takes precedence.
  - **`ci.parallel.test-sharding` is `not_applicable` when no test-like job
    (test/spec/e2e/integration/unit) and no shard-like matrix axis exists in
    any parsed workflow** — you cannot shard tests you do not have. A test job
    WITHOUT a matrix still fails (you have tests, shard them); the existing
    reusable-delegation n/a is unchanged and takes precedence.
  - **Live discovery + Round-6 retarget:** found by dogfooding the public
    skills repo, which installs its dependencies with `pip install pytest
    pyyaml` and caches none. An early file-existence-only manifest gate was the
    wrong instrument on two counts, exposed in round-6 dogfooding: (a) it
    *missed* the motivating repo — the skills repo actually carries a
    `pyproject.toml` (pytest config, `dependencies=[]`), so a file gate leaves
    it applicable but for the wrong reason, not the install it runs; and (b) a
    file-only gate would *mask real waste* — a repo with no manifest file but a
    bare inline `pip install` in every run would go `not_applicable`, silently
    dropping a genuine fail from the denominator and INFLATING the grade. So
    the gate was retargeted to install-signal semantics: the skills repo is now
    honestly applicable+fail (it installs deps and caches none — a real,
    two-step fix), and a manifest-less inline-install repo is applicable+fail
    (no masking).
  - **Scope of code change:** `practice_facts.py` only (an install-signal probe
    = manifest file-existence OR a step-code install regex, and a job-shape
    probe). The scorer arithmetic (`ci_score.py`) is unchanged — both gates
    land another `not_applicable` state, which already leaves the denominator;
    the check count stays 11. The dependency-cache fix recipe
    (`render_report.py`) is reworded to match: a fail means the repo DOES
    install dependencies and caches none (n/a, not a fail, only when it installs
    nothing), so the recipe wires a cache to a manifest, or adds the manifest
    first when deps are installed inline.
  - **Blast radius (computed by re-running the scorer, never hand-derived):**
    over the full 31-repo calibration table under v0.1.3, **two rows move** —
    both small/atypical repos with no test-like job at all, where the
    test-sharding gate fires: **anthropics/claude-code 44 → 50** and
    **coollabsio/coolify 22 → 25** (F → F, value only). adobe/leonardo also has
    no test job but already scored 0 (0 passed), so it holds at 0. Every
    other row is unchanged. The sharding gate is determinable from committed
    data (`workflow_job_graph` carries every job id + name, and a committed
    sharding fail implies no shard-axis existed); the dependency-cache
    install-signal gate is not recoverable from committed findings, but it is
    strictly MORE applicable than the file-only gate it replaced (a manifest OR
    an install command keeps it applicable), so it can only remove n/a cases,
    never add them — and every dep-cache-fail repo in the set both carries a
    standard root manifest AND runs an install step, so that gate fires on none
    of them.
    The six committed corpora each keep a test-like job (requests's sharding
    fail stays fail) and the two frozen checkout controls hold at 82.
    Per-row receipt: `calibration/dry-run-2026-07-28-v0.1.3.md`.
  - **Re-stamp scope + reasoning:** the corpora fixtures do NOT carry the repo
    tree, so their `practice_facts` cannot be recomputed from source; they were
    re-stamped by re-running the scorer over the committed facts (arithmetic
    only), which bumps the stamp's `spec_version` and nothing else. That is
    honest because every corpus's facts are consistent with the gates NOT
    firing, verified from the committed evidence strings: `deepgram-python-sdk`
    is the only dependency-cache *fail* and is a real Python SDK (manifest
    present → the fail stands); `requests` is the only sharding *fail* and its
    CI has real test jobs (the gate needs *no* test job to fire → the fail
    stands); every other corpus passes both checks (cache hits / test-with-
    matrix present). The two checkout controls DO carry the tree and were fully
    recomputed via the real collector — both pass dependency-cache (cache hits
    present) and test-sharding (real test jobs), holding at 82 / 82. The
    `collected/` calibration receipts stay their dated snapshots.

- **Every check name on the card links its explainer** (2026-07-28, owner:
  "the report is sparse - no path to learning more about each check"): the
  methodology gains an anchored "Each check, explained" section per check
  (fact verbatim from the registry, not-applicable condition, why-it-matters,
  guide link where a practice page exists - GENERATED from the registry so it
  cannot drift), and the score card links every check label to its section in
  the installed skill. A census test pins registry<->explainer coverage.

- **Number-only presentation (owner, 2026-07-28)**: surfaces render the
  numeric score ("82/100"), never the letter band. The bands remain defined
  in the frozen registry and stamped beside every value (no registry bump -
  presentation only). Rationale: with 8-12 applicable checks the instrument
  cannot distinguish adjacent values, and the number states that honestly;
  a numeric form is also softer on a public page than a report-card letter,
  and matches the category convention (Mintlify Agent Score, React Doctor).
  Card headline, collector summary, verify_report CARD invariant, close
  contract, and methodology all updated; format-pinning tests moved to the
  new form and assert the letter is NOT rendered.

- **Dogfood round 5 (self-scoring the public skills repo)** (2026-07-28):
  two owner-ratified refinements. (1) **Cleanest-clean-win offer**: the
  kickoff offer defaults to #1, but when #1 is not a clean apply on this
  repo (missing prerequisite, degraded payoff - with evidence stated), the
  agent may offer the highest-ranked clean apply instead, saying why it
  skipped ahead; the report's ranking is untouched. Ratifies the live
  session's judgment (dep-caching ranked #1 but the repo has no manifest to
  key a cache on; timeouts offered instead). (2) **Dependency-cache recipe
  gains a manifest precondition** (the sharding-guard pattern): without a
  lockfile/manifest the cache input has nothing to hash - add one first;
  the fix does not apply as written.

- **Security positioning: not-a-security-audit framing, no sibling pointers**
  (2026-07-28, owner-decided): the public skill must not point at non-public
  siblings, so every ci-secure mention is gone from shipped surfaces. The
  framing is positive scope-statement instead: ci-score grades best
  practices, and exactly two of its eleven checks (action pinning,
  job-scoped OIDC tokens) happen to be security-related - it is not a
  security audit and claims nothing further about workflow security
  (description, overview, and routing contract all carry it; security
  prompts route to "none"/do-not-trigger).

- **Skill renamed ci-advisor -> ci-score (OD-L6)** (2026-07-28): the owner's
  own invocation reflex (/ci-score) proved the entity->tool name hop real;
  the skill name now equals the metric, the website URLs, and the data dir -
  OD-W1's one-name rule extended to its last holdout. v1 scope unchanged
  (grade + fixes, eleven checks). The report filename becomes
  ./ci-score-report.md. "ci-advisor" is parked as a possible future
  speed+practices release, constrained by the hard separation rule.

- **Fresh-report rule (dogfood round 4)** (2026-07-28): the owner caught the
  report going stale after applies ("now slightly stale since it predates the
  pin fix") - the kickoff re-scored but never re-rendered. The contract now
  requires re-running render + verify whenever a re-score changes the stamp,
  so ./ci-advisor-report.md always matches the current state.

- **CI Score registry bumped to `ci-score-v0.1.2` — `ci.trigger.draft-gate`
  removed (OD-CS18)** (2026-07-28): the Draft-PR-gating check is dropped from
  the registry (12 → 11 checks). Rationale: it is a contested COST PREFERENCE,
  not a consensus best practice — many teams push draft PRs precisely to get CI
  feedback before review; in the B4 18-repo sweep the top-band (85) exemplars
  (vitejs/vite 90, home-assistant/core 90) each failed ONLY this check; and it
  is the one check with no published best-practice page behind it
  (`practice_slug` null). Pre-publication timing, the cheapest moment for a
  rubric fix. The scorer arithmetic (`ci_score.py`) is unchanged — it iterates
  the registry, so a smaller registry simply scores over fewer checks; the
  draft-gate fact computation (`practice_facts.py`), its `_FIX_TABLE` entry
  (`render_report.py`), and its methodology row are removed.
  - **Re-stamp scope:** every committed corpus fixture
    (`tests/fixtures/corpora/*/findings.json`) and the synthetic
    `stamped-fixture.json` were recomputed under v0.1.2 (their `practice_facts`
    are kept verbatim — the extra draft-gate fact is a harmless superset the
    scorer ignores). The 25 `maintainers/ci-advisor/calibration/collected/*`
    receipts were left as their dated **v0.1.1** snapshots so each stays
    coherent with its un-restamped `report.md`; the recomputed 31-repo spread
    is `calibration/dry-run-2026-07-28-v0.1.2.md`.
  - **Calibration delta (numeric, computed by the scorer over the 31-repo
    table).** Every row shifts by a few points because ~60% of the set was
    failing the removed draft-gate check (promotions only, none demoted):
    OneSignal-Flutter-SDK, requests, oven-sh/bun, cal.com, Infisical, nx and
    redpanda all rise; the two B4 positive controls move mastra 83 → 82 and
    better-auth 75 → 82 (both 9/11); langfuse and grafana 83 → 82, deepgram
    30 → 33, mastodon holds at 100, coolify/leonardo/claude-code hold. The
    complete per-row before/after (numeric) is
    `calibration/dry-run-2026-07-28-v0.1.2.md`.

### Fixed

- **Scoring-failure marker rendered from its real (string) shape** (2026-07-27):
  `render_card._render_score_card` emitted the "CI Score unavailable" line only
  for a `{"error": ...}` dict, but `collect_config` records a scoring failure as
  a plain string (`f"{type(exc).__name__}: {exc}"`). On every real scoring
  failure the card silently dropped the error and the B2 report verifier
  (`verify_report.py`) then went red with an opaque message. The card now
  honours both shapes, so a recorded failure is surfaced and the report↔verify
  pair stays green. Covered by a new contract cell built from the collector's
  actual output.

### Added

- **Stakes-first TL;DR per recommendation** (2026-07-27): every rendered
  recommendation now opens with a one-line plain-English headline (the
  `tldr` field, required for every fix-table entry and asserted present by
  `test_fix_table_covers_every_registry_check`) that states why the gap
  matters before the mechanics. The headlines describe the mechanism the
  fix changes — they deliberately do NOT promise repo-specific measured
  outcomes (no "minutes into seconds", no "divides the wait by N", no
  "makes it free"), staying consistent with the adherence-not-speed
  disclosure: the grade measures configuration, not timing this scan
  cannot see.

- **Dogfood round 3 (PR-body score receipts + consent precision)**
  (2026-07-27): (1) **PR requests get a receipt-bearing description** -
  opening a PR is never part of an apply (explicit ask only); when asked,
  the PR description states the CI Score before → after (read from the
  confirming re-scores' stamps, never recomputed), names the applied
  recommendation(s), and carries the intermediate-state caveat when one
  applies, so the reviewer understands why the PR exists from the
  description alone. (2) **Consent scope prefers files over occurrences**
  - state how many FILES a fix will touch, not just how many occurrences.
  The finding's example list is capped at three, but the offer is not
  capped by it: enumerate the real offender set from the checkout (the
  same scan the apply must run to fix every offender) and state that true
  file count; only when the scope genuinely can't be enumerated ahead of
  the edit does the offer fall back to the occurrence count, and then it
  names the diff — not the count — as the authoritative scope, so the
  offer never implies fewer files than the apply will change. (3) **Network-lookup disclosure** - an apply that needs
  network lookups (tag→SHA via `git ls-remote`) says so in the offer,
  the same ask-before-network spirit that governs cloning.

- **Dogfood round 2 (owner's second live session; apply flow exercised
  end-to-end)** (2026-07-27): three owner rulings folded in. (1) **Apply
  endpoint enforced: the yes covers the EDIT only** - the live agent read
  "yes do #1" as license to commit AND push a branch; the contract now says
  stop at the diff, with commit / push / PR each needing its own explicit ask
  (pushing is outward-facing). (2) **Deliberate-absence check ratified**: the
  agent spontaneously caught that the repo's full-depth checkout exists
  because an immutability test walks git history, and advised skipping that
  rec - now required: a quick scan for visible evidence a failing practice is
  deliberate, presented as "likely deliberate, I'd skip it" with the reason.
  (3) **Intermediate-state honesty ratified**: the agent's unprompted "a
  group without cancel-in-progress queues, it doesn't cancel" is now
  required - when an applied fix only fully pays off with a sibling rec,
  say so and name the sibling. Session also confirmed round-1 fixes live
  (report home, named-pair bundling, per-rec consent held under a
  different model).

- **Dogfood round 1 fixes (owner's first live session, an internal-dev-repo)**
  (2026-07-27): three outcomes from real usage. (1) **Report home = the
  ci-speedup convention** (owner-decided): the sanitized report renders to an
  accessible working-directory path (./ci-advisor-report.md) so the handoff
  prompts outlive the session; raw findings.json stays in scratch; never
  auto-committed; Gotchas document the report-in-tree -> next-run -dirty
  interplay honestly. The close contract's absolute-path line was corrected
  in the same move (it still claimed the report "lives in the scratch
  workdir, not the repo" - now points at ./ci-advisor-report.md). (2) **Same-edit bundling allowed, named explicitly**
  (owner-decided): when a single edit closes two checks at once (one
  concurrency block with cancel-in-progress satisfies both the
  concurrency-group and cancel-superseded checks - a superset, not two
  identical recipes), that one edit may be offered as a single apply naming
  both checks; two edits that merely touch the same file stay separate
  approvals - the live agent did exactly the one-edit case and the owner
  ratified it.
  (3) **Impact notes state what impact scales with**: the owner asked "do
  concurrency groups even matter at this repo's volume?" - fair; the generic
  high-impact tier overstates on low-traffic repos, so the concurrency/cancel
  impact notes now say impact scales with push frequency (cheap hygiene, not
  meaningful savings, on quiet repos).

- **Spec-compliance audit fixes (three-verifier pass over B1-B4)** (2026-07-27):
  three parallel verifiers audited the implementation against both specs; all
  numbers held (18/18 grades, 17/18 frozen-row matches, zero owner-decision
  violations), six process findings fixed: (1) absence-type findings now NAME
  their target workflows in `files` on fail (four PR-path checks +
  job-timeouts; capped-3 examples, evidence carries the true count) - the
  fixing agent re-derives nothing, closing the deferred B4 finding in-wave;
  (2) NEW sparse-checkout refusal in the collector (`sparse_checkout`, exit 2)
  - sparse mode hides missing files from the dirty flag, so a clean-HEAD
  partial view could stamp a confident inflated grade; (3) evals/evals.json
  now encodes the three B4 validation layers where the spec places them;
  (4) live refusal-repo smoke run and recorded (antirez/sds: honest refusal,
  report renders, verify OK); (5) sweep record corrected (17/18 calibration
  matches, not 7 - the draft understated its own coverage) and (6) the
  django-class precision recorded: fully off-tree external CI has no in-tree
  marker, so the W1-D judgment item can never be reduced to marker-scanning.

- **Cannot-see-mechanism caveats (B4 sense-check)** (2026-07-27): the owner's
  grade sense-check on the 18-repo sweep found three honest-but-misleading
  grades, all one root cause - practices delivered via mechanisms outside
  workflow YAML (nx distributes tests via Nx Cloud agents yet failed
  test-sharding, and the recipe would have advised adding a shard matrix on
  top; bun/django are Buildkite/Jenkins-primary and were graded on auxiliary
  GHA workflows). The report's recommendations section now carries a general
  external-CI disclosure, the sharding recipe carries an explicit
  do-not-apply-over-distribution caveat, and both are test-pinned. The
  Wave-1 publish-gate floor gained the measured datum (bun/django at 9
  applicable sail past a <8 floor - the human external-CI judgment item is
  load-bearing).

- **B4 (partial) — exact-match controls + 18-repo sweep green** (2026-07-27):
  frozen input-surface checkouts for the two calibration positive controls
  committed under `tests/fixtures/checkouts/` (upstream SHAs recorded);
  `tests/test_b4_controls.py` runs the FULL collector path on each, offline,
  and asserts the calibration grade exactly (mastra 83, better-auth 75) -
  an exact-match failure now always means engine drift, never input drift.
  The 18-repo live dogfood sweep ran clean (18/18 collect/render/verify green,
  7/7 calibration cross-checks matched on live HEADs, kickoff apply loop
  exercised end-to-end on a throwaway clone: full scope, oracle flip, expected
  -dirty confirmation). Sweep record: `maintainers/ci-advisor/`. One open
  follow-up logged (absence-type findings cite zero files). B4 remains OPEN
  behind the owner's interactive dogfooding; SHIP is owner-gated.

- **Contract hardening from the post-merge adversarial pass** (2026-07-27):
  two skeptic reviews read the live SKILL.md *as the obeying agent* and found
  15 text-level defects; all folded in. Kickoff protocol: pre-existing
  uncommitted work is now protected (never stash/reset/discard work the agent
  didn't create; ask on a dirty tree), the still-failing re-score loop is
  BOUNDED (stop only after the recipe reached the full offender scope - the
  evidence's count, not just its up-to-three examples - never mutate YAML to
  force a pass), the re-score is scoped as a config-fact oracle (not
  proof CI runs green), informed consent before apply (state file-count scope;
  surface risk notes for skip-CI recs in the offer itself), and a blanket
  "apply all" is explicitly not per-recommendation approval. Close contract:
  the recs/offer block is gated to scored-with-fails (clean repos and
  refusals no longer get an absurd "Apply #1?"), and the report's absolute
  path is stated. Flow: <workdir> defined (scratch outside the checkout,
  per-repo subdir), verify-failure gets a re-render + stamp-fallback recovery
  instead of a dead end, self-cloning is a valid acquisition path (ask first;
  third-party grades stay for the user's eyes), and the no-speed-claims
  boundary is operational (route mid-session speed questions to ci-speedup).
  Recipes: sharding guarded on runner support, pinning never fabricates SHAs
  (network lookup or hand back; Renovate = follow-up suggestion), change-
  scoped warns it needs a merge-base and must not combine with the
  shallow-clone fix on the same workflow.

- **B3 — SKILL.md goes live + close contract + routing contract** (2026-07-27):
  the NOT LAUNCHED placeholder is replaced by the real skill contract:
  triggering description per OD-L4 (explicit do-not-trigger routing to
  ci-speedup/ci-secure), the collect -> render -> verify -> close flow
  (verify_report must print OK before a report is presented), the close
  contract wired live (open with the grade from the stamp, refusal/error/
  absent handled honestly, never fabricate; top ranked recs; the OD-L5
  "Apply recommendation #1 now?" kickoff with per-approval apply), a Gotchas
  section, and Boundaries. The kickoff/handoff promise is honest about the
  three-example offender cap in `_practice_facts` (`files` truncates to 3):
  the fix applies everywhere the practice is missing and the re-scored check
  — not the listed files — is the completion oracle, so a >3-offender check
  is never falsely reported fixed. New: `evals/prompt-routing.json` (the committed
  OD-L4 routing contract; ambiguous middle -> ci-speedup) guarded by
  `tests/test_routing_artifact.py`, and `evals/evals.json` (4 scenarios incl.
  a should-not-trigger case). skills-best-practices audit run: 3 warnings
  found and fixed in the same PR (Gotchas section, evals.json, methodology
  TOC). Continuous-dogfood pass: collect->render->verify green on 3 real
  repos (the public skills repo and two internal-dev-repos).

- **B2 — report + ranked recommendations (OD-L5)** (2026-07-27):
  `scripts/render_report.py` renders the full report from a findings document
  only: score card (stamp-only, unchanged) + the adherence-not-speed
  disclosure beside it + one recommendation per FAILED check **ranked by
  impact x risk** (cheapest-safest first within a tier), each with honest
  impact/risk notes (path filters say plainly they can skip CI that should
  run), a concrete YAML fix recipe, the registry-bound best-practices page
  link (fix-table override only for the one page that postdates the freeze:
  bound-job-timeouts), and a paste-able agent handoff prompt grounded in the
  document (evidence + files + scored commit quoted; capture-once). Ranking
  tiers live in the renderer's `_FIX_TABLE`, never the frozen registry.
  `scripts/verify_report.py` is the one-command PASS/FAIL invariant checker
  (stamp<->card verbatim agreement, stamp recount, disclosure presence,
  every FAIL fully recommended, rank-order-follows-the-table, cited files =
  the stamp's own). Contract cells: `tests/test_render_report.py` (12 cells;
  the checker proven RED on all six invariants — a drifted stamp, a rec-less
  FAIL, a missing disclosure, a mis-ordered ranking, an internally
  inconsistent stamp, and a rec citing a file the stamp never cited).

- **B1 — the entry point** (2026-07-27): `scripts/collect_config.py`, the
  skill's front door. Local-checkout-only per OD-L2 (a partial fetch silently
  inflates grades, so outside a git checkout it refuses politely — never
  guesses); offline by construction (contract cells run with sockets
  booby-trapped); parses `.github/workflows/`, runs `_practice_facts` →
  `compute_ci_score`, writes `findings.json` with the `ci_score` stamp. Every
  outcome is stamped: no-workflows → the spec's `no_workflow_yaml` refusal in
  the stamp (exit 0 — a refusal is a result); not-a-checkout →
  `collection_refusal` (exit 2); scoring failure →
  `data_sources.ci_score_error`, never a partial stamp (exit 3); unparseable
  workflows counted + named in `data_sources.workflow_parse_errors`; workflow
  files present but **none parseable** → `collection_refusal`
  (`no_parseable_workflows`, exit 2) rather than a deflated `F(0)` computed from
  zero readable documents (that would assert false "absent" facts). A `--repo`
  target inside a checkout is normalized to the repo **top level** so a
  subdirectory target scores the whole repo, never a partial (grade-inflating)
  view. Provenance = full-repo HEAD SHA with an honest `-dirty` suffix
  (untracked files included; an *unverifiable* tree state — `git status`
  failing — is marked `-dirty`, never certified clean) that truncation never
  hides. `STARSLING_LOG_LEVEL` DEBUG lines record counts, names, and states —
  never file contents. Contract cells: `tests/test_collect_config.py` (11 cells
  incl. offline, determinism, subdir normalization, all refusal/error paths).

- **Born by extraction from ci-speedup** (2026-07-16, [#237](https://github.com/starslingdev/skills/pull/237)). ci-advisor
  is the new private home of the CI Score, lifted whole out of `ci-speedup` so
  that skill ships score-free:
  - `scripts/ci_score.py` — the pure-function scorer (moved byte-identical).
  - `scripts/practice_facts.py` — `_practice_facts` and its parsing helpers,
    lifted out of ci-speedup's `scan.py` (copied self-contained; no cross-skill
    imports) so ci-advisor owns its input end-to-end.
  - `scripts/render_card.py` — `_render_score_card` (+ a copied `_flatten_cell`)
    lifted out of ci-speedup's `blocking_path.py`.
  - `references/ci-score-spec.json` — the **frozen CI Score v0.1.1 registry**
    (moved byte-identical; never edited).
  - `references/ci-score-methodology.md` — the human-facing rubric.
  - Tests rebuilt against fixtures only (no imports of ci-speedup scripts, no
    reads of ci-speedup's `reports/`): `tests/test_ci_score_contract.py`
    (recompute-from-stamps over the six scored corpus fixtures +
    `stamped-fixture.json`), `tests/test_ci_score_spec.py` (spec shape),
    `tests/test_close_contract.py` (the relocated close-honesty pin).
  - WIP `SKILL.md` (explicitly **NOT LAUNCHED**), carrying the relocated close
    contract as ci-advisor's future close guidance.

### Fixed

- **Guarded the `yaml` import in `practice_facts.py`** (2026-07-27): was a
  bare `import yaml` that would kill a clean install with a raw traceback
  (PyYAML is not stdlib and the skills CLI installs no deps); now the loud
  install-hint + `sys.exit(1)` pattern the ci-speedup scanner uses.
