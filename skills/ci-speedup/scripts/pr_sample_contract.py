"""Provisional public defaults shared by PR-sample collection and reporting.

These values stay centralized while the report-defaults review decides their
long-term public values. Consumers import them instead of restating the numbers.
"""

PR_SAMPLE_TARGET = 100
PR_SAMPLE_MIN_DISTINCT_DAYS = 5
PR_SAMPLE_CANDIDATE_PAGE_CAP = 12
PR_SAMPLE_JOB_LIST_READ_CAP = 1200
PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR = 1
PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR = 2


_stop_reason_labels = {
    "era_door": "workflow configuration boundary reached",
}


def pr_sample_stop_reason_label(reason: object) -> str:
    """Reader-facing explanation for an internal sampler stop code."""
    key = str(reason or "")
    return _stop_reason_labels.get(key, key.replace("_", " "))


def pr_sample_goal_text() -> str:
    return (f"{PR_SAMPLE_TARGET} completed PR commits across "
            f"{PR_SAMPLE_MIN_DISTINCT_DAYS} distinct days")


def pr_sample_is_mostly_partial(partial: int, sampled: int) -> bool:
    return (sampled > 0
            and partial * PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR
            > sampled * PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR)
