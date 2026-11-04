"""N01: missing save paths, all successful sampled executions, no modeled credit."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import blocking_path as bp
import collect_runs as cr
import scan
from test_blocking_path import _doc_one_pole

WARNING = ('Path Validation Error: Path(s) specified in the action for caching '
           'do(es) not exist, hence no cache is being saved.')
LOG = '2026-11-04T00:04:01Z ##[warning]' + WARNING
STEP = {'name': 'Post Cache Bazel', 'conclusion': 'success',
        'started_at': '2026-11-04T00:04:00Z', 'completed_at': '2026-11-04T00:04:03Z'}
FACT = {'step': 'Cache Bazel', 'path': '~/.cache/bazel', 'likely_directory': None,
        'not_read': []}


def sample(logs, conclusions=None, fact=None):
    jobs = [dict(id=i+1, name='build', conclusion=c, steps=[copy.deepcopy(STEP)],
                 html_url=f'https://github.com/o/r/actions/runs/{i+1}/job/{i+1}')
            for i,c in enumerate(conclusions or ['success'] * len(logs))]
    class Client:
        pass
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cr, '_fetch_job_log', lambda client, repo, job: logs[job['id']-1])
        return cr._cache_path_sample(Client(), 'o/r', jobs, [fact or FACT])


def leaf(proof):
    return bp._parse_log(LOG, cache_path_sample=proof)


@pytest.mark.parametrize('cause', [None, 'bazel-disk-cache'])
def test_all_successful_save_attempts_admit(cause):
    f = dict(FACT, likely_directory=cause)
    result = leaf(sample([LOG, LOG], fact=f))
    assert result['fix_key'] == 'cache-effective-path-mismatch'
    assert result['magnitude'] is None
    assert not result.get('savings')
    if cause:
        assert cause in result['config_fact']


@pytest.mark.parametrize('logs,conclusions', [
    ([LOG, 'no warning'], None),
    ([LOG, LOG], ['failure', 'cancelled']),
    (['no warning', 'no warning'], None),
    ([LOG, ''], None),
])
def test_withhold_incomplete_or_nonuniversal_evidence(logs, conclusions):
    assert leaf(sample(logs, conclusions)) is None


def test_warning_outside_save_post_step_withheld():
    assert leaf(sample([LOG.replace('00:04:01', '00:03:01')] * 2)) is None


def test_single_log_or_bazel_counters_never_admit():
    assert bp._parse_log(LOG) is None
    assert bp._parse_log('INFO: 80 processes: 80 internal.') is None


def facts(tmp_path, rc, path='~/.cache/bazel', runner='ubuntu-latest', command='bazel build //...'):
    (tmp_path / '.bazelrc').write_text(rc)
    doc = {'jobs': {'build': {'runs-on': runner, 'steps': [
        {'name': 'Cache Bazel', 'uses': 'actions/cache@v4', 'with': {'path': path}},
        {'run': command}]}}}
    return scan._cache_effective_paths(tmp_path, [('ci.yml', doc, '')])['ci.yml']['build']


def test_static_disjointness_only_names_cause(tmp_path):
    f = facts(tmp_path, 'build --disk_cache=bazel-disk-cache\n')[0]
    assert f['likely_directory'] == 'bazel-disk-cache'
    assert leaf(sample(['no warning'] * 2, fact=f)) is None
    assert 'bazel-disk-cache' in leaf(sample([LOG] * 2, fact=f))['config_fact']


def test_scoped_line_is_not_read(tmp_path):
    f = facts(tmp_path, 'common:ci-windows --repository_cache=D:/bazel\n')[0]
    assert f['likely_directory'] is None
    assert 'not read' in ' '.join(f['not_read'])
    result = leaf(sample([LOG] * 2, fact=f))
    assert 'not read' in result['config_fact']


@pytest.mark.parametrize('path,rc,runner', [
    ('~/.cache/bazel', 'build --disk_cache=$HOME/.cache/bazel', 'ubuntu-latest'),
    ('D:/cache', 'build --disk_cache=d:/cache', 'windows-latest'),
    ('${{ runner.temp }}/bazel', 'build --disk_cache=${{ runner.temp }}/bazel', 'ubuntu-latest'),
])
def test_equivalent_paths_do_not_name_mismatch(tmp_path, path, rc, runner):
    assert facts(tmp_path, rc, path, runner)[0]['likely_directory'] is None


def test_literal_flag_and_unknown_expression(tmp_path):
    f = facts(tmp_path, '', command='bazel build --disk_cache=/tmp/bazel //...')[0]
    assert f['likely_directory'] == '/tmp/bazel'
    f = facts(tmp_path, 'build --disk_cache=${CACHE_DIR}\n')[0]
    assert f['likely_directory'] is None
    assert 'not read' in ' '.join(f['not_read'])


def test_render_uncredited_and_demoted():
    doc = _doc_one_pole()
    pole = doc['pr_critical_path']['poles'][0]
    pole['cache_path_sample'] = sample([LOG] * 2)
    md = bp.render(doc, {'pipeline': LOG}, {}, {}, '2026-11-04')
    assert 'OPT101' in md
    assert 'no savings credited' in md.lower()
    _, crown, secondary = bp._derive_pole_leaf(pole, 'pipeline', {'pipeline': LOG})
    assert crown is None and secondary['fix_key'] == 'cache-effective-path-mismatch'


def test_no_sizing_credit_or_overlap():
    assert 'OPT101' not in cr._SIZING
    proof = sample([LOG] * 2)
    result = leaf(proof)
    assert result['magnitude'] is None
    assert result['fix_key'] not in bp._CACHE_LEAF_KEYS


def test_report_handoff_verifier(tmp_path):
    from test_blocking_path import _load_verify_report
    vr = _load_verify_report()
    doc = _doc_one_pole()
    doc['pr_critical_path']['poles'][0]['cache_path_sample'] = sample([LOG] * 2)
    md = bp.render(doc, {'pipeline': LOG}, {}, {}, '2026-11-04')
    for check in (vr.check_rca_hands_off_never_prescribes(md),
                  vr.check_fences_balanced(md), vr.check_no_typographic_dashes(md)):
        assert check.ok, check


@pytest.mark.parametrize('short_warns', [True, False])
def test_collection_includes_successful_runs_below_drill_floor(tmp_path, short_warns):
    from test_opt96_cache_seeding import _LogClient
    wf = '.github/workflows/ci.yml'
    jobs = []
    logs = {}
    for jid, seconds in [(1, 300), (2, 300), (3, 10)]:
        step = dict(STEP)
        if jid == 3:
            step.update(started_at='2026-11-04T00:00:04Z',
                        completed_at='2026-11-04T00:00:07Z')
        jobs.append(dict(id=jid, name='build', conclusion='success', status='completed',
                         started_at='2026-11-04T00:00:00Z',
                         completed_at=('2026-11-04T00:05:00Z' if seconds == 300
                                       else '2026-11-04T00:00:10Z'),
                         steps=[step], html_url=f'https://github.com/o/r/actions/runs/{jid}/job/{jid}'))
        logs[jid] = (LOG if jid != 3 else
                     LOG.replace('00:04:01', '00:00:05') if short_warns else 'no warning')
    pole = dict(check='build', job='build', workflow_file=wf, p50_s=300)
    cr._persist_pole_logs(_LogClient(logs), 'o/r', [pole], {wf: [[j] for j in jobs]},
                         tmp_path, cache_effective_paths={wf: {'build': [FACT]}})
    proof = pole['cache_path_sample']
    assert bool(proof['admitted']) == short_warns
    if short_warns:
        assert proof['admitted'][0]['runs'] == 3


def test_other_post_step_warning_does_not_admit(monkeypatch):
    job = dict(id=1, conclusion='success', steps=[dict(STEP, name='Post Other Cache')])
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: LOG)
    assert not cr._cache_path_sample(None, 'o/r', [job], [FACT])['admitted']


def test_n01_does_not_replace_existing_dominant_cause():
    from test_blocking_path import _IMPORT_BOUND_LOG
    doc = _doc_one_pole()
    pole = doc['pr_critical_path']['poles'][0]
    pole['cache_path_sample'] = sample([LOG] * 2)
    _, crown, secondary = bp._derive_pole_leaf(
        pole, 'pipeline', {'pipeline': _IMPORT_BOUND_LOG + '\n' + LOG},
        doc['test_runner_isolation'])
    assert crown['fix_key'] == 'vitest-isolate-pool'
    assert secondary['fix_key'] == 'cache-effective-path-mismatch'


def test_echoed_flag_is_not_a_cause(tmp_path):
    assert facts(tmp_path, '', command='echo bazel build --disk_cache=/tmp/example')[0]['likely_directory'] is None
