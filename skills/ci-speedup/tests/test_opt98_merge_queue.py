"""Generic, measured merge-queue regressions; no repository captures."""
import copy
import datetime as dt
import pytest
import collect_runs as cr

WF = '.github/workflows/checks.yml'
DOC = {'on': ['pull_request', 'merge_group'], 'jobs': {
    'test': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]}}}
NO_QUEUE = {'on': ['pull_request'], 'jobs': DOC['jobs']}
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _at(seconds):
    return (T0 + dt.timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def runs(event, seconds, n=15, **extra):
    """n successful first-attempt runs of `event`, each lasting `seconds`."""
    row = {'event': event, 'run_started_at': _at(0), 'updated_at': _at(seconds),
           'conclusion': 'success', 'run_attempt': 1, **extra}
    return [dict(row) for _ in range(n)]


def detect(**overrides):
    args = dict(workflows={WF: copy.deepcopy(DOC)},
                runs_by_workflow={WF: runs('pull_request', 600) + runs('merge_group', 540)},
                branch_rules={'strict': True, 'merge_queue': False, 'complete': True},
                volume={'merges_per_day': 40, 'sampled_merged_prs': [1, 2]}, start_id=0)
    args.update(overrides)
    return cr._detect_opt98_merge_queue(**args)


def strict_case(**overrides):
    """The up-to-date rule at volume, with no queue anywhere."""
    args = dict(workflows={WF: copy.deepcopy(NO_QUEUE)}, runs_by_workflow={})
    args.update(overrides)
    return detect(**args)


class Client:
    def __init__(self, payloads): self.payloads, self.calls = payloads, []
    def json(self, path, **kwargs):
        self.calls.append(path)
        return self.payloads.get(path)


def prompt_for(findings):
    import blocking_path as bp
    return '\n'.join(bp._hygiene_prompt('OPT98', findings[0]['title'], findings, 'https://example.invalid/pattern'))


def footer(info):
    import blocking_path as bp
    return '\n'.join(bp._data_sources_footer({'data_sources': {'merge_queue': info}}, 'example/project'))


# --- C1: the up-to-date rule at high volume ---------------------------------

def test_strict_high_volume_fires_without_a_churn_share_from_run_pages():
    # GitHub empties `pull_requests[]` once a PR merges (and forks never carry
    # it), so a join from merged PR numbers to run pages always reads 0%.
    merged_shape = [{'event': 'pull_request', 'pull_requests': [], 'head_sha': f'h{i}'} for i in range(4)]
    findings, info = strict_case(runs_by_workflow={WF: merged_shape})
    assert len(findings) == 1 and findings[0]['merge_queue']['case'] == 'strict'
    assert findings[0]['merge_queue']['rebase_churn_share'] is None
    assert 'Rebase-churn share not measured' in findings[0]['evidence']
    assert '0.0%' not in findings[0]['evidence'] and '0%' not in findings[0]['evidence']
    assert findings[0]['wall_clock_p50_s'] is None
    assert info['status'] == 'measured'
    assert not strict_case(branch_rules={'strict': False, 'merge_queue': False, 'complete': True})[0]
    assert not strict_case(branch_rules={'strict': True, 'merge_queue': False, 'complete': False})[0]


def _timeline(*events):
    return list(events)


CHURN_PAYLOADS = {
    # base merged in through a two-parent commit
    'repos/example/project/issues/1/timeline?per_page=100': _timeline(
        {'event': 'committed', 'parents': [{'sha': 'a'}]},
        {'event': 'committed', 'parents': [{'sha': 'b'}, {'sha': 'c'}]}),
    # head force-pushed (a rebase, or an amend)
    'repos/example/project/issues/2/timeline?per_page=100': _timeline(
        {'event': 'committed', 'parents': [{'sha': 'a'}]}, {'event': 'head_ref_force_pushed'}),
    # neither
    'repos/example/project/issues/3/timeline?per_page=100': _timeline(
        {'event': 'committed', 'parents': [{'sha': 'a'}]}, {'event': 'merged'}),
    # 4: unreadable
}


def test_churn_counts_base_merges_and_force_pushes_from_pr_timelines():
    client = Client(CHURN_PAYLOADS)
    churn = cr._opt98_rebase_churn(client, 'example/project', [1, 2, 3, 4])
    assert churn == {'sampled': 3, 'unreadable': 1, 'merged_base': 1, 'force_pushed': 1,
                     'updated': 2, 'truncated': 0}
    findings, _ = strict_case(churn=churn)
    assert findings[0]['merge_queue']['rebase_churn_share'] == pytest.approx(2 / 3)
    assert '2 of 3 sampled merged PRs (67%)' in findings[0]['evidence']
    assert '1 merged the base branch in, 1 force-pushed' in findings[0]['evidence']
    assert '1 timeline(s) not readable' in findings[0]['evidence']


def test_churn_counts_a_pr_with_both_signals_once():
    both = {'repos/example/project/issues/9/timeline?per_page=100': _timeline(
        {'event': 'committed', 'parents': [{'sha': 'b'}, {'sha': 'c'}]}, {'event': 'head_ref_force_pushed'})}
    churn = cr._opt98_rebase_churn(Client(both), 'example/project', [9])
    assert (churn['updated'], churn['merged_base'], churn['force_pushed']) == (1, 1, 1)


def test_churn_reads_at_most_30_timelines_and_discloses_none_readable():
    client = Client({})
    churn = cr._opt98_rebase_churn(client, 'example/project', list(range(1, 41)))
    assert len(client.calls) == 30
    assert churn['sampled'] == 0 and churn['unreadable'] == 30
    findings, _ = strict_case(churn=churn)
    assert findings[0]['merge_queue']['rebase_churn_share'] is None
    assert 'not measured: no merged PR timeline was readable' in findings[0]['evidence']


def test_truncated_timelines_mark_the_share_a_lower_bound():
    long = {'repos/example/project/issues/1/timeline?per_page=100': [{'event': 'commented'}] * 100}
    churn = cr._opt98_rebase_churn(Client(long), 'example/project', [1])
    assert churn['truncated'] == 1
    findings, _ = strict_case(churn=churn)
    assert 'lower bound' in findings[0]['evidence']


@pytest.mark.parametrize('rate,fires', [(30, True), (29.9, False)])
def test_merge_threshold_is_30_a_day(rate, fires):
    findings, _ = strict_case(volume={'merges_per_day': rate, 'sampled_merged_prs': []})
    assert bool(findings) is fires


@pytest.mark.parametrize('queue,rate', [(True, 40), (False, 5)])
def test_strict_queue_and_low_volume_are_withheld(queue, rate):
    findings, info = strict_case(branch_rules={'strict': True, 'merge_queue': queue, 'complete': True},
                                 volume={'merges_per_day': rate, 'sampled_merged_prs': []})
    assert info['status'] == 'measured'
    assert not findings


def test_cached_merge_group_runs_count_as_a_queue():
    # A queue whose workflow file was not sampled still leaves merge_group runs.
    findings, _ = strict_case(runs_by_workflow={'.github/workflows/other.yml': [{'event': 'merge_group'}]})
    assert not findings


def test_unreadable_workflow_withholds_the_strict_case():
    findings, info = strict_case(workflows={WF: copy.deepcopy(NO_QUEUE), '.github/workflows/bad.yml': {'on': 7}})
    assert not findings and info['unreadable_workflows'] == ['.github/workflows/bad.yml']


def test_unreadable_rule_is_disclosed():
    findings, info = detect(branch_rules={}, runs_by_workflow={})
    assert not findings
    assert info['branch_rule'] == 'branch rule not readable'


def test_strict_without_a_count_is_disclosed():
    findings, info = strict_case(volume={})
    assert not findings
    assert info['merged_prs'] == 'merged PR count not fetched'
    assert info['status'] == 'not_measured'


@pytest.mark.parametrize('ruleset,classic,expected', [
    (False, None, None),               # classic 404 for a non-admin: off is unknown
    (False, {'strict': False}, False),  # both read off
    (True, None, True),                 # any source on is on
    (False, {'strict': True}, True),
    (None, {'strict': False}, False),
    (None, None, None),
    (False, {'contexts': ['test']}, None),  # classic read, but no strict value
])
def test_strict_off_needs_classic_protection_read(ruleset, classic, expected):
    payloads = {'repos/example/project/rulesets?includes_parents=true': [{'id': 1}] if ruleset is not None else []}
    if ruleset is not None:
        payloads['repos/example/project/rulesets/1'] = {'enforcement': 'active', 'rules': [
            {'type': 'required_status_checks', 'parameters': {'strict_required_status_checks_policy': ruleset}}]}
    if classic is not None:
        payloads['repos/example/project/branches/main/protection/required_status_checks'] = classic
    info = {}
    cr._fetch_required_checks(Client(payloads), 'example/project', branch_rules=info)
    assert info['strict'] is expected


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


@pytest.mark.parametrize('rulesets', [{'message': 'error'}, [{'name': 'no id'}]])
def test_unreadable_rulesets_leave_the_rule_incomplete(rulesets):
    info = {}
    cr._fetch_required_checks(Client({'repos/example/project/rulesets?includes_parents=true': rulesets,
        'repos/example/project/branches/main/protection/required_status_checks': {'strict': True}}),
        'example/project', branch_rules=info)
    assert info['complete'] is False


# --- collector gate, window and bounded calls --------------------------------

class GateClient(Client):
    def __init__(self, total=1200):
        super().__init__(dict(CHURN_PAYLOADS))
        self.total = total
    def json(self, path, **kwargs):
        if path.startswith('search/issues'):
            self.calls.append(path)
            return {'total_count': self.total, 'incomplete_results': False, 'items': [{'number': 1}, {'number': 2}]}
        return super().json(path, **kwargs)


STRICT = {'strict': True, 'merge_queue': False, 'complete': True}


def gate(rules=STRICT, wf_docs=None, runs=None, pin='2026-01-31T12:00:00Z', total=1200):
    client = GateClient(total)
    out = cr._opt98_collect_volume(client, 'example/project', 'main', rules,
                                   wf_docs if wf_docs is not None else {WF: NO_QUEUE}, runs or {}, pin)
    return client, out


def test_collector_counts_the_30_days_before_the_pin_then_reads_timelines():
    from urllib.parse import unquote
    client, (volume, churn) = gate()
    assert 'merged:2026-01-01..2026-01-30' in unquote(client.calls[0])
    assert volume['merges_per_day'] == 40 and volume['window_days'] == 30
    assert churn['sampled'] == 2 and len(client.calls) == 3


def test_collector_reads_no_timelines_below_the_threshold():
    client, (volume, churn) = gate(total=600)
    assert volume['merges_per_day'] == 20 and churn is None and len(client.calls) == 1


@pytest.mark.parametrize('rules,wf_docs,runs', [
    ({'strict': False, 'merge_queue': False, 'complete': True}, None, None),
    ({'strict': None, 'merge_queue': False, 'complete': True}, None, None),
    ({'strict': True, 'merge_queue': True, 'complete': True}, None, None),
    ({'strict': True, 'merge_queue': False, 'complete': False}, None, None),
    (STRICT, {WF: DOC}, None),
    (STRICT, None, {WF: [{'event': 'merge_group'}]}),
])
def test_collector_pays_nothing_unless_strict_without_a_queue(rules, wf_docs, runs):
    client, (volume, churn) = gate(rules, wf_docs, runs)
    assert client.calls == [] and volume == {} and churn is None


def test_unparseable_date_pin_is_disclosed():
    client, (volume, churn) = gate(pin='not-a-date')
    assert client.calls == [] and churn is None
    assert volume['reason'] == 'merged PR window not readable: the date pin did not parse'


def test_volume_search_count_uses_fixed_window_and_incomplete_fails_closed():
    class Search:
        def json(self, path, **kwargs):
            assert 'merged%3A2026-01-01..2026-01-30' in path
            return {'total_count': 1200, 'incomplete_results': False, 'items': [{'number': 1}]}
    assert cr._opt98_merge_volume(Search(), 'example/project', '2026-01-01', '2026-01-30', branch='main')['merges_per_day'] == 40
    for flag in (True, None):
        class Incomplete:
            def json(self, *args, **kwargs): return {'total_count': 1200, 'incomplete_results': flag}
        assert cr._opt98_merge_volume(Incomplete(), 'example/project', '2026-01-01', '2026-01-30', branch='main')['reason'] == 'merged PR search incomplete'


def test_merge_volume_filters_the_rule_branch():
    from urllib.parse import unquote
    class Search:
        def json(self, path, **kwargs):
            assert 'base:"main"' in unquote(path)
            return {'total_count': 1200, 'incomplete_results': False, 'items': [{'number': 1}]}
    volume = cr._opt98_merge_volume(Search(), 'example/project', '2026-01-01', '2026-01-30', branch='main')
    assert volume['sampled_merged_prs'] == [1]


# --- C2: the queue re-runs the full suite -------------------------------------

def test_full_suite_requires_static_and_measured_evidence():
    findings, info = detect()
    assert len(findings) == 1
    evidence = findings[0]['merge_queue']
    assert evidence['case'] == 'reproving'
    assert evidence['queue_median_s'] == 540 and evidence['pr_median_s'] == 600
    assert evidence['queue_samples'] == 15 and evidence['pr_samples'] == 15
    assert findings[0]['runner_min_saving'] is None
    assert info['status'] == 'measured'
    assert cr._opt98_full_suite(DOC) == ['test']


def test_reproving_finding_names_the_workflow_and_jobs():
    import blocking_path as bp
    findings, _ = detect()
    f = findings[0]
    assert f['workflow_file'] == WF and f['affected_jobs'] == ['test']
    assert f'`{WF}`' in f['evidence'] and 'jobs `test`' in f['evidence']
    block = '\n'.join(bp._opt98_report_block({'findings': findings}))
    assert f'### OPT98: The merge queue re-proves the full suite — `{WF}`' in block
    prompt = prompt_for(findings)
    assert f'Workflow: {WF}' in prompt and 'Jobs: test' in prompt


@pytest.mark.parametrize('event', ['pull_request', 'merge_group'])
@pytest.mark.parametrize('n,fires', [(14, False), (15, True)])
def test_each_event_needs_15_runs(event, n, fires):
    pr_n, queue_n = (n, 15) if event == 'pull_request' else (15, n)
    findings, info = detect(runs_by_workflow={WF: runs('pull_request', 600, pr_n) + runs('merge_group', 540, queue_n)})
    assert bool(findings) is fires
    if not fires:
        assert info['thin_samples'] == [f'{WF} (queue {queue_n} and PR {pr_n} successful first-attempt runs; 15 each needed)']
        assert info['status'] == 'partial'


@pytest.mark.parametrize('queue_s,fires', [(480, True), (479, False), (900, True), (901, False)])
def test_ratio_band(queue_s, fires):
    findings, info = detect(runs_by_workflow={WF: runs('pull_request', 600) + runs('merge_group', queue_s)})
    assert bool(findings) is fires
    if queue_s == 901:
        assert info['pr_runs_differ'] == [f'{WF} (queue median 901s is 1.5x the PR median 600s)']


def test_median_not_mean():
    # 8 of 15 queue runs at 500s (ratio 0.83), 7 very short: the median fires, a mean would not.
    queue = runs('merge_group', 500, 8) + runs('merge_group', 10, 7)
    assert detect(runs_by_workflow={WF: runs('pull_request', 600) + queue})[0]


@pytest.mark.parametrize('bad', [{'conclusion': 'failure'}, {'run_attempt': 2}, {'updated_at': _at(0)},
                                 {'updated_at': None}, {'event': 'push'}])
def test_only_successful_first_attempt_timed_runs_count(bad):
    queue = runs('merge_group', 540, 14) + [dict(runs('merge_group', 540, 1)[0], **bad)]
    findings, info = detect(runs_by_workflow={WF: runs('pull_request', 600) + queue})
    assert not findings and info['thin_samples']


@pytest.mark.parametrize('missing', ['pull_request', 'merge_group'])
def test_missing_event_timings_are_disclosed(missing):
    rows = [r for r in runs('pull_request', 600) + runs('merge_group', 540) if r['event'] != missing]
    findings, info = detect(runs_by_workflow={WF: rows})
    assert not findings and info['unmeasured_workflows'] == [WF]


def test_fast_queue_is_not_reproving():
    findings, info = detect(runs_by_workflow={WF: runs('pull_request', 600) + runs('merge_group', 300)})
    assert info['status'] == 'measured'
    assert not findings


REAL = {
    'name': 'CI', 'run-name': '${{ github.event_name }} checks',
    'on': {'pull_request': {'branches': ['main']}, 'merge_group': None},
    'concurrency': {'group': '${{ github.workflow }}-${{ github.ref }}', 'cancel-in-progress': True},
    'env': {'API_TOKEN': '${{ secrets.API_TOKEN }}'},
    'jobs': {
        'lint': {'runs-on': 'ubuntu-latest', 'timeout-minutes': 10, 'steps': [
            {'uses': 'actions/checkout@v4'},
            {'uses': 'pnpm/action-setup@v4'},
            {'uses': 'actions/setup-node@v4', 'with': {'node-version': 20, 'cache': 'pnpm'}},
            {'run': 'pnpm install --frozen-lockfile'},
            {'name': 'Lint ${{ github.ref }}', 'run': 'pnpm lint'}]},
        'test': {'runs-on': '${{ matrix.os }}', 'if': '${{ !cancelled() }}',
                 'strategy': {'matrix': {'os': ['ubuntu-latest'], 'shard': [1, 2]}},
                 'steps': [
            {'uses': 'actions/checkout@v4', 'with': {'ref': '${{ github.event.pull_request.head.sha }}'}},
            {'uses': 'actions/cache@v4', 'with': {'path': '~/.pnpm-store\nnode_modules', 'key': '${{ hashFiles(\'pnpm-lock.yaml\') }}'}},
            {'run': 'pnpm install && pnpm test -- --shard=${{ matrix.shard }}/2', 'env': {'CI': 'true'}},
            {'uses': 'example/upload-coverage@v4', 'if': 'always()', 'with': {'token': '${{ secrets.COVERAGE }}'}}]}}}


def test_real_shaped_workflow_reaches_the_static_check():
    # Expressions in unrelated places (names, concurrency, secrets, matrix,
    # runner labels) and dependency caches do not withhold the candidate.
    assert cr._opt98_full_suite(REAL) == ['lint', 'test']


@pytest.mark.parametrize('command', [
    'nx affected -t test', 'npx nx affected --target=test',
    'turbo run test --filter=...[base]', 'pnpm turbo run test --filter="...[origin/main]"',
    'pnpm --filter "...[origin/main]" test', 'turbo run test --affected',
    'pnpm vitest run --changed origin/main', 'npx jest --onlyChanged', 'npx jest --changedSince=origin/main',
    'npx playwright test --only-changed', 'git diff --name-only origin/main | xargs pytest',
    'bazel test --remote_cache=https://cache.invalid //...', 'bazel test --disk_cache=/tmp/b //...',
    './gradlew test --build-cache'])
def test_affected_or_remote_cache_clears_candidate(command):
    doc = copy.deepcopy(DOC); doc['jobs']['test']['steps'][0]['run'] = command
    assert cr._opt98_full_suite(doc) == []
    findings, info = detect(workflows={WF: doc})
    assert not findings and not info.get('static_unknown')


@pytest.mark.parametrize('step', [
    {'uses': 'nrwl/nx-set-shas@v4'}, {'uses': 'dorny/paths-filter@v3'},
    {'uses': 'tj-actions/changed-files@v44'}, {'uses': 'Swatinem/rust-cache@v2'},
    {'uses': 'gradle/actions/setup-gradle@v4'}, {'uses': 'example/sccache-action@v1'},
    {'uses': 'actions/cache@v4', 'with': {'path': '.turbo', 'key': 'turbo'}},
    {'uses': 'actions/cache/restore@v4', 'with': {'path': ['node_modules', 'dist'], 'key': 'x'}},
    {'uses': 'actions/cache@v4', 'with': {'key': 'x'}},
    {'run': 'pytest', 'env': {'TURBO_TOKEN': '${{ secrets.CACHE_TOKEN }}'}},
    {'run': 'pytest', 'env': {'NX_CLOUD_ACCESS_TOKEN': '${{ secrets.NX }}'}},
    {'uses': 'example/build@v1', 'with': {'args': 'nx affected -t build'}}])
def test_scoping_or_build_cache_steps_clear_candidate(step):
    doc = copy.deepcopy(DOC); doc['jobs']['test']['steps'].insert(0, step)
    assert cr._opt98_full_suite(doc) == []


@pytest.mark.parametrize('where', ['workflow', 'job'])
def test_remote_cache_env_at_any_level_clears_candidate(where):
    doc = copy.deepcopy(DOC)
    (doc if where == 'workflow' else doc['jobs']['test'])['env'] = {'TURBO_TOKEN': '${{ secrets.T }}'}
    assert cr._opt98_full_suite(doc) == []


def test_dependency_caches_alone_keep_the_candidate():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['steps'].insert(0, {'uses': 'actions/cache@v4', 'with': {'path': '~/.cache/pip\n.venv', 'key': 'deps'}})
    assert cr._opt98_full_suite(doc) == ['test']


@pytest.mark.parametrize('change,reason', [
    ('job_if', 'event-dependent condition on job `test`'),
    ('step_if', 'event-dependent condition on job `test`'),
    ('needs_if', 'job `test` runs only on another job\'s output'),
    ('command', 'event-dependent command or input in job `test`'),
    ('input', 'event-dependent command or input in job `test`'),
    ('step_env', 'event-dependent command or input in job `test`'),
    ('shell', 'event-dependent command or input in job `test`'),
    ('matrix', 'generated matrix on job `test`'),
    ('reusable', 'job `test` calls a reusable workflow'),
    ('local', 'job `test` uses local action `./.github/actions/build`, not read'),
    ('queue_job', 'event-dependent condition on job `queue`'),
    ('workflow_env', 'event-dependent workflow env'),
    ('parallel', 'unrecognized step shape in job `test`'),
])
def test_unsettled_work_is_withheld_and_named(change, reason):
    doc = copy.deepcopy(DOC)
    job = doc['jobs']['test']
    if change == 'job_if': job['if'] = "github.event_name == 'pull_request'"
    if change == 'step_if': job['steps'][0]['if'] = "github.event_name != 'merge_group'"
    if change == 'needs_if': job['if'] = "needs.changes.outputs.code == 'true'"
    if change == 'command': job['steps'][0]['run'] = "pytest ${{ github.event_name == 'merge_group' && 'smoke' || 'tests' }}"
    if change == 'input': job['steps'].insert(0, {'uses': 'actions/setup-python@v5', 'with': {'python-version': '${{ github.event_name }}'}})
    if change == 'step_env': job['steps'][0]['env'] = {'SUITE': '${{ github.event.merge_group.head_sha }}'}
    if change == 'shell': job['steps'][0]['run'] = 'pytest "$GITHUB_EVENT_NAME"'
    if change == 'matrix': job['strategy'] = {'matrix': '${{ fromJSON(needs.plan.outputs.matrix) }}'}
    if change == 'reusable': doc['jobs']['test'] = {'uses': './.github/workflows/test.yml'}
    if change == 'local': job['steps'] = [{'uses': './.github/actions/build'}]
    if change == 'queue_job': doc['jobs']['queue'] = {'if': "github.event_name == 'merge_group'", 'steps': [{'run': 'npm test'}]}
    if change == 'workflow_env': doc['env'] = {'MODE': '${{ github.event_name }}'}
    if change == 'parallel': job['steps'] = [{'parallel': [{'run': 'pytest'}]}]
    assert cr._opt98_full_suite(doc) == []
    findings, info = detect(workflows={WF: doc})
    assert not findings
    assert info['static_unknown'] == [f'{WF} ({reason})']
    assert info['status'] == 'partial'
    rendered = footer(info)
    assert f'queue scope not settled for {WF} ({reason})' in rendered


def test_package_scripts_and_third_party_actions_reach_the_check():
    for step in ({'run': 'npm test'}, {'uses': 'example/build@v1'}, {'run': './scripts/check.sh'},
                 {'run': 'pnpm install\npnpm test'}):
        doc = copy.deepcopy(DOC)
        doc['jobs']['test']['steps'] = [step]
        assert cr._opt98_full_suite(doc) == ['test'], step


@pytest.mark.parametrize('on', [['merge_group'], ['pull_request'], ['push', 'merge_group'], 'merge_group'])
def test_both_events_are_needed(on):
    doc = copy.deepcopy(DOC); doc['on'] = on
    assert cr._opt98_full_suite(doc) == []
    findings, info = detect(workflows={WF: doc}, branch_rules={'strict': False, 'merge_queue': False, 'complete': True})
    assert not findings and not info.get('static_unknown')


def test_queue_only_workflow_is_settled_silently():
    doc = copy.deepcopy(DOC); doc['on'] = {'merge_group': {'types': ['checks_requested']}}
    findings, info = detect(workflows={WF: doc})
    assert not findings and info['status'] == 'measured'


@pytest.mark.parametrize('doc', [{'on': 7, 'jobs': {}}, {'on': ['pull_request', 'merge_group'], 'jobs': {'test': {'steps': [{'run': 'npm test', 'env': 7}]}}},
                                 {'on': ['pull_request', 'merge_group'], 'jobs': []},
                                 {'on': ['pull_request', 'merge_group'], 'jobs': {'test': 'x'}}])
def test_malformed_workflow_does_not_abort_audit(doc):
    findings, info = detect(workflows={WF: doc})
    assert findings == []
    assert WF in info['unreadable_workflows']


# --- report -------------------------------------------------------------------

def test_prompt_carries_queue_guardrail_without_claiming_savings():
    findings, _ = detect()
    prompt = prompt_for(findings)
    assert 'Keep every required check on the merged result' in prompt
    assert 'No wall-clock or runner-minute saving is credited' in prompt
    assert 'git log' in prompt
    assert 'affected-only' in prompt
    assert 'Replace the require-up-to-date' not in prompt


def test_strict_prompt_names_the_merge_queue_fix():
    findings, _ = strict_case()
    prompt = prompt_for(findings)
    assert 'Replace the require-up-to-date branch rule with a merge queue' in prompt
    assert 'Keep every required check on the merged result' in prompt
    assert 'Workflow:' not in prompt


def test_data_sources_render_every_disclosure():
    rendered = footer({'branch_rule': 'branch rule not readable', 'merged_prs': 'merged PR search incomplete',
                       'unmeasured_workflows': ['a.yml'], 'unreadable_workflows': ['b.yml'],
                       'static_unknown': ['c.yml (local action)'], 'thin_samples': ['d.yml (thin)'],
                       'pr_runs_differ': ['e.yml (slow queue)']})
    for text in ('branch rule not readable', 'merged PR search incomplete',
                 'queue/PR timing not measured for a.yml', 'workflow configuration not readable for b.yml',
                 'queue scope not settled for c.yml (local action)', 'too few queue/PR runs for d.yml (thin)',
                 'PR runs differ from queue runs for e.yml (slow queue)'):
        assert text in rendered, text
    assert 'Merge queue' not in footer({'status': 'measured'})


def test_catalog_link_has_one_fragment_separator():
    import blocking_path as bp
    findings, _ = detect()
    assert bp._catalog_anchor('https://example.invalid/catalog', findings).count('#') == 1


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
    client = QueueClient([('ubuntu-latest', 300)] * 30,
                         'on: [pull_request, merge_group]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n')
    monkeypatch.setattr(cr, 'GhClient', lambda *a, **k: client)
    result = cr.collect({'repo': 'example/project', 'findings': [{'id': 'f1', 'pattern': 'OPT1', 'workflow_file': _CI}]}, 'o/r', max_runs=20, shallow_runs=20)
    findings = [f for f in result['findings'] if f.get('pattern') == 'OPT98']
    assert len(findings) == 1
    report = bp.render(result)
    assert 'No wall-clock or runner-minute saving is credited' in report
    assert 'branch rule not readable' in report
    assert report.count('## Repeated merge validation') == 1
    assert report.count('### OPT98:') == 1
    assert result['data_sources']['merge_queue']['status'] == 'partial'


def test_status_is_partial_when_only_the_queue_case_settles():
    findings, info = detect(branch_rules={})
    assert findings and info['status'] == 'partial'


def test_unknown_strict_value_is_not_readable_even_when_complete():
    findings, info = strict_case(branch_rules={'strict': None, 'merge_queue': False, 'complete': True})
    assert not findings and info['branch_rule'] == 'branch rule not readable'
    assert info['status'] == 'not_measured'


@pytest.mark.parametrize('on', [['pull_request', 'merge_group'], ['merge_group']])
def test_a_settled_queue_workflow_makes_an_unreadable_rule_partial(on):
    doc = copy.deepcopy(DOC); doc['on'] = on
    doc['jobs']['test']['steps'][0]['run'] = 'nx affected -t test'
    findings, info = detect(workflows={WF: doc}, branch_rules={})
    assert not findings and info['status'] == 'partial'


def test_yaml_dates_and_non_string_keys_do_not_make_a_workflow_unreadable():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['steps'][0]['with'] = {'since': dt.date(2026, 1, 1)}
    doc['jobs']['test']['steps'][0]['uses'] = 'example/check@v1'
    doc['jobs']['test']['strategy'] = {'matrix': {1: ['a'], 'name': ['b']}}
    assert cr._opt98_full_suite(doc) == ['test']


def test_evidence_says_package_scripts_were_not_read():
    findings, _ = detect()
    assert 'Package scripts and third-party actions were not read' in findings[0]['evidence']


def test_unread_rulesets_list_leaves_the_rule_incomplete():
    info = {}
    cr._fetch_required_checks(Client({'repos/example/project/branches/main/protection/required_status_checks': {'strict': True}}),
                              'example/project', branch_rules=info)
    assert info == {'strict': True, 'merge_queue': False, 'complete': False}


def test_findings_render_only_in_their_own_section():
    import blocking_path as bp
    findings, _ = detect()
    lines, _, _ = bp._also_noticed_block(findings, 'https://example.invalid/catalog')
    assert not any('OPT98' in line for line in lines)
    static = bp._render_static_only({'repo': 'example/project', 'findings': findings})
    assert '## Repeated merge validation' in static
