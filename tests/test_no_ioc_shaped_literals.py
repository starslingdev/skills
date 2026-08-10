"""No attack-shaped literals in shipped text (2026-07-31 incident).

Hours after the ci-score launch, a registry security scan rated the skill
CRITICAL over a single finding: our own negative-control example — a fake
typosquat host proving the repo-slug parser rejects look-alikes — shipped
as a literal domain in a docstring, a test table, and the changelog. The
scanner read the antibody as the virus.

This guard scans git-tracked text files (by a suffix allowlist covering
every text type the tree actually ships; extensionless files like the git
hook and LICENSE are outside it) for two families of attack-shaped literal:

1. brand-prefix look-alike domains — one of the trusted hosts THIS REPO
   references, followed by FURTHER domain labels (the typosquat shape);
2. installer shapes — a fetch of a literal URL that is then EXECUTED, and
   any literal URL whose path ends in a script (added 2026-08-10, after
   two registry scanners failed the launched ci-secure over its own
   `curl | bash` teaching material).

The brand/TLD lists are deliberately bounded to the hosts this repo cites —
a generic scanner is the registry's job; this guard exists to keep OUR
examples from tripping it. Tests that need such a string must construct it
at runtime from concatenated parts; prose must describe the class without
naming a domain.

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

# Two shapes, matching the two things the registry scanners actually key on.
# Both arms deliberately ignore the HOST: the 2026-08-07 pass moved these
# examples onto RFC-reserved domains (`example.com`, `.invalid`) precisely so
# scanners would read them as inert, and both scanners flagged them anyway
# three days later. There is no host that makes an installer literal look
# safe, so the shape itself must stay out of shipped text.
#
# Arm 1 — fetch a literal URL AND execute what comes back, in the forms the
# P14.24 catalog entry itself enumerates. The execution half is required, and
# that is deliberate: a fetch that only DOWNLOADS is not this class, and the
# tree carries dozens of verbatim third-party workflow fixtures under
# `skills/ci-score/tests/fixtures/checkouts-src/` whose plain `curl <url>`
# calls are real-world captures we cannot rewrite without destroying their
# fidelity. A guard that rejected those would be edited away the first time
# it fired. Prose that describes the class without an address
# (`curl … | bash`, as the catalog's own bullet list writes it) is likewise
# untouched — no scheme, no match.
# `[\w-]` after the scheme is what separates a literal address from prose:
# the catalog's own bullet list writes `deno run … https://…` with an
# ellipsis for the host, and that must stay writable.
_URL = r"https?://[\w-]"
_FETCH_AND_EXECUTE = re.compile(
    # Every class excludes `\n`: a negated class in Python matches newlines,
    # so without it an arm stitches a `curl` on one line to a `| bash`
    # hundreds of lines later and reports an unactionable multi-line blob.
    rf"\b(?:curl|wget)\b[^|\n]*{_URL}[^|\n]*\|\s*(?:sudo\s+)?(?:bash|sh)\b"
    rf"|\b(?:bash|sh)\s+<\(\s*(?:curl|wget)\b[^)\n]*{_URL}"
    rf"|\bdeno\s+run\b[^\n]*{_URL}",
    re.IGNORECASE,
)

# Arm 2 — a URL whose PATH carries a script-like segment (`\b`, not an
# anchor, so a query string or a further extension still matches), executed
# or not. This is the arm
# that models Snyk E005 ("suspicious download URL in skill": a link to a
# script or binary), which is why the catalog's download-then-VERIFY recipe
# also had to give up its literal address even though it never pipes.
#
# The `/` before the extension is load-bearing: it requires the script to sit
# at a URL PATH, so real hosts under the .sh ccTLD that this catalog cites
# (Saint Helena's TLD — zizmor's and Astral's doc sites) are not flagged.
_SCRIPT_URL = re.compile(
    r"https?://[^\s\"'`)>\]]+/[^\s\"'`)>\]]*\.(?:sh|ps1|bash)\b", re.IGNORECASE)

# `.fixture` earns its place here: the cloaked workflow fixtures are exactly
# where this class hid. The path cloak (dot-github/ + .fixture) stops a
# scanner reading them as live workflows; it does nothing about their TEXT.
# Four of the eight literal sites this guard was written for lived in `.fixture`
# files, invisible to the old allowlist (the rest were in `.md` and `.py`,
# which the old allowlist covered but the old REGEX could not recognise).
_TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt",
                  ".sh", ".mjs", ".js", ".fixture"}


_SELF = Path(__file__).resolve()


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files"],
                         capture_output=True, text=True, check=True)
    return [
        _REPO / line
        for line in out.stdout.splitlines()
        # Exempt THIS file by path, not by basename: a basename compare would
        # silently exempt any same-named file elsewhere in the tree.
        if Path(line).suffix in _TEXT_SUFFIXES and (_REPO / line).resolve() != _SELF
    ]


def _scannable_texts() -> list[tuple[Path, str]]:
    """Every tracked text file, with its contents — or a loud failure.

    An unreadable file is NOT a clean file. Swallowing the read error would
    make "I never opened it" and "it contains nothing" the same green check,
    which is the fail-open this whole module exists to prevent.
    """
    files = _tracked_text_files()
    assert len(files) > 200, (
        f"guard scanned only {len(files)} tracked files — the suffix allowlist "
        "or `git ls-files` stopped covering the tree, so a green result proves "
        "nothing"
    )
    out, unreadable = [], []
    for path in files:
        try:
            out.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            unreadable.append(f"{path}: {exc}")
    assert not unreadable, (
        "guard could not read tracked file(s); coverage is unproven:\n  "
        + "\n  ".join(unreadable)
    )
    return out


def test_guard_covers_the_paths_this_class_hides_in():
    """Pin the allowlist to the directories that actually shipped the literals.

    Dropping `.fixture` from `_TEXT_SUFFIXES` leaves every scan test green
    otherwise — the file SET has no positive control the way the regexes do.
    """
    scanned = {p.relative_to(_REPO).as_posix() for p in _tracked_text_files()}
    for required in (
        "skills/ci-secure/references/security-patterns.md",
        "skills/ci-secure/tests/test_scan.py",
        "skills/ci-secure/tests/fixtures/dot-github/workflows/"
        "p14_24_curl_pipe_bash.yml.fixture",
    ):
        assert required in scanned, f"guard stopped covering {required}"


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Yield (first_line_number, joined_text) with `\\`-continuations folded.

    A shell/YAML line continuation is the cheapest way to hide this class from
    a line-scoped regex — `curl -fsSL \\` then the URL on the next line reads
    to a scanner exactly like the one-liner. Folding first means the guard
    sees the logical command; keeping the FIRST physical line number means the
    failure message still points at something a maintainer can find.
    """
    out: list[tuple[int, str]] = []
    buf, start = "", None
    for lineno, line in enumerate(text.split("\n"), 1):
        if start is None:
            start = lineno
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        out.append((start, buf + line))
        buf, start = "", None
    if start is not None:
        out.append((start, buf))
    return out


def _parts(*parts: str) -> str:
    """Join at runtime — a CALL, never adjacent literals.

    CPython folds `"a" + "b"` into the finished string at compile time, so a
    scanner that evaluates constant expressions reconstructs exactly what the
    split was hiding. That lesson cost a second pass on the credential
    fixtures (public #28); it applies verbatim here.
    """
    return "".join(parts)


def test_detector_fires_on_a_constructed_lookalike():
    """Positive controls: every brand arm, more than one TLD arm, and a
    MIXED-CASE sample (defends re.IGNORECASE) must match, or the scan below
    is vacuous against that regression. Each sample is runtime-constructed."""
    brands = ["github.com", "gitlab.com", "bitbucket.org", "npmjs.com",
              "pypi.org", "anthropic.com", "starsling.dev"]
    # Every sample goes through _parts(): adjacent `+` literals with no
    # variable between them are constant-folded by CPython into the finished
    # typosquat, which is the exact mistake this file exists to prevent.
    for brand in brands:
        sample = _parts("https://", brand, ".not-really", ".test", "/x")
        assert _LOOKALIKE.search(sample), f"detector lost the {brand} arm"
    mixed = _parts("https://", "GitHub.Com", ".Not-Really", ".Test", "/x")
    assert _LOOKALIKE.search(mixed), "detector lost case-insensitivity"
    io_arm = _parts("https://", "starsling.io", ".not-really", ".test")
    assert _LOOKALIKE.search(io_arm), "detector lost the io TLD arm"
    benign = "https://github.com/starslingdev/skills"
    assert not _LOOKALIKE.search(benign), "detector must not flag the real host"
    cctld = _parts("https://", "github.com", ".au", "/owner/repo")
    assert not _LOOKALIKE.search(cctld), "bare ccTLD-style host must not flag"


def test_installer_detector_fires_on_constructed_samples():
    """Positive controls for both arms, plus the negatives that keep the
    guard from flagging things the catalog legitimately cites."""
    scheme = _parts("htt", "ps://")
    piped = _parts("curl -fsSL ", scheme, "get.example", ".com/install", ".sh | bash")
    assert _FETCH_AND_EXECUTE.search(piped), "fetch-and-execute arm lost the curl form"
    assert _SCRIPT_URL.search(piped), "script-URL arm lost the .sh path form"
    wget = _parts("wget -qO- ", scheme, "example.invalid/x", " | sudo sh")
    assert _FETCH_AND_EXECUTE.search(wget), "fetch-and-execute arm lost the wget form"
    procsub = _parts("bash <(curl -fsSL ", scheme, "example.invalid/x)")
    assert _FETCH_AND_EXECUTE.search(procsub), "lost the process-substitution form"
    deno = _parts("deno run -A ", scheme, "example.invalid/mod.ts")
    assert _FETCH_AND_EXECUTE.search(deno), "lost the deno-run form"
    ps1 = _parts(scheme, "example.invalid/tools/setup", ".ps1")
    assert _SCRIPT_URL.search(ps1), "script-URL arm lost the .ps1 form"
    bash_ext = _parts(scheme, "example.invalid/tools/setup", ".bash")
    assert _SCRIPT_URL.search(bash_ext), "script-URL arm lost the .bash form"

    # A `\`-continuation must not hide the shape (the fold happens in
    # _logical_lines, so assert on the folded form the scan actually sees).
    wrapped = "curl -fsSL \\\n  " + _parts(scheme, "example.invalid/x") + " | bash"
    folded = _logical_lines(wrapped)
    assert len(folded) == 1 and _FETCH_AND_EXECUTE.search(folded[0][1]), \
        "line-continuation folding lost the shape"

    # The shapes this guard must NOT flag, or it becomes unusable here.
    for benign in (
        'curl -fsSL "$INSTALLER_URL" | bash',   # the de-fanged catalog example
        "curl -fsSL -o install.sh \"<installer-url>\"",
        # A fetch that DOWNLOADS but never executes, carrying a real literal
        # URL whose path is not a script. This is the case a bot reviewer
        # raised on PR #39: the guard's contract is fetch-AND-execute, so a
        # non-executing fetch example must stay writable.
        _parts("curl -fsSL -o out.json ", scheme, "api.github.com/repos/o/r"),
        _parts("wget -q -O tags.json ", scheme, "example.invalid/tags"),
        # Prose that names the class without an address — the catalog's own
        # bullet list is written exactly this way.
        "- `curl … | bash` / `curl … | sh` (with or without `sudo`)",
        # The deno bullet is why `_URL` requires a host character after the
        # scheme; without that this guard fails on the catalog itself.
        _parts("- `deno run … ", scheme, "…` (deno executes remote URLs)"),
        _parts(scheme, "docs.zizmor", ".sh/audits/#template-injection"),
        _parts(scheme, "astral", ".sh/blog/open-source-security-at-astral"),
        _parts(scheme, "github.com/starslingdev/skills"),
    ):
        assert not _FETCH_AND_EXECUTE.search(benign), f"false positive: {benign}"
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
    for path, text in _scannable_texts():
        for lineno, logical in _logical_lines(text):
            for rx in (_FETCH_AND_EXECUTE, _SCRIPT_URL):
                for m in rx.finditer(logical):
                    offenders.append(
                        f"{path.relative_to(_REPO)}:{lineno}: {m.group(0)}")
    assert not offenders, (
        "Installer-shaped literal(s) in shipped text. A registry security "
        "scanner reads these as the skill fetching and running remote code — "
        "it failed ci-secure on 2026-08-10 over our own anti-pattern examples. "
        "An RFC-reserved host does NOT clear it; the shape is what matches. "
        "Exactly two shapes are rejected: (1) a fetch of a literal http(s) URL "
        "that is then EXECUTED (piped into a shell, run via process "
        "substitution, or handed to `deno run`) — a fetch that only downloads "
        "is fine; (2) any literal http(s) URL whose PATH ends in .sh/.ps1/"
        ".bash, executed or not, because Snyk E005 flags the link itself. "
        "Fix by writing the address as a placeholder (`<installer-url>`) — the "
        "curl-pipe-bash detector matches on the pipe, not the address — and by "
        "constructing any test string at runtime:\n  " + "\n  ".join(offenders)
    )


def test_no_lookalike_domains_in_tracked_files():
    offenders: list[str] = []
    for path, text in _scannable_texts():
        for m in _LOOKALIKE.finditer(text):
            offenders.append(f"{path.relative_to(_REPO)}: {m.group(0)}")
    assert not offenders, (
        "Attack-shaped look-alike domain literal(s) in shipped text — a registry "
        "security scan WILL read these as malicious URLs (it happened at launch, "
        "2026-07-31). Construct such strings at runtime in tests; describe the "
        "class in prose:\n  " + "\n  ".join(offenders)
    )
