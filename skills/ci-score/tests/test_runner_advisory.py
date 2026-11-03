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


@pytest.mark.parametrize('alteration', ['remove', 'label', 'count', 'move', 'duplicate', 'remedy'])
def test_verifier_binds_each_advisory_bullet_to_its_note(alteration):
    doc=fixture();doc['advisory_notes']=notes()
    text=rr.render_report(doc,SPEC)
    bullet=next(line for line in text.splitlines() if line.startswith('- **Runner choice'))
    if alteration=='remove':
        text=text.replace(bullet,'')
    elif alteration=='label':
        text=text.replace(bullet,bullet.replace('custom-linux','different-linux'))
    elif alteration=='count':
        text=text.replace(bullet,bullet.replace('3 jobs','30 jobs'))
    elif alteration=='move':
        text=text.replace(bullet,'')+'\n'+bullet
    elif alteration=='duplicate':
        text=text.replace(bullet,bullet+'\n'+bullet)
    else:
        text=text.replace(bullet,bullet.replace('vars.RUNNER_LINUX','vars.UNRELATED'))
    assert any('ADVISORY: rendered notes differ' in p for p in vr.verify(doc,text,SPEC))


def multi_file(label='custom-linux'):
    return [(f'wf{i}.yml', {'jobs': {'test': {'runs-on': label}}}, '') for i in range(3)]


def all_pass_doc():
    doc = fixture()
    for fact in doc['practice_facts'].values():
        if fact.get('state') == 'fail':
            fact['state'] = 'pass'
    doc['ci_score'] = sc.compute_ci_score(doc, SPEC)
    return doc


@pytest.mark.parametrize('label', ['custom-${{ matrix.size }}', ['self-hosted', 'Windows', 'X64'],
                                   ['self-hosted', 'macOS', 'ARM64'], ['self-hosted', 'Linux', 'arm']])
def test_partly_literal_expressions_and_generic_selectors_in_any_case_are_silent(label):
    assert notes(label) == []


def test_same_job_id_in_three_workflow_files_counts_three_jobs():
    [note] = pf._runner_advisories(multi_file())
    assert note['job_count'] == 3
    assert [j['file'] for j in note['jobs']] == ['wf0.yml', 'wf1.yml', 'wf2.yml']


def test_surrounding_whitespace_does_not_split_a_label():
    parsed = workflows()
    for job, label in zip(parsed[0][1]['jobs'].values(), [' custom-linux', 'custom-linux ', 'custom-linux']):
        job['runs-on'] = label
    assert pf._runner_advisories(parsed)[0]['job_count'] == 3


def test_all_pass_report_still_renders_the_advisory_after_the_card():
    doc = all_pass_doc(); doc['advisory_notes'] = notes()
    text = rr.render_report(doc, SPEC)
    assert text.index(rr.DISCLOSURE) < text.index('## Advisory notes') < text.index('## Recommendations')
    assert vr.verify(doc, text, SPEC) == []


def test_verifier_requires_the_unscored_disclosure():
    doc = fixture(); doc['advisory_notes'] = notes()
    text = rr.render_report(doc, SPEC).replace('These notes are not part of the score.', '')
    assert 'ADVISORY: missing unscored disclosure' in vr.verify(doc, text, SPEC)


def test_verifier_names_an_advisory_check_id_in_the_stamp():
    doc = fixture(); doc['advisory_notes'] = notes()
    doc['ci_score']['checks'].append({'check_id': 'ci.runner.swappable', 'label': 'Runner choice', 'state': 'fail'})
    doc['ci_score']['checks_applicable'] += 1
    assert 'ADVISORY: advisory entered the scored checks' in vr.verify(doc, rr.render_report(doc, SPEC), SPEC)


def test_verifier_names_an_advisory_table_row_after_the_section():
    doc = fixture(); doc['advisory_notes'] = notes()
    text = rr.render_report(doc, SPEC) + '\n| ci.runner.swappable | fail |\n'
    assert 'ADVISORY: advisory appears in a scored table row' in vr.verify(doc, text, SPEC)


def test_verifier_recomputes_the_score_when_advisories_are_present():
    doc = fixture(); doc['advisory_notes'] = notes()
    inflated = all_pass_doc()['ci_score']
    assert inflated != doc['ci_score']
    doc['ci_score'] = inflated
    problems = vr.verify(doc, rr.render_report(doc, SPEC), SPEC)
    assert 'ADVISORY: score differs from recomputation without advisories' in problems


def test_verifier_rejects_advisory_section_above_the_scored_checks():
    doc = fixture(); doc['advisory_notes'] = notes()
    text = rr.render_report(doc, SPEC)
    block = text[text.index('## Advisory notes'):text.index('## Recommendations')]
    text = text.replace(block, '').replace('| | Check | Evidence |', block + '| | Check | Evidence |', 1)
    assert 'ADVISORY: advisory section precedes the scored checks' in vr.verify(doc, text, SPEC)


def test_verifier_rejects_an_advisory_the_document_does_not_record():
    doc = fixture(); doc['advisory_notes'] = notes()
    text = rr.render_report(doc, SPEC)
    doc['advisory_notes'] = []
    assert any('ADVISORY: rendered notes differ' in p for p in vr.verify(doc, text, SPEC))


def test_close_names_advisories_as_unscored_and_never_as_a_fix_slot():
    close = (ROOT / 'references/close-contract.md').read_text()
    para = next(p for p in close.split('\n\n') if 'advisory_notes' in p)
    assert 'not part of the score' in para and 'never a fix slot' in para
