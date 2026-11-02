"""Literal cache switches obey the same verdict as resolved expressions."""
from __future__ import annotations

import pytest
import yaml

from test_ci_score_undeserved_fails import pf_mod, CACHE


def parsed_case(action, key, value, runner="ubuntu-latest"):
    raw = ("on: pull_request\njobs:\n  test:\n"
           f"    runs-on: {runner}\n    steps:\n"
           "      - uses: actions/checkout@v5\n"
           f"      - uses: {action}\n        with:\n          {key}: '{value}'\n"
           "      - run: pytest\n")
    return [("ci.yml", yaml.safe_load(raw), raw)]


@pytest.mark.parametrize("action,key,value", [
    ("actions/setup-go@v5", "cache", "no"),
    ("actions/setup-node@v4", "cache", "off"),
    ("astral-sh/setup-uv@v5", "enable-cache", "False"),
    ("actions/setup-go@v5", "Cache", "No"),
    ("ruby/setup-ruby@v1", "bundler-cache", "OFF"),
])
def test_literal_off_is_not_credited_and_named(tmp_path, action, key, value):
    (tmp_path / "go.mod").write_text("module example\n")
    (tmp_path / "go.sum").write_text("")
    fact = pf_mod._practice_facts(parsed_case(action, key, value), tmp_path)[CACHE]
    assert fact["state"] == "fail"
    assert action.split("/", 1)[1] in fact["evidence"]
    assert key.lower() in fact["evidence"]
    assert value in fact["evidence"]


@pytest.mark.parametrize("runner", ["[self-hosted, linux]", "${{ matrix.os }}",
                                   "{group: builders}", "[ubuntu-latest, linux]"])
@pytest.mark.parametrize("value", ["auto", " auto "])
def test_uv_auto_needs_a_literal_hosted_runner(tmp_path, runner, value):
    parsed = parsed_case("astral-sh/setup-uv@v5", "enable-cache", value, runner)
    assert pf_mod._practice_facts(parsed, tmp_path)[CACHE]["state"] == "fail"
    unresolved = pf_mod._unresolved_setup_defaults(parsed, tmp_path)
    assert unresolved and "setup-uv@v5" in unresolved[0]["step"]
    assert "enable-cache: true" in unresolved[0]["reason"]


@pytest.mark.parametrize("action,key,value,runner", [
    ("astral-sh/setup-uv@v5", "enable-cache", "auto", "ubuntu-latest"),
    ("actions/setup-node@v4", "cache", "npm", "ubuntu-latest"),
    ("astral-sh/setup-uv@v5", "enable-cache", "true", "[self-hosted, linux]"),
])
def test_enabled_controls_keep_credit(tmp_path, action, key, value, runner):
    assert pf_mod._practice_facts(parsed_case(action, key, value, runner), tmp_path)[CACHE]["state"] == "pass"
