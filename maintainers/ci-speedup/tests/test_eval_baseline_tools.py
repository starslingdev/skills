"""The discrimination-proof wrapper cuts exactly one catalog entry from the
session's skill copy, and never the repository's own catalog."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "eval_withhold_entry", _REPO / "maintainers" / "ci-speedup" / "scripts" / "eval_withhold_entry.py")
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)
_CATALOG = _REPO / "skills" / "ci-speedup" / "references" / "optimization-patterns.md"


def test_withholding_opt81_removes_its_section_and_only_it():
    text = _CATALOG.read_text(encoding="utf-8")
    cut = wrapper.withhold(text, "OPT81")
    assert "### OPT81 —" not in cut and "unavailable to fork pull requests" not in cut
    assert "### OPT51 —" in cut and "## Category 11: Stack-Specific" in cut
    removed = len(text) - len(cut)
    start = text.index("### OPT81 —")
    assert text[start:start + removed].rstrip().endswith("---")


def test_withholding_from_a_split_catalog_deletes_the_file_and_its_index_line(tmp_path):
    pats = tmp_path / "patterns"
    pats.mkdir()
    for n in ("opt8-a.md", "opt81-b.md", "opt86-c.md"):
        (pats / n).write_text("x\n")
    (pats / "INDEX.md").write_text("# i\n- OPT8 — A (LOW, static) — x\n- OPT81 — B (LOW, static) — x\n")
    wrapper.withhold_split(pats, "OPT81")
    assert sorted(p.name for p in pats.iterdir()) == ["INDEX.md", "opt8-a.md", "opt86-c.md"]
    assert (pats / "INDEX.md").read_text() == "# i\n- OPT8 — A (LOW, static) — x\n"
    with pytest.raises(SystemExit):
        wrapper.withhold_split(pats, "OPT81")


def test_an_unknown_id_refuses_rather_than_running_an_unchanged_session():
    with pytest.raises(SystemExit):
        wrapper.withhold(_CATALOG.read_text(encoding="utf-8"), "OPT999")


_spec_s = importlib.util.spec_from_file_location(
    "eval_baseline_summary", _REPO / "maintainers" / "ci-speedup" / "scripts" / "eval_baseline_summary.py")
summary = importlib.util.module_from_spec(_spec_s)
_spec_s.loader.exec_module(summary)
_BASELINES = sorted(p.parent for p in (_REPO / "maintainers" / "ci-speedup" / "evals" / "baseline").rglob(
    "summary.md"))


def test_the_summary_reports_medians_ranges_and_not_run_sessions():
    def run(b, ok, wall, checks):
        files = [{"path": "optimization-patterns.md", "bytes": b * 3 // 4},
                 {"path": "gap-fill.md", "bytes": b // 4}] if b else []
        return {"id": 11, "name": "q", "passed": ok, "checks": [{"name": n, "passed": v} for n, v in checks],
                "reading": {"reference_bytes": b, "reference_files": files, "catalog_opened": b > 0,
                            "session": {"input_tokens": 1000 + b, "wall_s": wall}}}
    older = {"id": 11, "name": "q", "run": 4, "passed": True, "checks": [{"name": "c", "passed": True}]}
    out = summary.summarize([run(0, False, 60.0, [("c", False)]),
                             run(200, True, None, [("c", True), ("d", True)]),
                             run(400, True, 80.0, [("c", True)]), older,
                             {"id": 11, "run": 5, "harness_error": "x"}])
    assert ("| 11 | `q` | 5 | 3 | 1 | 200 (0 to 400) | 150 (0 to 300) | 2 (0 to 2) | 2/3 "
            "| 1,200 (1,000 to 1,400) | 70 (60 to 80) |") in out
    assert "| 11 | `c` | 3/4 |" in out and "| 11 | `d` | 1/1 |" in out


def test_split_catalog_reads_get_their_own_table_and_count_as_catalog():
    def run(files):
        return {"id": 11, "name": "q", "passed": True, "checks": [],
                "reading": {"reference_bytes": sum(b for _, b in files), "catalog_opened": True,
                            "reference_files": [{"path": p, "bytes": b} for p, b in files],
                            "session": {"input_tokens": 1, "wall_s": 1.0}}}
    out = summary.summarize([run([("patterns/opt81-x.md", 100), ("patterns/INDEX.md", 20)]),
                             run([("patterns/opt81-x.md", 300), ("optimization-patterns.md", 50)])])
    assert "| 235 (120 to 350) | 235 (120 to 350) |" in out  # split files count as catalog
    assert "| 11 | 1/2 | `patterns/INDEX.md` 1/2 runs, 20 (20 to 20) bytes; `patterns/opt81-x.md` 2/2 runs, " \
        "200 (100 to 300) bytes |" in out
    assert "Split catalog" not in summary.summarize([run([("optimization-patterns.md", 5)])])


@pytest.mark.parametrize("d", _BASELINES, ids=lambda p: p.name if p.name not in ("before", "after")
                         else f"{p.parent.name}/{p.name}")
def test_every_committed_summary_matches_its_results(d):
    """summary.md quotes the tool's output; a hand edit or a stale table fails here."""
    import json
    rows = [r for p in sorted(d.glob("*.json")) for r in json.loads(p.read_text(encoding="utf-8"))]
    text = (d / "summary.md").read_text(encoding="utf-8")
    missing = [line for line in summary.summarize(rows).splitlines() if line and line not in text]
    assert not missing, missing


_FAKE_RUNNER = '''
import json, os, shutil as _sh, subprocess, types
shutil = types.SimpleNamespace(copytree=_sh.copytree, which=_sh.which)

def _env(extra=None):
    env = {"PATH": os.environ["FAKE_PATH"]}
    env.update(extra or {})
    return env

def main(argv):
    shutil.copytree(os.environ["FAKE_SRC"], os.environ["FAKE_DST"])
    env = _env({})
    out = {"no_proxy": env.get("NO_PROXY", ""), "https_proxy": env.get("HTTPS_PROXY", "")}
    for key, args in (("blocked", ["api", "repos/StarSlingDev/skills/contents/x"]), ("other", ["auth", "status"])):
        r = subprocess.run(["gh", *args], env=env, capture_output=True, text=True)
        out[key] = [r.returncode, r.stdout.strip()]
    curl = env.get("HTTPS_PROXY") and not any(
        "api.github.com".endswith(h.lstrip(".")) for h in env.get("NO_PROXY", "").split(",") if h)
    out["api_github_via_dead_proxy"] = bool(curl)
    open(os.environ["FAKE_REPORT"], "w").write(json.dumps(out))
    return 0
'''


def _wrapped(tmp_path, monkeypatch, with_catalog=True):
    skill = tmp_path / "src" / "ci-speedup"
    (skill / "references").mkdir(parents=True)
    if with_catalog:
        (skill / "references" / "optimization-patterns.md").write_text(
            "## Category\n\n### OPT81 — X\n\nbody\n\n### OPT82 — Y\n\nmore\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text('#!/bin/sh\necho "REAL proxy=${HTTPS_PROXY:-none}"\n')
    (bin_dir / "gh").chmod(0o755)
    runner = tmp_path / "fake_runner.py"
    runner.write_text(_FAKE_RUNNER, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(wrapper, "_RUNNER", runner)
    monkeypatch.setenv("FAKE_PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SRC", str(skill))
    monkeypatch.setenv("FAKE_DST", str(tmp_path / "mount" / "ci-speedup"))
    monkeypatch.setenv("FAKE_REPORT", str(report))
    return report


def test_the_published_copy_is_blocked_even_without_a_gh_fixture(tmp_path, monkeypatch):
    """Entry 12 has no fixture, so the runner sets no EVAL_GH_REAL: `gh` must
    still refuse the publishing repository, and plain web clients must not
    reach the GitHub API, while other `gh` calls still work."""
    import json
    report = _wrapped(tmp_path, monkeypatch)
    assert wrapper.main(["OPT81", "--id", "12"]) == 0
    out = json.loads(report.read_text())
    assert out["blocked"][0] != 0 and "REAL" not in out["blocked"][1]
    assert out["other"] == [0, "REAL proxy=none"]
    assert out["api_github_via_dead_proxy"] and "api.github.com" not in out["no_proxy"]
    cut = (tmp_path / "mount" / "ci-speedup" / "references" / "optimization-patterns.md").read_text()
    assert "OPT81" not in cut and "OPT82" in cut


def test_a_mounted_copy_without_the_catalog_stops_the_run(tmp_path, monkeypatch):
    _wrapped(tmp_path, monkeypatch, with_catalog=False)
    with pytest.raises(SystemExit) as exc:
        wrapper.main(["OPT81", "--id", "12"])
    assert exc.value.code not in (0, None)
