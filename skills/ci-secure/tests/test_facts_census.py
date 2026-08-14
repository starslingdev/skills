"""The disjointness census, as a PASS/FAIL guard.

Rule: no single YAML edit may move a ci-score check and a ci-secure fact at
once. The census is only as good as its coverage (a rule ABOUT data needs a
census OF it), so this test pins three bindings:

1. the FROZEN MANIFEST of ci-score's check ids this census was ruled against —
   if ci-score's live registry (when present on this machine) has grown or
   renamed checks, the census is stale and goes red rather than silently
   covering less than it claims;
2. every shipped fact id appears in the census table in
   references/security-facts.md — a fact added without a census row is exactly
   the drift that let the cancel-superseded collision ship undisclosed;
3. the one disjointness that is enforced IN CODE (id-token exclusion) matches
   the registry entry it defends against.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]

# ci-score's registry AS OF THE 2026-08-03 CENSUS. Frozen here on purpose:
# the census table in security-facts.md was written against exactly this set,
# so the guard must compare against the set that was censused on that date,
# then separately detect live drift.
_CI_SCORE_CHECKS_AT_CENSUS = frozenset({
    "ci.cache.dependency-cache",
    "ci.cache.build-cache",
    "ci.checkout.shallow-clone",
    "ci.parallel.test-sharding",
    "ci.build.change-scoped",
    "ci.trigger.concurrency-groups",
    "ci.trigger.cancel-superseded",
    "ci.trigger.path-filter",
    "ci.hygiene.job-timeouts",
    "ci.security.scoped-id-token",
    "ci.security.pinned-action-shas",
})

_EXPECTED_FACTS = frozenset({
    "sec.permissions.workflow-declares",
    "sec.permissions.write-scoped",
    "sec.codeowners.workflows",
    "sec.trigger.fork-code-uncleared",
    "sec.secrets.no-blanket-inherit",
    "sec.checkout.credentials-scoped",
    "sec.required-checks.skippable",
    "sec.fork-approval.effective",
})


def _facts_module():
    if "ci_secure_config_facts" in sys.modules:
        return sys.modules["ci_secure_config_facts"]
    spec = importlib.util.spec_from_file_location(
        "ci_secure_config_facts", _SKILL / "scripts" / "config_facts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_secure_config_facts"] = mod
    spec.loader.exec_module(mod)
    return mod


def _shipped_fact_ids(tmp_path) -> set[str]:
    cf = _facts_module()
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: [push]\njobs:\n  a:\n"
                               "    runs-on: ubuntu-latest\n"
                               "    steps: [{run: make}]\n")
    out = cf.compute_config_facts(tmp_path, [wf / "ci.yml"], [])
    return {f["fact_id"] for f in out["facts"]}


def test_shipped_facts_match_the_censused_set(tmp_path):
    """A fact added to the code without a census row — or removed without
    updating the census — goes red here, not silently live."""
    assert _shipped_fact_ids(tmp_path) == _EXPECTED_FACTS


def test_every_shipped_fact_has_a_census_row(tmp_path):
    doc = (_SKILL / "references" / "security-facts.md").read_text()
    table_ids = set(re.findall(r"\|\s*`(sec\.[a-z.-]+)`\s*\|", doc))
    missing = _shipped_fact_ids(tmp_path) - table_ids
    assert not missing, (
        "shipped fact(s) with no disjointness census row in "
        "references/security-facts.md: " + ", ".join(sorted(missing))
    )


def test_census_names_only_real_ci_score_checks():
    """Every ci-score check id the census document cites must exist in the
    frozen manifest — a typo'd or invented check id would make a census row
    vacuously true."""
    doc = (_SKILL / "references" / "security-facts.md").read_text()
    cited = set(re.findall(r"`(ci\.[a-z.-]+)`", doc))
    # `ci.trigger.*` is a legitimate family reference, not an id.
    cited = {c for c in cited if not c.endswith(".")}
    unknown = cited - _CI_SCORE_CHECKS_AT_CENSUS
    assert not unknown, "census cites unknown ci-score check id(s): " \
        + ", ".join(sorted(unknown))


def test_id_token_exclusion_defends_against_the_registry_entry():
    """The one disjointness enforced in CODE: `write-scoped` excludes the
    id-token scope because ci-score's scoped-id-token owns it. If that check
    ever leaves the manifest, the exclusion loses its reason and this census
    must be re-ruled."""
    assert "ci.security.scoped-id-token" in _CI_SCORE_CHECKS_AT_CENSUS
    cf = _facts_module()
    assert cf._write_scopes_at_workflow_level(
        {"permissions": {"id-token": "write"}}) == []
    assert cf._write_scopes_at_workflow_level(
        {"permissions": {"packages": "write"}}) == ["packages"]


def test_live_ci_score_registry_has_not_drifted_from_the_census():
    """When the public repo's checkout is present on this machine, compare its
    LIVE registry against the frozen manifest. Drift means the census was
    ruled against a stale set and must be re-run — skipped (loudly) when the
    checkout is absent, e.g. in CI of this repo alone."""
    live = Path.home() / "Development/skills/skills/ci-score/references/ci-score-spec.json"
    if not live.is_file():
        pytest.skip("public skills checkout not present; census freshness "
                    "not verifiable here (verified on maintainer machines)")
    checks = {c["check_id"] for c in json.loads(live.read_text())["checks"]}
    assert checks == _CI_SCORE_CHECKS_AT_CENSUS, (
        "ci-score's live registry differs from the set this census was ruled "
        "against — re-run the disjointness census against the new set: "
        f"added={sorted(checks - _CI_SCORE_CHECKS_AT_CENSUS)} "
        f"removed={sorted(_CI_SCORE_CHECKS_AT_CENSUS - checks)}"
    )
