"""One reader for a job's YAML `steps:` list, shared by every static detector.

GitHub Actions lets a step be a `parallel:` group (a LIST of ordinary child
steps that run concurrently, with an implicit wait at the end of the group), lets
a step carry `background: true`, and adds control steps (`wait:`, `wait-all:`,
`cancel:`) that run no command of their own:

    steps:
      - uses: actions/checkout@v4
      - parallel:
          - run: npm run lint
          - run: npm run typecheck
      - run: npm test

(Workflow syntax: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsparallel)

A reader that treats `job.steps` as a flat list of `run:`/`uses:` steps sees the
`parallel:` item as one step with neither key, so every child inside it is
invisible: a duplicate build, an uncached install or a `fetch-depth: 0`
checkout written inside a group is never read. `walk_steps` is the fix, and the
only place in this skill that knows the shape:

  * every LEAF step is returned once, in declaration order, with a group's
    children spliced in where the group stands (nested groups too — the docs do
    not address nesting, and reading more is the safe direction);
  * control steps are skipped but counted (a named one keeps its `name:` in
    `control_names`), because they run nothing;
  * each leaf is tagged `in_parallel_group`, `group` and `background`, so a
    reader whose rule depends on ORDER can tell two siblings run at the same
    time rather than one after the other. `group` is a running id shared by
    every leaf under one top-level group (it is the `groups` count when that
    group was entered, so it is not a 1-based ordinal of top-level groups), and
    None for a step outside any group. `background` is true for any child of a
    group (GitHub runs those as background steps) and for `background: true`
    or a string "true" in any case;
  * each leaf carries `inherited_if`, the `if:` of the group(s) it sits in.
    GitHub documents no group-level `if:`; treating one as gating the group's
    children is an ASSUMPTION, made so a hygiene rule never calls a step
    "unconditional" when the author wrote a condition around it;
  * a group is MALFORMED, its contents not read, when its `parallel:` value is
    not a list (`not_a_list`), the list contains itself through a YAML alias
    (`contains_itself`, e.g. `steps: &s [{parallel: *s}]`), or it is nested
    more than `WALK_MAX_DEPTH` groups deep (`nested_too_deep`). A group holding
    an item that is not a step mapping is malformed too (`non_mapping_step`;
    its readable children are still read). Each kind is recorded in
    `malformed_reasons` (`MALFORMED_KINDS` words it), and the walk counts the
    group instead of treating the job as clean (or recursing until Python
    gives up);
  * a list REUSED through YAML aliases is not a cycle and is read again at
    each use, so sibling groups aliasing one list double the leaves per level
    (40 levels is 2**40 leaves). The walk therefore reads at most
    `WALK_MAX_NODES` leaves and groups per job: on reaching it, the group it
    was about to enter is counted MALFORMED (`too_many_steps`) and the walk
    stops, so the job is a named coverage gap instead of a scan that never
    returns;
  * a `parallel:` on a step that also has `run:` or `uses:` (GitHub rejects
    it) is INVALID: the step's own command is still a leaf, and its children
    are read as the group's children (tagged in-group), so an order-free
    verdict such as the git-history check matches a flat read. The group is
    counted under `invalid_*`, which the Data sources row names; it is not a
    coverage gap, since its steps were read.

The leaf/group/control/malformed/invalid semantics follow ci-score's
`_walk_steps` (skills/ci-score/scripts/practice_facts.py, PR #120) so the two
engines read one repository's steps the same way; the repo-root parity test
pins the git-history verdicts that depend on it. The stats share ci-score's
COUNTER names (`groups`, `steps_in_groups`, `control_steps`,
`malformed_groups`, `invalid_groups`, pinned by that parity test) and record
what was unreadable in their own shape: files, jobs and reasons here
(`malformed_files`/`_jobs`/`_reasons`, `invalid_files`/`_jobs`), one record per
entry there (`invalid`, `malformed`, `skipped`). The counts are NOT comparable
across engines: ci-score's `groups` counts only groups whose children were
read, while here `groups` also counts malformed ones (it is the count of
`parallel:` keys seen); a group that is one step mapping is `invalid` there
and `malformed` here; a non-step item inside a group is `skipped` there and
makes the group `malformed` here. The stats also add their own lists
(`background_steps`, `jobs_with_groups`, `jobs_with_background`,
`sequential_jobs`).

The returned step mappings are the ORIGINAL objects from the parsed YAML (never
copied or mutated), so a caller comparing by identity (`step is checkout_step`)
keeps working.
Stdlib only; callers pass what they already parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PARALLEL_KEY = "parallel"
# A step carrying one of these keys and neither `run:` nor `uses:` is a control
# step: it blocks on, or stops, a background step and runs no command itself.
CONTROL_KEYS: tuple[str, ...] = ("wait", "wait-all", "cancel")
# Groups nested deeper than this are counted malformed and not read. ci-secure's
# walker (PR #121) uses the same number in different units: it counts every
# YAML node with `> 64`, this counts `parallel:` nesting with `>= 64`, so the
# two do not promise to agree on what is read.
WALK_MAX_DEPTH = 64
# Leaves + groups one job's walk reads before it stops (see the module
# docstring): far above any real job, far below an alias fan-out's 2**n.
WALK_MAX_NODES = 10_000

# Why a `parallel:` group was not (fully) read, in plain words: the gap record
# and the Data sources row print these, never the key. `verify_report` carries
# an equal copy (pinned by a coupling test).
MALFORMED_KINDS: dict[str, str] = {
    "not_a_list": "the value is not a list of steps",
    "contains_itself": "the list contains itself",
    "nested_too_deep": f"nested more than {WALK_MAX_DEPTH} groups deep",
    "too_many_steps": "more steps than the walk reads (a YAML alias fan-out)",
    "non_mapping_step": "an item in it is not a step",
}


@dataclass(frozen=True)
class LeafStep:
    """One step that is neither a `parallel:` group nor a control step."""
    step: dict[str, Any]
    index: int                  # position among the job's leaf steps, 0-based
    in_parallel_group: bool
    group: int | None           # running id shared under one top-level group
    background: bool            # `background: true`, or a child of a group
    # The enclosing groups' `if:` — undocumented by GitHub; inherited as an
    # assumption. Nested groups are AND-ed outermost first, `(outer) && (inner)`,
    # so no written gate is dropped. None when no enclosing group has one.
    inherited_if: str | None = None


@dataclass
class StepWalk:
    """Everything one job's `steps:` list held, read once."""
    leaves: list[LeafStep] = field(default_factory=list)
    groups: int = 0             # `parallel:` groups, nested ones included
    steps_in_groups: int = 0    # leaf steps read from inside a group
    background: int = 0         # leaf steps that run in the background
    control_steps: int = 0      # `wait:` / `wait-all:` / `cancel:` skipped
    malformed_groups: int = 0   # not a list / contains itself / too deep: not read
    invalid_groups: int = 0     # beside `run:`/`uses:` on one step: read anyway
    # One kind per malformed group, in walk order (`MALFORMED_KINDS`), so a gap
    # says WHY its steps were not read.
    malformed_reasons: list[str] = field(default_factory=list)
    # The `name:` of each control step that has one: GitHub may render the step
    # under it (`Wait for lint`), which no bare control-name pattern can see.
    control_names: list[str] = field(default_factory=list)

    def steps(self) -> list[dict[str, Any]]:
        return [leaf.step for leaf in self.leaves]


def is_group_step(step: Any) -> bool:
    """Any step carrying `parallel:`. One that also has `run:`/`uses:` is a
    group AND a leaf (counted invalid); with a control key, the group wins."""
    return isinstance(step, dict) and PARALLEL_KEY in step


def _is_leaf(step: dict[str, Any]) -> bool:
    return "run" in step or "uses" in step


def is_control_step(step: Any) -> bool:
    """A `wait:` / `wait-all:` / `cancel:` step: no `run:`, no `uses:`."""
    return (isinstance(step, dict) and "run" not in step and "uses" not in step
            and any(k in step for k in CONTROL_KEYS))


def _truthy(v: Any) -> bool:
    # A `${{ ... }}` expression reads as foreground, so a background count may understate.
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _and_if(outer: str | None, inner: Any) -> str | None:
    """`outer` AND a step's own `if:` (None / blank adds nothing)."""
    if inner is None or (isinstance(inner, str) and not inner.strip()):
        return outer
    if outer is None:  # a flat step's own `if:` comes back exactly as written
        return inner if isinstance(inner, str) else str(inner)
    return f"({outer}) && ({str(inner).strip()})"


def effective_if(leaf: LeafStep) -> str | None:
    """The condition a leaf runs under: its groups' inherited `if:` AND its
    own, or None when neither is written. Never mutates the step."""
    return _and_if(leaf.inherited_if, leaf.step.get("if"))


def walk_steps(steps: Any) -> StepWalk:
    """Read a `steps:` value (whatever the YAML held) into a `StepWalk`.
    Anything that is not a list reads as no steps, and a non-mapping item at
    the top level is skipped, exactly as every flat reader already treated
    them; inside a group, one marks that group malformed."""
    walk = StepWalk()
    stopped = False  # the walk budget was reached: read nothing more

    def _leaf(item: dict[str, Any], group: int | None, cond: str | None) -> None:
        bg = group is not None or _truthy(item.get("background"))
        walk.leaves.append(LeafStep(step=item, index=len(walk.leaves),
                                    in_parallel_group=group is not None,
                                    group=group, background=bg, inherited_if=cond))
        if group is not None:
            walk.steps_in_groups += 1
        if bg:
            walk.background += 1

    def _visit(items: Any, group: int | None, cond: str | None,
               path: frozenset[int], depth: int) -> None:
        nonlocal stopped
        if not isinstance(items, list):
            return
        path = path | {id(items)}  # the lists on THIS branch: a repeat is a cycle
        dropped_child = False
        for item in items:
            if stopped:
                return
            if not isinstance(item, dict):
                # Skipped at the top level as every flat reader did; inside a
                # group it is a child the walk could not read, so the group
                # counts malformed once (its readable children are still read).
                if depth > 0 and not dropped_child:
                    dropped_child = True
                    walk.malformed_groups += 1
                    walk.malformed_reasons.append("non_mapping_step")
                continue
            if is_group_step(item):
                if len(walk.leaves) + walk.groups >= WALK_MAX_NODES:
                    # Budget reached (an alias fan-out): this group is the
                    # unread one, and nothing after it is read either.
                    walk.groups += 1
                    walk.malformed_groups += 1
                    walk.malformed_reasons.append("too_many_steps")
                    stopped = True
                    return
                walk.groups += 1
                if _is_leaf(item):
                    _leaf(item, group, cond)
                children = item.get(PARALLEL_KEY)
                bad = ("not_a_list" if not isinstance(children, list)
                       else "contains_itself" if id(children) in path
                       else "nested_too_deep" if depth >= WALK_MAX_DEPTH else None)
                if bad:
                    walk.malformed_groups += 1
                    walk.malformed_reasons.append(bad)
                    continue
                if _is_leaf(item):
                    walk.invalid_groups += 1
                _visit(children, group if group is not None else walk.groups,
                       _and_if(cond, item.get("if")), path, depth + 1)
                continue
            if is_control_step(item):
                walk.control_steps += 1
                if isinstance(item.get("name"), str) and item["name"].strip():
                    walk.control_names.append(item["name"])
                continue
            _leaf(item, group, cond)

    _visit(steps, None, None, frozenset(), 0)
    return walk


def job_walk(job: Any) -> StepWalk:
    """`walk_steps` over a job mapping's `steps:`."""
    return walk_steps(job.get("steps") if isinstance(job, dict) else None)


def job_leaf_steps(job: Any) -> list[dict[str, Any]]:
    """Every leaf step of a job, in declaration order — the drop-in replacement
    for `[s for s in job.get("steps") or [] if isinstance(s, dict)]`."""
    return job_walk(job).steps()


def parallel_steps_stats(docs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Repo-wide provenance for the step walk (counter names shared with
    ci-score, see the module docstring): `parallel:` groups, steps read inside them, control steps skipped, malformed groups
    (with their files and jobs) whose contents could not be read, invalid
    groups (read anyway, with their files and jobs), and the three job lists a
    renderer needs to word a pole's step drill honestly:

      * `jobs_with_groups` — every job holding at least one `parallel:` group;
      * `jobs_with_background` — every job with a `background: true` step
        outside any group (`background_steps` counts those steps);
      * `sequential_jobs` — in a file holding either kind, every OTHER job, so
        a pole on one of them keeps "its steps run one after another" while a
        pole that matches no list in such a file is worded as uncertain.

    A step in either of the first two lists overlaps other steps, so a renderer
    never says that job's steps run one after another."""
    out: dict[str, Any] = {"groups": 0, "steps_in_groups": 0, "control_steps": 0,
                           "background_steps": 0,
                           "malformed_groups": 0, "malformed_files": [],
                           "malformed_jobs": [], "malformed_reasons": [],
                           "invalid_groups": 0,
                           "invalid_files": [], "invalid_jobs": [],
                           "jobs_with_groups": [], "jobs_with_background": [],
                           "sequential_jobs": []}
    for rel, doc in docs:
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            continue
        plain: list[dict[str, Any]] = []
        overlapping = False
        for key, job in jobs.items():
            w = job_walk(job)
            out["groups"] += w.groups
            out["steps_in_groups"] += w.steps_in_groups
            out["control_steps"] += w.control_steps
            row = {"path": rel, "job": str(key)}
            # The display name, when set, so a pole labelled by it matches.
            if isinstance(job, dict) and isinstance(job.get("name"), str) and job["name"]:
                row["name"] = job["name"]
            bg_only = sum(1 for lf in w.leaves if lf.background and not lf.in_parallel_group)
            out["background_steps"] += bg_only
            if w.groups:
                out["jobs_with_groups"].append(row)
            if bg_only:
                out["jobs_with_background"].append(dict(row))
            if w.groups or bg_only:
                overlapping = True
            else:
                plain.append(row)
            for kind, n in (("malformed", w.malformed_groups),
                            ("invalid", w.invalid_groups)):
                if not n:
                    continue
                out[f"{kind}_groups"] += n
                jrow: dict[str, Any] = {"path": rel, "job": str(key), "count": n}
                if kind == "malformed":
                    jrow["reasons"] = list(dict.fromkeys(w.malformed_reasons))
                    for r in jrow["reasons"]:
                        if r not in out["malformed_reasons"]:
                            out["malformed_reasons"].append(r)
                out[f"{kind}_jobs"].append(jrow)
                if rel not in out[f"{kind}_files"]:
                    out[f"{kind}_files"].append(rel)
        if overlapping:
            out["sequential_jobs"].extend(plain)
    return out


def parallel_steps_used(stats: dict[str, Any] | None) -> bool:
    """Whether the repo uses the syntax at all — the stamp is recorded only
    then, so every other findings document is byte-identical to before."""
    return isinstance(stats, dict) and bool(stats.get("groups") or stats.get("control_steps")
                                            or stats.get("background_steps"))


def _files_cell(files: Any) -> str:
    """At most three files in backticks, then "and N more file(s)"."""
    files = [f for f in (files or []) if isinstance(f, str)]
    shown = ", ".join(f"`{f}`" for f in files[:3])
    if len(files) > 3:
        shown += f", and {len(files) - 3} more file(s)"
    return shown


_GROUPS_FEEDS = ("Static detectors read each step inside a `parallel:` group "
                 "as its own step")
_BACKGROUND_FEEDS = ("Static detectors read `background: true` steps, which run "
                     "beside the steps after them, as ordinary steps")
_BOTH_FEEDS = (_GROUPS_FEEDS + "; `background: true` steps, which run beside the "
               "steps after them, are read as ordinary steps")


def parallel_steps_used_for(stats: Any) -> str:
    """The Data sources "Used for" cell beside `parallel_steps_disclosure`,
    naming what the repo actually uses: `parallel:` groups (any seen, malformed
    and invalid included), `background: true` steps, or both. A repo with only
    control steps keeps the group sentence. `verify_report` re-derives it."""
    stats = stats if isinstance(stats, dict) else {}
    groups = int(stats.get("groups") or 0)
    bg = int(stats.get("background_steps") or 0)
    if not bg:
        return _GROUPS_FEEDS
    return _BOTH_FEEDS if groups else _BACKGROUND_FEEDS


def parallel_steps_disclosure(stats: Any) -> str | None:
    """The Data sources cell for the step walk, or None when nothing to say.
    The renderer calls this; `verify_report` re-derives the WHOLE cell with its
    own code and imports nothing from the skill, so a drift fails the check.

    It names the steps read inside groups and how many `parallel:` keys were
    SEEN (malformed and invalid ones included), the background and control
    steps, each malformed group's kind (`MALFORMED_KINDS`) and files, and any
    invalid group (beside `run:`/`uses:` on one step: read, but GitHub rejects
    the workflow), with its files."""
    if not parallel_steps_used(stats):
        return None
    n = int(stats.get("steps_in_groups") or 0)
    groups = int(stats.get("groups") or 0)
    parts = [f"{n} step(s) inside `parallel:` groups read ({groups} `parallel:` "
             "group(s) seen)"]
    bg = int(stats.get("background_steps") or 0)
    if bg:
        parts.append(f"{bg} `background: true` step(s) read (they run beside the "
                     "steps after them)")
    control = int(stats.get("control_steps") or 0)
    if control:
        parts.append(f"{control} `wait`/`wait-all`/`cancel` control step(s) skipped "
                     "(they run nothing)")
    bad = int(stats.get("malformed_groups") or 0)
    if bad:
        kinds = "; ".join(MALFORMED_KINDS.get(str(k), str(k))
                          for k in (stats.get("malformed_reasons") or [])) or \
            "the value is not a readable list of steps"
        files = _files_cell(stats.get("malformed_files"))
        parts.append(f"**{bad} malformed `parallel:` group(s) not read** ({kinds})"
                     f"{' in ' + files if files else ''}")
    inv = int(stats.get("invalid_groups") or 0)
    if inv:
        files = _files_cell(stats.get("invalid_files"))
        parts.append(f"**{inv} invalid `parallel:` group(s)** (on a step that also "
                     "has `run:` or `uses:`, which GitHub rejects; the steps inside "
                     f"were read as the group's children){' in ' + files if files else ''}")
    return " · ".join(parts)
