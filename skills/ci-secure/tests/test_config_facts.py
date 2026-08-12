"""Oracle tests for the scored security config facts.

This number is a third of a published CI Score, so every test pins a property a
graded maintainer could dispute: what each fact counts, what clears it, what
must never silently pass, and how the score treats a fact it could not measure.
"""
from __future__ import annotations

import importlib.util
import sys
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
    out = cf.compute_config_facts(root, files, [])
    assert out["score"] == 100.0
    assert out["passed"] == out["scored_count"] == 6
    for f in out["facts"]:
        assert f["outcome"] == "pass", f"{f['fact_id']} failed: {f['evidence']}"


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
    # ...and it is a coverage gap, not a silent 5/6.
    assert out["scored_count"] == 5 and out["applicable_count"] == 6
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
        else:
            assert f["outcome"] == "unmeasured", (
                f"{f['fact_id']} resolved despite an unscanned workflow — "
                "that is a silent pass over a coverage hole"
            )
            assert "broken.yml" in f["evidence"]
    assert out["scored_count"] == 1
    assert out["applicable_count"] == 6
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
    out = cf.compute_config_facts(root, files, [])
    # bad.yml fails exactly one fact (permissions-declares); 5/6 pass.
    assert out["passed"] == 5 and out["scored_count"] == 6
    assert out["score"] == pytest.approx(round(100 * 5 / 6, 1))
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
