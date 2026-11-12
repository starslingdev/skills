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


# --- bypass ---------------------------------------------------------------------
#
# Owner decision (PR #341): a review rule that anyone short of an admin can
# skip does not stop a leaked credential, so it FAILS, naming each actor.
# Admins (organization admins, the repository admin role, classic protection
# that does not include administrators) stay a pass with a note: an admin can
# change these settings anyway. An unread bypass list is unmeasured, never a
# pass. `pull_request` bypass mode counts too — GitHub: "The actor can then
# choose to bypass any branch protections and merge that pull request."

_HEADLINE = "cannot land code on it alone"


def _actor(kind, aid, mode="always"):
    return {"actor_type": kind, "actor_id": aid, "bypass_mode": mode}


def _bypass_run(tmp_path, bypass, **kw):
    return _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)],
                                     rulesets=[_ruleset(bypass=bypass)], **kw))


@pytest.mark.parametrize("actor, named", [
    (_actor("Integration", 9), "app 9"),
    (_actor("DeployKey", None), "deploy keys"),
    (_actor("RepositoryRole", 4), "repository role 4"),
    (_actor("Team", 42), "team 42"),
    (_actor("User", 77), "user 77"),
])
def test_a_non_admin_bypass_actor_fails(tmp_path, actor, named):
    f, _ = _bypass_run(tmp_path, [actor])
    assert f["outcome"] == "fail", f
    ev = f["evidence"]
    assert named in ev, ev
    assert "with no review" in ev, ev
    assert "bypass list" in ev, ev
    assert _HEADLINE not in ev, ev


def test_pull_request_bypass_mode_still_fails(tmp_path):
    f, _ = _bypass_run(tmp_path, [_actor("Team", 42, "pull_request")])
    assert f["outcome"] == "fail", f
    assert "merge their own pull request" in f["evidence"], f


def test_admin_only_bypass_passes_with_a_note(tmp_path):
    f, _ = _bypass_run(tmp_path, [_actor("OrganizationAdmin", None),
                                  _actor("RepositoryRole", 5)])
    assert f["outcome"] == "pass", f
    ev = f["evidence"]
    assert _HEADLINE in ev, ev
    assert ("organization admins" in ev.lower()
            and "repository admins" in ev.lower()), ev
    assert "change these settings anyway" in ev, ev


def test_admins_plus_a_team_fail_and_name_only_the_team(tmp_path):
    f, _ = _bypass_run(tmp_path, [_actor("RepositoryRole", 5),
                                  _actor("Team", 42)])
    assert f["outcome"] == "fail", f
    assert "team 42" in f["evidence"], f


def test_an_unreadable_bypass_list_is_unmeasured(tmp_path):
    f, _ = _bypass_run(tmp_path, None)
    assert f["outcome"] == "unmeasured", f
    assert "bypass list is not readable" in f["evidence"], f
    assert _HEADLINE not in f["evidence"], f


def test_classic_admin_exemption_alone_passes_with_a_note(tmp_path):
    f, _ = _run(tmp_path, _responses(classic=_classic(
        dismiss=True, enforce_admins=False)))
    assert f["outcome"] == "pass", f
    assert "admins" in f["evidence"], f


@pytest.mark.parametrize("kw, named", [
    ({"apps": ["deploy-bot"]}, "app `deploy-bot`"),
    ({"users": ["release-bot"]}, "user `release-bot`"),
    ({"teams": ["releasers"]}, "team `releasers`"),
])
def test_a_classic_bypass_allowance_fails(tmp_path, kw, named):
    f, _ = _run(tmp_path, _responses(classic=_classic(dismiss=True, **kw)))
    assert f["outcome"] == "fail", f
    assert named in f["evidence"], f
    assert _HEADLINE not in f["evidence"], f


def test_a_bypasser_still_bound_by_another_source_passes(tmp_path):
    """GitHub layers rulesets and classic protection: an app that may skip
    the ruleset is still held by classic protection that lists no one."""
    f, gh = _bypass_run(tmp_path, [_actor("Integration", 9)],
                        classic=_classic(dismiss=True))
    assert f["outcome"] == "pass", f
    assert CLASSIC in gh.calls, gh.calls


def test_a_bypasser_with_hidden_classic_protection_is_unmeasured(tmp_path):
    f, _ = _bypass_run(tmp_path, [_actor("Integration", 9)],
                       classic=_HIDDEN_404, protected=True)
    assert f["outcome"] == "unmeasured", f


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


def test_a_failing_rule_also_names_its_bypassers(tmp_path):
    """A rule that fails on its own settings still names who may skip it, so
    one read of the report lists every fix: fixing the stale-approval
    setting alone would only turn this into a bypass fail on the next run."""
    f, _ = _run(tmp_path, _responses(
        rules=[_pr_rule()],
        rulesets=[_ruleset(bypass=[_actor("OrganizationAdmin", None),
                                   _actor("Integration", 101),
                                   _actor("Integration", 202)])]))
    assert f["outcome"] == "fail", f
    ev = f["evidence"]
    assert "Dismiss stale pull request approvals" in ev, ev
    assert "app 101" in ev and "app 202" in ev, ev
    assert "bypass list" in ev, ev
    assert "organization admins" not in ev.lower(), ev


def test_a_failing_rule_with_no_bypassers_names_none(tmp_path):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()]))
    assert f["outcome"] == "fail", f
    assert "bypass list" not in f["evidence"], f


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
    _api_error("gh: Not Found (HTTP 404)\nupstream Bad Gateway (HTTP 502)"),
    _api_error("gh: API rate limit exceeded (HTTP 403)"),
    RuntimeError("timed out after 30s"),
])
def test_other_classic_failures_stay_unmeasured(tmp_path, err):
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule()],
                                     rulesets=[_ruleset()],
                                     classic=err, protected=True))
    assert f["outcome"] == "unmeasured", f
    # Unread, not GitHub's ordinary hide: the evidence must not say hidden.
    assert "hidden from this reader" not in f["evidence"], f


def test_a_5xx_quoting_branch_not_protected_is_not_an_answer(tmp_path):
    """Only gh's own `Branch not protected (HTTP 404)` says the branch has no
    classic protection; a 5xx whose body quotes those words was not read."""
    err = _api_error('gh: Bad Gateway (HTTP 502)\n'
                     '{"message":"Branch not protected","status":"404"}')
    f, _ = _run(tmp_path, _responses(classic=err, protected=True))
    assert f["outcome"] == "unmeasured", f


def test_a_missing_stale_approval_setting_is_never_read_as_on(tmp_path):
    rule = _pr_rule()
    del rule["parameters"]["dismiss_stale_reviews_on_push"]
    f, _ = _run(tmp_path, _responses(rules=[rule], rulesets=[_ruleset()]))
    assert f["outcome"] == "unmeasured", f


def test_an_unreadable_ruleset_detail_is_unmeasured(tmp_path):
    """The rule came from the rules endpoint, which returns only active
    rules; who may bypass it is unread, so the check cannot pass."""
    f, _ = _run(tmp_path, _responses(rules=[_pr_rule(dismiss=True)]))
    assert f["outcome"] == "unmeasured", f
    assert "who may bypass ruleset 7 could not be read" in f["evidence"], f


def test_a_pass_discloses_an_unread_rules_endpoint(tmp_path):
    responses = _responses(classic=_classic(dismiss=True))
    responses[RULES] = _HTTP_502
    f, _ = _run(tmp_path, responses)
    assert f["outcome"] == "pass", f
    assert "Not read:" in f["evidence"], f


def test_classic_review_settings_of_unexpected_shape_are_unmeasured(tmp_path):
    body = _classic()
    body["required_pull_request_reviews"] = "on"
    f, _ = _run(tmp_path, _responses(classic=body))
    assert f["outcome"] == "unmeasured", f


def test_a_verdict_crash_costs_only_this_check(tmp_path):
    with mock.patch.object(cf, "_review_verdict",
                           side_effect=KeyError("surprise")):
        f, _ = _run(tmp_path, _responses())
    assert f["outcome"] == "unmeasured", f
    assert "could not be judged" in f["evidence"], f


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
