"""Evaluate a GitHub Actions job `if:` expression against an event context.

Only the subset our workflow guards use: `==`, `!=`, `&&`, `||`, `!`,
parentheses, string/number/boolean/null literals, dotted context paths, and
the status functions (`always()`, `success()`, `failure()`, `cancelled()`).
Semantics follow GitHub's documented rules where they matter for routing:

- a missing property is `null`;
- `&&` / `||` return an operand, not a boolean;
- strings compare case-insensitively;
- operands of different types are both cast to numbers (null -> 0,
  booleans -> 0/1, a non-numeric string -> NaN), and NaN equals nothing.

That last rule is why `github.event.pull_request.head.repo.full_name ==
github.repository` is false, and its `!=` twin true, on an event with no
pull request at all (merge_group, push): null casts to 0, the repository name
to NaN. Routing tests have to evaluate that, not grep for it.

Anything outside the subset raises ValueError, so a guard rewritten in a
shape this evaluator cannot read fails loudly instead of being judged wrongly.
Fixture tests: tests/test_gha_expr.py.
"""
from __future__ import annotations

import math
import re
from typing import Any, List, Tuple

_TOKEN = re.compile(r"""\s*(?:
    (?P<op>==|!=|&&|\|\||!|\(|\))
  | '(?P<str>(?:[^']|'')*)'
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)
)""", re.X)

_STATUS = {"always", "success", "failure", "cancelled"}


def _tokens(expr: str) -> List[Tuple[str, Any]]:
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    out, pos = [], 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read expression at {expr[pos:]!r}")
        pos = m.end()
        if m.group("op"):
            out.append(("op", m.group("op")))
        elif m.group("str") is not None:
            out.append(("lit", m.group("str").replace("''", "'")))
        elif m.group("num"):
            out.append(("lit", float(m.group("num"))))
        else:
            out.append(("ident", m.group("ident")))
    return out


def _to_number(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip()) if v.strip() else 0.0
        except ValueError:
            return math.nan
    return math.nan


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b) and not isinstance(a, (dict, list)):
        return a == b
    x, y = _to_number(a), _to_number(b)
    return not (math.isnan(x) or math.isnan(y)) and x == y


def _truthy(v: Any) -> bool:
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not math.isnan(v)
    return True


def evaluate(expr: str, context: dict, status: str = "success") -> Any:
    """Evaluate `expr` with `context` (e.g. {"github": {...}}); `status` feeds success() etc."""
    toks = _tokens(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take(kind=None, value=None):
        nonlocal pos
        tok = peek()
        if tok[0] is None or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise ValueError(f"unexpected {tok!r} in {expr!r}")
        pos += 1
        return tok

    def lookup(path: str) -> Any:
        cur: Any = context
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def primary():
        kind, val = peek()
        if (kind, val) == ("op", "("):
            take()
            v = or_()
            take("op", ")")
            return v
        if kind == "lit":
            take()
            return val
        if kind == "ident":
            take()
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            if peek() == ("op", "("):
                if val not in _STATUS:
                    raise ValueError(f"unsupported function {val}() in {expr!r}")
                take()
                take("op", ")")
                return {"always": True, "success": status == "success",
                        "failure": status == "failure",
                        "cancelled": status == "cancelled"}[val]
            return lookup(val)
        raise ValueError(f"unexpected {peek()!r} in {expr!r}")

    def unary():
        if peek() == ("op", "!"):
            take()
            return not _truthy(unary())
        return primary()

    def compare():
        left = unary()
        while peek()[0] == "op" and peek()[1] in ("==", "!="):
            op = take()[1]
            right = unary()
            left = _equal(left, right) if op == "==" else not _equal(left, right)
        return left

    def and_():
        left = compare()
        while peek() == ("op", "&&"):
            take()
            right = compare()
            left = right if _truthy(left) else left
        return left

    def or_():
        left = and_()
        while peek() == ("op", "||"):
            take()
            right = and_()
            left = left if _truthy(left) else right
        return left

    result = or_()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in {expr!r}: {toks[pos:]}")
    return result


def job_runs(job: dict, context: dict) -> bool:
    """Would a job with this `if:` (absent = success()) be scheduled for this event?"""
    return _truthy(evaluate(str(job.get("if", "success()")), context))


REPO = "starslingdev/skills"


def event(name: str, *, head_repo: Any = REPO) -> dict:
    """An event context for routing tests. `head_repo` is the PR head's full_name
    (None models a deleted fork); it is ignored for events with no pull request."""
    gh: dict = {"event_name": name, "repository": REPO, "event": {}}
    if name == "pull_request":
        gh["event"]["pull_request"] = {
            "head": {"repo": None if head_repo is None else {"full_name": head_repo}}}
    if name == "merge_group":
        gh["event"]["merge_group"] = {"head_ref": "refs/heads/gh-readonly-queue/main/pr-1-abc",
                                      "base_ref": "refs/heads/main"}
    return {"github": gh}


# The events every split (self-hosted / fork) workflow must route, and where each must go.
ROUTES = {
    "push": ("push", {}, "self"),
    "same-repo pull_request": ("pull_request", {}, "self"),
    "fork pull_request": ("pull_request", {"head_repo": "someone/skills"}, "fork"),
    "deleted-fork pull_request": ("pull_request", {"head_repo": None}, "fork"),
    "merge_group": ("merge_group", {}, "self"),
}
