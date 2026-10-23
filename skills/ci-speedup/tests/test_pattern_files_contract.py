"""The per-pattern catalog files keep their contract: one file per OPT id, the
file name's `opt<N>` matches the frontmatter `pattern` and the title, the
METADATA keys are present, INDEX.md lists every file exactly once (and so
fixes the scanner's catalog order), and retired ids keep their stub file."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))
import scan  # noqa: E402

_PATTERNS = _SKILL / "references" / "patterns"
_FILES = sorted(_PATTERNS.glob("opt*.md"))
_REQUIRED = ("pattern", "impact", "class", "detector", "affected_files", "title_template", "category")


def test_every_file_is_named_for_its_pattern_and_carries_the_required_keys():
    assert len(_FILES) >= 89
    ids = []
    for p in _FILES:
        meta, opt_id, _title, body = scan.parse_pattern_file(p)
        n = re.fullmatch(r"opt(\d+)-[a-z0-9_-]+\.md", p.name)
        assert n and f"OPT{n.group(1)}" == opt_id == meta["pattern"], p.name
        assert all(k in meta for k in _REQUIRED), (p.name, [k for k in _REQUIRED if k not in meta])
        assert meta["impact"] in ("HIGH", "MEDIUM", "LOW") and 1 <= meta["category"] <= 14, p.name
        assert meta.get("status") in (None, "cut", "removed"), p.name
        assert "<!-- METADATA" not in body, f"{p.name}: frontmatter replaces the METADATA comment"
        ids.append(opt_id)
    assert len(ids) == len(set(ids))


def test_index_lists_every_pattern_file_exactly_once_in_scanner_order():
    index = (_PATTERNS / "INDEX.md").read_text(encoding="utf-8")
    ids = re.findall(r"^- (OPT\d+) — .+ \((?:HIGH|MEDIUM|LOW), [a-z-]+\) — \S", index, re.M)
    assert sorted(ids) == sorted(scan.parse_pattern_file(p)[1] for p in _FILES) and len(ids) == len(set(ids))
    assert [scan.parse_pattern_file(p)[1] for p in scan.pattern_files(_PATTERNS)] == ids
    assert [e.pattern for e in scan.load_catalog(_PATTERNS)] == ids


def test_retired_ids_keep_their_stub_files():
    status = {scan.parse_pattern_file(p)[1]: scan.parse_pattern_file(p)[0].get("status") for p in _FILES}
    assert status["OPT49"] == status["OPT50"] == status["OPT51"] == "cut" and status["OPT66"] == "removed"
    index = (_PATTERNS / "INDEX.md").read_text(encoding="utf-8")
    for oid in ("OPT49", "OPT50", "OPT51"):
        assert re.search(rf"^- {oid} — .+ — cut; id retired$", index, re.M), oid


def test_a_stray_opt_named_file_that_is_not_a_pattern_does_not_crash_the_scanner(tmp_path):
    d = tmp_path / "patterns"
    shutil.copytree(_PATTERNS, d)
    (d / "optional.md").write_text("notes, not a pattern\n", encoding="utf-8")
    assert [e.pattern for e in scan.load_catalog(d)] == [e.pattern for e in scan.load_catalog(_PATTERNS)]
