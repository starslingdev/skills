"""Conservative push-cohort comparison for post-fix CI verification."""
from __future__ import annotations

from typing import Any


def compare_push_cohorts(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    changed_files: list[str],
    ci_paths: tuple[str, ...] = (".github/", "scripts/ci/", "Dockerfile", "docker-compose"),
) -> dict[str, Any]:
    """Compare complete before and after push cohorts."""
    return {"available": False, "reason": "comparison engine pending"}
