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


def test_a_parallel_only_job_stamps_no_background_steps():
    """Children of a `parallel:` group run in the background, but they are
    counted as steps in groups, not as `background: true` steps: the "Used
    for" cell keys on `background_steps` and must keep the group sentence."""
    docs = [("ci.yml", {"jobs": {"grouped": {"steps": [
        {"parallel": [{"run": "a"}, {"run": "b"}]}, {"run": "c"}]}}})]
    stats = ws.parallel_steps_stats(docs)
    assert stats["groups"] == 1 and stats["steps_in_groups"] == 2, stats
    assert stats["background_steps"] == 0, stats
    assert stats["jobs_with_background"] == [], stats
    assert ws.parallel_steps_used_for(stats) == ws._GROUPS_FEEDS


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


def _nested(levels: int) -> dict:
    node: list = [{"run": "deep"}]
    for _ in range(levels):
        node = [{"parallel": node}]
    return {"steps": node}


def test_malformed_groups_record_which_kind_they_are():
    """S14: each malformed group carries its kind, so the coverage-gap record and
    the row say WHY the steps were not read, not always "not a readable list"."""
    yaml = pytest.importorskip("yaml")
    cases = {
        "not_a_list": {"steps": [{"parallel": "nope"}]},
        "contains_itself": yaml.safe_load(
            "jobs:\n  a:\n    steps: &s\n      - parallel: *s\n")["jobs"]["a"],
        "nested_too_deep": _nested(65),
        "too_many_steps": yaml.safe_load(_alias_fanout_doc(16))["jobs"]["j"],
        "non_mapping_step": {"steps": [{"parallel": [{"run": "a"}, "b"]}]},
    }
    for kind, job in cases.items():
        w = ws.job_walk(job)
        assert w.malformed_reasons == [kind], (kind, w.malformed_reasons)
        stats = ws.parallel_steps_stats([("x.yml", {"jobs": {"j": job}})])
        assert stats["malformed_jobs"][0]["reasons"] == [kind], stats["malformed_jobs"]


def test_the_row_names_malformed_kinds_more_files_and_invalid_groups():
    """S9/S14/D7: the Data sources cell says how many groups were SEEN (malformed
    and invalid included), names each malformed kind, lists at most three files
    then "and N more", and names invalid groups (read, but GitHub rejects them)."""
    docs = [(f"w{i}.yml", {"jobs": {"j": {"steps": [{"parallel": "nope"}]}}})
            for i in range(4)]
    docs.append(("v.yml", {"jobs": {"k": {"steps": [{"run": "a",
                                                    "parallel": [{"run": "b"}]}]}}}))
    row = ws.parallel_steps_disclosure(ws.parallel_steps_stats(docs))
    assert row == (
        "1 step(s) inside `parallel:` groups read (5 `parallel:` group(s) seen)"
        " · **4 malformed `parallel:` group(s) not read** (the value is not a list"
        " of steps) in `w0.yml`, `w1.yml`, `w2.yml`, and 1 more file(s)"
        " · **1 invalid `parallel:` group(s)** (on a step that also has `run:` or"
        " `uses:`, which GitHub rejects; the steps inside were read as the group's"
        " children) in `v.yml`"), row


def test_an_indirect_cycle_is_malformed_and_the_rest_is_read_once():
    """T4: `&s [{parallel: [{parallel: *s}]}, {run: npm test}]` loops through
    an intermediate group; the walk stops at the repeat and reads `npm test`
    exactly once (a double read would be a false duplicate-build finding)."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load("jobs:\n  a:\n    steps: &s\n"
                         "      - parallel:\n          - parallel: *s\n"
                         "      - run: npm test\n")
    w = ws.job_walk(doc["jobs"]["a"])
    assert w.malformed_groups == 1, w
    assert [lf.step.get("run") for lf in w.leaves].count("npm test") == 1, w.leaves


def test_a_shared_acyclic_list_used_twice_is_read_twice_and_is_valid():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load("x: &x [{run: lint}]\n"
                         "jobs: {a: {steps: [{parallel: *x}, {parallel: *x}]}}\n")
    w = ws.job_walk(doc["jobs"]["a"])
    assert w.malformed_groups == 0 and len(w.leaves) == 2, w


def test_the_depth_cap_reads_64_nested_levels_and_stops_at_65():
    assert ws.job_walk(_nested(64)).malformed_groups == 0
    assert ws.job_walk(_nested(65)).malformed_groups == 1


def test_walker_small_shapes():
    """T10: a non-list `parallel:` on a run step is a leaf plus a malformed (not
    invalid) group; nested groups each count; background is tagged and counted
    (a string "true" in any case); a blank or non-string `if:` adds nothing."""
    w = ws.job_walk({"steps": [{"run": "x", "parallel": "nope"}]})
    assert [lf.step["run"] for lf in w.leaves] == ["x"]
    assert (w.malformed_groups, w.invalid_groups) == (1, 0), w
    w = ws.job_walk({"steps": [{"parallel": [{"parallel": [{"run": "a"}]}]}]})
    assert w.groups == 2 and w.steps_in_groups == 1, w
    w = ws.job_walk({"steps": [{"run": "a", "background": "TRUE"}, {"run": "b"}]})
    assert w.background == 1 and w.leaves[0].background and not w.leaves[1].background
    assert ws._truthy("true") and ws._truthy(" True ") and not ws._truthy("yes")
    assert ws._and_if(None, "") is None and ws._and_if("a", "  ") == "a"
    assert ws._and_if(None, True) == "True" and ws._and_if("a", True) == "(a) && (True)"
