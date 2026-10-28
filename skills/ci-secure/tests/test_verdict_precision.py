"""Synthetic coverage expansions and attempts to obtain a false verdict pass."""
import copy
import importlib.util
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('precision_assertions', ROOT / 'scripts/verdict_assertions.py')
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)


def suites(count=2):
    jobs = {'suite' + str(i): {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]}
            for i in range(count)}
    jobs['verdict'] = {'runs-on': 'ubuntu-latest', 'if': 'always()', 'needs': list(jobs),
                       'steps': [{'uses': va.ALLS_GREEN, 'with': {'jobs': '${{ toJSON(needs) }}'}}]}
    return jobs


def test_large_strict_aggregate_does_not_enumerate_exponentially():
    jobs = suites(18)
    start = time.monotonic()
    assert va.analyze(jobs, 'verdict')[0] == 'pass'
    assert time.monotonic() - start < 2


def test_large_permissive_aggregate_never_gets_strict_proof():
    jobs = suites(18)
    jobs['verdict']['steps'][0]['with']['allowed-skips'] = 'suite0'
    assert va.analyze(jobs, 'verdict')[0] != 'pass'
    jobs = suites(18)
    jobs['suite0']['steps'][0]['if'] = 'false'
    assert va.analyze(jobs, 'verdict')[0] != 'pass'


def test_reviewed_action_tag_resolves_once_and_keeps_original_data():
    jobs = suites()
    reference = va.ALLS_GREEN.split('@')[0] + '@v1.3.0'
    jobs['verdict']['steps'][0]['uses'] = reference
    original = copy.deepcopy(jobs)
    calls = []
    def resolve(ref):
        calls.append(ref)
        return va.ALLS_GREEN.split('@')[1]
    assert va.analyze(jobs, 'verdict', action_resolver=resolve)[0] == 'pass'
    assert calls == [reference]
    assert jobs == original


@pytest.mark.parametrize('resolved', [None, 'f' * 40, 'not-a-sha'])
def test_action_tag_unknown_or_unreviewed_commit_withholds(resolved):
    jobs = suites()
    jobs['verdict']['steps'][0]['uses'] = va.ALLS_GREEN.split('@')[0] + '@v1.3.0'
    assert va.analyze(jobs, 'verdict', action_resolver=lambda ref: resolved)[0] == 'unmeasured'


@pytest.mark.parametrize('filter', ['all(.[]; .result == "success")', 'all(.result == "success")'])
@pytest.mark.parametrize('indirect', [False, True])
def test_strict_jq_reads_only_complete_needs_json(filter, indirect):
    jobs = suites()
    step = {'run': "jq -e '" + filter + "' <<< '${{ toJSON(needs) }}'"}
    if indirect:
        step['run'] = "jq --exit-status '" + filter + "' <<< \"$RESULTS\""
        step['env'] = {'RESULTS': '${{ toJSON(needs) }}'}
    jobs['verdict']['steps'] = [step]
    assert va.analyze(jobs, 'verdict')[0] == 'pass'


@pytest.mark.parametrize('source', [
    "jq 'all(.[]; .result == \"success\")' <<< '${{ toJSON(needs) }}'",
    "jq -e 'all(.[]; .result != \"failure\")' <<< '${{ toJSON(needs) }}'",
    "jq -e 'all(.[]; .result == \"success\")' <<< '{}'",
    "jq -e 'all(.[]; .result == \"success\")' <<< '${{ toJSON(needs) }}' || true",
    "jq -e 'all(.[]; .result == \"success\")' <<< '${{ toJSON(needs) }}'; exit 0",
])
def test_jq_false_pass_hunt_refuses_nonfatal_partial_or_permissive_inputs(source):
    jobs = suites()
    jobs['verdict']['steps'] = [{'run': source}]
    assert va.analyze(jobs, 'verdict')[0] != 'pass'


def test_jq_environment_cannot_replace_tool_or_json():
    jobs = suites()
    step = {'run': 'jq -e \'all(.[]; .result == "success")\' <<< "$RESULTS"',
            'env': {'RESULTS': '${{ toJSON(needs) }}', 'PATH': './fake-tools'}}
    jobs['verdict']['steps'] = [step]
    assert va.analyze(jobs, 'verdict')[0] != 'pass'
    step['env'] = {'RESULTS': '{}'}
    assert va.analyze(jobs, 'verdict')[0] != 'pass'


def test_fact_routes_reviewed_action_resolution_through_real_verdict():
    spec = importlib.util.spec_from_file_location('precision_facts', ROOT / 'scripts/config_facts.py')
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    jobs = suites(18)
    jobs['verdict']['steps'][0]['uses'] = va.ALLS_GREEN.split('@')[0] + '@v1.3.0'
    doc = {'on': 'pull_request', 'jobs': jobs}
    result = cf._required_checks_skippable(
        [('ci.yml', doc)], 'sample/project', lambda repo: (['verdict'], 'test checks'),
        action_resolver=lambda ref: va.ALLS_GREEN.split('@')[1])
    assert result[0] == 'pass'


def test_network_resolver_fetches_only_modeled_action_tag(monkeypatch):
    from types import SimpleNamespace
    import json
    spec = importlib.util.spec_from_file_location('precision_network', ROOT / 'scripts/config_facts.py')
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    calls = []
    def api(path):
        calls.append(path)
        return json.dumps({'sha': va.ALLS_GREEN.split('@')[1]})
    monkeypatch.setattr(cf, '_gh_utils', lambda: SimpleNamespace(check_prereqs=lambda: True, run_gh_api=api))
    prefix = va.ALLS_GREEN.split('@')[0]
    assert cf._verdict_action_via_gh(prefix + '@release/v1') == va.ALLS_GREEN.split('@')[1]
    assert calls == ['repos/' + prefix + '/commits/release%2Fv1']
    assert cf._verdict_action_via_gh('sample/other-action@v1') is None
    assert len(calls) == 1


def test_jq_does_not_certify_a_suite_with_a_skipped_test_step():
    jobs = suites()
    jobs['suite0']['steps'][0]['if'] = 'false'
    jobs['verdict']['steps'] = [{'run': "jq -e 'all(.result == \"success\")' <<< '${{ toJSON(needs) }}'"}]
    assert va.analyze(jobs, 'verdict')[0] != 'pass'


def test_jq_does_not_treat_reserved_runner_environment_as_workflow_data():
    jobs = suites()
    jobs['verdict']['steps'] = [{
        'run': 'jq -e \'all(.result == "success")\' <<< "$RUNNER_RESULTS"',
        'env': {'RUNNER_RESULTS': '${{ toJSON(needs) }}'}}]
    assert va.analyze(jobs, 'verdict')[0] == 'unmeasured'


def test_inline_needs_json_with_outputs_cannot_certify_shell_quoting():
    jobs = suites()
    jobs['suite0']['outputs'] = {'message': "' || true; echo '"}
    jobs['verdict']['steps'] = [{'run': "jq -e 'all(.result == \"success\")' <<< '${{ toJSON(needs) }}'"}]
    assert va.analyze(jobs, 'verdict')[0] == 'unmeasured'
    jobs['verdict']['steps'] = [{
        'run': 'jq -e \'all(.result == "success")\' <<< "$RESULTS"',
        'env': {'RESULTS': '${{ toJSON(needs) }}'}}]
    assert va.analyze(jobs, 'verdict')[0] == 'pass'


def test_intermediate_inline_json_with_outputs_stays_unmeasured():
    jobs = suites(1)
    jobs['suite0']['outputs'] = {'message': "' || true; echo '"}
    jobs['middle'] = {'needs': ['suite0'], 'if': 'always()', 'runs-on': 'ubuntu-latest',
                      'steps': [{'run': "jq -e 'all(.result == \"success\")' <<< '${{ toJSON(needs) }}'"}]}
    jobs['verdict']['needs'] = ['middle']
    assert va.analyze(jobs, 'verdict')[0] == 'unmeasured'


@pytest.mark.parametrize('override', ['BASH_FUNC_jq%%', 'Path', 'Bash_Env', 'bashopts'])
def test_jq_holds_exported_functions_and_case_variant_shell_overrides(override):
    jobs = suites(1)
    jobs['verdict']['steps'] = [{
        'shell': 'bash',
        'run': 'jq -e \'all(.result == "success")\' <<< "$RESULTS"',
        'env': {'RESULTS': '${{ toJSON(needs) }}', override: '() { return 0; }'}}]
    assert va.analyze(jobs, 'verdict')[0] == 'unmeasured'


@pytest.mark.parametrize('count', [2, 18])
@pytest.mark.parametrize('placement', ['child', 'group', 'nested', 'background', 'top-background', 'unreadable'])
def test_grouped_suite_steps_cannot_certify_execution(count, placement):
    # A parallel group hides its children from a flat `steps` read. A skipped
    # or backgrounded test there still lets the job report success.
    jobs = suites(count)
    group = {'parallel': [{'run': 'pytest'}, {'run': 'ruff check'}]}
    if placement == 'child':
        group['parallel'][0]['if'] = 'false'
    elif placement == 'group':
        group['if'] = "github.event_name == 'push'"
    elif placement == 'nested':
        group['parallel'][1] = {'parallel': [{'run': 'pytest', 'if': 'false'}]}
    elif placement == 'background':
        group['parallel'][0]['background'] = True
    elif placement == 'top-background':
        group = {'run': 'pytest', 'background': True}
    else:
        group['parallel'] = 'pytest'
    jobs['suite0']['steps'] = [group]
    assert va.analyze(jobs, 'verdict')[0] != 'pass'


@pytest.mark.parametrize('count', [2, 18])
def test_unconditional_parallel_suite_steps_still_pass(count):
    jobs = suites(count)
    jobs['suite0']['steps'] = [{'parallel': [{'run': 'pytest'}, {'run': 'ruff check'}]}]
    assert va.analyze(jobs, 'verdict')[0] == 'pass'


@pytest.mark.parametrize('count', [1, 6, 18])
@pytest.mark.parametrize('value', [True, 'true', 'TRUE', '${{ matrix.experimental }}', None, 0, ''])
@pytest.mark.parametrize('placement', ['step', 'parallel', 'nested', 'group'])
def test_tolerated_suite_step_cannot_certify_pass(count, value, placement):
    jobs = suites(count)
    step = {'run': 'pytest', 'continue-on-error': value}
    if placement == 'parallel':
        step = {'parallel': [step]}
    elif placement == 'nested':
        step = {'parallel': [{'parallel': [step]}]}
    elif placement == 'group':
        step = {'parallel': [{'run': 'pytest'}], 'continue-on-error': value}
    jobs['suite0']['steps'] = [step]
    state, reason = va.analyze(jobs, 'verdict')
    assert state == 'unmeasured'
    assert 'suite0' in reason
    assert 'step is allowed to fail' in reason


@pytest.mark.parametrize('value', [False, 'false', 'FALSE'])
@pytest.mark.parametrize('count', [1, 6, 18])
def test_literal_false_tolerance_preserves_pass(value, count):
    jobs = suites(count)
    jobs['suite0']['steps'][0]['continue-on-error'] = value
    assert va.analyze(jobs, 'verdict')[0] == 'pass'


def test_tolerated_step_keeps_literal_output_failure_evidence():
    jobs = suites(1)
    jobs['suite0']['outputs'] = {'ok': 'true'}
    jobs['suite0']['steps'] = [
        {'run': 'pytest'}, {'run': 'lint', 'continue-on-error': True}]
    jobs['verdict']['steps'] = [
        {'run': 'test "${{ needs.suite0.outputs.ok }}" = true'}]
    assert va.analyze(jobs, 'verdict')[0] == 'fail'


def test_tolerated_step_blocks_enumerated_pass():
    jobs = suites(1)
    jobs['suite0']['outputs'] = {'ok': 'true'}
    jobs['suite0']['steps'][0]['continue-on-error'] = True
    jobs['verdict']['steps'] = [{'run':
        'test "${{ needs.suite0.result }}" = success\n'
        'test "${{ needs.suite0.outputs.ok }}" = true'}]
    state, reason = va.analyze(jobs, 'verdict')
    assert state == 'unmeasured'
    assert 'suite0' in reason and 'step is allowed to fail' in reason
