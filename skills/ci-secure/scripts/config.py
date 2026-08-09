"""Shared configuration for ci-secure.

Centralizes the constants used by gh_utils, scan, and report.
"""

import logging
import os
import re
from typing import Final

# Skill version. Stamped into the report's Scanner row so an INSTALLED skill
# (no .git checkout, so no commit sha to record) still carries a provenance
# marker instead of a bare "(unknown)". Bump this on every shipped change.
__version__: Final[str] = "0.1.0"


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
# VERSION
# =============================================================================

VERSION: Final[str] = "0.1.0"
