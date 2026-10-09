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
  * control steps are skipped but counted, because they run nothing;
  * each leaf is tagged `in_parallel_group` (with its top-level group's ordinal)
    and `background`, so a reader whose rule depends on ORDER can tell two
    siblings run at the same time rather than one after the other. Here
    `background` is true for any child of a group (GitHub runs those as
    background steps) and for `background: true` or the STRING "true";
    ci-secure's walker tags only a literal `background: true`. The flag feeds
    only order gates (OPT2, OPT79), never a rendered count;
  * each leaf carries `inherited_if`, the `if:` of the group(s) it sits in.
    GitHub documents no group-level `if:`; treating one as gating the group's
    children is an ASSUMPTION, made so a hygiene rule never calls a step
    "unconditional" when the author wrote a condition around it;
  * a `parallel:` whose value is not a list, a list that contains itself
    through a YAML alias (`steps: &s [{parallel: *s}]`), or a group nested
    deeper than `WALK_MAX_DEPTH` (ci-secure's cap) is MALFORMED: its contents
    are not read, and the walk counts it instead of treating the job as clean
    (or recursing until Python gives up). A group holding an item that is not
    a step mapping is counted malformed too (its readable children are read);
  * a list REUSED through YAML aliases is not a cycle and is read again at
    each use, so sibling groups aliasing one list double the leaves per level
    (40 levels is 2**40 leaves). The walk therefore reads at most
    `WALK_MAX_NODES` leaves and groups per job: on reaching it, the group it
    was about to enter is counted MALFORMED and the walk stops, so the job is
    a named coverage gap instead of a scan that never returns;
  * a `parallel:` on a step that also has `run:` or `uses:` (GitHub rejects
    it) is INVALID: the step's own command is still a leaf, its children are
    read anyway — the same verdict as if they were written flat — and the
    group is counted so the scan names the job as a coverage gap.

The leaf/group/control/malformed/invalid semantics are written to match
ci-score's `_walk_steps` (PR #120, skills/ci-score/scripts/practice_facts.py)
so the two engines read one repository's steps the same way; the repo-root
parity test pins the git-history verdicts that depend on it. The stats share
ci-score's key names (`groups`, `steps_in_groups`, `control_steps`,
`malformed_groups`/`_files`, `invalid_groups`/`_files`) and add three of their
own (`malformed_jobs`, `invalid_jobs`, `jobs_with_groups`). One count differs:
ci-score's `groups` counts only groups whose children were read, while here
`groups` also counts malformed ones (it is the count of `parallel:` keys seen).

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
# Groups nested deeper than this are counted malformed and not read — the same
# cap as ci-secure's `_WALK_MAX_DEPTH`, so the engines agree on what is read.
WALK_MAX_DEPTH = 64
# Leaves + groups one job's walk reads before it stops (see the module
# docstring): far above any real job, far below an alias fan-out's 2**n.
WALK_MAX_NODES = 10_000


@dataclass(frozen=True)
class LeafStep:
    """One step that is neither a `parallel:` group nor a control step."""
    step: dict[str, Any]
    index: int                  # position among the job's leaf steps, 0-based
    in_parallel_group: bool
    group: int | None           # ordinal of the enclosing top-level group
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
                continue
            if is_group_step(item):
                if len(walk.leaves) + walk.groups >= WALK_MAX_NODES:
                    # Budget reached (an alias fan-out): this group is the
                    # unread one, and nothing after it is read either.
                    walk.groups += 1
                    walk.malformed_groups += 1
                    stopped = True
                    return
                walk.groups += 1
                if _is_leaf(item):
                    _leaf(item, group, cond)
                children = item.get(PARALLEL_KEY)
                if (not isinstance(children, list) or id(children) in path
                        or depth >= WALK_MAX_DEPTH):
                    walk.malformed_groups += 1
                    continue
                if _is_leaf(item):
                    walk.invalid_groups += 1
                _visit(children, group if group is not None else walk.groups,
                       _and_if(cond, item.get("if")), path, depth + 1)
                continue
            if is_control_step(item):
                walk.control_steps += 1
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
    """Repo-wide provenance for the step walk (key names shared with ci-score,
    see the module docstring): `parallel:` groups, steps read inside them, control steps skipped, malformed groups
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
                           "malformed_jobs": [], "invalid_groups": 0,
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
                out[f"{kind}_jobs"].append({"path": rel, "job": str(key), "count": n})
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


def parallel_steps_disclosure(stats: Any) -> str | None:
    """The Data sources cell for the step walk, or None when nothing to say.
    The renderer calls this; `verify_report` re-derives the same row with its
    own code and imports nothing from the skill, so a drift fails the check."""
    if not parallel_steps_used(stats):
        return None
    n = int(stats.get("steps_in_groups") or 0)
    groups = int(stats.get("groups") or 0)
    parts = [f"{n} step(s) inside `parallel:` groups read ({groups} group(s))"]
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
        files = ", ".join(f"`{f}`" for f in (stats.get("malformed_files") or [])[:3])
        parts.append(f"**{bad} malformed `parallel:` group(s) not read** (the value "
                     f"is not a readable list of steps){' in ' + files if files else ''}")
    return " · ".join(parts)
