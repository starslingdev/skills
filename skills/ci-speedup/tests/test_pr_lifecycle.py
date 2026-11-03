"""Generic lifecycle fixtures; no repository captures."""
import importlib
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def module():
    return importlib.import_module('pr_lifecycle')


def ts(seconds):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def check(start, end, conclusion='SUCCESS', name='test'):
    return dict(name=name, startedAt=ts(start), completedAt=ts(end), conclusion=conclusion)


def connection(nodes, more=False):
    return dict(nodes=nodes, pageInfo=dict(hasNextPage=more, endCursor='next' if more else None))


def commit(push, checks, oid):
    return dict(commit=dict(oid=oid, committedDate=ts(push), checkSuites=connection([dict(checkRuns=connection(checks))])))


def pr(checks=None):
    return dict(createdAt=ts(0), mergedAt=ts(1000), headRefOid='b',
                commits=connection([commit(0, [check(100, 200, 'FAILURE')], 'a'),
                                    commit(400, checks or [check(500, 600)], 'b')]),
                timelineItems=connection([]))


def test_exact_partition():
    row = module().measure_pr(pr(), ['test'])
    assert row == dict(ci_running=.2, red_waiting=.2, green_waiting=.4, everything_else=.2, final_commit_ci=.1)


def test_overlaps_are_unioned_and_red_wait_excludes_running():
    p = pr([check(500, 650), check(600, 700, name='lint')])
    assert module().measure_pr(p, ['test', 'lint'])['ci_running'] == .3


@pytest.mark.parametrize('final,green,expected', [(.099,.251,True),(.1,.5,False),(.05,.25,False)])
def test_headline_strict_thresholds(final, green, expected):
    data = dict(status='measured', sample_size=1, shares={k:dict(median=v,p90=v) for k,v in dict(ci_running=.2,red_waiting=.1,green_waiting=green,everything_else=.2,final_commit_ci=final).items()})
    assert ('CI is not where your PRs wait.' in module().render(data)) == expected


def test_query_failure_discards_all_partial_results():
    calls=[]
    def query(q):
        calls.append(q)
        return dict(data=dict(repository=dict(pullRequests=connection([pr()], True)))) if len(calls)==1 else None
    result = module().collect(query, 'sample/project', ['test'])
    assert result['status'] == 'not measured'
    assert 'shares' not in result
    assert 'query 2 failed' in module().render(result)


def test_budget_is_four_queries_with_twenty_five_prs_each():
    calls=[]
    def query(q):
        calls.append(q)
        page = connection([pr() for _ in range(25)], True)
        page['pageInfo']['endCursor'] = str(len(calls))
        return dict(data=dict(repository=dict(pullRequests=page)))
    result=module().collect(query,'sample/project',['test'])
    assert len(calls)==4
    assert all('first: 25' in q for q in calls)
    assert result['sample_size']==100


@pytest.mark.parametrize('field', ['commits','timelineItems','checkSuites','checkRuns'])
def test_truncated_nested_connections_never_claim_complete(field):
    p=pr()
    node=p
    if field in ('checkSuites','checkRuns'):
        node=p['commits']['nodes'][0]['commit']
    if field=='checkRuns':
        node=node['checkSuites']['nodes'][0]
    node[field]['pageInfo']['hasNextPage']=True
    with pytest.raises(ValueError, match='truncated'):
        module().measure_pr(p,['test'])


@pytest.mark.parametrize('required', [None,[],['unobserved']])
def test_unknown_required_checks_do_not_invent_green_wait(required):
    with pytest.raises(ValueError,match='required checks'):
        module().measure_pr(pr(),required)


def test_missing_commit_timestamp_is_not_invented():
    p=pr();p['commits']['nodes'][1]['commit']['committedDate']=None
    with pytest.raises(ValueError,match='commit timestamp'):
        module().measure_pr(p,['test'])


def test_report_block_is_integrated_even_without_a_pole():
    m=module()
    import blocking_path
    report=blocking_path.render({'findings':[], 'pr_lifecycle':dict(status='not measured',reason='query 1 failed')})
    assert "Where your PRs' time goes" in report
    assert 'not measured: query 1 failed' in report


def test_timezone_free_dates_are_not_measured():
    p=pr();p['createdAt']='2026-01-01T00:00:00'
    with pytest.raises(ValueError,match='timezone'):
        module().measure_pr(p,['test'])


def test_graphql_requests_supported_commit_timestamp():
    query=module()._query('sample/project',None)
    assert 'committedDate' in query and 'pushedDate' not in query
    assert 'HEAD_REF_FORCE_PUSHED_EVENT' in query


def test_sample_follows_recent_merges_not_recent_creation():
    # Newest-created merged PRs drop every long-lived PR that was opened earlier but
    # merged recently, so the sample skews short. Update time tracks merge time.
    query=module()._query('sample/project',None)
    assert 'field: UPDATED_AT' in query and 'CREATED_AT' not in query
    data=dict(status='measured',sample_size=1,sampled=1,excluded={},
              shares={k:dict(median=.1,p90=.1) for k in ('ci_running','red_waiting','green_waiting','everything_else','final_commit_ci')})
    text=module().render(data)
    assert 'most recently updated' in text and 'creation time' not in text


def test_rewritten_history_discards_all_lifecycle_shares():
    rewritten=pr()
    rewritten['timelineItems']=connection([{'__typename':'HeadRefForcePushedEvent'}])
    page=connection([pr(),rewritten])
    result=module().collect(lambda q:dict(data=dict(repository=dict(pullRequests=page))),
                            'sample/project',['test'])
    assert result['status']=='not measured'
    assert 'shares' not in result
    assert 'rewritten commit history' in module().render(result)


def test_graphql_transport_records_and_replays_without_extra_requests(tmp_path,monkeypatch):
    import collect_runs as cr
    import json
    calls=[]
    class Response:
        returncode=0
        stdout=json.dumps({'data':{'repository':None}})
    monkeypatch.setattr(cr.subprocess,'run',lambda *a,**kw:(calls.append(a) or Response()))
    client=cr.GhClient()
    client._record_dir=tmp_path
    client._fixtures_dir=None
    assert client.graphql('query { viewer { login } }') == {'data':{'repository':None}}
    assert len(calls)==1
    client._fixtures_dir=tmp_path
    assert client.graphql('query { viewer { login } }') == {'data':{'repository':None}}
    assert len(calls)==1


def test_collect_dispatches_lifecycle_and_report_discloses_it(monkeypatch):
    m=module()
    import collect_runs as cr
    import importlib.util
    path=Path(__file__).with_name('test_disclosure_reaches_the_artifact.py')
    spec=importlib.util.spec_from_file_location('lifecycle_integration_fakes',path)
    fake=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fake;spec.loader.exec_module(fake)
    monkeypatch.setattr(cr,'GhClient',fake._TwoWorkflowClient)
    calls=[]
    def collect(*args):
        calls.append(args)
        return dict(status='not measured',reason='query 1 failed')
    monkeypatch.setattr(m,'collect',collect)
    doc=cr.collect(fake._doc(),'o/r',max_runs=8,shallow_runs=8)
    assert len(calls)==1 and doc['pr_lifecycle']['reason']=='query 1 failed'
    import blocking_path
    assert 'not measured: query 1 failed' in blocking_path.render(doc)


def test_draft_wait_is_remainder_not_green_wait():
    p=pr();p['timelineItems']=connection([{'__typename':'ReadyForReviewEvent','createdAt':ts(800)}])
    row=module().measure_pr(p,['test'])
    assert row['green_waiting']==.2
    assert row['red_waiting']==0
    assert row['everything_else']==.6
