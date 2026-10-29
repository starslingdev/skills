"""Synthetic regressions for status disclosure, source bounds, and SDK pointers."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import test_structural_findings as sf
import test_windows_setup_guidance as wg
from test_scan_detectors import _scan, _write_workflow
import summary

SKILL = Path(__file__).resolve().parents[1]


def collected(monkeypatch):
    monkeypatch.setattr(sf.cr, 'GhClient', sf._FakeRepoPublishedAggregateClient)
    return sf.cr.collect({
        'workflow_job_graph': copy.deepcopy(sf._AGG_JOB_GRAPH),
        'workflow_status_publishers': copy.deepcopy(sf._AGG_PUBLISHERS),
        'findings': [{'id': 'f1', 'workflow_file': sf._AGG_CI_WF},
                     {'id': 'f2', 'workflow_file': sf._AGG_WF}]},
        'o/r', max_runs=8, shallow_runs=8)


def test_agent_summary_names_repo_published_status(monkeypatch):
    doc = collected(monkeypatch)
    text = summary.build_summary(doc)
    line = next((line for line in text.splitlines() if 'posted by this repo' in line), '')
    assert 'All CI' in line
    assert 'not traced' in line


def test_unresolved_gate_reason_survives_missing_footer_metadata(monkeypatch):
    doc = collected(monkeypatch)
    cp = doc['pr_critical_path']
    assert 'All CI' in cp.get('unresolved_reason', '')
    cp.pop('required_checks_repo_published', None)
    for text in (summary.build_summary(doc), sf.bp.render(doc)):
        assert 'Merge gate unresolved' in text
        assert 'not traced' in text
        assert 'All CI' in text
    report = sf.bp.render(doc)
    assert report.index('Merge gate unresolved') < report.index('Bottom line')


@pytest.mark.parametrize('shape', ['script', 'action-file', 'action-dir', 'workflow'])
def test_status_detection_ignores_sources_symlinked_outside_checkout(tmp_path, shape):
    root = tmp_path / 'repo'
    root.mkdir()
    source = "github.rest.repos.createCommitStatus({context: 'All CI'});"
    outside = tmp_path / 'outside.js'
    outside.write_text(source)
    if shape == 'script':
        (root / 'status.js').symlink_to(outside)
        step = 'run: node status.js'
    elif shape.startswith('action'):
        action = root / '.github/actions/status'
        action.parent.mkdir(parents=True)
        outside_action = tmp_path / 'action'
        outside_action.mkdir()
        (outside_action / 'action.yml').write_text('runs:\n  using: composite\n  steps:\n    - run: ' + source + '\n')
        if shape == 'action-dir':
            action.symlink_to(outside_action, target_is_directory=True)
        else:
            action.mkdir()
            (action / 'action.yml').symlink_to(outside_action / 'action.yml')
        step = 'uses: ./.github/actions/status'
    else:
        outside.write_text('on: pull_request\njobs:\n  status:\n    steps:\n      - run: ' + source + '\n')
        workflows = root / '.github/workflows'
        workflows.mkdir(parents=True)
        (workflows / 'ci.yml').symlink_to(outside)
        assert _scan(root)['workflow_status_publishers'] == {}
        return
    _write_workflow(root, 'ci.yml', 'on: pull_request\njobs:\n  status:\n    steps:\n      - ' + step + '\n')
    assert _scan(root)['workflow_status_publishers'] == {}


def test_status_context_extraction_bounds_100kb_pathological_input():
    code = """import runpy, sys, time
scan = runpy.run_path(sys.argv[1])
start = time.monotonic()
scan['_status_contexts']([' ' * 100_000])
assert time.monotonic() - start < 1
"""
    result = subprocess.run([sys.executable, '-c', code, str(SKILL / 'scripts/scan.py')],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr


def test_opt80_duplicate_required_names_keep_unknown_reason():
    vr = wg.vr
    wf, other = '.github/workflows/ci.yml', '.github/workflows/other.yml'
    graph = {wf: {'test': {'name': 'Test', 'needs': []}},
             other: {'test': {'name': 'Test', 'needs': []}}}
    data = {'declared_pr_workflows': [wf, other], 'required_checks_complete': True,
            'required_checks': ['Test'], 'workflow_job_graph': graph}
    assert sf.cr._opt80_merge_gating_jobs(
        wf, sf.cr.RequiredChecks(frozenset({'Test'}), True), graph, {}) is None
    f = {'pattern': 'OPT80', 'workflow_file': wf,
         'evidence': vr._VR_OPT80_TAIL_WITHHELD_SENTENCES['merge_gating_unknown'].format(job='Test', pause='45'),
         'checkout_stall': {'job': 'Test', 'on_critical_path': True,
                            'tail_run_longest_pause_s': 45.0,
                            'tail_axis_withheld_reason': 'merge_gating_unknown'}}
    assert vr._vr_opt80_tail_withheld_expected(f, data) == {'merge_gating_unknown'}
    assert vr._vr_opt80_tail_withheld_problems(f, data) == []
    data['workflow_names'] = {wf: 'CI', other: 'Other'}
    data['required_checks'] = ['CI / Test']
    assert vr._vr_opt80_tail_withheld_expected(f, data) == {'not_merge_gating', 'tail_not_derivable'}


@pytest.mark.parametrize('step', ['Setup .NET', 'Install .NET SDK', 'Set up .NET 8 SDK', 'Setup .NET SDK'])
def test_common_dotnet_setup_names_are_uncredited_and_verifiable(step, tmp_path):
    f = wg.finding(step)
    original = copy.deepcopy(f)
    text = wg.render(f)
    pointer = next((line for line in text.splitlines() if 'Windows setup benchmark:' in line), '')
    assert 'OPT94' in pointer
    assert text.replace(pointer + '\n', '') == wg.render(wg.finding(step, labels=[]))
    assert f == original
    path = tmp_path / 'findings.json'
    path.write_text(json.dumps({'findings': [f]}))
    assert wg.vr.check_windows_setup_guidance(text, path).ok
    for labels in ([['ubuntu-latest']], [['windows-latest'], ['ubuntu-latest']]):
        assert 'Windows setup benchmark:' not in wg.render(wg.finding(step, labels=labels))


@pytest.mark.parametrize('step', ['Setup Node', 'Install .NET application', 'Build .NET SDK tests', 'Setup .NET Framework'])
def test_unrelated_setup_names_still_have_no_sdk_pointer(step):
    assert 'Windows setup benchmark:' not in wg.render(wg.finding(step))


def test_unresolved_reason_is_visible_without_measured_poles():
    doc = {'pr_critical_path': {'provenance': 'unresolved',
           'unresolved_reason': 'Required All CI inputs are not traced.'}}
    assert 'Merge gate unresolved' in sf.bp.render(doc)
    assert 'All CI' in summary.build_summary(doc)


def test_status_source_symlinks_within_checkout_still_count(tmp_path):
    (tmp_path / 'actual.js').write_text("github.rest.repos.createCommitStatus({context: 'All CI'});")
    (tmp_path / 'status.js').symlink_to(tmp_path / 'actual.js')
    _write_workflow(tmp_path, 'ci.yml', 'on: pull_request\njobs:\n  status:\n    steps:\n      - run: node status.js\n')
    assert _scan(tmp_path)['workflow_status_publishers']['.github/workflows/ci.yml']['contexts'] == ['All CI']


def test_complete_opt80_report_accepts_ambiguous_required_name(tmp_path):
    import test_verify_report_self as report_tests
    doc = report_tests._opt80_withheld_doc('merge_gating_unknown')
    wf = doc['findings'][0]['workflow_file']
    doc['required_checks_complete'] = True
    doc['required_checks'] = ['build']
    doc['workflow_job_graph']['.github/workflows/other.yml'] = copy.deepcopy(doc['workflow_job_graph'][wf])
    check = report_tests._opt80_withheld_check(tmp_path, doc)
    assert check.ok, check.detail
