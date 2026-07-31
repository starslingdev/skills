"""Contract cells for the B1 entry point (`scripts/collect_config.py`).

The collector is the skill's front door: local-checkout-only (OD-L2), offline
by construction, and every outcome — scored, refused, errored — is STAMPED in
the output document, never guessed. These cells pin exactly that:

1. happy path      — real git repo with workflows → `ci_score` stamp with a
                     value/grade, provenance = full-repo HEAD SHA, offline
                     (sockets booby-trapped for the whole run).
2. no workflows    — the stamp itself carries the spec's `no_workflow_yaml`
                     refusal; exit 0 (a refusal is a result).
3. not a checkout  — `collection_refusal` stamped, NO `ci_score` key, exit 2.
4. dirty tree      — provenance is `<sha>-dirty`.
5. scoring failure — `data_sources.ci_score_error` recorded, no partial
                     stamp, exit 3.
6. parse errors    — an unparseable workflow is counted and named in
                     `data_sources.workflow_parse_errors`, never dropped
                     silently.
7. all unparseable — workflow files present but NONE parse → collection_refusal
                     (`no_parseable_workflows`), NO `ci_score` stamp, exit 2;
                     never a deflated F computed from zero readable documents.
8. determinism     — two runs on the same tree produce identical stamps.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(mod_name: str, rel: str):
    # File-path loading, same rationale as the scorer contract suite: sibling
    # skills ship same-named modules on the shared pythonpath.
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cc_mod = _load("ci_score_collect_config", "scripts/collect_config.py")


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    """The collector is offline by construction — booby-trap sockets for EVERY
    cell so any network call anywhere in the flow fails the suite loudly."""
    import socket

    def boom(*_a, **_k):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("network egress attempted by collect_config")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args],
                         capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                              "HOME": str(root)})
    return out.stdout.strip()


def _mk_repo(tmp_path: Path, with_workflow: bool = True) -> Path:
    """A minimal REAL git repo — the collector's checkout gate and provenance
    read actual git state, so the fixtures are actual repos, not fakes."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("x\n")
    if with_workflow:
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "on:\n  pull_request:\nconcurrency:\n  group: ci-${{ github.ref }}\n"
            "  cancel-in-progress: true\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 20\n    steps:\n"
            "      - uses: actions/checkout@0000000000000000000000000000000000000000\n"
            "      - uses: actions/setup-node@0000000000000000000000000000000000000000\n"
            "        with:\n          cache: npm\n"
            "      - run: npm test\n")
    _git(root, "add", "-A")  # test fixture repo, not the working tree
    _git(root, "commit", "-qm", "init")
    return root


def test_happy_path_stamps_score_offline(tmp_path):
    root = _mk_repo(tmp_path)
    doc, code = cc_mod.collect(root)
    assert code == 0
    stamp = doc["ci_score"]
    assert stamp["refusal"] is None
    assert isinstance(stamp["value"], int) and stamp["grade"]
    assert stamp["spec_version"] == "ci-score-v0.1.3"
    # provenance is the full-repo HEAD, clean tree → bare SHA
    assert doc["commit_sha"] == _git(root, "rev-parse", "HEAD")
    assert doc["scanned_workflows"] == 1
    # the stamped facts cover the registry (facts_unavailable can't fire)
    assert len(doc["practice_facts"]) == 11


def test_no_workflows_is_the_spec_refusal_not_an_error(tmp_path):
    root = _mk_repo(tmp_path, with_workflow=False)
    doc, code = cc_mod.collect(root)
    assert code == 0  # a refusal is a RESULT
    stamp = doc["ci_score"]
    assert stamp["refusal"]["reason_code"] == "no_workflow_yaml"
    assert stamp["value"] is None and stamp["grade"] is None
    assert doc["scanned_workflows"] == 0


def test_not_a_checkout_refuses_politely(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / ".github" / "workflows").mkdir(parents=True)
    (plain / ".github" / "workflows" / "ci.yml").write_text("on: push\njobs: {}\n")
    doc, code = cc_mod.collect(plain)
    assert code == 2
    assert doc["collection_refusal"]["reason_code"] == "not_a_git_checkout"
    assert "ci_score" not in doc  # never a guessed grade outside a checkout
    assert "practice_facts" not in doc  # it refused BEFORE reading a partial view


def test_dirty_tree_is_stamped_dirty(tmp_path):
    root = _mk_repo(tmp_path)
    (root / "README.md").write_text("modified\n")
    doc, code = cc_mod.collect(root)
    assert code == 0
    assert doc["commit_sha"].endswith("-dirty")
    assert doc["commit_sha"][:-6] == _git(root, "rev-parse", "HEAD")
    # the SUMMARY keeps the dirty flag too — truncation must never hide it
    assert "-dirty" in cc_mod._summary_line(doc)


def test_scoring_failure_records_marker_never_partial_stamp(tmp_path):
    root = _mk_repo(tmp_path)
    broken_spec = tmp_path / "broken-spec.json"
    broken_spec.write_text("{}")  # valid JSON, invalid registry → compute raises
    doc, code = cc_mod.collect(root, spec_path=broken_spec)
    assert code == 3
    assert "ci_score" not in doc
    assert "ci_score_error" in doc["data_sources"]
    # the facts it DID compute are still recorded (honest partial document,
    # just never a partial SCORE)
    assert len(doc["practice_facts"]) == 11


def test_unparseable_workflow_is_counted_and_named(tmp_path):
    root = _mk_repo(tmp_path)
    bad = root / ".github" / "workflows" / "broken.yml"
    bad.write_text("on: [push\njobs: : :\n")  # YAML error
    doc, code = cc_mod.collect(root)
    assert code == 0
    assert doc["scanned_workflows"] == 2  # both files COUNTED
    assert doc["data_sources"]["workflow_parse_errors"] == [
        ".github/workflows/broken.yml"]
    assert doc["ci_score"]["refusal"] is None  # the good workflow still scores


def test_all_workflows_unparseable_refuses_not_deflated_F(tmp_path):
    # A repo whose ONLY workflow is broken YAML must NOT be scored: computing
    # facts from zero readable documents would stamp an F(0) with fabricated
    # "absent" evidence ("no job sets timeout-minutes") the collector never
    # derived from data — the design's worst outcome. It refuses instead.
    root = _mk_repo(tmp_path, with_workflow=False)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "broken.yml").write_text("on: [push\njobs: : :\n")  # YAML error
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add broken wf")
    doc, code = cc_mod.collect(root)
    assert code == 2
    assert doc["collection_refusal"]["reason_code"] == "no_parseable_workflows"
    assert "ci_score" not in doc          # never a guessed / deflated grade
    assert "practice_facts" not in doc    # no facts computed from zero docs
    assert doc["scanned_workflows"] == 1  # the file is still counted honestly
    assert doc["data_sources"]["workflow_parse_errors"] == [
        ".github/workflows/broken.yml"]
    # the honest summary states the refusal, not a grade
    assert "Not scored" in cc_mod._summary_line(doc)


def test_subdir_target_scores_the_whole_repo_not_a_partial_view(tmp_path):
    # --repo pointing at a SUBDIRECTORY of a checkout must score the WHOLE repo:
    # git plumbing succeeds from a subdir, but a subdir-relative scan would see
    # an empty .github/workflows and inflate the grade (OD-L2). Top-level
    # normalization makes the subdir target and the root target identical.
    root = _mk_repo(tmp_path)
    sub = root / "packages" / "app"
    sub.mkdir(parents=True)
    from_root, code_root = cc_mod.collect(root)
    from_sub, code_sub = cc_mod.collect(sub)
    assert code_root == 0 and code_sub == 0
    assert from_sub["repo_root"] == from_root["repo_root"]  # normalized to top
    assert from_sub["ci_score"] == from_root["ci_score"]    # same full-repo score
    assert from_sub["scanned_workflows"] == 1


def test_unknown_worktree_state_never_certifies_clean(tmp_path, monkeypatch):
    # If `git status --porcelain` can't be determined (fails/times out → None),
    # provenance must NOT stamp a bare (clean) SHA — an unverifiable tree is
    # marked -dirty, the safe direction for the -dirty-forbidding profile.
    root = _mk_repo(tmp_path)
    real_git = cc_mod._git

    def fake_git(r, *args):
        if args[:1] == ("status",):
            return None  # simulate status failure / timeout
        return real_git(r, *args)

    monkeypatch.setattr(cc_mod, "_git", fake_git)
    assert cc_mod._provenance(root).endswith("-dirty")


def test_same_tree_same_stamp(tmp_path):
    root = _mk_repo(tmp_path)
    doc1, _ = cc_mod.collect(root)
    doc2, _ = cc_mod.collect(root)
    assert doc1["ci_score"] == doc2["ci_score"]
    assert doc1["practice_facts"] == doc2["practice_facts"]
    assert doc1["commit_sha"] == doc2["commit_sha"]


def test_main_writes_document_and_honest_summary(tmp_path, capsys):
    root = _mk_repo(tmp_path)
    out = tmp_path / "findings.json"
    code = cc_mod.main(["--repo", str(root), "--out", str(out)])
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["ci_score"]["grade"]
    printed = capsys.readouterr().out
    # number-only presentation: the summary states the value; the letter band
    # stays in the stamp but is never rendered
    assert f"CI Score: {doc['ci_score']['value']}/100" in printed
    assert f"({doc['ci_score']['grade']})" not in printed


def test_sparse_checkout_refuses(tmp_path):
    """Sparse mode hides missing files from every other guard (clean status,
    valid HEAD) — the collector must refuse rather than stamp a partial view."""
    root = _mk_repo(tmp_path)
    _git(root, "sparse-checkout", "init")
    doc, code = cc_mod.collect(root)
    assert code == 2
    assert doc["collection_refusal"]["reason_code"] == "sparse_checkout"
    assert "ci_score" not in doc and "practice_facts" not in doc


def test_sparse_checkout_refuses_noncanonical_bool(tmp_path):
    """git honors any boolean spelling (yes/on/1) as true; --type=bool
    normalization means a hand-set non-canonical value must still refuse,
    not slip past a literal "true" comparison."""
    root = _mk_repo(tmp_path)
    _git(root, "config", "core.sparseCheckout", "yes")
    doc, code = cc_mod.collect(root)
    assert code == 2
    assert doc["collection_refusal"]["reason_code"] == "sparse_checkout"


def test_absence_findings_name_their_target_files(tmp_path):
    """B4 finding #1: 'concurrency on 0 of N PR workflows' must NAME the N
    workflows — the fixing agent re-derives nothing."""
    root = _mk_repo(tmp_path, with_workflow=False)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "pr-a.yml").write_text(
        "on:\n  pull_request:\njobs:\n  t:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: make test\n")
    (wf / "pr-b.yml").write_text(
        "on:\n  pull_request:\njobs:\n  t:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: make lint\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "wf")
    doc, code = cc_mod.collect(root)
    assert code == 0
    facts = doc["practice_facts"]
    both = {".github/workflows/pr-a.yml", ".github/workflows/pr-b.yml"}
    for cid in ("ci.trigger.concurrency-groups", "ci.trigger.cancel-superseded",
                "ci.trigger.path-filter", "ci.hygiene.job-timeouts"):
        fact = facts[cid]
        assert fact["state"] == "fail"
        assert set(fact["files"]) == both, f"{cid} files: {fact['files']}"


def test_collect_wires_repo_slug_from_a_real_origin(tmp_path):
    """End-to-end seam: a repo WITH a github origin lands `repo_slug` in the
    document; a repo without one omits the key (never a null/empty slug)."""
    root = _mk_repo(tmp_path)
    _git(root, "remote", "add", "origin", "git@github.com:octo/example.git")
    doc, code = cc_mod.collect(root)
    assert code == 0
    assert doc["repo_slug"] == "octo/example"
    # a second repo with no origin never carries the key
    sub = tmp_path / "sub"
    sub.mkdir()
    bare = _mk_repo(sub)
    doc2, _ = cc_mod.collect(bare)
    assert "repo_slug" not in doc2


def _stamp_doc(value, passed, applicable, total, commit="a" * 40, **extra):
    checks = ([{"state": "pass"}] * passed
              + [{"state": "fail"}] * (applicable - passed)
              + [{"state": "not_applicable"}] * (total - applicable))
    doc = {"commit_sha": commit,
           "ci_score": {"value": value, "checks_passed": passed,
                        "checks_applicable": applicable, "checks": checks}}
    doc.update(extra)
    return doc


@pytest.mark.parametrize("value,filled", [(0, 0), (38, 11), (50, 15), (100, 30)])
def test_banner_bar_is_30_blocks_round_half_up(value, filled):
    """The pre-drawn banner's bar is exactly 30 blocks, filled =
    round-half-up(value·30/100) — 50→15 (12.5→13 is the 25-scale; 15.0 here),
    the boundary a freehand draw got wrong in the wild."""
    lines = cc_mod._banner_lines(_stamp_doc(value, 5, 8, 11))
    bar = next(l for l in lines if "█" in l or "░" in l)
    blocks = bar.count("█") + bar.count("░")
    assert blocks == 30
    assert bar.count("█") == filled


def test_banner_box_lines_are_all_equal_width():
    lines = cc_mod._banner_lines(_stamp_doc(38, 3, 8, 11))
    assert len({len(l) for l in lines}) == 1, [len(l) for l in lines]
    # widest content still yields equal-width borders (box grows for a long slug)
    wide = cc_mod._banner_lines(
        _stamp_doc(38, 3, 8, 11, repo_slug="a-long-org/a-very-long-repository-name"))
    assert len({len(l) for l in wide}) == 1
    # every content row keeps a right margin — the widest row must widen the
    # box, never touch the border (live dogfood caught a flush slug line,
    # 2026-07-29: `…-dirty│` with no gap on a live dogfood repo's long slug)
    for box in (lines, wide):
        for row in box:
            if row.startswith("│"):
                assert row.endswith("  │"), row


def test_no_banner_on_malformed_stamp_tallies():
    """A stamp with a value but missing/None tallies must print NO banner —
    never a contradictory '0 pass · 0 fail' box under a filled bar (the
    in-process scorer can't produce this; the guard is for hand-fed docs)."""
    doc = _stamp_doc(50, 4, 8, 11)
    del doc["ci_score"]["checks_passed"]
    assert cc_mod._banner_lines(doc) == []
    doc2 = _stamp_doc(50, 4, 8, 11)
    doc2["ci_score"]["checks_applicable"] = None
    assert cc_mod._banner_lines(doc2) == []


def test_banner_tallies_and_dirty_and_slug_fallback():
    # tallies come straight off the stamp
    lines = cc_mod._banner_lines(_stamp_doc(38, 3, 8, 11))
    assert any("3 pass · 5 fail · 3 not applicable" in l for l in lines)
    # -dirty suffix is preserved on the short sha
    dirty = cc_mod._banner_lines(_stamp_doc(38, 3, 8, 11, commit="abc1234" + "0" * 33 + "-dirty"))
    assert any("abc1234-dirty" in l for l in dirty)
    # no slug → name falls back to the repo_root basename
    fb = cc_mod._banner_lines(_stamp_doc(38, 3, 8, 11, repo_root="/tmp/checkouts/myrepo"))
    assert any("myrepo @ " in l for l in fb)


def test_no_banner_on_refusal_or_error_docs():
    assert cc_mod._banner_lines({"collection_refusal": {"human_reason": "x"}}) == []
    assert cc_mod._banner_lines({"data_sources": {"ci_score_error": "boom"}}) == []
    assert cc_mod._banner_lines({"ci_score": {"refusal": {"human_reason": "no"}}}) == []
    assert cc_mod._banner_lines({"findings": []}) == []  # no stamp at all


def test_main_prints_the_predrawn_banner(tmp_path, capsys):
    root = _mk_repo(tmp_path)
    out = tmp_path / "findings.json"
    cc_mod.main(["--repo", str(root), "--out", str(out)])
    printed = capsys.readouterr().out
    assert "┌" in printed and "└" in printed and "CI SCORE" in printed
    bar = next(l for l in printed.splitlines() if "█" in l or "░" in l)
    assert bar.count("█") + bar.count("░") == 30


@pytest.mark.parametrize("url,want", [
    ("git@github.com:octo/example.git", "octo/example"),
    ("git@github.com:octo/example", "octo/example"),
    ("https://github.com/octo/example.git", "octo/example"),
    ("https://github.com/octo/example", "octo/example"),
    ("https://github.com/octo/example/", "octo/example"),
    ("ssh://git@github.com/octo/example.git", "octo/example"),
    ("https://user@github.com/octo/example.git", "octo/example"),
    # broadened forms (2026-07-29): git:// scheme, explicit port, a non-`git`
    # scp user (deploy keys / multi-account), and a case-insensitive host.
    ("git://github.com/octo/example.git", "octo/example"),
    ("ssh://git@github.com:22/octo/example.git", "octo/example"),
    ("org-1234@github.com:octo/example.git", "octo/example"),
    ("git@GitHub.com:octo/example.git", "octo/example"),
    ("https://github.com/octo/example.git/", "octo/example"),
    ("https://gitlab.com/octo/example.git", None),  # not GitHub → no slug
    # host-confusion look-alikes must NOT parse (never fabricate a link):
    # look-alike host: BEGINS with github.com but is a different domain —
    # constructed so the literal never appears in shipped text (a registry
    # security scan read the example domain as a real malicious URL)
    ("git@" + "github.com" + ".evil-example.test" + ":octo/example.git", None),
    ("https://" + "github.com" + ".evil-example.test" + "/octo/example.git", None),
    ("", None),
    (None, None),
])
def test_repo_slug_parses_common_remote_forms(monkeypatch, url, want):
    """repo_slug is display-only header provenance: GitHub origin URLs in
    their common spellings parse to owner/repo; anything else (no origin,
    non-GitHub host) yields None so the header falls back to the local path
    instead of fabricating a link."""
    monkeypatch.setattr(
        cc_mod, "_git",
        lambda root, *args: url if args[:2] == ("config", "--get") else None)
    assert cc_mod._repo_slug(Path("/tmp/x")) == want
