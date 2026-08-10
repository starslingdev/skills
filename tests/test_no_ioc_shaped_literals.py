"""No attack-shaped literals in shipped text (2026-07-31 incident).

Hours after the ci-score launch, a registry security scan rated the skill
CRITICAL over a single finding: our own negative-control example — a fake
typosquat host proving the repo-slug parser rejects look-alikes — shipped
as a literal domain in a docstring, a test table, and the changelog. The
scanner read the antibody as the virus.

This guard scans git-tracked text files (by a suffix allowlist covering
every text type the tree actually ships; extensionless files like the git
hook are outside it) for brand-prefix look-alike domains: one of the
trusted hosts THIS REPO references, followed by FURTHER domain labels
(the typosquat shape). The brand/TLD lists are deliberately bounded to
those hosts — a generic scanner is the registry's job; this guard exists
to keep OUR examples from tripping it. Tests that need such a string must
construct it at runtime from concatenated parts; prose must describe the
class without naming a domain.

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

# A fetch command carrying a literal URL, and a URL whose PATH ends in a
# script — the two shapes a registry scanner reads as "this skill downloads
# and runs remote code". Both arms deliberately ignore the host: the
# 2026-08-07 pass moved these examples onto RFC-reserved domains
# (`example.com`, `.invalid`) precisely so scanners would read them as inert,
# and both scanners flagged them anyway three days later, because the rule
# keys on the SHAPE. There is no host that makes a piped installer look safe,
# so the shape itself must stay out of shipped text.
_FETCH_WITH_URL = re.compile(r"\b(?:curl|wget)\b[^\n]*?https?://", re.IGNORECASE)

# The `/` before the extension is load-bearing: it requires the script to sit
# at a URL PATH, so real hosts under the .sh ccTLD that this catalog cites
# (Saint Helena's TLD — zizmor's and Astral's doc sites) are not flagged.
_SCRIPT_URL = re.compile(
    r"https?://[^\s\"'`)>\]]+/[^\s\"'`)>\]]*\.(?:sh|ps1|bash)\b", re.IGNORECASE)

# `.fixture` earns its place here: the cloaked workflow fixtures are exactly
# where this class hid. The path cloak (dot-github/ + .fixture) stops a
# scanner reading them as live workflows; it does nothing about their TEXT,
# and a suffix allowlist without `.fixture` cannot see the file that shipped
# the literal Snyk quoted.
_TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt",
                  ".sh", ".mjs", ".js", ".fixture"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files"],
                         capture_output=True, text=True, check=True)
    return [
        _REPO / line
        for line in out.stdout.splitlines()
        if Path(line).suffix in _TEXT_SUFFIXES and Path(line).name != Path(__file__).name
    ]


def test_detector_fires_on_a_constructed_lookalike():
    """Positive controls: every brand arm, more than one TLD arm, and a
    MIXED-CASE sample (defends re.IGNORECASE) must match, or the scan below
    is vacuous against that regression. Each sample is runtime-constructed."""
    brands = ["github.com", "gitlab.com", "bitbucket.org", "npmjs.com",
              "pypi.org", "anthropic.com", "starsling.dev"]
    for brand in brands:
        sample = "https://" + brand + ".not-really" + ".test" + "/x"
        assert _LOOKALIKE.search(sample), f"detector lost the {brand} arm"
    mixed = "https://" + "GitHub.Com" + ".Not-Really" + ".Test" + "/x"
    assert _LOOKALIKE.search(mixed), "detector lost case-insensitivity"
    io_arm = "https://" + "starsling.io" + ".not-really" + ".test"
    assert _LOOKALIKE.search(io_arm), "detector lost the io TLD arm"
    benign = "https://github.com/starslingdev/skills"
    assert not _LOOKALIKE.search(benign), "detector must not flag the real host"
    cctld = "https://" + "github.com" + ".au" + "/owner/repo"
    assert not _LOOKALIKE.search(cctld), "bare ccTLD-style host must not flag"


def _parts(*parts: str) -> str:
    """Join at runtime — a CALL, never adjacent literals.

    CPython folds `"a" + "b"` into the finished string at compile time, so a
    scanner that evaluates constant expressions reconstructs exactly what the
    split was hiding. That lesson cost a second pass on the credential
    fixtures (public #28); it applies verbatim here.
    """
    return "".join(parts)


def test_installer_detector_fires_on_constructed_samples():
    """Positive controls for both arms, plus the negatives that keep the
    guard from flagging things the catalog legitimately cites."""
    scheme = _parts("htt", "ps://")
    piped = _parts("curl -fsSL ", scheme, "get.example", ".com/install", ".sh | bash")
    assert _FETCH_WITH_URL.search(piped), "fetch-with-URL arm lost the curl form"
    assert _SCRIPT_URL.search(piped), "script-URL arm lost the .sh path form"
    wget = _parts("wget -q ", scheme, "example.invalid/x", ".sh")
    assert _FETCH_WITH_URL.search(wget), "fetch-with-URL arm lost the wget form"
    ps1 = _parts(scheme, "example.invalid/tools/setup", ".ps1")
    assert _SCRIPT_URL.search(ps1), "script-URL arm lost the .ps1 form"

    # The shapes this guard must NOT flag, or it becomes unusable here.
    for benign in (
        'curl -fsSL "$INSTALLER_URL" | bash',   # the de-fanged catalog example
        "curl -fsSL -o install.sh \"$INSTALLER_URL\"",
        _parts(scheme, "docs.zizmor", ".sh/audits/#template-injection"),
        _parts(scheme, "astral", ".sh/blog/open-source-security-at-astral"),
        _parts(scheme, "github.com/starslingdev/skills"),
    ):
        assert not _FETCH_WITH_URL.search(benign), f"false positive: {benign}"
        assert not _SCRIPT_URL.search(benign), f"false positive: {benign}"


def test_no_installer_shapes_in_tracked_files():
    """The 2026-08-10 regression this arm exists to prevent.

    Two registry scanners failed the launched ci-secure over its OWN teaching
    examples — Snyk E005 (CRITICAL) on a piped installer URL in the catalog
    and its fixtures, and the Gen Agent Trust Hub on a matching literal in a
    test assertion. The look-alike-domain arm above could not see any of it:
    it keys on brand typosquats, and none of these hosts is a brand.
    """
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rx in (_FETCH_WITH_URL, _SCRIPT_URL):
            for m in rx.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(
                    f"{path.relative_to(_REPO)}:{line}: {m.group(0)}")
    assert not offenders, (
        "Download-and-execute shaped literal(s) in shipped text. A registry "
        "security scanner reads these as the skill fetching and running remote "
        "code — it failed ci-secure on 2026-08-10 over our own anti-pattern "
        "examples. An RFC-reserved host does NOT clear it; the shape is what "
        "matches. Use a placeholder variable in examples and fixtures (the "
        "curl-pipe-bash detector matches on the pipe, not the address), and "
        "construct any test string at runtime:\n  " + "\n  ".join(offenders)
    )


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
