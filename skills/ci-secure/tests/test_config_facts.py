"""Oracle tests for the scored security config facts.

This number is a third of a published CI Score, so every test pins a property a
graded maintainer could dispute: what each fact counts, what clears it, what
must never silently pass, and how the score treats a fact it could not measure.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from unittest import mock
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


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


def _load_report():
    """`report.py` does `from config import ACTIVITY_RUN_LIMIT`, and pyproject
    puts a SIBLING skill's `scripts/` (which has its own `config`) on the path
    too — so a bare `import report` resolves the wrong `config` depending on
    which test ran first. Import it with this skill's scripts dir first and the
    sibling `config` evicted, then restore, exactly as test_report.py does.
    """
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
        except ValueError:                      # pragma: no cover - defensive
            pass
        sys.modules.pop("config", None)
        if saved_config is not None:
            sys.modules["config"] = saved_config


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


def _repo(tmp_path: Path, workflows: dict[str, str],
          codeowners: str | None = "/.github/workflows/ @sec-team\n") -> tuple:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    files = []
    for name, body in workflows.items():
        f = wf_dir / name
        # Fixture WORKFLOW YAML — trigger names, `permissions:` blocks, `uses:`
        # refs. Never a credential; there is nothing here to encrypt. Name the
        # variables that hold these bodies for what they are (a trigger, a
        # workflow) and not for their trust level: a static analyzer classifies
        # data by the name of the variable carrying it, so a fixture called
        # `trusted` reads to CodeQL as a stored secret written to disk in the
        # clear, and the shipped tree collects a high-severity false positive.
        f.write_text(body)
        files.append(f)
    if codeowners is not None:
        (tmp_path / ".github" / "CODEOWNERS").write_text(codeowners)
    return tmp_path, sorted(files)


def _outcome(result, fact_id):
    for f in result["facts"]:
        if f["fact_id"] == fact_id:
            return f
    raise AssertionError(f"{fact_id} missing from the fact table")


# --- the clean repo ----------------------------------------------------------

def test_a_well_configured_repo_passes_every_fact(tmp_path):
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    # `sec.required-checks.skippable` reads branch protection, so the repo and
    # a recorded API response are supplied here; without them it is UNMEASURED
    # (asserted separately below), never a pass.
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=lambda repo: ([], "branch `main`"),
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"))
    assert out["score"] == 100.0
    assert out["passed"] == out["scored_count"] == 8
    for f in out["facts"]:
        assert f["outcome"] == "pass", f"{f['fact_id']} failed: {f['evidence']}"


def test_the_api_gated_facts_are_the_only_unmeasured_ones_offline(tmp_path):
    """Offline, the six YAML/file facts still resolve and the two API-gated
    ones disclose that they did not — a scan with no token is a smaller
    measurement, not a cleaner repo."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    out = cf.compute_config_facts(root, files, [])
    assert sorted(out["unmeasured"]) == ["sec.fork-approval.effective",
                                         "sec.required-checks.skippable"]
    assert out["scored_count"] == 6 and out["applicable_count"] == 8
    assert "COVERAGE GAP" in out["caveat"]


# --- F1 / F2: permissions ----------------------------------------------------

def test_missing_permissions_block_fails_and_names_the_file(tmp_path):
    root, files = _repo(tmp_path, {
        "ci.yml": _SAFE_WF,
        "bad.yml": "on: [push]\njobs:\n  b:\n    runs-on: ubuntu-latest\n"
                   "    steps: [{run: make}]\n"})
    out = cf.compute_config_facts(root, files, [])
    f = _outcome(out, "sec.permissions.workflow-declares")
    assert f["outcome"] == "fail"
    assert "bad.yml" in f["evidence"], "a failed fact must name its evidence"


def test_per_job_permissions_satisfy_the_declares_fact(tmp_path):
    wf = ("on: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
          "    permissions: {contents: read}\n    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.permissions.workflow-declares")["outcome"] == "pass"


def test_workflow_level_write_fails_but_id_token_is_excluded_by_construction(tmp_path):
    """THE disjointness constraint. ci-score's scoped-id-token owns
    `id-token:` placement; if this fact also counted it, one YAML edit would
    move a ci-score check and a ci-secure fact at once."""
    idt = ("on: [push]\npermissions:\n  id-token: write\n  contents: read\n"
           "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": idt})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.permissions.write-scoped")
    assert f["outcome"] == "pass", (
        "id-token: write at workflow level is ci-score's check, not ours — "
        "counting it here breaks the registered disjointness"
    )

    # NOT a .replace on the same scope: that would produce a duplicate
    # `contents:` key, the file would fail YAML parsing and drop out of the
    # doc set, and the fact would pass VACUOUSLY — the test would then assert
    # the wrong thing about a file that was never examined.
    wide = idt.replace("id-token: write", "packages: write")
    root2, files2 = _repo(tmp_path / "b", {"ci.yml": wide})
    f2 = _outcome(cf.compute_config_facts(root2, files2, []),
                  "sec.permissions.write-scoped")
    assert f2["outcome"] == "fail"
    assert "packages" in f2["evidence"]


def test_write_all_string_fails_the_scoping_fact(tmp_path):
    wf = ("on: [push]\npermissions: write-all\njobs:\n  a:\n"
          "    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.permissions.write-scoped")["outcome"] == "fail"


# --- F3: CODEOWNERS ----------------------------------------------------------

def test_hostile_workflow_filename_is_neutralized_in_evidence(tmp_path):
    """A workflow FILENAME is a repo-controlled scanned string and may legally
    carry backticks. When such a file lands in a hygiene evidence cell, the raw
    backtick would unbalance that markdown cell's inline-code spans. The evidence
    must carry the filename with backticks neutralized — the same treatment the
    finding bullets give scanned strings. Structural forgery is blocked upstream
    by the cell renderer; this pins the cosmetic residual closed."""
    no_perms = "on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
    root, files = _repo(tmp_path, {"na`me`.yml": no_perms}, codeowners=None)
    result = cf.compute_config_facts(root, files, [])
    ev = _outcome(result, "sec.permissions.workflow-declares")["evidence"]
    assert "`me`" not in ev, ev
    assert "na'me'.yml" in ev, ev


def test_codeowners_missing_and_noncovering_both_fail(tmp_path):
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF}, codeowners=None)
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.codeowners.workflows")["outcome"] == "fail"

    root2, files2 = _repo(tmp_path / "b", {"ci.yml": _SAFE_WF},
                          codeowners="*.go @backend\n")
    f = _outcome(cf.compute_config_facts(root2, files2, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", (
        "an extension-only rule does not cover workflow files"
    )


def test_codeowners_rule_for_one_workflow_file_does_not_cover_the_directory(tmp_path):
    """The fact claims the DIRECTORY is covered. A rule naming a single
    workflow protects that file and leaves every sibling merging on the same
    approvals as any other change — reporting it as covered is a false clean
    on the only thing this row asserts."""
    for rule in (".github/workflows/release.yml @sec\n",
                 "/.github/workflows/deploy.yaml @sec\n"):
        root, files = _repo(tmp_path / rule.split("/")[-1].split(".")[0],
                            {"ci.yml": _SAFE_WF}, codeowners=rule)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "fail", f"{rule!r} covers one file, not the dir"


def test_codeowners_restricted_glob_does_not_cover_the_directory(tmp_path):
    """A glob that names part of a filename owns only the workflows matching
    it. `.github/workflows/*release*.yml` leaves ci.yml, test.yml and every
    other sibling merging on the same approvals as any other change — the same
    false clean as the single-file rule, one step removed."""
    for i, rule in enumerate((".github/workflows/*release*.yml @sec\n",
                              "/.github/workflows/*-deploy.yml @sec\n",
                              ".github/workflows/**/*release*.yml @sec\n")):
        root, files = _repo(tmp_path / f"g{i}", {"ci.yml": _SAFE_WF},
                            codeowners=rule)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "fail", (
            f"{rule!r} owns some workflows, not the directory")


def test_codeowners_rule_without_an_owner_does_not_cover_workflows(tmp_path):
    """A path with no owner assigns no reviewer — in GitHub's semantics an
    ownerless pattern REMOVES ownership for those paths. Matching the path
    alone reported the opposite of what the row claims."""
    for i, rule in enumerate((".github/workflows/\n",
                              "/.github/workflows/*\n",
                              ".github/**\n",
                              "*\n")):
        root, files = _repo(tmp_path / f"o{i}", {"ci.yml": _SAFE_WF},
                            codeowners=rule)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "fail", f"{rule!r} names no owner"
    # The failure names the near-miss rather than reading as "you wrote
    # nothing" — otherwise the reader looks in the wrong place.
    root, files = _repo(tmp_path / "msg", {"ci.yml": _SAFE_WF},
                        codeowners=".github/workflows/\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    # Pinned on `evidence` SPECIFICALLY. Concatenating evidence and fact before
    # the substring check meant either field could carry the message and the
    # test could not say which — the fact text is a fixed row label, so the
    # near-miss explanation has to land in the evidence or no reader sees it.
    assert "names no owner" in f["evidence"], f
    assert "names no owner" not in f["fact"], f


def test_codeowners_last_matching_rule_wins(tmp_path):
    """GitHub applies the LAST matching rule. A broad owned rule followed by an
    ownerless workflows rule leaves workflow changes with no assigned owner —
    stopping at the first match reported the opposite."""
    root, files = _repo(tmp_path / "override", {"ci.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", (
        "the later ownerless rule is the one GitHub applies")
    # And the other direction: a broad OWNED rule after an ownerless one is
    # what GitHub applies, so it covers.
    root, files = _repo(tmp_path / "override2", {"ci.yml": _SAFE_WF},
                        codeowners=".github/workflows/\n* @team\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", "the later owned rule is the one applied"


def test_codeowners_narrow_ownerless_override_strips_a_workflow(tmp_path):
    """GitHub applies the LAST matching rule PER FILE. A broad owned rule
    followed by a NARROW ownerless rule leaves that one workflow with no
    reviewer — the directory has a default owner but is not uniformly covered.
    Reporting it as covered is a false clean on a deliberately-exempted (often
    the most sensitive) workflow. The stripped rule must name a workflow that
    actually EXISTS — a stale entry for a deleted file removes coverage from
    nothing."""
    # Single-file ownerless override after a broad owner — release.yml IS a real
    # workflow, so exempting it is a genuine coverage hole.
    root, files = _repo(tmp_path / "one",
                        {"ci.yml": _SAFE_WF, "release.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/release.yml\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", f
    assert "release.yml" in f["evidence"], f
    # Restricted ownerless glob override — the same hole, one step removed; the
    # glob matches a workflow that exists.
    root, files = _repo(tmp_path / "glob",
                        {"ci.yml": _SAFE_WF, "deploy-prod.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/*deploy*.yml\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", f
    # A narrow rule that NAMES an owner is fine — the file is still owned.
    root, files = _repo(tmp_path / "owned",
                        {"ci.yml": _SAFE_WF, "release.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/release.yml @sec\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f
    # And a later directory rule re-owns every file under it, cancelling an
    # earlier narrow ownerless override.
    root, files = _repo(tmp_path / "recover",
                        {"ci.yml": _SAFE_WF, "release.yml": _SAFE_WF},
                        codeowners=".github/workflows/release.yml\n"
                                   ".github/workflows/ @team\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f


def test_codeowners_stale_ownerless_rule_for_a_deleted_workflow_still_passes(
        tmp_path):
    """A broad owner followed by an ownerless narrow rule for a file that no
    longer exists is a STALE CODEOWNERS residue, not a coverage hole: every
    workflow that is actually present is still owned by the broad rule.
    Treating the stale path as an exemption would fail a correctly-covered
    repo (a false positive)."""
    # Stale single-file rule (no gone.yml in the tree).
    root, files = _repo(tmp_path / "stale-file", {"ci.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/gone.yml\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f
    # Stale restricted glob that matches nothing present.
    root, files = _repo(tmp_path / "stale-glob", {"ci.yml": _SAFE_WF},
                        codeowners="* @team\n.github/workflows/*deploy*.yml\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f


def test_codeowners_owner_forms_are_recognized(tmp_path):
    """Teams and email owners count, and a later owned line rescues an earlier
    ownerless one."""
    for i, rule in enumerate((".github/workflows/ @org/sec-team\n",
                              ".github/workflows/ sec@example.com\n",
                              ".github/workflows/\n.github/workflows/ @sec\n")):
        root, files = _repo(tmp_path / f"w{i}", {"ci.yml": _SAFE_WF},
                            codeowners=rule)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "pass", f"{rule!r} names an owner"


def test_codeowners_directory_and_glob_forms_cover_workflows(tmp_path):
    """The forms that DO cover the directory keep passing — the tightened rule
    must not start failing correctly-configured repos."""
    for i, rule in enumerate((".github/workflows/ @sec\n",
                              "/.github/workflows/ @sec\n",
                              ".github/workflows/* @sec\n",
                              ".github/workflows/** @sec\n",
                              ".github/workflows/*.yml @sec\n",
                              ".github/workflows/**/* @sec\n",
                              ".github/workflows/**/*.yaml @sec\n")):
        root, files = _repo(tmp_path / f"r{i}", {"ci.yml": _SAFE_WF},
                            codeowners=rule)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "pass", f"{rule!r} should cover workflows"


def test_codeowners_global_star_and_double_star_cover_workflows(tmp_path):
    for pattern in ("* @owners\n", ".github/** @sec\n"):
        root, files = _repo(tmp_path / pattern[:3].strip("*/. ") or "x",
                            {"ci.yml": _SAFE_WF}, codeowners=pattern)
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.codeowners.workflows")
        assert f["outcome"] == "pass", f"{pattern!r} should cover workflows"


# --- F3: the CODEOWNERS matrix ----------------------------------------------
# One table over the whole rule surface. Each row is a mutant that survived the
# per-shape tests above: the reviewer could delete a pattern, loosen the owner
# regex to `"@" in line`, drop comment stripping, or forget a candidate
# location, and every existing CODEOWNERS test still passed.

_CODEOWNERS_MATRIX = [
    # (label, CODEOWNERS body, expected outcome)
    # --- single-star does not cross a slash (gitignore semantics) -----------
    ("single-star-under-github", ".github/* @team\n", "fail"),
    ("single-star-is-not-double", ".github/*\n", "fail"),
    # `.github/**` covers the tree; `.github/**/<restricted glob>` owns only
    # the files matching it, exactly like the restricted workflows globs.
    ("double-star-restricted-glob", ".github/**/*release*.yml @team\n",
     "fail"),
    # --- the slashless directory forms -------------------------------------
    ("slashless-workflows-dir", ".github/workflows @team\n", "pass"),
    ("slashless-github-dir", ".github @team\n", "pass"),
    ("slashless-rooted-workflows", "/.github/workflows @team\n", "pass"),
    # --- ownerless AT END OF LINE (no trailing whitespace) ------------------
    # The bug: the directory patterns required trailing whitespace, so a bare
    # `.github/` on the last line matched nothing, the earlier `* @team` was
    # the last match, and the repo graded covered — while GitHub applies the
    # ownerless rule and assigns nobody.
    ("eol-ownerless-github-slash", "* @team\n.github/\n", "fail"),
    ("eol-ownerless-github-bare", "* @team\n.github\n", "fail"),
    ("eol-ownerless-workflows", "* @team\n.github/workflows\n", "fail"),
    ("eol-ownerless-workflows-slash", "* @team\n.github/workflows/\n", "fail"),
    ("eol-ownerless-double-star", "* @team\n.github/**\n", "fail"),
    ("eol-ownerless-global-star", ".github/ @team\n*\n", "fail"),
    # --- comment stripping, both directions --------------------------------
    ("commented-out-rule-is-dead",
     "# .github/workflows/ @team\n", "fail"),
    ("commented-out-rule-does-not-rescue",
     ".github/workflows/\n# .github/workflows/ @team\n", "fail"),
    ("trailing-comment-on-a-live-rule-still-covers",
     ".github/workflows/ @team  # platform owns CI\n", "pass"),
    ("trailing-comment-cannot-invent-an-owner",
     ".github/workflows/  # @team\n", "fail"),
    # --- owner-regex anchoring: `"@" in line` must die ---------------------
    ("bare-at-is-not-an-owner", ".github/workflows/ @\n", "fail"),
    ("at-without-a-tld-is-not-an-email", ".github/workflows/ ops@internal\n",
     "fail"),
    ("path-fragment-containing-an-at-is-not-an-owner",
     ".github/workflows/ ./vendor/@scope\n", "fail"),
    ("team-owner-is-an-owner", ".github/workflows/ @org/sec-team\n", "pass"),
    ("email-owner-is-an-owner", ".github/workflows/ ops@example.com\n",
     "pass"),
]


@pytest.mark.parametrize("label,body,expected",
                         [(r[0], r[1], r[2]) for r in _CODEOWNERS_MATRIX],
                         ids=[r[0] for r in _CODEOWNERS_MATRIX])
def test_codeowners_matrix(tmp_path, label, body, expected):
    root, files = _repo(tmp_path / label, {"ci.yml": _SAFE_WF},
                        codeowners=body)
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == expected, f"{label}: {body!r} -> {f}"


def _repo_with_codeowners_at(tmp_path, relpaths: dict[str, str]):
    """A repo whose CODEOWNERS files live at arbitrary candidate locations."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF}, codeowners=None)
    for rel, body in relpaths.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root, files


@pytest.mark.parametrize("location", ["CODEOWNERS", "docs/CODEOWNERS"])
def test_codeowners_is_read_from_every_candidate_location(tmp_path, location):
    """Every existing test wrote `.github/CODEOWNERS`, so deleting either other
    candidate from the tuple changed nothing — and a repo that keeps its
    CODEOWNERS at the root (the most common placement outside `.github/`) would
    have been graded as having no CODEOWNERS at all."""
    root, files = _repo_with_codeowners_at(
        tmp_path / location.replace("/", "_"),
        {location: ".github/workflows/ @sec\n"})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f
    assert location in f["evidence"], f


def test_codeowners_candidate_precedence_is_github_then_root_then_docs(tmp_path):
    """GitHub reads exactly ONE CODEOWNERS: `.github/`, else root, else
    `docs/`. The others are inert, so a covering rule in a shadowed file must
    not rescue the repo."""
    # `.github/` shadows both: its ownerless rule is what GitHub applies.
    root, files = _repo_with_codeowners_at(tmp_path / "a", {
        ".github/CODEOWNERS": ".github/workflows/\n",
        "CODEOWNERS": "* @team\n",
        "docs/CODEOWNERS": "* @team\n",
    })
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", f
    assert ".github/CODEOWNERS" in f["evidence"].replace("\\", "/"), f
    # Root shadows docs/.
    root, files = _repo_with_codeowners_at(tmp_path / "b", {
        "CODEOWNERS": "*\n",
        "docs/CODEOWNERS": "* @team\n",
    })
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "fail", f


def test_codeowners_utf8_bom_does_not_hide_the_first_rule(tmp_path):
    """A UTF-8 BOM sits in front of the first line and defeats the `^` anchor,
    so a repo whose ONLY rule is the covering one was reported as having no
    entry at all — a confident fail invented by the decoder."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF}, codeowners=None)
    (root / ".github" / "CODEOWNERS").write_bytes(
        b"\xef\xbb\xbf.github/workflows/ @sec-team\n")
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.codeowners.workflows")
    assert f["outcome"] == "pass", f


def test_codeowners_undecodable_file_is_unmeasured_not_a_fail(tmp_path):
    """`errors="replace"` turned bytes that are not UTF-8 into a confident
    "no entry covering workflows". We do not know what that file says, so the
    honest outcome is unmeasured — and unmeasured never scores."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF}, codeowners=None)
    (root / ".github" / "CODEOWNERS").write_bytes(
        b"\xff\xfe.\x00g\x00i\x00t\x00h\x00u\x00b\x00")
    out = cf.compute_config_facts(root, files, [])
    f = _outcome(out, "sec.codeowners.workflows")
    assert f["outcome"] == "unmeasured", f
    assert "unmeasured" in f["evidence"], f
    assert "sec.codeowners.workflows" in out["unmeasured"]
    # ...and it is a coverage gap, not a silent pass. `applicable_count` stays
    # 8 — every fact is still applicable — while only 5 score: this one plus
    # both API-gated facts, which are unmeasured offline too.
    assert out["scored_count"] == 5 and out["applicable_count"] == 8
    assert "COVERAGE GAP" in out["caveat"]


def test_codeowners_unreadable_directory_is_one_unmeasured_row(tmp_path):
    """`Path.is_file()` re-raises EACCES, and the probe sat one line ABOVE the
    try — so a single unreadable directory escaped to scan.py's broad backstop
    and took all twelve facts down with it. It degrades to one row now."""
    import os
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF}, codeowners=None)
    docs = root / "docs"
    docs.mkdir()
    (docs / "CODEOWNERS").write_text("* @team\n")
    os.chmod(docs, 0o000)
    try:
        try:
            (docs / "CODEOWNERS").is_file()
        except OSError:
            pass
        else:
            pytest.skip("this filesystem/user does not enforce directory mode")
        out = cf.compute_config_facts(root, files, [])
    finally:
        os.chmod(docs, 0o755)
    f = _outcome(out, "sec.codeowners.workflows")
    assert f["outcome"] == "unmeasured", f
    # The OTHER facts still resolved — the failure is contained to this row.
    assert out["scored_count"] == 5, out


# --- F4: the sharpened trigger fact -----------------------------------

def test_bare_untrusted_trigger_passes_the_fact():
    """The 84% problem: 'has a dangerous trigger' is true of nearly every
    repo measured during development and discriminates nobody. A bare pull_request_target with no
    attacker-head checkout must PASS."""
    wf = ("on: [pull_request_target]\npermissions: {contents: read}\n"
          "jobs:\n  label:\n    runs-on: ubuntu-latest\n"
          "    steps: [{uses: actions/labeler@v5}]\n")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root, files = _repo(Path(td), {"ci.yml": wf})
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.trigger.fork-code-uncleared")
        assert f["outcome"] == "pass", (
            "a bare untrusted trigger failed the fact — that is the "
            "discriminates-nobody defect this fact was sharpened to avoid"
        )


def test_head_checkout_on_untrusted_trigger_fails_without_needing_execution(tmp_path):
    """The middle tier is this fact's whole reason to exist: trigger + checkout
    of the attacker's head. Execution on top of that is P14.9's FINDING and
    stays out of the hygiene checks — so the fact must fire WITHOUT the
    execution leg,
    or it would just duplicate the chain."""
    wf = ("on: [pull_request_target]\npermissions: {contents: read}\n"
          "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
          "    steps:\n"
          "      - uses: actions/checkout@v4\n"
          "        with: {ref: '${{ github.event.pull_request.head.sha }}',\n"
          "               persist-credentials: false}\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.trigger.fork-code-uncleared")
    assert f["outcome"] == "fail"
    assert "head checkout" in f["evidence"]


# --- F5 / F6 -----------------------------------------------------------------

def test_blanket_secrets_inherit_fails(tmp_path):
    wf = ("on: [push]\npermissions: {contents: read}\n"
          "jobs:\n  call:\n    uses: ./.github/workflows/deploy.yml\n"
          "    secrets: inherit\n")
    root, files = _repo(tmp_path, {"ci.yml": wf, "safe.yml": _SAFE_WF})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.secrets.no-blanket-inherit")
    assert f["outcome"] == "fail"
    assert "call" in f["evidence"]


def test_persisted_credentials_fail_only_on_untrusted_triggers(tmp_path):
    """A plain pull_request checkout that persists credentials is GitHub's
    default and is not this fact's business — the exposure needs an untrusted
    trigger carrying the base repo's token."""
    on_pull_request = ("on: [pull_request]\npermissions: {contents: read}\n"
                       "jobs:\n  t:\n    runs-on: ubuntu-latest\n"
                       "    steps: [{uses: actions/checkout@v4}, {run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": on_pull_request})
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.checkout.credentials-scoped")["outcome"] == "pass"

    on_pr_target = on_pull_request.replace("[pull_request]",
                                           "[pull_request_target]")
    root2, files2 = _repo(tmp_path / "b", {"ci.yml": on_pr_target})
    assert _outcome(cf.compute_config_facts(root2, files2, []),
                    "sec.checkout.credentials-scoped")["outcome"] == "fail"


def test_persisted_credentials_pass_on_payloadless_notification_triggers(tmp_path):
    """`fork`/`watch` fire on a fork/star with no attacker text, ref, or
    artifact entering the job, so a persisted checkout token is unreadable by any
    attacker-influenced execution — persist-credentials is not a defense there
    and the fact must PASS, not FAIL (a false positive that dragged the security
    score, and the blend, down). But a workflow that ALSO carries a real
    untrusted trigger still FAILs — the notification event must not launder it."""
    persisting = ("jobs:\n  t:\n    runs-on: ubuntu-latest\n"
                  "    steps: [{uses: actions/checkout@v4}, {run: make}]\n")
    for trig in ("[fork]", "[watch]", "[fork, watch]"):
        wf = f"on: {trig}\npermissions: {{contents: read}}\n" + persisting
        root, files = _repo(tmp_path / trig.strip("[]"), {"ci.yml": wf})
        assert _outcome(cf.compute_config_facts(root, files, []),
                        "sec.checkout.credentials-scoped")["outcome"] == "pass", \
            f"{trig}: a payload-less notification trigger was wrongly failed"

    # fork PLUS a real untrusted trigger: still exposed, still fails.
    combo = "on: [fork, pull_request_target]\npermissions: {contents: read}\n" + persisting
    rootc, filesc = _repo(tmp_path / "combo", {"ci.yml": combo})
    assert _outcome(cf.compute_config_facts(rootc, filesc, []),
                    "sec.checkout.credentials-scoped")["outcome"] == "fail", \
        "fork laundered a real untrusted trigger (pull_request_target)"


# --- coverage gaps: never a silent pass --------------------------------------

def test_unscannable_workflow_forces_workflow_facts_to_unmeasured(tmp_path):
    """A universal claim over every workflow cannot be asserted when one could
    not be read — the unreadable one could be the failure. Every workflow-
    scoped fact goes UNMEASURED with the gap named; CODEOWNERS (repo-file
    based) still resolves."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    gap = [{"workflow_file": ".github/workflows/broken.yml",
            "reason": "yaml parse error"}]
    out = cf.compute_config_facts(root, files, gap)
    for f in out["facts"]:
        if f["fact_id"] == "sec.codeowners.workflows":
            assert f["outcome"] == "pass"
        elif f["fact_id"] == "sec.fork-approval.effective":
            # Reads a repository SETTING, not workflow YAML — an unparsable
            # workflow says nothing about it. Unmeasured here only because
            # this call passes no repo.
            assert f["outcome"] == "unmeasured"
        else:
            assert f["outcome"] == "unmeasured", (
                f"{f['fact_id']} resolved despite an unscanned workflow — "
                "that is a silent pass over a coverage hole"
            )
            assert "broken.yml" in f["evidence"]
    assert out["scored_count"] == 1
    assert out["applicable_count"] == 8
    assert "COVERAGE GAP" in out["caveat"]
    # The ratio must be over RESOLVED facts only. If unmeasured facts leaked
    # into the numerator, 5 unmeasured + 1 pass over scored=1 would read 600 —
    # so pin the value, not just the counts.
    assert out["passed"] == 1
    assert out["score"] == pytest.approx(100.0)


def test_score_is_none_when_nothing_measured_never_100():
    out = cf.facts_to_score([
        {"fact_id": "f", "fact": "x", "outcome": "unmeasured", "evidence": "e"}])
    assert out["score"] is None
    # The reason is READER-VISIBLE (report.py prints it when `facts` is empty),
    # so it must say "coverage gap" WITHOUT reaching for a score the report no
    # longer renders — by design.
    assert "coverage gap, not a clean result" in out["reason"]
    assert "/100" not in out["reason"] and "score of 100" not in out["reason"]


# --- the registered rule -----------------------------------------------------

def test_score_is_the_registered_ratio_with_no_weights(tmp_path):
    root, files = _repo(tmp_path, {
        "ci.yml": _SAFE_WF,
        "bad.yml": "on: [push]\njobs:\n  b:\n    runs-on: ubuntu-latest\n"
                   "    steps: [{run: make}]\n"})
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=lambda repo: ([], "branch `main`"),
        fork_approval_fetcher=lambda repo: ("first_time_contributors", "x"))
    # bad.yml fails exactly one fact (permissions-declares); 7/8 pass.
    assert out["passed"] == 7 and out["scored_count"] == 8
    assert out["score"] == pytest.approx(round(100 * 7 / 8, 1))
    assert out["registered"]
    assert "no weights" in out["constants"]["rule"]


def test_degraded_block_has_the_same_keys_as_a_real_one(tmp_path, monkeypatch):
    """scan.py's crash guard must emit the SAME shape as the real block.

    A consumer that reads `constants` (the profile pipeline does — a disputed
    score has to be checkable without our source) would KeyError only on the
    failure path, which is exactly where a degraded block is supposed to be
    most honest and least surprising.
    """
    scan = cf._scan()
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    real = cf.compute_config_facts(root, files, [])

    def _boom(*_a, **_k):
        raise RuntimeError("facts layer exploded")

    monkeypatch.setattr(cf, "compute_config_facts", _boom)
    monkeypatch.setitem(sys.modules, "config_facts", cf)
    degraded = scan._compute_security_score(root, files, [])

    assert set(degraded) - {"reason"} <= set(real) | {"caveat"}
    for key in ("facts", "score", "passed", "scored_count",
                "applicable_count", "unmeasured", "constants", "registered"):
        assert key in degraded, f"degraded block is missing {key!r}"
    assert degraded["score"] is None
    # Same reader-visibility rule as facts_to_score: this string is what the
    # report prints under "Nothing here was checked", so it names the crash and
    # stops — no score wording in a report that renders no score.
    assert "facts layer exploded" in degraded["reason"]
    assert "no config fact could be checked" in degraded["reason"]
    assert "score of 100" not in degraded["reason"]

    # ARTIFACT-LEVEL: the string above is not merely stored, it is PRINTED.
    # Render the real degraded block and check the page a reader would see —
    # asserting on the dict alone is how "this is NOT a score of 100" survived
    # the score removal and shipped into a report that renders no score.
    md = _load_report().render({
        "findings": [], "repo": "x/y", "scanned_workflows": 1,
        "security_score": degraded,
    })
    assert "**Nothing here was checked.**" in md
    assert "facts layer exploded" in md, "the render must name the failure"
    for banned in ("Security score", "/100", "score of 100",
                   "scored facts pass"):
        assert banned not in md, f"the crash path rendered {banned!r}"


def test_null_permissions_is_not_a_declaration(tmp_path):
    """`permissions:` with a null value is omitted per GitHub — the
    workflow keeps the broad default token. The skill's own p14_3 fixture
    calls key-presence 'naive'; the fact must agree with the chain detector."""
    wf = ("on: [push]\npermissions:\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
          "    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.permissions.workflow-declares")["outcome"] == "fail"
    # empty dict IS a declaration (explicitly no permissions)
    wf2 = wf.replace("permissions:\n", "permissions: {}\n")
    root2, files2 = _repo(tmp_path / "b", {"ci.yml": wf2})
    assert _outcome(cf.compute_config_facts(root2, files2, []),
                    "sec.permissions.workflow-declares")["outcome"] == "pass"


def test_null_permissions_is_not_a_declaration_at_job_level_either(tmp_path):
    """The null-value rule is level-agnostic. A job carrying a valueless
    `permissions:` key keeps the broad default token exactly as a workflow with
    one does, so the per-job leg has to test the VALUE too — key presence alone
    let a one-character edit buy the fact, and inflate the security score."""
    wf = ("on: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
          "    permissions:\n    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.permissions.workflow-declares")["outcome"] == "fail"
    # a real per-job grant still passes — the fact is not simply disabled
    wf2 = wf.replace("permissions:\n", "permissions: {contents: read}\n")
    root2, files2 = _repo(tmp_path / "b", {"ci.yml": wf2})
    assert _outcome(cf.compute_config_facts(root2, files2, []),
                    "sec.permissions.workflow-declares")["outcome"] == "pass"


def test_only_the_two_shorthand_permission_strings_are_declarations(tmp_path):
    """`permissions:` takes a mapping or one of exactly two shorthand strings.
    Any other scalar is a value GitHub's workflow schema rejects, so crediting
    it would hand a repo the fact — and a higher security score — for a
    workflow that cannot run at all."""
    def outcome(body, sub):
        root, files = _repo(tmp_path / sub, {"ci.yml": body})
        return _outcome(cf.compute_config_facts(root, files, []),
                        "sec.permissions.workflow-declares")["outcome"]

    tpl = ("on: [push]\npermissions: {perms}\njobs:\n  a:\n"
           "    runs-on: ubuntu-latest\n    steps: [{{run: make}}]\n")
    assert outcome(tpl.format(perms="read-all"), "a") == "pass"
    assert outcome(tpl.format(perms="write-all"), "b") == "pass"
    assert outcome(tpl.format(perms="typo"), "c") == "fail"


def test_codeowners_directory_form_covers_workflows(tmp_path):
    """`.github/ @team` is the standard recursive directory rule and
    covers workflows — its absence graded correct repos down."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF},
                        codeowners=".github/ @sec-team\n")
    assert _outcome(cf.compute_config_facts(root, files, []),
                    "sec.codeowners.workflows")["outcome"] == "pass"


# --- evidence honesty --------------------------------------------------------

def test_f1_evidence_says_the_real_reason_not_a_missing_block(tmp_path):
    """The fail evidence read "no `permissions:` block in: X" for files that
    plainly HAVE one — a null value, an invalid scalar, or a grant on only some
    jobs. A reader who opens the named file and sees the key stops trusting the
    whole report."""
    null_wf = ("on: [push]\npermissions:\njobs:\n  a:\n"
               "    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    typo_wf = ("on: [push]\npermissions: raed-all\njobs:\n  a:\n"
               "    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    partial_wf = ("on: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                  "    permissions: {contents: read}\n    steps: [{run: make}]\n"
                  "  b:\n    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    absent_wf = ("on: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                 "    steps: [{run: make}]\n")

    def evidence(body, sub):
        root, files = _repo(tmp_path / sub, {"ci.yml": body})
        f = _outcome(cf.compute_config_facts(root, files, []),
                     "sec.permissions.workflow-declares")
        assert f["outcome"] == "fail"
        return f["evidence"]

    assert "has no value" in evidence(null_wf, "null")
    assert "raed-all" in evidence(typo_wf, "typo")
    assert "not on every job" in evidence(partial_wf, "partial")
    # Vacuous as `"b" in ...` — the evidence contains the substring "b" via
    # "block", "jobs", every path. Assert the real shape: the job that is
    # actually missing the grant, named.
    assert "missing on b" in evidence(partial_wf, "partial2"), (
        "name the job that lacks it"
    )
    assert "missing on a" not in evidence(partial_wf, "partial3"), (
        "job `a` HAS a permissions grant and must not be named as missing one"
    )
    assert "no `permissions:` block" in evidence(absent_wf, "absent")


def test_write_all_evidence_is_not_written_as_a_scope_name(tmp_path):
    """`permissions: write-all` is a shorthand scalar, not a scope — it used to
    render as the nonsense "write-all: write"."""
    wf = ("on: [push]\npermissions: write-all\njobs:\n  a:\n"
          "    runs-on: ubuntu-latest\n    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {"ci.yml": wf})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.permissions.write-scoped")
    assert f["outcome"] == "fail"
    assert "write-all: write" not in f["evidence"], f["evidence"]
    assert "every scope" in f["evidence"]


def test_truncated_offender_lists_say_how_many_were_left_out(tmp_path):
    """The offender lists cap at 5 (or 3) and used to end in a bare "…", so 6
    offenders and 60 rendered identically and the reader could not size the
    work."""
    bad = ("on: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
           "    steps: [{run: make}]\n")
    root, files = _repo(tmp_path, {f"w{i}.yml": bad for i in range(9)})
    f = _outcome(cf.compute_config_facts(root, files, []),
                 "sec.permissions.workflow-declares")
    assert f["outcome"] == "fail"
    assert "and 4 more" in f["evidence"], f["evidence"]
    assert "…" not in f["evidence"]


def test_capped_boundaries_at_exactly_the_cap_and_one_over():
    """The off-by-one that would read "and 0 more", or hide one offender.

    Exactly `cap` items is the whole list and must carry no remainder clause;
    `cap + 1` must say "and 1 more", not "and 0 more" and not silence.
    """
    assert cf._capped(["a", "b", "c"], 3, ", ") == "a, b, c"
    assert cf._capped(["a", "b", "c", "d"], 3, ", ") == "a, b, c — and 1 more"
    assert cf._capped(["a", "b"], 3, ", ") == "a, b"
    assert cf._capped([], 3, ", ") == ""


# --- F7: sec.required-checks.skippable ---------------------------------------
#
# A required status check whose only producer can skip is not a gate: GitHub
# counts a SKIPPED check as passing, so a PR that avoids the producing job's
# `if:` condition merges with the check green and the suite never run. This
# repository shipped exactly that bypass (#49) and closed it with the
# always-running verdict-job pattern, which is this fact's pass shape.
#
# The fact needs branch protection, which is an API question, so it is
# TOKEN-GATED like the impostor-SHA vector: with no repo or no token it is
# UNMEASURED with a stated reason — never a pass, never a fail for being
# unmeasurable. Tests stub the fetcher; nothing here touches the network.

_FACT = "sec.required-checks.skippable"

_CONDITIONAL_ONLY = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test-self:
    name: test
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: pytest -v
"""

_VERDICT_PATTERN = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test-self:
    name: test (self-hosted)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: pytest -v
  test-fork:
    name: test (fork)
    if: github.event.pull_request.head.repo.full_name != github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: pytest -v
  verdict:
    name: test
    needs: [test-self, test-fork]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo "assert the suite that should have run passed"
"""


def _fetcher(contexts, detail="branch `main` protection"):
    def fetch(repo):
        return list(contexts), detail
    return fetch


def _facts_with(tmp_path, workflows, contexts, **kw):
    root, files = _repo(tmp_path, workflows)
    return cf.compute_config_facts(
        root, files, kw.pop("scan_incomplete", []),
        repo="owner/repo", required_contexts_fetcher=_fetcher(contexts),
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"),
        **kw)


def test_required_check_produced_only_by_a_conditional_job_fails(tmp_path):
    f = _outcome(_facts_with(tmp_path, {"ci.yml": _CONDITIONAL_ONLY}, ["test"]),
                 _FACT)
    assert f["outcome"] == "fail"
    # Evidence names the required context, the workflow/job, and the condition.
    # Backticked, because a bare `"test" in evidence` is satisfied by the
    # sentence's own words ("required", "workflows") and proves nothing about
    # whether the context was ever named.
    assert "`test`" in f["evidence"], f["evidence"]
    assert "ci.yml" in f["evidence"]
    assert "`test-self`" in f["evidence"], f["evidence"]
    assert "head.repo.full_name" in f["evidence"]


def test_the_always_running_verdict_job_passes(tmp_path):
    """The #49 fix shape: two mutually-exclusive suite jobs, and one
    always-running job that carries the required check name."""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": _VERDICT_PATTERN}, ["test"]),
                 _FACT)
    assert f["outcome"] == "pass", f["evidence"]


_MATRIX_CONDITIONAL = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test-self:
    name: test
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11', '3.12']
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: pytest -v
"""


def test_a_matrix_expanded_context_maps_to_its_job(tmp_path):
    """Branch protection records `test (3.12)` for a matrix job named `test`.

    The fixture carries a real `strategy.matrix`, because that is what makes
    the expanded context exist: a job without one produces only its own name,
    and matching the expansion against it invents a producer."""
    f = _outcome(
        _facts_with(tmp_path, {"ci.yml": _MATRIX_CONDITIONAL}, ["test (3.12)"]),
        _FACT)
    assert f["outcome"] == "fail"
    assert "test (3.12)" in f["evidence"]


def test_a_check_skippable_through_its_needs_chain_fails(tmp_path):
    """A job with no `if:` of its own still skips when a job it `needs:`
    skips — and a skipped required check is a pass to GitHub."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  build:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - run: make
  test:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail"
    # Backticked: "build" as a bare substring would also be satisfied by a
    # sentence that never names the upstream job at all.
    assert "`build`" in f["evidence"], f["evidence"]


def test_an_unmapped_required_context_is_disclosed_not_failed(tmp_path):
    """A required context no workflow job produces is usually an external app
    check (coverage, a deploy preview). The scanner cannot see those, so it
    names them instead of failing a repo over what it cannot read — and it does
    not claim a clean bill for them either: a pass is a statement about EVERY
    required check, so one it never traced makes the fact unmeasured."""
    f = _outcome(
        _facts_with(tmp_path, {"ci.yml": _VERDICT_PATTERN},
                    ["test", "codecov/project"]),
        _FACT)
    assert f["outcome"] == "unmeasured"
    assert f["outcome"] != "fail"
    assert "codecov/project" in f["evidence"]


def test_no_required_checks_configured_says_so(tmp_path):
    f = _outcome(_facts_with(tmp_path, {"ci.yml": _CONDITIONAL_ONLY}, []), _FACT)
    assert f["outcome"] == "pass"
    assert "requires no status check" in f["evidence"].lower()


def test_without_a_repo_the_fact_is_unmeasured_never_a_pass(tmp_path):
    root, files = _repo(tmp_path, {"ci.yml": _CONDITIONAL_ONLY})
    f = _outcome(cf.compute_config_facts(root, files, []), _FACT)
    assert f["outcome"] == "unmeasured"
    assert "unmeasured" in f["evidence"].lower()


def test_an_api_failure_is_unmeasured_with_the_reason(tmp_path):
    def failing(repo):
        return None, "gh is not authenticated (run gh auth login)"
    root, files = _repo(tmp_path, {"ci.yml": _CONDITIONAL_ONLY})
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=failing,
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"))
    f = _outcome(out, _FACT)
    assert f["outcome"] == "unmeasured"
    assert "gh is not authenticated" in f["evidence"]
    # An unmeasured fact scores nothing and stays visible in the denominator.
    assert _FACT in out["unmeasured"]
    assert out["applicable_count"] == out["scored_count"] + 1


def test_a_scan_gap_forces_the_fact_unmeasured(tmp_path):
    """The claim is over every workflow: a file that would not parse could be
    the one carrying the always-running producer."""
    out = _facts_with(tmp_path, {"ci.yml": _CONDITIONAL_ONLY}, ["test"],
                      scan_incomplete=[{"workflow_file": "broken.yml"}])
    assert _outcome(out, _FACT)["outcome"] == "unmeasured"


def test_the_fact_table_carries_both_api_gated_facts(tmp_path):
    out = _facts_with(tmp_path, {"ci.yml": _VERDICT_PATTERN}, ["test"])
    assert out["applicable_count"] == 8
    assert out["scored_count"] == 8


# --- F8: sec.fork-approval.effective -----------------------------------------
#
# GitHub's fork-PR approval policy decides whose pull request can start CI
# without a maintainer clicking "approve". Its weakest setting requires
# approval only from accounts NEW TO GITHUB, so any attacker with an aged
# throwaway account runs workflows on the repository unapproved — the gate is
# on and gates nobody real. The middle setting (first-time contributors to
# this repo, GitHub's default) is a legitimate trust judgment and PASSES; so
# does the strictest. Only the weakest tier fails.
#
# Verified against the API's documented enum, not guessed:
# first_time_contributors_new_to_github | first_time_contributors |
# all_external_contributors.
#
# Token-gated like the required-checks fact: no API access is disclosed, never
# green and never red.

_FORK_FACT = "sec.fork-approval.effective"


def _fork_facts(tmp_path, policy, detail="repository Actions settings"):
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    return cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=lambda repo: ([], "branch `main`"),
        fork_approval_fetcher=lambda repo: (policy, detail))


def test_approval_only_for_accounts_new_to_github_fails(tmp_path):
    f = _outcome(_fork_facts(tmp_path, "first_time_contributors_new_to_github"),
                 _FORK_FACT)
    assert f["outcome"] == "fail"
    assert "first_time_contributors_new_to_github" in f["evidence"]


def test_first_time_contributors_is_a_legitimate_judgment_and_passes(tmp_path):
    f = _outcome(_fork_facts(tmp_path, "first_time_contributors"), _FORK_FACT)
    assert f["outcome"] == "pass", f["evidence"]


def test_all_external_contributors_passes(tmp_path):
    f = _outcome(_fork_facts(tmp_path, "all_external_contributors"), _FORK_FACT)
    assert f["outcome"] == "pass", f["evidence"]


def test_an_unrecognized_policy_value_is_disclosed_not_judged(tmp_path):
    """A value this detector's enum does not know is a future GitHub setting,
    not a verdict — calling it a pass or a fail would be inventing one."""
    f = _outcome(_fork_facts(tmp_path, "some_future_policy"), _FORK_FACT)
    assert f["outcome"] == "unmeasured"
    assert "some_future_policy" in f["evidence"]


def test_fork_approval_without_a_repo_is_unmeasured(tmp_path):
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    f = _outcome(cf.compute_config_facts(root, files, []), _FORK_FACT)
    assert f["outcome"] == "unmeasured"


def test_fork_approval_api_failure_is_unmeasured_with_the_reason(tmp_path):
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        fork_approval_fetcher=lambda repo: (None, "gh is not authenticated"))
    f = _outcome(out, _FORK_FACT)
    assert f["outcome"] == "unmeasured"
    assert "gh is not authenticated" in f["evidence"]


def test_a_scan_gap_does_not_unmeasure_the_fork_approval_fact(tmp_path):
    """It reads a repository setting, not workflow YAML — an unparsable
    workflow says nothing about it (the CODEOWNERS fact behaves the same)."""
    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    out = cf.compute_config_facts(
        root, files, [{"workflow_file": "broken.yml"}], repo="owner/repo",
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"))
    assert _outcome(out, _FORK_FACT)["outcome"] == "pass"


def test_the_fact_table_carries_eight_facts(tmp_path):
    out = _fork_facts(tmp_path, "all_external_contributors")
    assert out["applicable_count"] == 8
    assert out["scored_count"] == 8


# ---------------------------------------------------------------------------
# F7/F8 — the ways a gate check can lie in the direction of "you are fine".
#
# Every test below was written against the shipped fact and watched to fail.
# They are grouped because they share one property: the fact rendered a green
# or a red whose EVIDENCE SENTENCE asserted something the code never checked.
# ---------------------------------------------------------------------------

def test_always_with_a_further_condition_still_skips(tmp_path):
    """`always() && <guard>` is not `always()`. The job runs in every result
    state AND only when the guard holds — so a fork PR still skips it, and the
    required check is still green without the suite. Matching `always()` as a
    substring turns the #49 bypass with two tokens prepended into a pass."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test-self:
    name: test
    if: always() && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_success_or_failure_certifies_only_without_needs(tmp_path):
    """`success() || failure()` reads like `always()` and behaves like it only
    while the job has no `needs:`. Once it does, a SKIPPED dependency makes
    both predicates false and GitHub skips the job — so it is exactly as
    bypassable as the suite it was meant to gate. This test asserted the
    opposite when it was written; the shape it used (`needs:` plus
    `success() || failure()`) is the one that does NOT certify.
    """
    without_needs = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: success() || failure()
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    out = _facts_with(tmp_path / "no-needs", {"ci.yml": without_needs}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


def test_a_path_filtered_workflow_is_not_a_bypass(tmp_path):
    """GitHub does the OPPOSITE of what this fact once assumed. A workflow that
    path- or branch-filtering skips never reports its check at all, so the
    required check sits PENDING and the pull request cannot merge. Only a
    skipped JOB reports Success. Failing the filtered repo reds a repo whose
    merges are blocked — and the shape that really is green-without-running,
    GitHub's recommended always-succeeding stub job, passed.

    `pull_request.branches` filters the BASE branch, which is the branch whose
    protection was just read, so every pull request this fact gates is inside
    that filter by construction.

    Source: Troubleshooting required status checks — a required check that no
    workflow reports blocks the merge; it is not treated as passed.
    """
    for spelling in ("paths:\n      - 'src/**'", "branches: [main]",
                     "paths-ignore:\n      - 'docs/**'"):
        body = f"""\
name: ci
on:
  pull_request:
    {spelling}
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
        f = _outcome(_facts_with(tmp_path / spelling[:6].strip(),
                                 {"ci.yml": body}, ["test"]), _FACT)
        assert f["outcome"] == "pass", (spelling, f["evidence"])


def test_a_job_in_a_workflow_that_cannot_run_on_a_pr_is_not_a_producer(tmp_path):
    """Producer matching is by display name, so an unrelated job that happens to
    share the required check's name — `test` in a tag-only release workflow —
    was accepted as an always-running producer and vetoed the real, gated one.
    The check that gates the pull request can only come from a workflow that
    runs on pull requests."""
    ci = """\
name: ci
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    release = """\
name: release
on:
  push:
    tags: ['v*']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make smoke
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": ci, "release.yml": release},
                             ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]
    assert "ci.yml" in f["evidence"]


def test_a_dispatch_only_workflow_is_not_a_producer(tmp_path):
    """Same rule, the other common spellings: a workflow reachable only by
    `workflow_dispatch` or `workflow_call` never reports a check on a pull
    request, so it cannot be the thing that makes the gate sound."""
    for trigger in ("workflow_dispatch:", "workflow_call:"):
        body = f"""\
name: manual
on:
  {trigger}
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make smoke
"""
        out = _facts_with(tmp_path / trigger.strip(":"), {"m.yml": body},
                          ["test"])
        f = _outcome(out, _FACT)
        assert f["outcome"] == "unmeasured", (trigger, f["evidence"])


def test_a_push_workflow_still_counts_as_a_producer(tmp_path):
    """The positive control for the rule above. A plain `push` workflow does
    report check runs on a same-repo pull request's head commit, so excluding
    every non-`pull_request` workflow would stop judging repos that gate on
    one — the opposite failure."""
    body = """\
name: ci
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_when_no_required_context_is_produced_here_nothing_is_claimed(tmp_path):
    """Required contexts produced by no job in this repo — external app checks
    — are deliberately not judged. When they are ALL of them, zero checks were
    examined, so a green row saying "every required check is produced by a job
    that always runs" is a claim about an empty set, contradicted by its own
    next sentence."""
    f = _outcome(
        _facts_with(tmp_path, {"ci.yml": _VERDICT_PATTERN},
                    ["codecov/project", "netlify/deploy"]),
        _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    assert "codecov/project" in f["evidence"]


def test_an_unresolvable_needs_target_is_not_an_all_clear(tmp_path):
    """A `needs:` naming a job that is not in the file (a typo, or a job that
    moved) leaves the skip walk with no answer. Treating no-answer as "always
    runs" upgrades an unknown to a pass."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    needs: [ghost]
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    assert "ghost" in f["evidence"]


def test_empty_rulesets_plus_an_unreadable_classic_endpoint_is_not_a_pass(
    tmp_path,
):
    """The reader most likely to have this defect is auditing a repository
    they do not administer. Classic branch protection is admin-only, so their
    token 403s there; the rulesets endpoint answers `[]` because the repo uses
    classic protection. Reading that pair as "requires no status check" turns
    "I could not read your protection" into "your protection is fine"."""
    calls = []

    class _Gh:
        @staticmethod
        def check_prereqs():
            return True

        @staticmethod
        def run_gh_api(path, **kw):
            calls.append(path)
            if path == "repos/owner/repo":
                return json.dumps({"default_branch": "main"})
            if path.endswith("/rules/branches/main"):
                return json.dumps([])
            raise RuntimeError("HTTP 403: Must have admin rights to Repository")

    with mock.patch.object(cf, "_gh_utils", lambda: _Gh):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts is None, (contexts, detail)
    assert "admin" in detail or "could not be read" in detail


def test_the_pass_evidence_describes_the_policy_it_actually_read(tmp_path):
    """`all_external_contributors` gates EVERY outside account, contributor or
    not. Describing it with `first_time_contributors`' sentence understates the
    reader's own setting and states something false about GitHub."""
    f = _outcome(_fork_facts(tmp_path / "a", "all_external_contributors"),
                 _FORK_FACT)
    assert f["outcome"] == "pass"
    assert "every outside" in f["evidence"].lower(), f["evidence"]
    f2 = _outcome(_fork_facts(tmp_path / "b", "first_time_contributors"),
                  _FORK_FACT)
    assert f2["outcome"] == "pass"
    assert "not contributed" in f2["evidence"], f2["evidence"]


def test_a_yaml_boolean_condition_is_quoted_back_as_yaml(tmp_path):
    """The evidence quotes the reader's own file at them, so it must not print
    Python's `True` for a line that says `true`."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: true
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert "True" not in f["evidence"], f["evidence"]


def test_a_workflow_scan_gap_costs_no_api_calls_for_the_required_checks_fact(
    tmp_path,
):
    """An unscannable workflow forces this fact to `unmeasured` — rightly: the
    workflow nobody could read might be the one holding an always-running
    producer. So the API round-trips that would compute a verdict nothing reads
    must not be spent."""
    calls = []

    def fetch(repo):
        calls.append(repo)
        return ["test"], "branch `main`"

    root, files = _repo(tmp_path, {"ci.yml": _CONDITIONAL_ONLY})
    out = cf.compute_config_facts(
        root, files,
        [{"workflow_file": "broken.yml"}],
        repo="owner/repo", required_contexts_fetcher=fetch,
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"))
    assert _outcome(out, _FACT)["outcome"] == "unmeasured"
    assert calls == [], f"branch protection was read for a discarded verdict: {calls}"


def test_a_non_matrix_job_does_not_claim_a_matrix_expanded_context(tmp_path):
    """`name (value)` is a MATRIX expansion, so only a job that has a matrix
    can produce it. Prefix-matching every job means an always-running verdict
    job named `test` is read as a producer of `test (self-hosted)` — the exact
    shape of this repository's own CI — and its always-runs answer then covers
    for the suite job that really reports that context and really can skip.
    The bypass hides behind the fix for the bypass."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  suite:
    name: test (self-hosted)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
  verdict:
    name: test
    needs: [suite]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo assert
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test (self-hosted)"]),
                 _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_real_matrix_job_still_maps_to_its_expanded_context(tmp_path):
    """The positive control for the rule above: the expansion match has to keep
    working for jobs that actually carry a matrix, or every matrix-gated repo
    silently stops being judged."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11', '3.12']
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test (3.12)"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]
    assert "test (3.12)" in f["evidence"]


def test_a_matrix_job_only_produces_its_own_expansions(tmp_path):
    """A matrix over `3.11`/`3.12` expands to `test (3.11)` and `test (3.12)`
    — never `test (self-hosted)`. Matching the bare `name (` prefix lets an
    always-running matrix job alibi a completely unrelated required context,
    which is the same false green as before with one more step of indirection."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11', '3.12']
    steps:
      - run: pytest -v
  suite:
    name: test (self-hosted)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test (self-hosted)"]),
                 _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_matrix_does_not_produce_a_combination_it_cannot_run(tmp_path):
    """A flattened value set says yes to any context whose tokens appear
    ANYWHERE in the matrix — including a combination the matrix excludes, an
    axis order it never renders, and a single value drawn from a two-axis
    matrix. So an always-running matrix job over `[self-hosted, ubuntu]` was
    read as the producer of `test (self-hosted)`, which a skippable job really
    reports. Only combinations the matrix can actually run count."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [self-hosted, ubuntu]
        py: ['3.12']
    steps:
      - run: pytest -v
  suite:
    name: test (self-hosted)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test (self-hosted)"]),
                 _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_an_excluded_matrix_combination_is_not_a_producer(tmp_path):
    """`exclude:` removes a combination, so the job never renders that name."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [ubuntu, windows]
        py: ['3.11', '3.12']
        exclude:
          - os: windows
            py: '3.11'
    steps:
      - run: pytest -v
"""
    out = _facts_with(tmp_path / "a", {"ci.yml": body}, ["test (windows, 3.11)"])
    assert _outcome(out, _FACT)["outcome"] == "unmeasured"
    out2 = _facts_with(tmp_path / "b", {"ci.yml": body}, ["test (windows, 3.12)"])
    assert _outcome(out2, _FACT)["outcome"] == "pass"


def _protection_gh(rulesets, classic):
    """A stubbed `gh` whose two protection endpoints answer independently.

    Drives the REAL `_required_contexts_via_gh`, so the shipped parsing and the
    partial-read guard are what gets exercised.
    """
    class _Gh:
        @staticmethod
        def check_prereqs():
            return True

        @staticmethod
        def run_gh_api(path, **kw):
            if path == "repos/owner/repo":
                return json.dumps({"default_branch": "main"})
            answer = rulesets if "/rules/branches/" in path else classic
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer)
    return _Gh


_RULESET_WITH_TEST = [{
    "type": "required_status_checks",
    "parameters": {"required_status_checks": [{"context": "test"}]},
}]


def test_a_partially_read_protection_is_never_reported_as_complete():
    """Branch protection has two sources and a repository can use either. When
    one answers and the other ERRORS for a reason that is not the ordinary
    admin-only 403 — a rate limit, a timeout, a 5xx — the contexts that came
    back are a PARTIAL set, and returning them as complete means a required
    check configured in the unread source is invisible. The fact then renders a
    measured pass, counts toward `passed`, and never appears in `unmeasured`,
    so a consumer blends it as clean and fully measured."""
    for label, rulesets, classic in (
        ("classic errors", _RULESET_WITH_TEST, RuntimeError("HTTP 500")),
        ("rulesets errors", RuntimeError("HTTP 429 rate limit"),
         {"contexts": ["test"]}),
    ):
        with mock.patch.object(cf, "_gh_utils",
                               lambda r=rulesets, c=classic: _protection_gh(r, c)):
            contexts, detail = cf._required_contexts_via_gh("owner/repo")
        assert contexts is None, (label, contexts, detail)
        assert "part" in detail or "could not be read" in detail, (label, detail)


def test_an_unread_source_plus_an_empty_one_is_not_no_required_checks():
    """The worst shape of the same defect: the source that answered says
    "nothing required" and the other was never read at all, which came back as
    `pass — requires no status check`. That is a claim about a source the scan
    never saw."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(RuntimeError("HTTP 429"),
                               {"contexts": [], "checks": []})):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts is None, (contexts, detail)


def test_the_admin_only_403_with_a_ruleset_answer_still_measures():
    """The positive control that keeps the guard honest. A 403 from the
    admin-only classic endpoint is ORDINARY, and when the rulesets endpoint
    returned real contexts the scan has a complete answer from the source the
    repository actually uses — refusing that would make the fact unmeasurable
    for every non-admin, which is most of its readers."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(
            _RULESET_WITH_TEST,
            RuntimeError("HTTP 403: Must have admin rights to Repository."))):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["test"], (contexts, detail)


def test_both_sources_readable_unions_their_contexts():
    """Neither source alone is authoritative, so both are read and unioned —
    including classic's `checks` list, which is the newer spelling beside
    `contexts`."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(
            _RULESET_WITH_TEST,
            {"contexts": ["lint"], "checks": [{"context": "build"}]})):
        contexts, _ = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["build", "lint", "test"], contexts


def test_a_bare_needs_chain_is_not_an_always_running_producer(tmp_path):
    """A job with `needs:` and no condition of its own is SKIPPED when a job it
    needs fails — and a skipped required check is exactly what GitHub reports
    as passed. Calling it "runs whatever else happens" describes the opposite
    of what it does. The predicate is the one the fix recipe names: the
    producer itself carries `always()` / `!cancelled()` / `success() ||
    failure()`."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  suite:
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
  test:
    needs: [suite]
    runs-on: ubuntu-latest
    steps:
      - run: echo verdict
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]
    assert "suite" in f["evidence"]


def test_needs_as_a_scalar_string_is_read(tmp_path):
    """`needs: build` is at least as common as `needs: [build]`, and the
    failure direction of ignoring it is the false green."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  build:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - run: make
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]
    assert "build" in f["evidence"]


def test_conditions_that_cannot_be_false_are_not_bypasses(tmp_path):
    """These all run the job every time, so failing them reds a repository that
    did nothing wrong — and `if: ${{ always() }}` is the spelling GitHub's own
    documentation shows, which makes it a false RED against exactly the repos
    that implemented the recommended fix."""
    # `!` opens a YAML tag, so the bare `!cancelled()` spelling does not exist
    # in real workflows — it is written quoted or inside `${{ }}`.
    for cond in ("true", "always()", "${{ always() }}", "'!cancelled()'",
                 "${{ !cancelled() }}", "success() || failure()",
                 "${{ success()||failure() }}",
                 "success() || failure() || cancelled()", "success()"):
        body = f"""\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: {cond}
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
        out = _facts_with(tmp_path / cond.replace("/", "_").replace(" ", "_")[:24],
                          {"ci.yml": body}, ["test"])
        assert _outcome(out, _FACT)["outcome"] == "pass", (cond, _outcome(out, _FACT))


def test_success_with_a_needs_chain_is_still_skippable(tmp_path):
    """The counterpart that keeps the rule above honest: `if: success()` runs
    every time only when nothing upstream can fail first."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make
  test:
    needs: [build]
    if: success()
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_plainly_bypassable_producer_outranks_an_unknown_one(tmp_path):
    """A producer that demonstrably can skip is a finding whatever a second,
    unreadable producer might have done. Checking the unknown first downgraded
    a real fail to "not judged"."""
    a = """\
name: a
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    b = """\
name: b
on: [pull_request]
permissions:
  contents: read
jobs:
  first:
    needs: [second]
    runs-on: ubuntu-latest
    steps:
      - run: echo
  second:
    needs: [first]
    name: test
    runs-on: ubuntu-latest
    steps:
      - run: echo
"""
    f = _outcome(_facts_with(tmp_path, {"a.yml": a, "b.yml": b}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_pass_requires_every_required_check_to_have_been_traced(tmp_path):
    """The normal shape of a mature repo is a dozen required contexts, most of
    them from external apps. Returning `pass` on the strength of ONE traced
    context makes the machine outcome say "no required check can be bypassed"
    about eleven checks nobody looked at — and it counts toward `passed` and
    never reaches `unmeasured`, so the caveat never fires either."""
    out = _facts_with(tmp_path, {"ci.yml": _VERDICT_PATTERN},
                      ["test", "codecov/project", "vercel"])
    f = _outcome(out, _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    assert "codecov/project" in f["evidence"]
    assert _FACT in out["unmeasured"]


# ---------------------------------------------------------------------------
# The real API fetchers, on the paths where they SUCCEED.
#
# Everything above drives a fetcher's failure branch, or injects a recorded
# answer past it. That leaves the two things a fetcher does when it works —
# which endpoint it asks for, and which key of the answer it reads — pinned by
# nothing. Both failure modes degrade the same quiet way: the call comes back
# empty or 404, the fact renders `unmeasured: ... could not be read`, and that
# row is indistinguishable from the ordinary unauthenticated scan. A fact that
# was unmeasurable on every repository in the world would look exactly like
# this and no test would go red.
# ---------------------------------------------------------------------------

def test_the_branch_protection_fetcher_asks_for_the_two_documented_endpoints():
    """A mistyped endpoint path 404s for every repository, and this function
    turns that into "branch protection could not be read" — so the required-
    checks fact would never be measured anywhere, while looking like a missing
    token. The stub answers ONLY the two documented paths and 404s anything
    else, and the requested paths are asserted literally."""
    paths = []

    class _Gh:
        @staticmethod
        def check_prereqs():
            return True

        @staticmethod
        def run_gh_api(path, **kw):
            paths.append(path)
            if path == "repos/owner/repo":
                return json.dumps({"default_branch": "main"})
            if path == "repos/owner/repo/rules/branches/main":
                return json.dumps([{
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "test"}]},
                }])
            if path == ("repos/owner/repo/branches/main/protection/"
                        "required_status_checks"):
                return json.dumps({"contexts": ["lint"]})
            raise RuntimeError(f"HTTP 404: no such endpoint {path}")

    with mock.patch.object(cf, "_gh_utils", lambda: _Gh):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")

    assert contexts == ["lint", "test"], (contexts, detail)
    assert paths == [
        "repos/owner/repo",
        "repos/owner/repo/rules/branches/main",
        "repos/owner/repo/branches/main/protection/required_status_checks",
    ], paths


def test_the_fork_approval_fetcher_reads_the_documented_endpoint_and_key():
    """GitHub returns the setting under `approval_policy`. Read under any other
    key the body yields `None`, which this fetcher reports as "the endpoint
    returned no `approval_policy` value" — an unmeasured row that looks like an
    unreadable setting, so the weakest tier would never be failed on any
    repository. The stub carries a DECOY `policy` key holding the opposite
    verdict, so a fetcher reading the wrong key returns a passing value."""
    paths = []

    class _Gh:
        @staticmethod
        def check_prereqs():
            return True

        @staticmethod
        def run_gh_api(path, **kw):
            paths.append(path)
            if path == ("repos/owner/repo/actions/permissions/"
                        "fork-pr-contributor-approval"):
                return json.dumps({
                    "approval_policy": "first_time_contributors_new_to_github",
                    "policy": "all_external_contributors"})
            raise RuntimeError(f"HTTP 404: no such endpoint {path}")

    with mock.patch.object(cf, "_gh_utils", lambda: _Gh):
        policy, detail = cf._fork_approval_via_gh("owner/repo")

    assert policy == "first_time_contributors_new_to_github", (policy, detail)
    assert paths == ["repos/owner/repo/actions/permissions/"
                     "fork-pr-contributor-approval"], paths


# ---------------------------------------------------------------------------
# A fetcher that RAISES. Both facts wrap the injected call in an `except`, and
# nothing exercised it: a fetcher is allowed to fail the loud way (`gh` killed
# mid-call, a body that will not decode), and an exception escaping here takes
# the whole scan down — a scan that dies renders no report at all, which is a
# worse outcome than the one unmeasured row this is supposed to become.
# ---------------------------------------------------------------------------

def test_a_branch_protection_fetcher_that_raises_becomes_one_unmeasured_row(
    tmp_path,
):
    def exploding(repo):
        raise RuntimeError("gh subprocess died")

    root, files = _repo(tmp_path, {"ci.yml": _CONDITIONAL_ONLY})
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=exploding,
        fork_approval_fetcher=lambda repo: ("all_external_contributors", "x"))
    f = _outcome(out, _FACT)
    assert f["outcome"] == "unmeasured", f
    assert "gh subprocess died" in f["evidence"], f["evidence"]
    assert _FACT in out["unmeasured"]
    # The rest of the table still resolved — the failure is contained.
    assert out["scored_count"] == 7 and out["applicable_count"] == 8


def test_a_fork_approval_fetcher_that_raises_becomes_one_unmeasured_row(
    tmp_path,
):
    def exploding(repo):
        raise RuntimeError("gh subprocess died")

    root, files = _repo(tmp_path, {"ci.yml": _SAFE_WF})
    out = cf.compute_config_facts(
        root, files, [], repo="owner/repo",
        required_contexts_fetcher=lambda repo: ([], "branch `main`"),
        fork_approval_fetcher=exploding)
    f = _outcome(out, _FORK_FACT)
    assert f["outcome"] == "unmeasured", f
    assert "gh subprocess died" in f["evidence"], f["evidence"]
    assert _FORK_FACT in out["unmeasured"]
    assert out["scored_count"] == 7 and out["applicable_count"] == 8


# ---------------------------------------------------------------------------
# The producer-matching guards that make an UNKNOWABLE job name produce no
# match at all. Each is the same false green in a new spelling: an
# always-running job stands in as the producer of a context that a skippable
# job beside it is what really reports, and the fact renders "every required
# check is produced by a job that always runs".
# ---------------------------------------------------------------------------

def test_a_matrix_carrying_include_produces_no_expansion_at_all(tmp_path):
    """`include:` can add combinations, rename axes and extend existing ones,
    so what the job renders is not knowable from the YAML. Enumerating the
    plain axes anyway and ignoring `include:` lets an always-running job named
    `test` alibi `test (windows)` — which the skippable job beside it is what
    really reports."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [ubuntu, windows]
        include:
          - os: windows
            toolchain: msvc
    steps:
      - run: pytest -v
  suite:
    name: test (windows)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test (windows)"]),
                 _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_matrix_whose_values_are_computed_produces_no_expansion(tmp_path):
    """An axis value that is an expression renders to something the YAML does
    not contain — `${{ vars.RUNNER }}` could be any label at all, and a whole
    axis built by `fromJSON` could be any list. Treating the unexpanded text as
    a value is the same alibi as above: an always-running job covers for the
    skippable job that really reports the context."""
    expression_value = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: ['${{ vars.RUNNER }}', windows]
    steps:
      - run: pytest -v
  suite:
    name: test (windows)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(
        _facts_with(tmp_path / "expr", {"ci.yml": expression_value},
                    ["test (windows)"]),
        _FACT)
    assert f["outcome"] == "fail", f["evidence"]

    computed_axis = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: ${{ fromJSON(needs.setup.outputs.targets) }}
    steps:
      - run: pytest -v
  suite:
    name: test (windows)
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f2 = _outcome(
        _facts_with(tmp_path / "fromjson", {"ci.yml": computed_axis},
                    ["test (windows)"]),
        _FACT)
    assert f2["outcome"] == "fail", f2["evidence"]


def test_a_templated_job_name_is_never_matched_as_a_producer(tmp_path):
    """What `name: test ${{ matrix.os }}` renders to is not knowable here, so
    no context can be attributed to that job. A maintainer who typed the job's
    own `name:` line into the required-checks box has a check nothing will ever
    report; matching the literal templated text would answer that with "this
    check has a producer that runs whatever happens" — a green claim about a
    name that renders differently on every run."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  unit:
    name: test ${{ matrix.os }}
    if: always()
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [ubuntu, windows]
    steps:
      - run: pytest -v
"""
    f = _outcome(
        _facts_with(tmp_path, {"ci.yml": body}, ["test ${{ matrix.os }}"]),
        _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    assert "no job in these workflows reports it" in f["evidence"], f["evidence"]


def test_a_needs_cycle_terminates_and_is_not_an_all_clear(tmp_path):
    """Two jobs that `needs:` each other is a shape GitHub rejects but a
    scanner still meets in a work-in-progress branch. The claim the code makes
    about it is availability — the walk must not hang — and this test returning
    at all is that claim's only proof. The second half is the verdict: a cycle
    leaves the skip walk with no answer, so the honest outcome is "not judged".
    Resolving it to "always runs" would render a green row about a job whose
    runs nobody can predict."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  build:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - run: make
  test:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    assert "cycle" in f["evidence"], f["evidence"]


def test_the_skip_walk_visits_each_job_once(tmp_path):
    """A `needs:` graph that branches revisits the same subtree down every
    path, so an unmemoized walk is exponential in depth — the cycle guard's
    docstring promises a malformed graph "must not hang the scan".

    Asserted as a CALL-COUNT invariant rather than a wall-clock bound: a time
    limit on a private helper measures the machine, not the code, and would
    redden a correct fix on a loaded CI runner."""
    depth = 12
    jobs: dict[str, dict] = {}
    for i in range(depth):
        jobs[f"a{i}"] = {"needs": [f"a{i + 1}", f"b{i + 1}"]}
        jobs[f"b{i}"] = {"needs": [f"a{i + 1}", f"b{i + 1}"]}
    jobs[f"a{depth}"] = {"if": "github.event_name == 'push'"}
    jobs[f"b{depth}"] = {"if": "github.event_name == 'push'"}

    visits: list[str] = []
    real = cf._skip_path

    def counting(all_jobs, key, seen=frozenset(), memo=None):
        visits.append(key)
        return real(all_jobs, key, seen, memo)

    with mock.patch.object(cf, "_skip_path", counting):
        answer = counting(jobs, "a0")

    # Proportional to the GRAPH (one call per edge, give or take), never
    # exponential in its depth: 47 calls here against 8,191 before memoizing.
    assert len(visits) <= 4 * len(jobs), len(visits)
    assert answer is not None            # the chain really is skippable


# ---------------------------------------------------------------------------
# X1/R8 — a 404 from classic branch protection is an ANSWER, and an unread
# source has to be disclosed even when the other source answered.
# ---------------------------------------------------------------------------

def test_a_404_from_classic_protection_means_not_protected_not_unread():
    """GitHub returns `404 Branch not protected` precisely when classic
    protection is NOT configured — the normal state of every repository that
    uses rulesets, which is the population this fact was built for. Treating
    that answer as a failure to read makes the fact unmeasurable for all of
    them: a repo with a genuinely bypassable required check scores HIGHER than
    the same repo with classic protection configured empty, because unmeasured
    facts score nothing while a fail scores zero."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(_RULESET_WITH_TEST,
                               RuntimeError("HTTP 404: Branch not protected"))):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["test"], (contexts, detail)
    assert "could not be read" not in detail, detail


def test_a_404_is_only_an_answer_from_the_protection_endpoint():
    """The rulesets endpoint and the repository endpoint have no "not
    configured" 404 — a 404 there is a mistyped path or a missing repository,
    and accepting it as "nothing required" would make every repository in the
    world read as unprotected."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(RuntimeError("HTTP 404: Not Found"),
                               {"contexts": [], "checks": []})):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts is None, (contexts, detail)


def test_an_admin_only_403_discloses_the_source_it_could_not_read():
    """The common non-admin case: rulesets answered, classic 403'd, and the
    union is returned. It is returned as COMPLETE — "all 1 required check(s)"
    — while a check configured only in classic is invisible and the count is
    wrong. The docstring reasons about this correctly; the sentence the reader
    sees has to carry it too."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(
            _RULESET_WITH_TEST,
            RuntimeError("HTTP 403: Must have admin rights to Repository."))):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["test"], contexts
    assert "classic branch protection could not be read" in detail, detail
    assert "read from rulesets only" in detail, detail


def test_a_branch_filtered_push_workflow_cannot_veto_the_real_producer(tmp_path):
    """The tag-only shape was rejected; `on: push: branches: [main]` was not —
    and it is far commoner. A deploy workflow with a job named `test` never
    runs on a pull request, so accepting it as an always-running producer turns
    the real, gated producer's bypass green."""
    ci = """\
name: ci
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    deploy = """\
name: deploy
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make smoke
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": ci, "deploy.yml": deploy},
                             ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_an_unfiltered_push_workflow_is_still_a_producer(tmp_path):
    """The control: a plain `on: push` workflow shares its head commit with a
    same-repo pull request, so its checks really do land on the PR."""
    body = """\
name: ci
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "pass", f["evidence"]


def test_a_rulesets_body_of_an_unexpected_shape_is_unread_not_empty():
    """The rulesets arm skipped a non-list body and still marked the source
    READ, so an unexpected payload — an error object, a paginated envelope —
    came back as "this branch requires nothing". The classic arm treats an
    unexpected shape as unread; both sources have to fail the same way, or the
    asymmetry decides the verdict."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh({"message": "Bad credentials"},
                               {"contexts": ["lint"]})):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    # Classic answered, so a check IS required — but the rulesets source was
    # not read, and a check required only there would be invisible.
    assert contexts is None or "part" in detail, (contexts, detail)


def test_a_constant_true_expression_is_not_a_bypass(tmp_path):
    """`if: ${{ 1 == 1 }}` cannot be false, so failing it reds a repository
    that did nothing wrong. `if: true` was already handled; the wrapped
    constant comparison is the same statement written the long way."""
    for cond in ("${{ 1 == 1 }}", "${{ true }}", "${{ 'a' == 'a' }}"):
        body = f"""\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: {cond}
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
        out = _facts_with(tmp_path / cond.replace("/", "_").replace(" ", "_")[:20],
                          {"ci.yml": body}, ["test"])
        assert _outcome(out, _FACT)["outcome"] == "pass", (cond, _outcome(out, _FACT))


# ---------------------------------------------------------------------------
# G1 — a producer's ability to report is THREE-valued. Collapsing it to a
# boolean re-opened the "unmeasurable scores better than failing" lever this
# whole fact exists to close: a repository with a genuinely bypassable required
# check came back unmeasured (scoring nothing) instead of failing.
# ---------------------------------------------------------------------------

_SKIPPABLE_PUSH = """\
name: ci
on:
  push:
%s
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""


@pytest.mark.parametrize("filters", [
    "    branches: ['**']\n",
    "    branches: ['**']\n    tags: ['v*']\n",
    "    paths: ['src/**']\n",
    "    paths-ignore: ['docs/**']\n",
    "    branches: [main]\n",
])
def test_a_filtered_push_producer_is_never_silently_dropped(tmp_path, filters):
    """Dropping the only producer makes the fact UNMEASURED, and an unmeasured
    fact scores nothing while a fail scores zero — so a repository whose
    required check really can be bypassed came out ahead of one that configured
    protection properly. Whatever the filter, a producer that can skip has to
    reach the verdict."""
    out = _facts_with(tmp_path / filters.strip()[:14].replace("/", "_").replace(" ", "_"),
                      {"ci.yml": _SKIPPABLE_PUSH % filters}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "fail", _outcome(out, _FACT)


def test_a_push_over_every_branch_can_certify_a_check(tmp_path):
    """The reverse direction. `branches: ['**']` matches every branch push, so
    an always-running job in it really does report on a same-repo pull request
    — vetoing it turned a correctly-gated repository RED."""
    always = """\
name: ci
on:
  push:
    branches: ['**']
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    skippable = """\
name: pr
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    out = _facts_with(tmp_path, {"a.yml": always, "b.yml": skippable}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


def test_a_plain_push_workflow_is_a_producer_on_its_own(tmp_path):
    """The control the previous one lacked: its fixture carried `pull_request:`
    alongside `push:`, so the assertion passed no matter what the push arm did
    — a mutant rejecting every push workflow left the suite green. This fixture
    has NO `pull_request:` key, so it tests only the push arm."""
    body = """\
name: ci
on: push
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    out = _facts_with(tmp_path, {"ci.yml": body}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


def test_a_tag_only_push_workflow_still_cannot_certify(tmp_path):
    """And the one veto that IS provable: no pull-request branch push matches a
    tags-only filter, so a same-named job there is not what gates the PR."""
    ci = """\
name: ci
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    release = """\
name: release
on:
  push:
    tags: ['v*']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make smoke
"""
    out = _facts_with(tmp_path, {"ci.yml": ci, "release.yml": release}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "fail", _outcome(out, _FACT)


def test_a_branch_filtered_push_cannot_certify_either(tmp_path):
    """R1's shape, which must stay closed: a `deploy.yml` filtered to
    `branches: [main]` may or may not run on a pull request's head branch —
    unknowable — so it can never be the evidence that a check always reports.
    Unknown is not the same as absent: it just cannot certify."""
    ci = """\
name: ci
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    deploy = """\
name: deploy
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make smoke
"""
    out = _facts_with(tmp_path, {"ci.yml": ci, "deploy.yml": deploy}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "fail", _outcome(out, _FACT)


def test_the_partial_read_names_the_source_it_could_not_read():
    """The sentence hardcoded "read from rulesets only — {unread} is
    admin-only" with the name cut from the error list, so when RULESETS is the
    arm that 403s (a GitHub App token, a fine-grained PAT) the shipped markdown
    told the reader that rulesets — the source that ANSWERED — could not be
    read. Both halves have to come from which source actually succeeded."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(
            RuntimeError("HTTP 403: Resource not accessible by integration"),
            {"contexts": ["lint"]})):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["lint"], (contexts, detail)
    assert "rulesets" in detail, detail
    assert "read from rulesets only" not in detail, detail


def test_a_404_for_a_reason_other_than_unprotected_is_still_unread():
    """The comment says the 404 is matched on status AND reason; the regex was
    `\\b404\\b`, so a branch renamed between the two calls, a plan-gated
    endpoint, even a 502 mentioning 404 in its body, all became "nothing
    required here" — a measured PASS over a source never read."""
    for message in ("HTTP 404: Not Found",
                    "Upgrade to GitHub Pro to use this feature (HTTP 404)",
                    "HTTP 502: upstream returned 404 for 1 of 3 shards"):
        with mock.patch.object(
            cf, "_gh_utils",
            lambda m=message: _protection_gh(_RULESET_WITH_TEST,
                                             RuntimeError(m))):
            contexts, detail = cf._required_contexts_via_gh("owner/repo")
        assert contexts is None, (message, contexts, detail)


def test_the_documented_not_protected_404_is_still_an_answer():
    """The control: the reason GitHub actually returns when classic protection
    is not configured stays an answer, which is X1's whole point."""
    with mock.patch.object(
        cf, "_gh_utils",
        lambda: _protection_gh(_RULESET_WITH_TEST,
                               RuntimeError("HTTP 404: Branch not protected"))):
        contexts, detail = cf._required_contexts_via_gh("owner/repo")
    assert contexts == ["test"], (contexts, detail)


def test_the_memo_returns_the_same_answer_it_computed(tmp_path):
    """The memo's only test asserted a CALL COUNT and `is not None` — and
    `_Unknown` is not None, so a mutant returning `_Unknown` on every cache hit
    flipped a diamond graph from fail to unmeasured with the suite green. The
    cached answer has to be the answer."""
    depth = 6
    jobs: dict[str, dict] = {}
    for i in range(depth):
        jobs[f"a{i}"] = {"needs": [f"a{i + 1}", f"b{i + 1}"]}
        jobs[f"b{i}"] = {"needs": [f"a{i + 1}", f"b{i + 1}"]}
    jobs[f"a{depth}"] = {"if": "github.event_name == 'push'"}
    jobs[f"b{depth}"] = {"if": "github.event_name == 'push'"}

    answer = cf._skip_path(jobs, "a0")
    assert answer is not None, answer
    assert not isinstance(answer, cf._Unknown), answer
    assert "a0" in answer, answer
    # The same graph walked without any memo must agree exactly.
    assert answer == cf._skip_path(jobs, "a0", frozenset(), {}), answer


def test_success_or_failure_with_needs_is_skippable(tmp_path):
    """`success() || failure()` is NOT equivalent to `always()` once the job
    has `needs:`. If a dependency is SKIPPED, neither predicate is true, so
    GitHub skips the verdict job too — and a skipped required check is exactly
    what it reports as passed. Treating it as never-skipping certified the one
    shape this fact recommends, so the recommended fix was itself bypassable.
    `always()` and `!cancelled()` both still run when a dependency skips."""
    body = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  suite:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
  test:
    needs: [suite]
    if: success() || failure()
    runs-on: ubuntu-latest
    steps:
      - run: echo verdict
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": body}, ["test"]), _FACT)
    assert f["outcome"] == "fail", f["evidence"]


@pytest.mark.parametrize("cond", ["always()", "${{ always() }}", "'!cancelled()'"])
def test_the_recommended_verdict_conditions_still_certify(tmp_path, cond):
    """The control: the conditions the fix recipe actually recommends do run
    when a dependency is skipped, so the verdict job still reports."""
    body = f"""\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  suite:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
  test:
    needs: [suite]
    if: {cond}
    runs-on: ubuntu-latest
    steps:
      - run: echo verdict
"""
    out = _facts_with(tmp_path / cond.strip("'${} ()!")[:10], {"ci.yml": body},
                      ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


def test_a_matrix_job_does_not_produce_the_bare_context(tmp_path):
    """A matrix job named `test` emits `test (3.11)` and `test (3.12)` — never
    the bare `test`. Matching the exact display name before considering the
    expansion certified a required context that NOTHING emits, so a branch
    whose required check can never report read as a measured pass. It is
    unproduced, and routes where every other unproduced context does."""
    matrix_only = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11', '3.12']
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": matrix_only}, ["test"]), _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]
    # The near miss is named, because "no job produces it" would send a reader
    # hunting for an external app that is not there.
    assert "matrix" in f["evidence"], f["evidence"]
    assert "`test`" in f["evidence"], f["evidence"]


def test_a_matrix_job_still_produces_its_expansions(tmp_path):
    """The control on the other side: the contexts a matrix job really does
    emit must keep being judged."""
    matrix_only = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11', '3.12']
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": matrix_only}, ["test (3.11)"]),
                 _FACT)
    assert f["outcome"] == "pass", f["evidence"]


def test_a_plain_job_still_produces_its_bare_context(tmp_path):
    """And the control that keeps the exact-name match working for every job
    that has no matrix — which is most of them."""
    plain = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": plain}, ["test"]), _FACT)
    assert f["outcome"] == "pass", f["evidence"]


def test_a_bare_context_with_a_skippable_matrix_job_is_not_a_fail(tmp_path):
    """The near miss must not fabricate the opposite claim either: a matrix
    job that CAN skip still does not produce the bare context, so the honest
    answer stays "not judged", not "bypassable"."""
    matrix_skippable = """\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ['3.11']
    steps:
      - run: pytest -v
"""
    f = _outcome(_facts_with(tmp_path, {"ci.yml": matrix_skippable}, ["test"]),
                 _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]


@pytest.mark.parametrize("cond", ["true", "${{ true }}", "${{ 1 == 1 }}"])
def test_a_constant_condition_does_not_certify_a_job_with_needs(tmp_path, cond):
    """A constant `if:` does not make a job unskippable once it has `needs:`.
    GitHub skips a dependent when a dependency skips unless the condition is
    `always()` or `!cancelled()`; `if: true` is neither. Certifying it was the
    same false green as `success() || failure()`, one condition over — and the
    check it certified can be satisfied without the suite running."""
    body = f"""\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  suite:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
  test:
    needs: [suite]
    if: {cond}
    runs-on: ubuntu-latest
    steps:
      - run: echo verdict
"""
    out = _facts_with(tmp_path / cond.strip("${} =1"), {"ci.yml": body}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "fail", _outcome(out, _FACT)


@pytest.mark.parametrize("cond", ["true", "${{ 1 == 1 }}"])
def test_a_constant_condition_still_certifies_without_needs(tmp_path, cond):
    """The control: with nothing upstream to skip, a constant condition really
    cannot be false, and reding it would be a false RED on a repo that did
    nothing wrong (which is why M5 accepted these in the first place)."""
    body = f"""\
name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    if: {cond}
    runs-on: ubuntu-latest
    steps:
      - run: pytest -v
"""
    out = _facts_with(tmp_path / cond.strip("${} =1"), {"ci.yml": body}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


# ---------------------------------------------------------------------------
# F1 — a path filter is a reason a push may NOT run, whatever its branch filter
# says. The match-all-branches shortcut returned before the paths filter was
# ever looked at.
# ---------------------------------------------------------------------------

_PATHS_PUSH_PRODUCER = """\
name: build
on:
  push:
    branches: ['**']
    paths: ['src/**']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make build
"""

_SKIPPABLE_PR_PRODUCER = """\
name: pr
on: pull_request
permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""


def test_a_path_filtered_push_cannot_certify_even_over_every_branch(tmp_path):
    """`branches: ['**']` was answered before `paths:` was read, so an
    always-running job in a path-filtered push certified the required check —
    and on a pull request that touches nothing under `paths:` that workflow
    never runs, while the real producer skips. The check greens with neither
    job having reported."""
    f = _outcome(
        _facts_with(tmp_path, {"build.yml": _PATHS_PUSH_PRODUCER,
                               "pr.yml": _SKIPPABLE_PR_PRODUCER}, ["test"]),
        _FACT)
    assert f["outcome"] == "fail", f["evidence"]


@pytest.mark.parametrize("filters", [
    "    branches: ['**']\n    paths: ['src/**']\n",
    "    branches: ['**']\n    paths-ignore: ['docs/**']\n",
])
def test_a_path_filtered_push_still_counts_against_the_check(tmp_path, filters):
    """The other half of UNKNOWN, and the F13 trap: unable-to-certify must not
    become unable-to-run. A SKIPPABLE job in such a workflow still counts
    against the required check, so its bypass is still found."""
    body = f"""\
name: ci
on:
  push:
{filters}permissions:
  contents: read
jobs:
  test:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    out = _facts_with(tmp_path / filters[14:24].strip().replace("/", "_"),
                      {"ci.yml": body}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "fail", _outcome(out, _FACT)


def test_an_unfiltered_all_branches_push_still_certifies(tmp_path):
    """The control that keeps the fix narrow: with no path filter,
    `branches: ['**']` really does run on every branch push, so an
    always-running job there still certifies. Reding this would be the
    over-correction G1 was about."""
    always = """\
name: ci
on:
  push:
    branches: ['**']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
    out = _facts_with(tmp_path, {"a.yml": always,
                                 "b.yml": _SKIPPABLE_PR_PRODUCER}, ["test"])
    assert _outcome(out, _FACT)["outcome"] == "pass", _outcome(out, _FACT)


def test_a_path_filtered_push_alone_is_not_a_false_red(tmp_path):
    """And the F13 direction: an ALWAYS-running job in a path-filtered push,
    as the only producer, is not evidence of a bypass — it is simply not
    knowable whether it reports. Not judged, not failed."""
    f = _outcome(_facts_with(tmp_path, {"build.yml": _PATHS_PUSH_PRODUCER},
                             ["test"]), _FACT)
    assert f["outcome"] == "unmeasured", f["evidence"]


def test_a_branch_exclusion_also_defeats_the_match_all_shortcut(tmp_path):
    """`branches: ['**']` alongside `branches-ignore:` cannot be shown to run
    on every branch — the two filters contradict each other, and whichever way
    GitHub resolves that, "runs on every push" is not a claim this scan can
    make. The shortcut answered before reading the exclusion, so an
    always-running job there certified the check while the reachable
    pull-request producer could skip."""
    both = """\
name: build
on:
  push:
    branches: ['**']
    branches-ignore: ['release/**']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make build
"""
    f = _outcome(
        _facts_with(tmp_path, {"build.yml": both,
                               "pr.yml": _SKIPPABLE_PR_PRODUCER}, ["test"]),
        _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_a_negated_pattern_means_the_branch_filter_is_not_match_all(tmp_path):
    """GitHub's documented way to exclude branches while including others is a
    `!` pattern INSIDE `branches:` — `branches` and `branches-ignore` cannot
    filter the same event, so this is the expressible form of "match all
    except". Reading `'**'` and ignoring the exclusion beside it certified a
    check that never reports on an excluded branch, while the real producer
    could skip."""
    negated = """\
name: build
on:
  push:
    branches:
      - '**'
      - '!release/**'
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make build
"""
    f = _outcome(
        _facts_with(tmp_path, {"build.yml": negated,
                               "pr.yml": _SKIPPABLE_PR_PRODUCER}, ["test"]),
        _FACT)
    assert f["outcome"] == "fail", f["evidence"]


def test_an_unnegated_match_all_branch_filter_still_certifies(tmp_path):
    """The control: `branches: ['**']` with nothing excluded really does run on
    every branch push, and must keep certifying."""
    plain = """\
name: build
on:
  push:
    branches: ['**']
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make build
"""
    f = _outcome(
        _facts_with(tmp_path, {"build.yml": plain,
                               "pr.yml": _SKIPPABLE_PR_PRODUCER}, ["test"]),
        _FACT)
    assert f["outcome"] == "pass", f["evidence"]
