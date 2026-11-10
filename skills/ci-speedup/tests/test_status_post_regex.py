"""`_STATUS_POST_RE` matches a commit-status call in linear time.

The `repos/<owner>/<repo>/statuses/<sha>` branch used to alternate a
`${{ ... }}` expression with "any non-space, non-quote character". That second
class also matched `$`, `{` and `}`, so every expression could be consumed two
ways and a long run of `${{` fragments with no closing `/statuses/` backtracked
through every split (CodeQL `py/redos`). The branches are now disjoint: a `$`
either opens a complete `${{ ... }}` expression or stands alone, and the
catch-all class excludes `$`.

These tests pin two things. First, the old and new pattern return the same
`search()` result and span on a corpus of realistic lines, so no detection moved.
Second, the new pattern finishes the adversarial input quickly. The old pattern
is kept here only for the corpus comparison on short strings: it is NEVER run on
the long attack inputs, because that is the hang being fixed.

The one accepted difference is a `${{` that is still open at the `/statuses/`
where the old pattern stopped (`repos/${{x/statuses/`, `repos/${{x/statuses/y}}`).
The old catch-all class matched those three characters one by one; the new
pattern treats `${{ ... }}` as one unit. GitHub rejects a workflow whose
expression does not close, and `/statuses/` inside an expression is not a path
the API call hits, so no real detection moves.
`test_open_expression_is_the_only_difference` pins that behavior so a future
edit cannot widen the gap unnoticed.

Both repeats are also bounded (200 units each). Disjoint branches alone leave a
whole-file search quadratic in the number of `repos/` prefixes, because every
prefix starts a walk to the end of its line. The bound caps each walk, and a
real owner/repo path with expressions is far shorter than the limit.
`test_bounds_leave_real_paths_alone` pins the limit from both sides.
"""
from __future__ import annotations

import itertools
import random
import re
import sys
import time
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import scan  # noqa: E402

_NEW = scan._STATUS_POST_RE

# The pattern as shipped before the fix. Only the first alternative changed.
_OLD = re.compile(
    r"repos/(?:\$\{\{[^}]*\}\}|[^\s'\"`])*?/statuses/"
    r"|\bcreate_?(?:commit_?)?status\s*\("
    r"|\bcreateCommitStatus\b|\bCreateStatus\s*\("
    r"|\buses:\s*['\"]?(?:Sibz/github-status-action|ouzi-dev/commit-status-updater"
    r"|myrotvorets/set-commit-status-action|guibranco/github-status-action-v2)@",
    re.IGNORECASE)

# Every string is short: the old pattern is safe on each of them.
_MAX_CORPUS_LEN = 300

_CORPUS = [
    # --- shapes taken from this suite's own tests and fixtures ---
    "      - run: gh api repos/${{ github.repository }}/statuses/${{ github.sha }}"
    " -f context='All CI' -f state=pending",
    "      - run: gh api repos/${{ github.repository }}/statuses/${{ github.sha }}"
    " -f context='Preview' -f state=success",
    "      - run: gh api repos/${{ github.repository }}/commits/${{ github.sha }}/statuses",
    "gh api repos/o/r/statuses/$SHA -f \"context=All CI\" -f state=success",
    "gh api repos/o/r/statuses/$SHA -f context='All CI'",
    "gh api repos/o/r/statuses/$SHA -f context=lint",
    "          gh api repos/${{ github.repository }}/statuses/"
    "${{ github.event.pull_request.head.sha }} \\\n"
    "            -f state=pending -f context='All CI'\n",
    "name: Aggregate\non: pull_request\njobs:\n  publish:\n"
    "    name: Publish aggregate\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: gh api repos/${{ github.repository }}/statuses/${{ github.sha }}"
    " -f context='All CI' -f state=pending\n",
    "name: Reader\non: pull_request\njobs:\n  read:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: gh api repos/${{ github.repository }}/commits/"
    "${{ github.sha }}/statuses\n      - run: echo 'All CI'\n",
    # --- hand-written: expressions ---
    "repos/${{ github.repository }}/statuses/${{ github.sha }}",
    "repos/${{github.repository}}/statuses/${{github.sha}}",
    "repos/${{ env.OWNER }}/${{ env.REPO }}/statuses/${{ env.SHA }}",
    "repos/${{ a }}${{ b }}/statuses/",
    "repos/${{x}}/statuses/",
    "repos/${{}}/statuses/",
    "repos/$${{ x }}/statuses/",
    "repos/${{ github.repository }}}/statuses/x",
    "repos/${{ fromJSON(inputs.cfg).repo }}/statuses/${{ github.sha }}",
    "repos/${{ format('{0}/{1}', 'o', 'r') }}/statuses/sha",
    "repos/${{ github.event.pull_request.head.repo.full_name }}/statuses/"
    "${{ github.event.pull_request.head.sha }}",
    "repos/${{ github.repository }} /statuses/x",
    "repos/${{ github.repository }}/statuses /x",
    # --- hand-written: plain, shell, templating ---
    "repos/o/r/statuses/",
    "repos/o/r/statuses",
    "repos/o/r/commits/x/statuses",
    "repos/o/r/commits/x/statuses/",
    "REPOS/O/R/STATUSES/",
    "repos/$OWNER/$REPO/statuses/$SHA",
    "repos/${OWNER}/${REPO}/statuses/${SHA}",
    "repos/$GITHUB_REPOSITORY/statuses/$GITHUB_SHA",
    "repos/{owner}/{repo}/statuses/{sha}",
    "repos/%s/%s/statuses/%s",
    "repos/#{owner}/#{repo}/statuses/#{sha}",
    "repos/$/statuses/",
    "repos/${/statuses/",
    "repos/o/r$/statuses/",
    "repos/o/r{/statuses/",
    "repos/o/r}}/statuses/",
    "repos/$$/statuses/",
    "repos/{{o}}/{{r}}/statuses/",
    "repos/o r/statuses/",
    "repos/o/r /statuses/",
    "repos/o/'r/statuses/",
    "repos/o/\"r/statuses/",
    "repos/o/`r/statuses/",
    "repos/o/r\n/statuses/",
    "repos/o r/statuses/ and repos/o/r/statuses/x",
    "repos//statuses/",
    "repos/statuses/",
    # --- hand-written: quoted and embedded forms ---
    "curl -X POST \"https://api.github.com/repos/$OWNER/$REPO/statuses/$SHA\"",
    "curl -sf 'https://api.github.com/repos/o/r/statuses/abc' -d @payload.json",
    "await octokit.request(`POST /repos/${owner}/${repo}/statuses/${sha}`, body)",
    "github.request('POST /repos/{owner}/{repo}/statuses/{sha}', {owner, repo, sha})",
    "requests.post(f\"{API}/repos/{owner}/{repo}/statuses/{sha}\", json=payload)",
    "\"repos/{}/{}/statuses/{}\".format(owner, repo, sha)",
    "fmt.Sprintf(\"repos/%s/%s/statuses/%s\", owner, repo, sha)",
    "client.post(\"repos/#{owner}/#{repo}/statuses/#{sha}\", payload)",
    "url = 'https://api.github.com/repos/' + repo + '/statuses/' + sha",
    "url = \"https://api.github.com/repos/\" + repo + \"/statuses/\" + sha",
    "GET /repos/o/r/commits/abc/statuses",
    # --- the other alternatives, which must be untouched ---
    "await github.rest.repos.createCommitStatus({ ...context.repo, sha, state })",
    "repo.get_commit(sha).create_status(state='success', context='All CI')",
    "client.Repositories.CreateStatus(ctx, owner, repo, sha, status)",
    "create_commit_status(owner, repo, sha, state)",
    "      - uses: Sibz/github-status-action@v1.1.6",
    "      - uses: 'ouzi-dev/commit-status-updater@v2'",
    "      - uses: \"myrotvorets/set-commit-status-action@v2.0.1\"",
    "      - uses: guibranco/github-status-action-v2@v1",
    "      - uses: actions/checkout@v4",
    "echo done",
    "",
]


def _same(text: str) -> None:
    assert len(text) <= _MAX_CORPUS_LEN, "keep corpus lines short: the old pattern runs on them"
    old = _OLD.search(text)
    new = _NEW.search(text)
    if old is None or new is None:
        assert old is None and new is None, (text, old, new)
    else:
        assert old.span() == new.span(), (text, old.span(), new.span())
        assert old.group(0) == new.group(0)


@pytest.mark.parametrize("text", _CORPUS, ids=range(len(_CORPUS)))
def test_new_pattern_matches_exactly_what_the_old_one_did(text: str) -> None:
    _same(text)


def test_corpus_exercises_both_outcomes() -> None:
    hits = sum(1 for t in _CORPUS if _NEW.search(t))
    assert 20 <= hits < len(_CORPUS)


def test_realistic_calls_are_detected() -> None:
    assert _NEW.search("repos/${{ github.repository }}/statuses/${{ github.sha }}")
    assert _NEW.search("repos/o/r/statuses/")
    assert _NEW.search("repos/$OWNER/$REPO/statuses/$SHA")
    assert _NEW.search("repos/${OWNER}/${REPO}/statuses/${SHA}")
    assert _NEW.search("await octokit.request(`POST /repos/${owner}/${repo}/statuses/${sha}`)")
    assert not _NEW.search("repos/o/r/commits/x/statuses")
    assert not _NEW.search("repos/${{ github.repository }}/commits/${{ github.sha }}/statuses")


# Short building blocks. Concatenations stay well under the corpus limit, so the
# old pattern is safe on them, and the alphabet covers every character the
# rewrite touched: `$`, `{`, `}`, spaces, quotes and the `/statuses/` suffix.
_TOKENS = ("repos/", "/statuses/", "/statuses", "${{ x }}", "${{x}}", "${{", "}}", "$",
           "{", "}", "${X}", "o", "/", " ", "'", "\"", "`", "/commits/x")
_OPEN_EXPRESSION = re.compile(r"\$\{\{(?![^}]*\}\})")


def _differs_by_design(text: str) -> bool:
    """The old match text still has a `${{` open at its end: the documented difference."""
    old = _OLD.search(text)
    return old is not None and _OPEN_EXPRESSION.search(old.group(0)) is not None


def test_random_token_strings_agree() -> None:
    rng = random.Random(20261109)
    compared = skipped = 0
    for _ in range(4000):
        text = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 12)))
        if _differs_by_design(text):
            skipped += 1  # pinned separately below
            continue
        _same(text)
        compared += 1
    assert compared > 3000
    assert skipped > 0


def test_every_short_combination_agrees() -> None:
    core = ("${{ x }}", "${{x}}", "}}", "$", "{", "}", "o", "/", " ", "'")
    compared = skipped = 0
    for n in range(0, 4):
        for parts in itertools.product(core, repeat=n):
            text = "repos/" + "".join(parts) + "/statuses/"
            if _differs_by_design(text):
                skipped += 1  # pinned separately below
                continue
            _same(text)
            compared += 1
    assert compared + skipped == 1 + 10 + 100 + 1000
    assert compared > 1000


def test_open_expression_is_the_only_difference() -> None:
    # A `${{` still open at `/statuses/` is not a valid expression. The old
    # catch-all class matched it character by character; the new pattern does not.
    for text in ("repos/${{x/statuses/", "repos/${{x/statuses/y}}", "repos/${{/statuses/"):
        assert _differs_by_design(text), text
        assert _OLD.search(text), text
        assert _NEW.search(text) is None, text
    # With a space after the open `${{` neither pattern matched before either.
    assert not _differs_by_design("repos/${{ x/statuses/")
    _same("repos/${{ x/statuses/")
    # Closing the expression before `/statuses/` restores agreement.
    assert not _differs_by_design("repos/${{x}}/statuses/")
    _same("repos/${{x}}/statuses/")
    # Everything in the fixed corpus is outside the difference.
    assert not any(_differs_by_design(text) for text in _CORPUS)


def test_bounds_leave_real_paths_alone() -> None:
    long_expr = "${{ github.event.pull_request.head.repo.full_name || github.repository }}"
    assert _NEW.search(f"repos/{long_expr}/statuses/{long_expr}")
    assert _NEW.search("repos/" + "o" * 200 + "/statuses/")
    assert _NEW.search("repos/${{ " + "x" * 198 + " }}/statuses/")
    # Past the bound the pattern stops; nothing real is this long.
    assert _NEW.search("repos/" + "o" * 201 + "/statuses/") is None
    assert _NEW.search("repos/${{ " + "x" * 199 + " }}/statuses/") is None


# The old pattern needs minutes on these inputs; the new one takes milliseconds.
# The limit is generous so a loaded CI runner (the suite runs with four workers)
# cannot flake it, and still far below what a backtracking pattern needs.
_TIME_LIMIT_S = 2.0


@pytest.mark.parametrize("fragment,count", [
    ("${{", 5000),
    ("${{ ", 5000),
    ("${{x}}", 5000),
    ("$", 20000),
    ("${", 10000),
    ("${{x}", 5000),
    ("$o/", 5000),
])
def test_adversarial_input_finishes_in_linear_time(fragment: str, count: int) -> None:
    # Never run _OLD here: on these inputs it takes minutes to hours.
    text = "repos/" + fragment * count
    start = time.perf_counter()
    found = _NEW.search(text)
    elapsed = time.perf_counter() - start
    assert found is None
    assert elapsed < _TIME_LIMIT_S, f"{fragment!r} x {count}: {elapsed:.2f}s"


@pytest.mark.parametrize("fragment,count", [
    ("repos/", 10000),
    ("repos/${{x", 10000),
    ("repos/${{ x }}", 10000),
    ("repos/$o", 10000),
    ("repos/o/r/commits/x/statuses ", 2000),
])
def test_repeated_prefixes_finish_in_linear_time(fragment: str, count: int) -> None:
    # Every `repos/` starts another attempt; the bounded repeats cap each one.
    # The unbounded form took 0.4 s at 2000 prefixes and 1.45 s at 4000.
    text = fragment * count
    start = time.perf_counter()
    found = _NEW.search(text)
    elapsed = time.perf_counter() - start
    assert found is None
    assert elapsed < _TIME_LIMIT_S, f"{fragment!r} x {count}: {elapsed:.2f}s"
