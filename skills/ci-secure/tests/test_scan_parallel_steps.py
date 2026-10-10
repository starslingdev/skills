"""Steps inside a `parallel:` group are scanned like any other step.

GitHub Actions lets a job run steps concurrently: a step may be written as
`- parallel:` followed by a LIST of ordinary child steps, and a job may carry
pure control steps (`wait:`, `wait-all:`, `cancel:`) that hold no `run:` and
no `uses:`. The `parallel:` entry itself has neither key either, so a detector
that walks `job.steps` as a flat list and reads `step.get("run")` /
`step.get("uses")` skips every child — template injection, curl|bash, cache
poisoning and the rest, all unscanned, and the report reads clean.

The oracle here is the existing fixture corpus itself: every fixture is
scanned as written AND with each job's steps wrapped in a `- parallel:` group,
and the two scans must report the same findings at the same (shifted) lines,
except fixtures listed in `_RACE_ONLY_WHEN_WRAPPED`, whose steps really do
race once they share a group. No new workflow text is invented for the attack
vectors; the shapes are the fixtures' own.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _TESTS_DIR)
try:
    from _scan_import import load_scan, load_script  # noqa: E402
finally:
    try:
        sys.path.remove(_TESTS_DIR)
    except ValueError:                          # pragma: no cover - defensive
        pass

scan = load_scan()
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
_CLOAKED = _SKILL_DIR / "tests" / "fixtures" / "dot-github" / "workflows"

_STEPS_KEY_RE = re.compile(r"^(\s*)steps:\s*(?:#.*)?$")


def _wrap_steps_in_parallel(text: str) -> tuple[str, list[int]]:
    """Wrap every `steps:` list in one `- parallel:` group.

    Returns the new text and the ORIGINAL 1-based line numbers of each
    `steps:` key a group line was inserted after, so a finding's line can be
    mapped from the original file to the wrapped one.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted_after: list[int] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _STEPS_KEY_RE.match(line.rstrip("\n"))
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        block: list[str] = []
        while j < len(lines):
            cur = lines[j]
            stripped = cur.strip()
            cur_indent = len(cur) - len(cur.lstrip(" "))
            if not stripped or stripped.startswith("#") or cur_indent > indent \
                    or (cur_indent == indent and stripped.startswith("- ")):
                block.append(cur)
                j += 1
                continue
            break
        while block and (not block[-1].strip() or block[-1].strip().startswith("#")):
            block.pop()
            j -= 1
        items = [b for b in block if b.strip().startswith("- ")]
        if not items:
            i += 1
            continue
        item_indent = len(items[0]) - len(items[0].lstrip(" "))
        out.append(" " * item_indent + "- parallel:\n")
        inserted_after.append(i + 1)
        out.extend(("    " + b) if b.strip() else b for b in block)
        i = j
    return "".join(out), inserted_after


def _shift(line: int, inserted_after: list[int]) -> int:
    return line + sum(1 for at in inserted_after if at < line)


def _scan_root(root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root),
         "--gh-impostor", "off"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _write(root: Path, name: str, text: str) -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(text, encoding="utf-8")


def _signature(findings: list[dict], inserted_after: list[int] | None) -> list:
    return sorted(
        (f["pattern"],
         _shift(f["line"], inserted_after) if inserted_after is not None
         and isinstance(f.get("line"), int) else f.get("line"),
         tuple(sorted(f.get("affected_jobs") or [])))
        for f in findings
    )


# Positive fixtures for each step-level vector (P14.11 is network-gated and
# covered below through its pin collector; P14.18 is a document-level
# permission fact with no step in it).
_POSITIVE_FIXTURES = [
    ("p14_10_template_injection.yml.fixture", "P14.10"),
    ("p14_10_new_sinks.yml.fixture", "P14.10"),
    ("p14_10_line_attribution.yml.fixture", "P14.10"),
    ("p14_24_curl_pipe_bash.yml.fixture", "P14.24"),
    ("p14_24_mutable_fetch_exec.yml.fixture", "P14.24"),
    ("p14_14_context_dump.yml.fixture", "P14.14"),
    ("p14_15_env_path_inject.yml.fixture", "P14.15"),
    ("p14_7_pr_target_writes_cache.yml.fixture", "P14.7"),
    ("p14_7_pr_target_writes_cache.yml.fixture", "P14.9"),
    ("p14_19_credential_in_cache_path.yml.fixture", "P14.19"),
    ("p14_25_install_scripts_privileged.yml.fixture", "P14.25"),
]


@pytest.mark.parametrize("fixture,pattern", _POSITIVE_FIXTURES)
def test_vector_fires_the_same_inside_a_parallel_group(
    tmp_path: Path, fixture: str, pattern: str,
) -> None:
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    assert inserted_after, f"{fixture}: the transform wrapped nothing"
    assert "- parallel:" in wrapped

    plain_root, wrapped_root = tmp_path / "plain", tmp_path / "wrapped"
    name = fixture.removesuffix(".fixture")
    _write(plain_root, name, text)
    _write(wrapped_root, name, wrapped)
    plain = _scan_root(plain_root)
    inside = _scan_root(wrapped_root)

    want = [s for s in _signature(plain["findings"], inserted_after)
            if s[0] == pattern]
    got = [s for s in _signature(inside["findings"], None) if s[0] == pattern]
    assert want, f"{fixture}: the unwrapped fixture no longer fires {pattern}"
    assert got == want, (
        f"{pattern} inside a parallel: group: expected {want}, got {got}")
    # No new gap invented by the wrapping, and none lost.
    assert inside["dropped_matches"] == plain["dropped_matches"]


@pytest.mark.parametrize(
    "fixture", sorted(p.name for p in _CLOAKED.glob("*.yml.fixture")))
def test_whole_corpus_reports_identically_when_wrapped(
    tmp_path: Path, fixture: str,
) -> None:
    """Every fixture, positive and negative: wrapping must never lose a
    finding, and may add one only where the wrap really changes what runs
    first — every such fixture is listed in `_RACE_ONLY_WHEN_WRAPPED`."""
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    if not inserted_after:
        pytest.skip(f"{fixture} has no step list to wrap")
    name = fixture.removesuffix(".fixture")
    _write(tmp_path / "plain", name, text)
    _write(tmp_path / "wrapped", name, wrapped)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "wrapped")
    want = _signature(plain["findings"], inserted_after)
    got = _signature(inside["findings"], None)
    extra = list(got)
    for sig in want:
        assert sig in extra, f"{fixture}: {sig} lost inside a parallel: group"
        extra.remove(sig)
    assert sorted(extra) == _RACE_ONLY_WHEN_WRAPPED.get(fixture, []), extra
    # Wrapping must not invent or lose a disclosure either: a gap the flat
    # scan records, or a suppressed match, must read the same inside a group.
    assert inside["dropped_matches"] == plain["dropped_matches"]
    assert inside["coverage_notes"] == plain["coverage_notes"]


# Fixtures whose steps, once they all start together, really do race: the
# extra finding is the scanner reading concurrency, not a wrapping artifact.
# Each extra is pinned by its full signature (pattern, line in the wrapped
# file, jobs), so a different extra of the same pattern still fails.
_RACE_ONLY_WHEN_WRAPPED = {
    # The clone is pinned to a full commit id in step 1 before step 2 runs it.
    # As siblings, `python3 tools/setup.py` may run before the pin lands.
    "p14_24_negative_sha_pinned_fetch.yml.fixture": [
        ("P14.24", 23, ("build",))],
}


# The corpus wrap never reaches P14.25's mitigation arm on its own: no fixture
# repository pins pnpm >= 10. This variant writes that pin beside every
# P14.25 fixture, plus the builds-disabled workflow below, so a race is caught
# in both directions: a lost finding fails, and so does an unlisted new one.
_RACE_ONLY_WHEN_WRAPPED_ON_PNPM10 = {
    # Disable and install start together: the disable protects nothing.
    "vite-builds-disabled": [("P14.25", 13, ("publish",))],
}


@pytest.mark.parametrize("fixture", sorted(
    [p.name for p in _CLOAKED.glob("p14_25_*.yml.fixture")]
    + ["vite-builds-disabled"]))
def test_p14_25_corpus_reports_identically_when_wrapped_on_a_pnpm10_repo(
    tmp_path: Path, fixture: str,
) -> None:
    text = (textwrap.dedent(_P14_25_VITE) if fixture == "vite-builds-disabled"
            else (_CLOAKED / fixture).read_text(encoding="utf-8"))
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    if not inserted_after:
        pytest.skip(f"{fixture} has no step list to wrap")
    name = fixture.removesuffix(".fixture").removesuffix(".yml") + ".yml"
    _write(_pnpm10(tmp_path / "plain"), name, text)
    _write(_pnpm10(tmp_path / "wrapped"), name, wrapped)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "wrapped")
    want = _signature(plain["findings"], inserted_after)
    extra = _signature(inside["findings"], None)
    for sig in want:
        assert sig in extra, f"{fixture}: {sig} lost inside a parallel: group"
        extra.remove(sig)
    assert sorted(extra) == \
        _RACE_ONLY_WHEN_WRAPPED_ON_PNPM10.get(fixture, []), extra


def test_wrapped_scan_discloses_the_parallel_steps_it_read(tmp_path: Path) -> None:
    """Never silent: the scan says how many steps it read inside parallel:
    groups, so a reader can tell the new syntax was understood."""
    text = (_CLOAKED / "p14_10_template_injection.yml.fixture").read_text()
    wrapped, _ = _wrap_steps_in_parallel(text)
    _write(tmp_path, "a.yml", wrapped)
    data = _scan_root(tmp_path)
    stats = data.get("parallel_steps")
    assert stats is not None, "findings JSON carries no parallel_steps record"
    import yaml
    n_children = sum(
        s.in_parallel_group
        for job in yaml.safe_load(wrapped)["jobs"].values()
        for s in scan._iter_job_steps(job))
    assert n_children
    assert stats["steps_scanned"] == n_children
    assert stats["workflows"] == [".github/workflows/a.yml"]

    report = load_script("ci_secure_report", "report.py")
    md = report.render(data)
    assert f"{n_children} step(s) inside `parallel:` groups scanned" in md


_CONTROL_STEPS = textwrap.dedent("""\
    on: pull_request_target
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - id: a
            run: echo one
            background: true
          - wait: a
          - wait: [a]
          - wait-all:
          - cancel: a
          - parallel:
              - run: echo two
              - wait: a
    """)


def test_control_steps_never_crash_a_detector_and_are_counted(tmp_path: Path) -> None:
    _write(tmp_path, "ctl.yml", _CONTROL_STEPS)
    data = _scan_root(tmp_path)
    assert data["scan_incomplete"] == []
    stats = data["parallel_steps"]
    assert stats["steps_scanned"] == 1          # `echo two`
    assert stats["control_steps"] == 5          # 4 top-level + 1 nested
    assert stats["background_steps"] == 1


def test_walker_yields_leaves_in_declaration_order_with_tags() -> None:
    job = {"steps": [
        {"run": "one", "background": True},
        {"wait": "a"},
        {"parallel": [{"run": "two"}, {"uses": "x/y@v1"},
                      {"parallel": [{"run": "deep"}]}]},
        {"run": "three"},
    ]}
    leaves = list(scan._iter_job_steps(job))
    assert [(s.step.get("run") or s.step.get("uses")) for s in leaves] == \
        ["one", "two", "x/y@v1", "deep", "three"]
    assert [s.in_parallel_group for s in leaves] == \
        [False, True, True, True, False]
    assert [s.background for s in leaves] == [True, False, False, False, False]
    assert leaves[3].path == (2, 2, 0)


def test_malformed_parallel_is_disclosed_not_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "bad.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  run: echo not-a-list
        """))
    data = _scan_root(tmp_path)
    reasons = [e["reason"] for e in data["coverage_notes"]
               if e["workflow_file"].endswith("bad.yml")]
    assert any("parallel:" in r and "not read as steps" in r
               for r in reasons), reasons

    # The banner headline must say what happened to this step: it was NOT
    # scanned. The headline written for steps that WERE read ("carry a value
    # this scan cannot know") would contradict the bullet underneath it.
    report = load_script("ci_secure_report", "report.py")
    md = report.render(data)
    assert "carry a value this scan cannot know" not in md, md
    assert "1 `parallel:` / background note(s) in 1 workflow(s)" in md, md


def test_a_background_expression_note_is_not_headlined_as_a_rejected_shape(
    tmp_path: Path,
) -> None:
    """A run-time `background:` value is valid syntax: the banner must not
    say it is "not written in a shape GitHub accepts"."""
    _write(tmp_path, "bg.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: make
                background: ${{ inputs.bg }}
        """))
    data = _scan_root(tmp_path)
    assert [e["scope"] for e in data["coverage_notes"]] == ["parallel-group"]
    md = load_script("ci_secure_report", "report.py").render(data)
    assert "shape GitHub accepts" not in md, md
    assert "1 `parallel:` / background note(s) in 1 workflow(s)" in md, md


def _give_each_group_a_run(wrapped: str) -> str:
    """Turn every `- parallel:` entry into one that ALSO carries `run:`.

    GitHub does not accept that shape. The command added is the inert `make`
    already used in this file, so the only findings are the fixture's own."""
    return re.sub(r"^(\s*)- parallel:$",
                  lambda m: f"{m.group(1)}- run: make\n{m.group(1)}  parallel:",
                  wrapped, flags=re.MULTILINE)


def test_a_parallel_group_that_also_carries_run_is_scanned_and_disclosed(
    tmp_path: Path,
) -> None:
    """A `parallel:` entry that ALSO has `run:` (or `uses:`) has its own
    command scanned AND its children scanned at their own lines, and the
    shape surfaces as a coverage note, so the report is never clean over it."""
    fixture = "p14_10_template_injection.yml.fixture"
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    mixed = _give_each_group_a_run(wrapped)
    assert mixed.count("- run: make") == len(inserted_after) > 0
    name = fixture.removesuffix(".fixture")
    _write(tmp_path / "plain", name, text)
    _write(tmp_path / "mixed", name, mixed)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "mixed")
    # Two lines inserted per group (`- run: make` + `parallel:`).
    shifted_twice = sorted(inserted_after + inserted_after)
    want = [s for s in _signature(plain["findings"], shifted_twice)
            if s[0] == "P14.10"]
    got = [s for s in _signature(inside["findings"], None) if s[0] == "P14.10"]
    assert want and got == want, (want, got)
    notes = [e["reason"] for e in inside["coverage_notes"]]
    assert len(notes) == len(inserted_after), notes
    assert all("also carries `run:`" in r for r in notes), notes


def _node_get(node, key: str):
    """A scalar child of a composed mapping node, through `<<:` merges."""
    for k, v in node.value:
        if k.value == key:
            return v.value
    for k, v in node.value:
        if k.value == "<<":
            return _node_get(v, key)
    return None


def _lockstep_shapes() -> list:
    deep: list = [{"run": "deepest"}]
    for _ in range(scan._WALK_MAX_DEPTH + 2):
        deep = [{"parallel": deep}]
    return [
        [{"run": "own", "parallel": [{"run": "child"}, {"uses": "x/y@v1"}]}],
        [{"uses": "x/y@v1", "parallel": "not-a-list"}, {"run": "after"}],
        [{"parallel": [[{"run": "in-a-list"}], "run: text", {"run": "ok"}]},
         {"run": "after"}],
        _nested(scan._WALK_MAX_DEPTH),
        _nested(scan._WALK_MAX_DEPTH + 1),
        deep,
    ]


def _nested(groups: int) -> list:
    """A leaf step inside `groups` nested `parallel:` groups."""
    steps: list = [{"run": "leaf"}]
    for _ in range(groups):
        steps = [{"parallel": steps}]
    return steps


@pytest.mark.parametrize("groups,read", [(64, True), (65, False)])
def test_the_depth_cap_boundary_is_the_same_for_both_walkers(
    groups: int, read: bool,
) -> None:
    """The cap is `_WALK_MAX_DEPTH` groups: a leaf 64 groups down is read,
    one 65 down is a too-deep note. (The lockstep test pins the node walker
    to the same boundary.)"""
    assert scan._WALK_MAX_DEPTH == 64
    stats = scan._StepWalkStats()
    leaves = [s.step["run"] for s in scan._iter_job_steps(
        {"steps": _nested(groups)}, stats)]
    assert leaves == (["leaf"] if read else [])
    assert stats.malformed == (None if read else
                               [("." .join(["1"] * groups),
                                 scan._GROUP_TOO_DEEP)])


def test_a_group_past_the_depth_cap_is_a_parallel_group_coverage_note(
    tmp_path: Path,
) -> None:
    import yaml
    _write(tmp_path, "deep.yml", yaml.safe_dump(
        {"on": "push", "jobs": {"build": {"runs-on": "ubuntu-latest",
                                          "steps": _nested(65)}}}))
    data = _scan_root(tmp_path)
    notes = data["coverage_notes"]
    assert [e["scope"] for e in notes] == ["parallel-group"], notes
    assert "more than 64 groups deep" in notes[0]["reason"], notes


def test_a_coverage_note_names_a_child_as_step_group_dot_child(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: make
              - parallel:
                  - run: echo one
                  - - run: echo nested-list
        """))
    reasons = [e["reason"] for e in _scan_root(tmp_path)["coverage_notes"]]
    assert len(reasons) == 1, reasons
    assert reasons[0].startswith("jobs.build step 2.2 is an entry of a "
                                 "`parallel:` group"), reasons


def test_merge_keys_resolve_the_same_way_in_both_walkers() -> None:
    """Own keys win over a merge, and in `<<: [*a, *b]` the earlier source
    wins: both walkers must agree, or every later line shifts."""
    import yaml
    text = textwrap.dedent("""\
        x-a: &a
          parallel:
            - run: from-a
        x-b: &b
          parallel:
            - run: from-b
        x-r: &r
          run: merged
        jobs:
          j:
            steps:
              - <<: *r
                run: own
              - <<: [*a, *b]
        """)
    leaves = [s.step["run"] for s in scan._iter_job_steps(
        yaml.safe_load(text)["jobs"]["j"])]
    assert leaves == ["own", "from-a"]
    steps_node = scan._node_keys(scan._job_nodes(text)["j"])["steps"]
    assert [scan._node_keys(n)["run"].value
            for n in scan._iter_step_nodes(steps_node)] == leaves


@pytest.mark.parametrize("steps", _lockstep_shapes())
def test_node_walker_stays_in_lockstep_with_the_step_walker(steps) -> None:
    """Line attribution pairs the two walkers' outputs by position, so they
    must yield the same leaves in the same order — for every shape, including
    one nested past the depth cap and one carrying `run:` beside `parallel:`."""
    import yaml
    text = yaml.safe_dump({"jobs": {"j": {"steps": steps}}})
    leaves = [s.step for s in scan._iter_job_steps(
        yaml.safe_load(text)["jobs"]["j"])]
    node = yaml.compose(text)
    jobs = dict((k.value, v) for k, v in node.value)["jobs"]
    steps_node = dict((k.value, v) for k, v in
                      dict((k.value, v) for k, v in jobs.value)["j"].value)["steps"]
    nodes = list(scan._iter_step_nodes(steps_node))
    assert len(nodes) == len(leaves), (len(nodes), len(leaves))
    assert [_node_get(n, "run") or _node_get(n, "uses")
            for n in nodes] == \
        [s.get("run") or s.get("uses") for s in leaves]


def test_node_walker_follows_a_merge_key_holding_a_group() -> None:
    """`- <<: *grp` resolves to a `parallel:` group when loaded, so the node
    walker must see the same group, or every later step's line shifts."""
    import yaml
    text = textwrap.dedent("""\
        x-grp: &grp
          parallel:
            - run: one
            - run: two
        jobs:
          j:
            steps:
              - <<: *grp
              - run: three
        """)
    leaves = [s.step["run"] for s in scan._iter_job_steps(
        yaml.safe_load(text)["jobs"]["j"])]
    assert leaves == ["one", "two", "three"]
    root = yaml.compose(text)
    jobs = dict((k.value, v) for k, v in root.value)["jobs"]
    steps_node = dict((k.value, v) for k, v in
                      dict((k.value, v) for k, v in jobs.value)["j"].value)["steps"]
    assert [_node_get(n, "run")
            for n in scan._iter_step_nodes(steps_node)] == leaves


def test_impostor_pin_collector_sees_a_pin_inside_a_parallel_group(
    tmp_path: Path,
) -> None:
    """P14.11 is network-gated; what it can check is what the pin collector
    hands it, so the collector is the thing that must see the child step."""
    _write(tmp_path, "ci.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  - uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608
                  - uses: example-org/some-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        """))
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    assert [(line, repo) for _, line, repo, _ in pins] == [
        (7, "actions/checkout"), (8, "example-org/some-action")]


def test_config_facts_read_checkouts_inside_a_parallel_group() -> None:
    facts = load_script("ci_secure_config_facts", "config_facts.py")
    doc = {
        "on": "pull_request_target",
        "jobs": {"t": {"steps": [{"parallel": [
            {"uses": "actions/checkout@v4",
             "with": {"ref": "${{ github.event.pull_request.head.sha }}"}},
            {"run": "make"},
        ]}]}},
    }
    assert facts._unpersisted_checkout_violations(doc) == ["t"]
    assert facts._jobs_checking_out_attacker_head(scan, doc) == ["t"]


def test_a_group_past_the_depth_cap_is_not_called_a_non_list() -> None:
    """A valid list nested too deep is a coverage gap, but saying its value
    "is not a list of steps" would be false."""
    stats = scan._StepWalkStats()
    job = {"steps": _lockstep_shapes()[-1]}
    assert list(scan._iter_job_steps(job, stats)) == []
    assert stats.malformed and stats.malformed[0][1] == scan._GROUP_TOO_DEEP
    sentence = scan._GROUP_GAP_SENTENCE[scan._GROUP_TOO_DEEP].format(
        depth=scan._WALK_MAX_DEPTH)
    assert "not a list" not in sentence
    assert f"more than {scan._WALK_MAX_DEPTH} groups deep" in sentence


def test_parallel_row_never_opens_with_a_zero_and_names_background_steps() -> None:
    report = load_script("ci_secure_report", "report.py")
    control_only = report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 1, "background_steps": 0})
    assert control_only and not control_only.startswith("0 "), control_only
    background_only = report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 0, "background_steps": 2})
    assert background_only == "2 `background: true` step(s) scanned"
    assert report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 0, "background_steps": 0}) == ""


# ---------------------------------------------------------------------------
# Declaration order is not execution order.
#
# Siblings in one `parallel:` group run at the same time, and a
# `background: true` step keeps running past the steps declared after it. The
# three detectors that reason about ORDER (P14.9 "checkout, then execute",
# P14.24 "fetch, then execute", P14.25 "builds disabled, then install") must
# treat such steps as possibly concurrent and over-report rather than miss.
# Every input below is an existing fixture (or an existing test's workflow
# text) with its steps reordered, grouped or backgrounded; no new workflow
# content is invented.
# ---------------------------------------------------------------------------

def _split_steps(text: str) -> tuple[list[str], list[list[str]], list[str], int]:
    """(lines up to `steps:`, the step items as line lists, the rest, item
    indent) for a workflow with ONE job."""
    lines = textwrap.dedent(text).splitlines(keepends=True)
    at = next(i for i, ln in enumerate(lines)
              if _STEPS_KEY_RE.match(ln.rstrip("\n")))
    body = lines[at + 1:]
    indent = next(len(ln) - len(ln.lstrip(" ")) for ln in body
                  if ln.strip().startswith("- "))
    items: list[list[str]] = []
    end = len(body)
    for k, ln in enumerate(body):
        cur = len(ln) - len(ln.lstrip(" "))
        if ln.strip() and cur < indent:
            end = k
            break
        if ln.strip().startswith("- ") and cur == indent:
            items.append([ln])
        elif items:
            items[-1].append(ln)
    return lines[:at + 1], items, body[end:], indent


def _grouped(items: list[list[str]], indent: int) -> list[str]:
    out = [" " * indent + "- parallel:\n"]
    for item in items:
        out.extend(("    " + ln) if ln.strip() else ln for ln in item)
    return out


def _backgrounded(item: list[str], indent: int, step_id: str | None = None,
                  ) -> list[str]:
    extra = [" " * (indent + 2) + "background: true\n"]
    if step_id is not None:
        extra.append(" " * (indent + 2) + f"id: {step_id}\n")
    return [item[0], *extra, *item[1:]]


def _joined(*parts: list[str]) -> str:
    return "".join(ln for part in parts for ln in part)


def _patterns(root: Path, text: str, pattern: str) -> list[dict]:
    _write(root, "a.yml", text)
    data = _scan_root(root)
    # An empty answer must be the detector's, not a file that never parsed.
    assert data["scan_incomplete"] == [], data["scan_incomplete"]
    return [f for f in data["findings"] if f["pattern"] == pattern]


_P14_9 = (_CLOAKED / "p14_7_pr_target_writes_cache.yml.fixture").read_text()


def test_p14_9_a_sibling_declared_before_the_head_checkout_still_runs_it(
    tmp_path: Path,
) -> None:
    """The run steps share the checkout's group but are written above it: in a
    group they all start together, so they can execute the fork's tree."""
    head, items, tail, ind = _split_steps(_P14_9)
    checkout, setup, *runs = items
    # Negative control: the same reordering WITHOUT a group is ordinary
    # sequential order, the runs finish before the checkout lands.
    flat = _joined(head, *runs, checkout, setup, tail)
    assert _patterns(tmp_path / "flat", flat, "P14.9") == []
    grouped = _joined(head, _grouped([*runs, checkout, setup], ind), tail)
    hits = _patterns(tmp_path / "grp", grouped, "P14.9")
    assert len(hits) == 1
    # The evidence must not claim the run came AFTER the checkout: it is
    # written above it, and runs alongside it.
    assert "a step that may run after or alongside it executes from the " \
        "tree" in hits[0]["evidence"], hits[0]["evidence"]


def test_p14_9_a_background_step_started_before_the_checkout_still_runs(
    tmp_path: Path,
) -> None:
    head, (checkout, setup, first_run, _), tail, ind = _split_steps(_P14_9)
    text = _joined(head, _backgrounded(first_run, ind), checkout, setup, tail)
    assert len(_patterns(tmp_path, text, "P14.9")) == 1


_P14_9_ARMS = """\
    on: pull_request_target
    jobs:
      t:
        runs-on: ubuntu-latest
        steps:
          - {executes}
          - uses: actions/checkout@v4
            with:
              {key}: {value}
"""


@pytest.mark.parametrize("executes,key,value", [
    ("uses: ./.github/actions/setup", "ref",
     "${{ github.event.pull_request.head.sha }}"),
    ("run: make", "repository",
     "${{ github.event.pull_request.head.repo.full_name }}"),
    ("uses: $/.github/actions/setup", "ref",
     "${{ github.event.pull_request.head.sha }}"),
])
def test_p14_9_concurrency_arms_a_local_action_and_a_repository_checkout(
    tmp_path: Path, executes: str, key: str, value: str,
) -> None:
    """Every arm of the concurrent loop: a local `./` action, a
    self-repository `$/` action (its definition is the base repository's,
    its steps run on the fork's tree) and a `run:` step as the executing
    step, and a head checkout named by `repository:`. Each is written ABOVE
    the checkout in one group; the flat negative control also pins that the
    same step written before the checkout, outside a group, stays silent."""
    text = _P14_9_ARMS.format(executes=executes, key=key, value=value)
    head, items, tail, ind = _split_steps(text)
    assert _patterns(tmp_path / "flat", _joined(head, *items, tail),
                     "P14.9") == []
    grouped = _joined(head, _grouped(items, ind), tail)
    hits = _patterns(tmp_path / "grp", grouped, "P14.9")
    assert len(hits) == 1
    if executes.startswith("uses: $/"):
        assert "`$/` action" in hits[0]["evidence"], hits[0]["evidence"]


_P14_24_CLONE = (_CLOAKED / "p14_24_mutable_fetch_exec.yml.fixture").read_text()


def test_p14_24_execution_in_the_fetchs_group_declared_first_still_fires(
    tmp_path: Path,
) -> None:
    head, (clone, execute), tail, ind = _split_steps(_P14_24_CLONE)
    assert _patterns(tmp_path / "flat", _joined(head, execute, clone, tail),
                     "P14.24") == []
    grouped = _joined(head, _grouped([execute, clone], ind), tail)
    assert len(_patterns(tmp_path / "grp", grouped, "P14.24")) == 1


# The checkout spelling of the same fetch: the workflow text of
# test_chain_detectors.test_p1424_checkout_of_another_repo_at_a_branch_then_execution.
_P14_24_CHECKOUT = """\
    name: ci
    on: push
    jobs:
      b:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
            with:
              repository: acme/tools
              ref: main
              path: tools
          - run: bash tools/run.sh
"""


def test_p14_24_checkout_and_execution_racing_in_one_group_fires(
    tmp_path: Path,
) -> None:
    head, (checkout, execute), tail, ind = _split_steps(_P14_24_CHECKOUT)
    assert _patterns(tmp_path / "flat", _joined(head, execute, checkout, tail),
                     "P14.24") == []
    grouped = _joined(head, _grouped([execute, checkout], ind), tail)
    assert len(_patterns(tmp_path / "grp", grouped, "P14.24")) == 1


@pytest.mark.parametrize("shape",
                         ["in-order", "working-directory", "inline-quoted"])
def test_p14_24_mutable_fetch_arm_reads_children_of_a_group(
    tmp_path: Path, shape: str,
) -> None:
    """The mutable-fetch arm (`_checkout_fetches`, `_step_marks`) reads a
    group's children like top-level steps: siblings in declaration order
    (which, as siblings, also race), a child's own `working-directory:`, and
    a single-line quoted `run:`. Strict order with no race is pinned by
    `test_p14_24_a_group_then_a_top_level_execution_is_plain_order`."""
    if shape == "inline-quoted":
        head, (clone, execute), tail, ind = _split_steps(_P14_24_CLONE)
        one = (clone[0].strip().split("run: ", 1)[1] + " && "
               + execute[0].strip().split("run: ", 1)[1])
        items = [[" " * ind + "- run: '" + one + "'\n"]]
    else:
        head, (checkout, execute), tail, ind = _split_steps(_P14_24_CHECKOUT)
        if shape == "working-directory":
            execute = [execute[0].replace("tools/run.sh", "run.sh"),
                       " " * (ind + 2) + "working-directory: tools\n"]
        items = [checkout, execute]
    assert len(_patterns(tmp_path / "flat", _joined(head, *items, tail),
                         "P14.24")) == 1
    grouped = _joined(head, _grouped(items, ind), tail)
    assert len(_patterns(tmp_path / "grp", grouped, "P14.24")) == 1


def test_p14_24_a_group_then_a_top_level_execution_is_plain_order(
    tmp_path: Path,
) -> None:
    """The clone sits in a group and the execution after it: not siblings,
    so only the declaration order (the group's implicit wait) can fire this,
    at the same line as the flat workflow, one line down."""
    head, (clone, execute), tail, ind = _split_steps(_P14_24_CLONE)
    flat = _patterns(tmp_path / "flat", _joined(head, clone, execute, tail),
                     "P14.24")
    assert len(flat) == 1, flat
    grouped = _patterns(tmp_path / "grp",
                        _joined(head, _grouped([clone], ind), execute, tail),
                        "P14.24")
    assert [f["line"] for f in grouped] == [flat[0]["line"] + 1], grouped


def test_p14_24_a_pin_racing_the_clone_pins_nothing(tmp_path: Path) -> None:
    """The pin-racing arm: clone and pin are siblings, the execution comes
    after the group. The pin may land before the clone, so it pins nothing."""
    text = (_CLOAKED / "p14_24_negative_sha_pinned_fetch.yml.fixture"
            ).read_text()
    head, (fetch, execute), tail, ind = _split_steps(text)
    clone = [" " * ind + "- run: " + fetch[1].strip() + "\n"]
    pin = [" " * ind + "- run: " + fetch[2].strip() + "\n"]
    assert _patterns(tmp_path / "flat", _joined(head, clone, pin, execute,
                                                tail), "P14.24") == []
    grouped = _joined(head, _grouped([clone, pin], ind), execute, tail)
    assert len(_patterns(tmp_path / "grp", grouped, "P14.24")) == 1


# The workflow text of test_chain_detectors.
# test_p1425_vite_in_job_yq_disabling_allowbuilds_silences_the_finding: builds
# disabled in the step above the install, on a repo pinning pnpm 10.
_P14_25_VITE = """\
    name: publish
    on: push
    jobs:
      publish:
        runs-on: ubuntu-latest
        permissions:
          id-token: write
        steps:
          - name: Disallow installation scripts
            run: yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
          - name: Install deps
            run: pnpm install
"""


def _pnpm10(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        '{"name": "x", "packageManager": "pnpm@10.34.5"}\n')
    (root / "pnpm-workspace.yaml").write_text("allowBuilds:\n  core-js: true\n")
    return root


def test_p14_25_disable_racing_the_install_in_one_group_is_no_protection(
    tmp_path: Path,
) -> None:
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    assert _patterns(_pnpm10(tmp_path / "flat"),
                     _joined(head, disable, install, tail), "P14.25") == []
    grouped = _joined(head, _grouped([disable, install], ind), tail)
    assert len(_patterns(_pnpm10(tmp_path / "grp"), grouped, "P14.25")) == 1


def test_p14_25_a_background_disable_protects_only_once_waited_for(
    tmp_path: Path,
) -> None:
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    bg = _backgrounded(disable, ind, step_id="a")
    assert len(_patterns(_pnpm10(tmp_path / "bg"),
                         _joined(head, bg, install, tail), "P14.25")) == 1
    # Waited for by id, or by `wait-all:`, the disable has finished first.
    for name, wait in (("id", "- wait: a\n"), ("all", "- wait-all:\n")):
        joined = _joined(head, bg, [" " * ind + wait], install, tail)
        assert _patterns(_pnpm10(tmp_path / name), joined, "P14.25") == [], wait


def test_p14_25_a_re_enable_racing_the_install_exposes_it(
    tmp_path: Path,
) -> None:
    """Builds are disabled at the top, then a re-enable and the install
    start together: the re-enable may land first, so the install is
    exposed (the racing RE-ENABLE arm of `_builds_disabled_at`)."""
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    enable = [ln.replace("Disallow", "Allow").replace(
        ".allowBuilds[]=false", ".allowBuilds[]=true") for ln in disable]
    assert _patterns(_pnpm10(tmp_path / "flat"),
                     _joined(head, disable, install, enable, tail),
                     "P14.25") == []
    grouped = _joined(head, disable, _grouped([enable, install], ind), tail)
    assert len(_patterns(_pnpm10(tmp_path / "grp"), grouped, "P14.25")) == 1


@pytest.mark.parametrize("wait,joins", [
    ("- wait: other-id\n", False), ("- cancel: a\n", False),
    ("- wait: [a]\n", True), ("- wait: [other-id, a]\n", True),
])
def test_p14_25_only_a_wait_naming_the_background_step_joins_it(
    tmp_path: Path, wait: str, joins: bool,
) -> None:
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    bg = _backgrounded(disable, ind, step_id="a")
    text = _joined(head, bg, [" " * ind + wait], install, tail)
    assert len(_patterns(_pnpm10(tmp_path), text, "P14.25")) == \
        (0 if joins else 1), wait


def test_p14_25_a_group_finishes_before_the_step_after_it(tmp_path: Path) -> None:
    """A group ends with an implicit wait: a disable inside it protects an
    install written AFTER the group."""
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    text = _joined(head, _grouped([disable], ind), install, tail)
    assert _patterns(_pnpm10(tmp_path), text, "P14.25") == []


# ---------------------------------------------------------------------------
# Arms and facts the corpus wrap does not reach on its own.
# ---------------------------------------------------------------------------

def test_p14_24_checkout_of_another_repo_inside_a_group_fires_at_its_own_line(
    tmp_path: Path,
) -> None:
    """The checkout arm of P14.24 (`_checkout_fetches`): a checkout of ANOTHER
    repository written as a child of a group, executed by a top-level step
    after the group. The group's implicit wait makes this plain order, so it
    is the same finding as the flat workflow, one line down."""
    head, (checkout, execute), tail, ind = _split_steps(_P14_24_CHECKOUT)
    flat = _patterns(tmp_path / "flat", _joined(head, checkout, execute, tail),
                     "P14.24")
    assert len(flat) == 1, flat
    grouped = _patterns(tmp_path / "grp",
                        _joined(head, _grouped([checkout], ind), execute, tail),
                        "P14.24")
    assert [f["line"] for f in grouped] == [flat[0]["line"] + 1], grouped


_P14_7_ACTIONS_CACHE = """\
    name: ci
    on: pull_request_target
    jobs:
      measure:
        runs-on: ubuntu-latest
        steps:
          - uses: {uses}
            with:
              path: ~/.npm
              key: npm
          - run: npm ci
"""


@pytest.mark.parametrize("uses", ["actions/cache@v4", "actions/cache/save@v4"])
def test_p14_7_actions_cache_inside_a_group_is_a_cache_write(
    tmp_path: Path, uses: str,
) -> None:
    """`_job_uses_cache` reads `actions/cache` through
    `_job_step_uses_prefixes`, a separate loop from the `cache:` input arm the
    corpus fixture exercises, so a group around it is pinned on its own."""
    text = _P14_7_ACTIONS_CACHE.format(uses=uses)
    head, items, tail, ind = _split_steps(text)
    flat = _patterns(tmp_path / "flat", _joined(head, *items, tail), "P14.7")
    assert len(flat) == 1, flat
    grouped = _patterns(tmp_path / "grp",
                        _joined(head, _grouped(items, ind), tail), "P14.7")
    assert [f["line"] for f in grouped] == [flat[0]["line"]], grouped


def test_test_failure_fact_names_a_child_step_as_group_dot_child() -> None:
    """The test-failure-fatal fact names WHERE the suite's failure is
    swallowed. A child of the group at step 2 is `step 2.2`, not `step 2`
    (the group) and not `step 2` counted inside the group."""
    facts = load_script("ci_secure_config_facts", "config_facts.py")
    doc = {
        "on": "pull_request",
        "jobs": {"test": {"runs-on": "ubuntu-latest", "steps": [
            {"run": "make"},
            {"parallel": [{"run": "echo one"}, {"run": "pytest -q || true"}]},
        ]}},
    }
    offences, saw_suite, _ = facts._suite_failure_swallowed(
        ".github/workflows/ci.yml", doc)
    assert saw_suite
    assert len(offences) == 1, offences
    assert "job `test` step 2.2 " in offences[0], offences


# The workflow text of test_scan.test_a_step_level_gate_withdraws_the_job_level
# _verdict, with the step's `if:` moved up onto a group around it.
_P14_10_GATED_GROUP = """\
on:
  issues:
    types: [opened]
jobs:
  create-issue:
    runs-on: ubuntu-latest
    if: github.event.pull_request.user.login != 'dependabot[bot]'
    steps:
      - parallel:
          - run: echo '${{ github.event.issue.title }}'
"""


def _p14_10_note(root: Path, text: str) -> str:
    hits = _patterns(root, text, "P14.10")
    assert len(hits) == 1, hits
    return hits[0].get("derived_note") or ""


def test_p14_10_group_level_if_withdraws_the_job_gate_note(
    tmp_path: Path,
) -> None:
    """The job's gate is provably dead on `issues`, so P14.10 carries the
    informational INERT note. A group-level `if:` is the step's own guard, as
    a step-level `if:` is, and withdraws the note. The group's `if:` never
    suppresses the finding itself."""
    assert "INERT" in _p14_10_note(tmp_path / "plain", _P14_10_GATED_GROUP)
    gated = _P14_10_GATED_GROUP.replace(
        "      - parallel:\n",
        "      - if: github.event.issue.user.login == 'trusted-owner'\n"
        "        parallel:\n")
    assert gated != _P14_10_GATED_GROUP
    assert _p14_10_note(tmp_path / "gated", gated) == ""


def _wrap_tail_in_parallel(text: str) -> tuple[str, list[int]]:
    """Like `_wrap_steps_in_parallel`, but each `steps:` list keeps its FIRST
    step top-level and only the rest go into the group: a job holding plain
    steps and a group side by side. A one-step list is left as written."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted_after: list[int] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _STEPS_KEY_RE.match(line.rstrip("\n"))
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        block: list[str] = []
        while j < len(lines):
            cur = lines[j]
            stripped = cur.strip()
            cur_indent = len(cur) - len(cur.lstrip(" "))
            if not stripped or stripped.startswith("#") or cur_indent > indent \
                    or (cur_indent == indent and stripped.startswith("- ")):
                block.append(cur)
                j += 1
                continue
            break
        while block and (not block[-1].strip() or block[-1].strip().startswith("#")):
            block.pop()
            j -= 1
        starts = [k for k, b in enumerate(block) if b.strip().startswith("- ")]
        item_indent = (len(block[starts[0]]) - len(block[starts[0]].lstrip(" "))
                       if starts else 0)
        starts = [k for k in starts
                  if len(block[k]) - len(block[k].lstrip(" ")) == item_indent]
        if len(starts) < 2:
            out.extend(block)
            i = j
            continue
        cut = starts[1]
        out.extend(block[:cut])
        out.append(" " * item_indent + "- parallel:\n")
        inserted_after.append(i + 1 + cut)
        out.extend(("    " + b) if b.strip() else b for b in block[cut:])
        i = j
    return "".join(out), inserted_after


# Fixtures whose steps race once all but the first share a group.
_RACE_ONLY_WHEN_TAIL_WRAPPED: dict[str, list[str]] = {}


@pytest.mark.parametrize(
    "fixture", sorted(p.name for p in _CLOAKED.glob("*.yml.fixture")))
def test_whole_corpus_reports_identically_with_plain_steps_beside_a_group(
    tmp_path: Path, fixture: str,
) -> None:
    """The mixed shape: top-level steps and a group in the same job. The
    walkers must interleave both, and the node walker must keep every line
    attributed, so each flat finding reappears at its shifted line."""
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_tail_in_parallel(text)
    if not inserted_after:
        pytest.skip(f"{fixture} has no step list of two or more steps")
    name = fixture.removesuffix(".fixture")
    _write(tmp_path / "plain", name, text)
    _write(tmp_path / "mixed", name, wrapped)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "mixed")
    want = _signature(plain["findings"], inserted_after)
    got = _signature(inside["findings"], None)
    extra = list(got)
    for sig in want:
        assert sig in extra, f"{fixture}: {sig} lost beside a parallel: group"
        extra.remove(sig)
    assert sorted(s[0] for s in extra) == \
        _RACE_ONLY_WHEN_TAIL_WRAPPED.get(fixture, []), extra
    assert inside["dropped_matches"] == plain["dropped_matches"]
    assert inside["coverage_notes"] == plain["coverage_notes"]


# ---------------------------------------------------------------------------
# P14.10 takes each `run:` scalar's line from the parsed node, not from a raw
# text cursor: a cursor cannot see a flow-style `{run: ...}`, and it walks
# straight through a group it could not read, so the next block `run:` (in
# another step, or another job) was blamed for the injection.
# ---------------------------------------------------------------------------

_ISSUE_TITLE = "echo ${{ github.event.issue.title }}"


def _p14_10_lines(root: Path, text: str) -> tuple[list[int], list[str]]:
    _write(root, "a.yml", text)
    data = _scan_root(root)
    lines = sorted(f["line"] for f in data["findings"]
                   if f["pattern"] == "P14.10")
    return lines, [d["reason"] for d in data["dropped_matches"]]


def test_p14_10_a_flow_style_group_child_is_reported_at_its_own_line(
    tmp_path: Path,
) -> None:
    text = textwrap.dedent(f"""\
        on: issues
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - parallel: [{{run: "{_ISSUE_TITLE}"}}, {{run: "echo ok"}}]
          b:
            runs-on: ubuntu-latest
            steps:
              - run: {_ISSUE_TITLE}
        """)
    lines, dropped = _p14_10_lines(tmp_path, text)
    assert lines == [6, 10], lines
    assert dropped == [], dropped


def test_p14_10_a_group_it_cannot_read_does_not_shift_later_lines(
    tmp_path: Path,
) -> None:
    """The group's value is not a list: its `run:` is not a step. The two
    real steps after it are reported at their own lines, not one line up."""
    text = textwrap.dedent(f"""\
        on: issues
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - name: broken group
                parallel:
                  run: {_ISSUE_TITLE}
              - run: {_ISSUE_TITLE}
              - run: {_ISSUE_TITLE}
        """)
    lines, _ = _p14_10_lines(tmp_path, text)
    assert lines == [9, 10], lines


def test_p14_10_a_child_written_above_its_parents_run_is_not_dropped(
    tmp_path: Path,
) -> None:
    """`parallel:` written before `run:` on one step: the walker yields the
    step before its children, so a cursor anchored the parent at the child's
    line and then lost the child's injection as a "folded scalar"."""
    text = textwrap.dedent(f"""\
        on: issues
        jobs:
          t:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  - run: {_ISSUE_TITLE}
                run: echo hi
        """)
    lines, dropped = _p14_10_lines(tmp_path, text)
    assert lines == [7], lines
    assert dropped == [], dropped


# ---------------------------------------------------------------------------
# Nothing the walker passes over goes undisclosed.
# ---------------------------------------------------------------------------

def test_a_group_entry_that_is_not_a_step_is_disclosed() -> None:
    """A nested list or a bare string inside a group is not a step. It used
    to be skipped with no record; it is now a coverage note saying it was not
    read as steps."""
    stats = scan._StepWalkStats()
    job = {"steps": [{"parallel": [[{"run": "a"}], "run: b", {"run": "c"}]}]}
    assert [s.step["run"] for s in scan._iter_job_steps(job, stats)] == ["c"]
    assert stats.malformed == [("1.1", scan._GROUP_CHILD_NOT_A_STEP),
                               ("1.2", scan._GROUP_CHILD_NOT_A_STEP)]
    sentence = scan._GROUP_GAP_SENTENCE[scan._GROUP_CHILD_NOT_A_STEP]
    assert "not read as steps" in sentence and "review it manually" in sentence


@pytest.mark.parametrize("children,depth", [("not-a-list", 0),
                                            ([{"run": "child"}], None)])
def test_a_step_beside_a_group_never_claims_unwalked_children_were_scanned(
    children, depth,
) -> None:
    """`run:` beside `parallel:` whose children were NOT walked (not a list,
    or past the depth cap) must not get the sentence saying its child steps
    were scanned."""
    step = {"run": "own", "parallel": children}
    steps: list = [step]
    if depth is None:                       # put the step past the depth cap
        for _ in range(scan._WALK_MAX_DEPTH):
            steps = [{"parallel": steps}]
    stats = scan._StepWalkStats()
    leaves = [s.step.get("run") for s in scan._iter_job_steps(
        {"steps": steps}, stats)]
    assert "child" not in leaves
    assert stats.malformed, stats
    for _, shape in stats.malformed:
        sentence = scan._GROUP_GAP_SENTENCE[shape].format(
            depth=scan._WALK_MAX_DEPTH)
        assert "child steps were scanned" not in sentence, (shape, sentence)


def test_a_group_whose_value_is_not_a_list_is_not_called_unscanned_by_all() -> None:
    """Raw-text checks still read the text inside such a group, so "NOT
    scanned by any detector" overclaims; the note says it was not read as
    steps, and that some raw-text checks may still have matched."""
    sentence = scan._GROUP_GAP_SENTENCE[scan._GROUP_NOT_A_LIST]
    assert "by any detector" not in sentence
    assert "not read as steps" in sentence
    assert "raw-text checks may still have matched" in sentence


def test_a_background_child_of_a_group_is_counted_once() -> None:
    """scan-output.md: children of a group are counted in `steps_scanned`,
    not in `background_steps`, even when written `background: true`."""
    stats = scan._StepWalkStats()
    job = {"steps": [{"parallel": [{"run": "a", "background": True},
                                   {"run": "b"}]},
                     {"run": "c", "background": True}]}
    list(scan._iter_job_steps(job, stats))
    assert (stats.in_parallel, stats.background) == (2, 1), stats


# ---------------------------------------------------------------------------
# Fail safe: a step that is not provably finished is still running.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,background", [
    (True, True), ("true", True), ("${{ inputs.bg }}", True), (None, True),
    (False, False), ("<absent>", False),
])
def test_any_background_value_but_false_is_treated_as_running(
    value, background,
) -> None:
    step = {"run": "a"} if value == "<absent>" else {"run": "a",
                                                      "background": value}
    (leaf,) = scan._iter_job_steps({"steps": [step]})
    assert leaf.background is background


def test_a_background_value_chosen_at_run_time_is_disclosed() -> None:
    stats = scan._StepWalkStats()
    list(scan._iter_job_steps(
        {"steps": [{"run": "a", "background": "${{ inputs.bg }}"}]}, stats))
    assert stats.malformed == [("1", scan._STEP_BACKGROUND_EXPRESSION)]


@pytest.mark.parametrize("bg_value", ["'true'", "${{ inputs.bg }}"])
def test_p14_25_a_disable_backgrounded_by_a_non_literal_value_is_racing(
    tmp_path: Path, bg_value: str,
) -> None:
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    bg = [ln.replace("background: true", f"background: {bg_value}")
          for ln in _backgrounded(disable, ind)]
    assert len(_patterns(_pnpm10(tmp_path), _joined(head, bg, install, tail),
                         "P14.25")) == 1


@pytest.mark.parametrize("wait", ["- wait-all: false\n",
                                  "- wait-all: ${{ inputs.join }}\n"])
def test_p14_25_a_wait_all_that_is_not_true_joins_nothing(
    tmp_path: Path, wait: str,
) -> None:
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    bg = _backgrounded(disable, ind, step_id="a")
    text = _joined(head, bg, [" " * ind + wait], install, tail)
    assert len(_patterns(_pnpm10(tmp_path), text, "P14.25")) == 1, wait


# Keys written on the group entry itself.

def test_a_background_group_backgrounds_its_children(tmp_path: Path) -> None:
    (leaf,) = scan._iter_job_steps({"steps": [
        {"parallel": [{"run": "a"}], "background": True, "id": "g"}]})
    assert leaf.background
    head, (disable, install), tail, ind = _split_steps(_P14_25_VITE)
    group = [" " * ind + "- background: true\n",
             " " * ind + "  id: g\n",
             " " * ind + "  parallel:\n",
        *(("    " + ln) for ln in disable)]
    assert len(_patterns(_pnpm10(tmp_path / "bg"),
                         _joined(head, group, install, tail), "P14.25")) == 1
    # Waited for by the group's id, the disable has finished first.
    joined = _joined(head, group, [" " * ind + "- wait: g\n"], install, tail)
    assert _patterns(_pnpm10(tmp_path / "joined"), joined, "P14.25") == []


def test_group_level_continue_on_error_swallows_the_suite() -> None:
    facts = load_script("ci_secure_config_facts", "config_facts.py")
    doc = {
        "on": "pull_request",
        "jobs": {"test": {"runs-on": "ubuntu-latest", "steps": [
            {"parallel": [{"run": "pytest -q"}], "continue-on-error": True},
        ]}},
    }
    offences, saw_suite, _ = facts._suite_failure_swallowed(
        ".github/workflows/ci.yml", doc)
    assert saw_suite
    assert len(offences) == 1, offences
    assert "step 1.1" in offences[0] and "group-level" in offences[0], offences


@pytest.mark.parametrize("key,noted", [
    ("env", True), ("timeout-minutes", True), ("working-directory", True),
    ("if", False), ("name", False), ("id", False),
    ("continue-on-error", False), ("background", False),
])
def test_a_group_key_the_scan_does_not_model_is_disclosed(
    key: str, noted: bool,
) -> None:
    stats = scan._StepWalkStats()
    list(scan._iter_job_steps(
        {"steps": [{"parallel": [{"run": "a"}], key: "x"}]}, stats))
    want = [("1", scan._GROUP_UNKNOWN_KEY)] if noted else None
    assert stats.malformed == want, stats


_SELF_REFERENCING_STEPS = """\
on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: &s
      - run: echo hi
      - parallel: *s
      - parallel: *s
"""


def _exponential_steps(levels: int) -> str:
    """No cycle, but each level holds the one below twice: 2**levels leaves
    from a few dozen lines of text."""
    out = ["on: push", "x-l0: &l0", "  - run: echo hi"]
    for n in range(1, levels + 1):
        out += [f"x-l{n}: &l{n}", f"  - parallel: *l{n - 1}",
                f"  - parallel: *l{n - 1}"]
    out += ["jobs:", "  a:", "    runs-on: ubuntu-latest",
            f"    steps: *l{levels}", ""]
    return "\n".join(out)


_WALK_BOTH = """\
import sys, yaml
sys.path.insert(0, sys.argv[1])
from _scan_import import load_scan
scan = load_scan()
text = open(sys.argv[2]).read()
job = yaml.safe_load(text)["jobs"]["a"]
stats = scan._StepWalkStats()
leaves = [s.step.get("run") for s in scan._iter_job_steps(job, stats)]
steps_node = scan._node_keys(scan._job_nodes(text)["a"])["steps"]
nodes = list(scan._iter_step_nodes(steps_node))
print(len(leaves), len(nodes), sorted({s for _, s in stats.malformed or []}))
"""


@pytest.mark.parametrize("name", ["cycle", "exponential"])
def test_both_walkers_finish_on_alias_bombs_and_disclose_it(
    tmp_path: Path, name: str,
) -> None:
    """A step list that holds itself through an alias, or one that doubles
    at every level, must neither hang the scan nor read as clean: both
    walkers stop at the same point and the walk records why."""
    text = (_SELF_REFERENCING_STEPS if name == "cycle"
            else _exponential_steps(40))
    wf = tmp_path / "a.yml"
    wf.write_text(text)
    script = tmp_path / "walk.py"
    script.write_text(_WALK_BOTH)
    out = subprocess.run(
        [sys.executable, str(script), _TESTS_DIR, str(wf)],
        capture_output=True, text=True, timeout=20, check=True).stdout.split(
        " ", 2)
    assert out[0] == out[1], out
    want = scan._GROUP_CYCLE if name == "cycle" else scan._GROUP_OVER_BUDGET
    assert want in out[2], out


def test_a_pin_with_no_known_line_is_treated_as_racing() -> None:
    """P14.24: a pin suppresses a fetch only when it provably lands between
    the fetch and the execution. A pin whose line is unknown is not provably
    anywhere, so in a job with concurrent steps it is racing (fail safe). In
    a job with no concurrency the command order alone decides, as before."""
    never = lambda a, b: False                         # noqa: E731
    assert scan._pin_races(None, (10, 12), never, concurrency=True)
    assert not scan._pin_races(None, (10, 12), never, concurrency=False)
    assert not scan._pin_races(11, (10, 12), never, concurrency=True)


# ---------------------------------------------------------------------------
# Source spans (the concurrency model for the line-based detectors) resolve
# keys the way the loader does.
# ---------------------------------------------------------------------------

_SPAN_SHAPES = {
    "merge-key group": ("""\
        on: push
        x-grp: &grp
          parallel:
            - run: echo a
            - run: echo b
        jobs:
          t:
            runs-on: ubuntu-latest
            steps:
              - <<: *grp
        """, "t"),
    "merge-key job": ("""\
        on: push
        jobs:
          base: &b
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  - run: echo a
                  - run: echo b
          build:
            <<: *b
        """, "build"),
    "yaml-1.1 bool job key": ("""\
        on: push
        jobs:
          yes:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  - run: echo a
                  - run: echo b
        """, "True"),
}


@pytest.mark.parametrize("shape", sorted(_SPAN_SHAPES))
def test_step_spans_resolve_keys_as_the_loader_does(shape: str) -> None:
    text, job = _SPAN_SHAPES[shape]
    spans = [s for s in scan._step_spans(textwrap.dedent(text))
             if s.job == job]
    assert len(spans) == 2, (shape, spans)
    assert spans[0].start_line < spans[1].start_line


def test_a_job_whose_spans_cannot_be_found_is_disclosed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Concurrency silently falling back to declaration order is a coverage
    gap, not a clean answer."""
    text = textwrap.dedent(_SPAN_SHAPES["merge-key job"][0])
    monkeypatch.setattr(scan, "_job_nodes", lambda _text: {})
    unmatched: list[str] = []
    assert scan._step_spans(text, unmatched) == []
    assert unmatched == ["base", "build"]
    wf = tmp_path / "a.yml"
    wf.write_text(text)
    _, gaps = scan._parallel_step_stats(wf)
    assert any("jobs.build" in g and "declaration order" in g
               for g in gaps), gaps


def test_concurrency_within_one_top_level_entry_needs_two_grouped_steps() -> None:
    T = scan._StepTiming
    assert scan._steps_concurrent(T(2, True, None), T(2, True, None))
    # A step carrying `parallel:` beside `run:` and its own child share a
    # top-level entry, but the parent is not a group child: declared order.
    assert not scan._steps_concurrent(T(2, False, None), T(2, True, None))


def test_lines_in_different_jobs_are_never_concurrent() -> None:
    T, S = scan._StepTiming, scan._StepSpan
    both = T(0, True, None)
    spans = [S("a", 1, 2, both), S("a", 3, 4, both), S("b", 5, 6, both)]
    assert scan._lines_concurrent(spans, 1, 3)
    assert not scan._lines_concurrent(spans, 1, 5)


def test_a_repo_without_the_syntax_renders_no_parallel_steps_row(
    tmp_path: Path,
) -> None:
    """The findings JSON always carries `parallel_steps` (all zeros here);
    the rendered report shows no row for it."""
    text = (_CLOAKED / "p14_10_template_injection.yml.fixture").read_text()
    _write(tmp_path, "a.yml", text)
    data = _scan_root(tmp_path)
    assert data["parallel_steps"] == {"steps_scanned": 0, "control_steps": 0,
                                      "background_steps": 0, "workflows": []}
    md = load_script("ci_secure_report", "report.py").render(data)
    assert "Parallel steps" not in md, md
