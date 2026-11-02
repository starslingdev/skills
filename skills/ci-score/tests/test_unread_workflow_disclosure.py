"""Every fact discloses workflows the collector could not parse."""
import json

from test_ci_score_undeserved_passes import _load, _make_repo, cc_mod, _SKILL_DIR


def test_broken_workflow_is_named_on_every_card_and_report(tmp_path):
    root = _make_repo(tmp_path, {'.github/workflows/ci.yml':
        'on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n'
        '    timeout-minutes: 10\n    steps:\n      - run: pytest\n'})
    before, code = cc_mod.collect(root)
    assert code == 0
    (root / '.github/workflows/broken.yml').write_text('on: pull_request\njobs:\n\tbad: x\n')
    after, code = cc_mod.collect(root)
    assert code == 0
    facts = after['practice_facts']
    assert 'broken.yml' in facts['ci.hygiene.job-timeouts']['evidence']
    for check, fact in facts.items():
        assert fact['state'] == before['practice_facts'][check]['state']
        assert fact['files'] == before['practice_facts'][check]['files']
        assert fact['evidence'].count('not read, so nothing inside was graded') == 1
        assert '.github/workflows/broken.yml' in fact['evidence']
    assert before['ci_score']['value'] == after['ci_score']['value']
    report = _load('ci_score_render_unread_disclosure', 'scripts/render_report.py')
    rendered = report.render_report(after, json.loads(
        (_SKILL_DIR / 'references/ci-score-spec.json').read_text()))
    assert rendered.count('broken.yml') >= len(facts)
