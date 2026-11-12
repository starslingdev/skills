"""Oracle tests for `sec.default-branch.review-required`.

A leaked token that belongs to someone with write access can put code on the
default branch unless that branch requires a pull request reviewed by someone
else, and unless an approval stops counting once new commits are pushed
(otherwise the token pushes new code onto an approved pull request and merges
it on the old approval).

Every test drives the real reader (`_GhPushAccess`) with recorded answers in
the documented shapes, through the same seam the environment facts use;
nothing here touches the network.

Shapes, from GitHub's REST docs:
  * `GET /repos/{o}/{r}/rules/branches/{branch}` — "Returns all active rules
    that apply to the specified branch"; "Rules in rulesets with "evaluate" or
    "disabled" enforcement statuses are not returned". An array of rules, each
    with `type` and `parameters`; a `pull_request` rule's parameters include
    `required_approving_review_count`, `dismiss_stale_reviews_on_push` and
    `require_last_push_approval`, and each rule names its `ruleset_id`.
  * `GET /repos/{o}/{r}/rulesets/{id}` — `enforcement` is one of `disabled`,
    `active`, `evaluate`; "the bypass_actors property is only returned if the
    user making the API request has write access to the ruleset".
  * `GET /repos/{o}/{r}/branches/{branch}/protection` — classic protection,
    readable by admins; `required_pull_request_reviews` carries
    `required_approving_review_count`, `dismiss_stale_reviews`,
    `require_last_push_approval` and `bypass_pull_request_allowances`
    (`users`, `teams`, `apps`); `enforce_admins.enabled`. An unprotected
    branch answers 404 "Branch not protected".
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

FACT = "sec.default-branch.review-required"


def _load():
    if "ci_secure_config_facts" in sys.modules:
        return sys.modules["ci_secure_config_facts"]
    spec = importlib.util.spec_from_file_location(
        "ci_secure_config_facts", _SCRIPTS / "config_facts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_secure_config_facts"] = mod
    spec.loader.exec_module(mod)
    return mod


cf = _load()

_SAFE_WF = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: make test
"""

R = "repos/owner/repo"
RULES = f"{R}/rules/branches/main?per_page=100"
CLASSIC = f"{R}/branches/main/protection"
BRANCH = f"{R}/branches/main"


def _api_error(stderr: str) -> RuntimeError:
    # `gh_utils.run_gh_api` raises with this prefix plus gh's own stderr.
    return RuntimeError(f"API call failed: {stderr}")


_NOT_PROTECTED = _api_error("gh: Branch not protected (HTTP 404)")
_ADMIN_403 = _api_error("gh: Must have admin rights to Repository. (HTTP 403)")
_HTTP_502 = _api_error("gh: Bad Gateway (HTTP 502)")


class _Gh:
    """Answers only the recorded paths; anything else is an unrecorded 404,
    so a mistyped path reads as unmeasured and the assertions catch it."""

    def __init__(self, responses: dict, prereqs: bool = True):
        self.responses, self.prereqs = responses, prereqs
        self.calls: list[str] = []

    def check_prereqs(self):
        return self.prereqs

    def run_gh_api(self, path, **kw):
        self.calls.append(path)
        if path not in self.responses:
            raise _api_error(f"gh: Not Found (HTTP 404) [unrecorded {path}]")
        answer = self.responses[path]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)


def _pr_rule(count=1, dismiss=False, last_push=False, ruleset_id=7):
    return {"type": "pull_request", "ruleset_source_type": "Repository",
            "ruleset_source": "owner/repo", "ruleset_id": ruleset_id,
            "parameters": {"required_approving_review_count": count,
                           "dismiss_stale_reviews_on_push": dismiss,
                           "require_last_push_approval": last_push,
                           "require_code_owner_review": False,
                           "required_review_thread_resolution": False}}


def _ruleset(rid=7, enforcement="active", bypass=()):
    body = {"id": rid, "name": f"protect-main-{rid}", "target": "branch",
            "enforcement": enforcement}
    if bypass is not None:
        body["bypass_actors"] = list(bypass)
    return body


def _classic(count=1, dismiss=False, last_push=False, enforce_admins=True,
             users=(), teams=(), apps=()):
    return {"required_pull_request_reviews": {
                "required_approving_review_count": count,
                "dismiss_stale_reviews": dismiss,
                "require_last_push_approval": last_push,
                "require_code_owner_reviews": False,
                "bypass_pull_request_allowances": {
                    "users": [{"login": u} for u in users],
                    "teams": [{"slug": t} for t in teams],
                    "apps": [{"slug": a} for a in apps]}},
            "enforce_admins": {"enabled": enforce_admins}}


def _responses(rules=(), rulesets=(), classic=_NOT_PROTECTED, protected=None):
    out = {R: {"default_branch": "main", "permissions": {"admin": False}},
           RULES: list(rules), CLASSIC: classic}
    for rs in rulesets:
        out[f"{R}/rulesets/{rs['id']}?includes_parents=true"] = rs
    if protected is not None:
        out[BRANCH] = {"name": "main", "protected": protected}
    return out


def _run(tmp_path, responses, *, repo="owner/repo", prereqs=True):
    gh = _Gh(responses, prereqs=prereqs)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(_SAFE_WF)
    with mock.patch.object(cf, "_gh_utils", lambda: gh):
        out = cf.compute_config_facts(
            tmp_path, [wf / "ci.yml"], [], repo=repo,
            required_contexts_fetcher=lambda r: ([], "branch `main`"),
            fork_approval_fetcher=lambda r: ("first_time_contributors", "x"),
            codeowner_review_fetcher=lambda r: (True, "a ruleset"))
    return _fact(out, FACT), gh


def _fact(out, fact_id):
    for f in out["facts"]:
        if f["fact_id"] == fact_id:
            return f
    raise AssertionError(f"{fact_id} missing from the fact table")


# --- pass ---------------------------------------------------------------------

def test_a_reviewed_pull_request_with_stale_approvals_dismissed_passes(
        tmp_path):
    f, gh = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)],
                                      rulesets=[_ruleset()]))
    assert f["outcome"] == "pass", f
    assert "`main`" in f["evidence"] and "1 approving review" in f["evidence"]
    # A ruleset that already holds settles it: the admin-only classic
    # endpoint is never asked.
    assert CLASSIC not in gh.calls, gh.calls


def test_approval_of_the_most_recent_push_also_passes(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(last_push=True)],
                                     rulesets=[_ruleset()]))
    assert f["outcome"] == "pass", f


def test_classic_protection_alone_can_pass(tmp_path):
    f, _ = _run(tmp_path, _responses(classic=_classic(count=2, dismiss=True)))
    assert f["outcome"] == "pass", f
    assert "2 approving review" in f["evidence"], f


def test_rules_layer_across_rulesets_and_classic_protection(tmp_path):
    """GitHub applies the most restrictive version of each rule, so a ruleset
    needing one approval plus classic protection dismissing stale approvals
    holds together, though neither holds alone."""
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()],
                                     classic=_classic(count=0, dismiss=True)))
    assert f["outcome"] == "pass", f


# --- bypass: a pass whose evidence names who can skip review -------------------

def test_ruleset_bypass_actors_are_named_on_a_pass(tmp_path):
    bypass = [{"actor_id": 5, "actor_type": "RepositoryRole",
               "bypass_mode": "always"},
              {"actor_id": 42, "actor_type": "Team",
               "bypass_mode": "pull_request"}]
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)],
                                     rulesets=[_ruleset(bypass=bypass)]))
    assert f["outcome"] == "pass", f
    ev = f["evidence"]
    assert "bypass" in ev, ev
    assert "repository admins" in ev and "team 42" in ev, ev


def test_an_unreadable_bypass_list_is_disclosed_on_a_pass(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)],
                                     rulesets=[_ruleset(bypass=None)]))
    assert f["outcome"] == "pass", f
    assert "bypass list is not readable" in f["evidence"], f


def test_classic_admin_exemption_and_allowances_are_named_on_a_pass(tmp_path):
    f, _ = _run(tmp_path, _responses(classic=_classic(
        dismiss=True, enforce_admins=False, users=["release-bot"])))
    assert f["outcome"] == "pass", f
    ev = f["evidence"]
    assert "admins" in ev and "`release-bot`" in ev, ev


# --- fail ---------------------------------------------------------------------

def test_zero_required_approvals_fails(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(count=0, dismiss=True)],
                                     rulesets=[_ruleset()]))
    assert f["outcome"] == "fail", f
    assert "no approving review" in f["evidence"], f


def test_no_pull_request_rule_anywhere_fails(tmp_path):
    f, _ = _run(tmp_path, _responses())
    assert f["outcome"] == "fail", f
    assert "nothing requires a pull request" in f["evidence"], f


def test_neither_stale_approval_rule_fails_and_names_both(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()]))
    assert f["outcome"] == "fail", f
    ev = f["evidence"]
    assert "Dismiss stale pull request approvals" in ev, ev
    assert "most recent reviewable push" in ev, ev


def test_a_ruleset_in_evaluate_mode_is_not_counted(tmp_path):
    """The rules endpoint documents that it leaves evaluate-mode rulesets out;
    should one appear anyway, its detail decides, and evaluate enforces
    nothing."""
    f, _ = _run(tmp_path, _responses(
        rules=[_pr_rule(dismiss=True)],
        rulesets=[_ruleset(enforcement="evaluate")]))
    assert f["outcome"] == "fail", f
    assert "evaluate" in f["evidence"], f


def test_unprotected_branch_flag_settles_unreadable_classic_protection(
        tmp_path):
    """`protected: false` on the public branch endpoint means neither a
    ruleset nor classic protection covers the branch: a readable no."""
    f, _ = _run(tmp_path, _responses(classic=_ADMIN_403, protected=False))
    assert f["outcome"] == "fail", f


# --- unmeasured ---------------------------------------------------------------

def test_no_repository_is_unmeasured(tmp_path):
    f, gh = _run(tmp_path, _responses(), repo=None)
    assert f["outcome"] == "unmeasured", f
    assert not gh.calls


def test_no_gh_is_unmeasured(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)]),
                prereqs=False)
    assert f["outcome"] == "unmeasured", f
    assert "gh" in f["evidence"], f


def test_an_unreadable_rules_endpoint_is_unmeasured(tmp_path):
    responses = _responses(classic=_ADMIN_403)
    responses[RULES] = _HTTP_502
    f, _ = _run(tmp_path, responses)
    assert f["outcome"] == "unmeasured", f


def test_admin_only_classic_protection_cannot_be_read_as_none(tmp_path):
    """A ruleset that does not hold plus a 403 from classic protection
    establishes nothing: classic protection could turn on the missing rule.
    `protected: true` is set by the ruleset too, so it settles nothing."""
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()],
                                     classic=_ADMIN_403, protected=True))
    assert f["outcome"] == "unmeasured", f
    assert "classic" in f["evidence"], f


# --- classic protection hidden behind a plain 404 ------------------------------
#
# GitHub answers a non-admin reader of the classic protection endpoint with a
# plain "Not Found (HTTP 404)" (the same hide the required-checks fact
# accepts via `_is_classic_hidden_404`). Hidden classic protection can only
# ADD review rules, so it never undoes a pass the rulesets earn on their own,
# and it always keeps the rulesets alone from proving a fail: that case is
# unmeasured, naming the hide and that an admin can confirm.

_HIDDEN_404 = _api_error("gh: Not Found (HTTP 404)")


def test_a_hidden_classic_404_never_undoes_a_ruleset_pass(tmp_path):
    f, gh = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)],
                                      rulesets=[_ruleset()],
                                      classic=_HIDDEN_404, protected=True))
    assert f["outcome"] == "pass", f
    # The rulesets settle it, so the admin-only endpoint is never asked and
    # there is no unread source to disclose.
    assert CLASSIC not in gh.calls, gh.calls


def test_a_hidden_classic_404_keeps_short_rulesets_unmeasured(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()],
                                     classic=_HIDDEN_404, protected=True))
    assert f["outcome"] == "unmeasured", f
    ev = f["evidence"]
    assert "rulesets" in ev and "reset" in ev, ev
    assert "hidden from this reader" in ev, ev
    assert "admin can confirm" in ev, ev


def test_a_hidden_classic_404_with_no_pull_request_rule_is_unmeasured(
        tmp_path):
    f, _ = _run(tmp_path, _responses(classic=_HIDDEN_404, protected=True))
    assert f["outcome"] == "unmeasured", f
    assert "hidden from this reader" in f["evidence"], f
    assert "admin can confirm" in f["evidence"], f


def test_a_hidden_classic_404_on_an_unprotected_branch_still_fails(tmp_path):
    """`protected: false` is a readable no from both sources, so the hide
    hides nothing there."""
    f, _ = _run(tmp_path, _responses(classic=_HIDDEN_404, protected=False))
    assert f["outcome"] == "fail", f
    assert "nothing requires a pull request" in f["evidence"], f


def test_a_hidden_classic_404_never_settles_an_unread_rules_endpoint(
        tmp_path):
    responses = _responses(classic=_HIDDEN_404, protected=True)
    responses[RULES] = _HTTP_502
    f, _ = _run(tmp_path, responses)
    assert f["outcome"] == "unmeasured", f


@pytest.mark.parametrize("err", [
    _api_error("gh: Bad Gateway (HTTP 502): upstream Not Found (HTTP 404)"),
    _api_error("gh: API rate limit exceeded (HTTP 403)"),
    RuntimeError("timed out after 30s"),
])
def test_other_classic_failures_stay_unmeasured(tmp_path, err):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()],
                                     classic=err, protected=True))
    assert f["outcome"] == "unmeasured", f


def test_a_pull_request_rule_of_unexpected_shape_is_unmeasured(tmp_path):
    rule = _pr_rule(dismiss=True)
    rule["parameters"]["required_approving_review_count"] = "one"
    f, _ = _run(tmp_path, _responses(rules=[rule], rulesets=[_ruleset()]))
    assert f["outcome"] == "unmeasured", f


@pytest.mark.parametrize("body", [{"rules": []}, ["not a rule"]])
def test_a_rules_body_of_unexpected_shape_is_unmeasured(tmp_path, body):
    responses = _responses()
    responses[RULES] = body
    f, _ = _run(tmp_path, responses)
    assert f["outcome"] == "unmeasured", f
