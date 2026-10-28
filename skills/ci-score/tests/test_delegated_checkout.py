"""Checkout cannot be certified inside entirely remote PR workflows."""
import pytest
from test_ci_score_undeserved_passes import _collect, SHA, SHALLOW

REMOTE = f'    uses: example/shared/.github/workflows/ci.yml@{SHA}\n'
LOCAL = ('    runs-on: ubuntu-latest\n    steps:\n'
         f'      - uses: actions/checkout@{SHA}\n')


def test_entirely_delegated_checkout_is_not_applicable(tmp_path):
    doc = _collect(tmp_path, {'.github/workflows/ci.yml':
        'on: pull_request\njobs:\n  ci:\n' + REMOTE})
    fact = doc['practice_facts'][SHALLOW]
    assert fact['state'] == 'not_applicable'
    assert 'another repository' in fact['evidence']
    assert doc['ci_score']['value'] == 25


@pytest.mark.parametrize('steps,expected', [(LOCAL, 'pass'),
    (LOCAL + '        with:\n          fetch-depth: 0\n', 'fail')])
def test_local_job_keeps_checkout_applicable(tmp_path, steps, expected):
    doc = _collect(tmp_path, {'.github/workflows/ci.yml':
        'on: pull_request\njobs:\n  ci:\n' + REMOTE + '  local:\n' + steps})
    assert doc['practice_facts'][SHALLOW]['state'] == expected


def test_unread_local_callee_is_not_proven_remote(tmp_path):
    doc = _collect(tmp_path, {'.github/workflows/ci.yml':
        'on: pull_request\njobs:\n  ci:\n' + REMOTE +
        '  local:\n    uses: ./.github/workflows/missing.yml\n'})
    assert doc['practice_facts'][SHALLOW]['state'] == 'pass'
