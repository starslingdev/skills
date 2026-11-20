"""Fixture tests for tests/_gha_expr.py, the `if:` evaluator the routing guards rely on.

A routing guard is only as good as its evaluator: one that reads every guard as
true would pass every routing assertion. These pin the rules the routing
decisions actually turn on.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_gha_expr", Path(__file__).resolve().parent / "_gha_expr.py")
gx = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gx
_spec.loader.exec_module(gx)

SAME = "github.event.pull_request.head.repo.full_name == github.repository"
OTHER = "github.event.pull_request.head.repo.full_name != github.repository"


@pytest.mark.parametrize("name,kw,same,other", [
    ("pull_request", {}, True, False),
    ("pull_request", {"head_repo": "someone/skills"}, False, True),
    ("pull_request", {"head_repo": None}, False, True),   # deleted fork: null vs name
    ("merge_group", {}, False, True),                     # no pull_request at all
    ("push", {}, False, True),
])
def test_repo_comparison_follows_githubs_casting_rules(name, kw, same, other):
    ctx = gx.event(name, **kw)
    assert gx.evaluate(SAME, ctx) is same
    assert gx.evaluate(OTHER, ctx) is other


def test_operators_and_literals():
    ctx = gx.event("merge_group")
    assert gx.evaluate("github.event_name == 'merge_group'", ctx) is True
    assert gx.evaluate("github.event_name == 'MERGE_GROUP'", ctx) is True  # case-insensitive
    assert gx.evaluate("github.event_name != 'pull_request' && 'x'", ctx) == "x"
    assert gx.evaluate("'' || 'fallback'", ctx) == "fallback"
    assert gx.evaluate("!(github.event_name == 'push')", ctx) is True
    assert gx.evaluate("github.nope.deeper == null", ctx) is True
    assert gx.evaluate("${{ always() }}", ctx) is True
    assert gx.evaluate("success()", ctx, status="failure") is False
    assert gx.evaluate("null == 0", ctx) is True and gx.evaluate("'a' == 0", ctx) is False


def test_job_without_if_runs_on_success():
    assert gx.job_runs({}, gx.event("push")) is True


@pytest.mark.parametrize("bad", ["contains(github.ref, 'x')", "github.ref ==", "a b", "1 = 1"])
def test_unreadable_expressions_raise(bad):
    with pytest.raises(ValueError):
        gx.evaluate(bad, gx.event("push"))
