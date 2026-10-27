"""Synthetic checks for the maintainer-only hypothetical summary census."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def engine():
    path = ROOT / 'maintainers/ci-secure/scripts/required_check_census.py'
    spec = importlib.util.spec_from_file_location('required_check_census', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(script='test "${{ needs.suite.result }}" = success'):
    return {'repository': 'sample/project', 'revision': 'a' * 40, 'workflows': [{
        'path': '.github/workflows/ci.yml', 'sha': 'b' * 40,
        'text': '''on: pull_request
jobs:
  suite:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  verdict:
    needs: suite
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: >-
          ''' + script + '\n'}]}


def test_hypothetical_context_is_summary_only_and_real_engine_runs():
    report = engine().measure(snapshot(), ROOT)
    assert report['outcome'] == 'pass'
    assert [row['context'] for row in report['summaries']] == ['verdict']
    assert report['hypothetical'] is True
    assert 'suite' not in report['required_contexts']


def test_failed_summary_is_not_mislabeled_unmeasured():
    data = snapshot('echo "${{ needs.suite.result }}"')
    report = engine().measure(data, ROOT)
    assert report['outcome'] == 'fail'
    assert report['summaries'][0]['outcome'] == 'fail'


def test_unreadable_workflow_prevents_hypothetical_pass():
    data = snapshot()
    data['workflows'].append({'path': '.github/workflows/other.yml', 'sha': 'c' * 40,
                              'text': 'jobs: ['})
    report = engine().measure(data, ROOT)
    assert report['outcome'] == 'unmeasured'
    assert report['coverage_errors']


def test_rank_counts_refusals_and_compare_records_every_transition():
    census = engine()
    rows = [
        {'repository': 'sample/one', 'outcome': 'unmeasured',
         'summaries': [{'outcome': 'unmeasured', 'reason': 'unsupported shell control flow'}]},
        {'repository': 'sample/two', 'outcome': 'unmeasured',
         'summaries': [{'outcome': 'unmeasured', 'reason': 'unsupported shell control flow'}]},
        {'repository': 'sample/three', 'outcome': 'pass', 'summaries': []},
    ]
    ranked = census.summarize(rows)
    assert ranked['unmeasured_reasons'][0] == {'reason': 'unsupported shell control flow', 'count': 2}
    after = [dict(row, outcome='pass') for row in rows]
    comparison = census.compare(rows, after)
    assert comparison['transitions'] == {'unmeasured->pass': 2, 'pass->pass': 1}
    assert len(comparison['requires_manual_review']) == 2


def test_capture_rejects_private_repository_before_reading_workflows():
    calls = []
    def fetch(endpoint):
        calls.append(endpoint)
        return {'private': True}
    import pytest
    with pytest.raises(ValueError, match='public'):
        engine().capture('sample/project', fetch)
    assert calls == ['repos/sample/project']


def test_recorded_tag_resolution_reaches_the_engine_without_network():
    data = snapshot()
    cf = engine()._engine(ROOT)
    action = cf._verdict_assertions().ALLS_GREEN
    ref = action.split('@')[0] + '@v1.3.0'
    data['workflows'][0]['text'] = data['workflows'][0]['text'].split('      - run: >-')[0] + (
        '      - uses: ' + ref + '\n        with:\n          jobs: ${{ toJSON(needs) }}\n')
    data['action_resolutions'] = {ref: action.split('@')[1]}
    assert engine().measure(data, ROOT)['outcome'] == 'pass'


def test_comparison_audits_summary_pass_hidden_by_another_failing_check():
    before = [{'repository': 'sample/project', 'revision': 'a' * 40, 'outcome': 'fail',
               'summaries': [{'path': 'ci.yml', 'job': 'gate', 'context': 'gate',
                              'outcome': 'unmeasured'}]}]
    after = [{'repository': 'sample/project', 'revision': 'a' * 40, 'outcome': 'fail',
              'summaries': [{'path': 'ci.yml', 'job': 'gate', 'context': 'gate',
                             'outcome': 'pass'}]}]
    report = engine().compare(before, after)
    assert report['summary_transitions'] == {'unmeasured->pass': 1}
    assert len(report['summary_requires_manual_review']) == 1


@pytest.mark.parametrize('symlink_kind', ['snapshots', 'snapshot_file', 'report'])
def test_capture_destinations_cannot_follow_symlinks_into_source(tmp_path, monkeypatch, symlink_kind):
    import sys
    import json
    census = engine()
    source, data = tmp_path / 'source', tmp_path / 'data'
    source.mkdir()
    data.mkdir()
    monkeypatch.setattr(census, 'ROOT', source)
    repositories = tmp_path / 'repositories.json'
    repositories.write_text(json.dumps(['sample/project']))
    monkeypatch.setattr(census, 'capture', lambda repo: snapshot())
    monkeypatch.setattr(census, 'measure', lambda item, root: {
        'repository': 'sample/project', 'outcome': 'not_applicable', 'summaries': []})
    if symlink_kind == 'snapshots':
        (data / 'snapshots').symlink_to(source, target_is_directory=True)
    elif symlink_kind == 'snapshot_file':
        (data / 'snapshots').mkdir()
        (data / 'snapshots/sample__project.json').symlink_to(source / 'generated.json')
    else:
        (data / 'report.json').symlink_to(source / 'generated.json')
    monkeypatch.setattr(sys, 'argv', ['census', '--repositories', str(repositories), '--data-dir', str(data)])
    with pytest.raises(ValueError, match='source checkout|symlink|data directory'):
        census.main()
    assert not list(source.iterdir())


def test_shared_summary_name_records_each_job_and_is_ambiguous():
    data = snapshot()
    other = snapshot('echo "${{ needs.suite.result }}"')['workflows'][0]
    data['workflows'].append(dict(other, path='.github/workflows/other.yml'))
    report = engine().measure(data, ROOT)
    rows = {row['path']: row for row in report['summaries']}
    assert all(row['ambiguous'] for row in rows.values())
    assert rows['.github/workflows/ci.yml']['assertion'] == 'pass'
    assert rows['.github/workflows/other.yml']['assertion'] == 'fail'


def test_comparison_audits_newly_passing_job_masked_by_shared_name():
    def row(assertion):
        return [{'repository': 'sample/project', 'revision': 'a' * 40, 'outcome': 'pass',
                 'summaries': [{'path': 'ci.yml', 'job': 'gate', 'context': 'gate', 'outcome': 'pass',
                                'ambiguous': True, 'assertion': 'pass'},
                               {'path': 'other.yml', 'job': 'gate', 'context': 'gate', 'outcome': 'pass',
                                'ambiguous': True, 'assertion': assertion}]}]
    report = engine().compare(row('unmeasured'), row('pass'))
    assert [item['after']['path'] for item in report['summary_requires_manual_review']] == ['other.yml']
