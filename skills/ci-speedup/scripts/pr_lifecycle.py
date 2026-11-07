"""Bounded PR lifecycle measurement. Intervals partition life; no inferred timestamps."""
from __future__ import annotations

import json
import math
from datetime import datetime
from statistics import median

HEADLINE = "CI is not where your PRs wait. Most of their time is spent green, waiting to be merged."
BASIS_REQUIRED = 'required'
BASIS_ALL_FINAL = 'all-final-commit-checks'
ESTIMATE_LABEL = ("Estimated: required checks are not readable, so every check on each PR's final commit is "
                  "treated as required. An optional slow check delays the green moment, so green waiting may "
                  "be understated.")
PASSED = ('SUCCESS', 'NEUTRAL', 'SKIPPED')
UNCOUNTED = ('SKIPPED', 'NEUTRAL')


def _time(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp has no timezone')
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        if 'timezone' in str(exc):
            raise ValueError('timestamp has no timezone') from None
        raise ValueError('invalid timestamp') from None


def _nodes(connection, name):
    if not isinstance(connection, dict) or not isinstance(connection.get('nodes'), list):
        raise ValueError('missing ' + name)
    page = connection.get('pageInfo')
    if not isinstance(page, dict) or page.get('hasNextPage') is not False:
        raise ValueError('truncated ' + name)
    return connection['nodes']


def _union(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _length(intervals):
    return sum(b-a for a,b in _union(intervals))


def _without(intervals, occupied):
    """Length after removing higher-priority categories, without double counting."""
    return _length(intervals + occupied) - _length(occupied)


def _latest(entries):
    """Latest result per check name: (completed at, or inf if never, passed, counts as
    a check when every check is treated as required)."""
    latest = {}
    for name, done, passed, counted in entries:
        at = _time(done) if done else math.inf
        if name not in latest or at > latest[name][0]:
            latest[name] = (at, passed, counted)
    return latest


def _rollup_entries(contexts):
    for c in contexts:
        if c.get('__typename') == 'CheckRun':
            yield (c.get('name'), c.get('completedAt'), c.get('conclusion') in PASSED,
                   bool(c.get('completedAt')) and c.get('conclusion') not in UNCOUNTED)
        elif c.get('__typename') == 'StatusContext':
            yield (c.get('context'), c.get('createdAt'), c.get('state') == 'SUCCESS',
                   c.get('state') not in ('PENDING', 'EXPECTED'))


def _green_start(pr, required):
    """When the last required check on the merged head passed, and the names that
    were required. Reads the head's status rollup, which carries every app's latest
    check and commit statuses. With no readable required checks, every completed,
    non-skipped, non-neutral check on the merged head is treated as required."""
    heads = pr['head']['nodes']
    if len(heads) != 1 or heads[0]['commit']['oid'] != pr.get('headRefOid'):
        raise ValueError('final commit unavailable')
    rollup = heads[0]['commit']['statusCheckRollup']
    if required:
        contexts = rollup['contexts']['nodes'] if rollup else []
    else:  # the estimate needs the whole list: a check past the page could be the last to pass
        contexts = _nodes(rollup['contexts'] if rollup else None, 'final commit checks')
    latest = _latest(_rollup_entries(contexts))
    # A check that completed after the merge did not gate it: for the estimate it never completed.
    merged = _time(pr['mergedAt'])
    names = set(required) if required else {n for n, (at, _, counted) in latest.items() if counted and at <= merged}
    if not names:
        raise ValueError('no completed checks on final commit')
    if not names.issubset(latest):
        raise ValueError('required checks missing on final commit')
    if not all(latest[n][1] and latest[n][0] < math.inf for n in names):
        raise ValueError('required checks not green on final commit')
    return max(latest[n][0] for n in names), names


def _round_green_at(commit, names, estimate):
    """When an earlier commit's last required check passed, or None when that cannot be
    shown from its GitHub Actions jobs (truncated or missing data, a required check
    absent or not passing). Under the estimate the commit's own completed checks must
    pass too. Other apps' checks and commit statuses are read only on the merged head."""
    try:
        runs = [r for s in _nodes(commit.get('checkSuites'), 'checkSuites')
                for r in _nodes(s.get('checkRuns'), 'checkRuns')]
        latest = _latest((r.get('name'), r.get('completedAt'), r.get('conclusion') in PASSED,
                          bool(r.get('completedAt')) and r.get('conclusion') not in UNCOUNTED) for r in runs)
    except (ValueError, AttributeError, TypeError):
        return None
    need = set(names) | ({n for n, (_, _, counted) in latest.items() if counted} if estimate else set())
    if not need.issubset(latest) or not all(latest[n][1] for n in need):
        return None  # a passed job always has a completion time; unknown stays inf, never green
    return max(latest[n][0] for n in need)


def measure_pr(pr, required):
    """`required` is the repo's required-check names, or empty/None when they are not
    readable: then every check on the merged head stands in for them (an estimate)."""
    start, end = _time(pr['createdAt']), _time(pr['mergedAt'])
    if end <= start:
        raise ValueError('invalid PR lifetime')
    timeline = _nodes(pr.get('timelineItems'), 'timelineItems')
    if any(e.get('__typename') == 'HeadRefForcePushedEvent' for e in timeline):
        raise ValueError('rewritten commit history (earlier checks unreadable)')
    transitions = sorted((_time(e.get('createdAt')), e['__typename']) for e in timeline
                         if e.get('__typename') in ('ReadyForReviewEvent', 'ConvertToDraftEvent'))
    draft_start = start if transitions and transitions[0][1] == 'ReadyForReviewEvent' else None
    drafts = []
    for at, kind in transitions:
        at = min(end, max(start, at))
        if kind == 'ConvertToDraftEvent':
            draft_start = at
        elif draft_start is not None:
            drafts.append((draft_start, at))
            draft_start = None
    if draft_start is not None:
        drafts.append((draft_start, end))
    commits = _nodes(pr.get('commits'), 'commits')
    all_checks, pushes, final = [], [], []
    for row in commits:
        commit = row['commit']
        if not commit.get('committedDate'):
            raise ValueError('commit timestamp unavailable')
        pushes.append(_time(commit['committedDate']))
        checks = []
        for suite in _nodes(commit.get('checkSuites'), 'checkSuites'):
            checks.extend(_nodes(suite.get('checkRuns'), 'checkRuns'))
        for c in checks:
            a,b = _time(c.get('startedAt')), _time(c.get('completedAt'))
            if b < a and c.get('conclusion') in ('SKIPPED', 'CANCELLED'):
                b = a  # skipped or cancelled-before-start jobs complete just before they "start"
            if b < a or not c.get('conclusion'):
                raise ValueError('incomplete check interval')
            if a < end and b > start:
                all_checks.append((max(start,a),min(end,b),c))
        if commit['oid'] == pr.get('headRefOid'):
            final = checks
    green_start, names = _green_start(pr, required)
    if green_start > end:
        raise ValueError('required checks completed after merge')
    running = [(a,b) for a,b,_ in all_checks]
    red = []
    for _, completion, c in all_checks:
        if c['conclusion'] in ('FAILURE','TIMED_OUT','ACTION_REQUIRED','STARTUP_FAILURE'):
            later = [p for p in pushes if p > completion and p <= end]
            if later:
                red.append((completion,min(later)))
    # Every review round: an earlier commit is green from its last required check
    # passing until the next commit (or merge). Rounds whose checks cannot be read
    # completely add nothing; only the merged head's data is mandatory.
    green = [(max(start,green_start),end)]
    for i, row in enumerate(commits):
        if row['commit']['oid'] == pr.get('headRefOid'):
            continue
        at = _round_green_at(row['commit'], names, not required)
        if at is not None:
            green.append((max(start, at), min(pushes[i+1:] + [end])))
    ci = _length(running)
    red_s = _without(red, running + drafts)
    green_s = _without(green, running + red + drafts)
    duration = end-start
    final_ci = _length([(max(start,_time(c['startedAt'])),min(end,_time(c['completedAt']))) for c in final])
    return dict(ci_running=ci/duration, red_waiting=red_s/duration,
                green_waiting=green_s/duration, everything_else=max(0,duration-ci-red_s-green_s)/duration,
                final_commit_ci=final_ci/duration)


def _query(repo, cursor):
    owner, name = repo.split('/', 1)
    # Installed apps open empty check suites on every commit, so suites are limited
    # to GitHub Actions (app 15368); 25 PRs of full job history exceeded GitHub's
    # per-query resource limits on busy repos, so a page holds 10. A PR whose nested
    # history is truncated is left out rather than paginated. The merged head's
    # status rollup (every app's latest checks plus commit statuses) decides when
    # the required checks passed.
    return '''query { repository(owner: OWNER, name: NAME) {
      pullRequests(first: 10, states: MERGED, orderBy: {field: UPDATED_AT, direction: DESC}, after: CURSOR) {
        pageInfo {hasNextPage endCursor}
        nodes { createdAt mergedAt headRefOid
          timelineItems(first: 100, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, HEAD_REF_FORCE_PUSHED_EVENT]) {
            pageInfo {hasNextPage} nodes { __typename
              ... on ReadyForReviewEvent { createdAt } ... on ConvertToDraftEvent { createdAt } }
          }
          head: commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { pageInfo {hasNextPage} nodes { __typename
            ... on CheckRun { name completedAt conclusion } ... on StatusContext { context state createdAt } } } } } } }
          commits(first: 30) { pageInfo {hasNextPage} nodes { commit {
            oid committedDate checkSuites(first: 10, filterBy: {appId: 15368}) { pageInfo {hasNextPage} nodes {
              checkRuns(first: 50, filterBy: {checkType: ALL}) { pageInfo {hasNextPage}
                nodes {name startedAt completedAt conclusion}
              }
            }}
          }}}
        }
      }
    }}'''.replace('OWNER',json.dumps(owner)).replace('NAME',json.dumps(name)).replace('CURSOR',json.dumps(cursor))


MIN_MEASURED = 10
PAGE_SIZE = 10


def _excluded_text(excluded):
    return ', '.join(f'{n} {reason}' for reason, n in sorted(excluded.items(), key=lambda kv: (-kv[1], kv[0])))


def collect(query, repo, required):
    """At most four queries, no nested pagination. A failed or malformed page refuses
    the whole block; a PR whose own history cannot be read completely is left out and
    counted, and too few measurable PRs refuses the block. Without readable required
    checks the block is an estimate from every check on each merged head; `basis`
    records which one was used."""
    basis = BASIS_REQUIRED if required else BASIS_ALL_FINAL
    rows, cursor, excluded, sampled = [], None, {}, 0
    for page in range(1,5):
        try:
            data = query(_query(repo,cursor))
            if not data or data.get('errors'):
                return dict(status='not measured',reason=f'query {page} failed',queries=page)
            connection = data['data']['repository']['pullRequests']
            nodes = connection['nodes']
            if not isinstance(nodes,list) or len(nodes)>PAGE_SIZE:
                raise ValueError('malformed PR page')
            for p in nodes:
                if not isinstance(p, dict):
                    raise TypeError('malformed PR node')
                sampled += 1
                try:
                    rows.append(measure_pr(p,required))
                except ValueError as exc:
                    excluded[str(exc)] = excluded.get(str(exc), 0) + 1
            info=connection['pageInfo']
            if info['hasNextPage'] is False:
                break
            if info['hasNextPage'] is not True or not info.get('endCursor') or info['endCursor']==cursor:
                raise ValueError('invalid pagination cursor')
            cursor=info['endCursor']
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            reason = str(exc) if isinstance(exc,ValueError) else 'malformed lifecycle data'
            return dict(status='not measured',reason=reason,queries=page)
    if not sampled:
        return dict(status='not measured',reason='no merged PRs',queries=page)
    if len(rows) < MIN_MEASURED:
        return dict(status='not measured',queries=page,
                    reason=f'only {len(rows)} of {sampled} merged PRs could be measured ({_excluded_text(excluded)})')
    shares={}
    for key in rows[0]:
        values=sorted(r[key] for r in rows)
        shares[key]=dict(median=median(values),p90=values[math.ceil(.9*len(values))-1])
    return dict(status='measured',sample_size=len(rows),sampled=sampled,excluded=excluded,shares=shares,queries=page,
                sample_order='most recently updated merged PRs',basis=basis,
                required_checks=list(required) if required else None)


def render(data):
    lines=["**Where your PRs' time goes**",'']
    if not data or data.get('status')!='measured':
        reason=(data or {}).get('reason','lifecycle data was not collected')
        # Reasons are controlled collector messages, never GraphQL response text.
        return '\n'.join(lines+[f'not measured: {reason}',''])
    estimate = data.get('basis') == BASIS_ALL_FINAL
    if estimate:
        lines += [ESTIMATE_LABEL, '']
    shares=data['shares']
    if shares['final_commit_ci']['median'] < .1 and shares['green_waiting']['median'] > .25:
        lines += [HEADLINE,'']
    sampled, excluded = data.get('sampled', data['sample_size']), data.get('excluded') or {}
    coverage = f"Measured {data['sample_size']} of {sampled} merged PRs"
    if excluded:
        coverage += f" (left out: {_excluded_text(excluded)})"
    checks = 'every completed check on each final commit' if estimate else 'current required-check names'
    lines += [coverage + f', the most recently updated (this tracks recent merges); {checks}. '
              'Shares use each PR\'s open-to-merge lifetime. CI running counts every GitHub Actions job attempt; '
              'required checks from other apps or commit statuses only mark when the PR turned green. '
              'Green waiting covers every review round: each stretch where the PR\'s latest commit had passed '
              'its required checks, until the next commit or the merge. An earlier commit counts only when '
              'every one of those checks is a GitHub Actions job on that commit. '
              'Red and green waiting end at the next commit timestamp, not its push time (the API does not expose that time).', '',
              '| Time | Median share | p90 share |','| --- | ---: | ---: |']
    for key,label in [('ci_running','CI running'),('red_waiting','Red, waiting for the next commit'),
                      ('green_waiting','Green, waiting for review or merge'),('everything_else','Everything else'),
                      ('final_commit_ci','CI on the final commit (subset)')]:
        s=shares[key];lines.append(f"| {label} | {s['median']:.1%} | {s['p90']:.1%} |")
    lines += ['', 'Overlapping checks count once. Running time takes priority over red waiting, then green waiting; '
              'draft waiting stays in everything else. Category medians need not sum to 100%.','']
    return '\n'.join(lines)
