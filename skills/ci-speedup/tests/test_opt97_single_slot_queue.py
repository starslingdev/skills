"""Queue/capacity inference, with synthetic intervals and no external calls."""
import copy
from datetime import datetime, timedelta, timezone

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


def detect(sample=None, doc=None, **kwargs):
    return cr._detect_opt97_single_slot_queue(
        WF, runs() if sample is None else sample, 0, DOC if doc is None else doc,
        baseline_queue_s=10, merge_gating_jobs={'test'}, **kwargs)


def test_queue_tracks_inflight_names_global_resource_and_measured_excess():
    [f] = detect()
    assert f['pattern'] == 'OPT97'
    assert 'shared-device' in f['evidence']
    assert f['single_slot_queue']['queue_rank_correlation'] == 1
    assert f['single_slot_queue']['runtime_rank_correlation'] == 0
    assert f['wall_clock_p50_s'] == 335
    assert f['runner_min_saving'] is None


def test_overload_keeps_generic_queue_but_not_single_slot():
    assert detect(runs(overloaded=True)) == []
    assert cr._detect_opt43_queue_time(WF, runs(overloaded=True), 0, True)


def test_fourteen_samples_discloses_too_few():
    notes = []
    assert detect(runs(14), notes=notes) == []
    assert notes[0]['reason'] == 'too few samples'
    assert notes[0]['samples'] == 14


def test_merge_opt44_and_generic_queue_only_on_same_job():
    [f] = detect()
    existing = [{'id': 'f99', 'pattern': p, 'workflow_file': WF,
                 'affected_jobs': ['test']} for p in ('OPT44', 'OPT43')]
    other = {'id': 'f100', 'pattern': 'OPT44', 'workflow_file': WF,
             'affected_jobs': ['lint']}
    existing.append(other)
    cr._merge_opt97_findings(existing, [f], DOC)
    assert existing == [other, f]
    assert f['related_patterns'] == ['OPT43', 'OPT44']


@pytest.mark.parametrize('change', ['short_queue', 'no_correlation', 'missing_time', 'skipped'])
def test_rejects_unmeasured_or_unrelated_waits(change):
    sample = runs()
    for i, [job] in enumerate(sample):
        if change == 'short_queue':
            job['created_at'] = job['started_at']
        elif change == 'no_correlation':
            job['created_at'] = job['started_at']
        elif change == 'missing_time':
            job.pop('created_at')
        else:
            job['conclusion'] = 'skipped'
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
    [f] = cr._detect_opt97_single_slot_queue(WF, runs(), 0, DOC, baseline_queue_s=10)
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
    assert 'one registered runner' in f['evidence']
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


def test_collect_dispatches_and_discloses_insufficient_samples(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        'opt97_collect_fakes', Path(__file__).with_name('test_disclosure_reaches_the_artifact.py'))
    fakes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fakes
    spec.loader.exec_module(fakes)
    monkeypatch.setattr(cr, 'GhClient', fakes._TwoWorkflowClient)
    seen = []
    def detector(wf, sample, idx, doc, **kwargs):
        seen.append((wf, kwargs))
        kwargs['notes'].append({'job': 'test', 'samples': 8, 'reason': 'too few samples'})
        return []
    monkeypatch.setattr(cr, '_detect_opt97_single_slot_queue', detector, raising=False)
    doc = fakes._doc()
    cr.collect(doc, 'o/r', max_runs=8, shallow_runs=8)
    assert seen, 'collect must dispatch OPT97 over sampled workflows'
    assert all(kw['all_jobs_by_wf'] for _, kw in seen)
    assert any('OPT97' in str(row) and 'too few samples' in str(row)
               for row in doc['data_sources']['detectors_skipped'])


@pytest.mark.parametrize('queues,expected', [
    ([60 + i * 30 for i in range(20)], True),
    ([60 + i * 5 for i in range(20)], False),
    ([30] * 11 + [400] * 9, False),
    ([630 - i * 30 for i in range(20)], False),
])
def test_independent_queue_thresholds_and_correlation(queues, expected):
    sample = runs()
    for i, [job] in enumerate(sample):
        created = datetime.fromisoformat(job['created_at'])
        start = created + timedelta(seconds=queues[i])
        job['started_at'] = start.isoformat()
        job['completed_at'] = (start + timedelta(seconds=1000)).isoformat()
    assert bool(detect(sample)) is expected


def test_shared_database_hint_requires_parallel_fixed_resource():
    doc = copy.deepcopy(DOC)
    doc['jobs']['test']['services'] = {'db': {'env': {'POSTGRES_DB': 'integration'}, 'ports': ['5432:5432']}}
    doc['jobs']['other'] = copy.deepcopy(doc['jobs']['test'])
    [f] = detect(doc=doc)
    assert 'shared fixed database integration' in f['evidence']
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
    [f] = cr._detect_opt97_single_slot_queue(
        WF, sample, 0, doc, baseline_queue_s=10,
        merge_gating_jobs={'test': {'basis': 'required'}})
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
    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        'opt97_error_fakes', Path(__file__).with_name('test_disclosure_reaches_the_artifact.py'))
    fakes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fakes
    spec.loader.exec_module(fakes)
    monkeypatch.setattr(cr, 'GhClient', fakes._TwoWorkflowClient)
    def broken(*args, **kwargs):
        raise ValueError('generic malformed shape')
    monkeypatch.setattr(cr, '_detect_opt97_single_slot_queue', broken, raising=False)
    doc = fakes._doc()
    cr.collect(doc, 'o/r', max_runs=8, shallow_runs=8)
    assert any('OPT97' in str(row) and 'ValueError' in str(row)
               for row in doc['data_sources']['detectors_skipped'])
