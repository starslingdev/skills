"""Generic, measured merge-queue regressions; no repository captures."""
import copy
import pytest
import collect_runs as cr

WF = '.github/workflows/checks.yml'
DOC = {'on': ['pull_request', 'merge_group'], 'jobs': {
    'test': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]}}}


def detect(**overrides):
    args = dict(workflows={WF: copy.deepcopy(DOC)}, runs_by_workflow={WF: [
        {'event': 'pull_request', 'run_started_at': '2026-01-01T00:00:00Z',
         'updated_at': '2026-01-01T00:10:00Z', 'conclusion': 'success'},
        {'event': 'merge_group', 'run_started_at': '2026-01-01T00:00:00Z',
         'updated_at': '2026-01-01T00:09:00Z', 'conclusion': 'success'}]},
        branch_rules={'strict': True, 'merge_queue': False, 'complete': True},
        volume={'merges_per_day': 40, 'sampled_merged_prs': [1, 2]}, start_id=0)
    args.update(overrides)
    return cr._detect_opt98_merge_queue(**args)


def test_strict_high_volume_and_base_churn():
    doc = copy.deepcopy(DOC); doc['on'] = ['pull_request']
    rows = [{'event': 'pull_request', 'pull_requests': [{'number': 1, 'base': {'sha': b}}]} for b in ('base-a', 'base-b')]
    findings, info = detect(workflows={WF: doc}, runs_by_workflow={WF: rows})
    assert len(findings) == 1 and findings[0]['merge_queue']['case'] == 'strict'
    assert findings[0]['merge_queue']['rebase_churn_share'] == .5
    assert findings[0]['wall_clock_p50_s'] is None
    assert info['status'] == 'measured'
    assert not detect(workflows={WF: doc}, runs_by_workflow={}, branch_rules={'strict': False, 'merge_queue': False, 'complete': True})[0]
    assert not detect(workflows={WF: doc}, runs_by_workflow={}, branch_rules={'strict': True, 'merge_queue': False, 'complete': False})[0]


@pytest.mark.parametrize('queue,rate', [(True, 40), (False, 5)])
def test_strict_queue_and_low_volume_are_withheld(queue, rate):
    doc = copy.deepcopy(DOC); doc['on'] = ['pull_request']
    findings, info = detect(workflows={WF: doc}, runs_by_workflow={}, branch_rules={'strict': True, 'merge_queue': queue, 'complete': True}, volume={'merges_per_day': rate, 'sampled_merged_prs': []})
    assert info['status'] == 'measured'
    assert not findings


def test_unreadable_rule_is_disclosed():
    findings, info = detect(branch_rules={}, runs_by_workflow={})
    assert not findings
    assert info['branch_rule'] == 'branch rule not readable'


def test_full_suite_requires_static_and_measured_evidence():
    findings, info = detect()
    assert len(findings) == 1
    evidence = findings[0]['merge_queue']
    assert evidence['case'] == 'reproving'
    assert evidence['queue_median_s'] == 540 and evidence['pr_median_s'] == 600
    assert findings[0]['runner_min_saving'] is None
    assert cr._opt98_full_suite(DOC) == ['test']


@pytest.mark.parametrize('command', ['nx affected -t test', 'turbo run test --filter=...[base]', 'bazel test --remote_cache=https://cache.invalid //...'])
def test_affected_or_remote_cache_clears_candidate(command):
    doc = copy.deepcopy(DOC); doc['jobs']['test']['steps'][0]['run'] = command
    assert cr._opt98_full_suite(doc) == []
    assert not detect(workflows={WF: doc})[0]


def test_fast_queue_is_not_reproving():
    findings, info = detect(runs_by_workflow={WF: [
        {'event': 'pull_request', 'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:10:00Z', 'conclusion': 'success'},
        {'event': 'merge_group', 'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:05:00Z', 'conclusion': 'success'}]})
    assert info['status'] == 'measured'
    assert not findings


class Client:
    def __init__(self, payloads): self.payloads, self.calls = payloads, []
    def json(self, path, **kwargs):
        self.calls.append(path)
        return self.payloads.get(path)


def test_rules_reuse_existing_required_check_reads():
    client = Client({'repos/example/project/rulesets?includes_parents=true': [{'id': 1}],
        'repos/example/project/rulesets/1': {'enforcement': 'active', 'rules': [
            {'type': 'required_status_checks', 'parameters': {'strict_required_status_checks_policy': True}},
            {'type': 'merge_queue'}]},
        'repos/example/project/branches/main/protection/required_status_checks': {'strict': False}})
    info = {}
    cr._fetch_required_checks(client, 'example/project', branch_rules=info)
    assert info == {'strict': True, 'merge_queue': True, 'complete': True}
    assert len(client.calls) == 3

@pytest.mark.parametrize('change', ['conditional', 'cache', 'different_jobs'])
def test_uncertain_or_cached_work_is_not_full_suite(change):
    doc = copy.deepcopy(DOC)
    if change == 'conditional': doc['jobs']['test']['if'] = "github.event_name == 'pull_request'"
    if change == 'cache': doc['jobs']['test']['steps'].insert(0, {'uses': 'actions/cache/restore@v4', 'with': {'path': '.build', 'key': 'build'}})
    if change == 'different_jobs': doc['jobs']['queue'] = {'if': "github.event_name == 'merge_group'", 'steps': [{'run': 'npm test'}]}
    assert cr._opt98_full_suite(doc) == []


def test_volume_search_count_uses_fixed_window_and_incomplete_fails_closed():
    class Search:
        def json(self, path, **kwargs):
            assert 'merged%3A2026-01-01..2026-01-30' in path
            return {'total_count': 1200, 'incomplete_results': False, 'items': [{'number': 1}]}
    assert cr._opt98_merge_volume(Search(), 'example/project', '2026-01-01', '2026-01-30', branch='main')['merges_per_day'] == 40
    class Incomplete:
        def json(self, *args, **kwargs): return {'total_count': 1200, 'incomplete_results': True}
    assert cr._opt98_merge_volume(Incomplete(), 'example/project', '2026-01-01', '2026-01-30', branch='main')['reason'] == 'merged PR search incomplete'


def test_prompt_carries_queue_guardrail_without_claiming_savings():
    import blocking_path as bp
    findings, _ = detect()
    prompt = '\n'.join(bp._hygiene_prompt('OPT98', findings[0]['title'], findings, 'https://example.invalid/pattern'))
    assert 'Keep every required check on the merged result' in prompt
    assert 'No wall-clock or runner-minute saving is credited' in prompt
    assert 'git log' in prompt


def test_data_sources_render_missing_branch_rule():
    import blocking_path as bp
    rendered = '\n'.join(bp._data_sources_footer({'data_sources': {'merge_queue': {'branch_rule': 'branch rule not readable'}}}, 'example/project'))
    assert 'branch rule not readable' in rendered

@pytest.mark.parametrize('change', ['parallel', 'step_env'])
def test_nested_or_step_scoped_remote_cache_is_not_full_suite(change):
    doc = copy.deepcopy(DOC)
    if change == 'parallel': doc['jobs']['test']['steps'] = [{'parallel': [{'run': 'nx affected -t test'}]}]
    else: doc['jobs']['test']['steps'][0]['env'] = {'TURBO_TOKEN': '${{ secrets.CACHE_TOKEN }}'}
    assert cr._opt98_full_suite(doc) == []


def test_catalog_link_has_one_fragment_separator():
    import blocking_path as bp
    findings, _ = detect()
    assert bp._catalog_anchor('https://example.invalid/catalog', findings).count('#') == 1


def test_turbo_affected_is_scoped():
    doc = copy.deepcopy(DOC); doc['jobs']['test']['steps'][0]['run'] = 'turbo run test --affected'
    assert cr._opt98_full_suite(doc) == []


def test_collector_dispatch_reaches_report(monkeypatch):
    import blocking_path as bp
    from test_opt81_faster_runner import _Opt81Client, _CI
    class QueueClient(_Opt81Client):
        def json(self, endpoint, allow_missing=False):
            data = super().json(endpoint, allow_missing)
            if isinstance(data, dict) and 'workflow_runs' in data:
                for i, run in enumerate(data['workflow_runs']):
                    run['event'] = 'merge_group' if i % 2 else 'pull_request'
            return data
    client = QueueClient([('ubuntu-latest', 300)] * 20,
                         'on: [pull_request, merge_group]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n')
    monkeypatch.setattr(cr, 'GhClient', lambda *a, **k: client)
    result = cr.collect({'repo': 'example/project', 'findings': [{'id': 'f1', 'pattern': 'OPT1', 'workflow_file': _CI}]}, 'o/r', max_runs=20, shallow_runs=20)
    findings = [f for f in result['findings'] if f.get('pattern') == 'OPT98']
    assert len(findings) == 1
    report = bp.render(result)
    assert 'No wall-clock or runner-minute saving is credited' in report
    assert 'branch rule not readable' in report

@pytest.mark.parametrize('doc', [{'on': 7, 'jobs': {}}, {'on': ['pull_request', 'merge_group'], 'jobs': {'test': {'steps': [{'run': 'npm test', 'env': 7}]}}}])
def test_malformed_workflow_does_not_abort_audit(doc):
    findings, info = detect(workflows={WF: doc})
    assert findings == []
    assert WF in info['unreadable_workflows']


def test_merge_volume_filters_the_rule_branch():
    from urllib.parse import unquote
    class Search:
        def json(self, path, **kwargs):
            assert 'base:"main"' in unquote(path)
            return {'total_count': 1200, 'incomplete_results': False, 'items': [{'number': 1}]}
    volume = cr._opt98_merge_volume(Search(), 'example/project', '2026-01-01', '2026-01-30', branch='main')
    assert volume['sampled_merged_prs'] == [1]


def test_event_dependent_work_is_withheld():
    for change in ('command', 'input', 'matrix', 'shell'):
        doc = copy.deepcopy(DOC)
        job = doc['jobs']['test']
        if change == 'command': job['steps'][0]['run'] = "pytest ${{ github.event_name == 'merge_group' && 'smoke' || 'tests' }}"
        if change == 'input': job['steps'].insert(0, {'uses': 'actions/setup-python@v5', 'with': {'python-version': '${{ github.event_name }}'}})
        if change == 'matrix': job['strategy'] = {'matrix': '${{ fromJSON(github.event.inputs.matrix) }}'}
        if change == 'shell': job['steps'][0]['run'] = 'pytest "$GITHUB_EVENT_NAME"'
        assert cr._opt98_full_suite(doc) == [], change


def test_unread_script_and_action_indirection_are_withheld():
    for step in ({'run': 'npm test'}, {'uses': './.github/actions/build'}, {'uses': 'example/build@v1'}, {'run': './scripts/check.sh'}):
        doc = copy.deepcopy(DOC)
        doc['jobs']['test']['steps'] = [step]
        assert cr._opt98_full_suite(doc) == [], step
