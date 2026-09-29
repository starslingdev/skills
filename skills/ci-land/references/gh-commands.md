# `gh` commands used by ci-land

Contents: the gate check, resolving a PR and the round query's field-purpose
table, the consecutive-error budget, paging safety, a check waiting on a human,
the status query for waits and the wake-up after LANDED, the watch slice and its exit codes, required checks
missing from the rollup, failed-check run ids and job logs, resolving a thread,
the Reviewers table, the two reviewers behind `cursor`, and re-review after a push.

Every call the skill makes, with syntax checked against **`gh` v2.100.0**
(`gh <cmd> --help`, `gh <cmd> --json` with no value to list fields) and
GitHub's GraphQL schema (introspection through `gh api graphql`). Each query
and mutation below was run live against a real pull request. The pinned list
of names is [gh-surface.json](gh-surface.json); nothing outside it is called.

Add `-R <owner>/<repo>` to the `pr` and `run` subcommands when the working
directory is not the target repo. `gh api` takes the owner and repo in the
path instead.

## Gate: is `gh` usable?

```bash
gh auth status
```

The retry and reporting rules for this gate are in SKILL.md Phase 0. The
token needs contents and pull-requests write on the repo to push and to
resolve threads.

`gh` exit codes (`gh help exit-codes`): `0` success, `1` failure, `2`
cancelled, `4` authentication required. `gh pr checks` adds `8`: checks
pending (`gh pr checks --help`, "Additional exit codes: 8: Checks pending").
Exit `8` is never a failure: SKILL.md Phase 2 treats it exactly like the
slice's `124`.

### The consecutive-error budget

A `gh` failure, for the budget, is a non-zero exit with no usable output, or
a GraphQL response with an `errors` array (it can exit `0`). Exit `8` from
`gh pr checks` is not a failure. When the `errors` array carries
`RATE_LIMITED`, wait once, at most 60 seconds, and retry before counting it.
Three consecutive failures end the run STOPPED; the report quotes the last
error. Any successful call resets the count to zero. The budget exists so an
outage or an exhausted token ends the run with the cause named, rather than
spending the 30-minute budget on retries that cannot succeed.

### Rate-limit handling, with the numeric values tuned in production

The "wait once, at most 60 seconds" rule above matches the numbers a
companion bot-comment fetcher uses against this same API, tuned against real
rate-limit hits (observed in production, copied):

- A `403` is treated as a secondary (abuse) cooldown by default, since
  GitHub's message text varies too much to gate on. Primary quota exhaustion
  is distinguished by the `x-ratelimit-remaining` header reading exactly
  `"0"` (with either `403` or `429`); any other 403/429 is secondary.
- `Retry-After` is honored only when it fits under a **60-second** cap. A
  cooldown with no stated wait, or one beyond the cap, is not worth a local
  retry: give up on that call rather than sleep past the cap.
- A GraphQL response can come back HTTP `200` with an `errors[].type ==
  "RATE_LIMITED"` entry, which is rate limiting too, classified from the
  same response headers a 403/429 would carry, not from the status code.
- GraphQL calls are paced to **1 request/second**, a fixed minimum interval
  between successive calls, independent of volume, because GitHub's abuse
  limits punish bursts more than steady volume.
- `5xx` responses get a small bounded retry: **2 retries**, sleeping
  `2 x attempt` seconds (2s, then 4s), before giving up and surfacing the
  error; this is separate from the rate-limit path above.
- Paging stops the instant `hasNextPage` is `true` but `endCursor` is absent
  or empty, since a null cursor re-fetches the first page and the loop never
  ends (see "Paging safety" above).

## Resolve the PR

```bash
gh pr view <number|url|branch> --json number,url,title,state,isDraft,headRefName,baseRefName,isCrossRepository,headRepository,headRepositoryOwner
```

With no argument it selects the PR for the current branch; no PR for the branch
is a non-zero exit. `url` is `https://github.com/<owner>/<repo>/pull/<n>`, which
is where owner and repo come from. `state` is `OPEN`, `CLOSED` or `MERGED`.

```bash
gh repo view <owner>/<repo> --json defaultBranchRef --jq .defaultBranchRef.name
```

A `baseRefName` different from this is a stacked PR.

Review threads are not among the `gh pr view` JSON fields: asking for them
fails with `Unknown JSON field`. Threads are GraphQL only.

## The round query

The round query and the rules for reading it are in SKILL.md Phase 1; this
file says what each field is for and gives the fallbacks.

What each part answers:

| Field | Used for |
|---|---|
| `headRefOid` | the head every gate condition is measured against |
| `isDraft`, `state` | stop rules |
| `mergeStateStatus` | `BEHIND` (stale base), `DIRTY` (conflicts) |
| `CheckRun.status` / `conclusion` / `completedAt` | gate condition 1, failed-check worklist, start of the quiet window |
| `CheckRun.databaseId` | the job id for the logs endpoint |
| `CheckRun.title` / `summary`, `StatusContext.description` | the check's own output: whether a still-pending check is waiting on a human (below) |
| `checkSuite.workflowRun.databaseId` | the run id for `gh run rerun` |
| `StatusContext.state` | `PENDING`/`EXPECTED` still running, `FAILURE`/`ERROR` failed |
| `isRequired(pullRequestNumber:)` | whether a present check is required |
| `commits(last: 2)` | the head (last node) and the previous head (the node before it) |
| `CheckRun.name` / `StatusContext.context` whose first word, lowercased, begins a bot login | that bot's own check: done on a commit once finished |
| `reviews.commit.oid` | "this reviewer is done on the head"; on the previous head, "it reacted to the last push" |
| `reviews.state` | `CHANGES_REQUESTED` from a bot or a trusted human (NEEDS_HUMAN); pending reviews are not returned |
| `reviews.body` | findings from summary-style bots |
| `comments.body` (issue comments) | findings from bots that comment instead of reviewing |
| `reviewThreads.id` | the node id `resolveReviewThread` takes |
| `isResolved`, `isOutdated`, `path`, `line` | the thread worklist |
| `author.__typename` | `Bot` or not: who gates, and whether a human joined a thread |

The previous-head rule: a bot with neither a review nor a check run on the head
or on the previous head does not gate (it let the last push go by). A bot that
reacted to the previous head but not yet the head gets the 4-minute window.

The pagination, partial-errors, login-normalization and pending-review rules
for this query are in SKILL.md Phase 1.

**Paging safety.** Stop paging when `hasNextPage` is true but `endCursor` is
null: passing a null cursor fetches the first page again, and the loop never
ends. Treat that read as incomplete (a failure for the consecutive-error
budget above), never as the full set.

### A check waiting on a human

Some checks complete only when a person acts: a "Code Review Gate" that
passes once an owner approves, a deployment awaiting a reviewer. The driver
can never finish them, so waiting on one spends the whole budget. The rule
(SKILL.md Phase 2) covers a CheckRun whose `status` is `WAITING` (a job paused
for an environment's reviewers), or a check still pending after every other
check on the head has completed whose name or output says a person must act:
`CheckRun.name`, `title` or `summary`, or `StatusContext.context` or
`description`, containing words such as "review gate", "approval",
"review required" or "awaiting review". It never covers a bot reviewer's own
check (SKILL.md Phase 1): `cubic · AI code reviewer` and
`Cursor Approval Agent: ...` are bots still working. Such a check
is reported as waiting on a human, by name, with the line that says so, and
does not gate the push or LANDED. A pending check that says nothing about a
person is an ordinary pending check: it gates, and a check stuck past the
budget is BUDGET.

### A thread with more than 100 comments

There is no path from the pull request down to one thread by id, so page the
rest of its comments through `node(id:)`:

```bash
gh api graphql -f threadId=<thread-id> -f threadCursor=<endCursor> -f query='
query($threadId: ID!, $threadCursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $threadCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login __typename } authorAssociation body createdAt } } } } }'
```

## The status query, while waiting

Between slices, run this instead of the round query: no thread or comment
bodies, one page. Run the full round query once the gate could open (checks
complete, each bot done or out of its window), a slice reports a failure, or
the signature below changes because a review, thread or comment arrived; the
new findings are then triaged and fixed locally while the push gate stays
closed (SKILL.md Phase 3). Bot decisions that need the previous head carry
over from the last round query.

```bash
gh api graphql -F number=<n> -f owner=<owner> -f name=<repo> -f query='
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state headRefOid
      commits(last: 1) { nodes { commit { oid statusCheckRollup {
        contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion completedAt }
          ... on StatusContext { context state createdAt } } } } } } }
      reviews(last: 20) { totalCount nodes { author { login __typename } state commit { oid } submittedAt } }
      comments { totalCount }
      reviewThreads { totalCount }
} } }'
```

A `headRefOid` that changed, or a `totalCount` that grew, means someone pushed
or posted: run the full round query.

**For the Phase 2 wait**, save the query text (between the quotes) to
`<tmp>/status.graphql`. Its signature is one line that changes when the head,
a review, a thread, an issue comment, or any check's state changes. Take `S`
once when the window starts, then pass it to the wait:

```bash
gh api graphql -F number=<n> -f owner=<owner> -f name=<repo> -F query=@<tmp>/status.graphql --jq '<signature>'
```

`<signature>`, which has no quotes so it nests inside the wait's `bash -c`:

```
.data.repository.pullRequest | [.headRefOid, .reviewThreads.totalCount, .comments.totalCount, .reviews.totalCount, [.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]? | .status // .state]] | tostring
```

## After LANDED: the wake-up

The main thread arms it (SKILL.md Phase 7) through the host's scheduler: on
Claude Code, `/loop`. At arming, it saves the status query to
`<tmp>/status.graphql` and records `S0`, one run of this line; each wake-up
runs the same line once and compares:

```bash
gh api graphql -F number=<n> -f owner=<owner> -f name=<repo> -F query=@<tmp>/status.graphql --jq '.data.repository.pullRequest | .state + " " + ([.headRefOid, .reviewThreads.totalCount, .comments.totalCount, .reviews.totalCount, [.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]? | .status // .state]] | tostring)'
```

- First word `MERGED` or `CLOSED`: stop the wake-up; one line says so.
- The rest equal to `S0`'s: do nothing and say nothing; no reasoning.
- The rest differs (a new head, a new review, thread or issue comment, a
  check whose state changed): stop the wake-up and run ci-land on the PR
  again from Phase 0; that run re-arms on its own LANDED.
- A `gh` error: say nothing; the next wake-up retries. Three in a row: stop
  the wake-up with one line naming the last error.

Interval **10 minutes**, life **at most 4 hours** from the LANDED report; at
4 hours stop with one line: `Watch: lapsed at <time>; /ci-land <n> re-arms it.`
Cost per wake-up: one API call.

## Waiting: the watch slice

```bash
timeout 120 gh pr checks <n> -R <owner>/<repo> --watch --fail-fast
```

The exit codes for this slice and what each one does next are in SKILL.md
Phase 2.

`timeout` is GNU coreutils. Stock macOS lacks it: use `gtimeout` (Homebrew
coreutils), or poll once without `--watch`, where exit `8` means pending (the
same as `124`). A `perl` `alarm` wrapper does not work: `gh` is a Go binary and
ignores `SIGALRM`, so the slice would never expire.

Run SKILL.md Phase 2's wait first, then poll once:

```bash
gh pr checks <n> -R <owner>/<repo>
```

## Required checks absent from the rollup

The round query's `isRequired` covers checks that exist. A required check that
never started does not appear at all. Read the required names and diff them
against the rollup; one that is missing is UNVERIFIED in the report and does
not block the gate.

```bash
gh api repos/<owner>/<repo>/branches/<base> --jq '.protection.required_status_checks.checks[].context'
gh api repos/<owner>/<repo>/rules/branches/<base> --jq '.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
```

The first reads classic branch protection, the second rulesets. Either can be
empty or unreadable with the user's token; then say the required set was not
checked.

## Failed checks: run id, job logs, rerun

Run ids for the head, also the "CI runs on the head" metric in the report:

```bash
gh run list -R <owner>/<repo> --commit <sha> --json databaseId,name,status,conclusion,attempt
```

The failing job's log, as soon as that one job has failed:

```bash
gh api --allow-escape-sequences repos/<owner>/<repo>/actions/jobs/<job-id>/logs
```

`<job-id>` is the CheckRun's `databaseId` from the round query. The response is
plain text. Without `--allow-escape-sequences`, `gh` v2.100.0 refuses to print
it (`the response contains terminal escape sequences`) and exits `1` with empty
output even when redirected to a file, which reads as "no log".

From babysit-pr (verbatim, see NOTICE): "`gh run view --log-failed` is
workflow-run scoped and may not expose failed-job logs until the overall run
finishes. For faster diagnosis, poll the run's jobs first and, as soon as a
specific job has failed, fetch that job's logs directly from the Actions job
logs endpoint."

Rerun a FLAKE, once per head:

```bash
gh run rerun <run-id> -R <owner>/<repo> --failed
```

`--failed` reruns only the failed jobs, including their dependencies. A
StatusContext has no run and is never rerun.

## Resolve a thread you fixed

The mutation, and the rule to run it only after the push has succeeded, are
in SKILL.md Phase 5. `<thread-id>` is `reviewThreads.nodes[].id`
(`PRRT_...`). Check that the response has `isResolved: true` and no
`errors`. An unknown id returns `NOT_FOUND` in `errors` with
`resolveReviewThread: null`.

**Answering a DECLINE** uses `addPullRequestReviewThreadReply` (input `pullRequestReviewThreadId: ID!`,
`body: String!`, returns `comment { id }`; names read from the schema by introspection 2026-09-28). One reply per
thread, body `ci-land: <reason>`, sent before the resolve mutation. A thread with any non-Bot author gets neither.

## Reviewers

The gate rule in Phase 2 is generic and does not depend on this table. Bots
that publish a check run are done when it completes, with no quiet window. The
table records observed behaviour so the skill can explain a wait. Every row is
marked; UNVERIFIED rows are expected, not confirmed.

| Reviewer (normalized login) | Observed | Status |
|---|---|---|
| Greptile (`greptile-apps`) | one review on the first commit; no re-review on later pushes | VERIFIED on 4 PRs, one repo |
| Cubic (`cubic-dev-ai`) | registers its own check run, `cubic · AI code reviewer` (conclusion `NEUTRAL` or `SUCCESS` when complete); done when it completes; skipped a push entirely on one PR | VERIFIED on 2 PRs |
| Cursor Bugbot (`cursor`) | check run `Cursor Bugbot`; a findings pass says it "has reviewed your changes and found N potential issues", a clean one "Bugbot reviewed your changes and found no new issues" | PARTIAL: strings seen on public PRs |
| Cursor's security review (`cursor`) | check run `Cursor Security Agent: Security Reviewer`; body says "Agentic Security Review"; its own reviewer for the gate | PARTIAL: seen on public PRs |
| Cursor's Approval Agent (`cursor`) | check run `Cursor Approval Agent: ...`; runs after Bugbot; a bot check, never waiting on a human | PARTIAL: seen on public PRs |
| CodeRabbit (`coderabbitai`) | review plus a `CodeRabbit` status ("Review complete"); done when it is no longer `PENDING`; the walkthrough comment is not a thread | PARTIAL: status seen on 1 PR |
| Copilot (`copilot-pull-request-reviewer`) | suggested-change blocks; apply as edits | UNVERIFIED |
| Gemini (`gemini-code-assist`) | reviews when mentioned, not on each push; never resolves its own threads | UNVERIFIED |
| Codex connector (`chatgpt-codex-connector`) | reviews when a PR opens or is marked ready, or when mentioned; not on each push; every review names its "Reviewed commit"; a clean pass is a reaction, not a comment | PARTIAL: footer seen on a public PR |
| StarSling Review Runners | check run plus threads from the configured persona; handled like any other bot, no StarSling sign-in needed | UNVERIFIED |
| Human | never gates; a trusted human's `CHANGES_REQUESTED` means NEEDS_HUMAN; an untrusted author's item is DECLINE | n/a |

### Several reviewers behind the `cursor` login

Cursor's Bugbot, its security review and its Approval Agent all post as
`cursor`. Normalizing by login alone merges them into one reviewer, so one
finishing would mark the others done. Split them by check name on the head:
each `Cursor ...` check run is its own reviewer for SKILL.md Phase 2 (rule a
per check). Body tokens do not split them: a security review carries both
`CURSOR_AUTOMATION_ID` and "Agentic Security Review", and the Approval Agent
carries `CURSOR_AUTOMATION_ID` too. A `cursor` review counts as done (rule b)
only once none of its checks on the head is still pending.

### Re-review after a push

Cursor Bugbot, CodeRabbit and Cubic can review every push (UNVERIFIED as a
default; the Greptile and Cubic rows above record pushes they skipped).
Gemini and the Codex connector do not review a push unless mentioned, and
never resolve their own threads. ci-land never
comments, so neither will look at the pushed fixes: SKILL.md Phase 2's
previous-head rule lets them go, and the report says so whenever either
reviewed the PR (references/report-template.md).
