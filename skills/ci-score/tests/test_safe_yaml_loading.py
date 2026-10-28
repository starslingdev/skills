"""Strict YAML reads retain SafeLoader's security boundary and release parsers."""
import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'practice_facts.py'
spec = importlib.util.spec_from_file_location('ci_score_safe_yaml', SOURCE)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)


def test_no_generic_yaml_load_calls():
    """Custom SafeLoader subclasses must not leave ambiguous yaml.load calls."""
    tree = ast.parse(SOURCE.read_text())
    calls = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == 'yaml' and node.func.attr == 'load']
    assert calls == []


def test_safe_strict_yaml_keeps_standard_values():
    assert pf._load_strict_yaml('items: [1, true, null]\n') == {'items': [1, True, None]}


@pytest.mark.parametrize('text', ['key: 1\nkey: 2\n', 'outer: {key: 1, key: 2}'])
def test_safe_strict_yaml_rejects_duplicate_keys(text):
    with pytest.raises(pf._DuplicateKeyError, match='duplicated key'):
        pf._load_strict_yaml(text)


@pytest.mark.parametrize('tag', ['python/tuple', 'python/object:builtins.object'])
def test_safe_strict_yaml_rejects_python_tags(tag):
    # Construct inert tags; never include an executable payload in a fixture.
    with pytest.raises(yaml.constructor.ConstructorError):
        pf._load_strict_yaml('!!' + tag + ' []')


@pytest.mark.parametrize('text', ['valid: true', 'broken: [', 'key: 1\nkey: 2', '---\na: 1\n---\nb: 2'])
def test_safe_strict_yaml_disposes_on_success_and_errors(monkeypatch, text):
    disposed = []
    original = pf._StrictLockLoader.dispose

    def dispose(loader):
        disposed.append(loader)
        original(loader)

    monkeypatch.setattr(pf._StrictLockLoader, 'dispose', dispose)
    if text.startswith('valid'):
        assert pf._load_strict_yaml(text) == {'valid': True}
    else:
        with pytest.raises(yaml.YAMLError):
            pf._load_strict_yaml(text)
    assert len(disposed) == 1
