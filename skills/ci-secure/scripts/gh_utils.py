"""Shared GitHub CLI utilities.

This module provides common utilities for interacting with the GitHub CLI (gh),
including authentication checks, GraphQL execution, and error handling.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from typing import Any

from config import (
    AUTH_ERROR_PATTERNS,
    PENDING_LOG_MARKERS,
    RATE_LIMIT_PATTERNS,
    RATE_LIMIT_RESET_PATTERN,
    REPO_NAME_PATTERN,
    SUBPROCESS_TIMEOUT,
    SUBPROCESS_TIMEOUT_EXTENDED,
    setup_logging,
)

# Set up module logger
logger = setup_logging(__name__)


class GitHubAPIError(RuntimeError):
    """Base exception for GitHub API errors.

    Extends ``RuntimeError`` (not bare ``Exception``) so legacy
    callers that wrap GraphQL / REST calls in ``except RuntimeError``
    keep working after run_graphql switched from raising bare
    ``RuntimeError`` to raising the typed subclasses (``APITimeoutError``,
    ``RateLimitError``, ``AuthenticationError``). New callers can
    still branch on the specific subclass.
    """

    pass


class RateLimitError(GitHubAPIError):
    """Raised when GitHub API rate limit is exceeded."""

    def __init__(self, message: str, reset_time: str | None = None):
        super().__init__(message)
        self.reset_time = reset_time


class AuthenticationError(GitHubAPIError):
    """Raised when GitHub authentication fails or expires."""

    pass


class APITimeoutError(GitHubAPIError):
    """Raised when a GitHub API call times out."""

    pass


def _run_auth_check() -> bool:
    """Run gh auth status and return whether it succeeded.

    Returns:
        True if gh CLI is installed and authenticated, False otherwise.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        logger.debug("gh auth status — returncode=%s", result.returncode)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("Timeout checking gh CLI authentication status")
        return False
    except FileNotFoundError:
        logger.error("gh CLI is not installed. Install from: https://cli.github.com/")
        return False


@lru_cache(maxsize=1)
def check_prereqs() -> bool:
    """Verify gh CLI is installed and authenticated.

    Uses lru_cache to avoid repeated subprocess calls within the same process.

    Returns:
        True if gh CLI is installed and authenticated, False otherwise.
    """
    result = _run_auth_check()
    if not result:
        logger.error("gh CLI is not authenticated. Run: gh auth login")
    return result


def verify_auth_fresh() -> bool:
    """Verify GitHub authentication without caching.

    Use this before critical operations that may fail if token expired.

    Returns:
        True if authenticated, False otherwise.
    """
    result = _run_auth_check()
    if not result:
        logger.warning("GitHub authentication check failed")
    return result


def check_rate_limit_error(stderr: str) -> tuple[bool, str | None]:
    """Check if an error message indicates rate limiting.

    Uses pre-compiled regex patterns from config for efficiency.

    Args:
        stderr: The stderr output to check.

    Returns:
        Tuple of (is_rate_limited, reset_time_if_available).
    """
    for pattern in RATE_LIMIT_PATTERNS:
        if pattern.search(stderr):
            # Try to extract reset time if present
            reset_match = RATE_LIMIT_RESET_PATTERN.search(stderr)
            reset_time = reset_match.group(1) if reset_match else None
            return True, reset_time

    return False, None


def check_auth_error(stderr: str) -> bool:
    """Check if an error message indicates authentication failure.

    Uses pre-compiled regex patterns from config for efficiency.

    Args:
        stderr: The stderr output to check.

    Returns:
        True if authentication error detected, False otherwise.
    """
    for pattern in AUTH_ERROR_PATTERNS:
        if pattern.search(stderr):
            return True
    return False


def run_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: int = SUBPROCESS_TIMEOUT,
) -> dict[str, Any]:
    """Execute a GraphQL query via stdin to avoid shell escaping issues.

    Args:
        query: The GraphQL query string.
        variables: Optional dictionary of variables to pass to the query.
        timeout: Timeout in seconds for the subprocess call.

    Returns:
        Parsed JSON response from the GraphQL API.

    Raises:
        RuntimeError: If the command fails or returns invalid JSON.
        RateLimitError: If GitHub API rate limit is exceeded.
        AuthenticationError: If authentication fails or token expired.
        APITimeoutError: If the GraphQL request times out.
    """
    # Build the full GraphQL request as JSON and pass via stdin
    request_body = {"query": query}
    if variables:
        request_body["variables"] = variables

    cmd = ["gh", "api", "graphql", "--input", "-"]

    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(request_body),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        logger.debug(
            "gh api graphql — query=%s vars=%s, response %d bytes",
            query[:80].replace(chr(10), ' '),
            list(variables.keys()) if variables else [],
            len(result.stdout),
        )
    except subprocess.TimeoutExpired:
        msg = f"GraphQL query timed out after {timeout} seconds"
        logger.error(msg)
        raise APITimeoutError(msg)

    if result.returncode != 0:
        # Check for specific error types
        is_rate_limited, reset_time = check_rate_limit_error(result.stderr)
        if is_rate_limited:
            msg = "GitHub API rate limit exceeded."
            if reset_time:
                msg += f" Resets at: {reset_time}"
            msg += " Please wait and try again."
            logger.warning(msg)
            raise RateLimitError(msg, reset_time)

        if check_auth_error(result.stderr):
            msg = (
                "GitHub authentication failed or token expired. "
                "Please run 'gh auth login' to re-authenticate."
            )
            logger.error(msg)
            raise AuthenticationError(msg)

        logger.error(f"GraphQL query failed: {result.stderr}")
        # GitHubAPIError (a RuntimeError subclass) keeps legacy
        # `except RuntimeError` callers working while letting newer callers
        # catch `except GitHubAPIError` reliably across both API entry points
        # — run_gh_api already raises the typed error for its generic case.
        raise GitHubAPIError(f"GraphQL query failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse GraphQL response: {e}")
        raise GitHubAPIError(
            f"Failed to parse GraphQL response: {e}\nRaw: {result.stdout}"
        )


def run_gh_api(
    endpoint: str,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    jq_filter: str | None = None,
    paginate: bool = False,
    timeout: int = SUBPROCESS_TIMEOUT,
    quiet_not_found: bool = False,
) -> str:
    """Run a gh api command with proper error handling.

    Args:
        endpoint: The API endpoint (e.g., "repos/owner/repo/contents/file").
        method: HTTP method (GET, POST, PUT, DELETE, PATCH).
        fields: Dictionary of fields to pass with -f flag.
        jq_filter: Optional jq filter to apply to the response.
        paginate: Whether to paginate through all results (default: False).
        timeout: Timeout in seconds.
        quiet_not_found: If True, 404 errors are logged at DEBUG level instead
            of ERROR. Useful for existence checks where 404 is expected.

    Returns:
        The stdout content on success.

    Raises:
        APITimeoutError: If the request times out.
        RateLimitError: If GitHub API rate limit is exceeded.
        AuthenticationError: If authentication fails or token expired.
        GitHubAPIError: For other API errors.
    """
    cmd = ["gh", "api", endpoint]

    if method != "GET":
        cmd.extend(["-X", method])

    if paginate:
        cmd.append("--paginate")

    if fields:
        for key, value in fields.items():
            cmd.extend(["-f", f"{key}={value}"])

    if jq_filter:
        cmd.extend(["-q", jq_filter])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        logger.debug(
            "gh api %s — exit=%s, response %d bytes",
            endpoint,
            result.returncode,
            len(result.stdout),
        )
    except subprocess.TimeoutExpired:
        msg = f"API call to {endpoint} timed out after {timeout}s"
        logger.error(msg)
        raise APITimeoutError(msg)

    if result.returncode != 0:
        stderr = result.stderr

        # Check for rate limiting
        is_rate_limited, reset_time = check_rate_limit_error(stderr)
        if is_rate_limited:
            msg = "GitHub API rate limit exceeded."
            if reset_time:
                msg += f" Resets at: {reset_time}"
            msg += " Please wait and try again."
            logger.warning(msg)
            raise RateLimitError(msg, reset_time)

        # Check for auth errors
        if check_auth_error(stderr):
            msg = (
                "GitHub authentication failed or token expired. "
                "Please run 'gh auth login' to re-authenticate."
            )
            logger.error(msg)
            raise AuthenticationError(msg)

        # Generic API error - use debug level for 404s when quiet_not_found is set
        is_not_found = "HTTP 404" in stderr or "Not Found" in stderr
        if quiet_not_found and is_not_found:
            logger.debug(f"API call to {endpoint} returned 404: {stderr}")
        else:
            logger.error(f"API call to {endpoint} failed: {stderr}")
        raise GitHubAPIError(f"API call failed: {stderr}")

    return result.stdout


def run_gh_raw(
    args: list[str],
    timeout: int = SUBPROCESS_TIMEOUT_EXTENDED,
) -> tuple[int, bytes, str]:
    """Run a gh command that may return binary data (e.g., logs).

    Args:
        args: Command arguments (without 'gh' prefix).
        timeout: Timeout in seconds (uses extended timeout by default).

    Returns:
        Tuple of (return_code, stdout_bytes, stderr_string).
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            timeout=timeout,
        )
        logger.debug(
            "gh %s — exit=%s, response %d bytes",
            " ".join(args)[:80],
            proc.returncode,
            len(proc.stdout),
        )
        return proc.returncode, proc.stdout, proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        logger.error(f"gh command timed out after {timeout}s: {' '.join(args)}")
        return -1, b"", f"Timeout after {timeout} seconds"


def is_zip_payload(data: bytes) -> bool:
    """Check if binary data is a ZIP file (starts with PK header).

    Args:
        data: Binary data to check.

    Returns:
        True if data appears to be a ZIP file, False otherwise.
    """
    return data.startswith(b"PK")


def is_log_pending(message: str) -> bool:
    """Check if an error message indicates logs are pending.

    Args:
        message: The message to check.

    Returns:
        True if logs appear to be pending, False otherwise.
    """
    lowered = message.lower()
    return any(marker in lowered for marker in PENDING_LOG_MARKERS)


def validate_repo_format(repo: str) -> tuple[bool, str]:
    """Validate repository format is 'owner/repo'.

    Args:
        repo: Repository string to validate.

    Returns:
        Tuple of (is_valid, error_message_if_invalid).
    """
    if "/" not in repo:
        return False, "Repository must be in 'owner/repo' format (missing '/')"

    parts = repo.split("/")
    if len(parts) != 2:
        return False, f"Repository must be in 'owner/repo' format (found {len(parts)} parts separated by '/')"

    owner, name = parts

    if not owner:
        return False, "Repository owner cannot be empty"

    if not name:
        return False, "Repository name cannot be empty"

    # Use pre-compiled pattern for validation
    if not REPO_NAME_PATTERN.match(owner):
        return False, f"Invalid owner name '{owner}': must contain only letters, numbers, dashes, underscores, or dots"

    if not REPO_NAME_PATTERN.match(name):
        return False, f"Invalid repository name '{name}': must contain only letters, numbers, dashes, underscores, or dots"

    return True, ""


def check_repo_is_org(owner: str, repo: str) -> tuple[bool, str]:
    """Check if a repository belongs to a GitHub organization.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Tuple of (is_org, owner_type) where owner_type is e.g. "Organization" or "User".

    Raises:
        GitHubAPIError: If the API call fails or returns empty output.
    """
    stdout = run_gh_api(f"repos/{owner}/{repo}", jq_filter=".owner.type")
    owner_type = stdout.strip().strip('"')
    if not owner_type:
        raise GitHubAPIError(
            f"Could not determine owner type for {owner}/{repo}. "
            "The API returned an empty response."
        )
    return owner_type == "Organization", owner_type


def normalize_line_endings(content: str) -> str:
    """Normalize line endings to Unix-style (LF).

    Handles Windows (CRLF) and old Mac (CR) line endings.

    Args:
        content: The content to normalize.

    Returns:
        Content with Unix-style line endings.
    """
    return content.replace("\r\n", "\n").replace("\r", "\n")
