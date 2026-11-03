"""Bounded PR lifecycle measurement. Intervals partition life; no inferred timestamps."""
from __future__ import annotations

import json
import math
from datetime import datetime
from statistics import median

HEADLINE = "CI is not where your PRs wait. Most of their time is spent green, waiting to be merged."


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


def measure_pr(pr, required):
    if not required:
        raise ValueError('required checks unavailable')
    start, end = _time(pr['createdAt']), _time(pr['mergedAt'])
    if end <= start:
        raise ValueError('invalid PR lifetime')
    _nodes(pr.get('timelineItems'), 'timelineItems')
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
            if b < a or not c.get('conclusion'):
                raise ValueError('incomplete check interval')
            if a < end and b > start:
                all_checks.append((max(start,a),min(end,b),c))
        if commit['oid'] == pr.get('headRefOid'):
            final = checks
    latest = {}
    for c in final:
        name = c.get('name')
        if name not in latest or _time(c['completedAt']) > _time(latest[name]['completedAt']):
            latest[name] = c
    if not set(required).issubset(latest):
        raise ValueError('required checks missing on final commit')
    if any(latest[n]['conclusion'] not in ('SUCCESS','NEUTRAL','SKIPPED') for n in required):
        raise ValueError('required checks not green on final commit')
    green_start = max(_time(latest[n]['completedAt']) for n in required)
    if green_start > end:
        raise ValueError('required checks completed after merge')
    running = [(a,b) for a,b,_ in all_checks]
    red = []
    for _, completion, c in all_checks:
        if c['conclusion'] in ('FAILURE','TIMED_OUT','ACTION_REQUIRED','STARTUP_FAILURE'):
            later = [p for p in pushes if p > completion and p <= end]
            if later:
                red.append((completion,min(later)))
    green = [(max(start,green_start),end)]
    ci = _length(running)
    red_s = _without(red, running)
    green_s = _without(green, running + red)
    duration = end-start
    final_ci = _length([(max(start,_time(c['startedAt'])),min(end,_time(c['completedAt']))) for c in final])
    return dict(ci_running=ci/duration, red_waiting=red_s/duration,
                green_waiting=green_s/duration, everything_else=max(0,duration-ci-red_s-green_s)/duration,
                final_commit_ci=final_ci/duration)


def _query(repo, cursor):
    owner, name = repo.split('/', 1)
    # 25 PRs * 50 commits * 5 suites * 50 checks stays below the GraphQL node
    # limit. Any nested truncation refuses measurement rather than paginating.
    return '''query { repository(owner: OWNER, name: NAME) {
      pullRequests(first: 25, states: MERGED, orderBy: {field: CREATED_AT, direction: DESC}, after: CURSOR) {
        pageInfo {hasNextPage endCursor}
        nodes { createdAt mergedAt headRefOid reviewDecision
          timelineItems(first: 100, itemTypes: [PULL_REQUEST_REVIEW]) {
            pageInfo {hasNextPage} nodes { __typename ... on PullRequestReview { submittedAt } }
          }
          commits(first: 50) { pageInfo {hasNextPage} nodes { commit {
            oid committedDate checkSuites(first: 5) { pageInfo {hasNextPage} nodes {
              checkRuns(first: 50, filterBy: {checkType: ALL}) { pageInfo {hasNextPage}
                nodes {name startedAt completedAt conclusion}
              }
            }}
          }}}
        }
      }
    }}'''.replace('OWNER',json.dumps(owner)).replace('NAME',json.dumps(name)).replace('CURSOR',json.dumps(cursor))


def collect(query, repo, required):
    """At most four queries, no nested pagination and no partial aggregates."""
    if not required:
        return dict(status='not measured', reason='required checks unavailable', queries=0)
    rows, cursor = [], None
    for page in range(1,5):
        try:
            data = query(_query(repo,cursor))
            if not data or data.get('errors'):
                return dict(status='not measured',reason=f'query {page} failed',queries=page)
            connection = data['data']['repository']['pullRequests']
            nodes = connection['nodes']
            if not isinstance(nodes,list) or len(nodes)>25:
                raise ValueError('malformed PR page')
            rows.extend(measure_pr(p,required) for p in nodes)
            info=connection['pageInfo']
            if info['hasNextPage'] is False:
                break
            if info['hasNextPage'] is not True or not info.get('endCursor') or info['endCursor']==cursor:
                raise ValueError('invalid pagination cursor')
            cursor=info['endCursor']
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            reason = str(exc) if isinstance(exc,ValueError) else 'malformed lifecycle data'
            return dict(status='not measured',reason=reason,queries=page)
    if not rows:
        return dict(status='not measured',reason='no merged PRs',queries=page)
    shares={}
    for key in rows[0]:
        values=sorted(r[key] for r in rows)
        shares[key]=dict(median=median(values),p90=values[math.ceil(.9*len(values))-1])
    return dict(status='measured',sample_size=len(rows),shares=shares,queries=page,
                sample_order='newest created merged PRs',required_checks=list(required))


def render(data):
    lines=["**Where your PRs' time goes**",'']
    if not data or data.get('status')!='measured':
        reason=(data or {}).get('reason','lifecycle data was not collected')
        # Reasons are controlled collector messages, never GraphQL response text.
        return '\n'.join(lines+[f'not measured: {reason}',''])
    shares=data['shares']
    if shares['final_commit_ci']['median'] < .1 and shares['green_waiting']['median'] > .25:
        lines += [HEADLINE,'']
    lines += [f"Sample: {data['sample_size']} merged PRs, newest by creation time; current required-check names. "
              'Shares use each PR\'s open-to-merge lifetime. CI intervals include every available attempt. '
              'Red waiting ends at the next commit timestamp, not its push time (the API does not expose that time).', '',
              '| Time | Median share | p90 share |','| --- | ---: | ---: |']
    for key,label in [('ci_running','CI running'),('red_waiting','Red, waiting for the next commit'),
                      ('green_waiting','Green, waiting for review or merge'),('everything_else','Everything else'),
                      ('final_commit_ci','CI on the final commit (subset)')]:
        s=shares[key];lines.append(f"| {label} | {s['median']:.1%} | {s['p90']:.1%} |")
    lines += ['', 'Overlapping checks count once. Running time takes priority over red waiting, then green waiting; '
              'everything else is the remainder. Category medians need not sum to 100%.','']
    return '\n'.join(lines)
