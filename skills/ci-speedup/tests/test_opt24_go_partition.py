"""#462 / #466: when a Go matrix `-run`/`-skip` filter counts as existing sharding.

Three outcomes, each pinned here:

* the filters provably assign every readable test of the one literal package to
  exactly one leg -> already sharded, no OPT24 finding;
* the filters provably do NOT (a repeated value, one leg, an overlap or a gap on
  a test that certainly runs) -> classified normally, and the finding says why
  the existing split is not a partition;
* the split cannot be checked (generated values, a file-fed list, an unreadable
  package, subtest filters, build-constrained tests) -> the finding is withheld
  and listed as held back with the reason, never reported as "shard this" and
  never called verified.
"""
from __future__ import annotations

from pathlib import Path

import collect_runs as cr


_TWO_TESTS = (
    "package app\n"
    "import \"testing\"\n"
    "func TestMain(m *testing.M) { m.Run() }\n"
    "func TestCLI(t *testing.T) {}\n"
    "func TestConfig(t *testing.T) {}\n"
    "func testHelper(t *testing.T) {}\n"
    "func Testable() {}\n"
)


def _package(root: Path, rel: str = "cmd/app", body: str = _TWO_TESTS,
             name: str = "app_test.go") -> Path:
    package = root / rel
    package.mkdir(parents=True, exist_ok=True)
    (package / name).write_text(body, encoding="utf-8")
    return package


def _job(run: str, matrix, **extra):
    job = {"strategy": {"matrix": matrix}, "steps": [{"run": run}]}
    job.update(extra)
    return {"jobs": {"go-tests": job}}


def _status(doc, root):
    job = doc["jobs"]["go-tests"]
    return cr._go_matrix_split_status(job, job["strategy"]["matrix"], root,
                                      doc)


def _job_runs(secs: int = 400):
    def one(name):
        mm, ss = divmod(secs, 60)
        return {"name": name, "html_url": "http://x",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": f"2026-01-01T00:{mm:02d}:{ss:02d}Z"}
    return [[one("go-tests (a)"), one("go-tests (b)")] for _ in range(4)]


# --- verified partitions stay quiet ------------------------------------------

def test_package_named_after_the_filter_is_read(tmp_path):
    _package(tmp_path)
    doc = _job("go test -race -run '${{ matrix.filter }}' ./cmd/app -v",
               {"filter": ["^TestCLI$", "^TestConfig$"]})
    assert _status(doc, tmp_path)[0] == "verified"
    assert "go-tests" in cr._sharded_bases(doc, tmp_path)


def test_filter_values_from_matrix_include_entries_are_read(tmp_path):
    _package(tmp_path)
    doc = _job("go test ./cmd/app -run \"${{ matrix.tests }}\"",
               {"include": [{"tests": "^(TestCLI)$"},
                            {"tests": "^(TestConfig)$"}]})
    assert _status(doc, tmp_path)[0] == "verified"


def test_filter_through_step_env_is_read(tmp_path):
    _package(tmp_path)
    doc = {"jobs": {"go-tests": {
        "strategy": {"matrix": {"tests": ["^TestCLI$", "^TestConfig$"]}},
        "steps": [{"env": {"TESTS": "${{ matrix.tests }}"},
                   "run": "go test -run \"$TESTS\" ./cmd/app"}]}}}
    assert _status(doc, tmp_path)[0] == "verified"


def test_test_main_and_non_test_functions_are_not_tests(tmp_path):
    """`TestMain`, `testHelper` and `Testable` are not run by -run; requiring a
    leg for them would call a correct split a gap."""
    _package(tmp_path)
    doc = _job("go test ./cmd/app -run '${{ matrix.f }}'",
               {"f": ["^TestCLI$", "^TestConfig$"]})
    assert _status(doc, tmp_path)[0] == "verified"


def test_package_is_resolved_against_the_working_directory(tmp_path):
    _package(tmp_path, "services/api/cmd/app")
    doc = {"jobs": {"go-tests": {
        "defaults": {"run": {"working-directory": "services/api"}},
        "strategy": {"matrix": {"f": ["^TestCLI$", "^TestConfig$"]}},
        "steps": [{"run": "go test ./cmd/app -run '${{ matrix.f }}'"}]}}}
    assert _status(doc, tmp_path)[0] == "verified"


def test_skip_complement_and_alternation_partitions_are_verified(tmp_path):
    _package(tmp_path, body=_TWO_TESTS + "func TestServe(t *testing.T) {}\n")
    skip = _job("go test ./cmd/app -skip '${{ matrix.f }}'",
                {"f": ["^(TestCLI|TestConfig)$", "^TestServe$"]})
    assert _status(skip, tmp_path)[0] == "verified"
    alt = _job("go test ./cmd/app -run='${{ matrix.f }}'",
               {"f": ["^(TestCLI|TestConfig)$", "^TestServe$"]})
    assert _status(alt, tmp_path)[0] == "verified"


def test_module_import_path_package_is_read(tmp_path):
    (tmp_path / "go.mod").write_text("module example.invalid/proj\n", encoding="utf-8")
    _package(tmp_path)
    doc = _job("go test example.invalid/proj/cmd/app -run '${{ matrix.f }}'",
               {"f": ["^TestCLI$", "^TestConfig$"]})
    assert _status(doc, tmp_path)[0] == "verified"


# --- provable non-partitions are classified normally --------------------------

def test_repeated_overlapping_single_and_gapped_filters_are_broken(tmp_path):
    _package(tmp_path)
    run = "go test ./cmd/app -run '${{ matrix.f }}'"
    for values in (["^TestCLI$", "^TestCLI$"], ["^Test", "^TestCLI$"],
                   ["^TestCLI$"], ["^TestCLI$", "^TestNope$"]):
        doc = _job(run, {"f": values})
        state, reason = _status(doc, tmp_path)
        assert state == "broken", (values, state, reason)
        assert reason
        assert "go-tests" not in cr._sharded_bases(doc, tmp_path), values


def test_broken_partition_finding_says_the_split_is_not_a_partition(tmp_path):
    _package(tmp_path)
    doc = _job("go test ./cmd/app -run '${{ matrix.f }}'",
               {"f": ["^Test", "^TestCLI$"]})
    findings = cr._detect_opt24_long_test_no_sharding(
        "ci.yml", _job_runs(), 0, sharded_bases=cr._sharded_bases(doc, tmp_path),
        wf_doc=doc, repo_root=tmp_path)
    assert len(findings) == 1
    finding = findings[0]
    assert "do not partition" in finding["evidence"]
    assert "TestCLI" in finding["evidence"]
    summary = finding["measured_evidence"]["summary"]
    assert "single job" not in summary and "runs unsharded" not in summary, summary


# --- unverifiable splits are withheld and disclosed ---------------------------

_UNVERIFIABLE = {
    "generated-list": ("go test ./cmd/app -run '${{ matrix.f }}'",
                       {"f": "${{ fromJSON(needs.list.outputs.shards) }}"}),
    "file-fed": ("go test -run \"$(cat parts/${{ matrix.part }}.txt)\" ./cmd/app",
                 {"part": [0, 1]}),
    "wildcard-package": ("go test ./... -run '${{ matrix.f }}'",
                         {"f": ["^TestCLI$", "^TestConfig$"]}),
    "missing-package": ("go test ./cmd/absent -run '${{ matrix.f }}'",
                        {"f": ["^TestCLI$", "^TestConfig$"]}),
    "subtests": ("go test ./cmd/app -run '${{ matrix.f }}'",
                 {"f": ["^TestCLI$/fast", "^TestCLI$/slow"]}),
    "literal-suffix": ("go test ./cmd/app -run '${{ matrix.f }}Suite'",
                       {"f": ["^TestCLI", "^TestConfig"]}),
    "extra-literal-run": ("go test ./cmd/app -run '^TestC' -skip '${{ matrix.f }}'",
                          {"f": ["^TestCLI$", "^TestConfig$"]}),
    "posix-class": ("go test ./cmd/app -run '${{ matrix.f }}'",
                    {"f": ["^Test[[:upper:]]LI$", "^TestConfig$"]}),
    "include-and-exclude": ("go test ./cmd/app -run '${{ matrix.f }}'",
                            {"f": ["^TestCLI$", "^TestConfig$"],
                             "exclude": [{"f": "^TestCLI$"}]}),
}


def test_unverifiable_splits_are_unknown_and_stay_quiet(tmp_path):
    _package(tmp_path)
    for shape, (run, matrix) in _UNVERIFIABLE.items():
        doc = _job(run, matrix)
        state, reason = _status(doc, tmp_path)
        assert state == "unknown", (shape, state, reason)
        assert "go-tests" in cr._sharded_bases(doc, tmp_path), shape
        held: list = []
        findings = cr._detect_opt24_long_test_no_sharding(
            "ci.yml", _job_runs(), 0,
            sharded_bases=cr._sharded_bases(doc, tmp_path), wf_doc=doc,
            repo_root=tmp_path, opt24_withheld=held)
        assert findings == [], shape
        assert held == [{"workflow_file": "ci.yml", "job": "go-tests",
                         "pattern": "OPT24",
                         "gate": cr._GO_PARTITION_UNVERIFIED_GATE}], (shape, held)


def test_build_constrained_tests_only_make_the_split_unknown(tmp_path):
    """A test in a `//go:build` file may not be compiled in CI, so a filter that
    leaves it uncovered is unknown, not a provable gap."""
    package = _package(tmp_path)
    (package / "integ_test.go").write_text(
        "//go:build integration\n\npackage app\n"
        "func TestIntegration(t *testing.T) {}\n", encoding="utf-8")
    doc = _job("go test ./cmd/app -run '${{ matrix.f }}'",
               {"f": ["^TestCLI$", "^TestConfig$"]})
    assert _status(doc, tmp_path)[0] == "unknown"


def test_short_job_with_unverifiable_split_is_not_listed(tmp_path):
    doc = _job("go test ./... -run '${{ matrix.f }}'",
               {"f": ["^TestCLI$", "^TestConfig$"]})
    held: list = []
    assert cr._detect_opt24_long_test_no_sharding(
        "ci.yml", _job_runs(120), 0, sharded_bases=cr._sharded_bases(doc, tmp_path),
        wf_doc=doc, repo_root=tmp_path, opt24_withheld=held) == []
    assert held == []


def test_held_back_registry_is_coupled_across_collector_renderer_and_verifier():
    import importlib.util
    import blocking_path as bp
    spec = importlib.util.spec_from_file_location(
        "vr_opt24", Path(__file__).with_name("verify_report.py"))
    vr = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["vr_opt24"] = vr
    spec.loader.exec_module(vr)
    assert cr._OPT24_WITHHELD_DOC_KEY == bp._OPT24_WITHHELD_DOC_KEY \
        == vr._VR_OPT24_WITHHELD_DOC_KEY
    assert set(bp._OPT24_WITHHOLD_PHRASES) == {cr._GO_PARTITION_UNVERIFIED_GATE}
    assert bp._OPT24_WITHHOLD_PHRASES == vr._VR_OPT24_WITHHOLD_PHRASES
    row = next(r for r in bp._WITHHELD_ROWS if r.doc_key == bp._OPT24_WITHHELD_DOC_KEY)
    vrow = next(r for r in vr._VR_WITHHELD_ROWS
                if r.doc_key == vr._VR_OPT24_WITHHELD_DOC_KEY)
    assert tuple(row) == tuple(vrow)
    phrase = bp._OPT24_WITHHOLD_PHRASES[cr._GO_PARTITION_UNVERIFIED_GATE]
    assert "could not be verified" in phrase and "_" not in phrase


def test_command_shapes_around_a_correct_partition_are_verified(tmp_path):
    """Redirections, pipes, value flags, continuations, unquoted expressions,
    other matrix axes and output-less examples do not break a correct split."""
    _package(tmp_path, body=_TWO_TESTS + "func ExampleRun() {}\n")
    f = {"f": ["^TestCLI$", "^TestConfig$"]}
    shapes = {
        "pipe": ("go test ./cmd/app -run '${{ matrix.f }}' 2>&1 | tee out.txt", f),
        "unquoted": ("go test ./cmd/app -run ${{ matrix.f }}", f),
        "value-flags": ("go test -count 1 -coverprofile ./cover.out "
                        "-run=${{matrix.f}} ./cmd/app -timeout 10m", f),
        "continuation": ("go test \\\n  -race \\\n  -run '${{ matrix.f }}' \\\n"
                         "  ./cmd/app", f),
        "other-axis": ("go test ./cmd/app -run '${{ matrix.f }}'",
                       {"go": ["1.22", "1.23"], **f}),
    }
    for shape, (run, matrix) in shapes.items():
        assert _status(_job(run, matrix), tmp_path)[0] == "verified", shape
