# Changelog

All notable changes to the `ci-speedup` skill are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/); the skill is
unversioned and updates by reinstall from `main`.

> **Note on PR/issue numbers.** Entries below reference the pull requests and
> issues of the skill's pre-public development archive, which is not part of
> this repository's history. The numbers are kept for the maintainers' audit
> trail; they are not links you can follow here.

## [Unreleased]

### Added

- **2026-07-28** — **Credential-shaped strings are masked in every quoted log
  line.** The report quotes verbatim job-log and workflow-YAML text as evidence,
  and that is the artifact users commit and share; GitHub masks only the secrets
  registered as repo/org secrets, so an accidentally-echoed token reached the
  report in the clear (skills.sh Snyk W007, HIGH; issue #12). `_redact_secrets`
  now runs inside `_fence_safe` — the single sink every evidence line, repo name
  and agent-prompt line already flows through — replacing GitHub tokens, AWS
  access keys, Slack tokens, JWTs, Google API keys, private-key headers and
  `key=value` credential assignments with `[REDACTED:<kind>]` while keeping the
  surrounding words, so the evidence stays readable. The LLM gap-fill's `cause`
  and `breakdown` (markdown prose, which renders as markdown rather than fenced
  text) are masked at their own site, and `_flatten_cell` — the appendix
  `**Evidence:**` lines and the Tier-2 / structural rows, which quote workflow
  YAML verbatim without passing through `_fence_safe` — is the third masked sink.
  Shaped patterns only, no entropy heuristic: step names, durations, run URLs and
  40-hex provenance shas render unchanged, and a value that is a *reference*
  (`${{ secrets.X }}`, `${VAR}`, `%VAR%`) is left alone — that is what correct
  workflow YAML looks like, and masking it would destroy the diagnostic and imply
  a hardcoded token that isn't there. SKILL.md phase 4a and
  `references/gap-fill.md` gained the matching instruction-level rule (never
  quote a credential; mask it and note the mask), and the repo's `SECURITY.md`
  documents the three-layer log-data model (W011).

### Changed

- **2026-07-30** — **Two gating poles ⇒ both get their own menu slot.** The
  ≤4-option fold used to collapse the second pole into "Fix all," leaving no
  way to pick pole 2 alone — a live user who had just fixed pole 1 had no
  button for the other check. With exactly two poles the menu is now pole 1 /
  pole 2 / "Fix both" / save, and the bill option folds out to the close
  prose instead — named there either way (the source-backed `~N min/mo`
  saving or the modeled pointer) so it stays reachable (≥3 poles keep the
  old fold).

- **2026-07-30** — **Era disclosures lead the close.** When the report's top
  matter carries a config-era caveat — `disclosed_pre` (too few runs since the
  workflow changed, so the numbers describe the PREVIOUS config), a narrowed
  window, or `post_only_thin` (the new config measured on a thin post-change
  sample, so the numbers are provisional) — the close states it before any
  number, with re-audit guidance, and never presents a retired-era or provisional
  number as current. The fast-CI "your CI is already in good shape" preface is
  retained but now yields to an era caveat, so a sub-2-minute retired/provisional
  number never reads as healthy. Live miss: a post-fix audit led with the pre-fix
  merge wait and no era note, so the user read their freshly shipped speedup as
  absent. Offsetting close-prose compressions keep SKILL.md under the 500-line
  budget (no rule lost).

- **2026-07-30** — **The `gh` gate is sandbox-aware.** In approval-gated agent
  environments (Codex), the first restricted shell can't reach a keyring-held
  credential, so `gh auth status` false-fails for a logged-in account. The gate
  now retries with host access before concluding, and never reports auth
  "expired" off a sandboxed probe (live Codex run 2026-07-30 told a logged-in
  user to re-authenticate).
- **2026-07-30** — **Verify-fail withholds the save option.** If phase-5
  verification stays red after its one re-render retry, the close drops `None,
  just save the report (.md)` and says why in one line — a report that failed
  its own checker is never offered for saving (codifies behavior a live Codex
  run improvised).

### Fixed

- **2026-07-28** — **An aggregation-gate pole tells the honest upstream story
  instead of prompting the reader to optimize a 3-second no-op** (issue #1). Many
  repos make one trivial job the single required status check: it runs no work,
  `needs:` everything else in its workflow, and exists only so branch protection
  has one name to require (vercel/next.js `thank you, build` — job `buildPassed`,
  `needs: [deploy-target, build, build-wasm, build-native]`, body `run: exit 1`,
  P50 **3s**). Because it gates every PR it was correctly crowned Long pole 1 by
  frequency — and then rendered with a drill placeholder plus a "capture timing,
  then optimize this step" agent prompt: correct data, inert advice, at the first
  thing a reader sees. `blocking_path._agg_gate_shape` now detects the shape at
  render time from data the artifact already stamps (`workflow_job_graph` + the
  check spine — no producer change): trivial P50 (<= 30s, below any real hosted
  job's checkout+setup floor), a *terminal* job whose transitive `needs:` closure
  (>= 2 jobs) covers every non-terminal job in its workflow (uncovered siblings
  must themselves be terminal — the shape of an `if:`-conditional peer sink like
  `publishRelease`), no sampled step above that threshold, and at least one
  upstream member with measured timing. Such a pole now renders a role line
  saying what it is — its wait IS its `needs:` upstream — names the slowest
  measured upstream member with its P50, and points at the pole that drills it
  (an anchor when it renders, otherwise the check name and what a re-run does —
  never a dead link), with **no drill, no floor note and no agent prompt**.
  Structure, not duration, is the test: a real 3s `lint` job with no `needs:`
  coverage renders byte-identically to before. Crowning and ranking are
  unchanged, a sink that is a modal-chain member keeps its chain-stage framing
  (no double-framing), and a pole with a matched log-detector leaf or a routed
  structural lever keeps its drill (there the advice is not inert, and dropping
  it would lose a measured lever). `verify_report` gained the matching pair:
  `check_speed_poles_complete` exempts such a pole from the every-pole-has-a-
  prompt rule, and the new `check_aggregation_gate_poles_never_prescribe` fails
  any aggregation-framed pole that carries a prompt, omits the upstream pointer,
  or whose shape does not re-derive from `findings.json` — so the exemption can
  never launder a genuinely stunted pole. ARCHITECTURE §12.6b documents the
  shape; SKILL.md 5b carries the matching self-audit carve-out.

- **2026-07-28** — **The physical-bound sizing guard now joins a finding's jobs
  through the scanned job graph, never a cross-workflow name match** (issue #2).
  `verify_report.check_saving_within_measured_compute` matched a finding's
  `affected_jobs` (YAML keys) against the cost spine's job names (rendered
  `name:` display names, one row per matrix leg). A `name:`-overridden job missed
  that join entirely and fell through to the cross-workflow same-name fallback,
  which bound an UNRELATED job in another workflow that happened to share the bare
  name — on biomejs/biome, OPT33's `lint` in `pull_request.yml` (real compute
  13,381.6 min/mo across two matrix legs) bound `pull_request_markdown.yml`'s
  553.0 min/mo `lint`, and a correctly-sized 3,187.9 min/mo credit false-FAILed
  the gate. The join now resolves key ↔ `name:` in BOTH directions through the
  artifact's own `workflow_job_graph`, tries every same-workflow identity before
  the cross-workflow fallback, and sums all matrix legs of the resolved job.
  Graph-resolved aliases are SAME-WORKFLOW ONLY — the cross-workflow fallback
  still matches the literal base, so a job with no spine row in its own workflow
  can never bind an unrelated namesake's compute and inflate the upper bound. An
  affected job genuinely absent from the spine still surfaces as a coverage gap,
  and an artifact with no job graph keeps the previous bare-name behavior. An
  AMBIGUOUS display name — two job keys in one workflow rendering to the same
  `name:`, hence one shared spine row — yields no alias, so a finding is never
  bounded by its twin's compute.

### Fixed (pre-flip audit wave 2)

- **2026-07-22** — **Catalog deep-links now use GitHub's real GFM anchor rule.**
  `scan._slug_anchor` / `collect_runs._catalog_anchor` hyphenated punctuation
  where GitHub deletes it, so the report's "Catalog (background + fix recipe)"
  links for 15 patterns whose titles contain `/`, backticks, `.`, `:`, or `≫`
  (OPT13/14/32/41/42/47/52/55/56/58/69/70/72/74 + the OPT14 sub-head) landed at
  the top of the patterns file instead of the cited section. Both slug
  implementations now delete punctuation and map each space to one hyphen —
  validated against every catalog heading (0 mismatches) — and the two shipped
  examples' live links (playwright OPT14, flask OPT32) plus the baked test
  fixture are corrected. Also fixed two stale hand-authored anchors (OPT12's
  guardrail link still pointed at the pre-renumber `#p22--…`;
  `savings-methodology.md`'s ToC still said `#billing-semantics-…` after the
  pricing punt renamed the heading) and the `verify_report.py` docstring
  escape-sequence DeprecationWarning contributors saw on every `pytest` run.
  Shipped docs no longer point readers at the archive-only `reports/` corpus
  without saying so (ARCHITECTURE.md ×3, MAINTAINERS.md), the examples' scripts
  tree stamps carry the "(pre-public archive)" label like their commit stamps,
  and PROVENANCE.md states the caveat outright.

### Changed

- **2026-07-22** — **Public cut-over pre-flight.** The repository's public
  history begins at the cut-over; earlier PR/issue numbers cited in this
  changelog live in the maintainers' private archive (note added at top). The
  worked examples' catalog links now target `blob/main` (their pinned pre-cut
  SHA will not exist publicly) and their provenance stamps carry an explicit
  `(pre-public archive)` marker; the examples-provenance guard accepts that
  marker only for reports generated before the first public day, so a post-cut
  stale example can never hide behind it. Prose that said "git history
  preserves" retired modules (pricing engine, OPT66 detector) now says the
  pre-public archive does.

### Fixed

- **2026-07-21** — **The static-only banner no longer calls a live repo dormant
  when its CI simply never gates a PR** (pre-flip audit, live on
  `simonw/sf-tree-history`). A repo running CI daily whose workflows fire only
  on `schedule`/`push`/`tag`/`release` rendered the dormant-repo hedge ("an
  archived, brand-new, or low-activity repo whose run history aged out … found
  no run timing") while the same report priced findings off 20 timed schedule
  runs — a direct self-contradiction in the loudest banner. `_render_static_only`
  now derives the repo's CI shape from `workflow_triggers`: (1) no workflow with
  a PR trigger ⇒ a new "**No PR-gating CI to measure**" banner that owns the
  timed non-PR runs and never speculates about archived/aged-out history (its
  bottom line and hygiene tail stop promising a merge-wait that can never
  exist); (2) a PR-triggered workflow exists but produced no sampled PR timing ⇒
  the hedge now says "found no **PR** run timing", acknowledges measured non-PR
  runs when present, and names the since-deleted-workflows cause (recent PR runs
  whose workflow file no longer exists are excluded by design). New
  `verify_report` invariant `check_static_only_banner_matches_ci_shape`
  re-derives the shape from the findings JSON and fails any static-only report
  that hedges "archived/brand-new/low-activity" at a no-PR-trigger repo or
  claims "no run timing"/"no run history was available" beside
  `runs_sampled > 0`. Goes RED on the pre-fix artifact, GREEN on the fixed one.

- **2026-07-21** — **Catalog correctness pass (pre-flip audit, hostile
  senior-CI-engineer read of all 73 patterns).** OPT34/OPT70's change-scoping
  recipes (`turbo --filter=...[origin/main]`, `nx affected`, `vitest --changed`)
  now instruct fetching the base ref (and flag that `main` is illustrative —
  substitute the repo's actual base branch, e.g. `origin/${{ github.base_ref }}`
  on PR runs, so the recipe doesn't break on non-`main` default branches) —
  under the default shallow single-branch
  `actions/checkout` the base does not resolve, turbo/nx fail loud, and
  `vitest --changed` can silently run zero tests and pass green; OPT28's
  shallow-checkout guidance cross-references the exception both ways. OPT35's
  prose now matches its shipped detector: only an explicit `fail-fast: false`
  on a shard-indexed matrix is a finding (GitHub Actions already defaults to
  `fail-fast: true`; the old prose flagged absent `fail-fast`, a no-op), and its
  metadata label corrects `yaml-path-absent` → `yaml-path`. OPT26 names the
  `upload-artifact`/`download-artifact` v3→v4 landmine (immutable, name-unique
  v4 artifacts break same-name matrix uploads; v3 download can't read v4).
  OPT66's tombstone no longer miscounts OPT50 among the CUT ids.

### Removed

- **2026-07-20** — **Pricing punted: the priced-dollar surface is stripped from
  the report; runner-MINUTES are the whole bill/capacity story (owner decision
  2026-07-20).** Per the owner: "estimate the number of minutes and punt on
  pricing — right now it's unnecessary complexity for very little value; we can
  add it back later." A full strip (the score-ectomy pattern), not a render-time
  suppression — the pricing complexity leaves the shipped tree. **Out:**
  `references/runner-rates.json` and `scripts/billing.py` (its rate-free
  `billable_equiv_min` per-job round-up relocated into `collect_runs` as
  `_billable_equiv_min`); the cost-spine `SKU` / `Billing` / `Weighted min/mo` /
  `USD/mo` columns (the table is now a single minutes-only shape); the OPT66
  SKU-arbitrage-ceiling detector; every rate-derived finding stamp (`sku`,
  `sku_class`, `billing_class`, `usd_saving_per_month`, `sku_arbitrage_ceiling`,
  weighted-minute stamps) — the findings JSON keeps every MINUTE fact; the R-row
  `USD/mo` / "Billing class & price" lines and the `$X/mo` in the TOC /
  section-lead / After-the-gate; and the rate-derived `verify_report` checks
  (`check_tier2_billing_class_honest`, `check_sku_arbitrage_ceiling_contract`,
  the USD cross-derivation legs), replaced by one new sweep
  (`check_no_rate_derived_dollars`) that fails a report carrying any
  rate-derived `$`/USD token in its minutes surfaces. The #96 all-unpriced
  lead-in and the capacity/unpriced footnotes are gone (nothing is priced, so
  nothing needs excusing). Maintainer rates-freshness infra
  (`check_rates_freshness.py`, `propose_rates_refresh`, dogfood wiring) is
  mothballed. The entire pricing apparatus is replaced by one sentence in the
  cost-spine lead: *"All figures are runner-minutes; multiply by your runner's
  per-minute rate to get dollars."* **Kept:** raw + billable min/mo everywhere,
  the cost spine (slimmer), `runner_min_saving`, R-row promotion, neutrality
  certificates, the remainder-basis OPT46 sizing, the "take the bill savings
  (~N min/mo)" close option, and the workflow/job/step glossary. Git history
  preserves the pricing infra (#98/#100) for later re-introduction.

### Added

- **2026-07-21** — **Gap-fill evidence-grounding backstop (`check_gap_fill_evidence_grounded`,
  issue #106).** New `verify_report.py` check: every `evidence` line a rendered `🤖 LLM
  root-cause analysis` block quotes must be a verbatim substring of the captured job log it
  claims to read. Converts the phase-4a "ground it in the log" prose rule into a mechanical
  gate against a fabricated or injection-altered quote. Offline and honest about coverage — it
  binds each block to its log by the same (check, workflow) identity `check_pole_drill_belongs_to_its_job`
  uses, matches with the renderer's own transforms (the `_fence_safe` twin + the em-dash
  flatten) applied to the log side, FAILs (naming the offending line) when the log is present
  and a quote isn't in it, and SKIPs loudly (never silently passes) when the captured log can't
  be located. Classified AUTO_SEED / `fabricated-or-unsupported-finding` in `grader_seeds.py`.
- **2026-07-21** — **Two accepted launch residuals documented (SECURITY.md "Accepted security
  residuals"; issues #105/#106).** Plain-language sections stating that the honesty gate is
  agent-invoked with no in-tree tamper detection (mitigated by the #5 burn-in panel, re-derivable
  deterministic output, and an upstream harness-wrapper fix), and that the phase-4a gap-fill reads
  untrusted third-party logs (a prompt-injection surface bounded to one pole's prose reading, never
  a measured number, and now backstopped by the grounding check above). One cross-reference line
  added to SKILL.md's Data-handling section (body stays under the 500-line budget).

- **2026-07-20** — **Empty-spine diagnostics + honest auto-escalation in the data-pass
  summary (`summary.py`, `run.py`, issue #81).** Two consecutive default-target runs on an
  active repo (~766 runs/30d) printed only "No drill logs were captured" and rendered
  static-only; a repro 30 min later at the DEFAULT target recovered the full spine, proving
  the mechanism was TRANSIENT gh state, not sampling depth. The agent-facing summary now, on
  an empty-funnel anomaly (zero poles, or poles resolved but zero drill logs captured) whose
  measured volume says a spine should exist — OR whenever the gh collection outright failed —
  leads with a **reason chain** that walks the funnel from facts the pass already stamped
  (workflows analyzed → runs sampled → PR candidates fetched → PRs carrying a completed
  required suite → required-set resolution → config-era / thin-flip effects → poles resolved
  → drill-log capture) and MARKS the first empty stage with its value (e.g. "16 PR(s)
  fetched, 0 carried a completed required suite in the window"). No gh calls, no response
  bodies (log-hygiene). It then classifies the break as **transient** (a fetch/rate-limit
  gap a plain re-run clears — emits the exact `run.py` re-run command, computed from
  `repo`/`root`) or **durable** (a property of the repo/window — says so, no pointless
  re-run). A **genuinely low-volume repo stays quiet** (the current static-only outcome).
  Escalation is a HINT, not an auto-deepen: the code exposes no deeper knob without a
  redesign, and the repro falsified depth as the cause. The hint deliberately does NOT raise
  `--target` — it is inert for sampling depth (the PR gate sample is fixed at 20 PRs, the run
  sample at `--max-runs`, and `collect_runs` ignores `--target`), so raising it is the very
  mislead #81 documents; the summary warns against it and `run.py`'s `--target` help was
  corrected to stop claiming it gates the gh pass.

### Changed

- **2026-07-21** — **Re-rendered the `microsoft-playwright` worked example on the
  post-wave engine so the public showcase matches the shipped renderer.** The
  committed example (rendered from the same preserved `findings.json`) still
  showed four `disclosed_pre` era caveats — "changed ~N days ago - this audit
  measures the previous configuration" for `fix-flakes.yml`, `roll_nodejs.yml`,
  `roll_stable_test_runner.yml`, and `triage.yml` — that the merged engine now
  suppresses (#122): these are non-spine maintenance workflows with no real
  pre-era measurement, so a global pre-only caveat over them was noise, and the
  renderer now drops it while keeping the `post_only` "narrowed to the current
  configuration" notes that still describe a genuinely excluded era. The example
  was re-rendered from its preserved input (no re-collection — collection stays
  824 gh calls / ~3m07s, waits 39m 58s / ~2m 56s off `ubuntu-22.04 (webkit -
  Node.js 20)`, skill commit `3bb6e2e`), so only the four suppressed caveats and
  the scripts-tree provenance token (`f978505` → `4c21de6`) change; verify-gated
  (`verify_report.py` rc 0), never hand-edited. `pallets-flask` carries only
  `post_only` era notes (no wave-affected `disclosed_pre` construct) and renders
  identically under the merged engine, so it was left untouched.

- **2026-07-21** — **Regenerated both committed worked examples on the de-priced
  engine (issue #103).** `examples/pallets-flask/` and
  `examples/microsoft-playwright/` still rendered the PRE-excision priced spine
  (SKU / Billing / Weighted / USD columns, "bills $0" clauses) and contradicted
  the shipped minutes-only product. Both were regenerated from scratch via the
  real pipeline (fresh `run.py` + `blocking_path.py` runs, verify-gated, sanitized
  — never hand-edited), stamped to on-main skill commit `3bb6e2e`: flask (314 gh
  calls, ~51s) now waits 34s / ~8s off `Windows`; playwright (824 gh calls,
  ~3m07s) waits 39m 58s / ~2m 56s off `ubuntu-22.04 (webkit - Node.js 20)`. Both
  now carry the minutes-only cost spine (no dollar/USD/SKU tokens) and pass
  `verify_report.py` including `check_no_rate_derived_dollars`. Each example's
  `README.md` and the top-level `examples/README.md` headline figures were
  restated to the fresh measured numbers.

- **2026-07-20** — **A workflow/job/step glossary (issue #96).** A one-line
  **workflow ▸ jobs ▸ steps** glossary now renders once in `blocking_path.py`,
  under the `## 🗺️ Long pole map` heading (the first place all three collide),
  and only when that section renders. (The same issue's all-unpriced cost-table
  presentation change was reverted by the 2026-07-21 pricing punt above.)

- **2026-07-20** — **Phase-1 gh gate + fast-CI close preface (SKILL.md, owner UX).**
  Two graceful-degradation edits. (1) **The gh gate:** if `gh` is missing or
  unauthenticated, phase 1 now STOPS before anything else and tells the user plainly
  what's needed and why — the audit measures real CI runs over the GitHub API, so
  without an authenticated `gh` the merge-wait numbers are unavailable and only a
  config-pattern scan remains — with the install path (https://cli.github.com,
  `brew install gh`, `gh auth login`); static-only proceeds ONLY on the user's
  say-so. Previously the first contact was a raw `gh: command not found` in the
  repo-resolve step, and recovery depended on the agent connecting it to the
  Requirements line unaided. (2) **Fast-CI preface:** when the typical merge wait is
  under ~2 minutes, the phase-6 close now OPENS by saying the CI is already in good
  shape and nothing needs changing unless a finding is a cheap, glaring easy win —
  same menu, honest framing, so a well-optimized repo isn't nudged into unnecessary
  churn. Adjacent SKILL.md prose compressed to hold the <500-line budget (499).

- **2026-07-20** — **Interaction contract names Codex's `request_user_input`
  (follow-up to #83).** Codex's App Server protocol carries an experimental
  `tool/requestUserInput` (1–3 short questions, options + an `isOther` free-form
  escape) that a SKILL.md instruction can nudge the agent to call where the host
  exposes it — the live Codex run fell back to typed prose because nothing named
  it. The contract now reads "your platform's structured-question tool where one
  exists" with both examples (`AskUserQuestion` on Claude Code, `request_user_input`
  on Codex), and the plain-message fallback tier is phrased so the default answer
  is one keystroke ("Reply **y** to audit <owner/repo>, or name a different
  repo/path"). Adjacent prose re-tightened to hold the body under the <500-line
  budget (499).

- **2026-07-20** — **Phase 1 & 6 interaction contract is now tool-agnostic (SKILL.md,
  issue #82).** The installer ships ci-speedup to 13 agent types, but phase 1's repo
  confirm ("via the AskUserQuestion tool") and phase 6's close menu (one page, save option
  last, verbatim `None, just save the report (.md)`) were keyed to Claude Code's
  AskUserQuestion tool and silently degraded on agents that lack it (a live Codex run fell
  back to open-ended prose and a numbered list with no save option + machinery narration).
  Both sites now reference a single **Interaction contract** stated once atop `## Phases`:
  deliver **via AskUserQuestion where available**; on an agent without a structured-question
  tool, ask the **same** question as **one plain message** — same options, same order, same
  ≤4-option fold, phase 6's save option still **last** and verbatim, nothing open-ended, no
  re-offer after a save pick. The owner-dictated verbatim label and all close semantics are
  byte-identical and agent-independent; only the delivery mechanism varies. The NEVER-list
  "question label" clause and the eval's close description were generalized the same way, and
  adjacent phase-6 prose was tightened to hold the SKILL.md body under its <500-line budget.

- **2026-07-19** — **Long pole map is now the full blocker cascade, not just level 1
  (`blocking_path.py`).** The 🗺️ Long pole map (restored after the Contents in #75)
  rendered only level 1 — the flat race of merge-gating checks. Owner-approved: it now
  descends the whole cascade in one `text` fence, restoring the multi-level arrow idiom
  from the pre-split reports. **Checks-first** (GitHub gates on checks, which race
  concurrently): level 1 is the flat check race (each bar now labelled `{check} ·
  {workflow file}`); the **◀┐** hangs from the row of the check the drill descends into
  (pole 1); **level 2** breaks that check into its stamped **steps** (share of the
  check's own p50, via a new backward-compatible `pct_denom` on `_emit_level`, no
  roll-up row); **level 3** opens the dominant step's internals ONLY when the drill
  actually has them (`deeper` first level ≥2 rows). Degenerate levels collapse (never a
  one-bar level); if level 2/3 can't draw it falls back to the level-1-only #75 render,
  and a single-check/single-step doc skips the section. Still **presentation-only** — no
  claim / verify check binds to any map line; level 1 is the SAME typical-PR `src` set the
  Contents draws. The descent pole's leaf is derived once (`_derive_pole_leaf`, shared via
  `_pole_leaves`) so the map's level 3 and the pole section's level 3 can never disagree.

- **2026-07-19** — **Owner-directed layout: Long pole map restored after Contents, a
  single Bottom-line block, and check/step name-collision disambiguation
  (`blocking_path.py`).** Reviewing fresh output the owner found the trimmed report's
  "levels" hard to follow, a check/step name collision that read as self-contradiction,
  and multiple top-matter blockquotes where only one was wanted. Three edits: (1) a new
  **🗺️ Long pole map** section placed AFTER the Contents and before Long pole 1 —
  restores the Level-1 cascade ASCII (reusing the pre-#73 bar-chart machinery: the gate
  bar is marked ◀) wrapped in plain-worded lead + a connector into the per-pole
  drill-downs, so the parallel-checks → gate → serial-steps cascade reads at a glance
  again. It is presentation-only (nothing binds to it; the Contents list + stamped
  fields stay the source of truth), so the restoration does not re-couple any verify pin
  to the chart. (2) **Top matter = metadata table → ONE Bottom-line blockquote →
  Contents, nothing else:** the config-era caveat (it qualifies the headline number),
  the fileless/managed disclosure, the chain model-check, and the "After the gate"
  runner-minute line all FOLD into the single Bottom-line blockquote as `>`-continued
  paragraphs (every claim + era guard stays green — they are text-anchored, not
  position-anchored); the bill-scope methodology note MOVED down into 🗄️ Data sources
  (it sizes total compute, not the headline). (3) **Check/step name-collision
  disambiguation:** when a pole's CHECK name collides with a small STEP name inside it
  (case-insensitive, and the step is not the dominant one — e.g. the check `test` IS the
  gate while a 31s `Test` step reads as "so test isn't the bottleneck"), the pole header
  gains ONE clarifying clause naming the collision + the real dominant step, and the
  Level-2 waterfall lead is strengthened to "its **steps** (not checks)". Collision-
  triggered, never boilerplate. Review fix (#75, Greptile P2): the collision helper now
  bails when the pole has no known dominant step, so it can never render the self-
  contradictory "its small `X` step is not the bottleneck — the dominant step is `X`".

- **2026-07-19** — **Report layout pass (owner-requested): consolidated provenance,
  a single-page top matter, plain-text Contents links, and the Level-1 chart
  removed (`blocking_path.py`).** Five rendered-report edits: (1) the prose
  provenance block ("Where this data comes from") no longer renders after the
  Contents — it is consolidated, with all narrative methodology, as the lead block
  of the bottom **🗄️ Data sources** section (claim-adjacent citations, e.g. each
  drill's representative-run label, stay put). (2) The standalone headline paragraph
  is gone: its claim sentence(s) FOLD verbatim into the Bottom-line blockquote as a
  continuation (keeping every headline/framing regex green — they are
  position-independent), and its orientation tail ("the N heaviest checks run in
  parallel … an ASCII drill-down … does not prescribe the fix") is dropped. (3)
  Contents critical-path entries render as plain-text anchor labels
  (`[test](#pole-1)`) instead of a backticked code span that read as a non-link code
  chip. (4) All provenance/derivation narrative is swept into Data sources (see #1);
  body sections keep claim-adjacent citations only. (5) The top-level **Level-1 ASCII
  chart** ("what a typical PR waits on") is removed — it duplicated the Contents
  critical-path list; its ◀ "the gate" signal survives as a `(the gate)` tag on the
  first Contents row (emitted only when the frequency gate is the slowest single
  check, never on a `needs:` chain). Per-pole Level-2 waterfalls are untouched.
  The provenance prose is also scrubbed of the now-orphaned "Level 1"/"levels 1-2"
  numbering (no visible numbered-level heading survives the chart removal): "Which
  checks gate (level-1 ordering)" → "(the critical-path ordering)", and the
  "levels 1-2" / "level-1–2 audit above" qualifiers are dropped.

- **2026-07-19** — **Single-page close menu — save-report fused into the
  always-last option; tabs never used (SKILL.md phase-6 close contract).** The
  owner's screenshot of a live run showed the overflow rule below (#70) split the
  close into TWO questions in one AskUserQuestion call — and Claude Code renders
  multiple questions as **tabs** behind a separate Submit, so "Save the full
  report" became a hidden second tab the owner never saw (worse than the overflow
  it fixed). This replaces that two-question rule entirely: the phase-6 close is
  now **one question, one page, never tabs**. The last option is **always**,
  verbatim, **`None, just save the report (.md)`** — it fuses declining fixes with
  saving the already-verified report to `./ci-speedup-findings-report.md`, and
  because that pick explicitly declines fixes the menu is **not** re-offered
  afterward (close with one line naming the remaining levers). Slots 1..3 are the
  fix options (per-pole top-first, then "Fix all gating checks" at ≥2 poles, then
  the Tier-2 bill-savings option); extra per-pole options **fold** into "Fix all"
  so the total including the always-last save option never exceeds 4. The
  standalone "nothing for now" option is gone (declining without saving is the
  tool's own Esc / free-text). Dead-end repos keep the Tier-2-first exception with
  the save option still last.
- **2026-07-19** — **Close-menu overflow rule + structured post-save re-offer
  (SKILL.md phase-6 close contract) (#70).** A live `/ci-speedup` run on a
  multi-pole repo built one AskUserQuestion with 2 per-pole options + Tier-2 +
  "save the full report" + "none" — 5 options past the tool's 4-option cap —
  producing a visible `Invalid tool parameters` error, then a retried menu with
  options silently dropped. The phase-6 contract now carries an explicit overflow
  rule: when candidates exceed 4, split into TWO questions in the SAME
  AskUserQuestion call — Q1 = the fix selection under the existing ordering
  (per-pole, then fix-all, then Tier-2, then "nothing for now"; if still over 4,
  per-pole options collapse into fix-all until Q1 fits the 4-option cap — which
  also closes the 2-pole + Tier-2 case), Q2 = "Save the full
  report?" (Yes / No) — so the report option is never one of the choices squeezed
  out. When candidates fit in 4, the single-question form is unchanged. Separately,
  the post-save re-offer (which the same run rendered as prose) is now pinned to
  "re-offer the fix selection via the same AskUserQuestion form (every choice in
  this skill is structured)". Adjacent close prose was tightened to keep the
  SKILL.md body under its <500-line progressive-disclosure budget (still 499).
- **2026-07-18** — The phase-1 repo-confirmation gate now mandates the
  structured question tool (one confirm option; a different path/repo via the
  built-in Other free-text) instead of leaving the ask open-ended — real runs
  rendered it inconsistently (sometimes a structured question, sometimes prose).
  Every user choice in the skill is now uniformly a structured question.
- **2026-07-18** — **Fix completion re-offers the report's remaining findings
  (SKILL.md phase-6 close contract).** A real `/ci-speedup` run drove one picked fix
  (a 6m10s serial mutation-testing step sharded out of the merge gate) to completion,
  and the report's other surviving finding — a 914 min/mo runner-minute bill lever —
  then evaporated: the close path said to act on a pick "rather than re-offering", with
  no instruction to restate what was left once the fix landed. The phase-6 fix path now
  distinguishes the pre-commit **pause** (still no re-offer there — unchanged) from fix
  **completion** (it lands, or the user closes it out): on completion, restate the
  report's remaining findings as the next-step question (one orienting line + the
  still-open options) so remaining levers never silently evaporate after a fix arc.
  Wording tightened to keep the SKILL.md body under its <500-line progressive-disclosure
  budget (now 499). Catalog (`references/optimization-patterns.md`): OPT24 (Long Test
  Job Without Sharding), OPT25 (Shard Imbalance — split a leg into new jobs), and OPT22
  (consolidate `workflow_run` workflows) each gained a **Required-checks caveat** — a fix
  that moves work out of a check into new jobs must re-add those jobs to branch protection
  as required checks, or the split-out work silently stops gating merges.

### Fixed

- **2026-07-21** — **A matrix-templated job whose own `name:` contains `" / "` now binds to its
  job instead of being mis-parsed as a reusable-workflow separator (`collect_runs.py`, issue
  #118).** `_check_to_job_node` resolved the reusable-workflow `<caller> / <child>` `" / "` split
  FIRST, so a job named `test / ${{ matrix.type }}` — whose matrix leg expands to the check-run
  `test / ethereum` (a real recurring gate) — had the `" / "` inside its OWN name read as the
  caller/child separator, and the unexpanded `${{ matrix.type }}` template could not equality-match
  the expanded check name. The resolver returned None → the pole rendered `workflow_file=None` and
  the summary demoted it as "fileless/managed, don't investigate." The resolver now tries a
  SAME-WORKFLOW job match first (sampled-timing anchor, then the scanned-graph template match via
  the literal-prefix-anchored regex, so a triage-skipped workflow's templated job still binds), and
  falls back to the reusable `" / "` split only when NO same-workflow job produced the check — so a
  genuine reusable child (`<caller> / build`, matching no same-workflow job) still resolves to its
  caller, and a name matching both a same-workflow templated job and a reusable parse resolves to
  the same-workflow job. The deliberate cross-workflow same-expanded-name refusal (issue #59) is
  preserved: when two workflows each carry a matrix `test` job whose legs expand to the identical
  check name (reth's live super-case — `unit.yml` + `integration.yml` both with an `ethereum` leg),
  the check stays genuinely unpinnable and the resolver still returns None rather than guess a file.
  Because the reusable `" / "` split is now the last resort, a `" / "`-bearing check first goes
  through the scanned template pass; a FOREIGN matrix job named with a LEADING placeholder
  (`${{ matrix.variant }} / build` → `^.+? / build$`, whose `.+?` eats across the `" / "`) would
  otherwise template-match a genuine reusable check (`Suite / build`) and preempt its real caller.
  All three resolver paths — the sampled-timing-anchored step-1 loop in `_check_to_job_node`, the
  scanned `_check_to_job_node_scanned`, AND the sibling `_check_to_workflow_file_static` (used
  directly by the structural-finding callers) — refuse a leading-placeholder template match ONLY
  when a genuine reusable caller actually competes for the check (`_reusable_caller_claims`: a
  reusable job whose name equals or `<name> / `-prefixes the check). The step-1 guard is further
  scoped to a NON-direct anchor: when `_map_check_to_job` anchors a workflow because it DIRECTLY
  sampled a job whose name token-equals the check (exact tier — that file definitively produced the
  check), its own matrix template keeps the match and a same-named reusable caller in ANOTHER file
  can't steal it; only a weaker token-SUBSET anchor defers to the reusable caller. Exact timing
  proves only that the WORKFLOW ran the check, though — not that the matrix template (vs a co-resident
  reusable caller) owns it — so direct ownership is treated as present ONLY when no reusable caller in
  that same workflow claims the check; a same-workflow reusable caller keeps precedence over the
  leading-placeholder matrix template. Literal-prefix
  templates (`test / ${{…}}`), ordinary leading-placeholder matrix checks without a `" / "`
  (`${{ matrix.os }}-build`), a LONE leading-placeholder `" / "` job (`${{ matrix.variant }} / build`
  producing `linux / build`), AND a file that directly sampled the check all still bind — the refusal
  is scoped so a real matrix job never reads fileless — while a genuine reusable caller still wins
  its own `<caller> / <child>` check on the no-direct-owner paths (PR #126 review, six Greptile P1s).
  **Honest-labeling arm (same #118):** that unpinnable cross-workflow super-case is no longer
  mislabeled as a fileless gate. `_decompose_pole` now stamps the pole with `ambiguous_workflows`
  (the sorted candidate workflow set, from `_check_producing_workflows`) whenever a check has real
  developer job timing but maps to no single file because >1 workflow produces it under the same
  name. The agent-facing summary (`summary.py`) reads that stamp and, instead of "Fileless/managed
  check … Don't investigate its gating manually," emits "`<check>` is a REAL CI job (P50 …) that
  more than one workflow (<named workflows>) produces … This DOES warrant investigating: rename one
  job (or its matrix leg) …" A genuinely fileless bot/app check never carries the stamp, so its
  framing stays byte-identical. The rendered report already framed the check honestly (shown in the
  timing bars, disclosed as "a job defined across multiple workflows," excluded from the fileless
  disclosure), so only the summary needed the flip.
- **2026-07-21** — **`verify_report` no longer false-fails on a transitive `needs:` chain member
  of the modal gate chain (`verify_report.py`, `blocking_path.py`; #112).** The renderer correctly
  drills a modal-chain member on-spine ("Stage N/M of the gate chain — its time ADDS"), but verify's
  shared typicality predicate (`_vr_typical_predicate`, feeding both `_rare_demoted_check_names` and
  `_non_typical_pole_check_names`) recognized on-spine membership only via literal `required_checks`
  membership or a recurring `pole_n` — a chain member (home-assistant/core `Prepare dependencies
  (3.14.5)`, stage 2/3 of `Collect information … → Prepare dependencies → Check hassfest` feeding the
  required `Check hassfest`, `pole_n` 0) has neither, so verify mis-classified it "spine-demoted" and
  failed the on-gate framing the renderer rightly used ("no spine-demoted pole carries the
  typical-gate framing"). Fixed at the SHARED predicate — a member of the modal gate chain (re-derived
  from `chain_facts` via `_vr_modal_chain`, single-door #19 — never the renderer's reduced
  `chain_summary`) whose chain FEEDS a required check (verifier-only sink-in-`required` guard) is now
  ON-spine, so all demotion legs agree with the renderer. Secondary
  (`blocking_path.py`): the per-pole agent-prompt gate line for a chain member no longer renders the
  misleading "Slowest check a typical PR waits on: P50 37s" for a mid-chain stage — it now frames the
  stage against the chain total ("Stage 2/3 of the `needs:` gate chain (chain P50 1m 56s); this
  stage: P50 37s. Its time ADDS on the gate path — the chain, not this single stage, is what a
  typical PR waits on."). A genuinely spine-demoted pole framed on-gate still FAILS.
- **2026-07-21** — **OPT29 (merge-queue step-level skip) no longer credits more runner-minutes
  than the flagged job physically burns (`collect_runs.py`, refs #113).** On a `merge_group` repo
  the flagged job provisions a runner but skips every step; the waste is confined to that ONE job,
  yet OPT29 priced it off the WORKFLOW long pole × full volume — crediting the whole run's compute
  to a step-skip on a tiny gate. Live biome (biomejs/biome): the `changes` gate was credited
  1290.7 min/mo against its own 823 min/mo of measured billable compute — a physically-impossible
  saving that `check_saving_within_measured_compute` failed on. OPT29 now carries
  `cost_basis: affected_jobs` and flows through the SAME DERIVE machinery as OPT45
  (`_reground_whole_run_cancel_saving`): the saving is `hit_rate (the merge_group-run share) × the
  affected job's MEASURED billable` from the cost spine — within the physical bound by construction
  (hit_rate ≤ 1), so biome's `changes` credit becomes 82.3 min/mo (≤ 823). The credited figure is
  disclosed as a CEILING (only runner provisioning is wasted; the steps already skip), and an
  affected job that never joins the spine is unsized (omit rather than fake), matching the OPT45
  discipline. Sibling audit: OPT35's STATIC fallback shared the same defect (it names one matrix
  job but priced it off the workflow long pole) — a shard matrix that is not the long pole could be
  credited more than the matrix job's own measured billable; it now carries `scope: "job"` (like
  OPT30/31/40) and sizes off the affected job's own p50. OPT35's MEASURED detector (post-failure
  sibling waste, a subset of the job's compute) and OPT36 (measured schedule-run basis, with empty
  `affected_jobs` so the per-job bound never applies; static fallback spans all workflow jobs, so
  its bound is their sum) were left unchanged — both are physically bounded by construction.
- **2026-07-21** — **A non-PR-gating workflow's config change no longer globalizes a "the whole
  report reflects the OLD/thin config" caveat (`collect_runs.py`, `blocking_path.py`,
  `verify_report.py`; issue #116).** The era machinery stamped a straddle fact for EVERY sampled
  workflow that changed in-window, and the renderer globalized any `disclosed_pre` / `post_only_thin`
  fact into the loud top-of-report ⚠️ caveat — even when the straddling workflow never runs on
  `pull_request`/`merge_group` and contributes ZERO checks to the PR-gating spine, so it cannot
  affect the headline the caveat impugns (live: astro `build-sandbox-image.yml`, `on: push[main] +
  workflow_dispatch`, 0/33 spine checks; biome `preview.yml` + `repository_dispatch.yml`,
  non-PR-gating (`workflow_dispatch + schedule`) — 1 + 2 overreaching caveats). The GLOBAL caveat is now scoped to spine-relevant straddles:
  `collect_runs._era_stamp_spine_relevance` stamps `spine_relevant` (a `developer_event` AND a
  `kept_checks`/`other_era_checks` spine check) once the enumeration is bound. `developer_event`
  combines two signals, strong-first, so a STALE trigger read can never veto real evidence: STRONG is
  a kept/other check the DEVELOPER-TIMED mapper pins to the workflow (direct proof it produced a
  PR-gating check in the sample — this is what keeps a workflow that DROPPED `pull_request` in the new
  config, whose PRE-era `disclosed_pre` kept check is a genuine gate but whose fetched HEAD `on:` is
  now push-only, from silently losing its caveat); WEAK is the static `on:` parse via the canonical
  `_on_has_pr_trigger` (`pull_request` / `pull_request_target` / `merge_group`, incl. the fork-PR
  gate), needed only to confirm a check attributed via the timing-less job-graph scan. A straddle
  whose workflow doc is UNKNOWN (absent from `wf_docs`) AND lacks a strong signal is left unstamped
  rather than asserted non-gating, so the enumeration-set fallback preserves its disclosure on a
  missing read. `blocking_path._era_fact_spine_relevant` gates the two loud caveats on the stamp
  (falling back to the enumeration sets for legacy artifacts). The fact is NOT dropped — every other
  era consumer (the Data-sources bill-scope note, the `post_only` narrowed note, the cost-spine
  full-sample note, era-scoped bill figures) keeps reading the same `config_eras`, so a non-gating
  workflow's runner-minute staleness still surfaces in the bill-scope note. `verify_report`'s
  `check_era_disclosure_matches_enumeration` gains a converse leg (a global caveat rendered for a
  spine-irrelevant straddle FAILs, plus a `spine_relevant`-stamp-integrity arm), and
  `check_config_era_boundary` no longer demands EITHER loud disclosure (pre `disclosed_pre` or the
  `post_only_thin` provisional note) for a spine-irrelevant straddle — both filtered through the same
  `_era_fact_spine_relevant`, so the renderer's suppression and the guard's demand stay in lockstep.
- **2026-07-21** — **The headline crown binds to the cluster's OWN presence on the
  merge-gating spine, not its workflow's required-hosting (#114, `blocking_path.py` +
  `verify_report.py`).** The crown-eligibility path treated "the workflow hosts a required
  check" as license to crown ANY cluster in that workflow, so an off-spine `Run pytest`
  matrix (its jobs dropped from the required-scoped spine, whose workflow gates via a
  *different* required check) headlined a ~9m08s "biggest single measured win" — 4.7× the
  entire 1m56s merge wait (home-assistant/core). `_is_credited_cluster_lever` now excludes
  findings stamped `off_spine=True` (the same exclusion the credited long-pole selection
  already applies), so an off-spine cluster demotes to the bill/Also-noticed side and the
  honest bottom line leads with the real gating levers. New verify leg
  `check_headline_cluster_lever_on_spine` re-derives the crown from the stamped `off_spine`
  fact and FAILs an off-spine crown; the burial and presence-eligible guards now exclude
  `off_spine` from their credited set so the required demotion no longer trips the "no
  burial" invariant.
- **2026-07-21** — **The chain headline leads with the observed wall when the makespan
  materially exceeds the chain sum (#115, `blocking_path.py` + `verify_report.py`).** A
  serial `needs:` chain also carries the queue gaps between its stages, so the chain sum
  UNDERSTATES the real wait. The bottom line led with a 16m18s chain sum while the report's
  own Model check said the observed per-PR wall was ~69m04s (divergence −76%) and advised
  "Budget on the observed wall" — the headline led with the number its own note told the
  reader not to budget on, and the close reused it verbatim (withastro/astro). When
  |divergence| exceeds the Model-check threshold (25%), the "typical PR waits / until all
  checks finish" WALL now leads with the observed makespan and the chain sum is demoted to
  its attribution role — the honest arm the milder-divergence lead already took, unified
  onto the chained-gate shape. New verify leg `check_headline_wait_is_divergence_correct`
  re-derives makespan/chain from `chain_facts` and FAILs a chain-sum lead beyond threshold.
- **2026-07-21** — **Scrubbed the internal dev repo's name from shipped surfaces and added a
  content guard so internal identifiers can't ship again (issue #117, a regression of the #60
  neutralization).** The internal repo name leaked 10× into reader-facing files that ship to
  end users: `CHANGELOG.md` (6×), `ARCHITECTURE.md` (2×), a `blocking_path.py` code comment,
  and `references/before-after-verification-spec.md` — plus test-file prose (`test_config_era_boundary.py`,
  `test_blocking_path.py`, `test_structural_findings.py`). All neutralized to a generic slug
  (`internal-dev-repo` / "live dogfood repo") in the #60 style; every technical fact (check
  names, P50s, run boundaries) is preserved, and the test fixtures already used synthetic
  `acme/*` slugs for their data (only prose mentioned the real name). New content-guard test
  (`test_no_internal_identifier_in_tracked_files` in `tests/test_skill_install_surface.py`)
  sweeps a denylist of internal identifiers (the internal repo name plus internal org/staging
  slugs — spelled out only in the test's own allowlisted denylist, never in prose) over every
  git-tracked file, closing the #117 R4 gap (the install-surface checks were name-pinned to
  file leaks, not content). The guard is case-insensitive and byte-aware (utf-8 + utf-16, so a
  stray bad byte or 2-byte encoding can't mask a leak), scans its own file too with a
  line-scoped `content-guard:allow` marker (rather than a whole-file allowlist that could hide
  a later leak), and skips cleanly when git is unavailable. Also gitignored the maintainer-local
  `.ci-speedup-dogfood/` capture dir beside the other loop capture dirs, and added it to the
  precious-capture / never-delete warnings in `CLAUDE.md`, `CONTRIBUTING.md`, and `SECURITY.md`.
- **2026-07-21** — **The Long pole map's level-1 lead no longer contradicts its own rows when a
  minority-present check is drawn (`blocking_path.py`).** A level-1 row can be "typical" — kept in
  the drawn check set — by pole FREQUENCY (it was the actual per-PR gate on enough PRs) or the
  required-check exemption, yet have run on only a MINORITY of sampled PRs. Under the fixed
  "checks racing on every PR; the merge waits for the slowest" lead, such a row (e.g. the
  playwright `Windows (firefox)`, 72m57s, present on 2/20 PRs) read as the normal blocker —
  self-contradicting the report's own typical-wait headline. Now, when any drawn level-1 row is
  minority-present (mirroring the presence filter's `present <= npop*_RARE_PRESENCE_FRAC`
  threshold and its `npop >= _RARE_PRESENCE_MIN_PR` small-sample guard), the map marks each such
  row's DISPLAY label with a trailing ` †` (added before truncation/width-padding so the
  fixed-width bars and connector columns stay aligned), reframes the lead to "checks racing on a
  typical PR … — rows marked † ran on a minority of sampled PRs (path-conditional — they gate only
  the PRs that trigger them)" (the `needs:` chain-variant lead carries the same clause), and adds
  one legend line naming each marked row with its real fraction (e.g. "† `Windows (firefox)` ran
  on 2/20 sampled PRs"). Presentation-only: the marker is on the rendered label alone — never on a
  `present`/pole key or any match key, so the descent-pole lookup, name-collision helper, and
  claims/verify layers are untouched; the `†` passes `_clean_label`/`_fence_safe` unchanged. When
  no drawn row is minority the output is byte-identical to before (the flask example and every
  existing fixture are unchanged). Both committed worked examples were regenerated on the fixed
  renderer (only the playwright map's lead/†/legend changed; flask's map is unaffected because it
  opens the fence at level 2).

- **2026-07-21** — **Repo-controlled free text can no longer break out of the report's
  Markdown fences or desync the verifier (`blocking_path.py`, `tests/verify_report.py`).**
  GitHub check/job/step names and verbatim captured job-log / workflow-YAML "evidence" lines
  were dropped into the rendered report unescaped. A name or evidence line containing a
  triple-backtick closed a ```` ```text ```` fence early — the rest of the report rendered as
  broken Markdown on GitHub — AND desynced `verify_report`'s own
  `re.findall(r"```text\n(.*?)```")` fence split, so the identical stray fence fooled the safety
  net with the same input. Embedded newlines and control chars also broke heading lines, table
  columns, and the fixed-width ASCII bars. Now a centralized `_fence_safe(s)` (defuse any run of
  >=3 backticks to an equal-length apostrophe run, collapse embedded newlines/CRs to a space,
  drop dangerous control chars — byte-identical on clean single-line input) is folded into the
  canonical name normalizer `_clean_label`, so every repo NAME (pole heading, ```` ```text ````
  waterfall labels, and agent-prompt fences) is neutralized at one chokepoint that can't drift;
  the pole heading additionally wraps the check as an inline code span (`_safe_span`, like the
  workflow file) so a `*`/`_`/backtick can't render as formatting. Verbatim evidence lines
  (which bypass `_clean_label`) are fence-safed at every emission site, prompt bodies per-line via
  `_fence_body`, and table/prose cells via `_flatten_cell`. `verify_report._strip_scope` mirrors
  the `_clean_label` transform (kept verbatim-coupled by `test_s1a`) so the comparators stay
  aligned, and a new defense-in-depth check `check_fences_balanced` FAILS loud if a CommonMark
  fence walk ever ends still inside a fence — catching a future renderer regression instead of
  silently mis-splitting. The `#pole-N` anchors/TOC links key off the integer pole index, not the
  heading text, so the display change leaves anchor resolution untouched.
  Review follow-ups (same PR): the pole heading now wraps the workflow FILENAME with `_safe_span`
  too — a repo may name a `.github/workflows/*.yml` file with a backtick, and the old raw
  `` `wf_base` `` wrap let a single backtick close the heading's inline span; the pole-gate-prompt
  `Claim` now stores `_fence_safe(wf)` so its `rendered` sentence stays byte-identical to the
  `_fence_body`-emitted prompt line (a >=3-backtick filename otherwise defused on emit but not in
  the manifest, false-failing `check_claims_cover_framing_vocabulary`); and a new
  `test_s1a_fence_safe_stays_coupled_to_the_engine` pins the duplicated `_fence_safe` transform
  (bodies, regexes, and behavior) equal across `blocking_path.py` and `verify_report.py`, since
  the existing `test_s1a` return-line pin did not see past the `_fence_safe(...)` call.

- **2026-07-21** — **Docs no longer deny the PyYAML dependency; Python floor
  documented and reconciled (`README.md`, `SKILL.md`, `pyproject.toml`,
  `scripts/scan.py`).** `scan.py` hard-exits without PyYAML (it parses workflow
  YAML), but `README.md` promised "standard library only, no third-party
  packages" and `SKILL.md` said "stdlib-only scripts" — a stranger on a clean
  machine crashed on the first script against a promise the docs had just made.
  The docs now name **PyYAML** as the one third-party dependency (`pip install
  pyyaml`) and state the **Python 3.9+** runtime floor (confirmed by census: the
  code uses `str.removesuffix` (3.9) and no 3.10+ syntax — no `match`/`case`, no
  `tomllib`, no runtime `X | Y` unions, `from __future__ import annotations`
  everywhere). `pyproject.toml`'s `requires-python` corrected `>=3.10` → `>=3.9`
  to match reality. `scan.py`'s error message now names the exact install
  command. `collect_runs.py`'s yaml import stays a soft fallback (unchanged).

- **2026-07-21** — **`check_no_rate_derived_dollars` closes two bypasses
  (`tests/verify_report.py`).** The safety net for the #104 pricing punt missed
  (a) spelled-out figures ("42 dollars per month", "1200 cents/mo") — the regex
  now matches `\bdollars?\b|\bcents?\b` alongside `$N`/`USD`; and (b) a
  `$`-figure on a 4-space-indented line, which the visible-lines filter dropped
  as a code block — the sweep now scans indented lines too (a new
  `_nonfence_markdown_lines` helper still skips fenced blocks, so legitimate `$`
  inside agent-prompt / shell-echo fences stays exempt). The one sanctioned
  methodology sentence ("multiply by your runner's per-minute rate to get
  dollars") legitimately ends in "dollars", so its exact phrase is stripped
  before matching and still PASSES. Pins added for all four cases.

- **2026-07-20** — **OPT46 sizes the cancellable OVERLAP REMAINDER, not whole superseded
  runs (`collect_runs.py`, `references/optimization-patterns.md`, issue #89).** `cancel-in-progress`
  cancels a superseded run the MOMENT its successor starts, so the reclaimable compute is only
  the portion that run would have burned AFTER that moment — the overlap remainder — not the
  whole run. OPT46 previously credited `superseded_count × whole mean-per-run compute`, so a run
  superseded 30s before its natural finish was charged its FULL cost; the credited "lower" end of
  the range was therefore NOT a lower bound of reclaimable compute (an independent reviewer's
  2026-07-20 Codex finding, hand-verified). The detector now measures, per superseded run *i*,
  `remainder_i = end_i − (earliest later start < end_i)` and credits the mean per-run compute
  **pro-rated** by `Σ(remainder_i / duration_i)` — an "effective superseded count" in [0, count]
  — instead of the whole-run figure (per-second compute is unknowable because a run's jobs run in
  parallel, so the mean is scaled by each run's wall-clock remainder fraction; disclosed as such).
  The naive `Σ(runs−1)` whole-run figure survives ONLY as the loose UPPER bound; the old
  overlap-confirmed × whole-run figure is now NEITHER bound. Degenerate guards: a run missing/with
  an unordered timestamp contributes to neither the count nor the credit and is disclosed as a
  skip (`superseded_skipped_missing_ts`, never a crash); the remainder is clamped into
  `[0, duration]`. The remainder ratio is computed on the sampled window and applied BEFORE the
  30-day volume extrapolation. The basis ratio is stamped (`superseded_remainder_ratio` /
  `_units` / `_seconds`) so the credited figure and any downstream re-derivation share ONE number
  (single-door discipline); the evidence sentence, `_measured_evidence` rows, and note name the
  remainder basis and the mean-compute pro-rata honestly (count from the all-status slice, price
  from timed PR-success runs, remainder ratio from all-status timestamps — different populations).
  `verify_report`'s `post_completion_waste` corroboration now requires the remainder basis in both
  the signal and the rendered evidence.

- **2026-07-20** — **Check/step name-collision clause now fires on drilled poles: the
  helper reads the timeline's real `name` key, not `step` (`blocking_path.py`, #92).**
  `_check_step_collision` (the owner-ordered #75 disambiguation for a check whose name
  collides with a small non-dominant step inside it, e.g. the check `test` gating while a
  35s `Test` step reads as "so test isn't the bottleneck") built its step-name list with
  `s.get("step")`, but the captured `*.steps.json` timeline `collect_runs._step_timeline`
  writes is keyed **`name`** (`name`/`number`/`start_s`/`dur_s`). Every drilled pole carries
  a timeline, so the scan saw only empty names and returned no-collision — the clause had
  plausibly never fired on a real drilled pole, only on undrilled ones (confirmed live on
  internal-dev-repo 2026-07-20). Fix: a single `_tl_name(s)` accessor (`name` then `step`)
  that BOTH the collision helper's name-sources — the captured timeline (`name`) and the
  pole's P50 step list (`step`) — now funnel through, so the scan reads the writer's real
  key from one boundary; every other timeline consumer (`_dom_index`, `_emit_gantt`,
  `_dominant_step_from_timeline`, `_audit_links`) already read `name` and is untouched. The
  `_COLLISION_TIMELINE` unit fixture — hand-built with `step` keys, a shape the capture
  pipeline never produces, which is how the bug passed its own test — was rebuilt to the
  real capture schema; added a schema-parity pin (invokes the `collect_runs` writer and
  asserts the reader accepts exactly the keys it emits, so a future writer-side rename fails
  a test instead of silently killing the clause) and a live-shape regression pin (a
  real-schema pole modeled on the live capture renders both the role clause and the
  "**steps** (not checks)" waterfall lead).

- **2026-07-20** — **Lockfile provenance probe walks up to the current-CLI
  grandparent layout (`run.py`, issue #91).** A fresh install via the current
  skills CLI stamped `installed:unversioned` even with a matching lockfile entry:
  the current CLI writes `~/.agents/.skill-lock.json` (the skill root's
  GRANDPARENT), where earlier CLIs wrote `~/.agents/skills/.skill-lock.json` (the
  parent), and `_skill_lock_provenance` probed only the skill root and its parent.
  The probe now does a BOUNDED upward walk of three levels — skill root, parent,
  grandparent, NEAREST first — stopping at the first lockfile that parses AND
  carries a matching entry. A parseable lockfile with no matching entry does NOT
  stop the walk (a higher-level lockfile may hold the entry); nearest-first means
  the closest lockfile wins a tie. The walk never reads above the third level. All
  prior behavior held: entry matching by dir basename + sole-entry fallback,
  `installed:<hash12>` form, `installed:unversioned` terminal fallback, and the
  never-raise `(OSError, ValueError)` net.

- **2026-07-20** — **Identical-lever matrix sibling legs collapse to one compact line
  in the structural root-cause section (`blocking_path.py`, #53).** A live internal-dev-repo
  run rendered FOUR near-identical OPT75 blocks inside one pole: the pole's own structural
  block plus a full "Sibling matrix leg `guard shard N/4` also carries a structural lever"
  block for each of the three faster legs — each repeating the identical guardrail / rollout
  / failure-mode / catalog boilerplate for the SAME lever on the SAME dominant step
  (`Verify the guards can actually fail (mutation registry)`), ~40 lines to say one thing.
  The anti-drop rule (a faster leg's lever must never silently vanish) stays; what's added is
  a **collapse for the identical-lever case**. Collapse IDENTITY = same routed pattern id
  (OPT75) AND same dominant-step BASE name (the `+ N more <category> step` aggregation suffix
  normalized away, so leg 1/4's suffixless label collapses with 2/4/3/4/4/4's suffixed one)
  AND same dominant category (part of the identity by design — the base step name almost
  always fixes the category, so including it can never create a false collapse, only refuse
  the pathological same-step-different-category case). When a collapsed sibling matches the
  pole's OWN structural block it now adds ONE compact line naming each leg with its OWN
  measured p50 · share (`` `guard shard 1/4` 158s · 71%, … ``); the boilerplate renders
  exactly once. A sibling carrying a genuinely different lever keeps its full block; a pole
  with no own structural block keeps every sibling's full block (unchanged). Presentation-only
  — detection, routing, and the stamped findings.json are untouched, so `verify_report`'s
  structural checks (which read findings.json, not the rendered blocks) still pass on the
  collapsed form. The per-leg number parse is anchored to the evidence grammar: the check
  duration matches the `s):` that always closes it (so a `(Ns)` token inside a check NAME —
  a timeout-matrix leg like `test (3s)` — can't shadow the real duration), and the
  aggregation-suffix strip is anchored to the exact ` + N more <category> step(s)` shape at
  end-of-string (so a real step name containing `+ N more <noun>`, e.g. `Deploy + 2 more
  regions`, keeps all its content instead of being clipped to its head).
- **2026-07-20** — **The per-PR chain/makespan spine is bound to the kept config era
  (#80).** #66/#68 scoped the spine RUNS to one config era and #69 scoped the enumerated
  CHECK SET, but the PER-PR layer — the sample feeding `chain_facts → chain_summary →
  makespan_p50_s` (the "a typical PR waits **N**" headline and the #24 physical-bound cap),
  the populations, and the presence denominators (`check_present_n_pr` / `present_on`) — was
  still the raw sample, filtered only by check NAME. Because a check NAME survives a config
  change (`test` is still `test` after it gets 3× faster), a dropped-era PR's latest-attempt
  interval for a kept-named check flowed straight into the makespan: on the live
  internal-dev-repo run two dropped-era PRs' 166s makespan crowned "a typical PR waits **2m
  46s**" directly under a disclosure claiming everything reflects the config BEFORE the
  change (whose `test` p50 was 538s — physically impossible). **The fix**
  (`_era_scope_pr_spine_sample`) scopes the per-PR spine sample to the kept side under a
  straddle, **surgically per straddling workflow**: a PR on workflow W's DROPPED side loses
  only its W-attributed checks (era-neutral checks from non-straddling siblings survive); a
  row left with no gate-bearing check drops whole. It runs AFTER the #74 thin-flip resolves
  the facts, so an emptied kept side has already flipped to `post_only_thin` and its post PRs
  are kept here — never an empty-spine render under a pre claim. `sampled_pr_count` now
  reflects the kept-side count and a new `era_dropped_pr_count` stamps the drop so the
  sampling caveat stays honest. The dropped-side PRs stay visible to the thin-flip's decision
  input (unchanged). New guard `check_era_chain_spine_bound_to_kept_era` re-derives the bind
  offline in three legs: (1) `chain_summary.n ≤ sampled_pr_count`; (2) no chain fact for a
  content-classified dropped head carries a kept-era chain member; (3) a pre-claiming makespan
  cannot sit below a kept gate that ran on EVERY kept sampled PR (the live 166s-vs-538s
  signature) — restricted to unanimous presence so it can never false-positive on a
  legitimately fast minority PR; a non-unanimous kept gate is left to legs 1–2 and the
  engine-side door.

- **2026-07-20** — **Config-era classification is content-keyed, not timestamp-based
  (#77).** A `pull_request` run executes the workflow file from the PR's OWN head, so the
  two PRs that CARRY a CI fix run the NEW config from their own heads — often minutes
  BEFORE the fix merges. The live internal-dev-repo run (2026-07-20) classified both
  fix-PRs "pre" by `created_at`, which cascaded into a self-contradicting report: their
  new-config makespan (166s) rendered under a disclosure claiming everything measured the
  config BEFORE the change (old `test` p50 538s — physically incompatible), the #74
  thin-flip was SUPPRESSED (the fix-PRs looked like kept-side pre gate PRs, so
  `kept_has=True`), and the verify guard's repr-run leg compared the same timestamps and
  passed. **The fix** classifies each sampled run of a straddling workflow by the
  workflow-file BLOB its `head_sha` carries: `_workflow_change_boundary` now returns the
  boundary commit SHAs, `_resolve_content_eras` fetches the POST blob (boundary commit),
  the PRE blob (predecessor), and each unique sampled head's blob, and
  `_partition_config_era` / `_era_pr_side` / the re-drill / the pole stamps consult that
  content era first, falling back to `created_at` when a head matches NEITHER blob. On the
  live shape the fix-PRs now classify post → the kept (pre) side is check-empty → the
  thin-flip FIRES → `post_only_thin` + a post re-drill, the honest report. The converse
  (a stale branch merged after the boundary running the old config) collapses the straddle
  correctly too. **API cost:** ≤2 boundary-blob calls + ≤1 per unique sampled head_sha,
  and ONLY for a workflow whose sample TIMESTAMP-straddles its boundary — a non-straddling
  repo fetches zero blobs (byte-identical). The era fact gains `content_era_by_sha` +
  `classification` bookkeeping; each straddling pole gains `repr_run_era` /
  `repr_run_era_basis` / `repr_run_head_sha`; and `verify_report`'s
  `check_era_disclosure_matches_enumeration` timing-provenance leg reads the basis (a
  content-post pole with a pre-boundary timestamp is no longer a false FAIL; the pole's era
  stamp is cross-checked against the fact-level content map).

- **2026-07-20** — **Installed skill copies derive provenance from the skills-CLI
  lockfile instead of recording a NULL sha (`run.py`, `blocking_path.py`,
  `tests/verify_report.py`; issue #2).** A fresh `npx skills add` install ships
  `skills/ci-speedup/` as a recursive copy with **no `.git`**, so `run.py`'s
  `git rev-parse` provenance derivation went fatal → NULL skill sha → the report's
  skill-commit footer was blank → `verify_report`'s
  `check_skill_commit_provenance` FAILED the completed report ("no `skill commit`
  recorded in the Data sources footer"). The live cost of that failure was the
  driving agent re-running the **entire ~98-call gh data pass** just to pass a
  **guessed** `--skill-commit-sha` (main's tip at the time). Now, when the skill
  root is not a git checkout, `run.py` reads the installer's `.skill-lock.json`
  (sibling of the installed skill dirs; a lockfile inside the root is also
  accepted) and stamps a **distinct `installed:<hash12>` provenance form** from the
  entry's `skillFolderHash`, or the honest terminal `installed:unversioned` when no
  lockfile/entry exists — **never a NULL, never a failed run, never a guessed
  remote sha** (an explicit `--skill-commit-sha` still wins). The footer renders
  the `installed:` form as a plain `skill build \`installed:<hash12>\`` identity
  string with **no fabricated commit/catalog URL** (a content hash is not a git
  ref); the git-sha footer path is byte-identical. `verify_report` **accepts** the
  `installed:` forms for live/installed runs (verified without `--skill-repo`) but
  **rejects** them for committed worked examples (verified **with** `--skill-repo`),
  which must keep a real, resolvable git sha. The lockfile probe degrades to
  `installed:unversioned` on any corrupt lockfile — including a non-UTF-8 file
  (`UnicodeDecodeError`), not just malformed JSON — so the provenance step can
  never raise out and fail the run.

- **2026-07-19** — **Era straddles resolve to a TOTAL, honest state space — the
  blended-while-claiming-purity fallback is unreachable (#74).** The live post-#72 run
  (internal-dev-repo, skill dd51d85, boundary 16h ago) rendered `test` @ 8m58s + `guard
  shard 3/4` as poles under a "the headline and every drill-down below reflect the
  configuration BEFORE it" disclosure — a report whose disclosure LIED about its own
  contents. Mechanism: the sole gate-bearing sampled PRs were BOTH post-change, so #72's
  `disclosed_pre` run-count cut would empty the enumeration; the never-empties fallback
  in `_era_scope_enumeration` then SKIPPED THE CUT WHOLE — leaving the blend intact AND
  clearing the very stamps `check_era_enumeration_bound` re-derives from, so the guard
  went blind. Everything measured was actually the NEW config (from 2 post-change runs);
  "measure the old config" was never available. **The fix makes the state space total AND
  flips the TIMING SPINE with the rule (direction (a)):** when a `disclosed_pre` straddle's
  kept (PRE) era carries no gate-bearing check in the sample, `_era_resolve_thin_flip`
  (decided pre-drill, from the PR gate sample) flips the outcome to a new `post_only_thin`
  rule AND **re-drills that workflow's whole spine from its POST runs** — so `crit_by_wf`,
  `pr_check_p50`, the poles, the representative-run links, and the makespans all derive from
  the new configuration (on the live shape: `test` renders its real ~2m36s, not the retired
  8m58s), never a pre-era number under a "measures the new configuration" claim. The
  disclosure becomes a prominent provisional caveat ("only N sampled runs have run on the new
  configuration … treat these numbers as provisional; re-run as history accumulates")
  REPLACING the pre-only one. Every straddle now resolves to exactly one of
  `{post_only, post_only_thin, disclosed_pre}`, each with stamps that SURVIVE and a disclosure
  that matches what enumerates — the blended state is structurally unreachable, not caught
  after the fact. The re-drilled post sample is below `_RARE_PRESENCE_MIN_PR`, so the
  presence-dependent machinery (minority demotion, populations, the presence-causal headline
  forms) stays INERT on the POST timings and the thin disclosure carries the reduced
  confidence — no sub-floor pretend-confident output. When the re-drill yields no usable
  post-era timing (a fetch wipeout OR runs that fetched but carry no developer-event
  timing — the `long_pole_p50 <= 0` signal the triage-recovery pass guards on), it is NAMED
  a coverage gap through the standard machinery (`jobs_fetch_failures` /
  `_note_job_fetch_wipeout` → `partial_kind`, which also bars the queue-inflated fallback):
  the pre-era spine is discarded regardless, so the thin disclosure never renders over an
  unmeasured spine, and the flipped workflow's timing-less checks never fall through to the
  (non-era-scoped) queue-inflated fallback under the "measures the new configuration" claim.
  **Guard hardening:** (i) the residual
  empty-spine path keeps the stamps (never clears them), so a detected straddle whose
  enumeration still can't be scoped FAILs loudly rather than skipping blind; (ii) a new
  direct-contradiction guard (`check_era_disclosure_matches_enumeration`, AUTO_SEED, +1 → 56)
  FAILs a rendered pre-only disclosure that co-exists with an all-post / hollow
  (empty-`kept_checks`) measurement, AND — via a timing-provenance leg (`_stamp_pole_repr_run_era`
  stamps each pole's earliest drilled-run timestamp) — FAILs a POST-claiming disclosure whose
  pole was drilled from a pre-boundary run. Era classification stays **timestamp-based
  best-effort** (a new-config run pre-dating the merge commit classifies as pre — precision
  limitation tracked in #77, noted in `ARCHITECTURE.md §2.1a`). Bill-scope caveat unchanged:
  the runner-minute / cost-spine figures keep the full sample by design and say so on every
  straddle (`collect_runs.py`, `blocking_path.py`, `verify_report.py`, `ARCHITECTURE.md §2.1a`).

- **2026-07-19** — **The config-era partition binds CHECK ENUMERATION to the kept era,
  not just the drilled runs (#69).** #66/#68 scoped the spine TIMING to one era, but the
  enumerated check SET was still drawn from the raw PR-gate check sample — so a live
  post-#68 `disclosed_pre` run (internal-dev-repo) rendered `test` @ 8m58s (the pre-#195
  full-guard config) in the Level-1 chart BESIDE four `guard shard N/4` bars — jobs that
  exist ONLY post-#195, enumerated from 2 post-change PRs — under a disclosure claiming
  every drill-down reflects the config BEFORE the change. Pole 2 (`guard shard 3/4`, post
  era) drilled under that pre-era disclosure, and the close reproduced #66's fabricated
  cross-era redundancy ("the full-suite guard overlaps the sharded version") through this
  new path. No configuration ever ran both. `collect_runs._era_scope_enumeration` now binds
  the enumeration to the kept era: for each straddle it splits the sampled PRs at the
  boundary (`_era_pr_side`, mirroring the run-partition's kept-side selection incl. the
  multi-boundary `[prev, last)` window), attributes each spine check to its workflow, and
  drops any check bound to that workflow observed ONLY on the dropped side — so a post-change
  PR's check-runs can no longer seed pre-era enumeration. In `disclosed_pre` the post-era-only
  checks leave the spine (pole candidacy, presence, populations, and the Level-1 chart all
  re-derive from `pr_check_p50`, so one drop binds every surface); in `post_only` the converse
  (pre-era-only checks leave). The dropped checks are stamped as `other_era_checks` (with
  `kept_checks`) and NAMED in the era note ("the new configuration adds checks not measured
  here: `guard shard 1/4`…`guard shard 4/4` — too few post-change runs to measure them yet"; the converse
  "the previous configuration ran checks not measured here" for `post_only`). Never empties
  the spine (an all-dropped cut is skipped whole); a pure no-op when nothing straddles (L2
  byte-identity). New guard `verify_report.check_era_enumeration_bound` re-derives the bind
  from the stamped `other_era_checks` and FAILs a report whose enumerated pole/check/population
  set contains a member absent from the kept era; a loud narrow SKIP on artifacts predating the
  #69 stamps. One report describes ONE configuration and names what the other adds/removes.
- **2026-07-19** — **Provenance block pluralizes count-nouns (#70).** The "Where
  this data comes from" block and the data-sources provenance table rendered
  `across 1 workflows` (and would have printed `1 runs` / `1 jobs`) on a
  single-workflow/degenerate sample — a hardcoded plural. A new `_count_noun`
  helper in `blocking_path.py` pluralizes the runs / jobs / workflows census
  counts correctly (`1 workflow` / `N workflows`) and passes a non-int (partial
  doc) count through verbatim; pinned by a unit test.
- **2026-07-18** — **A sample straddling a workflow-config change never blends eras
  (disclosed, partitioned, guarded) (#66).** On the universal second-run journey
  (audit → the user's own fix lands → re-audit), the collector sampled PRs from BOTH
  sides of a mid-window `ci.yml` change and blended them: it drilled a PRE-change run
  for one job and a POST-change run for another, synthesized a FABRICATED cross-era
  redundancy ("guard verification runs twice, once whole and once sharded" — no PR ever
  ran both), and set a `~6m28s recoverable` ceiling beside a `2m46s` typical wait (a
  ceiling >2× the wait it recovers). Two fixes, one PR. **(1) Config-era boundary
  (collector).** Per workflow file with ≥2 sampled runs, `_workflow_change_boundary`
  reads the two most-recent commits that touched it (ONE `commits?path=<wf>&per_page=2`
  REST call returning `(last, prev)`, pinned `&until=<created_before>` so a post-window
  edit is never the boundary; the runs API exposes no workflow-content hash, so per-run
  content diffing would cost one `/contents/` fetch per run, N≫K — the commit lookup is
  O(1) per workflow and strictly cheaper, and `per_page=2` is still a single call so the
  budget is unchanged). `_partition_config_era` then splits that workflow's spine + drill
  runs at the boundary: no straddle → byte-identical no-op (a workflow that did not change
  keeps its FULL sample); straddle with a sufficient post-change sample
  (≥ `_RARE_PRESENCE_MIN_PR`) → keep ONLY the post-change runs (measures the CURRENT
  config on a narrowed window, disclosed); straddle with too-few post-change runs → keep
  the pre-change runs and render a PROMINENT era disclosure near the headline (`ci.yml`
  changed N ago — this audit measures the PREVIOUS configuration; re-run once history
  accumulates). In BOTH straddle branches the change's OWN before/after never blend (one
  side is dropped whole), so the fabricated "runs twice, once whole and once sharded"
  retired-vs-current synthesis is structurally impossible. **Multi-boundary:** if the
  workflow changed TWICE in the window, the pre-change runs themselves span two older eras;
  the disclosed-pre fallback narrows the kept set to the single `[prev, last)` era (using
  `prev` from the same one call), so the pre-side is a single era too — recorded by
  `multi_change`/`kept_count` and noted in the disclosure. (In the rare ≥3-change corner
  where `[prev, last)` holds no sampled run — only the two most-recent boundaries are
  fetched — it falls back to a disclosed wider pre set, best-effort and never silent.) The runner-minute /
  relative-recovery consumers keep the FULL sample (`sampled_runs_by_wf` is unfiltered —
  the partition is scoped to the PR spine + drill only, L2); a straddle co-renders a
  caveat that those cost figures still include the earlier configuration and a
  duration/structure-changing edit blends both layouts there. Per-workflow era facts
  (boundary, prev boundary, kept era, rule, multi-change flag, pre/post/kept counts) are
  stamped in `pr_critical_path.config_eras`; `verify_report.check_config_era_boundary`
  re-derives that no drilled pole (nor the spine) bound to a `kept_era == "pre"` workflow
  ships without the disclosure — FAILs otherwise, and a `post_only` straddle missing its
  narrowed-window note FAILs symmetrically; LOUD-narrow SKIP on a pre-#66 artifact. The
  disclosure surfaces on every render arm, including the degenerate no-pole
  (all-fileless-gate) report, so a straddle is never dropped just because no pole was
  crownable.
  **(2) Recoverable-within-wait coherence (bounds-family sibling, #24/#25/#30 lineage).**
  A rendered recoverable "up to ~X" ceiling — the headline "biggest single measured win"
  or a per-pole "what a change here can buy" note — that EXCEEDS the headline typical
  merge wait now co-renders the slow-mode/worst-case reconciliation (the excess is the
  pole's conditional figure on the PRs where it IS the pole, exceeding the typical wait
  because it runs that long on only a minority of PRs — recovering it speeds those PRs,
  not the median). `verify_report.check_recoverable_within_wait` re-derives the typical
  wait from the same rendered headline `check_headline_wait_within_makespan` bounds and
  FAILs a ceiling above the wait whose context lacks the reconciliation marker. New
  `tests/test_config_era_boundary.py` covers both fixes (both partition branches, the
  no-change byte-identical pin, and FAIL/PASS/SKIP discriminators for both guards); the
  offline-pipeline golden gh-call count moves 35 → 38 (+3, one boundary lookup per
  workflow with ≥2 runs). Registered in `grader_seeds` (TRIAGE_ALLOWLIST + CHECK_CLASS,
  54 AUTO_SEED).
- **2026-07-18** — **Sharding / parallelization agent prompts carry the
  required-checks caveat (`_FIX_META` constraints).** In the same real run, splitting the
  serial gate step into a 4-way CI matrix would have silently ungated `main` — the new
  shard jobs are not required status checks until an admin adds them to branch protection,
  so the split-out work stops gating merges while everything stays green; the run only
  avoided this because the operator's local agent caught it unprompted. The rendered
  per-pole agent prompt is where a user actually reads the fix, so the caveat now rides
  the `constraints` block of every `_FIX_META` fix direction that splits work out of a
  (gating) check into new jobs: `cargo-test-shard`, `android-emulator-shard`,
  `gradle-test-parallelism`, and the split-across-jobs alternatives of `pytest-no-xdist`,
  `playwright-parallel`, and `benchmark-serial-reruns` (its parallelise-across-runners
  path). Each now warns that the new jobs must be added to branch
  protection as required checks (or the ruleset equivalent) or the split-out work silently
  stops gating merges — an admin-only step the fix isn't complete without. New
  `tests/test_required_checks_caveat.py` pins the caveat on both surfaces (the six
  `_FIX_META` constraints and the three catalog patterns) and red-proves its own predicate,
  so dropping the caveat from any split-into-new-jobs site fails loudly.

- **2026-07-18** — **Merge-queue (`merge_group`) runs no longer inflate the
  presence denominator (PR-identity dedup, #58).** A `merge_group` run executes on
  a GitHub-generated temporary branch (`gh-readonly-queue/<base>/pr-<N>-<sha>`),
  i.e. a DISTINCT head_sha per PR. The presence population (`pr_sha_ts` →
  `_select_repr_shas`) was keyed by raw head_sha, so a repo running its heavy suite
  in the merge queue counted each queue run as a SEPARATE "PR" in the denominator.
  The heavy suite then read as present on a minority of the sampled "PRs", the
  presence-weighted machinery (#26/#27/#57 — `_workflow_gates_minority`) demoted the
  REAL merge gate to a runner-minute-only "minority slow mode", and the report crowned
  a lighter check — a confidently-wrong headline on exactly the large-OSS merge-queue
  shape. The fix (`_group_dev_shas_by_pr`) collapses the population denominator to PR
  IDENTITY **before** sampling: a queue run is folded onto its PR's population row (the
  PR number recovered from the queue branch, matched to the pull_request run's
  `pull_requests[0].number`), so the queue's heavy suite and the PR event's checks share
  ONE row and the gate that runs only in the queue is present on that PR (correctly
  crowned). The queue-branch parser matches `<base>` greedily, so a slashed base branch
  (`release/1.x`, `feature/foo` — the merge queue keeps the base's slashes in the temp
  branch) still resolves to its PR number instead of falling to the orphan class. A
  queue run whose PR is not derivable (a branch off the naming scheme)
  collapses onto a SINGLE orphan class that cannot dilute PR presence — its timing is
  still measured, never silently dropped. The per-PR union treats a fetch failure on
  ANY member head-sha as a coverage gap (the whole PR row is dropped and counted as a
  fetch failure), never laundering a partial fetch into a complete-looking row that
  would silently drop the failed member's checks. A repo with no `merge_group` runs is
  untouched (the grouping is the identity map — one member per row, byte-for-byte the
  prior per-sha behaviour). The correction is at the data layer, so `populations`,
  `check_present_n_pr`, `_gate_counts`, and `chain_facts` all inherit it and every
  `verify_report` mirror (which re-derives from the stamped, already-deduped
  `populations`) needs no change. New `tests/test_mergequeue_presence_dedup.py`
  red-proves the demotion on a merge-queue-shaped synthetic repo (heavy suite on
  `merge_group`, light checks on `pull_request`) and pins the no-regression identity on
  the no-queue shape.

### Changed

- **2026-07-18** — **Pre-flip sanitization scrub — docs/tests only, no engine
  behavior change** (issue #60). Neutralized private-repo references (the internal
  development repo, a separate internal skill) to neutral phrasing across the six
  shipped test files' skip strings/comments (test semantics unchanged; re-grep to
  zero). Rewrote `maintainers/ci-speedup/MAINTAINERS.md`: the 22 private-repo PR links
  (404 for public readers) became plain-text `internal PR #NNN` references; the
  repo-lever hunt section plus the bill-pole convergence panel/gate sections and the
  rates-refresh automation step — all of which documented workflows, panel files, and
  helper scripts this repo does not ship — were removed (loop count reconciled 4→3;
  the NEVER block cleaned up); the L1–L9 checklist and the shipped-infra runbooks
  (gap→catalog, transcript, dogfood, manual rates refresh + freshness check) are
  preserved. README First-run figure reconciled to the
  committed `pallets/flask` example's own numbers (275 gh calls in ~34s, was ~36s),
  with the `microsoft/playwright` example (683 calls, ~1m 29s) cited for scale. Added
  a SKILL.md body-line-budget guard (`test_close_guidance.py`) pinning the body under
  500 lines (currently 498, #17) so the progressive-disclosure budget can't silently
  regress. New `tests/test_examples_provenance.py` brings the shipped `examples/`
  worked reports under a freshness gate for the first time — it pins each example's
  stamped skill-commit SHA to a real, internally-consistent, on-mainline (ancestor of
  HEAD) commit **without re-rendering** (no engine run, no `gh` calls).
- **2026-07-18** — **`examples/microsoft-playwright` regenerated by the current
  engine (now including the OPT73 presence-weighted-anchor fix, #56/#57); all
  figures re-derived from the current measured basis.** The shipped sample was
  produced before this week's sizing/headline fixes (#38/#39/#40/#47/#51/#54/#56/#57),
  so it carried pre-fix numbers — most visibly an OPT45 "Missing Concurrency Groups"
  figure of 7,482 min/mo that the measured sizing DOOR (`runner_min_basis`) now
  declines to credit (modeled, no cost-spine match), and a Bottom-line crown that
  anchored on the `tests_secondary.yml` cluster — a workflow that gates only 2/20
  sampled PRs — at ~3m 15s. Re-run end-to-end at skill commit `bbbc328` against
  `microsoft/playwright@449349c` (20/20 PR sample, `verify_report` PASS): with the
  #57 fix the crown is now the presence-eligible `tests_primary.yml` pole
  (`ubuntu-22.04 (webkit - Node.js 20)`, the check that gates 13/20 PRs), biggest
  measured win **~2m 37s**, and the runner-minute total is the measured,
  de-overlapped 5,401 min/mo. Report + both example READMEs refreshed.
- **2026-07-17** — **One measured sizing DOOR — every runner-minute saving derives
  from, or is clamped to, the measured cost-spine rows for its affected jobs**
  (issues #43/#44/#45; contract change). Historically each finding pattern carried
  its OWN sizing path, so models kept pricing from modeled or single-sample bases
  where measured data exists — three instance-fixes in a week (OPT45 #33, OPT73 #43,
  `chain_win_s` #45). This generalizes PR #38's OPT45-only reground into a single
  post-spine pass (`collect_runs._reground_runner_minute_savings`) over EVERY finding
  that credits a `runner_min_saving`: OPT45 **derives** (hit_rate × measured
  billable), OPT73 **clamps** (min(modeled, measured)), and every other rm-crediting
  pattern is on an EXPLICIT, reasoned `not_spine_derivable` whitelist
  (`_RM_DOOR_OVERRIDES` + `_rm_door_policy`, a total function). Every sized finding
  stamps `runner_min_basis`; a pattern with no declared policy stamps a loud
  `UNCLASSIFIED_door_policy` sentinel so `verify_report` FAILs — a new pattern
  **cannot ship its own unmeasured sizing path**. New invariant
  `check_saving_carries_measured_basis` makes the door PASS/FAIL: a saving with no
  basis stamp under a render-ready spine FAILs. The engine and that verifier share
  ONE render-ready predicate (`_spine_binds`), so they never disagree: under a
  render-ready spine a CLAMP/DERIVE finding that can't join (even when the whole
  spine yields no joinable rows) UNSIZES at the source rather than keeping an
  unbounded figure; only a genuinely absent spine keeps the figure (both gates then
  skip). A DERIVE pattern that reaches the shared clamp loop unstamped (its
  derivation pre-pass never ran) is flagged `UNCLASSIFIED_door_policy`, never
  silently clamped. Per-pattern semantics still differ; only the measured basis and
  the join live in one place. Surfaces:
  `scripts/collect_runs.py`, `tests/verify_report.py`, ARCHITECTURE §5.1.
- **2026-07-17** — **SKILL.md progressive-disclosure sweep** (issue #17,
  skills-best-practices audit). Docs-only restructure — **no behavior change**: the
  flow, phases, options, gates, and the report-opt-in contract (#18/#31) are frozen.
  SKILL.md body dropped from 610 → 498 lines (back under the 500-line budget) by
  moving depth into three new reference docs and pointing at them from a slimmed
  entry file: `references/spine-scoping.md` (required-scoping, PR-floor fallback,
  pole provenance, one-path demotion — the five spine blockquotes),
  `references/structural-track.md` (the OPT70–75 risk model + git-history/intent
  interrogation), and `references/gap-fill.md` (the phase-4a/4b/4c coverage-gap
  fallback). The phase sequence, verify gate, and phase-6 close contract stay in
  SKILL.md verbatim (the close text is pinned by `test_close_guidance.py`);
  remaining edits are prose tightening only. Surfaces: SKILL.md, three new
  `references/*.md`.
- **2026-07-17** — The full markdown report is now **opt-in** (issue #18). The
  default close no longer writes the report into the working tree or announces a
  file path; it opens with the measured result (biggest lever + each gating long
  pole) and the fix-selection question, and **"Save the full report"** is one of
  the AskUserQuestion options (alongside the per-pole fixes, fix-all, Tier-2, and
  none). The report is **still rendered and verify-gated internally on every run** —
  the `verify_report.py` honesty gate is unconditional; opting in only copies the
  already-verified `.md` into `./ci-speedup-findings-report.md`. `run.py`'s printed
  render command now targets an internal/session path beside the scratch
  `findings.json` (`--report-out` default) instead of the current working directory;
  pass `--report-out` to override. This shrinks the default blast radius for
  prose-vs-data (Class A) defects while keeping the evidence trail one keystroke
  away. Surfaces: SKILL.md phases 4–6, `scripts/run.py`, README First-run,
  ARCHITECTURE §2.

### Fixed

- **2026-07-18** — **`_map_check_to_job` disambiguates by evidence or bails
  honestly — no more slowest-match guessing on monorepos** (issue #59, launch
  robustness). The check→job/file binder exact-matched a check-run name against
  jobs across ALL workflows and, on a same-name collision, kept the SLOWEST match —
  so a monorepo with copy-pasted or reusable same-named jobs (two package workflows
  each declaring a `Build`/`test` job) rendered a pole whose `workflow_file`, step
  decomposition, and fix recipe belonged to a DIFFERENT workflow: a confident,
  hard-to-detect mis-attribution (same name-collision family as #52/#54 at a
  different join site; the docstring also overclaimed the behaviour). Fix:
  `_map_check_to_job` now (1) lets the `require_developer_timing` filter disambiguate
  first — when only one candidate workflow is PR/merge-timed the ambiguity is already
  resolved and it binds that file cleanly — and (2) on genuine cross-workflow
  same-name ambiguity REFUSES to guess (returns None), because the check-runs
  endpoint carries no workflow path and a check-run's own span is queue-inflated (an
  80s job can read 1871s) so it can't pick the "closest" job p50. Same-name jobs
  WITHIN one workflow (matrix legs) still resolve to the slowest — only cross-workflow
  collisions bail, so every unambiguous repo is byte-identical. The bail routes to the
  honest unmapped path: `_is_pr_gate_check` decides PR-gating from the FULL set of
  matching workflows (`_workflows_matching_check`), never the single slowest pick, so
  no real gate is dropped; and the structural disclosure now names the real cause via
  the new `_check_producing_workflows` helper — "produced by a same-named job in more
  than one workflow … give the colliding jobs distinct names to attribute and drill
  it" — instead of mislabelling a file-backed check a "fileless / third-party app
  check" (L8). Docstring corrected. Red-proofed by a monorepo fixture (pre-fix the
  pole binds the slower wrong workflow; post-fix it bails + discloses honestly); the
  single-workflow slowest-leg and scope-prefixed-subset cases are pinned unchanged.
  Blast-radius guard: because the mapper's bail also removed the ambiguous check's
  job timing, an ambiguous check that is the REAL merge gate would otherwise fall
  into the fileless partition and be silently uncrowned + mislabelled PR-lifetime
  status-gating latency (worse than the original mis-attribution). The spine now
  grounds its crown MAGNITUDE on the slowest same-named job p50 (new
  `_check_grounded_job_p50` — the check-run's own span stays unused, so no queue
  inflation), stamping it `workflow_jobs` so `_partition_fileless_checks` keeps a
  file-backed ambiguous merge gate in the crowning basis; only the per-file
  drill/fix stays withheld and disclosed. Guarded by a partition regression test.
  Two more None-consuming callers were audited and repaired the same way (probe the
  ambiguity-aware full match set, not the single-pick mapper): `_required_reachable_checks`'s
  "never silently drop a file-backed check" safety net (else a non-required-but-reachable
  duplicated gate was dropped as "non-required" once a required set resolved — also fed the
  OPT71 de-trigger candidate set, risking a recommendation to remove a real gate), and
  `_workflow_gate_freq`'s per-workflow gate count (else a duplicated majority gate's frequency
  was credited to no workflow, flooring its wall-clock lever to bill-only). Both are
  byte-identical for unambiguous checks and pinned by new regression tests.
- **2026-07-18** — **`verify_report.check_headline_lever_is_presence_eligible`'s
  no-credited-lever branch is now a LOUD skip, not a silent clean PASS** (issue #60,
  item 4). When no cluster-floor lever is wall-clock-credited there is nothing to
  presence-check, yet the guard returned a plain `Check(..., True)` that read as a
  verified pass. It now returns `skipped=True` with a detail naming what couldn't be
  checked ("Coverage gap, not a clean pass."), matching its sibling burial guard's
  coverage-gap semantics (L8: a check that asserts nothing never reads clean). Its
  discriminator test flips to expect the loud skip and red-proofs the branch.
- **2026-07-18** — **OPT73 cluster anchor and on-path label are presence-weighted —
  a minority-present cluster can't crown the typical-PR headline** (issue #56;
  playwright PR #55 regen). The OPT73 cluster-floor detector chose its anchor (the
  leg whose p50 sizes the win and leads the Evidence) by ABSOLUTE slowest p50, while
  the pole ranking is presence-weighted — so on microsoft/playwright the anchor was
  `Test msedge-dev on macos-latest` (the actual pole on ~0/20 sampled PRs, in NO
  Level-1 pole), and its `tests_secondary.yml` cluster — a workflow that gates only
  2/20 PRs — crowned the Bottom line's "biggest single measured win (~3m 15s)" and
  self-labeled "sits ON the merge-gating critical path", above the honest typical-PR
  ceiling (pole 1's own ~2m 37s). Two coupled fixes, both re-using the SPINE's own
  presence predicate (never a parallel notion): (1) **anchor** —
  `collect_runs._detect_shared_substep` now orders cluster legs presence-eligible
  first (`_leg_presence_eligible`, the inverse of the spine's `is_rare` — the exact
  complement for any spine-ranked leg, unknown-to-the-map legs treated as eligible),
  so a minority-present leg can never lead the Evidence or be `affected_jobs[0]`; and
  (2) **crown/label** — a cluster whose WORKFLOW gates a minority of sampled PRs
  (`_workflow_gates_minority`, a workflow-level majority test on the same gate count
  `_toc_block` renders — summed in the check-name domain via `_workflow_gate_freq` /
  `_map_check_to_job`, not by a raw job-name lookup) has its wall-clock **demoted to
  bill-only** (tier-2, `realization=none`), exactly like the
  existing all-legs-rare demotion — the runner-minute saving survives, but the crown
  and the on-path label do not. A **majority** workflow with one minority leg keeps
  its wall-clock and simply re-anchors (option (a)); a **minority workflow** demotes
  (option (b)). New converse guard `verify_report.check_headline_lever_is_presence_
  eligible` re-derives the crowned lever's workflow gate frequency from
  `checks[].pole_n` and FAILs when a minority-workflow cluster crowns the headline —
  the check that would have caught the playwright sample (registered, AUTO_SEED).
  Surfaces: `scripts/collect_runs.py`, `tests/verify_report.py`, ARCHITECTURE §5.2 +
  §12.
- **2026-07-18** — **The sizing-door and its guard join spine rows by EXACT job
  identity — name-colliding / matrix-leg job bases no longer widen the runner-minute
  bound** (issue #52; mastodon round-5 dogfood). Both the door
  (`collect_runs._measured_billable_for_jobs`) and the guard
  (`verify_report.check_saving_within_measured_compute`) sum a finding's affected
  jobs' measured billable from the cost spine, matched by (workflow_file, matrix-base
  job). The spine index ALREADY sums a base's matrix legs into one figure, but both
  sides iterated the finding's RAW affected-job list and re-added that summed figure
  once per listed leg — so a finding naming the two legs of ONE job (mastodon
  build-push-pr.yml OPT73: `build-image / build-image (linux/amd64)` and
  `(linux/arm64)`, base `build-image / build-image`) double-counted the base
  (16,642.4 → 33,284.8), widening the bound enough that OPT73's **18,165.8 min/mo**
  credit read as "within measured compute" when the two legs measure only **16,642.4
  billable** (a ~9% overstatement). The fix reduces the affected jobs to their
  DISTINCT (workflow_file, base) identities before summing — the SAME dedupe
  principle on both the door and the guard (L3), each applied through its OWN base
  normalization (the door's `_whole_run_cancel_base_key` is at least as strict as the
  guard's `_base`/`_cmp_name`, doing no scope-stripping). So each job's compute is
  counted once and a name-similar-but-different job (`build-image-streaming /
  build-image`) never folds in. Bare-base job names still aggregate their expanded
  matrix legs (no under-match).
  On the real mastodon bundle the door now clamps OPT73 build-push-pr to **16,642.4
  min/mo** (`measured_spine_clamped`); exactly one finding changes and the guard
  PASSes on the re-grounded artifact. The subset-strictness invariant (PR #38/#47)
  holds: the door's base key is at least as strict as the guard's, so the door still
  matches a subset of the rows the guard bounds against. The OPT73 per-job step credit
  (`wall_clock.credit_shared_substep`, cheapest-member conservative-shared-floor step
  × every leg's volume — a modeled step×volume figure that outruns the measured
  billable by basis mismatch, not member choice) is
  left bounded by this tightened door clamp rather than re-derived per-leg — one
  authoritative measured chokepoint, no drifting second sizing path. Surfaces:
  `scripts/collect_runs.py`, `scripts/wall_clock.py`, `tests/verify_report.py`,
  ARCHITECTURE §5.1.
- **2026-07-17** — **The Bottom-line / headline lever consumes the stamped
  per-finding cluster ceilings — the render-layer single door** (issue #49;
  electron + mastodon round-4 dogfood). PR #47 fixed the SIZING (findings now carry
  correct cluster-aware ceilings — mastodon OPT73 `wall_clock_p50_s=627s`, electron
  `2635s`), but the REPORT still buried them: the Bottom line re-computed its OWN
  sibling-capped headroom, so mastodon headlined a **~36s** per-leg win over the
  stamped **627s** cluster lever (~17x undersell) and electron claimed **~5m37s**
  against the next sibling window while its own tier-1 OPT73 credits **~43m55s** on
  the same cluster (~8x). The renderer now SELECTS and SIZES the headline from the
  stamped `wall_clock_p50_s`: a credited cluster-floor lever (OPT73) drops every
  concurrent sibling leg in lockstep, a win the per-pole `_pole_addressable` /
  chain-headroom arithmetic can't reach — so when its stamped ceiling beats the win a
  branch would show, the Bottom line LEADS with it instead of leaving it in "Also
  noticed" (`blocking_path._headline_cluster_lever` /
  `_cluster_headline_bottom_line`). It re-derives the SAME selection from data
  `collect_runs` already sized; it never re-computes headroom. Repos with no credited
  cluster lever, and legacy artifacts predating the stamp, render **byte-identically**
  (the selection keys strictly on the persisted marker). The headline also phrases the
  cluster's concurrency HONESTLY: `collect_runs` persists `cluster_legs_concurrent`, so
  a `needs:`-chained SEQUENTIAL cluster reads "N sequential (`needs:`-chained) stages …
  the per-stage savings compound" — never the concurrent-matrix "in lockstep" framing
  (the deepgram f19 mislabel the appendix already forbids, now kept out of the headline
  too). Both the flag and the concurrency marker are pinned by an artifact-path test
  (`test_shared_substep_across_cluster_is_a_floor_lever` /
  `_labels_sequential_needs_chain_truthfully`), so dropping either stamp goes red.
  Live-verified on both bundles (headline flips 36s→10m27s and 5m37s→43m55s). A
  wall-clock-credited cluster lever whose monthly volume is unknown
  (`runner_min_saving` None) is now also appendix-owned (`_is_pole_structural`), so the
  headline's "Also noticed" pointer never anchors at a section that doesn't hold it.
  Surfaces: `scripts/collect_runs.py`, `scripts/blocking_path.py`,
  `tests/verify_report.py`, `tests/test_headline_cluster_ceiling.py`,
  `tests/test_structural_findings.py`, ARCHITECTURE §12.
- **2026-07-17** — **`cluster_floor_lever` is persisted to findings.json, and the
  cluster guard can no longer read clean on a real report** (issue #49, L8 — the
  week's THIRD "SKIP reads clean" instance). The engine set `cluster_floor_lever` only
  in-pass (for sizing) and NEVER stamped it on the saved finding, so
  `check_cluster_lever_ceiling_escapes_sibling` had no marker to key on and SKIPped on
  every captured report (PR #47's accepted "never fires on real reports" residual was
  this hole). `collect_runs` now stamps `cluster_floor_lever=True` on every OPT73
  cluster construction; legacy artifacts without it SKIP LOUDLY (never FAIL). Two guard
  changes make the fail-open structurally hard to repeat: (1) the escapes-sibling
  check's no-bounded-cap path is now a LOUD, NARROW skip that names the count of
  unchecked cluster levers; (2) a NEW invariant
  `check_headline_consumes_stamped_cluster_ceiling` FAILs when the rendered
  Bottom-line lever is strictly smaller than a stamped credited cluster ceiling
  re-derived from findings.json — the one invariant that would have caught BOTH live
  reports. Registered in `run_checks` + `grader_seeds.TRIAGE_ALLOWLIST` (AUTO_SEED,
  count 50 → 51). Surfaces: `scripts/collect_runs.py`, `tests/verify_report.py`,
  `maintainers/ci-speedup/scripts/grader_seeds.py`,
  `maintainers/ci-speedup/tests/test_grader_seeds.py`.
- **2026-07-17** — **OPT73's runner-minute saving never exceeds the measured compute
  it cuts** (issue #43; nrwl/nx round-3b, caught internally by
  `check_saving_within_measured_compute`). The cluster-floor lever priced its bill
  saving from a MODELED shared-step credit (per-job step time × job count × volume),
  so on nx it credited **1919.7 min/mo** while its 4 affected cluster jobs measure
  **1404.4 min/mo** in the cost spine. The sizing door now **clamps** OPT73 to the
  affected jobs' measured billable (stamped `measured_spine_clamped`, dollars
  re-priced) — a fix cannot save more minutes than the jobs consume — and unsizes
  honestly (basis `unmeasured_no_spine_match`, saving None) when no affected job
  joins the spine. Surfaces: `scripts/collect_runs.py`.
- **2026-07-17** — **A cluster-floor lever escapes its own matrix sibling's cap**
  (issue #44, HIGH; mastodon/mastodon round-3b). OPT73 f83 targets
  `Run bin/flatware rspec`, a step recurring across 3 concurrent test matrix legs —
  "optimizing it once lowers all of them at the same time." But `chain_win_s` capped
  its wall-clock at **~40.5s** (the gap to a runner-up chain whose pole is a SIBLING
  leg that descends in lockstep with the fix), ~15× under the true cluster effect,
  burying the repo's largest genuine merge-wait lever in "Also noticed." Root cause:
  the chain-aware flooring in `wall_clock.bound_measured_critical_path` treated a
  sibling leg as a cap. A new `cluster_floor_lever` flag on `WallClockContext` (set
  by the OPT73 detector) BYPASSES the `chain_win_s` cap and the chain collapse: the
  sibling legs are all "own," so the ceiling floors at the slowest NON-sibling check
  (mastodon: Elastic Search ~202s → ceiling ≈ **639s**), which the `own_check_names`
  scoping already computed. New invariant
  `check_cluster_lever_ceiling_escapes_sibling` FAILs any OPT73 whose measured cap
  names one of its own sibling legs. The corrected ceiling re-ranks the lever
  prominently (out of "Also noticed") through the existing lever cascade. Surfaces:
  `scripts/wall_clock.py`, `scripts/collect_runs.py`, `tests/verify_report.py`.
- **2026-07-17** — **`chain_win_s` clamps to the p50 co-occurrence floor; the ceiling
  guard's short-sample SKIP is loud and narrow** (issue #45, MEDIUM;
  electron/electron round-3b). A headline "up to ~6m56s" (416s) rested on
  `chain_facts.chain_win_s` derived from ONE sampled PR whose runner-up (4640s)
  measured BELOW the population p50 leg (4761s). `_chain_facts_for_pr` now floors the
  per-PR runner-up at the population p50 of the surviving competitor's own pole leg
  (`caps`), bringing the win down to ~4m55s (295s) and toward the tighter ~3m29s the
  report's own OPT25 co-occurrence finding on the same job implies (a separate
  downstream bound). A single below-norm PR can no longer inflate the win (the per-PR
  identity `chain_win_s == chain_s − runner_up_s` holds whenever the win is positive;
  caps-empty/legacy calls stay byte-stable). The floor draws only on the surviving
  path's OWN legs — zeroed chain members are excluded, so a fan-in diamond isn't
  self-floored by the p50 of the very node being fixed. And
  `check_pole_ceiling_within_cooccurrence`'s short-sample SKIP —
  which fell open ("no per-PR populations") on exactly the 1/20-PR shape that
  produces the overstatement — now NAMES the unbounded ceiling claims loudly instead
  of reading clean. Surfaces: `scripts/collect_runs.py`, `tests/verify_report.py`.
- **2026-07-17** — **A fileless/managed status check's PR-lifetime span never crowns
  the merge-wait headline** (issue #12; caught on electron/electron, where the headline
  read ~8 days crowned by `Backport Labels Added` / `faraday/cage` — label/app status
  checks with no sampled workflow job — while the file-backed poles traced <1% of it).
  Root cause: a fileless check's only timing is its `pr_check_runs` span, measured from
  the check's *creation*, so a label that sat open for 8 days reads as an 8-day "CI wait"
  though no CI compute ran; `_pole_caps` builds de-inflation caps only from sampled jobs,
  so a check with no sampled job is never capped and its raw span flowed into
  `critical_path_s` / `chain_summary.makespan_p50_s` and crowned the headline.
  **Product rule (contract change):** PR-lifetime latency of a fileless/managed status
  check (bot gates, label gates, external app checks — anything producing no sampled
  workflow job) is NEVER a valid basis for the CI merge-wait headline. Fix:
  `collect_runs._partition_fileless_checks` excludes the non-job-groundable set from the
  crowning basis *at the data layer* (so `critical_path_check` / `chain_summary` /
  `populations` all re-derive from the job-groundable population), and stamps the excluded
  set in `pr_critical_path.fileless_status_checks`; `blocking_path` DISCLOSES the slowest
  one near the headline as PR-lifetime status-gating latency (not CI compute) via a single
  shared `_fileless_disclosure_lines` helper used by every render exit — the measured
  report, the all-fileless degenerate case, AND the static-only body (an all-fileless repo
  that also has static hygiene findings renders static-only, so the disclosure rides along
  there too rather than being dropped behind the short-circuit). A triage-skipped but
  file-backed check (scanned-graph mapped) stays in the basis — it is real CI compute the
  crown-recovery pass can still recover. New
  `verify_report.check_headline_basis_excludes_fileless` re-derives the disjointness (no
  crowned slot — `critical_path_check` / `checks[]` / `poles[]` / modal chain — names a
  fileless check) and the disclosure↔stamp bind, and ENFORCES that bind even on a
  static-only report that carries a non-empty stamp (so a dropped disclosure fails rather
  than skips). Surfaces: `scripts/collect_runs.py`, `scripts/blocking_path.py`,
  `tests/verify_report.py`, `tests/test_fileless_span_headline.py`, ARCHITECTURE §12.
- **2026-07-17** — A whole-log detector leaf no longer hijacks a pole's MEASURED
  CAUSE from its dominant measured steps (issue #16, sveltejs/svelte + nrwl/nx,
  HIGH — the `eslint-leaf-category-blind` class). A `_parse_log` leaf fires on a
  tool marker ANYWHERE in the joined job log with no check that the tool's work is
  the pole's dominant step, so the `eslint-no-cache` (`scan`) leaf crowned a
  TEST-dominant pole — mislabelling nrwl/nx's one combined `Run Checks/Lint/Test/
  Build` step (an `nx affected` that lints + tests + builds, binned `test` by the
  OPT72 payload classifier) as "the lint step" and pinning the pole's full ~5m08s
  ceiling on a lint-cache fix. **Class rule:** a leaf may crown the cause / claim
  the ceiling only when its target step-category AGREES with the pole's measured
  `dominant_category` (the crown `_decompose_job_steps` computes over the SAME
  `_step_category` taxonomy) AND — for a leaf sharing a coarse category with a
  distinct sibling tool (eslint vs. type-check, both `scan`) — the dominant STEP is
  one the leaf actually addresses. Otherwise the leaf is DEMOTED to a labelled
  secondary observation (never a silent drop) and the pole falls back to its
  generic dominant-step hand-off. **Ceiling design:** demote rather than credit a
  fractional ceiling — inside a single combined step the lint sub-share is
  unmeasurable from step timing, so any partial ceiling would be invented; the
  honest ceiling is the generic dominant-step wall, which IS measured.
  `blocking_path.py` adds `_LEAF_STEP_CATEGORY` + `_offcategory_leaf` /
  `_demote_offcategory_leaf` at the leaf-selection point (mirroring the
  `_apply_cache_dist` reframe precedent) and emits a per-pole
  `<!-- ci-speedup:leaf-crown fix_key=… -->` machine marker. `verify_report.py`
  adds `check_detector_leaf_agrees_with_dominant_category` (registered in
  `run_checks`, classified AUTO_SEED / `fabricated-or-unsupported-finding`),
  re-deriving the crowned leaf's category from the marker's fix_key and FAILing any
  pole whose crowned leaf category ≠ its `dominant_category` (ground truth from
  `findings.json`). `grader_seeds.py` AUTO_SEED count 46 → 47. Surfaces:
  `scripts/blocking_path.py`, `tests/verify_report.py`, `maintainers/…/grader_seeds.py`,
  ARCHITECTURE §12.
- **2026-07-17** — **OPT45 (missing concurrency) runner-minute saving now derives
  from the affected jobs' MEASURED compute** (issue #33; caught by the
  `check_saving_within_measured_compute` guard from #25/#30 on the mastodon/nx
  round-3 sweep, where OPT45 credited 2025.3 min/mo while its affected jobs
  measurably consume only 892.8 min/mo). Root cause: OPT45 is a whole-run cancel,
  but its saving was modeled as `hit_rate × workflow-long-pole-p50 × full monthly
  volume` — pricing one (often larger, differently-gated) job at the whole
  workflow's volume, ignoring that the cancelled jobs run on only a fraction of
  those runs. Fix (a *derivation*, not a clamp): once the cost spine is final,
  `collect_runs._reground_whole_run_cancel_saving` re-derives the saving as
  `hit_rate × Σ(affected-job billable_equiv_min_per_month from the spine)` — the
  same measured rows the guard bounds against, joined the same way (matrix-stripped
  base + reusable-workflow fallback) and at least as strictly, so `hit_rate ≤ 1`
  keeps it within bound by construction. The provisional (no-spine) sizing-time
  figure now also grounds in the affected jobs' summed p50 rather than the long
  pole (`_SIZING["OPT45"]` gains `cost_basis: affected_jobs`). The credited
  finding stamps `runner_min_basis: measured_spine_billable` and a size-note that
  states the measured basis; dollars are re-priced in place. An OPT45 finding
  whose affected jobs ALL miss the spine join is UNSIZED at the source
  (`runner_min_saving: null`, `runner_min_basis: unmeasured_no_spine_match`)
  rather than left carrying its provisional figure — the guard's loud coverage
  SKIP only fires when EVERY runner-minute finding misses the spine, so an
  unmatched one in a mixed report would otherwise render unbounded (omit rather
  than fake). Surfaces: `scripts/collect_runs.py`,
  `tests/test_collect_runs_sizing.py`, ARCHITECTURE §5.
- **2026-07-17** — Physical-bounds invariant family (issue #25): three
  machine-checkable coherence guards added to `verify_report.py`, each re-deriving
  its comparison ground truth from `findings.json` (never rendered text) and
  registered in `run_checks` + classified AUTO_SEED in `grader_seeds.py`.
  (a) `check_aggregate_total_ge_largest_member` — a rendered chain/aggregate total
  is never below the largest member it claims to sum (a serial `needs:` chain can't
  finish faster than its longest stage). (b) `check_headline_wait_within_makespan` —
  the headline "X until all checks finish" figure is never above the measured
  makespan p50 (the median per-PR span-capped wall). (c)
  `check_saving_within_measured_compute` — a finding's credited `runner_min_saving`
  is never above its affected jobs' measured monthly billable compute in the
  runner-minute cost spine; when savings exist but none of their jobs resolve to a
  cost-spine row it SKIPs loud (bounded nothing) rather than passing green, and a
  partial job match is surfaced as such (never presented as a full bound).
  `grader_seeds.py` AUTO_SEED count 43 → 46.
- **2026-07-17** — Chain headline total no longer renders BELOW a member it sums
  (issue #22, tokio-rs/tokio). `chain_p50` is a median of per-PR summed member spans
  diluted by fast PRs, so a `compile → miri-test` chain "totalled" 17m18 while
  miri-test's own drill rendered 18m36 (and the measured makespan was 19m14). The
  renderer (`blocking_path.render` chain-active headline) now clamps the rendered
  chain wait into `[largest modal member p50, measured makespan p50]` — capping at
  the wall first, then flooring to the largest member (the member is a hard measured
  lower bound) — and the claim carries the coherent `chain_wait_p50_s`;
  `check_headline_chain_matches_stamp` re-derives the same clamp from the facts +
  `checks[]`. A chain whose sum is already coherent renders byte-identically.
- **2026-07-17** — Headline merge-wait no longer overstates the measured makespan
  (issue #24, nrwl/nx). A crowned gate's population floor (median of per-PR maxima,
  taken over re-run-inflated check-run clocks) crowned "typical PR waits 15m08" while
  the span-capped measured makespan was only 11m00. The renderer now caps the
  headline floor (`blocking_path.render`, `floor_p50`) at the measured makespan p50
  (`chain_summary.makespan_p50_s`), so the "until all checks finish" wall derives from
  the same span-capped basis; `check_headline_floor_presence_reconciled` mirrors the
  cap. It only lowers, and only when a makespan was measured — a well-behaved report
  (wall ≥ every per-PR max check) is unchanged.
- **2026-07-17** — playwright-parallel cause no longer asserts unrendered timeline
  shape (CLASS fix). Detector C fires on ≥2 `playwright test <spec>` invocations
  anywhere in the joined job log with no step-structure or sequencing check, yet its
  canned MEASURED CAUSE asserted the invocations are "visible as sequential steps in
  the timeline above" and "so they don't share a worker pool" — shape claims the log
  the detector reads cannot establish (nrwl/nx: the invocations live in one
  `Run Checks/Lint/Test/Build` step whose timeline renders a single bar, and they are
  separate `nx e2e` targets nx may run concurrently). The cause now states only what
  the log shows (≥2 separate `playwright test` invocations rather than one parallel
  run) and hands the scheduling / worker-pool question to the agent; the `constraints`
  and `deliver` blocks no longer presuppose serial execution. New invariant
  `check_measured_cause_matches_rendered_timeline` (verify_report.py) re-derives each
  pole's rendered step count and fails any MEASURED CAUSE that points at "the timeline
  above" as showing sequential steps when the pole's waterfall renders <2 steps — the
  R2 rule (generic prompts never point at a "step timeline above" the pole doesn't
  render) applied to the catalog cause path. Classified AUTO_SEED in
  `grader_seeds.py`. (dogfood loop)
- **2026-07-17** — Headline no longer blames a lowered typical merge floor on a
  check's presence when that check is present on a MAJORITY of sampled PRs (nx
  `main-linux` non-sequitur). The `gate_is_slowest` non-universal-disclosure
  branch hard-coded the presence-causal "ran on only N/npop sampled PRs, so a
  typical PR finishes in {merge_dur}" template regardless of presence — but
  presence at 19/20 (95%) cannot lower a median wait from 46m to 11m, because the
  median PR runs the check; the drop is a duration/population skew (the check's
  conditional p50, measured over a wider run-sample, overstates the typical
  wait). The engine now emits the presence-causal framing only for a genuinely
  MINORITY-present check (where a typical PR skips it) and a
  conditional-p50-overstatement framing otherwise. CLASS fix: new re-derivation
  invariant `verify_report.check_headline_presence_causal_only_when_minority`
  re-derives present/npop for the named check from the per-PR `populations`
  ground truth and FAILS any form-1 (name-first) presence-causal headline whose
  check is majority-present. (grader_seeds: AUTO_SEED / `fabricated-or-unsupported-finding`.)
  The sibling `verify_report.check_headline_floor_presence_reconciled` (which
  requires a lowered floor be disclosed) now accepts EITHER reconciliation — the
  minority presence caveat OR the majority conditional-p50-overstatement clause —
  so the two headline guards no longer contradict each other on the same
  majority-present, floor-lowered form-1 report.
- **2026-07-16** — Bimodal pole header no longer prints a "The P50 sits on the fast
  mode" caveat when the median actually sits in the SLOW cluster. `_pole_headline`
  fired the slow-mode override (and its fast-mode caveat) whenever
  `high_p50 > p50 * 1.15`, without checking where the P50 sat — so on a
  strict-slow-majority job (nrwl/nx: 59% of runs slow, p50 46m33s, low 13m41s,
  high 54m14s) it claimed the median was on the fast mode, contradicting the split
  rendered in the same sentence. The override now also requires the median to be on
  the fast cluster (`p50 <= (lo + hi) / 2`), mirroring the exact midpoint predicate
  `_bimodal_note` already uses; a slow-median job keeps its honest p50 header and no
  caveat. CLASS fix — invariant
  `test_pole_headline_no_fast_mode_caveat_when_median_sits_in_the_slow_cluster`
  re-derives the fast/slow-mode split from the findings pole fields
  (`p50_s` + `bimodal.{low,high}_p50_s`).
- **2026-07-16** — A pole's agent prompt (and the LLM-analysis provenance line) no
  longer cites "the cross-run check above" when that pole renders no `🔬 Cross-run
  check` section. A singleton magnitude sample (only the drilled run) suppresses the
  section (`_mag_line` returns `[]` on `<2` values), but the prompt template emitted
  the "validated across runs" claim unconditionally whenever a timeline-derived
  dominant step existed — a dangling reference to a section that isn't there (seen as
  Pole 2 `goreleaser-check` on caddyserver/caddy). The prompt builders + the
  LLM-analysis block now gate the citation on whether the section actually rendered
  (`cross_run_rendered`), falling back to "measured in the drilled run". CLASS fix:
  strengthened invariant `check_rca_hands_off_never_prescribes` in `verify_report.py`
  re-derives this per pole — a cross-run-check locator phrase must co-occur with the
  `🔬 Cross-run check` marker in the same rendered pole. (blocking_path.py,
  verify_report.py, ARCHITECTURE.md §12.4)
- **2026-07-16** — An all-placeholder matrix job `name:` (e.g. `${{ matrix.target }}`)
  no longer binds a managed/external check to a workflow file. Such a name compiles to a
  match-anything regex (`^.+?$`), which the no-timing scanned check→file binders
  (`_check_to_job_node_scanned`, `_check_to_workflow_file_static`) used to treat as a real
  match — grabbing an external Netlify/CLA/app check-run (which appears in no workflow YAML)
  and rendering it as a file-backed long pole with a wrong-file agent prompt (seen on
  `tokio-rs/tokio`: `Redirect rules` / `Header rules` / `Pages changed` bound to `ci.yml`).
  The binders now refuse a degenerate all-placeholder template (`_name_template_is_degenerate`),
  so a genuinely fileless check stays fileless; a degenerate-named job's own legs still resolve
  via the sampled-timing anchor a foreign check never reaches. CLASS fix: a new re-derivation
  invariant in `verify_report.py` (`_external_check_misbound_offenders`, folded into the
  "report drills every gating pole" check) re-derives production from `workflow_job_graph` and
  fails on any no-sampled-job pole bound to a workflow that produces no matching job.
- **2026-07-16** — A demoted pole's agent prompt and Contents row no longer assert
  the typical-gate framing its own header disowns. A long pole the spine
  DEMOTED (rarely the actual slowest check — e.g. caddy `goreleaser-check`:
  present on 13/20 PRs but the per-PR slowest on 0/20) rendered a "Rarely the
  merge gate …" header, yet its agent prompt still opened "Slowest check a
  typical PR waits on: … its workflow `ci.yml` gates 20/20 sampled PRs" and its
  Contents row still carried "`ci.yml` gates 20/20 PRs" — the workflow's
  typical-gate frequency, driven by a *sibling* required check, borrowed next to a
  pole that gates almost nothing. Both sites now follow the header: the prompt
  states the "Rarely the merge pole" framing with the pole's own (low) gate
  frequency and the Contents row is tagged "rarely the merge pole" instead of the
  sibling's count. CLASS fix — new re-derivation invariant
  `check_demoted_pole_not_framed_typical_gate` in `verify_report.py` re-derives
  the demoted set from `pr_critical_path` (`checks[].pole_n`/presence, the same
  `_typical_check` split the engine ranks by) and fails any report whose demoted
  pole carries the typical-gate prompt phrase or a gate-count Contents tail.
- **2026-07-16** — Tier-2 `non_pr_event` (OPT36 schedule-burn) certificates no
  longer fail `verify_report.py`'s re-derivation on `[push, schedule]` workflows.
  The persisted event mirror (`events_by_wf`) was built only from the main-pass
  success slice, so a workflow whose recent successes were all `push` omitted
  `schedule` and the certificate's `subset ⊆ events_by_wf[wf]` check failed
  (`f-…: non_pr_event lacks stamped event-subset evidence`, seen on
  tauri-apps/tauri, caddyserver/caddy, mastodon/mastodon). The new
  `_fold_observed_events` helper unions the events the dedicated schedule probe
  actually observed back into the mirror. CLASS fix, guarded by the existing
  `check_tier2_neutrality_derived` invariant plus regression
  `test_opt36_schedule_probe_folds_event_into_persisted_mirror`.
- **2026-07-16** — Cost-spine billing floor is now soundly re-derivable (CLASS fix). The
  `runner-minute cost spine source block is re-derivable` invariant
  (`check_runner_minute_spine_contract`) false-failed with
  `positive-duration job has <1 billable minute` on any job bucket that mixed a
  short real run with zero-span occurrences (`started_at == completed_at`, which
  GitHub bills 0 minutes) — such a bucket has a positive MEAN compute-second but
  a sub-1.0 MEAN billable-minute, which the old aggregate test could not
  distinguish from a broken (un-rounded) engine (seen on `electron/electron`).
  The cost-spine source producer (`collect_runs.py`) now stamps
  `sampled_positive_duration_occurrence_count` per row, and the verifier
  re-derives the per-occurrence 1-minute floor from it
  (`round(mean_billed × occurrences) ≥ positive-duration occurrences`) instead of
  asserting `mean_s > 0 → mean_billed ≥ 1`. Regression:
  `test_runner_minute_spine_mixed_zero_span_bucket_re_derives_billing_floor`.
  ARCHITECTURE.md §12 (cost-spine row schema) updated for the new stamped field.
- **2026-07-16** — A spine-rare-demoted matrix leg is no longer framed on the merge-gating
  critical path (dogfood: `tauri-apps/tauri`; CLASS fix). The
  `check_dropped_check_not_framed_on_path` invariant caught `test (macos-latest)`
  — a leg the footnote demotes as opt-in/rare — framed "sits ON the merge-gating
  critical path" via two renderer gaps. (1) `_stamp_spine_rare`'s join is now by
  check NAME (exact, or unexpanded matrix base↔leg) instead of `(workflow, job)`
  identity, so it no longer folds a distinct TYPICAL sibling leg into a single
  rare leg's match set (which made the kept-guard wrongly decline the stamp) and
  no longer skips a cross-workflow same-name collision — mirroring the
  name-level footnote the reader sees. (2) An on-path cluster (OPT73) finding's
  `**Where:**` now LEADS with its on-path (non-`spine_rare`) leg rather than
  `affected_jobs[0]`, and `_detect_shared_substep` orders `affected_jobs`
  slowest-first (the evidence's "slowest cluster job"). Regression tests in
  `tests/test_blocking_path.py` (`test_spine_rare_stamps_exact_rare_leg_despite_typical_sibling_leg`,
  `test_on_path_cluster_where_leads_with_on_path_leg_not_rare_sibling`,
  `test_spine_rare_demotes_a_cross_workflow_name_collision_with_a_rare_check`,
  `test_rendered_tauri_shape_passes_the_spine_dropped_verify_gate_end_to_end`),
  the last tying the rendered bytes to the invariant end-to-end; `ARCHITECTURE.md`
  §12 updated to document the name-level keying and the Where-line lead.
- 2026-07-16 (#6): **OPT66 double-framing.** An OPT66 SKU-arbitrage ceiling
  whose only job was the drilled headline pole rendered in "Also noticed" as a
  valueless `~0 wall-clock` cleanup, contradicting the headline that crowned the
  same job the biggest lever (vite/qdrant on windows/mac). `_on_pole_job`
  exempted every `sku_arbitrage_ceiling`; since a ceiling carries no credited
  saving (`runner_min_saving=None`), it now falls through to the valueless +
  all-pole exclusion and is dropped from the appendix, matching
  `check_pole_not_reframed_as_hygiene`. A ceiling that also touches a non-pole
  job still renders.
- 2026-07-16 (#6): **Tier-2 section-lead count drift.** The section lead's
  "not promoted: N …" tail counts the de-overlapped findings its rows render
  from, but `check_tier2_total_deoverlapped` re-derived the count from the raw
  findings list, double-counting an exact-duplicate occurrence the renderer's
  `_dedupe_findings` collapses (qdrant 17≠18, grafana 11≠12) — a false FAIL
  against a correct report. The re-derivation now de-overlaps first via a
  standalone `_vr_dedupe_findings` mirror, counting the same deduped collection.
  The fix covers both halves of the drift — the not-promoted tail AND the
  promoted count/credited-minute/dollar totals — regression-tested on each side
  (`test_tier2_total_deoverlapped_counts_deduped_collection`,
  `test_tier2_total_deoverlapped_dedupes_promoted_side`). A coupling test
  (`test_vr_dedupe_findings_stays_coupled_to_the_engine`) pins the mirror
  behavior-equal to the engine's `_dedupe_findings` over every dedupe-key branch
  (including the sole non-empty default, `source`), so a future retune of the
  engine's collapse rule can't silently re-introduce the drift.
- **2026-07-16** — Form-2 headline now reconciles a non-universal slowest check with the
  population floor. The floor-lowered form-2 headline (slowest
  typical check ≠ the frequency gate) labeled a path-filtered check "the slowest
  check a typical PR waits on (~X)" beside a strictly-lower "Y until all checks
  finish" floor with no presence caveat — contradicting its own floor and the
  opt-in footnote its identical matrix siblings carry (tauri-apps/tauri
  `test (windows-latest)`, 5/20). It now discloses "ran on only N/M sampled PRs,
  so a typical PR finishes in Y", exactly as the form-1 (gate-is-slowest) branch
  always did. CLASS fix: new `verify_report.py` invariant
  `check_headline_floor_presence_reconciled` re-derives the floor-lowering from
  `pr_critical_path.populations` (median of per-PR maxima vs the named check's
  p50, mirroring the engine's `floor_p50 = _pop_floor` guard) and requires the
  reconciliation clause on a lowered-floor report that headlines a slowest check.
  It SKIPs a chain-form ("the gate is the X → Y chain") or generic headline, which
  names no slowest check and states no floor to reconcile even when the populations
  carry a lowered-floor shape, so the guard can't false-FAIL those. Classified
  `AUTO_SEED` / `mis-ranked-lever` in the dogfood grader.

## 2026-07-16 — Initial public release
- **2026-07-16** — A combined `test`+`build` step (e.g. nrwl/nx's `Run Checks/Lint/Test/Build`,
  one `nx affected` step that lints + tests + builds) is no longer binned 100% as
  `build`. The fine-grained step classifier (`_step_category` in
  `collect_runs.py`) gains two high-precedence COMBINED entries above the `build`
  entry: a step binned as payload (`test`/`scan`) iff its name carries BOTH a build
  token AND a genuine payload-execution token, mirroring the existing
  `package`-before-`build` precedent. Previously the trailing `Build` token stole
  the whole step for the `build` category — a `_SETUP_BUILD_CATEGORIES` member — so
  a payload-bearing step's entire duration landed in the redundant-work numerator
  (setup+build ÷ payload), inflating the ratio past 2.0 and misrouting the pole
  onto OPT72 ("warm the build cache") when the step's time was actually test
  execution. The fix is deliberately additive and requires BOTH tokens: a *pure*
  build step still classifies as `build`, and the bare `test(s)` token is guarded
  against build-artifact compounds (`Build FIPS test image`, `Compile test
  fixtures` — a build OUTPUT, not test execution), so the genuine OPT70/OPT72 build
  levers are untouched. **CLASS fix:** new re-derivation invariant
  `check_structural_step_category_not_payload_binned_as_build` in `verify_report.py`
  fails any report whose `decomposition` crowns a `build` dominant step whose name
  clearly runs payload work (test/lint/spec/e2e/…), catching the whole class on
  every future report.

- **Initial public release of `ci-speedup`.** Audits a repository's GitHub
  Actions workflows against a catalog of CI optimization patterns and produces a
  measured, root-cause-analysis report ranked by developer wall-clock wait (the
  merge-gating critical path), with runner-minutes (the cloud bill) as a
  secondary axis. Detection, ranking, and every measured number are
  deterministic — computed from sampled `gh` run history, not estimated. The
  skill **diagnoses but does not prescribe**: every finding ships a ready-to-paste
  agent prompt that hands the measured root cause to your own coding agent, which
  reads the real logs and the file's intent before shaping a fix. Every report
  self-checks against a suite of invariants and stamps the skill commit and
  scripts-tree hash it was produced by.

Pre-release development history lives in StarSling's internal repository.
