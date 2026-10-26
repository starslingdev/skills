"""Oracle tests for "if a push credential leaks": the two environment facts
and the unscored exposure inventory.

`sec.environments.branch-restricted` asks whether every deployment
environment either accepts only branches and tags that a non-admin push
credential can neither create nor move ("closed"), or requires a reviewer.
`sec.environments.approval-holds` asks whether every environment WITH
reviewers stops the person who started a run from approving it. The inventory
lists the repository- and organisation-level secret NAMES readable from any
branch.

Every test drives the REAL fetcher through the `_gh_utils` seam with recorded
answers, the same way the required-checks and CODEOWNERS tests do, so the
shipped endpoint paths and parsing are what gets exercised. Nothing here
touches the network. `fixtures/push-access/example-repo-settings.json` is an
invented repository, read with a non-member token, built to reach every
verdict path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import pytest

_TESTS = Path(__file__).resolve().parent
_SCRIPTS = _TESTS.parent / "scripts"
_EXAMPLE = (_TESTS / "fixtures" / "push-access"
            / "example-repo-settings.json")

BRANCH = "sec.environments.branch-restricted"
APPROVAL = "sec.environments.approval-holds"
SCOPED = "sec.secrets.environment-scoped"


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


# --- the recorded-answer harness ---------------------------------------------

class _Pages:
    """A `gh api --paginate` answer: every page's JSON, back to back, which is
    what gh prints for a paginated endpoint that returns an object."""

    def __init__(self, *pages):
        self.pages = pages

    def text(self):
        return "".join(json.dumps(p) for p in self.pages)


def _api_error(stderr: str) -> RuntimeError:
    # `gh_utils.run_gh_api` raises with this prefix plus gh's own stderr.
    return RuntimeError(f"API call failed: {stderr}")


_BRANCH_NOT_FOUND = _api_error("gh: Branch not found (HTTP 404)")
_BARE_404 = _api_error("gh: Not Found (HTTP 404)")
_SECRETS_403 = _api_error(
    "gh: You must have repository read permissions or have the repository "
    "secrets fine-grained permission. (HTTP 403)")
_ADMIN_403 = _api_error("gh: Must have admin rights to Repository. (HTTP 403)")
_HTTP_502 = _api_error("gh: Bad Gateway (HTTP 502)")


class _Gh:
    """A stubbed `gh` answering only the endpoints in `responses`; anything
    else is a 404 for a path no recording backs, so a mistyped path makes the
    fact unmeasured and the verdict assertions catch it."""

    def __init__(self, responses: dict, prereqs: bool = True):
        self.responses = responses
        self.prereqs = prereqs
        self.calls: list[tuple[str, dict]] = []

    def check_prereqs(self):
        return self.prereqs

    def run_gh_api(self, path, **kw):
        self.calls.append((path, kw))
        if path not in self.responses:
            raise _api_error(f"gh: Not Found (HTTP 404) [unrecorded {path}]")
        answer = self.responses[path]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, _Pages):
            return answer.text()
        if isinstance(answer, str):
            return answer
        return json.dumps(answer)

    def paths(self):
        return [p for p, _ in self.calls]


R = "repos/owner/repo"


def _env(name, policy=None, reviewers=0, prevent_self_review=False,
         admins=False):
    rules = []
    if policy is not None:
        rules.append({"type": "branch_policy"})
    if reviewers:
        rules.append({"type": "required_reviewers",
                      "prevent_self_review": prevent_self_review,
                      "reviewers": [{"type": "User",
                                     "reviewer": {"login": f"u{i}"}}
                                    for i in range(reviewers)]})
    return {"name": name, "can_admins_bypass": admins,
            "protection_rules": rules, "deployment_branch_policy": policy}


_ANY = None
_PROTECTED = {"protected_branches": True, "custom_branch_policies": False}
_CUSTOM = {"protected_branches": False, "custom_branch_policies": True}


def _dbp(name):
    return (f"{R}/environments/{quote(name, safe='')}"
            "/deployment-branch-policies?per_page=100")


def _ruleset(rid, include, rules, *, target="branch", enforcement="active",
             bypass=(), exclude=()):
    """A ruleset DETAIL. `bypass=None` leaves the key out, which is what a
    reader without admin rights is served."""
    body = {"id": rid, "name": f"rs{rid}", "target": target,
            "enforcement": enforcement, "source_type": "Repository",
            "conditions": {"ref_name": {"include": list(include),
                                        "exclude": list(exclude)}},
            "rules": [{"type": t} for t in rules]}
    if bypass is not None:
        body["bypass_actors"] = list(bypass)
    return body


def _graphql(permission="ADMIN", rules=()):
    return {"data": {"repository": {
        "viewerPermission": permission,
        "branchProtectionRules": {"totalCount": len(rules),
                                  "nodes": list(rules)}}}}


def _responses(envs, *, rulesets=(), classic=_graphql(), protected=(),
               repo_secrets=(), org_secrets=(), extra=None):
    admin = classic["data"]["repository"]["viewerPermission"] == "ADMIN"
    out = {
        R: {"default_branch": "main",
            "permissions": {"admin": admin, "pull": True}},
        f"{R}/environments?per_page=100": {"total_count": len(envs),
                                           "environments": list(envs)},
        f"{R}/rulesets?includes_parents=true&per_page=100": [
            {"id": r["id"], "name": r["name"]} for r in rulesets],
        "graphql": classic,
        f"{R}/branches?protected=true&per_page=100": [
            {"name": n, "protected": True} for n in protected],
        f"{R}/actions/secrets?per_page=100": {
            "total_count": len(repo_secrets),
            "secrets": [{"name": n} for n in repo_secrets]},
        f"{R}/actions/organization-secrets?per_page=100": {
            "total_count": len(org_secrets),
            "secrets": [{"name": n} for n in org_secrets]},
    }
    for r in rulesets:
        out[f"{R}/rulesets/{r['id']}?includes_parents=true"] = r
    out.update(extra or {})
    return out


def _run(responses, *, repo="owner/repo", prereqs=True, tmp_path):
    gh = _Gh(responses, prereqs=prereqs)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(_SAFE_WF)
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        "/.github/workflows/ @sec-team\n")
    with mock.patch.object(cf, "_gh_utils", lambda: gh):
        out = cf.compute_config_facts(
            tmp_path, [wf / "ci.yml"], [], repo=repo,
            required_contexts_fetcher=lambda r: ([], "branch `main`"),
            fork_approval_fetcher=lambda r: ("first_time_contributors", "x"),
            codeowner_review_fetcher=lambda r: (True, "a ruleset"))
    return out, gh


def _fact(out, fact_id):
    for f in out["facts"]:
        if f["fact_id"] == fact_id:
            return f
    raise AssertionError(f"{fact_id} missing from the fact table")


def _envs(out):
    return {e["name"]: e for e in out["push_access"]["environments"]}


# --- the example repository ---------------------------------------------------------

def _example_gh():
    doc = json.loads(_EXAMPLE.read_text())
    responses = {}
    for path, rec in doc["responses"].items():
        responses[path] = (rec["stdout"] if rec["status"] == "ok"
                           else _api_error(rec["stderr"]))
    return responses


_EXAMPLE_BRANCH_FAIL = {
    "nightly", "qa-east", "demo", "mobile-beta", "load-test", "sandbox",
    "qa-west",
}
_EXAMPLE_REVIEWED = {"artifact-signing", "release-train", "canary"}
_EXAMPLE_MANAGED = {"copilot", "github-pages"}


def test_the_example_repository(tmp_path):
    """The example repository, read the way a stranger reads it. Seven
    environments accept a branch any push credential can make; the three with
    reviewers pass the branch fact on those reviewers and fail the approval
    fact, because each lets the requester approve their own run; the two
    GitHub-managed environments are listed, never judged, because whether
    they hold secrets needs admin rights; and the secret inventory is
    unmeasured, never "0 secrets"."""
    out, _ = _run(_example_gh(), repo="example-org/example-repo", tmp_path=tmp_path)
    envs = _envs(out)
    assert set(envs) == (_EXAMPLE_BRANCH_FAIL | _EXAMPLE_REVIEWED
                         | _EXAMPLE_MANAGED), sorted(envs)

    assert {n for n, e in envs.items() if e["branch_check"] == "fail"} \
        == _EXAMPLE_BRANCH_FAIL
    assert {n for n, e in envs.items() if e["branch_check"] == "pass"} \
        == _EXAMPLE_REVIEWED
    assert {n for n, e in envs.items() if e["branch_check"] == "info"} \
        == _EXAMPLE_MANAGED
    assert {n for n, e in envs.items() if e["approval_check"] == "fail"} \
        == _EXAMPLE_REVIEWED

    branch, approval = _fact(out, BRANCH), _fact(out, APPROVAL)
    assert branch["outcome"] == "fail", branch
    assert approval["outcome"] == "fail", approval
    for name in _EXAMPLE_REVIEWED:
        assert name in approval["evidence"], approval

    # `mobile-beta` restricts to `trunk` and `ship-*`. A branch matching the
    # wildcard already exists with no protection, so any push credential
    # pushes a workflow onto it: an open pattern, proven from the settings
    # alone and independent of the unreadable classic rules.
    why = envs["mobile-beta"]["branch_check_why"]
    assert "`ship-ios`" in why, why
    # A ref with a `/` after the prefix does not match `ship-*`, and a
    # protected one proves nothing, so the proof names neither.
    assert "ship-/" not in why and "ship-android" not in why, why

    for name in _EXAMPLE_MANAGED:
        assert "GitHub-managed" in envs[name]["branch_check_why"], envs[name]

    inv = out["push_access"]["inventory"]
    assert inv["status"] == "unmeasured", inv
    assert inv["repo_secrets"] is None and inv["org_secrets"] is None, inv
    assert "0 secrets" not in json.dumps(inv)
    # The secret lists need secret-read access, so the scored secret check
    # is unmeasured here, never a pass.
    assert _fact(out, SCOPED)["outcome"] == "unmeasured", _fact(out, SCOPED)

    # Admin bypass is an information line, never part of either verdict.
    assert envs["qa-east"]["admins_can_bypass"] is True
    assert envs["nightly"]["admins_can_bypass"] is False


def test_the_example_repository_reads_only_recorded_endpoints(tmp_path):
    """Every path the engine asked for is one the recording answers — a
    mistyped path would read as 'unreadable' and hide behind the token
    caveat."""
    responses = _example_gh()
    out, gh = _run(responses, repo="example-org/example-repo", tmp_path=tmp_path)
    assert "repos/example-org/example-repo/environments?per_page=100" in gh.paths()
    unrecorded = [p for p in gh.paths() if p not in responses]
    assert not unrecorded, unrecorded


# --- "Protected branches only" ------------------------------------------------

# Probe P1 (sandbox, 2026-10-24) answered NO: only CLASSIC branch protection
# rules decide which branches a "Protected branches only" environment admits.
# A ruleset-only branch is rejected; with ZERO classic rules in the repository
# the environment admits every branch and every tag; one classic rule, even
# one matching nothing, makes it restrictive again. Classic rules are readable
# only by an admin.

_CLOSED_CLASSIC = {
    "pattern": "release/*", "blocksCreations": True,
    "restrictsPushes": True, "requiresApprovingReviews": True,
    "allowsDeletions": False, "pushAllowances": {"totalCount": 0},
    "bypassPullRequestAllowances": {"totalCount": 0}}


def test_protected_branches_with_zero_classic_rules_admits_everything(
        tmp_path):
    """A closed RULESET does not help: with no classic rule at all, the
    environment admits any branch and any tag, which is no restriction."""
    rs = _ruleset(1, ["refs/heads/stable/**"],
                  ["creation", "deletion", "pull_request"])
    out, _ = _run(_responses([_env("release", _PROTECTED)], rulesets=[rs]),
                  tmp_path=tmp_path)
    env = _envs(out)["release"]
    assert env["branch_check"] == "fail", env
    assert "every branch and tag" in env["branch_check_why"], env
    assert _fact(out, BRANCH)["outcome"] == "fail"


def test_one_closed_classic_rule_matching_nothing_is_restrictive(tmp_path):
    rule = dict(_CLOSED_CLASSIC, pattern="ghost-nomatch/*")
    out, _ = _run(_responses([_env("release", _PROTECTED)],
                             classic=_graphql("ADMIN", [rule])),
                  tmp_path=tmp_path)
    assert _envs(out)["release"]["branch_check"] == "pass", _envs(out)


def test_a_ruleset_does_not_close_a_classic_pattern(tmp_path):
    """Only the classic rule decides here: a ruleset blocking creation on
    the same pattern is not counted (that combination is untested)."""
    rule = dict(_CLOSED_CLASSIC, blocksCreations=False)
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"])
    out, _ = _run(_responses([_env("release", _PROTECTED)], rulesets=[rs],
                             classic=_graphql("ADMIN", [rule])),
                  tmp_path=tmp_path)
    assert _envs(out)["release"]["branch_check"] == "fail", _envs(out)


def test_probe_p1_is_one_switch(tmp_path, monkeypatch):
    """Probe P1 answered NO, and that answer is one constant. Flipped back
    to YES, a closed ruleset pattern would join the protected set and the
    same settings would pass."""
    assert cf.P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED is False
    rs = _ruleset(1, ["refs/heads/stable/**"],
                  ["creation", "deletion", "pull_request"])
    responses = _responses([_env("release", _PROTECTED)], rulesets=[rs],
                           classic=_graphql("ADMIN", [dict(
                               _CLOSED_CLASSIC, pattern="ghost/*")]))
    off, _ = _run(responses, tmp_path=tmp_path / "off")
    assert _envs(off)["release"]["branch_check"] == "pass", _envs(off)
    rs_open = _ruleset(1, ["refs/heads/stable/**"],
                       ["deletion", "pull_request"])
    responses = _responses([_env("release", _PROTECTED)], rulesets=[rs_open],
                           classic=_graphql("ADMIN", [dict(
                               _CLOSED_CLASSIC, pattern="ghost/*")]))
    off, _ = _run(responses, tmp_path=tmp_path / "off-open")
    assert _envs(off)["release"]["branch_check"] == "pass", _envs(off)
    monkeypatch.setattr(cf, "P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED",
                        True)
    on, _ = _run(responses, tmp_path=tmp_path / "on")
    assert _envs(on)["release"]["branch_check"] == "fail", _envs(on)


def test_protected_branches_with_classic_rules_unreadable_is_unmeasured(
        tmp_path):
    """A non-admin reader is answered with an empty classic list, which is
    NOT "none" (recorded on a repository with 100+ classic-protected
    branches). The verdict depends on the rules, so it is unmeasured with
    the reason: never a pass, and never the zero-rules fail either."""
    out, gh = _run(_responses([_env("release", _PROTECTED)],
                              classic=_graphql("READ")), tmp_path=tmp_path)
    env = _envs(out)["release"]
    assert env["branch_check"] == "unmeasured", env
    assert "admin" in env["branch_check_why"], env
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"
    # Admin rights are read from the repository's own permissions first, so
    # a non-admin never spends the GraphQL call.
    assert "graphql" not in gh.paths()


def test_admin_rights_are_read_from_the_repository_permissions(tmp_path):
    """GraphQL reporting ADMIN is not enough on its own: the repository
    endpoint's `permissions.admin` must say so too."""
    responses = _responses([_env("release", _PROTECTED)],
                           classic=_graphql("ADMIN", [_CLOSED_CLASSIC]))
    responses[R] = {"default_branch": "main",
                    "permissions": {"admin": False, "pull": True}}
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _envs(out)["release"]["branch_check"] == "unmeasured", _envs(out)


def test_classic_rule_without_creation_restriction_is_open(tmp_path):
    classic = _graphql("ADMIN", [{
        "pattern": "release/*", "blocksCreations": False,
        "restrictsPushes": True, "requiresApprovingReviews": True,
        "allowsDeletions": False, "pushAllowances": {"totalCount": 0},
        "bypassPullRequestAllowances": {"totalCount": 0}}])
    out, _ = _run(_responses([_env("ship", _PROTECTED)], classic=classic),
                  tmp_path=tmp_path)
    assert _envs(out)["ship"]["branch_check"] == "fail", _envs(out)


def test_classic_rule_that_blocks_creation_and_pushes_is_closed(tmp_path):
    classic = _graphql("ADMIN", [{
        "pattern": "release/*", "blocksCreations": True,
        "restrictsPushes": True, "requiresApprovingReviews": True,
        "allowsDeletions": False, "pushAllowances": {"totalCount": 0},
        "bypassPullRequestAllowances": {"totalCount": 0}}])
    out, _ = _run(_responses([_env("ship", _PROTECTED)], classic=classic),
                  tmp_path=tmp_path)
    assert _envs(out)["ship"]["branch_check"] == "pass", _envs(out)


def test_classic_push_allowance_opens_the_pattern(tmp_path):
    """Someone on the push-allowance list walks through the restriction,
    and a leaked credential of theirs does too."""
    classic = _graphql("ADMIN", [{
        "pattern": "release/*", "blocksCreations": True,
        "restrictsPushes": True, "requiresApprovingReviews": True,
        "allowsDeletions": False, "pushAllowances": {"totalCount": 1},
        "bypassPullRequestAllowances": {"totalCount": 0}}])
    out, _ = _run(_responses([_env("ship", _PROTECTED)], classic=classic),
                  tmp_path=tmp_path)
    assert _envs(out)["ship"]["branch_check"] == "fail", _envs(out)


# --- custom policies: exact names, wildcards, tags ----------------------------

def _custom_case(tmp_path, patterns, rulesets=(), extra=None, envname="prod"):
    extra = dict(extra or {})
    extra[_dbp(envname)] = {"total_count": len(patterns), "branch_policies": [
        {"name": n, "type": t} for n, t in patterns]}
    return _run(_responses([_env(envname, _CUSTOM)], rulesets=rulesets,
                           extra=extra), tmp_path=tmp_path)


def test_an_exact_name_that_does_not_exist_yet_without_creation_block_fails(
        tmp_path):
    """`deploy` is protected against direct pushes, but no branch of that
    name exists, and nothing blocks creating one: whoever creates it first
    decides what is on it."""
    rs = _ruleset(1, ["refs/heads/deploy"], ["update", "deletion"])
    out, gh = _custom_case(tmp_path, [("deploy", "branch")], [rs], extra={
        f"{R}/branches/deploy": _BRANCH_NOT_FOUND})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)
    assert "does not exist" in _envs(out)["prod"]["branch_check_why"]
    assert f"{R}/branches/deploy" in gh.paths()


def test_an_exact_name_that_does_not_exist_yet_with_creation_block_passes(
        tmp_path):
    rs = _ruleset(1, ["refs/heads/deploy"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("deploy", "branch")], [rs], extra={
        f"{R}/branches/deploy": _BRANCH_NOT_FOUND})
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_an_existing_exact_name_needs_updates_blocked(tmp_path):
    rs = _ruleset(1, ["~DEFAULT_BRANCH"], ["pull_request", "deletion"])
    rs["rules"][0]["parameters"] = {"required_approving_review_count": 1,
                                    "dismiss_stale_reviews_on_push": True}
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs], extra={
        f"{R}/branches/main": {"name": "main", "protected": True}})
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_an_existing_branch_that_can_be_deleted_and_recreated_is_open(
        tmp_path):
    """Pull requests are required on `main`, but nothing stops deleting it
    and nothing stops creating it again with a different history."""
    rs = _ruleset(1, ["~DEFAULT_BRANCH"], ["pull_request"])
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs], extra={
        f"{R}/branches/main": {"name": "main", "protected": True}})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_an_existing_unprotected_exact_name_fails(tmp_path):
    out, _ = _custom_case(tmp_path, [("deploy", "branch")], extra={
        f"{R}/branches/deploy": {"name": "deploy", "protected": False}})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_custom_wildcard_without_a_creation_ruleset_fails(tmp_path):
    rs = _ruleset(1, ["refs/heads/release/*"], ["update", "deletion"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_custom_wildcard_with_a_creation_ruleset_passes(tmp_path):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_a_broader_ruleset_pattern_covers_the_wildcard(tmp_path):
    rs = _ruleset(1, ["~ALL"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_a_ruleset_exclude_that_may_overlap_uncovers_the_wildcard(tmp_path):
    rs = _ruleset(1, ["~ALL"], ["creation", "update"],
                  exclude=["refs/heads/release/hot*"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


@pytest.mark.parametrize("actor", [
    {"actor_id": 9, "actor_type": "Integration", "bypass_mode": "always"},
    {"actor_id": 3, "actor_type": "DeployKey", "bypass_mode": "always"},
    {"actor_id": 7, "actor_type": "Team", "bypass_mode": "always"},
    {"actor_id": 2, "actor_type": "RepositoryRole", "bypass_mode": "always"},
], ids=["app", "deploy-key", "team", "write-role"])
def test_a_creation_ruleset_with_a_non_admin_bypass_actor_is_open(
        tmp_path, actor):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  bypass=[actor])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_an_admin_only_bypass_keeps_the_pattern_closed(tmp_path):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  bypass=[{"actor_id": 5, "actor_type": "RepositoryRole",
                           "bypass_mode": "always"},
                          {"actor_id": None, "actor_type": "OrganizationAdmin",
                           "bypass_mode": "always"}])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_unreadable_bypass_actors_cannot_prove_closed(tmp_path):
    """A reader without admin rights is served rulesets with no
    `bypass_actors` key at all. Closed then cannot be shown; the
    environment is unmeasured, not passed."""
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  bypass=None)
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_an_existing_unprotected_branch_matching_the_wildcard_fails(tmp_path):
    """No ruleset is needed to prove this one: a branch matching the
    accepted pattern already exists and nothing protects it."""
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], extra={
        f"{R}/git/matching-refs/heads/release/": [
            {"ref": "refs/heads/release/old"}],
        "graphql": _graphql("READ")})
    env = _envs(out)["prod"]
    assert env["branch_check"] == "fail", env
    assert "release/old" in env["branch_check_why"], env


def test_a_custom_tag_pattern_needs_a_closed_tag_ruleset(tmp_path):
    open_rs = _ruleset(1, ["refs/tags/v*"], ["update", "deletion"],
                       target="tag")
    out, _ = _custom_case(tmp_path, [("v*", "tag")], [open_rs])
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_custom_tag_pattern_with_a_closed_tag_ruleset_passes(tmp_path):
    closed = _ruleset(1, ["refs/tags/v*"], ["creation", "update"],
                      target="tag")
    out, _ = _custom_case(tmp_path, [("v*", "tag")], [closed])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_a_branch_ruleset_does_not_close_a_tag_pattern(tmp_path):
    branch_rs = _ruleset(1, ["~ALL"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("v*", "tag")], [branch_rs])
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled"])
def test_a_ruleset_not_actively_enforced_counts_for_nothing(
        tmp_path, enforcement):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  enforcement=enforcement)
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_an_organisation_ruleset_counts(tmp_path):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"])
    rs["source_type"] = "Organization"
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


# --- reviewers, self-review, admin bypass -------------------------------------

def test_reviewers_with_self_review_allowed_fail_only_the_approval_fact(
        tmp_path):
    """One bad setting costs one check: the reviewers satisfy the branch
    fact, and only the approval fact fails."""
    out, _ = _run(_responses([_env("prod", _ANY, reviewers=2)]),
                  tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "pass", _fact(out, BRANCH)
    approval = _fact(out, APPROVAL)
    assert approval["outcome"] == "fail", approval
    assert "prod" in approval["evidence"]


def test_reviewers_that_block_self_review_pass_both(tmp_path):
    out, _ = _run(_responses([_env("prod", _ANY, reviewers=1,
                                   prevent_self_review=True)]),
                  tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "pass"
    assert _fact(out, APPROVAL)["outcome"] == "pass"


def test_admin_bypass_is_information_not_a_verdict(tmp_path):
    out, _ = _run(_responses([_env("prod", _ANY, reviewers=1,
                                   prevent_self_review=True, admins=True)]),
                  tmp_path=tmp_path)
    assert _fact(out, APPROVAL)["outcome"] == "pass"
    assert _envs(out)["prod"]["admins_can_bypass"] is True


def test_no_environment_with_reviewers_leaves_the_approval_fact_out(tmp_path):
    out, _ = _run(_responses([_env("prod", _ANY)]), tmp_path=tmp_path)
    assert _fact(out, APPROVAL)["outcome"] == "not_applicable"
    assert APPROVAL in out["not_applicable"]


def test_an_environment_with_no_secrets_and_no_restriction_fails(tmp_path):
    """An environment's name is what a cloud login trusts, so one holding no
    secrets is in scope like any other."""
    out, _ = _run(_responses([_env("prod", _ANY)]), tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "fail"
    assert _fact(out, BRANCH)["outcome"] == "fail"


# --- GitHub-managed environments ----------------------------------------------

@pytest.mark.parametrize("managed", ["github-pages", "copilot"])
def test_managed_environment_with_unreadable_secrets_is_listed_not_judged(
        tmp_path, managed):
    out, _ = _run(_responses(
        [_env(managed, _ANY), _env("prod", _ANY, reviewers=1,
                                    prevent_self_review=True)],
        extra={f"{R}/environments/{managed}/secrets?per_page=100":
               _ADMIN_403}), tmp_path=tmp_path)
    env = _envs(out)[managed]
    assert env["branch_check"] == "info", env
    assert "can't tell" in env["branch_check_why"], env
    # It accepts any branch and may hold secrets, so the check cannot pass.
    assert _fact(out, BRANCH)["outcome"] == "unmeasured", _fact(out, BRANCH)


def test_a_managed_environment_that_would_pass_anyway_does_not_block_a_pass(
        tmp_path):
    """Its secrets cannot be read, but it requires a reviewer, so whether it
    holds secrets cannot change the verdict."""
    out, _ = _run(_responses(
        [_env("github-pages", _ANY, reviewers=1, prevent_self_review=True),
         _env("prod", _ANY, reviewers=1, prevent_self_review=True)],
        extra={f"{R}/environments/github-pages/secrets?per_page=100":
               _ADMIN_403}), tmp_path=tmp_path)
    assert _envs(out)["github-pages"]["branch_check"] == "info"
    assert _fact(out, BRANCH)["outcome"] == "pass", _fact(out, BRANCH)


def test_managed_environment_holding_a_secret_is_judged(tmp_path):
    out, _ = _run(_responses([_env("github-pages", _ANY)], extra={
        f"{R}/environments/github-pages/secrets?per_page=100":
            {"total_count": 1, "secrets": [{"name": "PAGES_KEY"}]}}),
        tmp_path=tmp_path)
    assert _envs(out)["github-pages"]["branch_check"] == "fail"


def test_managed_environment_holding_no_secret_is_out_of_scope(tmp_path):
    out, _ = _run(_responses(
        [_env("github-pages", _ANY), _env("prod", _ANY, reviewers=1,
                                           prevent_self_review=True)],
        extra={f"{R}/environments/github-pages/secrets?per_page=100":
               {"total_count": 0, "secrets": []}}), tmp_path=tmp_path)
    assert _envs(out)["github-pages"]["branch_check"] == "info"
    assert _fact(out, BRANCH)["outcome"] == "pass"


# --- unreadable sources, 404s, pagination, errors -----------------------------

def test_rulesets_unreadable_makes_dependent_environments_unmeasured(
        tmp_path):
    responses = _responses([_env("prod", _CUSTOM)], extra={
        _dbp("prod"): {"total_count": 1, "branch_policies": [
            {"name": "release/*", "type": "branch"}]},
        f"{R}/git/matching-refs/heads/release/": []})
    responses[f"{R}/rulesets?includes_parents=true&per_page=100"] = _HTTP_502
    out, _ = _run(responses, tmp_path=tmp_path)
    env = _envs(out)["prod"]
    assert env["branch_check"] == "unmeasured", env
    assert "rulesets" in env["branch_check_why"], env


def test_an_unrestricted_environment_still_fails_with_rulesets_unreadable(
        tmp_path):
    responses = _responses([_env("prod", _ANY)])
    responses[f"{R}/rulesets?includes_parents=true&per_page=100"] = _HTTP_502
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "fail"


def test_environments_404_is_unmeasured_not_zero_environments(tmp_path):
    """P6: a 404 on the environments list is a wrong repository or path,
    not "no environments" (which GitHub answers with an empty list)."""
    responses = _responses([])
    responses[f"{R}/environments?per_page=100"] = _BARE_404
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"
    assert _fact(out, APPROVAL)["outcome"] == "unmeasured"


def test_deployment_branch_policies_404_on_a_custom_policy_is_unmeasured(
        tmp_path):
    responses = _responses([_env("prod", _CUSTOM)], extra={
        _dbp("prod"): _BARE_404})
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_only_branch_not_found_means_a_branch_does_not_exist(tmp_path):
    """P6, recorded: the branch endpoint answers 404 "Branch not found" for
    a missing branch. A bare 404 is not that answer."""
    rs = _ruleset(1, ["refs/heads/deploy"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("deploy", "branch")], [rs], extra={
        f"{R}/branches/deploy": _BARE_404})
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


_P6 = json.loads((_TESTS / "fixtures" / "push-access"
                  / "p6-not-found-2026-10-24.json").read_text())["cases"]


def _recorded(case: str):
    """The recorded answer for one P6 case, as `gh` hands it back: the body
    on a 200, or `gh: <message> (HTTP <status>)` on an error."""
    rec = _P6[case]
    if rec["status"] == 200:
        return rec["body"]
    return _api_error(f"gh: {rec['message']} (HTTP {rec['status']})")


def test_p6_recorded_branch_not_found_means_it_does_not_exist(tmp_path):
    rs = _ruleset(1, ["refs/heads/deploy"], ["update"])
    out, _ = _custom_case(tmp_path, [("deploy", "branch")], [rs], extra={
        f"{R}/branches/deploy": _recorded("branch-does-not-exist")})
    env = _envs(out)["prod"]
    assert env["branch_check"] == "fail", env
    assert "does not exist" in env["branch_check_why"], env


@pytest.mark.parametrize("case", ["dbp-policy-null", "dbp-protected-branches",
                                  "dbp-environment-missing"])
def test_p6_recorded_policy_404s_on_a_custom_environment_are_unmeasured(
        tmp_path, case):
    """GitHub answers the same 404 for no policy, protected-branches-only and
    a missing environment, so on an environment whose list reports a custom
    policy it can only mean the read failed."""
    out, _ = _run(_responses([_env("prod", _CUSTOM)], extra={
        _dbp("prod"): _recorded(case)}), tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


@pytest.mark.parametrize("env_policy", [_ANY, _PROTECTED],
                         ids=["policy-null", "protected-branches"])
def test_the_policy_list_is_never_asked_for_a_non_custom_environment(
        tmp_path, env_policy):
    out, gh = _run(_responses([_env("prod", env_policy)]), tmp_path=tmp_path)
    # The environment was read and judged, so the absence below means the
    # engine chose not to ask, not that it never got that far.
    assert f"{R}/environments?per_page=100" in gh.paths(), gh.paths()
    assert _envs(out)["prod"]["branch_check"] in ("pass", "fail", "unmeasured")
    assert _dbp("prod") not in gh.paths(), gh.paths()


def test_p6_recorded_wrong_repository_environments_404_is_unmeasured(
        tmp_path):
    responses = _responses([])
    responses[f"{R}/environments?per_page=100"] = _recorded(
        "environments-wrong-repo")
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"


def test_p6_recorded_empty_answers_are_answers(tmp_path):
    responses = _responses([])
    responses[f"{R}/environments?per_page=100"] = _recorded(
        "environments-none")
    responses[f"{R}/actions/secrets?per_page=100"] = _recorded("secrets-none")
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "not_applicable"
    assert out["push_access"]["inventory"]["repo_secrets"] == []


@pytest.mark.parametrize("case", ["secrets-wrong-repo",
                                  "secrets-non-collaborator",
                                  "secrets-anonymous"])
def test_p6_recorded_secret_list_failures_are_unmeasured(tmp_path, case):
    responses = _responses([])
    responses[f"{R}/actions/secrets?per_page=100"] = _recorded(case)
    responses[f"{R}/actions/organization-secrets?per_page=100"] = \
        _recorded(case)
    out, _ = _run(responses, tmp_path=tmp_path)
    assert out["push_access"]["inventory"]["status"] == "unmeasured"
    assert _fact(out, SCOPED)["outcome"] == "unmeasured"


def test_p6_recorded_rulesets_404_is_unmeasured(tmp_path):
    responses = _responses([_env("prod", _CUSTOM)], extra={
        _dbp("prod"): {"total_count": 1, "branch_policies": [
            {"name": "release/*", "type": "branch"}]},
        f"{R}/git/matching-refs/heads/release/": []})
    responses[f"{R}/rulesets?includes_parents=true&per_page=100"] = \
        _recorded("rulesets-wrong-repo")
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_secret_list_404_is_unmeasured_not_empty(tmp_path):
    responses = _responses([])
    responses[f"{R}/actions/secrets?per_page=100"] = _BARE_404
    out, _ = _run(responses, tmp_path=tmp_path)
    assert out["push_access"]["inventory"]["status"] in ("unmeasured",
                                                         "partial")
    assert out["push_access"]["inventory"]["repo_secrets"] is None


def test_more_than_one_page_of_environments_is_read_in_full(tmp_path):
    responses = _responses([])
    responses[f"{R}/environments?per_page=100"] = _Pages(
        {"total_count": 2, "environments": [
            _env("a", _ANY, reviewers=1, prevent_self_review=True)]},
        {"total_count": 2, "environments": [_env("b", _ANY)]})
    out, gh = _run(responses, tmp_path=tmp_path)
    assert set(_envs(out)) == {"a", "b"}
    assert _envs(out)["b"]["branch_check"] == "fail"
    kw = dict(gh.calls)[f"{R}/environments?per_page=100"]
    assert kw.get("paginate") is True, kw


def test_list_endpoints_are_paginated(tmp_path):
    out, gh = _run(_example_gh(), repo="example-org/example-repo", tmp_path=tmp_path)
    calls = dict(gh.calls)
    R2 = "repos/example-org/example-repo"
    for path in (f"{R2}/environments?per_page=100",
                 f"{R2}/rulesets?includes_parents=true&per_page=100",
                 _dbp("mobile-beta").replace(R, R2),
                 f"{R2}/actions/secrets?per_page=100",
                 f"{R2}/actions/organization-secrets?per_page=100"):
        assert calls.get(path, {}).get("paginate") is True, (path, calls)


def test_an_api_5xx_is_unmeasured_with_the_reason_and_no_retry(tmp_path):
    responses = _responses([])
    responses[f"{R}/environments?per_page=100"] = _HTTP_502
    out, gh = _run(responses, tmp_path=tmp_path)
    branch = _fact(out, BRANCH)
    assert branch["outcome"] == "unmeasured", branch
    assert "502" in branch["evidence"], branch
    assert gh.paths().count(f"{R}/environments?per_page=100") == 1


def test_no_token_is_unmeasured_and_says_so(tmp_path):
    out, gh = _run(_responses([]), prereqs=False, tmp_path=tmp_path)
    for fid in (BRANCH, APPROVAL):
        f = _fact(out, fid)
        assert f["outcome"] == "unmeasured", f
        assert "gh auth login" in f["evidence"], f
    assert out["push_access"]["inventory"]["status"] == "not_checked"
    assert gh.paths() == []


def test_no_repository_is_unmeasured_without_any_call(tmp_path):
    out, gh = _run(_responses([]), repo=None, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"
    assert _fact(out, APPROVAL)["outcome"] == "unmeasured"
    assert out["push_access"]["status"] == "not_checked"
    assert gh.paths() == []


# --- zero environments ---------------------------------------------------------
#
# D4 is retired (owner, 2026-10-24): with `sec.secrets.environment-scoped`
# scoring readable repo/org secrets on its own, a repository with no
# environments leaves the branch-restriction check NOT APPLICABLE whatever
# its secrets are, so one setup never fails two checks.

def test_zero_environments_with_a_readable_secret_fails_only_the_secret_check(
        tmp_path):
    out, _ = _run(_responses([], repo_secrets=["NPM_TOKEN"]),
                  tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "not_applicable", _fact(out, BRANCH)
    assert BRANCH in out["not_applicable"]
    assert _fact(out, SCOPED)["outcome"] == "fail", _fact(out, SCOPED)


def test_zero_environments_with_an_org_secret_fails_only_the_secret_check(
        tmp_path):
    out, _ = _run(_responses([], org_secrets=["SHARED_KEY"]),
                  tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "not_applicable"
    assert _fact(out, SCOPED)["outcome"] == "fail"


def test_zero_environments_and_no_secrets(tmp_path):
    out, _ = _run(_responses([]), tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "not_applicable"
    assert BRANCH in out["not_applicable"]
    assert _fact(out, APPROVAL)["outcome"] == "not_applicable"
    assert _fact(out, SCOPED)["outcome"] == "pass"


def test_zero_environments_with_secrets_unreadable(tmp_path):
    responses = _responses([])
    responses[f"{R}/actions/secrets?per_page=100"] = _SECRETS_403
    responses[f"{R}/actions/organization-secrets?per_page=100"] = _SECRETS_403
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "not_applicable"
    assert _fact(out, SCOPED)["outcome"] == "unmeasured"


# --- sec.secrets.environment-scoped ---------------------------------------------
#
# Scored (owner, 2026-10-24): PASS when no repository- or organisation-level
# Actions secret is readable by the repository, which includes having no
# secrets at all; FAIL when one is, naming them; UNMEASURED when a list cannot
# be read, unless the list that was read already proves the fail.

_CLOSED_ENV = _env("prod", _ANY, reviewers=1, prevent_self_review=True)


def test_no_repo_or_org_secret_passes(tmp_path):
    out, _ = _run(_responses([_CLOSED_ENV]), tmp_path=tmp_path)
    f = _fact(out, SCOPED)
    assert f["outcome"] == "pass", f
    assert SCOPED not in out["unmeasured"]


def test_a_repo_secret_fails_and_says_what_must_be_done(tmp_path):
    out, _ = _run(_responses([_CLOSED_ENV],
                             repo_secrets=["CODECOV_TOKEN", "PYPI_TOKEN"]),
                  tmp_path=tmp_path)
    f = _fact(out, SCOPED)
    assert f["outcome"] == "fail", f
    ev = f["evidence"]
    assert "CODECOV_TOKEN" in ev and "PYPI_TOKEN" in ev, ev
    # The likely high-value one is listed first.
    assert ev.index("PYPI_TOKEN") < ev.index("CODECOV_TOKEN"), ev
    # Plain steps: an environment that passes the branch check, the value
    # re-entered there by the user (GitHub never shows it), the old copy
    # deleted.
    assert "environment" in ev and "re-enter" in ev and "delete" in ev, ev
    assert "never" in ev and "value" in ev, ev


def test_an_org_secret_fails_and_offers_narrowing_its_access(tmp_path):
    out, _ = _run(_responses([_CLOSED_ENV], org_secrets=["SHARED_KEY"]),
                  tmp_path=tmp_path)
    f = _fact(out, SCOPED)
    assert f["outcome"] == "fail", f
    assert "organisation" in f["evidence"], f
    assert "repository access" in f["evidence"], f


def test_both_secret_lists_unreadable_is_unmeasured(tmp_path):
    responses = _responses([_CLOSED_ENV])
    responses[f"{R}/actions/secrets?per_page=100"] = _SECRETS_403
    responses[f"{R}/actions/organization-secrets?per_page=100"] = _SECRETS_403
    out, _ = _run(responses, tmp_path=tmp_path)
    f = _fact(out, SCOPED)
    assert f["outcome"] == "unmeasured", f
    assert "403" in f["evidence"], f
    assert SCOPED in out["unmeasured"]


def test_one_list_unreadable_and_the_other_holding_a_secret_still_fails(
        tmp_path):
    responses = _responses([_CLOSED_ENV], repo_secrets=["RELEASE_TOKEN"])
    responses[f"{R}/actions/organization-secrets?per_page=100"] = _SECRETS_403
    out, _ = _run(responses, tmp_path=tmp_path)
    f = _fact(out, SCOPED)
    assert f["outcome"] == "fail", f
    assert "could not be read" in f["evidence"], f


def test_one_list_unreadable_and_the_other_empty_is_unmeasured(tmp_path):
    responses = _responses([_CLOSED_ENV])
    responses[f"{R}/actions/organization-secrets?per_page=100"] = _SECRETS_403
    out, _ = _run(responses, tmp_path=tmp_path)
    assert _fact(out, SCOPED)["outcome"] == "unmeasured"


def test_the_secret_check_without_a_repository_or_token_is_unmeasured(
        tmp_path):
    out, _ = _run(_responses([]), repo=None, tmp_path=tmp_path / "norepo")
    assert _fact(out, SCOPED)["outcome"] == "unmeasured"
    out, _ = _run(_responses([]), prereqs=False, tmp_path=tmp_path / "notok")
    f = _fact(out, SCOPED)
    assert f["outcome"] == "unmeasured" and "gh auth login" in f["evidence"]


# --- the exposure inventory ----------------------------------------------------

def test_inventory_lists_names_grouped_with_a_high_value_hint(tmp_path):
    out, _ = _run(_responses(
        [_env("prod", _ANY, reviewers=1, prevent_self_review=True)],
        repo_secrets=["CODECOV_TOKEN", "PYPI_TOKEN"],
        org_secrets=["RELEASE_APP_PRIVATE_KEY"]), tmp_path=tmp_path)
    inv = out["push_access"]["inventory"]
    assert inv["status"] == "measured", inv
    assert inv["repo_secrets"] == ["CODECOV_TOKEN", "PYPI_TOKEN"]
    assert inv["org_secrets"] == ["RELEASE_APP_PRIVATE_KEY"]
    assert inv["high_value"] == ["PYPI_TOKEN", "RELEASE_APP_PRIVATE_KEY"]
    # Unscored: the inventory never becomes a fact row.
    assert all("inventory" not in f["fact_id"] for f in out["facts"])


def test_inventory_with_one_list_unreadable_is_partial(tmp_path):
    responses = _responses([_env("prod", _ANY, reviewers=1,
                                 prevent_self_review=True)],
                           repo_secrets=["A"])
    responses[f"{R}/actions/organization-secrets?per_page=100"] = _SECRETS_403
    out, _ = _run(responses, tmp_path=tmp_path)
    inv = out["push_access"]["inventory"]
    assert inv["status"] == "partial", inv
    assert inv["repo_secrets"] == ["A"] and inv["org_secrets"] is None


def test_every_run_states_the_tier_one_gap_and_the_token_sentence(tmp_path):
    for repo in ("owner/repo", None):
        out, _ = _run(_responses([]), repo=repo, tmp_path=tmp_path / str(
            bool(repo)))
        block = out["push_access"]
        assert "workflow files" in block["tier1_gap"], block
        assert "OIDC" in block["credential_note"], block


# --- the report surface --------------------------------------------------------

def _load_report():
    if "ci_secure_report" in sys.modules:
        return sys.modules["ci_secure_report"]
    saved_config = sys.modules.pop("config", None)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "ci_secure_report", _SCRIPTS / "report.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ci_secure_report"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        try:
            sys.path.remove(str(_SCRIPTS))
        except ValueError:                      # pragma: no cover
            pass
        sys.modules.pop("config", None)
        if saved_config is not None:
            sys.modules["config"] = saved_config


def test_report_renders_the_push_credential_sub_heading(tmp_path):
    out, _ = _run(_example_gh(), repo="example-org/example-repo", tmp_path=tmp_path)
    md = _load_report().render({"findings": [], "repo": "example-org/example-repo",
                                "scanned_workflows": 1,
                                "security_score": out})
    assert "### If a push credential leaks" in md
    head, sub = md.split("### If a push credential leaks", 1)
    # The two environment rows live under the sub-heading, not the main table.
    main_table = head.split("## 🧰 Config hygiene checks", 1)[1]
    assert "deployment environment" not in main_table
    assert "deployment environment" in sub
    for name in ("mobile-beta", "artifact-signing", "github-pages"):
        assert name in sub, name
    # The example's secret lists are unreadable: said as a gap, never a count.
    assert 'coverage gap, not "no secrets"' in sub, sub
    assert "secret(s)" not in sub, sub
    # The tier-1 gap, stated in one line.
    assert "cannot push workflow files" in sub
    # The secret check is the third row under the sub-heading, and the
    # inventory list sits under it.
    # (The ids can appear in the main section's "could not be measured"
    # line; the ROW is what must sit under the sub-heading.)
    assert "lives in an environment" not in main_table
    assert "lives in an environment" in sub
    assert "repository or organisation secret" in sub
    # Token tiers cite GitHub's own docs, and say nothing about deploy keys.
    for url in (
        "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/"
        "scopes-for-oauth-apps",
        "https://docs.github.com/en/apps/creating-github-apps/"
        "registering-a-github-app/choosing-permissions-for-a-github-app",
        "https://docs.github.com/en/rest/authentication/"
        "permissions-required-for-fine-grained-personal-access-tokens"):
        assert url in sub, url
    assert "deploy key" not in sub.lower()
    # No agent prompts: ci-secure already runs inside the user's agent.
    assert "paste" not in sub.lower() and "prompt" not in sub.lower()
    # Never a score.
    assert "/100" not in md and "Security score" not in md


def test_admin_bypass_note_only_where_there_are_rules_to_skip(tmp_path):
    """`can_admins_bypass` is true on environments with no branch policy and
    no reviewers too. With nothing to skip, "admins can skip these rules"
    is noise that reads as a contradiction, so it is shown only where a
    branch policy or required reviewers exist."""
    out, _ = _run(_example_gh(), repo="example-org/example-repo", tmp_path=tmp_path)
    md = _load_report().render({"findings": [], "repo": "example-org/example-repo",
                                "scanned_workflows": 1,
                                "security_score": out})
    rows = {line.split("|")[1].strip(): line for line in md.splitlines()
            if line.startswith("| `")}
    for bare in ("`qa-east`", "`sandbox`", "`nightly`", "`copilot`",
                 "`github-pages`"):
        assert "admins can skip" not in rows[bare], rows[bare]
    for ruled in ("`artifact-signing`", "`mobile-beta`", "`canary`"):
        assert "admins can skip" in rows[ruled], rows[ruled]


# --- what a pull request, a bypass mode and a pattern really allow ------------

def _closed_main(pr_params, bypass=()):
    rs = _ruleset(1, ["~DEFAULT_BRANCH"], ["pull_request", "deletion"],
                  bypass=bypass)
    for r in rs["rules"]:
        if r["type"] == "pull_request" and pr_params is not None:
            r["parameters"] = pr_params
    return rs


def _main_exists():
    return {f"{R}/branches/main": {"name": "main", "protected": True}}


def test_a_pull_request_rule_with_zero_approvals_does_not_block_updates(
        tmp_path):
    """A pull_request rule needing no approval: the leaked credential opens a
    pull request from its own branch and merges it itself."""
    rs = _closed_main({"required_approving_review_count": 0})
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs],
                          extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_pull_request_rule_without_parameters_is_unmeasured(tmp_path):
    """How many approvals the rule needs is unknown, so whether it blocks
    updates is too: unmeasured, never passed."""
    rs = _closed_main(None)
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs],
                          extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_a_pull_request_mode_bypass_actor_can_still_update(tmp_path):
    """A non-admin team that may bypass "through a pull request" merges its
    own unapproved pull request into the accepted branch."""
    rs = _ruleset(1, ["~DEFAULT_BRANCH"], ["update", "deletion"],
                  bypass=[{"actor_id": 7, "actor_type": "Team",
                           "bypass_mode": "pull_request"}])
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs],
                          extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_pull_request_mode_bypass_actor_cannot_create_or_delete(tmp_path):
    """A pull request can neither create nor delete a ref, so that actor
    leaves the creation block intact."""
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  bypass=[{"actor_id": 7, "actor_type": "Team",
                           "bypass_mode": "pull_request"}])
    rs_upd = _ruleset(2, ["refs/heads/release/*"], ["update"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs, rs_upd])
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_classic_double_star_does_not_cross_slash(tmp_path):
    """Classic protection matches with File.fnmatch and FNM_PATHNAME, under
    which `release/**` does not match `release/a/b`."""
    rule = dict(_CLOSED_CLASSIC, pattern="release/**")
    extra = {_dbp("prod"): {"total_count": 1, "branch_policies": [
        {"name": "release/a/b", "type": "branch"}]},
        f"{R}/branches/release%2Fa%2Fb": _BRANCH_NOT_FOUND,
        f"{R}/branches/release/a/b": _BRANCH_NOT_FOUND}
    out, _ = _run(_responses([_env("prod", _CUSTOM)],
                             classic=_graphql(rules=[rule]), extra=extra),
                  tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_classic_double_star_slash_crosses_folders(tmp_path):
    """`release/**/*` is the documented way to match any depth."""
    rule = dict(_CLOSED_CLASSIC, pattern="release/**/*")
    extra = {_dbp("prod"): {"total_count": 1, "branch_policies": [
        {"name": "release/a/b", "type": "branch"}]},
        f"{R}/branches/release%2Fa%2Fb": _BRANCH_NOT_FOUND,
        f"{R}/branches/release/a/b": _BRANCH_NOT_FOUND}
    out, _ = _run(_responses([_env("prod", _CUSTOM)],
                             classic=_graphql(rules=[rule]), extra=extra),
                  tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_a_bare_double_star_classic_rule_covers_one_level_only(tmp_path):
    """A classic rule `**` acts like `*`: it leaves `team/x` uncovered."""
    rule = dict(_CLOSED_CLASSIC, pattern="**")
    extra = {_dbp("prod"): {"total_count": 1, "branch_policies": [
        {"name": "team/*", "type": "branch"}]},
        f"{R}/git/matching-refs/heads/team/": []}
    out, _ = _run(_responses([_env("prod", _CUSTOM)],
                             classic=_graphql(rules=[rule]), extra=extra),
                  tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_ruleset_double_star_does_not_cross_slash(tmp_path):
    """Rulesets use the same fnmatch flags: a literal `refs/heads/**` include
    does not cover `team/x`."""
    rs = _ruleset(1, ["refs/heads/**"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("team/x", "branch")], [rs], extra={
        f"{R}/branches/team%2Fx": _BRANCH_NOT_FOUND,
        f"{R}/branches/team/x": _BRANCH_NOT_FOUND})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_branch_policy_entry_without_a_name_is_unmeasured(tmp_path):
    extra = {_dbp("prod"): {"total_count": 1, "branch_policies": [
        {"id": 1, "pattern": "release/*"}]}}
    out, _ = _run(_responses([_env("prod", _CUSTOM)], extra=extra),
                  tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_an_environment_entry_without_a_name_is_unmeasured(tmp_path):
    envs = [_env("prod", _ANY, reviewers=1, prevent_self_review=True),
            {"id": 9, "deployment_branch_policy": None}]
    out, _ = _run(_responses(envs), tmp_path=tmp_path)
    f = _fact(out, BRANCH)
    assert f["outcome"] == "unmeasured", f
    assert "no name" in f["evidence"], f


def _classic_release(tmp_path, **rule):
    """'Protected branches only'; one classic rule on the existing literal
    branch `release`."""
    base = {"pattern": "release", "blocksCreations": False,
            "restrictsPushes": False, "requiresApprovingReviews": True,
            "requiredApprovingReviewCount": 1,
            "dismissesStaleReviews": True, "requireLastPushApproval": False,
            "allowsDeletions": False, "pushAllowances": {"totalCount": 0},
            "bypassPullRequestAllowances": {"totalCount": 0}}
    base.update(rule)
    out, _ = _run(_responses(
        [_env("ship", _PROTECTED)], classic=_graphql("ADMIN", [base]),
        extra={f"{R}/branches/release": {"name": "release",
                                         "protected": True}}),
        tmp_path=tmp_path)
    return _envs(out)["ship"]


def test_a_classic_review_rule_with_zero_approvals_does_not_hold_updates(
        tmp_path):
    env = _classic_release(tmp_path, requiredApprovingReviewCount=0)
    assert env["branch_check"] == "fail", env


def test_the_classic_query_asks_for_the_approval_count():
    assert "requiredApprovingReviewCount" in cf._CLASSIC_QUERY


@pytest.mark.parametrize("managed", ["github-pages", "copilot"])
def test_a_managed_environment_that_could_fail_leaves_the_check_unmeasured(
        tmp_path, managed):
    """It accepts any branch and its secrets cannot be read: it may hold
    secrets, so the overall check cannot pass."""
    out, _ = _run(_responses(
        [_env(managed, _ANY), _env("prod", _ANY, reviewers=1,
                                    prevent_self_review=True)],
        extra={f"{R}/environments/{managed}/secrets?per_page=100":
               _ADMIN_403}), tmp_path=tmp_path)
    f = _fact(out, BRANCH)
    assert f["outcome"] == "unmeasured", f
    assert managed in f["evidence"], f


# --- lines that decide a pass, each pinned by one test -------------------------
# Each of these went red under a one-line mutation of the engine that every
# other test in this file survived.

def test_a_classic_pull_request_bypass_allowance_opens_an_existing_branch(
        tmp_path):
    """Someone may skip the pull request requirement on `release`: their
    leaked credential pushes straight to it."""
    env = _classic_release(tmp_path,
                           bypassPullRequestAllowances={"totalCount": 1})
    assert env["branch_check"] == "fail", env


def test_classic_reviews_alone_hold_updates_on_an_existing_branch(tmp_path):
    env = _classic_release(tmp_path)
    assert env["branch_check"] == "pass", env


def test_a_deletable_existing_classic_branch_is_open(tmp_path):
    """Reviews are required, but deletions are allowed and creation is not
    restricted: delete `release`, push a new one with a workflow."""
    env = _classic_release(tmp_path, allowsDeletions=True)
    assert env["branch_check"] == "fail", env


def test_a_single_star_ruleset_does_not_cover_a_double_star_environment(
        tmp_path):
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("release/**", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_double_star_ruleset_covers_only_its_prefix(tmp_path):
    rs = _ruleset(1, ["refs/heads/release/**"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("rel*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/rel": []})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_a_protected_matching_branch_is_not_proof_of_open(tmp_path):
    """The bypass list is unreadable, so the rules leave it undecided; the
    only matching branch is protected, so it does not prove OPEN."""
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"],
                  bypass=None)
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": [
            {"ref": "refs/heads/release/1"}],
        f"{R}/branches?protected=true&per_page=100": [
            {"name": "release/1", "protected": True}]})
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_only_a_managed_environment_with_unreadable_secrets_is_unmeasured(
        tmp_path):
    out, _ = _run(_responses([_env("github-pages", _ANY)], extra={
        f"{R}/environments/github-pages/secrets?per_page=100": _ADMIN_403}),
        tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured", _fact(out, BRANCH)


def test_more_than_100_classic_rules_is_unmeasured(tmp_path):
    classic = _graphql("ADMIN", [dict(_CLOSED_CLASSIC)])
    classic["data"]["repository"]["branchProtectionRules"]["totalCount"] = 150
    out, _ = _run(_responses([_env("ship", _PROTECTED)], classic=classic),
                  tmp_path=tmp_path)
    assert _envs(out)["ship"]["branch_check"] == "unmeasured", _envs(out)


def test_a_fail_wins_over_an_unmeasured_environment(tmp_path):
    out, _ = _run(_responses(
        [_env("open", _ANY), _env("rel", _PROTECTED)],
        classic=_graphql("READ")), tmp_path=tmp_path)
    assert _fact(out, BRANCH)["outcome"] == "fail", _fact(out, BRANCH)


def test_probe_p1_on_a_closed_ruleset_pattern_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED",
                        True)
    rs = _ruleset(1, ["refs/heads/stable/**"],
                  ["creation", "deletion", "pull_request"])
    rs["rules"][2]["parameters"] = {"required_approving_review_count": 1,
                                    "require_last_push_approval": True}
    out, _ = _run(_responses(
        [_env("release", _PROTECTED)], rulesets=[rs],
        classic=_graphql("ADMIN", [dict(_CLOSED_CLASSIC,
                                        pattern="ghost/*")])),
        tmp_path=tmp_path)
    assert _envs(out)["release"]["branch_check"] == "pass", _envs(out)


# --- output contract and log hygiene ------------------------------------------

def test_environment_rows_name_their_checks_not_fact_numbers(tmp_path):
    """The per-environment verdict keys say which check they hold; `branch` /
    `approval` collided with the fact numbering (BRANCH is another fact)."""
    out, _ = _run(_responses([_env("prod", _ANY, reviewers=1)]),
                  tmp_path=tmp_path)
    row = _envs(out)["prod"]
    assert {"branch_check", "branch_check_why", "approval_check",
            "approval_check_why"} <= set(row), row
    assert not {"f10", "f11", "f10_why", "f11_why"} & set(row), row


def test_secret_lists_are_asked_for_with_403_expected(tmp_path):
    """A reader without secret access is answered 403; the engine says so up
    front, so gh_utils does not log it as an ERROR."""
    out, gh = _run(_responses([_env("github-pages", _ANY)]), tmp_path=tmp_path)
    kw = dict(gh.calls)
    for path in (f"{R}/actions/secrets?per_page=100",
                 f"{R}/actions/organization-secrets?per_page=100",
                 f"{R}/environments/github-pages/secrets?per_page=100"):
        assert kw[path].get("quiet_forbidden") is True, (path, kw[path])


def test_an_expected_403_is_logged_below_error(caplog):
    gh = cf._gh_utils()
    done = mock.Mock(returncode=1, stdout="", stderr=(
        "gh: Must have admin rights to Repository. (HTTP 403)"))
    with mock.patch.object(gh.subprocess, "run", return_value=done), \
            caplog.at_level("DEBUG"):
        with pytest.raises(gh.GitHubAPIError):
            gh.run_gh_api("repos/o/r/actions/secrets", quiet_forbidden=True)
    assert not [r for r in caplog.records if r.levelname == "ERROR"], \
        caplog.records
    assert any("403" in r.getMessage() for r in caplog.records)


def test_a_pass_says_it_covers_a_non_admin_credential(tmp_path):
    """Admin-only bypass lists count as closed, so a pass is a claim about a
    non-admin credential only, and the wording says so."""
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation", "update"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs])
    assert "non-admin push credential" in _envs(out)["prod"][
        "branch_check_why"], _envs(out)
    f = _fact(out, BRANCH)
    assert f["outcome"] == "pass", f
    assert "non-admin push credential" in f["evidence"], f
    text = " ".join(str(v) for k, v in f.items() if k != "evidence")
    assert "non-admin push credential" in text, f


def test_the_report_says_admin_credentials_are_not_covered(tmp_path):
    out, _ = _run(_responses([_CLOSED_ENV]), tmp_path=tmp_path)
    md = _load_report().render({"findings": [], "repo": "owner/repo",
                                "scanned_workflows": 1,
                                "security_score": out})
    sub = md.split("### If a push credential leaks", 1)[1]
    assert "admin or owner credential" in sub, sub[:600]


# --- review-loop regressions: one classic rule, stale approvals, managed
# environments' approval check, malformed secret entries -----------------------

_OPEN_CLASSIC = {
    "blocksCreations": False, "restrictsPushes": False,
    "requiresApprovingReviews": False, "requiredApprovingReviewCount": 0,
    "allowsDeletions": True, "pushAllowances": {"totalCount": 0},
    "bypassPullRequestAllowances": {"totalCount": 0}}


def _custom_classic(tmp_path, patterns, rules, extra=None):
    extra = dict(extra or {})
    extra[_dbp("prod")] = {"total_count": len(patterns), "branch_policies": [
        {"name": n, "type": "branch"} for n in patterns]}
    out, _ = _run(_responses([_env("prod", _CUSTOM)],
                             classic=_graphql("ADMIN", list(rules)),
                             extra=extra), tmp_path=tmp_path)
    return _envs(out)["prod"]


def test_an_exact_classic_rule_outranks_a_closed_wildcard_rule(tmp_path):
    """GitHub applies ONE classic rule to a branch, and a rule naming it
    exactly outranks every wildcard rule. `release`'s own rule lets anyone
    push, so the closed `*` rule protects nothing there."""
    env = _custom_classic(
        tmp_path, ["release"],
        [dict(_CLOSED_CLASSIC, pattern="*"),
         dict(_OPEN_CLASSIC, pattern="release")],
        extra={f"{R}/branches/release": {"name": "release",
                                         "protected": True}})
    assert env["branch_check"] == "fail", env


def test_an_exact_classic_rule_inside_a_closed_wildcard_opens_it(tmp_path):
    """`release/hotfix` is accepted by `release/*` and governed by its own,
    open rule: a push credential moves it."""
    env = _custom_classic(
        tmp_path, ["release/*"],
        [_CLOSED_CLASSIC, dict(_OPEN_CLASSIC, pattern="release/hotfix")],
        extra={f"{R}/git/matching-refs/heads/release/": []})
    assert env["branch_check"] == "fail", env


def test_two_overlapping_classic_wildcards_that_disagree_are_unmeasured(
        tmp_path):
    """Between wildcard rules the OLDER one applies, and the query does not
    read their order: closed is not proven, open is not either."""
    env = _custom_classic(
        tmp_path, ["release/*"],
        [_CLOSED_CLASSIC, dict(_OPEN_CLASSIC, pattern="*/*")],
        extra={f"{R}/git/matching-refs/heads/release/": []})
    assert env["branch_check"] == "unmeasured", env


def test_a_disjoint_open_classic_wildcard_does_not_matter(tmp_path):
    env = _custom_classic(
        tmp_path, ["release/*"],
        [_CLOSED_CLASSIC, dict(_OPEN_CLASSIC, pattern="feature/*")],
        extra={f"{R}/git/matching-refs/heads/release/": []})
    assert env["branch_check"] == "pass", env


def test_an_approval_that_survives_new_pushes_does_not_hold_updates(
        tmp_path):
    """Approvals stay after a push and the last push needs no approval: the
    credential pushes a workflow onto an already-approved pull request and
    merges it."""
    rs = _closed_main({"required_approving_review_count": 1,
                       "dismiss_stale_reviews_on_push": False,
                       "require_last_push_approval": False})
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs],
                          extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


@pytest.mark.parametrize("flag", ["dismiss_stale_reviews_on_push",
                                  "require_last_push_approval"])
def test_either_fresh_approval_setting_holds_updates(tmp_path, flag):
    params = {"required_approving_review_count": 1,
              "dismiss_stale_reviews_on_push": False,
              "require_last_push_approval": False}
    params[flag] = True
    out, _ = _custom_case(tmp_path, [("main", "branch")],
                          [_closed_main(params)], extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "pass", _envs(out)


def test_a_pull_request_rule_silent_on_stale_approvals_is_unmeasured(
        tmp_path):
    rs = _closed_main({"required_approving_review_count": 1})
    out, _ = _custom_case(tmp_path, [("main", "branch")], [rs],
                          extra=_main_exists())
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)


def test_a_classic_approval_that_survives_new_pushes_does_not_hold(tmp_path):
    env = _classic_release(tmp_path, dismissesStaleReviews=False,
                           requireLastPushApproval=False)
    assert env["branch_check"] == "fail", env


def test_the_classic_query_asks_for_the_fresh_approval_settings():
    assert "dismissesStaleReviews" in cf._CLASSIC_QUERY
    assert "requireLastPushApproval" in cf._CLASSIC_QUERY


@pytest.mark.parametrize("managed", ["github-pages", "copilot"])
def test_a_managed_environment_allowing_self_review_blocks_the_approval_pass(
        tmp_path, managed):
    """Its secrets cannot be read, so it may hold some; it lets whoever
    started a run approve it, so the approval check cannot pass."""
    out, _ = _run(_responses(
        [_env(managed, _ANY, reviewers=1, prevent_self_review=False),
         _env("prod", _ANY, reviewers=1, prevent_self_review=True)],
        extra={f"{R}/environments/{managed}/secrets?per_page=100":
               _ADMIN_403}), tmp_path=tmp_path)
    f = _fact(out, APPROVAL)
    assert f["outcome"] == "unmeasured", f
    assert managed in f["evidence"], f


@pytest.mark.parametrize("entry", [{}, {"name": ""}, "TOKEN", None])
def test_a_secret_entry_of_an_unexpected_shape_is_unmeasured(tmp_path, entry):
    """A list entry with no name is not "no secret": the list is unread."""
    out, _ = _run(_responses([_CLOSED_ENV], extra={
        f"{R}/actions/secrets?per_page=100": {"total_count": 1,
                                              "secrets": [entry]}}),
        tmp_path=tmp_path)
    assert _fact(out, SCOPED)["outcome"] == "unmeasured", _fact(out, SCOPED)


# --- review-loop: lines no test pinned, each of which a one-line mutant
# turned into a false pass ------------------------------------------------------

def test_a_creation_only_ruleset_leaves_pushes_open(tmp_path):
    """"Restrict creations" alone: nobody makes a new `release/x`, but every
    existing one takes direct pushes."""
    rs = _ruleset(1, ["refs/heads/release/*"], ["creation"])
    out, _ = _custom_case(tmp_path, [("release/*", "branch")], [rs], extra={
        f"{R}/git/matching-refs/heads/release/": [
            {"ref": "refs/heads/release/1"}],
        f"{R}/branches?protected=true&per_page=100": [
            {"name": "release/1", "protected": True}]})
    assert _envs(out)["prod"]["branch_check"] == "fail", _envs(out)


def test_protected_branches_with_an_unreadable_branch_is_unmeasured(
        tmp_path):
    rule = dict(_CLOSED_CLASSIC, pattern="release")
    out, _ = _run(_responses([_env("ship", _PROTECTED)],
                             classic=_graphql("ADMIN", [rule]),
                             extra={f"{R}/branches/release": _HTTP_502}),
                  tmp_path=tmp_path)
    assert _envs(out)["ship"]["branch_check"] == "unmeasured", _envs(out)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"


@pytest.mark.parametrize("body", ['{"message": "weird"}', "", "[1, 2]"])
def test_a_secret_list_of_an_unexpected_shape_is_unmeasured(tmp_path, body):
    out, _ = _run(_responses([_CLOSED_ENV], extra={
        f"{R}/actions/secrets?per_page=100": body}), tmp_path=tmp_path)
    assert _fact(out, SCOPED)["outcome"] == "unmeasured", _fact(out, SCOPED)


def test_an_unrecognised_branch_policy_is_unmeasured(tmp_path):
    policy = {"protected_branches": False, "custom_branch_policies": False}
    out, _ = _run(_responses([_env("prod", policy)]), tmp_path=tmp_path)
    assert _envs(out)["prod"]["branch_check"] == "unmeasured", _envs(out)
    assert _fact(out, BRANCH)["outcome"] == "unmeasured"


def test_a_reviewer_rule_silent_on_self_review_allows_it(tmp_path):
    env = _env("prod", _ANY, reviewers=1)
    del env["protection_rules"][0]["prevent_self_review"]
    out, _ = _run(_responses([env]), tmp_path=tmp_path)
    assert _fact(out, APPROVAL)["outcome"] == "fail", _fact(out, APPROVAL)


def test_a_crashed_facts_layer_never_reads_as_no_secrets(tmp_path,
                                                         monkeypatch):
    """scan.py's crash block renders as "not checked", never as a count."""
    scan = cf._scan()

    def _boom(*_a, **_k):
        raise RuntimeError("facts layer exploded")

    monkeypatch.setattr(cf, "compute_config_facts", _boom)
    monkeypatch.setitem(sys.modules, "config_facts", cf)
    degraded = scan._compute_security_score(tmp_path, [], [])
    md = _load_report().render({"findings": [], "repo": "owner/repo",
                                "scanned_workflows": 1,
                                "security_score": degraded})
    sub = md.split("### If a push credential leaks", 1)[1]
    assert "not checked here" in sub, sub
    assert "secret(s)" not in sub, sub


def _block(inventory, envs=()):
    return {"status": "measured", "reason": "", "environments": list(envs),
            "inventory": inventory, "tier1_gap": "", "credential_note": ""}


def test_the_report_marks_each_environment_verdict():
    md = _load_report()._push_access_block([], _block(
        {"status": "measured", "repo_secrets": [], "org_secrets": []},
        [{"name": "open", "branch_check": "fail", "approval_check": "pass",
          "branch_check_why": "w", "approval_check_why": ""}]))
    row = next(line for line in md.splitlines() if line.startswith("| `open`"))
    cells = [c.strip() for c in row.split("|")]
    assert cells[2] == "❌" and cells[3] == "✅", row


def test_the_report_lists_names_and_high_value_ones():
    md = _load_report()._push_access_block([], _block(
        {"status": "measured", "repo_secrets": ["A_TOKEN", "NPM_TOKEN"],
         "org_secrets": [], "high_value": ["NPM_TOKEN"]}))
    assert "2 repository secret(s): `A_TOKEN`, `NPM_TOKEN`" in md, md
    assert "0 organisation secret(s)" in md, md
    assert "Likely high-value" in md and "`NPM_TOKEN`." in md, md


def test_the_report_never_counts_an_unread_list():
    md = _load_report()._push_access_block([], _block(
        {"status": "partial", "repo_secrets": ["A"], "org_secrets": None,
         "reason": "the organisation list could not be read"}))
    assert "the organisation list could not be read" in md, md
    assert "organisation secret(s)" not in md, md
    assert "Partial" in md and "not a full count" in md, md


def test_the_report_says_an_unmeasured_inventory_is_a_gap():
    md = _load_report()._push_access_block([], _block(
        {"status": "unmeasured", "repo_secrets": None, "org_secrets": None,
         "reason": "403"}))
    assert "unmeasured" in md and 'not "no secrets"' in md, md
    assert "secret(s)" not in md, md
