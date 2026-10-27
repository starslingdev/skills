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
