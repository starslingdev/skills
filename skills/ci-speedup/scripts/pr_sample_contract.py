"""Provisional public defaults shared by PR-sample collection and reporting.

These values stay centralized while the report-defaults review decides their
long-term public values. Consumers import them instead of restating the numbers.
"""

PR_SAMPLE_TARGET = 100
PR_SAMPLE_MIN_DISTINCT_DAYS = 5
PR_SAMPLE_CANDIDATE_PAGE_CAP = 12
PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR = 1
PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR = 2


def pr_sample_goal_text() -> str:
    return (f"{PR_SAMPLE_TARGET} completed PR commits across "
            f"{PR_SAMPLE_MIN_DISTINCT_DAYS} distinct days")


def pr_sample_is_mostly_partial(partial: int, sampled: int) -> bool:
    return (sampled > 0
            and partial * PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR
            > sampled * PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR)
