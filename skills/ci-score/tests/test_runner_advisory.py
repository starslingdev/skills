"""Unscored runner portability notes never enter rubric arithmetic."""
import copy
import importlib.util
import json
import sys
from pathlib import Path
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location('runner_advisory_'+name,ROOT/'scripts'/f'{name}.py')
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);return m
pf=load('practice_facts');sc=load('ci_score');rr=load('render_report');vr=load('verify_report')
SPEC=json.loads((ROOT/'references/ci-score-spec.json').read_text())


def workflows(label='custom-linux', count=3):
    return [('ci.yml', {'jobs':{f'test{i}':{'runs-on':label} for i in range(count)}},'')]


def notes(label='custom-linux', count=3):
    return pf._runner_advisories(workflows(label,count))


def fixture():
    return json.loads((ROOT/'tests/fixtures/ci-score/stamped-fixture.json').read_text())


def test_custom_label_note_renders_outside_score_with_identical_stamp():
    doc=fixture();before=json.dumps(sc.compute_ci_score(doc,SPEC),sort_keys=True)
    doc['advisory_notes']=notes()
    assert doc['advisory_notes'][0]['advisory_id']=='ci.runner.swappable'
    assert json.dumps(sc.compute_ci_score(doc,SPEC),sort_keys=True)==before
    text=rr.render_report(doc,SPEC)
    assert '## Advisory notes' in text and 'not part of the score' in text
    assert 'vars.RUNNER_LINUX' in text and 'custom-linux' in text
    assert vr.verify(doc,text,SPEC)==[]


@pytest.mark.parametrize('label', ['${{ vars.RUNNER_LINUX }}','${{ matrix.runner }}','${{ inputs.runner }}','ubuntu-latest','windows-2025','macos-15',['self-hosted','linux','x64']])
def test_central_or_standard_labels_are_silent(label):
    assert notes(label)==[]


def test_two_jobs_are_silent_and_duplicate_labels_count_one_job():
    assert notes(count=2)==[]
    assert notes(['custom-linux','custom-linux'],count=2)==[]


def test_label_list_and_group_labels_count_unique_jobs():
    assert len(notes(['self-hosted','custom-linux']))==1
    assert len(notes({'group':'pool','labels':['custom-linux']}))==1


@pytest.mark.parametrize('path', sorted((ROOT/'tests/fixtures/corpora').glob('*/findings.json')), ids=lambda p:'corpus-'+str(sorted((ROOT/'tests/fixtures/corpora').glob('*/findings.json')).index(p)+1))
def test_every_corpus_keeps_exact_score_with_advisory(path):
    doc=json.loads(path.read_text());before=sc.compute_ci_score(doc,SPEC)
    doc['advisory_notes']=notes()
    assert sc.compute_ci_score(doc,SPEC)==before==doc['ci_score']


def test_verifier_rejects_advisory_in_scored_table():
    doc=fixture();doc['advisory_notes']=notes();text=rr.render_report(doc,SPEC)
    text=text.replace('## Advisory notes','| ci.runner.swappable | fail |\n\n## Advisory notes',1)
    assert any('ADVISORY' in p for p in vr.verify(doc,text,SPEC))


def test_verifier_rejects_advisory_added_to_score_even_with_consistent_arithmetic():
    doc=fixture();doc['advisory_notes']=notes()
    doc['ci_score']['checks'].append({'check_id':'ci.runner.swappable','label':'Runner choice','state':'fail'})
    doc['ci_score']['checks_applicable']+=1
    assert any('ADVISORY' in p for p in vr.verify(doc,rr.render_report(doc,SPEC),SPEC))


def test_metadata_only_owner_decision():
    notes()
    decision=next(d for d in SPEC['decision_log'] if d['id']=='OD-CS27')
    assert decision['metadata_only'] is True
    assert SPEC['spec_version']=='ci-score-v0.1.8' and len(SPEC['checks'])==11


def test_collector_stamps_advisories_without_scoring_them(tmp_path, monkeypatch):
    cc=load('collect_config')
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    folder=tmp_path/'.github/workflows';folder.mkdir(parents=True)
    workflow=workflows()[0][1]
    workflow['on']='pull_request'
    for job in workflow['jobs'].values():job['steps']=[{'run':'python -m pytest'}]
    (folder/'ci.yml').write_text(yaml.safe_dump(workflow))
    subprocess.run(['git','-C',str(tmp_path),'add','.github/workflows/ci.yml'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
    doc,code=cc.collect(tmp_path)
    assert code==0
    assert doc['advisory_notes'][0]['advisory_id']=='ci.runner.swappable'
    assert len(doc['ci_score']['checks'])==11
    clean=copy.deepcopy(doc);clean.pop('advisory_notes')
    assert sc.compute_ci_score(clean,SPEC)==doc['ci_score']


def test_custom_labels_count_case_insensitively():
    parsed=workflows()
    for job,label in zip(parsed[0][1]['jobs'].values(), ['Custom-Linux','custom-linux','CUSTOM-LINUX']):
        job['runs-on']=label
    assert pf._runner_advisories(parsed)[0]['job_count']==3


def test_historical_engine_adapter_has_no_advisories():
    from types import SimpleNamespace
    from _baseline_engine import _adapt
    historical = SimpleNamespace(_practice_facts=lambda parsed, root: {'sentinel': True})
    adapted = _adapt(historical)
    assert adapted._runner_advisories(workflows()) == []
    assert adapted._practice_facts([], None) == {'sentinel': True}
