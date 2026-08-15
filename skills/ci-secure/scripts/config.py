"""Shared configuration for ci-secure.

Centralizes the constants used by gh_utils, scan, and report.
"""
# Defers annotation evaluation, which is what makes PEP 604 (`X | Y`) safe on
# Python 3.9 - the floor `pyproject.toml` declares. Without it, a parameter or
# return annotation is evaluated when the `def` runs, so 3.9 raises TypeError at
# IMPORT time and takes the whole scanner down with it. This is an INSTALLED
# skill: it runs under whatever python3 a user has, while every workflow here
# pins 3.12, so CI cannot catch that break for you. Every other module under
# scripts/ carries this line for the same reason.
from __future__ import annotations

import logging
import os
import re
from typing import Final

# Skill version. Stamped into the report's Scanner row so an INSTALLED skill
# (no .git checkout, so no commit sha to record) still carries a provenance
# marker instead of a bare "(unknown)". Bump this on every shipped change.
__version__: Final[str] = "0.2.0"


# Default log level resolved from STARSLING_LOG_LEVEL env var
# (e.g. `STARSLING_LOG_LEVEL=DEBUG`). Falls back to INFO when unset
# or set to a value Python's logging module doesn't recognize.
def _resolve_default_level() -> int:
    raw = os.environ.get("STARSLING_LOG_LEVEL", "INFO").upper().strip()
    candidate = logging.getLevelName(raw)
    return candidate if isinstance(candidate, int) else logging.INFO


DEFAULT_LOG_LEVEL: Final[int] = _resolve_default_level()

# =============================================================================
# API AND SUBPROCESS CONFIGURATION
# =============================================================================

# Default timeout for subprocess calls (in seconds)
SUBPROCESS_TIMEOUT: Final[int] = 30

# Extended timeout for operations that may take longer (e.g., paginated APIs)
SUBPROCESS_TIMEOUT_EXTENDED: Final[int] = 60

# =============================================================================
# WORKFLOW ACTIVITY (used by scan.py when --repo is supplied)
# =============================================================================

# A workflow with no runs in this many days is treated as "dormant"
DORMANT_DAYS: Final[int] = 90

# Max recent runs to inspect per workflow when computing activity
ACTIVITY_RUN_LIMIT: Final[int] = 50

# =============================================================================
# LOG ANALYSIS (used by gh_utils.is_log_pending)
# =============================================================================

PENDING_LOG_MARKERS: Final[tuple[str, ...]] = (
    "still in progress",
    "log will be available when it is complete",
)

# =============================================================================
# PRE-COMPILED REGEX PATTERNS
# =============================================================================

# Pattern for validating repository owner/name
REPO_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9._-]+$")

# Rate limit error patterns (case-insensitive)
RATE_LIMIT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in [
        r"rate limit exceeded",
        r"API rate limit",
        r"secondary rate limit",
        r"abuse detection",
        r"403.*rate",
    ]
)

# Pattern to extract rate limit reset time
RATE_LIMIT_RESET_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"reset (?:at |in )?(\d+|[\d:]+)", re.IGNORECASE
)

# Authentication error patterns (case-insensitive)
AUTH_ERROR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in [
        # HTTP status codes must be word-bounded; a bare `401` substring
        # match would fire on any stderr containing those three digits
        # (e.g. a rate-limit reset epoch or a content-length header) and
        # surface a misleading "run gh auth login" message.
        r"\bHTTP\s+401\b",
        r"\b401\s+Unauthorized\b",
        r"authentication\s+(failed|required|error)",
        r"\bunauthorized\b",
        r"bad credentials",
        r"token.*expired",
        r"not logged in",
    ]
)

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

LOG_FORMAT: Final[str] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    name: str,
    level: int = DEFAULT_LOG_LEVEL,
    stream_handler: bool = True,
) -> logging.Logger:
    """Set up and return a configured logger.

    The default ``level`` is resolved from the ``STARSLING_LOG_LEVEL``
    environment variable at module load time (e.g. ``STARSLING_LOG_LEVEL=DEBUG``).
    Falls back to ``INFO`` when unset or invalid.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    if logger.parent and logger.parent.handlers:
        logger.setLevel(level)
        return logger

    root_logger = logging.getLogger()
    if root_logger.handlers:
        logger.setLevel(level)
        return logger

    logger.setLevel(level)

    if stream_handler:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


# =============================================================================
# CONFIG-FACT OUTCOMES — the blocking rule
# =============================================================================
#
# `config_facts.py` gives every check an OUTCOME. Three separate questions are
# asked about one, and each gets its own name here, because collapsing any two
# of them is how a new failure state ships green:
#
#   BLOCKING_OUTCOMES  which outcomes fail a build
#   KNOWN_OUTCOMES     which outcomes anything here recognises at all
#   OUTCOME_MARKS      how each one is displayed to a reader
#
# NONE of these is derived from another, and in particular the first two are
# never computed from the third. Deriving the allowlist from the display table
# reads as tidy and is a security hole: adding a display style for a new
# outcome would silently widen the set of outcomes accepted as recognised, so
# the "outcome I cannot classify" red never fires — while the new outcome is
# still not in BLOCKING_OUTCOMES, so it does not fail either. A one-line
# cosmetic edit would then be enough to ship a failure state as a pass.
#
# The CI gate (`.github/scripts/ci_secure_gate.py`) imports all three by name.
# Adding an outcome to `config_facts.py` means editing all three deliberately;
# the census test in `tests/test_ci_secure_gate_resolution.py` fails until you do.

BLOCKING_OUTCOMES: Final[frozenset[str]] = frozenset({"fail"})

KNOWN_OUTCOMES: Final[frozenset[str]] = frozenset({"pass", "fail", "unmeasured"})

OUTCOME_MARKS: Final[dict[str, str]] = {
    "pass": "PASS",
    "fail": "**FAIL**",
    "unmeasured": "UNMEASURED",
}


def coverage_is_complete(
    blocking: frozenset[str] | set[str] = BLOCKING_OUTCOMES,
    known: frozenset[str] | set[str] = KNOWN_OUTCOMES,
    marks: dict[str, str] | None = None,
) -> bool:
    """True when the three tables above are mutually coherent.

    Coherent means: everything that blocks is recognised, and everything
    recognised can be displayed. It is NOT a claim about `config_facts.py` —
    that the engine's actual vocabulary fits inside these tables is a census
    the test suite runs against the source, since this module cannot import
    the engine without dragging PyYAML into a stdlib-only gate.

    The parameters exist so the predicate can be exercised against tables
    other than the live ones; production callers pass nothing.
    """
    marks = OUTCOME_MARKS if marks is None else marks
    return set(blocking) <= set(known) <= set(marks)


def flatten_scanned(value: object) -> str:
    """Flatten and neutralize an ATTACKER-CONTROLLED scanned string.

    Everything ci-secure reports about is read out of the repository under
    audit — workflow file names, job names, fact evidence — and on a fork pull
    request an attacker writes all of it. Three characters carry structure in
    the Markdown these values land in:

      newline   starts a row the tool did not write, which can read as a pass
      backtick  closes an inline code span (or a fence) early, so whatever
                follows renders as live Markdown on the same line
      pipe      splits a table row into phantom columns

    Collapsing all whitespace kills the first, replacing the backtick kills the
    second, escaping the pipe kills the third. This is the single definition of
    that rule: the report renderer and the CI gate both use it, so a value that
    is safe in one surface cannot be unsafe in the other.
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("`", "'").replace("|", "\\|")


# =============================================================================
# VERSION
# =============================================================================

VERSION: Final[str] = "0.2.0"
