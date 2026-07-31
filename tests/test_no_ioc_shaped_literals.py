"""No attack-shaped literals in shipped text (2026-07-31 incident).

Hours after the ci-score launch, a registry security scan rated the skill
CRITICAL over a single finding: our own negative-control example — a fake
typosquat host proving the repo-slug parser rejects look-alikes — shipped
as a literal domain in a docstring, a test table, and the changelog. The
scanner read the antibody as the virus.

This guard scans every git-tracked text file for brand-prefix look-alike
domains: a well-known host name followed by FURTHER domain labels (the
typosquat shape, e.g. a domain that merely begins with a trusted host's
name). Tests that need such a string must construct it at runtime from
concatenated parts; prose must describe the class without naming a domain.

The detector carries its own positive control (the fail-open lesson from
the launch's install-surface review): a runtime-constructed sample must
match, so a broken regex fails loudly instead of passing vacuously.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# A trusted host's full name (brand.tld) followed by at least one more
# dotted label — the look-alike/typosquat shape. Matching the shape, not
# any specific domain, keeps this file itself free of literals.
_LOOKALIKE = re.compile(
    r"\b(?:github|gitlab|bitbucket|npmjs|pypi|anthropic|starsling)"
    r"\.(?:com|org|dev|io)"
    r"\.[a-z0-9-]{2,}(?:\.[a-z]{2,})+",
    re.IGNORECASE,
)

_TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".sh", ".mjs", ".js"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files"],
                         capture_output=True, text=True, check=True)
    return [
        _REPO / line
        for line in out.stdout.splitlines()
        if Path(line).suffix in _TEXT_SUFFIXES and Path(line).name != Path(__file__).name
    ]


def test_detector_fires_on_a_constructed_lookalike():
    """Positive control: the detector must match a runtime-constructed
    look-alike, or every scan below is vacuous."""
    sample = "https://" + "github.com" + ".not-really" + ".test" + "/x"
    assert _LOOKALIKE.search(sample), "look-alike detector no longer matches its own class"
    benign = "https://github.com/starslingdev/skills"
    assert not _LOOKALIKE.search(benign), "detector must not flag the real host"


def test_no_lookalike_domains_in_tracked_files():
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _LOOKALIKE.finditer(text):
            offenders.append(f"{path.relative_to(_REPO)}: {m.group(0)}")
    assert not offenders, (
        "Attack-shaped look-alike domain literal(s) in shipped text — a registry "
        "security scan WILL read these as malicious URLs (it happened at launch, "
        "2026-07-31). Construct such strings at runtime in tests; describe the "
        "class in prose:\n  " + "\n  ".join(offenders)
    )
