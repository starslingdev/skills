"""Changelog fragments: the format, the assembler, and the hand-edit gate.

Every PR used to prepend a bullet at the same lines of a shared
`skills/<skill>/CHANGELOG.md`, so open PRs conflicted there. A change now adds
one file under `skills/<skill>/changelog.d/`, and
`maintainers/scripts/assemble_changelog.py` files those into CHANGELOG.md at
release. The logic lives in pure functions in that script; these are their
fixture tests, plus the live checks that the real tree's fragments are valid
and that CI runs the gate.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "maintainers" / "scripts" / "assemble_changelog.py"
_spec = importlib.util.spec_from_file_location("assemble_changelog", _SCRIPT)
ac = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ac
_spec.loader.exec_module(ac)

TODAY = dt.date(2026, 11, 20)

HEADINGS_LOG = """# Changelog

Intro.

## [Unreleased]

### Fixed

- **2026-11-01** (#10) — old fix.

### Added

- **2026-10-01** (#9) — old add.
"""

FLAT_LOG = """# Changelog — x

## [Unreleased]

- **2026-11-01** (#10) — **Changed**: old change.

### Fixed

- **2026-01-01** — older.
"""


def frag(name, text, mode=ac.MODE_HEADINGS):
    f, errs = ac.parse_fragment(name, text, mode=mode, today=TODAY)
    assert not errs, errs
    return f


# --- fragment validation --------------------------------------------------------------------

def test_a_well_formed_fragment_parses():
    f = frag("merge-queue.fixed.md",
             "- **2026-11-19** — Fix the thing (#431).\n  Second line.\n")
    assert f.kind == "fixed" and f.date == "2026-11-19"
    assert f.bullets == ("- **2026-11-19** — Fix the thing (#431).\n  Second line.",)


@pytest.mark.parametrize("name,text,needle", [
    ("x.md", "- **2026-11-19** (#1) a", "fragment names are"),
    ("x.bugfix.md", "- **2026-11-19** (#1) a", "unknown kind"),
    ("X.fixed.md", "- **2026-11-19** (#1) a", "fragment names are"),
    ("x.fixed.txt", "- **2026-11-19** (#1) a", "fragment names are"),
    ("x.fixed.md", "", "empty fragment"),
    ("x.fixed.md", "- 2026-11-19 (#1) a", "must open with its UTC date"),
    ("x.fixed.md", "- **2026-02-30** (#1) a", "not a calendar date"),
    ("x.fixed.md", "- **2026-12-01** (#1) a", "in the future"),
    ("x.fixed.md", "- **2026-11-19** a fix with no PR", "must cite its pull request"),
    ("x.fixed.md", "- **2026-11-19** a (#1)\n- **2026-11-18** b", "must cite its pull request"),
    ("x.fixed.md", "### Fixed\n\n- **2026-11-19** (#1) a", "no headings"),
    ("x.fixed.md", "Some prose.\n- **2026-11-19** (#1) a", "starts with a"),
    ("x.fixed.md", "- **2026-11-19** (#1) a\nunindented", "indent continuation"),
])
def test_malformed_fragments_are_refused(name, text, needle):
    f, errs = ac.parse_fragment(name, text, today=TODAY)
    assert f is None
    assert any(needle in e for e in errs), errs


def test_a_flat_changelog_needs_the_kind_inline():
    _, errs = ac.parse_fragment("x.changed.md", "- **2026-11-19** (#1) — no kind",
                                mode=ac.MODE_FLAT, today=TODAY)
    assert any("**Changed**" in e for e in errs), errs
    frag("x.changed.md", "- **2026-11-19** (#1) — **Changed**: ok", mode=ac.MODE_FLAT)


def test_a_directory_needs_its_readme_and_reports_every_bad_file():
    _, errs = ac.validate_dir({"a.fixed.md": "nope", "b.oops.md": "x"},
                              mode=ac.MODE_HEADINGS, today=TODAY)
    assert any("README.md is missing" in e for e in errs)
    assert any(e.startswith("a.fixed.md") for e in errs)
    assert any(e.startswith("b.oops.md") for e in errs)


# --- assembly -------------------------------------------------------------------------------

def test_mode_detection():
    assert ac.changelog_mode(HEADINGS_LOG) == ac.MODE_HEADINGS
    assert ac.changelog_mode(FLAT_LOG) == ac.MODE_FLAT
    with pytest.raises(ac.AssembleError):
        ac.changelog_mode("# Changelog\n\n- a\n")


def test_assembly_files_each_kind_newest_first_and_merges_the_opening_heading():
    out = ac.assemble(HEADINGS_LOG, [
        frag("b.fixed.md", "- **2026-11-18** — fix b (#2)."),
        frag("a.fixed.md", "- **2026-11-19** — fix a (#3)."),
        frag("c.added.md", "- **2026-11-17** — add c (#4).\n  more."),
        frag("d.removed.md", "- **2026-11-16** — remove d (#5)."),
    ])
    assert out == """# Changelog

Intro.

## [Unreleased]

### Added

- **2026-11-17** — add c (#4).
  more.

### Removed

- **2026-11-16** — remove d (#5).

### Fixed

- **2026-11-19** — fix a (#3).

- **2026-11-18** — fix b (#2).

- **2026-11-01** (#10) — old fix.

### Added

- **2026-10-01** (#9) — old add.
"""


def test_assembly_order_includes_removed_before_the_existing_section():
    out = ac.assemble(HEADINGS_LOG, [frag("d.removed.md", "- **2026-11-16** — d (#5).")])
    assert "## [Unreleased]\n\n### Removed\n\n- **2026-11-16** — d (#5).\n\n### Fixed\n" in out


def test_a_heading_that_is_not_first_is_not_merged_into():
    out = ac.assemble(HEADINGS_LOG, [frag("c.added.md", "- **2026-11-17** — c (#4).")])
    assert out.index("- **2026-11-17**") < out.index("### Fixed")
    assert out.count("### Added") == 2


def test_flat_changelogs_take_bare_bullets_at_the_top():
    out = ac.assemble(FLAT_LOG, [
        frag("a.fixed.md", "- **2026-11-18** (#2) — **Fixed**: a.", ac.MODE_FLAT),
        frag("b.changed.md", "- **2026-11-19** (#3) — **Changed**: b.", ac.MODE_FLAT),
    ])
    assert out.startswith("# Changelog — x\n\n## [Unreleased]\n\n"
                          "- **2026-11-19** (#3) — **Changed**: b.\n\n"
                          "- **2026-11-18** (#2) — **Fixed**: a.\n\n"
                          "- **2026-11-01** (#10)")


def test_assembling_nothing_changes_nothing():
    assert ac.assemble(HEADINGS_LOG, []) == HEADINGS_LOG


def test_an_empty_unreleased_section_gets_a_heading():
    log = "# C\n\n## [Unreleased]\n\n## [0.1]\n\n- old\n"
    out = ac.assemble(log, [frag("a.fixed.md", "- **2026-11-19** (#1) a")])
    assert out == "# C\n\n## [Unreleased]\n\n### Fixed\n\n- **2026-11-19** (#1) a\n\n## [0.1]\n\n- old\n"


# --- the hand-edit gate ---------------------------------------------------------------------

README = {"README.md": "format notes"}


def test_an_untouched_changelog_passes():
    assert ac.check_transition("s", HEADINGS_LOG, HEADINGS_LOG, README, ["README.md"]) == []


def test_adding_a_fragment_passes():
    assert ac.check_transition("s", HEADINGS_LOG, HEADINGS_LOG, README,
                               ["README.md", "x.fixed.md"]) == []


def test_a_hand_edit_is_refused():
    edited = HEADINGS_LOG.replace("### Fixed\n", "### Fixed\n\n- **2026-11-19** — mine (#7).\n")
    errs = ac.check_transition("s", HEADINGS_LOG, edited, README, ["README.md"])
    assert errs and "edited by hand" in errs[0] and "changelog.d" in errs[0]


def test_the_assemblers_own_output_passes():
    base = {**README, "a.fixed.md": "- **2026-11-19** — fix a (#3)."}
    head = ac.assemble(HEADINGS_LOG, [frag("a.fixed.md", base["a.fixed.md"])])
    assert ac.check_transition("s", HEADINGS_LOG, head, base, ["README.md"]) == []


def test_a_release_with_an_extra_hand_edit_is_refused():
    base = {**README, "a.fixed.md": "- **2026-11-19** — fix a (#3)."}
    head = ac.assemble(HEADINGS_LOG, [frag("a.fixed.md", base["a.fixed.md"])])
    head = head.replace("old add", "old add, quietly reworded")
    errs = ac.check_transition("s", HEADINGS_LOG, head, base, ["README.md"])
    assert errs and "not the assembler's output" in errs[0]


def test_deleting_fragments_without_filing_them_is_refused():
    base = {**README, "a.fixed.md": "- **2026-11-19** — fix a (#3)."}
    errs = ac.check_transition("s", HEADINGS_LOG, HEADINGS_LOG, base, ["README.md"])
    assert errs and "would be lost" in errs[0]


def test_a_new_skill_creating_its_changelog_passes():
    assert ac.check_transition("s", None, HEADINGS_LOG, {}, ["README.md"]) == []


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd),
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def _tree(root: Path):
    for skill in ac.SKILLS:
        d = root / "skills" / skill
        (d / "changelog.d").mkdir(parents=True)
        (d / "CHANGELOG.md").write_text(HEADINGS_LOG, encoding="utf-8")
        (d / "changelog.d" / "README.md").write_text("notes\n", encoding="utf-8")


def _cli(root, *args):
    return subprocess.run([sys.executable, str(_SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True)


def test_the_cli_end_to_end(tmp_path):
    """Assemble writes and deletes; --check --diff-base accepts that and refuses a hand edit."""
    _tree(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "skills")
    _git(tmp_path, "commit", "-qm", "base")
    fragment = tmp_path / "skills" / "sling" / "changelog.d" / "x.fixed.md"
    fragment.write_text("- **2026-11-19** — fix x (#3).\n", encoding="utf-8")
    assert _cli(tmp_path, "--check", "--diff-base", "HEAD").returncode == 0
    _git(tmp_path, "add", "skills")
    _git(tmp_path, "commit", "-qm", "fragment")

    proc = _cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not fragment.exists()
    assert "- **2026-11-19** — fix x (#3)." in (tmp_path / "skills/sling/CHANGELOG.md").read_text()
    assert _cli(tmp_path, "--check", "--diff-base", "HEAD").returncode == 0

    secure = tmp_path / "skills" / "ci-secure" / "CHANGELOG.md"
    secure.write_text(HEADINGS_LOG + "\n- hand edit\n", encoding="utf-8")
    proc = _cli(tmp_path, "--check", "--diff-base", "HEAD")
    assert proc.returncode == 1 and "skills/ci-secure/CHANGELOG.md was edited by hand" in proc.stderr

    proc = _cli(tmp_path, "--check", "--diff-base", "no-such-rev")
    assert proc.returncode == 2, "an unreadable base is an operational failure, never a pass"


def test_assemble_refuses_to_write_anything_when_a_fragment_is_bad(tmp_path):
    _tree(tmp_path)
    good = tmp_path / "skills" / "sling" / "changelog.d" / "good.fixed.md"
    good.write_text("- **2026-11-19** — ok (#3).\n", encoding="utf-8")
    (tmp_path / "skills" / "ci-score" / "changelog.d" / "bad.fixed.md").write_text("nope\n")
    proc = _cli(tmp_path)
    assert proc.returncode == 1
    assert good.exists()
    assert (tmp_path / "skills/sling/CHANGELOG.md").read_text() == HEADINGS_LOG


# --- the real tree --------------------------------------------------------------------------

@pytest.mark.parametrize("skill", ac.SKILLS)
def test_every_shipped_skill_has_a_valid_fragment_directory(skill):
    changelog = (_REPO / "skills" / skill / "CHANGELOG.md").read_text(encoding="utf-8")
    mode = ac.changelog_mode(changelog)
    files = ac._read_dir(_REPO, skill)
    today = dt.datetime.now(dt.timezone.utc).date()
    _, errs = ac.validate_dir(files, mode=mode, today=today)
    assert not errs, errs


def test_the_skill_list_is_every_shipped_skill():
    shipped = sorted(p.parent.name for p in (_REPO / "skills").glob("*/SKILL.md"))
    assert sorted(ac.SKILLS) == shipped


def test_the_assembler_is_maintainer_tooling_outside_the_install_tree():
    """`skills/<name>/` installs recursively, so the script must never move under it,
    and a fragment directory ships markdown only."""
    assert _SCRIPT.is_file() and "skills" not in _SCRIPT.relative_to(_REPO).parts[:1]
    assert not list((_REPO / "skills").rglob("assemble_changelog*"))
    for skill in ac.SKILLS:
        d = _REPO / "skills" / skill / "changelog.d"
        assert all(p.suffix == ".md" and p.is_file() for p in d.iterdir()), sorted(d.iterdir())


_GATE = "python3 maintainers/scripts/assemble_changelog.py --check --diff-base HEAD^1"


@pytest.mark.parametrize("job_id", ["test-self", "test-fork"])
def test_ci_runs_the_gate_inside_both_suite_jobs(job_id):
    """The gate runs inside the jobs whose result the required `test` verdict reads,
    on every event, with nothing that could skip it or swallow its exit code.
    `HEAD^1` is the base on every event: a pull request's test merge, a
    merge-queue commit, and a push to main all have the tree they land on as
    their first parent, and the checkout carries full history."""
    yaml = pytest.importorskip("yaml")
    job = yaml.safe_load((_REPO / ".github/workflows/ci.yml").read_text())["jobs"][job_id]
    steps = [s for s in job["steps"] if s.get("run", "").strip() == _GATE]
    assert len(steps) == 1, f"{job_id} must run `{_GATE}` exactly once"
    assert "if" not in steps[0] and "continue-on-error" not in steps[0]
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "HEAD^1 must exist in the checkout"
