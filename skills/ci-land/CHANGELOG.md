# Changelog

All notable changes to the `ci-land` skill. Unversioned; dated (UTC). Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **2026-09-28**: **A DECLINE-only round answers and resolves its threads,
  and a new run stops an armed wake-up.** With nothing to push, Phase 3 went
  straight to Phase 6, so declined bot threads stayed unresolved and LANDED
  (which now needs them resolved) was unreachable. And the session's own push
  both re-ran ci-land and changed the wake-up's signature, starting a second
  driver that removed the first one's worktree at the same path.

- 2026-09-28: `/ci-land` with no number refused to use the PR the session had just pushed to, reading the
  Never bullet on guessing against the Invocation rule; the bullet now names that PR as not a guess.
- 2026-09-28: a LANDED report left five declined bot threads to be discovered on GitHub, and the next run
  called them the owner's decision. Declined threads now head every report under Waiting on you, in both
  forms, and a DECLINE stays ci-land's call, pending the owner, across runs.
- 2026-09-28: a declined bot thread is now answered (one reply with the reason, via
  `addPullRequestReviewThreadReply`) and resolved after the push, so LANDED means no bot thread is left for
  the owner to close. Three runs on one PR had each left the same five declines open. Threads a human
  joined are still never replied to or resolved.

- **2026-09-28**: **Reading review bodies now cites the exact strings and
  regexes a production bot-comment parser uses, organized by bot, in place
  of a paraphrase.** Two corrections fall out of this: Cubic's consolidated
  block is gated on the substring `"Prompt for AI agents"` alone, not on the
  fuller "(all issues)" wording previously implied; and Cursor Bugbot's
  "found no new issues" clean-rescan phrasing has no matching regex in the
  reference parser and is now marked an unconfirmed variant rather than an
  established fact. Also added: the exact CodeRabbit file/line regexes and
  its `Additional comments`/`Review details` skip-by-nesting rule, Greptile's
  five-backtick prompt fence, the Codex connector's exact placeholder string
  and cleanup regexes, the general noise-stripping order applied to every
  bot's body, and a cross-check of the reference parser's author-substring
  list against the Reviewers table (no missing rows: Copilot and Gemini get
  no special-cased parsing there and stay UNVERIFIED). `gh-commands.md`
  gained the numeric values (60s Retry-After cap, 1 req/s GraphQL pacing, 2
  bounded 5xx retries at 2s/4s, the `x-ratelimit-remaining: 0` primary-vs-
  secondary split, the HTTP 200 `RATE_LIMITED` body case) behind the
  consecutive-error budget's existing "wait once, at most 60 seconds" rule.

- **2026-09-28**: **Closed the early-push and lost-fix paths in fixing as
  findings arrive.** A bot reviewer's own pending check is never "waiting on
  a human", and a CheckRun `WAITING` for an environment's reviewers is; Phase
  3's routing to the stale-base merge or Phase 6 fires only once the gate is
  open; with fixes pending, a stale-base merge waits for the next round; at
  the gate, a local fix whose thread is now resolved or outdated is reverted,
  and a rebase that brings in new commits re-runs the local verification; a
  rejected stale-base push merges again, never rebases; each red-then-green
  swap runs before the next edit to that file; a fix with nothing to run is
  listed under Not verified; stock macOS's missing `timeout` (exit `127`) is
  not a failed check; the exclude line creates `info/` and survives a file
  with no trailing newline. The report adds STOPPED to its header, an
  Unpushed fixes line, and the oversized fix as a NEEDS_HUMAN blocker. Cursor
  reviewers are split by check name, since live security-review posts carry
  both Cursor tokens; the Bugbot clean-pass and Codex done signals are
  corrected.

- **2026-09-28**: **The main thread re-runs ci-land after its own push.** When the session that
  invoked ci-land pushes another commit to a PR a run has already landed, it runs ci-land on that
  PR again at once, without asking. ci-land is bounded and does not watch for later pushes, so
  the session that pushed is the only thing that knows a new round is needed.
- **2026-09-28**: **Folded in two surveys of other PR-landing tools, plus
  two owner decisions from dogfood.** In SKILL.md: a check still pending after
  every other check has completed, whose name or output says it awaits an
  approval or review (a "Code Review Gate"), is reported as waiting on a
  human and does not gate the push or LANDED (the round query now fetches
  `CheckRun.title`/`summary` and `StatusContext.description`); `gh pr checks`
  exit `8` is pending, like `124`; an out-of-diff failure goes to the
  stale-base merge instead of FLAKE when the base moved and the previous head
  was not green on that check; three consecutive `gh` errors (one wait of at most
  60 s on `RATE_LIMITED`) end the run STOPPED naming the last error; `cursor`
  is one reviewer per check name (Bugbot, its security review, its Approval
  Agent); a finding needing more
  than 500 changed lines is NEEDS_HUMAN, not a FIX; a new regression test
  only for a security or data loss finding, every other fix verified by the
  existing check or touched test file; and the worktree moves from under
  `.git` to `<repo>/.ci-land/` (tooling such as Vite refuses paths under
  `.git`), kept out of the user's status by a local `info/exclude` line. In
  the references: CodeRabbit body sections that are findings without a
  thread, consolidated agent-prompt blocks that must not be counted twice,
  "nothing to do" summaries as done signals, bot noise to skip, Bugbot pass
  keys as a dedupe aid, three DECLINE signals, per-bot re-review behaviour
  (Gemini and the Codex connector do not review a push unless mentioned, so the report
  says they will not re-review), and a paging stop when `hasNextPage` has no
  `endCursor`. Token strings from Cursor's MIT-licensed poteto-mode are
  credited in NOTICE.
- **2026-09-28**: **The gate governs the push, not the fixing.** Phase 2 is
  now the push gate. While it is closed, every status change that brings a
  new review, thread or comment, or a failed check, triggers the full round
  query, triage of what is new under the same rules, and the FIX edits in the
  worktree at once; nothing is committed or pushed until the gate opens, and
  FLAKE reruns still wait for it. When it opens, the driver re-derives the
  worklist from a fresh round query (fixing late arrivals, re-checking any
  local fix whose thread gained a newer comment), then makes one commit and
  one push. One CI run and one re-review per round, as before; the wait on the
  slowest bot is spent fixing what the faster ones said. The report's round
  line adds "fixed while waiting: N of M".
- **2026-09-28**: **No-argument invocation defaults to the PR the session is
  working on.** The prior rule (never take the PR from the conversation; stop
  and ask with no argument and no current-branch PR) overcorrected. The
  driver still only ever receives an explicit PR number and URL and reads no
  session context of its own, but the main thread (the user's session) now
  resolves a missing argument itself: the current branch's open
  PR first, else the open PR this session most recently opened, pushed to, or
  reviewed, naming whichever it picks in the hand-off. Only with neither does
  it ask; a `gh` failure while resolving is reported as the gh gate's failure,
  never as "no PR". A driver given no explicit number stops.
- **2026-09-28**: **Threads and comments are trusted by author, not by
  account.** The round query now fetches `authorAssociation` on every comment
  and thread comment; Phase 3 only worklists a bot reviewer or a human in
  `OWNER`, `MEMBER`, `COLLABORATOR`. Any other human's comment on a public PR
  is a DECLINE, listed as "not acted on (untrusted author)" and never
  resolved, so it cannot hold LANDED hostage: any account can comment on a
  public PR. Reviews carry `authorAssociation` too.
- **2026-09-28**: **Refuses to push to the wrong remote.** Phase 0 now pins
  `headRepository`/`headRepositoryOwner` alongside the PR it confirms, and
  stops (STOPPED) if the local clone's `origin` owner/name does not match
  the PR's own repository (compared case-insensitively, without `.git`),
  instead of pushing there anyway.
- **2026-09-28**: **A bot's own `CHANGES_REQUESTED` blocks LANDED like a
  trusted human's.** Its threads are still fixed in Phase 4; if its latest
  review (on any commit) is still `CHANGES_REQUESTED`, the run ends
  NEEDS_HUMAN naming the reviewer and quoting its summary. An untrusted
  account's `CHANGES_REQUESTED` does not.
- **2026-09-28**: **The PENDING rule states only what the pinned query
  returns.** It returns submitted reviews only, so nothing pending is ever
  visible to act on; dropped the unimplementable instruction to ignore
  inline comments on pending reviews, which the query never fetches.
- **2026-09-28**: **Red-then-green proof isolates the one fix, not the whole
  file.** The test is written first; the file is then snapshotted just
  before its fix and again just after. The proof swaps the before-snapshot
  in to get the fail and the after-snapshot back to get the pass, so it
  isolates correctly when two fixes touch the same file or the new test lives
  in the file just changed, and never restores from git, which would drop
  the uncommitted fix.
- **2026-09-28**: **Two more trigger phrasings.** "Fix what the bots got
  right and push once" and "finish the review loop even when the session is
  nearly out of context" are now named in the description and covered by
  positive evals.
- **2026-09-28**: **Gate, waits and report tuned from the first dogfood
  rounds.** A bot whose own check run has completed on the head is done at
  once, and a bot that reacted to neither the head nor the previous head
  (the round query now fetches `commits(last: 2)`) no longer holds a 4-minute
  window. Waits between slices run a small status query, and the full round
  query only when the gate could open or a check fails. A run that pushes
  nothing reports in at most 6 lines. The PR number is never taken from the
  conversation. Copy-wording findings are declined as the author's call, and
  a base conflict's next step names the conflicting files.

### Added

- **2026-09-28**: **After a LANDED report, a scheduled wake-up re-runs ci-land
  when the PR changes (Phase 7).** The run itself stays bounded; the main
  thread arms the host's scheduler (on Claude Code, `/loop`) every 10 minutes
  for at most 4 hours. Each wake-up is one status query compared with the
  signature recorded at LANDED: unchanged, it does and says nothing; a new
  head, review, thread or check state starts a fresh bounded run, which re-arms
  on its own LANDED; a merged or closed PR stops it in one line. Hosts with no
  scheduler re-invoke by hand. The status query now also reads the PR `state`,
  and the report gains a main-thread `Watch: armed until` / `Watch: lapsed`
  line.

- **2026-09-28**: **Initial skill.** One command takes an open
  pull request to green checks and no unhandled review threads. It hands off
  to a fresh driver so the coding session's context is never spent, waits
  until every review bot is done on the current head or has gone quiet, fixes
  every verified finding and failing check in one commit per round, pushes
  once to the PR's own branch, and resolves only the threads it fixed. Budget:
  3 rounds, 30 minutes, one flake rerun per check per head. It never merges,
  approves, force-pushes or comments on the PR, and needs only `gh` and `git`.
  Ships the round GraphQL query inline, a pinned capture of every `gh` and
  GraphQL name it uses (`references/gh-surface.json`), triage criteria adapted
  from OpenAI's babysit-pr (Apache-2.0, see NOTICE), and a report template
  with per-round metrics.
