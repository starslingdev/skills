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
# The real emitted form: actions/cache logs the toolkit error via `core.info`
# with a literal `[warning]` prefix, stamped with seven fractional digits.
LOG = '2026-11-04T00:04:01.4567891Z [warning]' + WARNING
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


@pytest.mark.parametrize('path,rc,runner,command', [
    ('~/.cache/bazel', '', 'ubuntu-latest', 'bazel build --disk_cache=$HOME/.cache/bazel //...'),
    ('D:/cache', 'build --disk_cache=d:/cache', 'windows-latest', 'bazel build //...'),
    ('${{ runner.temp }}/bazel', '', 'ubuntu-latest',
     'bazel build --disk_cache=${{ runner.temp }}/bazel //...'),
])
def test_equivalent_paths_do_not_name_mismatch(tmp_path, path, rc, runner, command):
    f = facts(tmp_path, rc, path, runner, command)[0]
    assert f['likely_directory'] is None
    assert f['not_read'] == [], 'recognized as equal, not merely unreadable'


def test_literal_home_variable_in_cache_path_is_not_read(tmp_path):
    # actions/cache does not expand `$HOME`; only a shell `run:` line does.
    f = facts(tmp_path, '', path='$HOME/.cache/bazel',
              command='bazel build --disk_cache=$HOME/.cache/bazel //...')[0]
    assert 'cache path not read' in f['not_read']


def test_real_actions_cache_log_shape_admits(monkeypatch):
    # Verbatim shape of a real successful job (actions/cache@v6, 2026-10-25): the
    # API reports the sub-second save post-step with started_at == completed_at.
    log = '\n'.join([
        '2026-10-25T12:00:52.6609432Z Post job cleanup.',
        '2026-10-25T12:00:52.7376284Z [warning]' + WARNING,
        '2026-10-25T12:00:52.7491466Z Post job cleanup.',
        '2026-10-25T12:00:52.8335401Z Post job cleanup.'])
    step = dict(name='Post Cache pnpm store', conclusion='success',
                started_at='2026-10-25T12:00:52Z', completed_at='2026-10-25T12:00:52Z')
    jobs = [dict(id=i, name='build', conclusion='success', steps=[dict(step)]) for i in (1, 2)]
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: log)
    fact = dict(FACT, step='Cache pnpm store', path='~/.pnpm-store')
    proof = cr._cache_path_sample(None, 'o/r', jobs, [fact])
    assert proof['admitted'] and proof['admitted'][0]['evidence'][0] == '[warning]' + WARNING


def _jobs_with(*step_lists):
    return [dict(id=i, name='build', conclusion='success', steps=s)
            for i, s in enumerate(step_lists, 1)]


@pytest.mark.parametrize('second', [
    [],                                                       # no step data: withhold
    [dict(STEP), dict(STEP)],                                 # ambiguous duplicate: withhold
    [{k: v for k, v in STEP.items() if k != 'completed_at'}],  # no timing: withhold
])
def test_unprovable_successful_attempt_withholds(monkeypatch, second):
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: LOG)
    jobs = _jobs_with([dict(STEP)], second)
    assert not cr._cache_path_sample(None, 'o/r', jobs, [FACT])['admitted']


def test_skipped_save_is_not_an_attempt(monkeypatch):
    monkeypatch.setattr(cr, '_fetch_job_log',
                        lambda client, repo, job: LOG if job['id'] == 1 else 'no warning')
    jobs = _jobs_with([dict(STEP)], [dict(STEP, conclusion='skipped')])
    admitted = cr._cache_path_sample(None, 'o/r', jobs, [FACT])['admitted']
    assert admitted and admitted[0]['runs'] == 1


def test_near_miss_warning_text_never_admits():
    near = LOG.replace('[warning]', '[warning]note: ')
    assert leaf(sample([near] * 2)) is None


@pytest.mark.parametrize('rc,path,runner,command', [
    ('build --disk_cache=~/.cache/bazel/disk', '~/.cache/bazel', 'ubuntu-latest',
     'bazel build //...'),                                     # overlap, not disjoint
    ('', '~/.cache/bazel', '${{ matrix.os }}',
     'bazel build --disk_cache=/tmp/bazel //...'),              # unknown runner OS
    ('try-import %workspace%/user.bazelrc\nbuild --disk_cache=/x\n', '~/.cache/bazel',
     'ubuntu-latest', 'bazel build //...'),                    # imported config unread
])
def test_unprovable_static_context_names_no_directory(tmp_path, rc, path, runner, command):
    assert facts(tmp_path, rc, path, runner, command)[0]['likely_directory'] is None


def test_opt101_agent_prompt_renders_with_location():
    doc = _doc_one_pole()
    doc['pr_critical_path']['poles'][0]['cache_path_sample'] = sample([LOG] * 2)
    md = bp.render(doc, {'pipeline': LOG}, {}, {}, '2026-11-04')
    assert bp._FIX_META['cache-effective-path-mismatch']['deliver'] in md
    assert 'Pattern: OPT101' in md and 'pipeline.yml' in md


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
    assert 'leaf-crown fix_key=cache-effective-path-mismatch' not in md
    _, crown, _secondary = bp._derive_pole_leaf(pole, 'pipeline', {'pipeline': LOG})
    assert crown is None


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
    # OPT101 is observation-only, so the unexplained dominant work stays a coverage
    # gap that phase 4a fills; the verifier still rejects an unfilled one.
    md = bp.render(doc, {'pipeline': LOG}, {}, {}, '2026-11-04',
                   analyses={'pipeline': _ANALYSIS})
    assert 'OPT101' in md
    bare = bp.render(doc, {'pipeline': LOG}, {}, {}, '2026-11-04')
    assert not vr.check_rca_hands_off_never_prescribes(bare).ok, 'OPT101 must not mask a dead end'
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


def test_collection_never_downloads_the_drilled_log_twice(tmp_path):
    from test_opt96_cache_seeding import _LogClient
    wf = '.github/workflows/ci.yml'

    def run(effective):
        jobs = [dict(id=jid, name='build', conclusion='success', status='completed',
                     started_at='2026-11-04T00:00:00Z', completed_at='2026-11-04T00:05:00Z',
                     steps=[dict(STEP)], html_url=f'https://github.com/o/r/actions/runs/{jid}/job/{jid}')
                for jid in (1, 2, 3)]
        client = _LogClient({1: LOG, 2: LOG, 3: LOG})
        pole = dict(check='build', job='build', workflow_file=wf, p50_s=300)
        cr._persist_pole_logs(client, 'o/r', [pole], {wf: [[j] for j in jobs]}, tmp_path,
                              cache_effective_paths=effective)
        return client.calls, pole

    base, _ = run(None)
    calls, pole = run({wf: {'build': [FACT]}})
    assert pole['cache_path_sample']['admitted'][0]['runs'] == 3
    assert len(calls) - len(base) == 2, 'only the two undrilled successful jobs are fetched'


def test_other_post_step_warning_does_not_admit(monkeypatch):
    job = dict(id=1, conclusion='success', steps=[dict(STEP, name='Post Other Cache')])
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: LOG)
    assert not cr._cache_path_sample(None, 'o/r', [job], [FACT])['admitted']


def test_n01_does_not_replace_existing_dominant_cause():
    from test_blocking_path import _IMPORT_BOUND_LOG
    doc = _doc_one_pole()
    pole = doc['pr_critical_path']['poles'][0]
    pole['cache_path_sample'] = sample([LOG] * 2)
    log = _IMPORT_BOUND_LOG + '\n' + LOG
    _, crown, _secondary = bp._derive_pole_leaf(
        pole, 'pipeline', {'pipeline': log}, doc['test_runner_isolation'])
    assert crown['fix_key'] == 'vitest-isolate-pool'
    md = bp.render(doc, {'pipeline': log}, {}, {}, '2026-11-04')
    assert 'leaf-crown fix_key=vitest-isolate-pool' in md and 'OPT101' in md


def test_echoed_flag_is_not_a_cause(tmp_path):
    assert facts(tmp_path, '', command='echo bazel build --disk_cache=/tmp/example')[0]['likely_directory'] is None


def test_subsecond_save_with_seven_digit_stamp_admits(monkeypatch):
    # GitHub truncates step times to whole seconds (a sub-second save has
    # started_at == completed_at) and stamps log lines with seven fractional digits.
    log = '2026-11-04T00:04:03.2345678Z ##[warning]' + WARNING
    step = dict(STEP, started_at='2026-11-04T00:04:03Z', completed_at='2026-11-04T00:04:03Z')
    jobs = [dict(id=i, name='build', conclusion='success', steps=[dict(step)]) for i in (1, 2)]
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: log)
    proof = cr._cache_path_sample(None, 'o/r', jobs, [FACT])
    assert proof['admitted'] and proof['admitted'][0]['runs'] == 2


def test_adjacent_cache_post_steps_never_borrow_a_warning(monkeypatch):
    # Back-to-back post-steps share a truncated boundary second; only one warns.
    steps = [dict(name='Post Cache Bazel', conclusion='success',
                  started_at='2026-11-04T00:04:02Z', completed_at='2026-11-04T00:04:03Z'),
             dict(name='Post Cache Repo', conclusion='success',
                  started_at='2026-11-04T00:04:03Z', completed_at='2026-11-04T00:04:04Z')]
    repo = dict(FACT, step='Cache Repo', path='~/.cache/repo')
    jobs = [dict(id=i, name='build', conclusion='success', steps=copy.deepcopy(steps))
            for i in (1, 2)]
    one = '2026-11-04T00:04:03.1000000Z ##[warning]' + WARNING
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: one)
    admitted = cr._cache_path_sample(None, 'o/r', jobs, [FACT, repo])['admitted']
    assert 'Cache Repo' not in [a['step'] for a in admitted]
    both = one + '\n2026-11-04T00:04:03.6000000Z ##[warning]' + WARNING
    monkeypatch.setattr(cr, '_fetch_job_log', lambda *a: both)
    admitted = cr._cache_path_sample(None, 'o/r', jobs, [FACT, repo])['admitted']
    assert sorted(a['step'] for a in admitted) == ['Cache Bazel', 'Cache Repo']


def test_non_utf8_bazelrc_is_not_read_and_never_stops_the_scan(tmp_path):
    (tmp_path / '.bazelrc').write_bytes(b'build --disk_cache=/tmp/caf\xe9\n')
    doc = {'jobs': {'build': {'runs-on': 'ubuntu-latest', 'steps': [
        {'name': 'Cache Bazel', 'uses': 'actions/cache@v4', 'with': {'path': '~/.cache/bazel'}},
        {'run': 'bazel build //...'}]}}}
    f = scan._cache_effective_paths(tmp_path, [('ci.yml', doc, '')])['ci.yml']['build'][0]
    assert f['likely_directory'] is None
    assert '.bazelrc not read' in ' '.join(f['not_read'])


def test_quoted_flag_path_is_read_whole_or_not_at_all(tmp_path):
    f = facts(tmp_path, '', path='D:/cache', runner='windows-latest',
              command='bazel build --disk_cache="D:/Bazel Cache" //...')[0]
    assert f['likely_directory'] == 'D:/Bazel Cache'
    f = facts(tmp_path, '', path='D:/cache', runner='windows-latest',
              command='bazel build --disk_cache="D:/Bazel //...')[0]
    assert f['likely_directory'] is None


# An admitted proof built directly, so render tests do not depend on collection.
PROOF = {'admitted': [dict(FACT, runs=2, evidence=['[warning]' + WARNING])]}

_ANALYSIS = {'cause': 'SENTINEL-LLM-CAUSE slow compile.', 'breakdown': [['compile', '~30s']],
             'evidence': ['compiling 4000 targets'], 'prompt': 'SENTINEL-LLM-PROMPT compile.'}


def test_cache_observation_keeps_the_gap_fill_for_unexplained_work():
    doc = _doc_one_pole()
    control = bp.render(copy.deepcopy(doc), {'pipeline': LOG}, analyses={'pipeline': _ANALYSIS})
    assert 'SENTINEL-LLM-CAUSE' in control  # fixture sanity: a true gap fills without OPT101
    doc['pr_critical_path']['poles'][0]['cache_path_sample'] = copy.deepcopy(PROOF)
    md = bp.render(doc, {'pipeline': LOG}, analyses={'pipeline': _ANALYSIS})
    assert 'SENTINEL-LLM-CAUSE' in md and 'OPT101' in md
    assert bp._gap_poles(doc, {'pipeline': LOG}), 'OPT101 must not close the coverage gap'


def test_cache_observation_keeps_an_existing_secondary_observation():
    from test_blocking_path import _nx_offcategory_doc
    doc, logs = _nx_offcategory_doc()
    doc['pr_critical_path']['poles'][0]['cache_path_sample'] = copy.deepcopy(PROOF)
    out = bp.render(doc, logs)
    assert 'OPT101' in out
    assert '`eslint-no-cache`' in out, 'the demoted eslint observation must still render'
