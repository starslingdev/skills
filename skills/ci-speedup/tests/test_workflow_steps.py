"""Unit tests for the shared step walker (`scripts/workflow_steps.py`): the
shapes a flat or naive walk mis-reads — a step that is both a `parallel:` group
and a `run:`/`uses:` leaf, a `parallel:` that contains itself through a YAML
alias, a group-level `if:`, and the per-job record of which jobs hold groups."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workflow_steps as ws  # noqa: E402


def test_a_group_on_a_run_step_still_reads_its_children_and_is_counted_invalid():
    """`parallel:` beside `run:` on one step is not valid GitHub syntax. The
    step's own command is still a leaf, its children are read anyway (never a
    silent drop), and the group is counted as rejected so the scan names it."""
    job = {"steps": [{"run": "npm test",
                      "parallel": [{"run": "git log --oneline"}]}]}
    w = ws.job_walk(job)
    runs = [leaf.step.get("run") for leaf in w.leaves]
    assert runs == ["npm test", "git log --oneline"], runs
    assert w.invalid_groups == 1 and w.malformed_groups == 0, w
    assert w.groups == 1 and w.steps_in_groups == 1, w


def test_a_group_on_a_uses_step_with_a_control_key_reads_the_same_way():
    job = {"steps": [{"uses": "actions/checkout@v4", "wait": "db",
                      "parallel": [{"run": "git describe --tags"}]}]}
    w = ws.job_walk(job)
    assert [leaf.step.get("run") or leaf.step.get("uses") for leaf in w.leaves] == [
        "actions/checkout@v4", "git describe --tags"]
    assert w.invalid_groups == 1 and w.control_steps == 0, w


def test_a_parallel_list_that_contains_itself_stops_and_is_counted_malformed():
    """`steps: &s [{parallel: *s}]` loads as a list that contains itself. The
    walk must stop, not recurse until Python's recursion limit."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load("jobs:\n  a:\n    steps: &s\n      - parallel: *s\n"
                         "      - run: npm test\n")
    w = ws.job_walk(doc["jobs"]["a"])
    assert w.malformed_groups == 1, w
    assert [leaf.step.get("run") for leaf in w.leaves] == ["npm test"]


def test_a_group_level_if_is_inherited_by_its_children():
    job = {"steps": [
        {"if": "github.event_name == 'push'",
         "parallel": [{"run": "pip install foo"},
                      {"if": "env.X != ''",
                       "parallel": [{"run": "pip install bar", "if": "env.Y"}]}]},
        {"run": "echo top"},
    ]}
    leaves = ws.job_walk(job).leaves
    by_run = {leaf.step["run"]: leaf for leaf in leaves}
    assert by_run["pip install foo"].inherited_if == "github.event_name == 'push'"
    assert by_run["pip install bar"].inherited_if == (
        "(github.event_name == 'push') && (env.X != '')")
    assert ws.effective_if(by_run["pip install bar"]) == (
        "((github.event_name == 'push') && (env.X != '')) && (env.Y)")
    assert ws.effective_if(by_run["pip install foo"]) == "github.event_name == 'push'"
    assert by_run["echo top"].inherited_if is None
    assert ws.effective_if(by_run["echo top"]) is None
    # The parsed YAML is never mutated.
    assert "if" not in job["steps"][0]["parallel"][0]


def test_jobs_with_groups_names_every_job_that_holds_a_group():
    docs = [("a.yml", {"jobs": {"flat": {"steps": [{"run": "x"}]},
                                "grouped": {"steps": [{"parallel": [{"run": "y"}]}]}}}),
            ("b.yml", {"jobs": {"wait_only": {"steps": [{"wait-all": None}]},
                                "bad": {"steps": [{"parallel": "nope"}]}}})]
    stats = ws.parallel_steps_stats(docs)
    assert stats["jobs_with_groups"] == [{"path": "a.yml", "job": "grouped"},
                                         {"path": "b.yml", "job": "bad"}], stats


def test_jobs_with_groups_carries_the_display_name_when_the_job_has_one():
    # The pole drill matches a pole's check/job against this row; a job whose
    # `name:` differs from its key is only matchable when the name is stamped.
    docs = [("ci.yml", {"jobs": {"test": {"name": "Unit tests",
                                          "steps": [{"parallel": [{"run": "a"}]}]}}})]
    stats = ws.parallel_steps_stats(docs)
    assert stats["jobs_with_groups"] == [{"path": "ci.yml", "job": "test",
                                          "name": "Unit tests"}], stats


def test_a_job_with_only_background_steps_is_stamped_too():
    """`background: true` alone (no group) also makes step times overlap: the
    job is recorded under `jobs_with_background`, the repo counts as using the
    syntax, the row says so, and the other jobs in that file are recorded as
    running in sequence (so a pole on one of them keeps its sequential wording,
    while a pole matching neither list is hedged)."""
    docs = [("ci.yml", {"jobs": {
        "it": {"name": "Integration", "steps": [
            {"run": "./start-db.sh", "background": True}, {"run": "npm test"}]},
        "lint": {"steps": [{"run": "npm run lint"}]}}}),
        ("other.yml", {"jobs": {"x": {"steps": [{"run": "make"}]}}})]
    stats = ws.parallel_steps_stats(docs)
    assert stats["jobs_with_background"] == [
        {"path": "ci.yml", "job": "it", "name": "Integration"}], stats
    assert stats["background_steps"] == 1, stats
    assert stats["sequential_jobs"] == [{"path": "ci.yml", "job": "lint"}], stats
    assert ws.parallel_steps_used(stats)
    row = ws.parallel_steps_disclosure(stats)
    assert row is not None and "1 `background: true` step(s)" in row, row


def test_invalid_groups_are_recorded_per_job():
    docs = [("c.yml", {"jobs": {"j": {"steps": [{"run": "a", "parallel": [{"run": "b"}]}]}}})]
    stats = ws.parallel_steps_stats(docs)
    assert stats["invalid_groups"] == 1 and stats["invalid_files"] == ["c.yml"], stats
    assert stats["invalid_jobs"] == [{"path": "c.yml", "job": "j", "count": 1}], stats
    assert stats["malformed_groups"] == 0, stats


def _alias_fanout_doc(n: int) -> str:
    # Each level is a two-group list whose groups BOTH alias the level below,
    # so the expanded tree holds 2**n leaves with no list containing itself.
    lines = ["x0: &x0 [{run: a}]"]
    for i in range(1, n + 1):
        lines.append(f"x{i}: &x{i} [{{parallel: *x{i - 1}}}, {{parallel: *x{i - 1}}}]")
    lines.append(f"jobs: {{j: {{steps: *x{n}}}}}")
    return "\n".join(lines)


def test_an_alias_fan_out_stops_at_the_walk_budget_and_is_counted_malformed():
    """A list reused by sibling groups through YAML aliases is not a cycle, but
    it doubles the leaves per level: n=40 is 2**40 leaves and the walk never
    returns. The walk reads at most `WALK_MAX_NODES` leaves and groups, then
    counts the group it was entering as malformed and stops, so the job lands
    in `scan_incomplete` instead of hanging every detector that walks it."""
    import signal
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_alias_fanout_doc(40))

    def _timeout(*_a):
        raise TimeoutError("walk did not finish within 10s")

    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(10)
    try:
        w = ws.job_walk(doc["jobs"]["j"])
        stats = ws.parallel_steps_stats([("fan.yml", doc)])
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert w.malformed_groups == 1, w.malformed_groups
    # The budget is checked on entering a group, so a walk can pass it by at
    # most the one group it stops on plus one list's direct leaves (here 1).
    assert len(w.leaves) + w.groups <= ws.WALK_MAX_NODES + 2, (len(w.leaves), w.groups)
    assert stats["malformed_files"] == ["fan.yml"], stats["malformed_files"]
    assert [(r["path"], r["job"], r["count"]) for r in stats["malformed_jobs"]] == [
        ("fan.yml", "j", 1)], stats["malformed_jobs"]


def test_a_small_alias_reuse_is_read_in_full_under_the_budget():
    # Reuse below the budget is ordinary YAML: every use is read, nothing malformed.
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_alias_fanout_doc(4))
    w = ws.job_walk(doc["jobs"]["j"])
    assert len(w.leaves) == 16 and w.malformed_groups == 0, w


def test_a_non_mapping_child_inside_a_group_marks_the_group_malformed():
    """A flat `steps:` list has always skipped a non-mapping item, but inside a
    `parallel:` list a dropped child is a step the walk did not read: the group
    is counted malformed (so the job is named), and its readable children are
    still read."""
    job = {"steps": [{"parallel": [{"run": "npm run lint"}, "npm run typecheck",
                                   ["nested"]]},
                     {"run": "npm test"}, "stray"]}
    w = ws.job_walk(job)
    assert w.malformed_groups == 1, w
    assert [leaf.step["run"] for leaf in w.leaves] == ["npm run lint", "npm test"]
    # A non-mapping item at the TOP level is skipped as before: not a group.
    assert ws.job_walk({"steps": [{"run": "a"}, "stray"]}).malformed_groups == 0
