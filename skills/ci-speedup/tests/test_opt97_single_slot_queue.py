"""Queue/capacity inference, with synthetic intervals and no external calls."""
import base64
import copy
import importlib.util
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import blocking_path as bp
import collect_runs as cr
import pytest

WF = '.github/workflows/ci.yml'
DOC = {'concurrency': {'group': 'shared-device', 'cancel-in-progress': False},
       'jobs': {'test': {'runs-on': ['self-hosted', 'device'],
                         'steps': [{'run': 'adb devices'}]}}}


def runs(n=20, overloaded=False):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def iso(s):
        return (t + timedelta(seconds=s)).isoformat()
    return [[{'id': i + 1, 'run_id': i + 1, 'name': 'test',
              'created_at': iso(i * 10), 'started_at': iso(i * 10 + 60 + i * 30),
              'completed_at': iso(i * 10 + 60 + i * 30 + 1000 + (i * 100 if overloaded else 0)),
              'labels': ['self-hosted', 'device'], 'conclusion': 'success',
              'html_url': f'https://github.com/example/project/actions/runs/{i + 1}/job/{i + 1}'}]
            for i in range(n)]


def rivals_from(by_wf, docs=None):
    """A competition measurement whose dense job list is exactly `by_wf`."""
    rows = [r for wf, rr in by_wf.items() for run in rr
            for r in cr._opt97_competitors(wf, run, (docs or {}).get(wf, DOC))]
    calls = []

    def competition(labels, group, lo, hi):
        calls.append((labels, group, lo, hi))
        return rows, None
    competition.calls = calls
    return competition


def detect(sample=None, doc=None, **kwargs):
    sample = runs() if sample is None else sample
    doc = DOC if doc is None else doc
    kwargs.setdefault('competition', rivals_from(
        kwargs.get('all_jobs_by_wf') or {WF: sample},
        {**(kwargs.get('workflow_docs') or {}), WF: doc}))
    kwargs.setdefault('baseline_queue_s', 10)
    kwargs.setdefault('merge_gating_jobs', {'test'})
    return cr._detect_opt97_single_slot_queue(WF, sample, 0, doc, **kwargs)


def test_queue_tracks_inflight_names_global_resource_and_measured_excess():
    [f] = detect()
    assert f['pattern'] == 'OPT97'
    assert 'shared-device' in f['evidence']
    assert f['single_slot_queue']['queue_rank_correlation'] == 1
    assert f['single_slot_queue']['runtime_rank_correlation'] == 0
    assert f['wall_clock_p50_s'] == 335
    assert f['runner_min_saving'] is None

    sample = runs()
    doc = copy.deepcopy(DOC)
    doc['jobs']['other'] = {'runs-on': 'ubuntu-latest'}
    for row in sample:
        sibling = copy.deepcopy(row[0])
        sibling['name'] = 'other'
        sibling['labels'] = ['ubuntu-latest']
        sibling['completed_at'] = (datetime.fromisoformat(row[0]['completed_at']) + timedelta(seconds=1000)).isoformat()
        row.append(sibling)
    [bounded] = [f for f in detect(sample, doc) if f["affected_jobs"] == ["test"]]
    assert bounded['wall_clock_p50_s'] == 0


def test_overload_keeps_generic_queue_but_not_single_slot():
    assert detect(runs(overloaded=True)) == []
    assert cr._detect_opt43_queue_time(WF, runs(overloaded=True), 0, True)


def test_fourteen_samples_discloses_too_few():
    notes = []
    assert detect(runs(14), notes=notes) == []
    assert notes[0]['reason'] == 'too few samples'
    assert notes[0]['samples'] == 14


def test_merge_replaces_only_the_same_workflow_and_job_opt43():
    """OPT43 is the only pattern merged in production (OPT44 has no detector);
    the finding is built by the real OPT43 detector, not hand-shaped."""
    [f] = detect()
    [same] = cr._detect_opt43_queue_time(WF, runs(), 90, True)
    [elsewhere] = cr._detect_opt43_queue_time('.github/workflows/other.yml', runs(), 91, True)
    assert same['affected_jobs'] == ['test'] and same['pattern'] == 'OPT43'
    findings = [same, elsewhere]
    cr._merge_opt97_findings(findings, [f], DOC)
    assert findings == [elsewhere, f]
    assert f['related_patterns'] == ['OPT43']


def test_merge_keeps_a_multi_job_finding_that_covers_other_jobs():
    [f] = detect()
    wider = {'id': 'f100', 'pattern': 'OPT44', 'workflow_file': WF,
             'affected_jobs': ['test', 'lint']}
    narrow = {'id': 'f101', 'pattern': 'OPT44', 'workflow_file': WF,
              'affected_jobs': ['test']}
    findings = [wider, narrow]
    cr._merge_opt97_findings(findings, [f], DOC)
    assert findings == [wider, f]
    assert f['related_patterns'] == ['OPT44']


@pytest.mark.parametrize('change', ['short_queue', 'missing_time', 'skipped'])
def test_rejects_unmeasured_or_unrelated_waits(change):
    sample = runs()
    for i, [job] in enumerate(sample):
        if change == 'short_queue':
            job['created_at'] = job['started_at']
        elif change == 'missing_time':
            job.pop('created_at')
        else:
            job['conclusion'] = 'skipped'
    assert detect(sample) == []


def test_long_queue_without_rank_correlation_does_not_fire():
    """Queue thresholds met, but queue order is unrelated to in-flight order."""
    sample = runs()
    order = [7, 13, 2, 18, 9, 0, 15, 5, 11, 19, 3, 16, 8, 1, 12, 6, 17, 4, 14, 10]
    for i, [job] in enumerate(sample):
        created = datetime.fromisoformat(job['created_at'])
        start = created + timedelta(seconds=60 + order[i] * 30)
        job['started_at'] = start.isoformat()
        job['completed_at'] = (start + timedelta(seconds=1000)).isoformat()
    queues = [60 + o * 30 for o in order]
    assert statistics.median(queues) >= 60 and cr._percentile(queues, 90) >= 300
    assert abs(cr._opt97_rank_correlation(queues, list(range(20)))) < .6
    assert detect(sample) == []


def test_runner_labels_are_separate_populations():
    sample = runs()
    for i, [job] in enumerate(sample):
        job['labels'] = ['self-hosted', f'pool-{i % 2}']
    notes = []
    assert detect(sample, notes=notes) == []
    assert len(notes) == 2


def test_upstream_runtime_never_counted_as_resource_queue():
    sample = runs()
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['needs'] = ['build']
    doc['jobs']['build'] = {'runs-on': 'ubuntu-latest'}
    for row in sample:
        job = row[0]
        row.append({'name': 'build', 'completed_at': job['started_at']})
    assert detect(sample, doc) == []


def test_unknown_gate_has_no_wall_clock_credit():
    [f] = detect(merge_gating_jobs=None)
    assert f['wall_clock_p50_s'] == 0
    assert 'not established' in f['size_note']


def test_prestart_axis_and_renderer_include_single_slot():
    [f] = detect()
    assert cr._build_covered_job_savings([f]) == {}
    assert bp._is_wait_finding(f)


def test_runner_inventory_and_device_are_hints_not_utilization_claims():
    doc = copy.deepcopy(DOC)
    doc.pop('concurrency')
    [f] = detect(doc=doc, runner_inventory=[{'labels': [{'name': 'device'}]}])
    assert 'one repo-level runner (org runners not visible)' in f['evidence']
    assert 'adb' in f['evidence']
    assert 'mostly idle' not in f['evidence']
    [unknown] = detect(doc=doc)
    assert unknown['single_slot_queue']['runner_inventory'] == 'unknown'


def test_scoped_concurrency_is_not_named_as_global():
    doc = copy.deepcopy(DOC)
    doc['concurrency']['group'] = 'ci-${{ github.ref }}'
    [f] = detect(doc=doc)
    assert 'global concurrency group' not in f['evidence']


def test_cross_workflow_same_label_competitors_are_counted():
    own = runs()
    competitors = copy.deepcopy(own)
    for [j] in competitors:
        j['id'] += 100
        j['run_id'] += 100
    [f] = detect(all_jobs_by_wf={WF: own, 'other.yml': competitors},
                 workflow_docs={'other.yml': DOC})
    assert f['single_slot_queue']['inflight_counts'][-1] == 38


def test_collect_dispatches_and_discloses_insufficient_samples(monkeypatch):
    seen = []

    def detector(wf, sample, idx, doc, **kwargs):
        seen.append((wf, kwargs))
        kwargs['notes'].append({'job': 'test', 'samples': 8, 'reason': 'too few samples'})
        return []
    doc, _, _ = pipeline(monkeypatch, ['self-hosted', 'device'], detector=detector)
    assert seen, 'collect must dispatch OPT97 over sampled workflows'
    assert all(kw['all_jobs_by_wf'] for _, kw in seen)
    assert all(callable(kw['competition']) for _, kw in seen)
    assert doc['opt97_notes']
    assert 'too few samples' in '\n'.join(bp._data_sources_footer(doc, 'o/r'))
    assert not any('OPT97' in row['detectors']
                   for row in doc['data_sources']['detectors_skipped']
                   if 'too few samples' in row['reason'])


@pytest.mark.parametrize('queues,expected', [
    ([60 + i * 30 for i in range(20)], True),
    ([60 + i * 5 for i in range(20)], False),
    ([30] * 11 + [400] * 9, False),
    ([630 - i * 30 for i in range(20)], False),
    ([60 + i * 12 for i in range(20)], False),                      # P90 between 200 and 300
    ([60 + 30 * p for p in (0, 19, 1, 3, 7, 5, 15, 4, 12, 2, 6, 9, 14, 8, 10, 16, 13, 18, 11, 17)],
     False),                                                        # rank correlation 0.54
])
def test_independent_queue_thresholds_and_correlation(queues, expected):
    sample = runs()
    for i, [job] in enumerate(sample):
        created = datetime.fromisoformat(job['created_at'])
        start = created + timedelta(seconds=queues[i])
        job['started_at'] = start.isoformat()
        job['completed_at'] = (start + timedelta(seconds=1000)).isoformat()
    assert bool(detect(sample)) is expected


def test_job_local_services_are_not_shared_resource_hints():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['services'] = {'db': {'env': {'POSTGRES_DB': 'integration'}, 'ports': ['5432:5432']}}
    doc['jobs']['other'] = copy.deepcopy(doc['jobs']['test'])
    [f] = detect(doc=doc)
    assert 'shared fixed database' not in f['evidence']
    assert 'shared fixed port' not in f['evidence']
    doc['jobs']['other']['needs'] = ['test']
    [f] = detect(doc=doc)
    assert 'shared fixed database' not in f['evidence']


@pytest.mark.parametrize('response,expected', [
    ({'total_count': 1, 'runners': [{'labels': [{'name': 'device'}]}]}, [{'labels': [{'name': 'device'}]}]),
    ({'total_count': 101, 'runners': [{'labels': [{'name': 'device'}]}]}, None),
    (None, None),
])
def test_runner_inventory_never_treats_partial_page_as_singleton(response, expected):
    class Client:
        def json(self, endpoint, allow_missing=False):
            assert endpoint == 'repos/example/project/actions/runners?per_page=100'
            assert allow_missing
            return response
    assert cr._opt97_runner_inventory(Client(), 'example/project') == expected


def test_named_job_credits_yaml_gate_identity():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['name'] = 'Integration tests'
    sample = runs()
    for [job] in sample:
        job['name'] = 'Integration tests'
    [f] = detect(sample, doc, merge_gating_jobs={'test': {'basis': 'required'}})
    assert f['wall_clock_p50_s'] == 335


@pytest.mark.parametrize('malformed', ['strategy', 'steps'])
def test_unfamiliar_workflow_shapes_do_not_crash_audit(malformed):
    doc = copy.deepcopy(DOC)
    if malformed == 'strategy':
        doc['jobs']['other'] = {'strategy': None, 'name': 'other'}
    else:
        doc['jobs']['test']['steps'] = ['unexpected', None]
    assert detect(doc=doc)


def test_collect_detector_exception_discloses_coverage_gap(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('generic malformed shape')
    doc, _, _ = pipeline(monkeypatch, ['self-hosted', 'device'], detector=broken)
    assert any('OPT97' in str(row) and 'ValueError' in str(row)
               for row in doc['data_sources']['detectors_skipped'])


# --- Global-group definition (string form, default cancel, scoped keys) ------

@pytest.mark.parametrize('concurrency', [
    'shared-device',                                   # string shorthand
    {'group': 'shared-device'},                        # cancel-in-progress omitted = false
    {'group': '${{ github.workflow }}', 'cancel-in-progress': False},
])
def test_global_group_forms_are_named(concurrency):
    doc = copy.deepcopy(DOC)
    doc['concurrency'] = concurrency
    [f] = detect(doc=doc)
    assert 'global concurrency group' in f['evidence']


@pytest.mark.parametrize('concurrency', [
    {'group': 'ci-${{ github.ref }}'},
    {'group': 'pr-${{ github.head_ref }}'},
    {'group': 'run-${{ github.run_id }}'},
    {'group': 'pr-${{ github.event.pull_request.number }}'},
    {'group': 'n-${{ github.event.number }}'},
    {'group': 'sha-${{ github.sha }}'},
    {'group': 'shared-device', 'cancel-in-progress': True},
    {'group': 'shared-device', 'cancel-in-progress': "${{ github.event_name == 'pull_request' }}"},
])
def test_scoped_or_cancelling_groups_are_not_global(concurrency):
    doc = copy.deepcopy(DOC)
    doc['concurrency'] = concurrency
    [f] = detect(doc=doc)
    assert 'global concurrency group' not in f['evidence']


def test_job_level_concurrency_overrides_workflow_level():
    scoped = copy.deepcopy(DOC)
    scoped['jobs']['test']['concurrency'] = {'group': 'ci-${{ github.ref }}'}
    [f] = detect(doc=scoped)
    assert 'global concurrency group' not in f['evidence']
    job_only = copy.deepcopy(DOC)
    job_only.pop('concurrency')
    job_only['jobs']['test']['concurrency'] = 'device-lock'
    [f] = detect(doc=job_only)
    assert 'global concurrency group `device-lock`' in f['evidence']


def test_workflow_level_group_wait_is_measured_from_run_creation():
    """A run waiting on a workflow-level group creates its jobs only when the
    group frees: job created_at lands a second before started_at, so the wait
    is visible only from the run's created_at."""
    sample = runs()
    for [job] in sample:
        job['_run_created_at'] = job['created_at']
        job['run_attempt'] = 1
        job['created_at'] = (datetime.fromisoformat(job['started_at'])
                             - timedelta(seconds=1)).isoformat()
    [f] = detect(sample)
    assert f['single_slot_queue']['queue_p50_s'] == 345
    # A job-level group waits after job creation: no run-level anchor.
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['concurrency'] = doc.pop('concurrency')
    assert detect(sample, doc) == []
    # A re-run keeps the original run created_at: never charge that gap as queue.
    for [job] in sample:
        job['run_attempt'] = 2
    assert detect(sample) == []


# --- Hosted pools: account limits explain the queue, not a slot ---------------

HOSTED_DOC = {'jobs': {'test': {'runs-on': 'ubuntu-22.04', 'steps': [{'run': 'make test'}]}}}


def hosted_runs(n=20):
    sample = runs(n)
    for [job] in sample:
        job['labels'] = ['ubuntu-22.04']
    return sample


def test_hosted_pool_without_static_hint_is_left_to_opt43():
    sample = hosted_runs()
    competition = rivals_from({WF: sample}, {WF: HOSTED_DOC})
    assert detect(sample, HOSTED_DOC, competition=competition) == []
    assert competition.calls == []   # a population that cannot fire costs no calls
    [opt43] = cr._detect_opt43_queue_time(WF, sample, 0, True)
    findings = [opt43]
    cr._merge_opt97_findings(findings, [], HOSTED_DOC)
    assert findings == [opt43]


@pytest.mark.parametrize('hint', ['group', 'device'])
def test_hosted_pool_with_static_hint_can_fire(hint):
    doc = copy.deepcopy(HOSTED_DOC)
    if hint == 'group':
        doc['concurrency'] = 'device-lab'
    else:
        doc['jobs']['test']['steps'] = [{'run': 'adb devices'}]
    [f] = detect(hosted_runs(), doc)
    assert f['pattern'] == 'OPT97'


@pytest.mark.parametrize('text', ['./loadbalancer --check', 'tools/my-adb-helper',
                                  'adb-tools install', 'run sadb'])
def test_device_step_match_needs_token_boundaries(text):
    doc = copy.deepcopy(HOSTED_DOC)
    doc['jobs']['test']['steps'] = [{'run': text}]
    assert detect(hosted_runs(), doc) == []


# --- Dense competition: all runs in the window, not the newest successes -----

def test_without_a_competition_measurement_correlation_never_fires():
    notes = []
    assert cr._detect_opt97_single_slot_queue(
        WF, runs(), 0, DOC, baseline_queue_s=10, notes=notes) == []
    [note] = notes
    assert note['reason'] == 'competition not measured'
    assert note['job'] == 'test'


def test_failed_or_capped_competition_is_check_this_never_a_finding():
    def failed(labels, group, lo, hi):
        return None, 'the run list could not be read'
    notes = []
    assert detect(competition=failed, notes=notes) == []
    [note] = notes
    assert note['reason'] == 'competition not measured'
    assert note['detail'] == 'the run list could not be read'


def _spread(sample, gap=5000):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, [job] in enumerate(sample):
        created = t + timedelta(seconds=i * gap)
        job['created_at'] = created.isoformat()
        job['started_at'] = (created + timedelta(seconds=60 + i * 30)).isoformat()
        job['completed_at'] = (created + timedelta(seconds=1060 + i * 30)).isoformat()
    return sample


def test_dense_competition_decides_when_the_sparse_sample_has_no_overlap():
    """The sampled runs never overlap each other, so a sparse count is all
    zeros; the dense list shows queue tracking competing jobs."""
    sample = _spread(runs())
    competition = rivals_from({WF: sample})
    assert detect(sample, competition=competition) == []
    [(labels, group, lo, hi)] = competition.calls
    created = [datetime.fromisoformat(j['created_at']) for [j] in sample]
    assert labels == ('device', 'self-hosted') and group == 'shared-device'
    assert (lo, hi) == (min(created) - timedelta(hours=3), max(created))
    rivals = []
    for i, at in enumerate(created):
        for k in range(i):
            rivals.append([{'id': 10000 + i * 100 + k, 'name': 'test',
                            'labels': ['self-hosted', 'device'], 'conclusion': 'success',
                            'created_at': (at - timedelta(seconds=5)).isoformat(),
                            'started_at': (at - timedelta(seconds=5)).isoformat(),
                            'completed_at': (at + timedelta(seconds=5)).isoformat()}])
    [f] = detect(sample, competition=rivals_from({WF: sample, 'other.yml': rivals},
                                                 {'other.yml': {}}))
    assert f['single_slot_queue']['inflight_counts'] == list(range(20))


T = datetime(2026, 1, 1, 6, tzinfo=timezone.utc)


def _run(i, path=WF):
    return {'id': i, 'path': path, 'created_at': T.isoformat(), 'run_attempt': 1}


def _jobs_body(i, labels=('self-hosted', 'device')):
    return {'total_count': 1, 'jobs': [{'id': 50000 + i, 'run_id': i, 'name': 'test',
                                        'labels': list(labels), 'conclusion': 'success',
                                        'created_at': T.isoformat(),
                                        'started_at': T.isoformat(),
                                        'completed_at': (T + timedelta(seconds=9)).isoformat()}]}


class RunsClient:
    def __init__(self, pages, jobs=None):
        self.pages, self.jobs, self.endpoints = pages, jobs or {}, []

    def json(self, endpoint, allow_missing=False):
        assert allow_missing
        self.endpoints.append(endpoint)
        if '/jobs' in endpoint:
            return self.jobs.get(int(endpoint.split('/runs/')[1].split('/')[0]))
        page = int(endpoint.rsplit('page=', 1)[1])
        return self.pages[page - 1] if page <= len(self.pages) else {'workflow_runs': []}


DOCS = {WF: DOC,
        '.github/workflows/docs.yml': {'jobs': {'build': {'runs-on': 'ubuntu-latest'}}},
        '.github/workflows/matrix.yml': {'jobs': {'t': {'runs-on': '${{ matrix.os }}'}}},
        '.github/workflows/lock.yml': {'jobs': {'d': {'runs-on': 'ubuntu-latest',
                                                      'concurrency': 'Shared-Device'}}},
        '.github/workflows/cased.yml': {'jobs': {'e': {'runs-on': ['Self-Hosted', 'Device']}}},
        '.github/workflows/call.yml': {'jobs': {'docs': {'runs-on': 'ubuntu-latest'},
                                                'c': {'uses': './.github/workflows/r.yml'}}}}


def dense_call(client, docs=DOCS, known=None, group=None):
    stats = {'run_pages': 0, 'job_lists': 0, 'populations': 0}
    out = cr._opt97_dense_competition(
        client, 'example/project', ('device', 'self-hosted'), group,
        T - timedelta(hours=1), T, workflow_docs=docs, known_runs=known or {},
        stats=stats, cache={})
    return out, stats


def test_dense_fetch_reads_every_run_in_the_window_and_only_sharing_workflows():
    page = {'workflow_runs': [_run(1), _run(2, '.github/workflows/docs.yml'),
                              _run(3, '.github/workflows/unknown.yml'),
                              _run(4, '.github/workflows/matrix.yml'),
                              _run(5, '.github/workflows/lock.yml'), _run(6),
                              _run(7, '.github/workflows/call.yml'),
                              _run(8, '.github/workflows/cased.yml')]}
    client = RunsClient([page], {i: _jobs_body(i) for i in (1, 3, 4, 5, 7, 8)})
    (rivals, why), stats = dense_call(client, known={'6': _jobs_body(6)['jobs']},
                                      group='shared-device')
    assert why is None
    assert client.endpoints[0] == ('repos/example/project/actions/runs?created='
                                   '2026-01-01T05:00:00Z..2026-01-01T06:00:00Z'
                                   '&per_page=100&page=1')
    fetched = sorted(e for e in client.endpoints if '/jobs' in e)
    assert fetched == [f'repos/example/project/actions/runs/{i}/jobs?per_page=100'
                       for i in (1, 3, 4, 5, 7, 8)]
    assert stats == {'run_pages': 1, 'job_lists': 6, 'populations': 1}
    assert sorted(r['id'] for r in rivals) == [50001, 50003, 50004, 50005, 50006, 50007, 50008]


def test_dense_fetch_without_a_shared_group_skips_other_static_pools():
    page = {'workflow_runs': [_run(5, '.github/workflows/lock.yml')]}
    client = RunsClient([page], {5: _jobs_body(5)})
    (rivals, why), stats = dense_call(client)
    assert (rivals, why) == ([], None) and stats['job_lists'] == 0


def test_dense_fetch_page_cap_is_three_full_pages():
    full = {'workflow_runs': [_run(i, '.github/workflows/docs.yml') for i in range(100)]}
    (rivals, why), stats = dense_call(RunsClient([full, full, full]))
    assert rivals is None and why == 'more than 300 runs in the window'
    assert stats['run_pages'] == 3
    tail = {'workflow_runs': [_run(999, '.github/workflows/docs.yml')]}
    (rivals, why), stats = dense_call(RunsClient([full, full, tail]))
    assert (rivals, why) == ([], None) and stats['run_pages'] == 3


def test_dense_fetch_short_page_ends_the_read():
    page = {'workflow_runs': [_run(i, '.github/workflows/docs.yml') for i in range(99)]}
    (rivals, why), stats = dense_call(RunsClient([page, page]))
    assert (rivals, why) == ([], None) and stats['run_pages'] == 1


def test_dense_fetch_job_list_cap():
    page = {'workflow_runs': [_run(i) for i in range(61)]}
    jobs = {i: _jobs_body(i) for i in range(61)}
    (rivals, why), stats = dense_call(RunsClient([page], jobs))
    assert rivals is None and why == 'more than 60 runs could share the runners'
    assert stats['job_lists'] == 0
    page = {'workflow_runs': [_run(i) for i in range(60)]}
    (rivals, why), stats = dense_call(RunsClient([page], jobs))
    assert why is None and len(rivals) == 60 and stats['job_lists'] == 60


@pytest.mark.parametrize('pages,jobs,reason', [
    ([None], {}, 'the run list could not be read'),
    ([{'workflow_runs': [_run(1)]}], {}, 'a job list could not be read'),
    ([{'workflow_runs': [_run(1)]}], {1: {'total_count': 101, 'jobs': _jobs_body(1)['jobs']}},
     'a run has more than 100 jobs'),
])
def test_dense_fetch_failure_is_reported_never_partial(pages, jobs, reason):
    (rivals, why), _ = dense_call(RunsClient(pages, jobs))
    assert rivals is None and why == reason


def rival(created, end, labels=('device', 'self-hosted'), group=None):
    return {'labels': tuple(labels), 'group': group,
            'created': T + timedelta(seconds=created),
            'end': None if end is None else T + timedelta(seconds=end)}


def test_inflight_counts_strict_overlap_by_label_set_or_group():
    labels = ('device', 'self-hosted')
    row = {'created': T + timedelta(seconds=100), 'group': None}
    rivals = [rival(50, 150), rival(50, None), rival(100, 150), rival(50, 100),
              rival(50, 150, labels=('other',)), rival(50, 150, labels=())]
    assert cr._opt97_inflight(row, labels, rivals) == 2
    grouped = {'created': T + timedelta(seconds=100), 'group': 'lock'}
    rivals = [rival(50, 150, labels=(), group='lock'), rival(50, 150, labels=()),
              rival(50, 150, labels=('x',), group='other')]
    assert cr._opt97_inflight(grouped, (), rivals) == 1


def test_group_keys_are_case_insensitive_and_expressions_stay_per_workflow():
    upper = copy.deepcopy(DOC)
    upper['concurrency'] = {'group': 'Shared-Device'}
    [r] = cr._opt97_competitors('other.yml', runs(1)[0], upper)
    [own] = cr._opt97_samples(WF, runs(1), DOC)
    assert r['group'] == own['group'] == 'shared-device'
    expr = copy.deepcopy(DOC)
    expr['concurrency'] = {'group': '${{ github.workflow }}'}
    [a] = cr._opt97_competitors('a.yml', runs(1)[0], expr)
    [b] = cr._opt97_competitors('b.yml', runs(1)[0], expr)
    assert a['group'] and b['group'] and a['group'] != b['group']


def test_competitor_waiting_on_upstream_counts_only_once_started():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['needs'] = ['build']
    doc['jobs']['build'] = {'runs-on': 'ubuntu-latest'}
    [run] = runs(1)
    run.append({'id': 900, 'name': 'build', 'labels': ['ubuntu-latest'], 'conclusion': None,
                'created_at': run[0]['created_at'], 'completed_at': None})
    started = cr._opt97_competitors(WF, run, doc)
    assert [(r['id'], r['created']) for r in started if r['id'] == 1] == [
        (1, datetime.fromisoformat(run[0]['started_at']))]
    run[0]['started_at'] = None
    assert [r for r in cr._opt97_competitors(WF, run, doc) if r['id'] == 1] == []


def test_rank_correlation_uses_average_ranks_and_zero_for_constants():
    assert cr._opt97_rank_correlation([1, 2, 3, 4], [5, 5, 5, 5]) == 0
    assert cr._opt97_rank_correlation([1, 1, 2, 2], [1, 2, 3, 4]) == pytest.approx(
        0.894427, abs=1e-6)


# --- Unresolved jobs and note volume ----------------------------------------

@pytest.mark.parametrize('jobs', [
    {'call': {'uses': './.github/workflows/reusable.yml'}},
    {'a': {'name': 'test', 'runs-on': 'x'}, 'b': {'name': 'test', 'runs-on': 'y'}},
])
def test_unresolved_jobs_are_counted_per_workflow(jobs):
    sample = runs()
    if 'call' in jobs:
        for [j] in sample:
            j['name'] = 'call / test'
    notes = []
    assert detect(sample, {'jobs': jobs}, notes=notes) == []
    assert notes == [{'workflow_file': WF, 'reason': 'unresolved jobs',
                      'unresolved': 20, 'jobs': 20}]


def test_thin_population_is_noted_only_when_its_queue_could_fire():
    short = runs(5)
    for [j] in short:
        j['created_at'] = j['started_at']
    notes = []
    assert detect(short, notes=notes) == []
    assert notes == []
    detect(runs(14), notes=notes)
    assert [(n['reason'], n['samples']) for n in notes] == [('too few samples', 14)]


def test_thin_hosted_population_without_hint_is_not_noted():
    notes = []
    assert detect(hosted_runs(14), HOSTED_DOC, notes=notes) == []
    assert notes == []


def test_zero_length_execution_is_not_a_sample():
    sample = runs(15)
    sample[0][0]['completed_at'] = sample[0][0]['started_at']
    notes = []
    assert detect(sample, notes=notes) == []
    assert [n['samples'] for n in notes] == [14]


def test_capacity_rows_are_capped_in_the_report():
    notes = [{'workflow_file': WF, 'job': f'job-{i}', 'labels': [], 'samples': 3,
              'reason': 'too few samples'} for i in range(12)]
    notes.append({'workflow_file': WF, 'reason': 'unresolved jobs', 'unresolved': 4, 'jobs': 9})
    notes.append({'workflow_file': WF, 'job': 'test', 'labels': [], 'samples': 20,
                  'reason': 'competition not measured',
                  'detail': 'the run list could not be read'})
    doc = {'opt97_notes': notes,
           'opt97_competition_calls': {'run_pages': 2, 'job_lists': 7, 'populations': 1}}
    lines = '\n'.join(bp._data_sources_footer(doc, 'o/r')).splitlines()
    held = next(ln for ln in lines if 'single-slot capacity: held back' in ln)
    assert held.count('too few samples') == 5 and '+8 more in findings JSON' in held
    check = next(ln for ln in lines if 'single-slot capacity: check this' in ln)
    assert 'competition not measured (the run list could not be read)' in check
    calls = next(ln for ln in lines if 'single-slot capacity: competition fetch' in ln)
    assert '2 run-list pages' in calls and '7 job lists' in calls


# --- Sizing ------------------------------------------------------------------

def test_baseline_is_p10_of_other_sampled_jobs_not_the_evaluated_population():
    own = runs()
    lint = runs()
    for i, [j] in enumerate(lint):
        j['name'], j['labels'] = 'lint', ['ubuntu-latest']
        start = datetime.fromisoformat(j['created_at']) + timedelta(seconds=20 + 10 * i)
        j['started_at'] = start.isoformat()
        j['completed_at'] = (start + timedelta(seconds=30)).isoformat()
    lint_doc = {'jobs': {'lint': {'runs-on': 'ubuntu-latest'}}}
    [f] = detect(own, all_jobs_by_wf={WF: own, 'lint.yml': lint},
                 workflow_docs={'lint.yml': lint_doc}, baseline_queue_s=None)
    expected = cr._percentile([20 + 10 * i for i in range(20)], 10)
    assert f['single_slot_queue']['baseline_queue_s'] == expected
    assert f['wall_clock_p50_s'] == round(345 - expected, 1)
    [alone] = detect(baseline_queue_s=None)
    assert alone['wall_clock_p50_s'] == 0
    assert 'baseline unavailable' in alone['size_note']


def _with_sibling(**sibling):
    sample = runs()
    doc = copy.deepcopy(DOC)
    doc['jobs']['other'] = {'runs-on': 'ubuntu-latest'}
    for row in sample:
        row.append({'id': row[0]['id'] + 500, 'name': 'other', 'labels': ['ubuntu-latest'],
                    'created_at': row[0]['created_at'], **sibling})
    return sample, doc


def test_missing_sibling_completion_gets_no_credit():
    sample, doc = _with_sibling(conclusion=None, completed_at=None)
    [f] = detect(sample, doc)
    assert f['wall_clock_p50_s'] == 0


def test_skipped_sibling_never_bounds_the_credit():
    sample, doc = _with_sibling(conclusion='skipped',
                                completed_at='2026-01-02T00:00:00+00:00')
    [f] = detect(sample, doc)
    assert f['wall_clock_p50_s'] == 335


def test_unknown_upstream_completion_excludes_the_sample_without_crashing():
    sample = runs()
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['needs'] = 'build'
    doc['jobs']['build'] = {'runs-on': 'ubuntu-latest'}
    for row in sample:
        row.append({'id': row[0]['id'] + 500, 'name': 'build', 'labels': ['ubuntu-latest'],
                    'conclusion': 'success', 'created_at': row[0]['created_at'],
                    'completed_at': None})
    assert detect(sample, doc) == []


def test_execution_weakly_rising_with_contention_is_not_flat():
    sample = runs()
    order = (8, 3, 6, 5, 15, 16, 2, 12, 0, 1, 13, 10, 19, 9, 14, 11, 4, 17, 18, 7)
    for i, [job] in enumerate(sample):
        start = datetime.fromisoformat(job['started_at'])
        job['completed_at'] = (start + timedelta(seconds=1000 + 10 * order[i])).isoformat()
    assert .3 <= cr._opt97_rank_correlation(list(order), list(range(20))) < .4
    assert detect(sample) == []


def test_self_hosted_label_match_ignores_case():
    sample = runs()
    for [job] in sample:
        job['labels'] = ['Self-Hosted', 'device']
    doc = copy.deepcopy(DOC)
    doc.pop('concurrency')
    doc['jobs']['test']['steps'] = [{'run': 'make'}]
    [f] = detect(sample, doc)
    assert f['single_slot_queue']['hints'] == []


def test_execution_falling_with_contention_is_not_flat():
    sample = runs()
    for i, [job] in enumerate(sample):
        start = datetime.fromisoformat(job['started_at'])
        job['completed_at'] = (start + timedelta(seconds=3000 - i * 100)).isoformat()
    assert detect(sample) == []


def test_label_on_two_repo_runners_is_not_a_single_runner_hint():
    doc = copy.deepcopy(DOC)
    doc.pop('concurrency')
    [f] = detect(doc=doc, runner_inventory=[{'labels': [{'name': 'device'}]},
                                            {'labels': ['device']}])
    assert 'repo-level runner' not in f['evidence']


def test_finding_carries_no_unconsumed_queue_cause():
    [f] = detect()
    assert 'queue_cause' not in f


# --- Pipeline: the real collect() against a fake GitHub ----------------------

def _fakes():
    spec = importlib.util.spec_from_file_location(
        'opt97_pipeline_fakes', Path(__file__).with_name('test_disclosure_reaches_the_artifact.py'))
    fakes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fakes
    spec.loader.exec_module(fakes)
    return fakes


def _z(seconds):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def pipeline(monkeypatch, labels, dense='ok', yaml_text=None, detector=None):
    fakes = _fakes()
    gate = fakes._GATE
    workflow = yaml_text if yaml_text is not None else yaml.safe_dump(
        {'name': 'test', 'on': {'pull_request': {}},
         'jobs': {'test': {'runs-on': labels, 'steps': [{'run': 'make test'}]}}})

    class Client(fakes._TwoWorkflowClient):
        endpoints = []

        def __init__(self):
            super().__init__()
            self._shas = [f's{i}' for i in range(20)]

        def _runs(self, wf_id):
            if wf_id != 1:
                return super()._runs(wf_id)
            return [{'id': 100 + i, 'event': 'pull_request', 'head_sha': f's{i}',
                     'path': gate, 'created_at': _z(i * 10), 'run_started_at': _z(i * 10),
                     'updated_at': _z(i * 40 + 1060), 'run_attempt': 1,
                     'conclusion': 'success', 'status': 'completed'} for i in range(20)]

        def _jobs(self, run_id):
            if run_id >= 200:
                return super()._jobs(run_id)
            i = run_id - 100
            return [{'id': 7000 + i, 'run_id': run_id, 'name': 'test', 'labels': labels,
                     'created_at': _z(i * 10), 'started_at': _z(i * 40 + 60),
                     'completed_at': _z(i * 40 + 1060), 'conclusion': 'success',
                     'status': 'completed', 'runner_name': 'r1', 'steps': [],
                     'html_url': f'https://github.com/o/r/actions/runs/{run_id}/job/{7000 + i}'}]

        def json(self, endpoint, allow_missing=False):
            Client.endpoints.append(endpoint)
            if endpoint.startswith('repos/o/r/actions/runs?'):
                self.queries += 1
                return None if dense == 'fail' else {'workflow_runs': self._runs(1)}
            if endpoint == f'repos/o/r/contents/{gate}':
                self.queries += 1
                return {'encoding': 'base64',
                        'content': base64.b64encode(workflow.encode()).decode()}
            return super().json(endpoint, allow_missing)

    monkeypatch.setattr(cr, 'GhClient', Client)
    if detector is not None:
        monkeypatch.setattr(cr, '_detect_opt97_single_slot_queue', detector)
    doc = fakes._doc()
    doc['workflow_job_graph'] = {gate: {'test': {'name': 'test', 'needs': []}}}
    cr.collect(doc, 'o/r', max_runs=20, shallow_runs=20)
    mine = [f for f in doc['findings'] if f.get('workflow_file') == gate]
    return doc, mine, Client.endpoints


def test_pipeline_hosted_pool_keeps_opt43_and_makes_no_extra_calls(monkeypatch):
    doc, mine, endpoints = pipeline(monkeypatch, ['ubuntu-22.04'])
    assert [f.get('pattern') for f in mine if f.get('pattern') in {'OPT43', 'OPT97'}] == ['OPT43']
    assert not any(e.startswith('repos/o/r/actions/runs?') for e in endpoints)
    assert not any('actions/runners' in e for e in endpoints)


def test_pipeline_self_hosted_pool_fires_on_dense_counts_and_discloses_calls(monkeypatch):
    doc, mine, endpoints = pipeline(monkeypatch, ['self-hosted', 'device'])
    assert [f.get('pattern') for f in mine if f.get('pattern') in {'OPT43', 'OPT97'}] == ['OPT97']
    assert 'repos/o/r/actions/runners?per_page=100' in endpoints
    assert any(e.startswith('repos/o/r/actions/runs?created=') for e in endpoints)
    assert doc['opt97_competition_calls']['run_pages'] == 1
    footer = '\n'.join(bp._data_sources_footer(doc, 'o/r'))
    assert 'single-slot capacity: competition fetch' in footer


def test_pipeline_failed_dense_fetch_is_check_this_and_keeps_opt43(monkeypatch):
    doc, mine, _ = pipeline(monkeypatch, ['self-hosted', 'device'], dense='fail')
    assert [f.get('pattern') for f in mine if f.get('pattern') in {'OPT43', 'OPT97'}] == ['OPT43']
    [note] = [n for n in doc['opt97_notes'] if n['reason'] == 'competition not measured']
    assert note['detail'] == 'the run list could not be read'
    footer = '\n'.join(bp._data_sources_footer(doc, 'o/r'))
    assert 'competition not measured (the run list could not be read)' in footer


def test_pipeline_unparseable_yaml_discloses_opt97_skip(monkeypatch):
    doc, mine, _ = pipeline(monkeypatch, ['self-hosted', 'device'],
                            yaml_text='jobs: [unbalanced\n')
    entry = next(e for e in doc['data_sources']['detectors_skipped']
                 if e['workflow'].endswith('test.yml'))
    assert 'OPT97' in entry['detectors'] and 'could not be parsed' in entry['reason']
    assert not [n for n in doc.get('opt97_notes') or [] if n['workflow_file'].endswith('test.yml')]


def test_dense_job_list_reads_count_as_job_fetches():
    """A token that dies during the dense read must be counted in the collection
    banner's "N of M job fetches failed": each uncached job-list read is one fetch."""
    class CountingClient(RunsClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.recorded = []

        def record_job_fetch(self, failed):
            self.recorded.append(failed)

    page = {'workflow_runs': [_run(1), _run(6)]}
    client = CountingClient([page], {1: _jobs_body(1)})
    dense_call(client)
    assert client.recorded == [False, True]
