"""GitHub Actions parallel steps (shipped 2026-06-25): a step may be
`- parallel:` holding a LIST of ordinary child steps, and the control steps
`- wait:` / `- wait-all:` / `- cancel:` carry no `run:`/`uses:`.

Every check that reads steps read `job.steps` as a flat list, so a child step
inside a `parallel:` group was invisible and a verdict could flip on a step the
engine never saw. The invariant pinned here: wrapping the DECISIVE step of a
check's positive or negative fixture in `- parallel:` never changes the
verdict. Plus: a control step never crashes a check, the checks that read no
steps are unaffected, and a malformed `parallel:` (not a list) is disclosed in
the findings document and the report header, never silently skipped.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SPEC_PATH = _SKILL_DIR / "references" / "ci-score-spec.json"


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pf_mod = _load("ci_score_practice_facts", "scripts/practice_facts.py")
cc_mod = _load("ci_score_collect_config", "scripts/collect_config.py")
rr_mod = _load("ci_score_render_report", "scripts/render_report.py")
vr_mod = _load("ci_score_verify_report", "scripts/verify_report.py")

SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
PR_ON = {"pull_request": None}


def _wf(steps: list, on=None, job_id: str = "b") -> dict:
    return {"on": PR_ON if on is None else on, "jobs": {job_id: {"steps": steps}}}


def _parsed(doc: dict, rel: str = "ci.yml") -> list[tuple[str, dict, str]]:
    raw = yaml.safe_dump(doc, sort_keys=False)
    return [(rel, yaml.safe_load(raw), raw)]


def _wrap(doc: dict, idx: int, job_id: str = "b") -> dict:
    """The same workflow with step `idx` moved inside a `parallel:` group (next
    to a harmless sibling, as a real group would hold)."""
    out = copy.deepcopy(doc)
    steps = out["jobs"][job_id]["steps"]
    steps[idx] = {"parallel": [steps[idx], {"run": "true"}]}
    return out


def _roots(tmp_path: Path, files: tuple[str, ...]) -> Path:
    for f in files:
        (tmp_path / f).write_text("{}\n")
    return tmp_path


CHECKOUT_DEEP = {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}

# (case id, check id, root files, flat steps, index of the decisive step, verdict)
# Every row's step is DECISIVE (the test asserts removing it changes the
# verdict). A build-cache FAIL and a shallow-checkout PASS have no such row:
# the first is decided by the root build-tool config alone, the second holds
# with no checkout step at all, so wrapping a step could prove nothing.
CASES = [
    ("dep-cache pass: cache action", "ci.cache.dependency-cache", ("package.json",),
     [{"run": "npm ci"}, {"uses": "actions/cache@v4"}], 1, "pass"),
    ("dep-cache pass: setup-* cache input", "ci.cache.dependency-cache", ("package.json",),
     [{"uses": "actions/setup-node@v4", "with": {"cache": "pnpm"}}], 0, "pass"),
    ("dep-cache fail: install, no cache", "ci.cache.dependency-cache", (),
     [{"run": "pnpm install"}], 0, "fail"),
    ("dep-cache applicable: inline install is the only signal", "ci.cache.dependency-cache", (),
     [{"run": "pip install pytest"}], 0, "fail"),
    ("dep-cache applicable: setup action is the only signal", "ci.cache.dependency-cache", (),
     [{"uses": "actions/setup-python@v5"}], 0, "fail"),
    ("build-cache pass: cache action", "ci.cache.build-cache", ("turbo.json",),
     [{"run": "turbo build"}, {"uses": "actions/cache@v4"}], 1, "pass"),
    ("shallow fail: fetch-depth 0", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"run": "npm test"}], 0, "fail"),
    ("shallow pass: history op exempts the job", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"run": "git log --oneline"}], 1, "pass"),
    ("shallow pass: history action exempts the job", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"uses": "tj-actions/changed-files@v45"}], 1, "pass"),
    ("change-scoped pass: --filter", "ci.build.change-scoped", ("turbo.json",),
     [{"run": "pnpm turbo run build --filter=...[origin/main]"}], 0, "pass"),
    ("change-scoped pass: changed-files action", "ci.build.change-scoped", ("turbo.json",),
     [{"uses": "dorny/paths-filter@v3"}, {"run": "pnpm turbo run build"}], 0, "pass"),
    ("change-scoped pass: github-script code", "ci.build.change-scoped", ("turbo.json",),
     [{"uses": "actions/github-script@v7", "with": {"script": "const affected = 1"}}], 0, "pass"),
    ("change-scoped applicable: nx command is the only task-graph signal",
     "ci.build.change-scoped", (), [{"run": "npx nx affected -t test"}], 0, "pass"),
    ("change-scoped fail: unscoped", "ci.build.change-scoped", (),
     [{"run": "pnpm turbo run build"}], 0, "fail"),
    ("pinned pass: every action pinned", "ci.security.pinned-action-shas", (),
     [{"uses": f"actions/checkout@{SHA}"}], 0, "pass"),
    ("pinned fail: unpinned action inside the group", "ci.security.pinned-action-shas", (),
     [{"uses": f"actions/checkout@{SHA}"}, {"uses": "some-org/deploy@main"}], 1, "fail"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_wrapping_the_decisive_step_in_parallel_never_changes_the_verdict(tmp_path, case):
    _cid, check, root_files, steps, idx, want = case
    root = _roots(tmp_path, root_files)
    flat = _wf(steps)
    assert pf_mod._practice_facts(_parsed(flat), root)[check]["state"] == want, \
        "fixture precondition: the flat workflow must give the pinned verdict"
    # The step must be DECISIVE: with it gone the verdict changes, so a walker
    # that dropped the group's children could not pass this case by accident.
    without = copy.deepcopy(flat)
    del without["jobs"]["b"]["steps"][idx]
    assert pf_mod._practice_facts(_parsed(without), root)[check]["state"] != want, \
        "fixture precondition: the wrapped step must decide the verdict"
    wrapped = _wrap(flat, idx)
    got = pf_mod._practice_facts(_parsed(wrapped), root)[check]
    assert got["state"] == want, (
        f"{check} read {got['state']!r} once the decisive step moved inside a "
        f"`parallel:` group (flat workflow reads {want!r}): {got['evidence']}")


def test_nested_parallel_groups_are_read_too(tmp_path):
    flat = _wf([{"uses": f"actions/checkout@{SHA}"}, {"uses": "some-org/deploy@main"}])
    nested = copy.deepcopy(flat)
    nested["jobs"]["b"]["steps"][1] = {"parallel": [{"parallel": [{"uses": "some-org/deploy@main"}]}]}
    f = pf_mod._practice_facts(_parsed(nested), tmp_path)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail" and "1 of 2" in f["evidence"], f["evidence"]


AUTOMATION_SIGNALS = [
    ("test command", {"run": "pytest -q"}),
    ("install command", {"run": "npm ci"}),
    ("build command", {"run": "go build ./..."}),
    ("build action", {"uses": "docker/build-push-action@v6"}),
]


@pytest.mark.parametrize("label,step", AUTOMATION_SIGNALS, ids=[a[0] for a in AUTOMATION_SIGNALS])
def test_a_project_work_signal_inside_parallel_never_refuses_as_automation_only(tmp_path, label, step):
    """The automation-only refusal reads steps too: real CI whose only
    test/build/install step sits in a `parallel:` group must still score."""
    flat = _wf([{"run": "echo hi"}, step], on={"push": None}, job_id="ci")
    assert pf_mod._automation_only(_parsed(flat), tmp_path) is False
    wrapped = _wrap(flat, 1, job_id="ci")
    assert pf_mod._automation_only(_parsed(wrapped), tmp_path) is False, label


_COMPOSITE_WITH_PARALLEL = (
    "runs:\n  using: composite\n  steps:\n    - parallel:\n"
    "        - uses: actions/setup-node@v4\n          with:\n            cache: pnpm\n"
    "        - run: echo hi\n          shell: bash\n")


def test_a_composite_action_parallel_group_is_read_defensively_and_disclosed(tmp_path):
    """GitHub's workflow syntax: "You cannot use `parallel` inside a composite
    action." The walk reads the children anyway (defensive: the same verdict
    as if the steps were written flat) and counts the group as one GitHub
    rejects, naming the file, so the report says so (disclosure test below)."""
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(_COMPOSITE_WITH_PARALLEL)
    (tmp_path / "package.json").write_text("{}\n")
    wf = _wf([{"uses": "./.github/actions/setup"}, {"run": "npm ci"}])
    f = pf_mod._practice_facts(_parsed(wf), tmp_path)["ci.cache.dependency-cache"]
    assert f["state"] == "pass", f["evidence"]
    stats = pf_mod._step_walk_stats(_parsed(wf), tmp_path)
    assert stats["invalid_groups"] == 1
    assert stats["invalid"] == [_entry(".github/actions/setup/action.yml", None, "1",
                                       "in_composite")]
    # Not a group GitHub runs side by side, so not in the side-by-side counts.
    assert stats["groups"] == 0 and stats["steps_in_groups"] == 0


def _entry(file: str, job: str | None, step: str | None, *reasons: str) -> dict:
    return {"file": file, "job": job, "step": step, "reasons": list(reasons)}


def test_a_step_with_parallel_and_run_reads_both_and_is_disclosed():
    """`parallel:` next to `run:`/`uses:` is not a documented shape. Never a
    silent skip: the step is read, its group's children are read too, and the
    group is counted as undocumented (not as one GitHub runs side by side)."""
    stats = pf_mod._new_step_stats()
    steps = [{"run": "a", "parallel": [{"uses": "some-org/deploy@main"}]}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml", job="test")
    assert [s.get("run") or s.get("uses") for s in leaves] == ["a", "some-org/deploy@main"]
    assert stats["invalid"] == [_entry("ci.yml", "test", "1", "beside_run_uses")]
    assert stats["groups"] == 0 and stats["steps_in_groups"] == 0


def test_a_step_with_run_beside_an_unreadable_parallel_counts_as_both():
    stats = pf_mod._new_step_stats()
    leaves = pf_mod._walk_steps([{"run": "npm ci", "parallel": "x"}], stats, "ci.yml", job="t")
    assert [s["run"] for s in leaves] == ["npm ci"]
    assert stats["invalid"] == [_entry("ci.yml", "t", "1", "beside_run_uses")]
    assert stats["malformed"] == [_entry("ci.yml", "t", "1", "not_a_list:string")]


def test_a_single_step_mapping_as_parallel_is_read_and_named_undocumented(tmp_path):
    """`parallel: {uses: ...}` is one step where GitHub documents a list. It
    is readable, so it is read (the same verdict as the step written flat)
    and counted as undocumented, not as unreadable."""
    wf = _wf([{"uses": f"actions/checkout@{SHA}"},
              {"parallel": {"uses": "some-org/deploy@main"}}])
    f = pf_mod._practice_facts(_parsed(wf), tmp_path)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail", f["evidence"]
    stats = pf_mod._step_walk_stats(_parsed(wf), tmp_path)
    assert stats["invalid"] == [_entry("ci.yml", "b", "2", "not_a_list:mapping")]
    assert stats["malformed_groups"] == 0


def test_non_step_entries_inside_a_group_are_counted_and_named():
    stats = pf_mod._new_step_stats()
    steps = [{"parallel": ["junk", None, [1], {"run": "a"}]}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml", job="t")
    assert [s["run"] for s in leaves] == ["a"]
    assert stats["skipped_children"] == 3
    assert stats["skipped"] == [_entry("ci.yml", "t", "1.1", "not_a_step:string"),
                                _entry("ci.yml", "t", "1.2", "null"),
                                _entry("ci.yml", "t", "1.3", "not_a_step:list")]


def test_a_nested_group_is_read_but_counted_undocumented():
    """GitHub documents `parallel:` on a job's own step list; a group inside a
    group is read defensively and counted as undocumented, and its steps stay
    out of the side-by-side counts."""
    stats = pf_mod._new_step_stats()
    steps = [{"parallel": [{"run": "a"}, {"parallel": [{"run": "b"}, {"run": "c"}]}]}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml", job="t")
    assert [s["run"] for s in leaves] == ["a", "b", "c"]
    assert stats["groups"] == 1 and stats["steps_in_groups"] == 1
    assert stats["invalid"] == [_entry("ci.yml", "t", "1.2", "nested")]


def test_in_composite_propagates_to_nested_groups():
    stats = pf_mod._new_step_stats()
    steps = [{"parallel": [{"parallel": [{"run": "a"}]}]}]
    pf_mod._walk_steps(steps, stats, "action.yml", in_composite=True)
    assert stats["invalid"] == [_entry("action.yml", None, "1", "in_composite"),
                                _entry("action.yml", None, "1.1", "in_composite", "nested")]


def test_a_run_step_with_wait_is_a_step_not_a_control_step():
    stats = pf_mod._new_step_stats()
    leaves = pf_mod._walk_steps([{"run": "a", "wait": "x"}], stats, "ci.yml")
    assert [s["run"] for s in leaves] == ["a"] and stats["control_steps"] == 0


def test_a_local_history_action_inside_a_parallel_group_exempts_a_deep_checkout(tmp_path):
    """The git-history exemption's local-composite-action arm: the decisive
    step is `uses: ./<local action that runs git>` sitting in a group."""
    act = tmp_path / ".github" / "actions" / "changed"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - run: git diff origin/main...HEAD\n"
        "      shell: bash\n")
    flat = _wf([CHECKOUT_DEEP, {"uses": "./.github/actions/changed"}])
    check = "ci.checkout.shallow-clone"
    assert pf_mod._practice_facts(_parsed(flat), tmp_path)[check]["state"] == "pass"
    got = pf_mod._practice_facts(_parsed(_wrap(flat, 1)), tmp_path)[check]
    assert got["state"] == "pass", got["evidence"]


def test_a_cyclic_parallel_group_is_disclosed_not_a_crash():
    """A YAML alias can make a group contain itself (`steps: &s [{parallel: *s}]`
    loads as a list that holds itself). The walk must terminate and count it
    as unreadable."""
    loaded = yaml.safe_load("steps: &s\n  - run: a\n  - parallel: *s\n")["steps"]
    stats = pf_mod._new_step_stats()
    leaves = pf_mod._walk_steps(loaded, stats, "ci.yml", job="b")
    assert [s["run"] for s in leaves] == ["a"]
    assert stats["malformed"] == [_entry("ci.yml", "b", "2", "cyclic")]
    facts = pf_mod._practice_facts([("ci.yml", {"on": PR_ON, "jobs": {"b": {"steps": loaded}}}, "")],
                                   Path("/nonexistent-root"))
    assert len(facts) == 11


def test_checks_that_read_no_steps_are_unaffected_by_parallel_groups(tmp_path):
    """Concurrency, cancel-superseded, path filters, job timeouts, OIDC scoping
    and test sharding read workflow/job structure, not steps — a parallel
    group must leave every one of them exactly as the flat workflow reads."""
    base = {
        "on": {"pull_request": {"paths": ["src/**"]}},
        "permissions": {"contents": "read"},
        "concurrency": {"group": "g", "cancel-in-progress": True},
        "jobs": {"test": {
            "timeout-minutes": 10,
            "permissions": {"id-token": "write"},
            "strategy": {"matrix": {"shard": [1, 2]}},
            "steps": [{"uses": "actions/checkout@v4"}, {"run": "pytest"}]}},
    }
    flat = pf_mod._practice_facts(_parsed(base), tmp_path)
    wrapped = pf_mod._practice_facts(_parsed(_wrap(base, 1, job_id="test")), tmp_path)
    for cid in ("ci.trigger.concurrency-groups", "ci.trigger.cancel-superseded",
                "ci.trigger.path-filter", "ci.hygiene.job-timeouts",
                "ci.security.scoped-id-token", "ci.parallel.test-sharding"):
        assert wrapped[cid]["state"] == flat[cid]["state"] == "pass", cid


CONTROL_STEPS = [{"wait": ["lint"]}, {"wait-all": None}, {"cancel": "lint"},
                 {"wait": None}, {"parallel": None}, {"parallel": "oops"},
                 {"parallel": {"run": "npm ci"}}, {"parallel": []}]


@pytest.mark.parametrize("step", CONTROL_STEPS, ids=[json.dumps(s) for s in CONTROL_STEPS])
def test_control_and_malformed_steps_never_crash_a_check(tmp_path, step):
    (tmp_path / "package.json").write_text("{}\n")
    wf = _wf([step], job_id="test")
    facts = pf_mod._practice_facts(_parsed(wf), tmp_path)
    assert len(facts) == 11
    assert all(f["state"] in ("pass", "fail", "not_applicable") for f in facts.values())
    pf_mod._automation_only(_parsed(wf), tmp_path)  # no raise


def test_walker_yields_leaves_in_declaration_order_and_counts_what_it_skipped():
    stats = pf_mod._new_step_stats()
    steps = [{"run": "a"},
             {"parallel": [{"run": "b"}, {"parallel": [{"run": "c"}]}, {"wait-all": None}]},
             {"wait": ["x"]}, {"cancel": "y"}, {"parallel": "not-a-list"},
             {"parallel": {"run": "e"}}, None, "junk", {"run": "d"}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml", job="t")
    assert [s["run"] for s in leaves] == ["a", "b", "c", "e", "d"]
    assert stats["groups"] == 1            # the outer group (documented shape)
    assert stats["steps_in_groups"] == 1   # b (c sits in the nested group)
    assert stats["control_steps"] == 3     # wait-all, wait, cancel
    assert stats["invalid"] == [_entry("ci.yml", "t", "2.2", "nested"),
                                _entry("ci.yml", "t", "6", "not_a_list:mapping")]
    assert stats["malformed"] == [_entry("ci.yml", "t", "5", "not_a_list:string")]
    assert stats["skipped_children"] == 0  # top-level junk is not inside a group


# --- disclosure: findings document + report header ---------------------------

def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args],
                         capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                              "HOME": str(root)})
    return out.stdout.strip()


def _repo(tmp_path: Path, workflow: str, extra: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    _git(root, "init", "-q")
    files = {".github/workflows/ci.yml": workflow, **(extra or {})}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
        _git(root, "add", rel)
    _git(root, "commit", "-qm", "init")
    return root


PARALLEL_WF = (
    "on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - parallel:\n"
    "          - run: npm ci\n"
    "          - uses: actions/cache@v4\n"
    "      - wait-all:\n"
    "      - run: npm test\n")

MALFORMED_WF = ("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - parallel: npm ci\n      - run: npm test\n")


def _render_and_verify(doc: dict) -> str:
    registry = json.loads(_SPEC_PATH.read_text())
    report = rr_mod.render_report(doc, registry)
    assert vr_mod.verify(doc, report, registry) == []
    return report


def _row(report: str) -> str:
    rows = [l for l in report.split("```", 1)[0].splitlines() if "**Parallel steps**" in l]
    assert len(rows) == 1, rows
    return rows[0]


def test_collector_records_and_report_discloses_steps_read_from_parallel_groups(tmp_path):
    doc, code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    assert code == 0
    rec = doc["data_sources"]["parallel_steps"]
    assert rec["groups"] == 1 and rec["steps_in_groups"] == 2
    assert rec["control_steps"] == 1 and rec["malformed_groups"] == 0
    assert doc["practice_facts"]["ci.cache.dependency-cache"]["state"] == "pass"
    assert _row(_render_and_verify(doc)) == (
        "| **Parallel steps** | 2 step(s) in 1 `parallel:` group(s) checked like any "
        "other step (GitHub runs a group's steps side by side, up to 10 at a time) · "
        "1 `wait`/`wait-all`/`cancel` step(s) skipped: they only wait for or cancel "
        "background steps and run no code of their own |")


def test_a_malformed_parallel_group_is_disclosed_never_silently_skipped(tmp_path):
    doc, code = cc_mod.collect(_repo(tmp_path, MALFORMED_WF))
    assert code == 0
    rec = doc["data_sources"]["parallel_steps"]
    assert rec["groups"] == 0 and rec["malformed_groups"] == 1
    assert rec["malformed"] == [_entry(".github/workflows/ci.yml", "test", "1",
                                       "not_a_list:string")]
    row = _row(_render_and_verify(doc))
    assert "0 step(s)" not in row   # no group was read, so no "read" clause
    assert row == ("| **Parallel steps** | **1 `parallel:` group(s) could not be read, "
                   "so their steps were not checked**: `.github/workflows/ci.yml` job "
                   "`test` step 1 (a string, not a list of steps) |")


def test_a_parallel_group_only_in_a_composite_action_is_disclosed(tmp_path):
    """No workflow uses the syntax; the only groups sit in local composite
    actions, where GitHub does not document them — one well-formed (read
    defensively) and one malformed (not readable). Still stamped, still in
    the header, still verified."""
    wf = ("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - uses: ./.github/actions/setup\n      - uses: ./.github/actions/bad\n")
    bad = "runs:\n  using: composite\n  steps:\n    - parallel: npm ci\n"
    doc, code = cc_mod.collect(_repo(tmp_path, wf, {
        ".github/actions/setup/action.yml": _COMPOSITE_WITH_PARALLEL,
        ".github/actions/bad/action.yml": bad}))
    assert code == 0
    rec = doc["data_sources"]["parallel_steps"]
    assert rec["groups"] == 0 and rec["steps_in_groups"] == 0
    assert rec["invalid"] == [
        _entry(".github/actions/bad/action.yml", None, "1", "in_composite"),
        _entry(".github/actions/setup/action.yml", None, "1", "in_composite")]
    assert rec["malformed"] == [_entry(".github/actions/bad/action.yml", None, "1",
                                       "not_a_list:string")]
    row = _row(_render_and_verify(doc))
    assert ("**2 `parallel:` group(s) in a shape GitHub does not document were read "
            "anyway**: `.github/actions/bad/action.yml` step 1 (inside a composite "
            "action), `.github/actions/setup/action.yml` step 1 (inside a composite "
            "action)") in row
    assert ("**1 `parallel:` group(s) could not be read, so their steps were not "
            "checked**: `.github/actions/bad/action.yml` step 1 (a string, not a list "
            "of steps) |") in row


def test_wait_or_cancel_without_any_parallel_group_adds_nothing(tmp_path):
    """`background: true` + `wait:` / `cancel:` with no `parallel:` group: the
    background steps are ordinary steps and already read, so the document and
    report stay exactly as before — no stamp, no row."""
    wf = ("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - id: srv\n        run: ./serve\n        background: true\n"
          "      - run: npm test\n      - wait: srv\n      - cancel: srv\n")
    doc, _code = cc_mod.collect(_repo(tmp_path, wf))
    assert "parallel_steps" not in doc["data_sources"]
    assert "Parallel steps" not in _render_and_verify(doc)


def _parallel_doc(tmp_path: Path) -> tuple[dict, dict, str]:
    doc, _code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    doc["data_sources"]["parallel_steps"].update(
        malformed_groups=1, malformed=[_entry(".github/workflows/x.yml", "test", "3", "null")])
    registry = json.loads(_SPEC_PATH.read_text())
    report = rr_mod.render_report(doc, registry)
    assert vr_mod.verify(doc, report, registry) == []
    return doc, registry, report


def _red(doc: dict, report: str, registry: dict) -> bool:
    return any(p.startswith("HEADER:") and "Parallel steps" in p
               for p in vr_mod.verify(doc, report, registry))


def test_verify_goes_red_when_the_parallel_disclosure_is_dropped(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    stripped = "\n".join(l for l in report.splitlines() if "**Parallel steps**" not in l)
    assert _red(doc, stripped, registry)


def test_verify_goes_red_when_only_the_unreadable_group_warning_is_cut(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    row = _row(report)
    cut = row.split(" · **", 1)[0] + " |"
    assert cut != row
    assert _red(doc, report.replace(row, cut), registry)


def test_verify_goes_red_on_a_wrong_count(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    row = _row(report)
    assert _red(doc, report.replace(row, row.replace("2 step(s)", "3 step(s)")), registry)


def test_verify_goes_red_when_the_row_leaves_the_provenance_header(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    row = _row(report)
    moved = report.replace(row + "\n", "") + "\n" + row + "\n"
    assert _red(doc, moved, registry)


def test_verify_goes_red_on_a_row_the_document_does_not_record(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    del doc["data_sources"]["parallel_steps"]
    assert _red(doc, report, registry)


def test_many_unreadable_groups_are_counted_never_cut_short(tmp_path):
    doc, registry, _report = _parallel_doc(tmp_path)
    entries = [_entry(f".github/workflows/w{i}.yml", "t", "2", "cyclic") for i in range(5)]
    doc["data_sources"]["parallel_steps"].update(malformed_groups=5, malformed=entries)
    row = _row(_render_and_verify(doc))
    assert row.endswith("`.github/workflows/w0.yml` job `t` step 2 (contains itself), "
                        "`.github/workflows/w1.yml` job `t` step 2 (contains itself), "
                        "`.github/workflows/w2.yml` job `t` step 2 (contains itself) "
                        "and 2 more |")


def test_a_pipe_in_a_name_cannot_break_the_table_row(tmp_path):
    doc, registry, _report = _parallel_doc(tmp_path)
    doc["data_sources"]["parallel_steps"]["malformed"] = [
        _entry(".github/workflows/a|b.yml", "j|k", "1", "null")]
    row = _row(_render_and_verify(doc))
    assert "`.github/workflows/a\\|b.yml` job `j\\|k`" in row
    assert row.replace("\\|", "").count("|") == 3   # label cell + value cell only


def test_a_backtick_fence_in_a_name_cannot_cut_the_header_short(tmp_path):
    """The verifier reads the header as everything before the first ```; a
    file name holding one must not end the header inside the row."""
    doc, registry, _report = _parallel_doc(tmp_path)
    doc["data_sources"]["parallel_steps"]["malformed"] = [
        _entry(".github/workflows/a```b.yml", "j`k", "1", "null")]
    report = _render_and_verify(doc)
    assert "```b" not in _row(report)


@pytest.mark.parametrize("key,value", [("groups", "1"), ("malformed_groups", None),
                                       ("control_steps", True), ("malformed", "x.yml")])
def test_verify_reports_a_mistyped_record_instead_of_crashing(tmp_path, key, value):
    doc, registry, report = _parallel_doc(tmp_path)
    doc["data_sources"]["parallel_steps"][key] = value
    problems = vr_mod.verify(doc, report, registry)
    assert any(p.startswith("HEADER:") and key in p for p in problems), problems


def test_verify_reports_a_count_that_disagrees_with_its_entries(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    doc["data_sources"]["parallel_steps"]["malformed_groups"] = 2
    problems = vr_mod.verify(doc, report, registry)
    assert any(p.startswith("HEADER:") and "malformed_groups" in p for p in problems), problems


def test_no_parallel_syntax_leaves_the_document_and_header_unchanged(tmp_path):
    flat = PARALLEL_WF.replace("      - parallel:\n          - run: npm ci\n"
                               "          - uses: actions/cache@v4\n      - wait-all:\n",
                               "      - run: npm ci\n      - uses: actions/cache@v4\n")
    assert "parallel" not in flat and "wait-all" not in flat
    doc, _code = cc_mod.collect(_repo(tmp_path, flat))
    assert "parallel_steps" not in doc["data_sources"]
    assert "Parallel steps" not in _render_and_verify(doc)


# --- bounded walk: depth cap + per-walk step budget ---------------------------

def _alias_chain(depth: int) -> str:
    """`a{depth}` is a `parallel:` nested `depth` levels deep through YAML
    aliases (PyYAML builds it without deep recursion)."""
    lines = ["a0: &a0\n  - run: git log\n"]
    lines += [f"a{i}: &a{i}\n  - parallel: *a{i - 1}\n" for i in range(1, depth + 1)]
    return "".join(lines)


def _doubling_chain(levels: int, bottom: str = "\n  - run: x\n") -> str:
    """Each level holds the level below twice: 2**levels entries in all."""
    lines = [f"l0: &l0{bottom}"]
    lines += [f"l{i}: &l{i}\n  - parallel: *l{i - 1}\n  - parallel: *l{i - 1}\n"
              for i in range(1, levels + 1)]
    return "".join(lines)


def test_a_deep_alias_chain_is_capped_and_counted_never_a_crash():
    """1,200 nested groups used to raise RecursionError out of the walk."""
    steps = yaml.safe_load(_alias_chain(1200))["a1200"]
    stats = pf_mod._new_step_stats()
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml")
    assert leaves == []
    assert stats["groups"] == 1   # the outer group
    # every deeper group is nested (undocumented); the one past the cap is
    # ALSO unreadable, so it lands in both buckets
    assert stats["invalid_groups"] == pf_mod._MAX_PARALLEL_DEPTH
    assert stats["malformed"] == [_entry("ci.yml", None, ".".join(["1"] * 65), "too_deep")]


def test_a_group_at_the_depth_cap_is_still_read():
    cap = getattr(pf_mod, "_MAX_PARALLEL_DEPTH", 64)
    steps = yaml.safe_load(_alias_chain(cap))[f"a{cap}"]
    stats = pf_mod._new_step_stats()
    assert [s["run"] for s in pf_mod._walk_steps(steps, stats, "ci.yml")] == ["git log"]
    assert stats["malformed_groups"] == 0


@pytest.mark.parametrize("bottom", ["\n  - run: x\n", " []\n"], ids=["leaves", "empty-groups"])
def test_an_alias_doubling_chain_stops_at_the_step_budget(bottom):
    """`lK: [{parallel: *lK-1}, {parallel: *lK-1}]` fans out to 2**K entries
    (K≈30 is ~1e9). The walk stops at its step budget and counts the group it
    stopped in as unreadable, whether the bottom holds steps or nothing."""
    steps = yaml.safe_load(_doubling_chain(16, bottom))["l16"]
    stats = pf_mod._new_step_stats()
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml")
    assert len(leaves) <= getattr(pf_mod, "_MAX_WALK_STEPS", 10_000)
    assert stats["malformed_groups"] == 1
    assert stats["malformed"][0]["reasons"] == ["over_budget"]


def _wf_text(steps_yaml_anchor_block: str, ref: str) -> str:
    return ("x-anchors:\n" + "".join("  " + l + "\n" for l in steps_yaml_anchor_block.splitlines())
            + "on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            f"    steps: *{ref}\n")


@pytest.mark.parametrize("anchors,ref", [
    (_alias_chain(1200), "a1200"),
    (_doubling_chain(16), "l16"),
    ("s: &s\n  - run: npm test\n  - parallel:\n      - parallel: *s\n", "s"),
], ids=["deep-chain", "doubling-chain", "two-hop-cycle"])
def test_a_pathological_group_is_scored_and_counted_end_to_end(tmp_path, anchors, ref):
    doc, code = cc_mod.collect(_repo(tmp_path, _wf_text(anchors, ref)))
    assert code == 0, doc["data_sources"].get("ci_score_error")
    assert doc["data_sources"]["parallel_steps"]["malformed_groups"] == 1
    _render_and_verify(doc)


def test_a_two_hop_cycle_is_counted_once_at_walker_level():
    steps = yaml.safe_load("s: &s\n  - run: a\n  - parallel:\n      - parallel: *s\n")["s"]
    stats = pf_mod._new_step_stats()
    assert [s["run"] for s in pf_mod._walk_steps(steps, stats, "ci.yml")] == ["a"]
    assert stats["groups"] == 1
    assert stats["malformed"] == [_entry("ci.yml", None, "2.1", "cyclic")]


@pytest.mark.parametrize("exc", [RecursionError, MemoryError])
def test_a_walk_failure_is_a_scoring_error_marker_never_a_traceback(tmp_path, monkeypatch, exc):
    real = cc_mod._load_sibling

    def boom(*_a, **_k):
        raise exc("walk blew up")

    def load(mod_name, filename):
        mod = real(mod_name, filename)
        if filename == "practice_facts.py":
            monkeypatch.setattr(mod, "_practice_facts", boom)
        return mod

    monkeypatch.setattr(cc_mod, "_load_sibling", load)
    doc, code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    assert code == 3
    assert doc["data_sources"]["ci_score_error"] == f"{exc.__name__}: walk blew up"
    assert "ci_score" not in doc


# --- step lists that cannot be read at all: recorded like workflow parse errors

def test_a_step_list_that_is_not_a_list_is_recorded_by_name(tmp_path):
    wf = ("on:\n  pull_request:\njobs:\n  a:\n    runs-on: x\n    steps:\n      run: npm ci\n"
          "  b:\n    runs-on: x\n    steps: npm test\n  c:\n    uses: ./.github/workflows/r.yml\n")
    doc, code = cc_mod.collect(_repo(tmp_path, wf, {
        ".github/actions/s/action.yml": "runs:\n  using: composite\n  steps: echo\n"}))
    assert code == 0
    assert doc["data_sources"]["unreadable_step_lists"] == [
        ".github/workflows/ci.yml: jobs.a.steps", ".github/workflows/ci.yml: jobs.b.steps",
        ".github/actions/s/action.yml: runs.steps"]


def test_a_composite_action_that_fails_to_parse_is_recorded_by_name(tmp_path):
    doc, code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF, {
        ".github/actions/bad/action.yml": "runs: [unclosed\n",
        ".github/actions/list/action.yaml": "- not a mapping\n"}))
    assert code == 0
    assert doc["data_sources"]["composite_parse_errors"] == [
        ".github/actions/bad/action.yml", ".github/actions/list/action.yaml"]


def test_readable_step_lists_and_composites_add_no_record(tmp_path):
    doc, _code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF, {
        ".github/actions/ok/action.yml": "runs:\n  using: composite\n  steps: []\n"}))
    assert "unreadable_step_lists" not in doc["data_sources"]
    assert "composite_parse_errors" not in doc["data_sources"]


# --- coverage pins (test review) ----------------------------------------------

def test_no_step_list_is_read_outside_the_walker():
    """Every `X.get("steps")` / `X["steps"]` in the skill's scripts is an
    argument of `_walk_steps(...)`, or sits in `_unreadable_step_sources`,
    which only checks the list's TYPE. A new flat read of a step list (the bug
    class this change fixed) fails here."""
    import ast
    offenders = []
    for path in sorted((_SKILL_DIR / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def is_steps_read(n) -> bool:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "get" and n.args \
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "steps":
                return True
            return (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "steps")

        for node in ast.walk(tree):
            if not is_steps_read(node):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) \
                    and parent.func.id == "_walk_steps":
                continue
            fn = node
            while fn is not None and not isinstance(fn, ast.FunctionDef):
                fn = parents.get(fn)
            if fn is not None and fn.name == "_unreadable_step_sources":
                continue
            offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"step list read outside the walker: {offenders}"


def _n_entries(n: int) -> list[dict]:
    return [_entry(f".github/workflows/w{i}.yml", "t", "2", "cyclic") for i in range(n)]


@pytest.mark.parametrize("n,tail", [(3, "`.github/workflows/w2.yml` job `t` step 2 (contains itself) |"),
                                    (4, "(contains itself) and 1 more |")])
def test_the_and_more_tail_starts_after_exactly_three_names(tmp_path, n, tail):
    doc, _registry, _report = _parallel_doc(tmp_path)
    doc["data_sources"]["parallel_steps"].update(malformed_groups=n, malformed=_n_entries(n))
    row = _row(_render_and_verify(doc))
    assert row.endswith(tail), row
    assert ("more" in row) is (n > 3)


def test_verify_goes_red_on_a_duplicated_row(tmp_path):
    doc, registry, report = _parallel_doc(tmp_path)
    row = _row(report)
    assert _red(doc, report.replace(row, row + "\n" + row), registry)


def test_a_run_and_parallel_step_inside_a_group_counts_once_as_a_side_by_side_step():
    stats = pf_mod._new_step_stats()
    steps = [{"parallel": [{"run": "a", "parallel": [{"run": "b"}]}]}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml", job="t")
    assert [s["run"] for s in leaves] == ["a", "b"]
    assert stats["groups"] == 1 and stats["steps_in_groups"] == 1   # a, not b
    assert stats["invalid"] == [_entry("ci.yml", "t", "1.1", "nested", "beside_run_uses")]


def test_wait_or_cancel_without_a_group_reads_every_check_as_the_flat_workflow(tmp_path):
    """No group: the `wait:` / `cancel:` steps run no code, so every check
    reads exactly what the same workflow without them reads."""
    def facts(steps):
        return pf_mod._practice_facts(_parsed(_wf(steps, job_id="test")), tmp_path)
    (tmp_path / "package.json").write_text("{}\n")
    base = [{"id": "srv", "run": "./serve", "background": True},
            {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}, {"run": "npm ci"}]
    assert facts(base[:1] + [{"wait": "srv"}] + base[1:] + [{"cancel": "srv"}]) == facts(base)


_AUTOMATION_WF = ("on:\n  push:\njobs:\n  triage:\n    runs-on: ubuntu-latest\n    steps:\n"
                  "      - parallel:\n          - run: echo hi\n"
                  "          - uses: actions/labeler@v5\n")


def test_an_automation_only_refusal_report_carries_the_verified_row(tmp_path):
    doc, code = cc_mod.collect(_repo(tmp_path, _AUTOMATION_WF))
    assert code == 0 and doc["ci_score"].get("refusal"), doc.get("ci_score")
    assert "Parallel steps" in _row(_render_and_verify(doc))


def test_a_scoring_error_report_carries_the_verified_row(tmp_path, monkeypatch):
    real = cc_mod._load_sibling

    def load(mod_name, filename):
        mod = real(mod_name, filename)
        if filename == "ci_score.py":
            monkeypatch.setattr(mod, "compute_ci_score",
                                lambda *_a: (_ for _ in ()).throw(ValueError("boom")))
        return mod

    monkeypatch.setattr(cc_mod, "_load_sibling", load)
    doc, code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    assert code == 3 and "parallel_steps" in doc["data_sources"]
    report = _render_and_verify(doc)
    assert "```" not in report or "Parallel steps" in report.split("```", 1)[0]
    _row(report)
