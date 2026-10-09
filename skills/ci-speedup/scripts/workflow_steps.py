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
    and `background` (`background: true`, or any child of a group, which GitHub
    runs as a background step), so a reader whose rule depends on ORDER can tell
    two siblings run at the same time rather than one after the other;
  * a `parallel:` whose value is not a list is MALFORMED: its contents cannot be
    read, and the walk counts it instead of treating the job as clean.

The step/group/control/malformed semantics and the stats keys mirror ci-score's
`_walk_steps` (skills/ci-score/scripts/practice_facts.py) so the two engines read
one repository's steps the same way; the repo-root parity test pins the
git-history verdicts that depend on it.

The returned step mappings are the ORIGINAL objects from the parsed YAML, so a
caller comparing by identity (`step is checkout_step`) keeps working.
Stdlib only; callers pass what they already parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PARALLEL_KEY = "parallel"
# A step carrying one of these keys and neither `run:` nor `uses:` is a control
# step: it blocks on, or stops, a background step and runs no command itself.
CONTROL_KEYS: tuple[str, ...] = ("wait", "wait-all", "cancel")


@dataclass(frozen=True)
class LeafStep:
    """One step that is neither a `parallel:` group nor a control step."""
    step: dict[str, Any]
    index: int                  # position among the job's leaf steps, 0-based
    in_parallel_group: bool
    group: int | None           # ordinal of the enclosing top-level group
    background: bool            # `background: true`, or a child of a group


@dataclass
class StepWalk:
    """Everything one job's `steps:` list held, read once."""
    leaves: list[LeafStep] = field(default_factory=list)
    groups: int = 0             # `parallel:` groups, nested ones included
    steps_in_groups: int = 0    # leaf steps read from inside a group
    background: int = 0         # leaf steps that run in the background
    control_steps: int = 0      # `wait:` / `wait-all:` / `cancel:` skipped
    malformed_groups: int = 0   # `parallel:` values that are not a list

    def steps(self) -> list[dict[str, Any]]:
        return [leaf.step for leaf in self.leaves]


def is_group_step(step: Any) -> bool:
    return (isinstance(step, dict) and PARALLEL_KEY in step
            and "run" not in step and "uses" not in step)


def is_control_step(step: Any) -> bool:
    """A `wait:` / `wait-all:` / `cancel:` step: no `run:`, no `uses:`."""
    return (isinstance(step, dict) and "run" not in step and "uses" not in step
            and any(k in step for k in CONTROL_KEYS))


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def walk_steps(steps: Any) -> StepWalk:
    """Read a `steps:` value (whatever the YAML held) into a `StepWalk`.
    Anything that is not a list reads as no steps, and a non-mapping item is
    skipped, exactly as every flat reader already treated them."""
    walk = StepWalk()

    def _visit(items: Any, group: int | None) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if is_group_step(item):
                walk.groups += 1
                children = item.get(PARALLEL_KEY)
                if not isinstance(children, list):
                    walk.malformed_groups += 1
                    continue
                _visit(children, group if group is not None else walk.groups)
                continue
            if is_control_step(item):
                walk.control_steps += 1
                continue
            bg = group is not None or _truthy(item.get("background"))
            walk.leaves.append(LeafStep(step=item, index=len(walk.leaves),
                                        in_parallel_group=group is not None,
                                        group=group, background=bg))
            if group is not None:
                walk.steps_in_groups += 1
            if bg:
                walk.background += 1

    _visit(steps, None)
    return walk


def job_walk(job: Any) -> StepWalk:
    """`walk_steps` over a job mapping's `steps:`."""
    return walk_steps(job.get("steps") if isinstance(job, dict) else None)


def job_leaf_steps(job: Any) -> list[dict[str, Any]]:
    """Every leaf step of a job, in declaration order — the drop-in replacement
    for `[s for s in job.get("steps") or [] if isinstance(s, dict)]`."""
    return job_walk(job).steps()


def parallel_steps_stats(docs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Repo-wide provenance for the step walk, in ci-score's shape: `parallel:`
    groups, steps read inside them, control steps skipped, and malformed groups
    (with their files and jobs) whose contents could not be read."""
    out: dict[str, Any] = {"groups": 0, "steps_in_groups": 0, "control_steps": 0,
                           "malformed_groups": 0, "malformed_files": [],
                           "malformed_jobs": []}
    for rel, doc in docs:
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            continue
        for key, job in jobs.items():
            w = job_walk(job)
            out["groups"] += w.groups
            out["steps_in_groups"] += w.steps_in_groups
            out["control_steps"] += w.control_steps
            if w.malformed_groups:
                out["malformed_groups"] += w.malformed_groups
                out["malformed_jobs"].append({"path": rel, "job": str(key),
                                              "count": w.malformed_groups})
                if rel not in out["malformed_files"]:
                    out["malformed_files"].append(rel)
    return out


def parallel_steps_used(stats: dict[str, Any] | None) -> bool:
    """Whether the repo uses the syntax at all — the stamp is recorded only
    then, so every other findings document is byte-identical to before."""
    return isinstance(stats, dict) and bool(stats.get("groups") or stats.get("control_steps"))


def parallel_steps_disclosure(stats: Any) -> str | None:
    """The Data sources cell for the step walk, or None when nothing to say.
    The renderer and `verify_report` both call this, so they cannot drift."""
    if not parallel_steps_used(stats):
        return None
    n = int(stats.get("steps_in_groups") or 0)
    groups = int(stats.get("groups") or 0)
    parts = [f"{n} step(s) inside `parallel:` groups read ({groups} group(s))"]
    control = int(stats.get("control_steps") or 0)
    if control:
        parts.append(f"{control} `wait`/`wait-all`/`cancel` control step(s) skipped "
                     "(they run nothing)")
    bad = int(stats.get("malformed_groups") or 0)
    if bad:
        files = ", ".join(f"`{f}`" for f in (stats.get("malformed_files") or [])[:3])
        parts.append(f"**{bad} malformed `parallel:` group(s) not read** (the value "
                     f"is not a list of steps){' in ' + files if files else ''}")
    return " · ".join(parts)
