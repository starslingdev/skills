"""OPT81 — The same job is measurably faster on another runner.

Two halves, tested apart:

- A1 (measured): the repository's own sampled history already ran one job on two
  runner CLASSES. Every gate, the credit rule and every stamped number.
- A2 (advisory): the lever of last resort on a merge-gating compute pole on a
  standard GitHub-hosted label. Every sub-gate of "no cheaper lever exists",
  and the rule that it carries no number anywhere.

Plus the guards the owner's decisions require: the runner-class taxonomy table,
the vendor rule (the advisory names a larger GitHub-hosted size or StarSling
runners and nothing else, and no OPT81 text names another runner vendor or any
domain but github.com / starsling.dev), the app-install prerequisite, the
publisher disclosure on every surface, and the verifier's re-derivation.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_CATALOG = _SKILL_DIR / "references" / "optimization-patterns.md"
_WF = ".github/workflows/bench.yml"
_STEPS = ["Set up job", "Run actions/checkout@v4", "Run benchmarks",
          "Post Run actions/checkout@v4", "Complete job"]


def _load_verify_report():
    name = "ci_speedup_verify_report_opt81"
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "tests" / "verify_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name: str, label: "str | list[str]", dur: float, run_id: int, *,
         steps: "list[str] | None" = None, skipped: tuple[str, ...] = (),
         conclusion: str = "success", work: str = "Run benchmarks") -> dict:
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(hours=run_id)
    names = list(steps or _STEPS)
    if work != "Run benchmarks":
        names = [work if n == "Run benchmarks" else n for n in names]
    fixed = 11.0
    out_steps, t = [], t0
    for i, n in enumerate(names, 1):
        d = (dur - fixed) if n == work else {"Set up job": 2, "Complete job": 0}.get(
            n, 8 if "checkout@" in n and not n.startswith("Post") else 1)
        out_steps.append({"name": n, "number": i, "started_at": _iso(t),
                          "completed_at": _iso(t + timedelta(seconds=d)),
                          "status": "completed",
                          "conclusion": "skipped" if n in skipped else "success"})
        t += timedelta(seconds=d)
    return {"id": run_id * 10 + (1 if "8" not in str(label) else 2),
            "run_id": run_id, "name": name, "status": "completed",
            "conclusion": conclusion, "started_at": _iso(t0),
            "completed_at": _iso(t0 + timedelta(seconds=dur)),
            "labels": label if isinstance(label, list) else [label], "steps": out_steps}


_SLOW = [148, 150, 152, 149, 151, 147, 153, 150]
_FAST = [88, 90, 92, 89, 91, 87, 93, 90]


def _runs(slow_label="ubuntu-latest", fast_label="ubuntu-latest-8-cores",
          slow=_SLOW, fast=_FAST, extra=None, slow_kw=None, fast_kw=None):
    out = []
    for i in range(max(len(slow), len(fast))):
        run = []
        if i < len(slow):
            run.append(_job("bench", slow_label, slow[i], 100 + i, **(slow_kw or {})))
        if i < len(fast):
            run.append(_job("bench", fast_label, fast[i], 100 + i, **(fast_kw or {})))
        if extra:
            run.extend(extra(i))
        out.append(run)
    return out


def _alt_runs(slow_label="ubuntu-latest", fast_label="ubuntu-latest-8-cores",
              slow=_SLOW, fast=_FAST, extra=None, fast_start=None):
    """One leg per run (no runner matrix). By default the two labels interleave:
    the slow label on even run ids, the fast on odd ones. `fast_start` instead
    starts the fast population at that run id (hours after the first run), the
    shape of a `runs-on` switch."""
    out = []
    for i, d in enumerate(slow):
        out.append([_job("bench", slow_label, d, 100 + 2 * i)])
    for i, d in enumerate(fast):
        rid = (101 + 2 * i) if fast_start is None else fast_start + i
        out.append([_job("bench", fast_label, d, rid)])
    if extra:
        for run in out:
            run.extend(extra(run[0]["run_id"]))
    return out


def _a1(runs, **kw):
    crit = cr._critical_path(runs)
    withheld: dict = {}
    cands: list = []
    multi: set = set()
    found = cr._detect_opt81_measured_runner_gap(
        _WF, runs, crit, 0, withheld=withheld, withheld_candidates=cands,
        multi_label_jobs=multi, **kw)
    return found, withheld, cands, multi, crit


# =============================================================================
# The runner-class taxonomy
# =============================================================================

@pytest.mark.parametrize("label,want", [
    # (class, operating system, processor architecture, size tier)
    ("ubuntu-latest", ("github-standard", "linux", "x64", "")),
    ("ubuntu-24.04", ("github-standard", "linux", "x64", "")),
    ("ubuntu-22.04", ("github-standard", "linux", "x64", "")),
    ("ubuntu-24.04-arm", ("github-standard", "linux", "arm64", "")),
    ("ubuntu-slim", ("github-slim", "linux", "x64", "")),
    ("windows-latest", ("github-standard", "windows", "x64", "")),
    ("windows-2022", ("github-standard", "windows", "x64", "")),
    ("windows-11-arm", ("github-standard", "windows", "arm64", "")),
    # macOS: plain 14/15/26/latest images are Apple silicon, 13 and older Intel
    ("macos-latest", ("github-standard", "macos", "arm64", "")),
    ("macos-14", ("github-standard", "macos", "arm64", "")),
    ("macos-15", ("github-standard", "macos", "arm64", "")),
    ("macos-13", ("github-standard", "macos", "x64", "")),
    ("macos-15-intel", ("github-standard", "macos", "x64", "")),
    ("ubuntu-latest-8-cores", ("github-larger", "linux", "x64", "8")),
    ("ubuntu-24.04-16core", ("github-larger", "linux", "x64", "16")),
    ("ubuntu-24.04-arm-4-cores", ("github-larger", "linux", "arm64", "4")),
    ("windows-latest-8-cores", ("github-larger", "windows", "x64", "8")),
    # macOS `-large` sizes are Intel, `-xlarge` sizes are Apple silicon
    ("macos-14-xlarge", ("github-larger", "macos", "arm64", "xlarge")),
    ("macos-14-large", ("github-larger", "macos", "x64", "large")),
    ("macos-latest-large", ("github-larger", "macos", "x64", "large")),
    ("starsling-ubuntu-24.04", ("starsling", "linux", "x64", "")),
    ("starsling-ubuntu-24.04-8", ("starsling", "linux", "x64", "8")),
    ("starsling-ubuntu-24.04-arm", ("starsling", "linux", "arm64", "")),
    ("starsling-windows-2022", ("starsling", "windows", "x64", "")),
    ("starsling-macos-14", ("starsling", "macos", "x64", "")),
    ("windows-11-8-cores", ("github-larger", "windows", "x64", "8")),
    ("windows-11-arm-4-cores", ("github-larger", "windows", "arm64", "4")),
    # StarSling's `linux` spelling, an `arm64` spelling, and a mixed-case size
    ("starsling-linux-24.04-8", ("starsling", "linux", "x64", "8")),
    ("starsling-ubuntu-24.04-arm64", ("starsling", "linux", "arm64", "")),
    ("macos-14-XLarge", ("github-larger", "macos", "arm64", "xlarge")),
    # `self-hosted` makes a set unclassifiable unless it carries a StarSling
    # label; label matching ignores case
    ("self-hosted ubuntu-latest", None),
    ("self-hosted starsling-ubuntu-24.04", ("starsling", "linux", "x64", "")),
    ("Self-Hosted Ubuntu-Latest", None),
    ("UBUNTU-LATEST", ("github-standard", "linux", "x64", "")),
    ("linux self-hosted x64", None),
    ("self-hosted", None),
    ("my-big-box", None),
    ("", None),
    (None, None),
    # two labels that classify differently are not resolved by picking one
    ("ubuntu-latest windows-latest", None),
])
def test_opt81_runner_class_taxonomy(label, want):
    assert cr._opt81_runner_class(label) == want


def test_opt81_taxonomy_is_a_named_table_with_a_verifier_twin():
    vr = _load_verify_report()
    eng = [(rx.pattern, rx.flags, c, o) for rx, c, o in cr._OPT81_RUNNER_CLASSES]
    twin = [(rx.pattern, rx.flags, c, o) for rx, c, o in vr._VR_OPT81_RUNNER_CLASSES]
    assert eng == twin
    eng_arch = [(rx.pattern, rx.flags, a) for rx, a in cr._OPT81_RUNNER_ARCH]
    twin_arch = [(rx.pattern, rx.flags, a) for rx, a in vr._VR_OPT81_RUNNER_ARCH]
    assert eng_arch == twin_arch
    for label in ("ubuntu-latest", "ubuntu-latest-8-cores", "starsling-ubuntu-24.04",
                  "self-hosted linux", "macos-14-xlarge", "macos-14-large", "macos-14",
                  "ubuntu-24.04-arm", "starsling-ubuntu-24.04-arm-8", "ubuntu-slim",
                  "starsling-linux-24.04-8", "starsling-ubuntu-24.04-arm64",
                  "macos-14-XLarge"):
        assert cr._opt81_runner_class(label) == vr._vr_opt81_runner_class(label)


def test_opt81_verifier_constants_stay_coupled_to_the_engine():
    vr = _load_verify_report()
    assert vr._VR_OPT81_MIN_SAMPLES_PER_LABEL == cr._OPT81_MIN_SAMPLES_PER_LABEL == 8
    assert vr._VR_OPT81_MIN_GAP_S == cr._OPT81_MIN_GAP_S == 30.0
    assert vr._VR_OPT81_MIN_GAP_FRAC == cr._OPT81_MIN_GAP_FRAC == 0.25
    assert vr._VR_OPT81_COVERED_FRAC == cr._OPT81_COVERED_FRAC
    assert vr._VR_OPT81_CACHE_LEVER_MIN_S == cr._OPT81_CACHE_LEVER_MIN_S
    assert vr._VR_OPT81_CHEAPER_STRUCTURAL == cr._OPT81_CHEAPER_STRUCTURAL
    assert vr._VR_OPT81_PRESTART_PATTERNS == cr._PRESTART_AXIS_PATTERNS
    assert (vr._VR_OPT81_DISCLOSURE == cr._OPT81_DISCLOSURE == bp._OPT81_DISCLOSURE)
    assert (vr._VR_OPT81_RUNNER_MIN_UNKNOWN == bp._OPT81_RUNNER_MIN_UNKNOWN
            == cr._OPT81_RUNNER_MIN_UNKNOWN)
    assert vr._VR_OPT81_INSTALL_SENTENCE in bp._OPT81_A2_OPTION_STARSLING
    assert vr._VR_OPT81_LOG_CHECKED == bp._OPT81_A2_LOG_CHECKED
    assert (vr._VR_OPT81_WITHHELD_DOC_KEY == cr._OPT81_WITHHELD_DOC_KEY
            == bp._OPT81_WITHHELD_DOC_KEY)


def test_opt81_larger_runner_option_does_not_deny_the_naming_the_taxonomy_reads():
    """The advisory's larger-runner option must not say an organisation-chosen
    name can never be sized: the taxonomy classifies GitHub's documented
    `<image>-<N>core(s)` naming. The card and the catalog say the same."""
    assert cr._opt81_runner_class("ubuntu-24.04-16core")[0] == "github-larger"
    assert cr._opt81_runner_class("windows-2022-16-cores")[0] == "github-larger"
    sentence = ("A runner name the organisation chooses cannot be classified by size "
                "by this audit unless it follows GitHub's `<image>-<N>core(s)` naming.")
    assert bp._OPT81_A2_OPTION_LARGER.endswith(sentence), bp._OPT81_A2_OPTION_LARGER
    assert sentence in _CATALOG.read_text(encoding="utf-8")


# =============================================================================
# A1 — measured
# =============================================================================

def test_opt81_a1_fires_on_the_same_job_on_two_classes():
    found, withheld, cands, multi, _crit = _a1(_runs())
    assert len(found) == 1, (withheld, cands)
    f = found[0]
    fr = f["faster_runner"]
    assert f["pattern"] == "OPT81" and fr["half"] == "A1"
    assert fr["slow"] == {"label": "ubuntu-latest", "class": "github-standard",
                          "os": "linux", "arch": "x64", "size": "", "p50_s": 150.0, "n": 8,
                          "first_run_at": "2026-06-05T04:00:00Z",
                          "last_run_at": "2026-06-05T11:00:00Z"}
    assert fr["fast"] == {"label": "ubuntu-latest-8-cores", "class": "github-larger",
                          "os": "linux", "arch": "x64", "size": "8", "p50_s": 90.0, "n": 8,
                          "first_run_at": "2026-06-05T04:00:00Z",
                          "last_run_at": "2026-06-05T11:00:00Z"}
    assert fr["gap_s"] == 60.0 and fr["floor_s"] == 37.5
    assert fr["step_names"] == _STEPS and len(fr["rows"]) == 16
    assert f["runner_min_saving"] is None
    assert fr["runner_min_saving_basis"] == cr._OPT81_RUNNER_MIN_UNKNOWN
    assert f["sizing_basis"] == "measured"
    assert cr._OPT81_DISCLOSURE in f["evidence"]
    assert "runs this repository already made" in f["evidence"]
    assert ("bench" and _WF) and (_WF, "bench") in multi
    assert cands == []


def test_opt81_a1_credits_the_gap_only_on_the_slow_long_pole():
    # One leg per run, interleaved; the job alone in its workflow is the long pole
    # and runs on the slow label most (9 vs 8), floor 0 -> the whole gap credits.
    found, *_ = _a1(_alt_runs(slow=_SLOW + [150]))
    assert found[0]["wall_clock_p50_s"] == 60.0
    fr = found[0]["faster_runner"]
    assert fr["credited_pre_cascade_s"] == 60.0 and fr["runner_matrix"] is False
    # Beside a 130s job the headroom is 150 - 130 = 20s: pre-capped there.
    found, *_ = _a1(_alt_runs(slow=_SLOW + [150],
                              extra=lambda rid: [_job("lint", "ubuntu-latest", 130, rid)]))
    assert found[0]["wall_clock_p50_s"] == 20.0, found[0]["faster_runner"]
    # Not the long pole: measured, stated, not credited.
    found, *_ = _a1(_alt_runs(slow=_SLOW + [150],
                              extra=lambda rid: [_job("e2e", "ubuntu-latest", 400, rid)]))
    assert found[0]["wall_clock_p50_s"] == 0.0
    assert "not this workflow's long pole" in found[0]["faster_runner"]["credit_reason"]
    # The long pole, but it runs on the FAST label most: its median does not move.
    found, *_ = _a1(_alt_runs(fast=_FAST + [90, 91]))
    assert found[0]["wall_clock_p50_s"] == 0.0
    assert "already runs on the faster" in found[0]["faster_runner"]["credit_reason"]


def test_opt81_a1_an_exact_count_tie_is_not_dominant():
    """8 runs on each label, one leg per run: neither label is the one the job
    runs on most, whatever the labels' alphabetical order."""
    for kw in ({}, {"fast_label": "starsling-ubuntu-24.04"}):
        found, *_ = _a1(_alt_runs(**kw))
        assert found[0]["wall_clock_p50_s"] == 0.0, kw
        assert "neither label" in found[0]["faster_runner"]["credit_reason"], kw


def _pole_population_cases():
    """Two shapes where the success-only, classified-only A1 split names the slow
    label as the one the job runs on most, but the pole's p50 (`_critical_path`'s
    dominant label, counted over every conclusion and every label) is not it."""
    base = _SLOW + [150]  # 9 slow successes vs 8 fast: n_slow > n_fast
    # (1) 20 runs on an unclassifiable self-hosted set at 60s set the pole's p50.
    self_hosted = _alt_runs(slow=base) + [
        [_job("bench", ["self-hosted", "linux"], 60, 300 + i)] for i in range(20)]
    # (2) 5 FAILED fast runs make the fast label the pole's population (13 vs 9).
    failed_fast = _alt_runs(slow=base) + [
        [_job("bench", "ubuntu-latest-8-cores", 90, 400 + i, conclusion="failure")]
        for i in range(5)]
    return [("linux self-hosted", self_hosted),
            ("ubuntu-latest-8-cores", failed_fast)]


@pytest.mark.parametrize("dominant,runs", _pole_population_cases(),
                         ids=["unclassified-self-hosted-dominant",
                              "failed-fast-runs-dominant"])
def test_opt81_a1_credits_only_when_the_slow_label_is_the_poles_population(dominant, runs):
    found, withheld, _cands, _multi, crit = _a1(runs)
    assert crit["job_runner"]["bench"] == dominant, crit["job_runner"]
    assert len(found) == 1, withheld
    f = found[0]
    fr = f["faster_runner"]
    assert fr["slow"]["label"] == "ubuntu-latest" and fr["job_is_workflow_long_pole"]
    # The pole's p50 describes `dominant`, not the slow label: moving the slow
    # label's runs does not move it, so nothing is credited.
    assert f["wall_clock_p50_s"] == 0.0, fr
    assert fr["credited_pre_cascade_s"] == 0.0, fr
    assert fr["pole_runner_label"] == dominant
    assert "not the population the pole's median describes" in fr["credit_reason"]
    assert withheld.get("slow_label_is_not_the_poles_population") == 1, withheld


def test_opt81_verifier_rederives_the_poles_population_from_timing():
    vr = _load_verify_report()
    found, *_ = _a1(_alt_runs(slow=_SLOW + [150]))
    f = copy.deepcopy(found[0])
    f["id"] = "f9"
    assert f["faster_runner"]["pole_runner_label"] == "ubuntu-latest"
    doc = {"per_workflow_timing": {_WF: {"job_runner": {"bench": "ubuntu-latest"}}}}
    assert vr._opt81_a1_rederived(f, doc) == []
    # The timing says the pole runs on another label: the credit cannot stand.
    other = {"per_workflow_timing": {_WF: {"job_runner": {"bench": "linux self-hosted"}}}}
    assert any("population" in p for p in vr._opt81_a1_rederived(f, other))
    # A stamped pole label that is not the slow label reddens on its own.
    g = copy.deepcopy(f)
    g["faster_runner"]["pole_runner_label"] = "ubuntu-latest-8-cores"
    assert any("population" in p for p in vr._opt81_a1_rederived(g))


def test_opt81_a1_runner_matrix_is_measured_but_never_credited():
    """Both labels in every run (a runner matrix under one display name), the
    slow label sorting AFTER the fast one. Every run executes both legs, so the
    finding is stated with matrix wording and no wall-clock credit."""
    found, withheld, cands, *_ = _a1(_runs(fast_label="starsling-ubuntu-24.04"))
    assert len(found) == 1, withheld
    fr = found[0]["faster_runner"]
    assert fr["runner_matrix"] is True
    assert found[0]["wall_clock_p50_s"] == 0.0
    assert "every run executes both legs" in fr["credit_reason"], fr["credit_reason"]
    assert "already runs on the faster" not in fr["credit_reason"]


def test_opt81_a1_withholds_a_runner_switch_as_not_interleaved():
    """8 runs on the slow label, then (about 100 days later) 8 on the fast one:
    the shape of a commit that edited `runs-on`. Code changed in between could
    explain the gap, so it is held back and listed."""
    found, withheld, cands, *_ = _a1(_alt_runs(fast_start=2500))
    assert found == []
    assert withheld.get("not_interleaved") == 1, withheld
    assert cands == [{"workflow_file": _WF, "job": "bench", "gate": "not_interleaved",
                      "half": "A1"}]
    assert "not_interleaved" in bp._OPT81_WITHHOLD_PHRASES


def test_opt81_a1_interleaved_single_leg_runs_stamp_their_time_ranges():
    found, *_ = _a1(_alt_runs())
    assert len(found) == 1
    fr = found[0]["faster_runner"]
    assert fr["slow"]["first_run_at"] == "2026-06-05T04:00:00Z"
    assert fr["fast"]["last_run_at"] == "2026-06-05T19:00:00Z"
    assert all(r.get("at") for r in fr["rows"])


def test_opt81_verifier_reddens_on_an_uninterleaved_or_matrix_credited_a1():
    vr = _load_verify_report()
    f = copy.deepcopy(_a1(_alt_runs())[0][0])
    f["id"] = "f8"
    assert vr._opt81_a1_rederived(f) == []
    for r in f["faster_runner"]["rows"]:
        if r["label"] == "ubuntu-latest-8-cores":
            r["at"] = "2027-01-01T00:00:00Z"
    assert any("different periods" in p for p in vr._opt81_a1_rederived(f))
    # a matrix finding (both legs in every run) carrying wall-clock credit
    f = copy.deepcopy(_a1_finding())
    f["faster_runner"]["credited_pre_cascade_s"] = 30.0
    f["wall_clock_p50_s"] = 30.0
    assert any("runner matrix" in p for p in vr._opt81_a1_rederived(f))


@pytest.mark.parametrize("kw,gate", [
    ({"slow": _SLOW[:7]}, "fewer_than_min_samples_on_two_labels"),
    ({"slow_label": ["self-hosted", "linux"]}, "runner_label_not_classifiable_by_size"),
    ({"fast_kw": {"skipped": ("Run actions/checkout@v4",)}}, "step_lists_differ"),
    ({"fast_kw": {"steps": _STEPS[:2] + ["Warm cache"] + _STEPS[2:]}},
     "step_lists_differ"),
])
def test_opt81_a1_withholds_and_lists_the_candidate(kw, gate):
    found, withheld, cands, multi, _ = _a1(_runs(**kw))
    assert found == []
    assert withheld.get(gate) == 1, withheld
    assert cands == [{"workflow_file": _WF, "job": "bench", "gate": gate, "half": "A1"}]
    assert (_WF, "bench") in multi


@pytest.mark.parametrize("kw,gate", [
    ({"slow_label": "ubuntu-22.04", "fast_label": "ubuntu-24.04"}, "same_runner_class"),
    ({"slow_label": "windows-latest", "fast_label": "ubuntu-latest-8-cores"},
     "different_operating_system"),
    # Same OS, different processor architecture: the gap would measure ARM vs x86.
    ({"slow_label": "ubuntu-24.04-arm", "fast_label": "ubuntu-latest-8-cores"},
     "different_architecture"),
    ({"slow_label": "ubuntu-latest", "fast_label": "starsling-ubuntu-24.04-arm"},
     "different_architecture"),
    # GitHub's macOS `-large` is Intel; plain `macos-14` is Apple silicon.
    ({"slow_label": "macos-14", "fast_label": "macos-14-large"}, "different_architecture"),
])
def test_opt81_a1_design_exclusions_are_verdicts_not_held_back(kw, gate):
    """Two operating systems, two architectures, or one runner class are not this
    lever by design: counted, never listed on the held-back row."""
    found, withheld, cands, multi, _ = _a1(_runs(**kw))
    assert found == []
    assert withheld.get(gate) == 1, withheld
    assert gate in cr._OPT81_VERDICT_GATES
    assert cands == []


@pytest.mark.parametrize("slow_label,fast_label", [
    ("ubuntu-latest-4-cores", "ubuntu-latest-16-cores"),
    ("ubuntu-slim", "ubuntu-latest"),
    ("starsling-ubuntu-24.04", "starsling-ubuntu-24.04-8"),
    ("macos-14", "macos-14-xlarge"),
])
def test_opt81_a1_two_size_tiers_of_one_vendor_are_compared(slow_label, fast_label):
    found, withheld, cands, *_ = _a1(_runs(slow_label=slow_label, fast_label=fast_label))
    assert len(found) == 1, withheld
    fr = found[0]["faster_runner"]
    assert fr["slow"]["arch"] == fr["fast"]["arch"]
    assert (fr["slow"]["class"], fr["slow"]["size"]) != (fr["fast"]["class"],
                                                         fr["fast"]["size"])


def test_opt81_a1_gap_below_floor_is_a_verdict_not_a_held_back_candidate():
    # 150 vs 125: 25s, below max(30, 37.5).
    found, withheld, cands, _m, _ = _a1(_runs(fast=[s - 25 for s in _SLOW]))
    assert found == [] and withheld.get("gap_below_floor") == 1 and cands == []
    # 400 vs 320: 80s, but the floor is 25% of 400 = 100s.
    found, withheld, *_ = _a1(_runs(slow=[400] * 8, fast=[320] * 8))
    assert found == [] and withheld.get("gap_below_floor") == 1


def test_opt81_a1_three_qualifying_labels_withhold():
    runs = _runs(extra=lambda i: [_job("bench", "starsling-ubuntu-24.04", 70, 100 + i)])
    found, withheld, cands, *_ = _a1(runs)
    assert found == [] and withheld.get("more_than_two_qualifying_runner_labels") == 1


def test_opt81_a1_drops_an_unclassifiable_population_and_still_compares():
    runs = _runs(extra=lambda i: ([_job("bench", ["self-hosted", "linux"], 300, 100 + i)]
                                  if i < 3 else []))
    found, withheld, cands, *_ = _a1(runs)
    assert len(found) == 1
    assert found[0]["faster_runner"]["excluded_labels"] == {"linux self-hosted": 3}
    assert withheld.get("unclassifiable_label_population_excluded") == 1


def test_opt81_a1_compares_starsling_against_github_hosted():
    found, *_ = _a1(_runs(fast_label="starsling-ubuntu-24.04"))
    assert len(found) == 1
    assert found[0]["faster_runner"]["fast"]["class"] == "starsling"


def test_opt81_a1_ignores_failed_and_single_label_jobs():
    runs = _runs(slow_kw={"conclusion": "failure"})
    found, withheld, cands, multi, _ = _a1(runs)
    assert found == [] and withheld.get("job_ran_on_one_runner_label") == 1
    assert cands == [] and multi == set()


def test_opt81_crash_tripwire_on_malformed_jobs():
    """Third-party API payloads: None entries, missing labels, steps None, bad
    timestamps. The detector counts and moves on; it never raises."""
    bad = [[None, {"name": "x"}, {"name": "bench", "conclusion": "success",
                                   "started_at": "nope", "completed_at": None},
            {"name": "bench", "conclusion": "success", "labels": None,
             "started_at": "2026-06-01T00:00:00Z", "completed_at": "2026-06-01T00:02:00Z",
             "steps": None},
            {"name": "bench", "conclusion": "success", "labels": ["ubuntu-latest"],
             "started_at": "2026-06-01T00:00:00Z", "completed_at": "2026-06-01T00:02:00Z",
             "steps": [None, 3, {"name": None}]}], []]
    assert cr._detect_opt81_measured_runner_gap(_WF, bad, {}, 0, withheld={}) == []
    assert cr._detect_opt81_measured_runner_gap(_WF, [], {}, 0) == []
    # A2 on an empty / shapeless crit
    assert cr._detect_opt81_runner_size_advisory(
        _WF, bad, {}, True, [], [], [], set(), 0, withheld={}) == []


# =============================================================================
# A2 — advisory, the lever of last resort
# =============================================================================

_CI = ".github/workflows/ci.yml"


def _a2_runs(label="ubuntu-latest", work="Run tests", n=6):
    return [[_job("test", label, 197, 300 + i, work=work)] for i in range(n)]


def _a2(findings=None, *, runs=None, is_pr=True, poles=None, unc=None, multi=None):
    runs = runs if runs is not None else _a2_runs()
    crit = cr._critical_path(runs)
    poles = poles if poles is not None else [{"workflow_file": _CI, "job": "test",
                                              "check": "CI / test"}]
    withheld: dict = {}
    cands: list = []
    out = cr._detect_opt81_runner_size_advisory(
        _CI, runs, crit, is_pr, poles, findings or [], unc or [], multi or set(), 40,
        withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


def _f(pat, wc=0.0, job="test", wf=_CI, **kw):
    return {"id": f"x{pat}", "pattern": pat, "workflow_file": wf, "affected_jobs": [job],
            "wall_clock_p50_s": wc, **kw}


def test_opt81_a2_fires_on_a_compute_pole_with_no_cheaper_lever():
    out, withheld, cands = _a2([_f("OPT75", structural=True), _f("OPT45")])
    assert len(out) == 1, withheld
    a2 = out[0]
    fr = a2["faster_runner"]
    assert fr["half"] == "A2" and fr["benchmark_required"] is True
    assert a2["advisory"] is True
    assert a2["wall_clock_p50_s"] is None and a2["runner_min_saving"] is None
    assert fr["dominant_step"] == "Run tests" and fr["dominant_category"] == "test"
    assert fr["runner_label"] == "ubuntu-latest" and fr["runner_class"] == "github-standard"
    assert a2["risk"] and a2["guardrail"] and a2["rollout"]
    assert "OPT81" not in cr._SIZING
    levers = [c["lever"] for c in fr["cheaper_levers_checked"]]
    assert any("OPT70" in lv or "structural" in lv for lv in levers)
    assert any("sharding" in lv for lv in levers)
    assert any("cache" in lv for lv in levers)
    assert any("OPT75" in c["why"] for c in fr["cheaper_levers_checked"])
    assert cr._OPT81_DISCLOSURE in a2["evidence"]
    vr = _load_verify_report()
    assert not vr._VR_OPT81_NUMBER_RE.search(a2["evidence"]), a2["evidence"]
    assert cands == []


@pytest.mark.parametrize("findings,gate", [
    ([_f("OPT70", structural=True)], "a2_cheaper_structural_lever_on_the_pole"),
    ([_f("OPT71", structural=True)], "a2_cheaper_structural_lever_on_the_pole"),
    ([_f("OPT72", structural=True)], "a2_cheaper_structural_lever_on_the_pole"),
    ([_f("OPT73", structural=True)], "a2_cheaper_structural_lever_on_the_pole"),
    # trust-boundary cold work and per-file test isolation: cheaper than hardware
    ([_f("OPT74")], "a2_cheaper_structural_lever_on_the_pole"),
    ([_f("OPT78")], "a2_cheaper_structural_lever_on_the_pole"),
    # a credited lever covering the pole: at least half its 197s median
    ([_f("OPT17", wc=98.5)], "a2_credited_lever_covers_the_pole"),
    ([_f("OPT24", wc=0.5)], "a2_sharding_lever_on_the_pole"),
    ([_f("OPT79", wc=30.0)], "a2_cache_lever_on_the_pole"),
])
def test_opt81_a2_is_suppressed_by_every_cheaper_lever(findings, gate):
    out, withheld, cands = _a2(findings)
    assert out == [] and withheld.get(gate) == 1, withheld
    assert cands == []  # a verdict, not "could not tell"


def test_opt81_a2_credited_lever_below_half_the_pole_does_not_suppress():
    out, withheld, _ = _a2([_f("OPT17", wc=98.0)])
    assert len(out) == 1, withheld


def test_opt81_a2_a_pre_start_or_other_job_finding_does_not_suppress():
    out, *_ = _a2([_f("OPT43", wc=190.0), _f("OPT72", job="lint", structural=True),
                   _f("OPT72", wf=".github/workflows/other.yml", structural=True)])
    assert len(out) == 1


def test_opt81_a2_uncredited_opt79_cache_on_the_pole_suppresses():
    unc = [{"workflow_file": _CI, "job": "test", "waste_s": 31.0}]
    out, withheld, _ = _a2(unc=unc)
    assert out == [] and withheld.get("a2_cache_lever_on_the_pole") == 1
    out, *_ = _a2(unc=[{"workflow_file": _CI, "job": "test", "waste_s": 29.0}])
    assert len(out) == 1


@pytest.mark.parametrize("kw,gate", [
    ({"is_pr": False}, "a2_not_a_pull_request_workflow"),
    ({"poles": []}, "a2_not_on_the_merge_gating_critical_path"),
    ({"multi": {(_CI, "test")}}, "a2_measured_runner_data_exists"),
    ({"runs": _a2_runs(label="ubuntu-latest-8-cores")},
     "a2_not_on_a_standard_github_hosted_label"),
    ({"runs": _a2_runs(label="starsling-ubuntu-24.04")},
     "a2_not_on_a_standard_github_hosted_label"),
    ({"runs": _a2_runs(label="ubuntu-slim")},
     "a2_not_on_a_standard_github_hosted_label"),
    ({"runs": _a2_runs(work="npm ci")}, "a2_dominant_step_is_not_compute"),
    ({"runs": _a2_runs(work="Wait for deploy tests")},
     "a2_dominant_step_waits_or_moves_bytes"),
])
def test_opt81_a2_shape_gates(kw, gate):
    out, withheld, cands = _a2(**kw)
    assert out == [] and withheld.get(gate) == 1, withheld
    assert cands == []


def test_opt81_a2_self_hosted_pole_is_a_verdict_not_a_held_back_row():
    """A2 can never fire on a self-hosted label, so such a pole is a verdict
    (counted, not listed). And the shape gates run first: a self-hosted pole
    whose dominant step waits is reported as that, not as a runner question."""
    out, withheld, cands = _a2(runs=_a2_runs(label=["self-hosted", "linux"],
                                             work="Wait for deploy tests"))
    assert out == [] and cands == []
    assert withheld == {"a2_dominant_step_waits_or_moves_bytes": 1}, withheld
    out, withheld, cands = _a2(runs=_a2_runs(label=["self-hosted", "linux"]))
    assert out == [] and cands == []
    assert withheld == {"a2_not_on_a_standard_github_hosted_label": 1}, withheld


def test_opt81_a2_evidence_says_exactly_what_was_checked():
    out, *_ = _a2([_f("OPT75", structural=True)])
    ev = out[0]["evidence"]
    assert "A different or larger runner class is the lever left (benchmark required)" in ev, ev
    assert "no credited fix on this job at half its median or more" in ev, ev
    assert "A bigger runner" not in ev
    checked = out[0]["faster_runner"]["cheaper_levers_checked"]
    assert sorted(checked[0]["patterns"]) == sorted(cr._OPT81_CHEAPER_STRUCTURAL)
    for pat in sorted(cr._OPT81_CHEAPER_STRUCTURAL):
        assert pat in checked[0]["lever"], checked[0]


def test_opt81_a2_matches_the_pole_and_its_levers_by_workflow_and_job():
    # the pole match reads the workflow: the same job name in another workflow's
    # critical path does not put this one on it
    out, withheld, _ = _a2(poles=[{"workflow_file": ".github/workflows/other.yml",
                                   "job": "test"}])
    assert out == [] and withheld == {"a2_not_on_the_merge_gating_critical_path": 1}
    # a pole job named with different punctuation is the same job (token match)
    out, *_ = _a2(poles=[{"workflow_file": _CI, "job": "Test"}])
    assert len(out) == 1
    out, withheld, _ = _a2([_f("OPT72", job="(test)", structural=True)])
    assert out == [] and withheld == {"a2_cheaper_structural_lever_on_the_pole": 1}
    # an uncredited net-negative cache on another workflow or another job does not
    # hold the advisory back
    out, *_ = _a2(unc=[{"workflow_file": ".github/workflows/other.yml", "job": "test",
                        "waste_s": 31.0}])
    assert len(out) == 1
    out, *_ = _a2(unc=[{"workflow_file": _CI, "job": "lint", "waste_s": 31.0}])
    assert len(out) == 1


def test_opt81_jobs_match_is_exact_or_same_tokens():
    assert cr._opt81_jobs_match("test", "test")
    assert cr._opt81_jobs_match("Build (linux)", "build-linux")
    assert not cr._opt81_jobs_match("build", "build-linux")
    assert not cr._opt81_jobs_match("", "")


def test_opt81_a2_does_not_fire_when_a_credited_lever_covers_the_pole():
    """The owner's named case: a credited fix already shortens this pole by at
    least half, so a bigger runner is not the next move."""
    out, withheld, _ = _a2([_f("OPT28", wc=120.0, sizing_basis="measured")])
    assert out == []
    assert withheld == {"a2_credited_lever_covers_the_pole": 1}


# =============================================================================
# Rendering: disclosure, install prerequisite, no number, vendor rule
# =============================================================================

def _a1_finding():
    f = _a1(_runs())[0][0]
    f["id"] = "f7"
    return f


def _a1_credited_finding():
    """A1 that credits: one leg per run (no matrix), interleaved in time, the job
    run on the slow label strictly more often (9 vs 8), alone in its workflow."""
    f = _a1(_alt_runs(slow=_SLOW + [150]))[0][0]
    f["id"] = "f7"
    assert f["wall_clock_p50_s"] == 60.0, f["faster_runner"]["credit_reason"]
    return f


def _a2_finding():
    f = _a2([_f("OPT75", structural=True)])[0][0]
    f["id"] = "f9"
    return f


def test_opt81_a2_recipe_names_the_install_prerequisite_and_no_number():
    vr = _load_verify_report()
    card = "\n".join(bp._opt81_card(_a2_finding(), "https://github.com/x/y/blob/m/c.md"))
    assert "installing the StarSling GitHub app" in card
    assert "the benchmark can only be run after installing the StarSling GitHub app" in card
    assert "change only the job's `runs-on`" in card
    assert card.count(cr._OPT81_DISCLOSURE) == 2          # the card and its prompt
    m = vr._VR_OPT81_NUMBER_RE.search(card)
    assert m is None, m.group(0)
    # The prompt fence carries the recipe too, so the agent never needs the card.
    fence = card[card.index("```text"):]
    assert "installing the StarSling GitHub app" in fence
    assert cr._OPT81_DISCLOSURE in fence


def test_opt81_a2_held_back_by_a_matched_leaf_renders_no_recipe():
    card = "\n".join(bp._opt81_card(_a2_finding(), "u", held_back_by="vitest-isolate-pool"))
    assert "advisory held back" in card and "vitest-isolate-pool" in card
    assert "Option 1" not in card and "Option 2" not in card
    assert cr._OPT81_DISCLOSURE in card


def test_opt81_a1_card_states_both_distributions_and_the_disclosure():
    card = "\n".join(bp._opt81_card(_a1_finding(), "u"))
    assert "at p50 150s" in card and "at p50 90s" in card
    assert "8 time(s) on `ubuntu-latest`" in card
    assert "runs this repository already made" in card
    assert card.count(cr._OPT81_DISCLOSURE) >= 2
    assert "rate table" in card


# The vendor-name deny-list lives in maintainers/ci-speedup/tests/
# test_opt81_vendor_guard.py so it never ships in the installed skill; the
# shipped half is the vendor-neutral domain rule below.
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9-]+\.)+(?:com|io|dev|net|org|app|cloud|sh|so|co|ai|run|build|tech"
    r"|xyz|gg|me|page|us|eu|cc|tv|works|systems|software|tools|ci))\b",
    re.I)
_ALLOWED_DOMAINS = ("github.com", "starsling.dev")


def _foreign_domains(text: str) -> list[str]:
    """Every domain in `text` other than github.com / starsling.dev (and their
    subdomains). The guard and its non-vacuous proof share this one scan."""
    out = []
    for dom in _DOMAIN_RE.findall(text):
        d = dom.lower()
        if not (d in _ALLOWED_DOMAINS or any(d.endswith("." + a) for a in _ALLOWED_DOMAINS)):
            out.append(dom)
    return out


def _opt81_catalog_entry() -> str:
    text = _CATALOG.read_text(encoding="utf-8")
    m = re.search(r"### OPT81 — .*?(?=\n### OPT|\n## Category|\Z)", text, re.S)
    assert m, "the catalog has no ### OPT81 entry"
    return m.group(0)


def _opt81_texts() -> dict[str, str]:
    a1, a2 = _a1_finding(), _a2_finding()
    url = "https://github.com/starslingdev/skills/blob/main/skills/ci-speedup/references/optimization-patterns.md"
    return {
        "a1 finding": json.dumps(a1),
        "a2 finding": json.dumps(a2),
        "a1 card + prompt": "\n".join(bp._opt81_card(a1, url)),
        "a2 card + prompt": "\n".join(bp._opt81_card(a2, url)),
        "a2 held back": "\n".join(bp._opt81_card(a2, url, held_back_by="x")),
        "unrouted section": "\n".join(bp._opt81_unrouted_block([a1, a2], url)),
        # The generic pole prompt's OPT81 lead + block, and every pointer to the card.
        "generic prompt (OPT81)": bp._build_generic_agent_prompt(
            _pole("bench"), [], None, "o/r", None, 1, 1, None, data_driven=[a1]),
        "card pointers": "\n".join(bp._dd_where(p, entry=e)
                                   for p in (["OPT81"], ["OPT24", "OPT81"])
                                   for e in (False, True)),
        "held-back phrases": json.dumps(bp._OPT81_WITHHOLD_PHRASES),
        "catalog entry": _opt81_catalog_entry(),
    }


def test_opt81_text_names_no_domain_but_github_and_starsling():
    """Owner rule: no OPT81 text, anywhere it renders, names any domain but
    github.com and starsling.dev."""
    for where, text in _opt81_texts().items():
        assert not _foreign_domains(text), f"{where} names a foreign domain: {_foreign_domains(text)}"


def test_opt81_domain_guard_is_not_vacuous():
    assert _foreign_domains("see https://example.org/x") == ["example.org"]
    assert _foreign_domains("runners at fast.xyz") == ["fast.xyz"]
    assert _foreign_domains("https://docs.github.com/x and https://starsling.dev") == []


def test_opt81_catalog_entry_carries_the_disclosure_and_no_dollars():
    entry = _opt81_catalog_entry()
    assert cr._OPT81_DISCLOSURE in entry.replace("\n", " ") or \
        " ".join(cr._OPT81_DISCLOSURE.split()) in " ".join(entry.split())
    assert "$" not in entry
    assert "OPT66" in entry  # says plainly the dollar pattern is not revived
    assert "installing the StarSling GitHub app" in " ".join(entry.split())


def test_opt81_no_dollars_anywhere_it_renders():
    for where, text in _opt81_texts().items():
        assert not re.search(r"\$\s*\d|\bUSD\b|\bdollar", text, re.I), where


# =============================================================================
# The verifier re-derives A1 and holds A2 to no number
# =============================================================================

def _doc(*findings, timing=None):
    return {"findings": list(findings),
            "per_workflow_timing": timing or {_CI: {"job_p50": {"test": 197.0}}}}


def test_opt81_verifier_accepts_a_clean_a1_and_a2():
    vr = _load_verify_report()
    assert vr._opt81_a1_rederived(_a1_finding()) == []
    a2 = _a2_finding()
    assert vr._opt81_a2_rederived(a2, _doc(a2, _f("OPT75", structural=True))) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda f: f["faster_runner"]["fast"].__setitem__("p50_s", 80.0), "fast p50_s"),
    (lambda f: f["faster_runner"]["slow"].__setitem__("n", 9), "slow n"),
    (lambda f: [r.__setitem__("label", "ubuntu-24.04-8-cores")
                for r in f["faster_runner"]["rows"]
                if r["label"] == "ubuntu-latest-8-cores"], "labels"),
    (lambda f: f["faster_runner"]["fast"].__setitem__("class", "starsling"), "classifies"),
    (lambda f: f["faster_runner"]["rows"][3].__setitem__("step_list_sha", "0" * 16),
     "same step list"),
    (lambda f: f["faster_runner"].__setitem__("gap_s", 90.0), "gap_s"),
    (lambda f: f["faster_runner"].__setitem__("floor_s", 10.0), "floor_s"),
    (lambda f: f.__setitem__("runner_min_saving", 12.0), "runner-minute saving"),
    (lambda f: f.__setitem__("wall_clock_p50_s", 75.0), "exceeds its pre-cascade"),
    (lambda f: f["faster_runner"].__setitem__("credited_pre_cascade_s", 61.0),
     "exceeds the measured"),
])
def test_opt81_verifier_reddens_on_a_tampered_a1(mutate, needle):
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_finding())
    mutate(f)
    problems = vr._opt81_a1_rederived(f)
    assert any(needle in p for p in problems), problems


def test_opt81_verifier_reddens_on_a_same_class_or_sub_floor_a1():
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_finding())
    for r in f["faster_runner"]["rows"]:
        if r["label"] == "ubuntu-latest-8-cores":
            r["label"] = "ubuntu-24.04"
    f["faster_runner"]["fast"].update(label="ubuntu-24.04", size="", **{"class": "github-standard"})
    assert any("same runner class" in p for p in vr._opt81_a1_rederived(f))
    f = copy.deepcopy(_a1_finding())
    for r in f["faster_runner"]["rows"]:
        if r["label"] == "ubuntu-latest-8-cores":
            r["duration_s"] = r["duration_s"] + 40
    problems = vr._opt81_a1_rederived(f)
    assert any("p50_s" in p for p in problems), problems


def test_opt81_verifier_reddens_on_a_cross_architecture_a1():
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_finding())
    for r in f["faster_runner"]["rows"]:
        if r["label"] == "ubuntu-latest":
            r["label"] = "ubuntu-24.04-arm"
    f["faster_runner"]["slow"].update(label="ubuntu-24.04-arm", arch="arm64")
    problems = vr._opt81_a1_rederived(f)
    assert any("processor architectures" in p for p in problems), problems


@pytest.mark.parametrize("mutate,extra,needle", [
    (lambda f: f.__setitem__("wall_clock_p50_s", 30.0), [], "wall_clock_p50_s > 0"),
    (lambda f: f.__setitem__("runner_min_saving", 5.0), [], "runner_min_saving"),
    (lambda f: f.__setitem__("advisory", False), [], "not marked advisory"),
    (lambda f: f.__setitem__("evidence", f["evidence"] + " Saves ~40s."), [],
     "states a number"),
    (lambda f: None, [_f("OPT72", structural=True)], "OPT72 already addresses"),
    (lambda f: None, [_f("OPT74")], "OPT74 already addresses"),
    (lambda f: None, [_f("OPT78")], "OPT78 already addresses"),
    (lambda f: None, [_f("OPT17", wc=150.0)], "at least half its median"),
    (lambda f: None, [_f("OPT24")], "sharding"),
    (lambda f: f["faster_runner"].__setitem__("runner_label", "ubuntu-latest-8-cores"),
     [], "standard GitHub-hosted"),
])
def test_opt81_verifier_reddens_on_a_tampered_a2(mutate, extra, needle):
    vr = _load_verify_report()
    f = copy.deepcopy(_a2_finding())
    mutate(f)
    problems = vr._opt81_a2_rederived(f, _doc(f, *extra))
    assert any(needle in p for p in problems), problems


def test_opt81_a2_step_name_carrying_digits_is_not_a_promised_number():
    """A step named `Run tests (30s timeout)` is a repo-controlled NAME. The
    number ban reads the advisory's own prose, not quoted names, so a valid
    advisory on such a pole verifies; a number in the prose still fails."""
    vr = _load_verify_report()
    runs = _a2_runs(work="Run tests (30s timeout)")
    out, withheld, _ = _a2([_f("OPT75", structural=True)], runs=runs)
    assert len(out) == 1, withheld
    a2 = out[0]
    a2["id"] = "f9"
    assert "`Run tests (30s timeout)`" in a2["evidence"]
    assert vr._opt81_a2_rederived(a2, _doc(a2)) == []
    card = "\n".join(bp._opt81_card(a2, "u"))
    assert vr._vr_opt81_number_in(card) is None
    bad = copy.deepcopy(a2)
    bad["evidence"] += " It would save about 40s."
    assert any("states a number" in p for p in vr._opt81_a2_rederived(bad, _doc(bad)))


def test_opt81_verifier_mirrors_the_uncredited_opt79_cache_gate():
    vr = _load_verify_report()
    a2 = _a2_finding()
    doc = _doc(a2)
    doc["opt79_uncredited_pole_caches"] = [
        {"workflow_file": _CI, "job": "test", "waste_s": 31.0}]
    problems = vr._opt81_a2_rederived(a2, doc)
    assert any("net-negative cache" in p for p in problems), problems
    doc["opt79_uncredited_pole_caches"][0]["waste_s"] = 29.0
    assert vr._opt81_a2_rederived(a2, doc) == []


def test_opt81_an_off_category_leaf_still_holds_the_advisory_back():
    """A build-cache leaf on a test-dominated pole is demoted off-category, but
    it matched the pole's own log: it is still cheaper than hardware."""
    offcat = {"fix_key": "build-cache-miss"}
    assert bp._opt81_holding_leaf(None, offcat) is offcat
    assert bp._opt81_holding_leaf({"fix_key": "a"}, offcat) == {"fix_key": "a"}
    assert bp._opt81_holding_leaf(None, None) is None
    held = bp._opt81_holding_leaf(None, offcat)
    card = "\n".join(bp._opt81_card(_a2_finding(), "u",
                                    held_back_by=bp._opt81_leaf_name(held)))
    assert "advisory held back" in card and "build-cache-miss" in card
    assert "Option 1" not in card


def _report_with(cards: list[str]) -> str:
    return "# r\n\n" + "\n".join(cards) + "\n\n## 🗄️ Data sources\n"


def test_opt81_report_check_pairs_the_disclosure_with_every_card(tmp_path):
    vr = _load_verify_report()
    a1, a2 = _a1_finding(), _a2_finding()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a1, a2)), encoding="utf-8")
    good = _report_with(bp._opt81_card(a1, "u") + bp._opt81_card(a2, "u"))
    assert vr.check_opt81_runner_comparison_rederived(good, path).ok
    # a card that lost its disclosure
    bad = good.replace(f"_{cr._OPT81_DISCLOSURE}_", "", 1)
    r = vr.check_opt81_runner_comparison_rederived(bad, path)
    assert not r.ok and "disclosure" in r.detail, r.detail
    # a finding that never rendered
    only_a1 = _report_with(bp._opt81_card(a1, "u"))
    r = vr.check_opt81_runner_comparison_rederived(only_a1, path)
    assert not r.ok and "rendered 0 time(s)" in r.detail, r.detail
    # an A2 card that dropped the install prerequisite
    no_install = good.replace("installing the StarSling GitHub app", "the app")
    r = vr.check_opt81_runner_comparison_rederived(no_install, path)
    assert not r.ok and "install" in r.detail, r.detail
    # an A2 card that grew a number
    numbered = good.replace("benchmark required, no number attached",
                            "benchmark required, about 40s")
    r = vr.check_opt81_runner_comparison_rederived(numbered, path)
    assert not r.ok and "states a number" in r.detail, r.detail


def test_opt81_a1_floored_card_never_claims_a_runner_minute_saving():
    """A push-only A1 is floored to 0 by the generic developer-facing bound, whose
    reason ends "runner-minute (bill) saving only". OPT81 states its runner-minute
    effect as unknown (a different class bills differently), so the card must not
    also tell the reader the finding is a bill saving."""
    import wall_clock as wcm
    f = _a1_finding()
    res = wcm.bound_developer_facing(60.0, wcm.WallClockContext(events=("push",)))
    assert res.value == 0.0 and "runner-minute (bill) saving only" in res.reason
    f["wall_clock_p50_s"] = 0.0
    f["wall_clock_derivation"] = [{"bound": "developer_facing", "reason": res.reason}]
    card = "\n".join(bp._opt81_card(f, "u"))
    assert "Merge wait:** not credited" in card
    assert "post-merge/scheduled time" in card
    assert "saving only" not in card, card
    assert "rate table" in card


# =============================================================================
# Pins: each test below kills a named mutant the suite above let through.
# =============================================================================

# ---- A1 floor boundaries ---------------------------------------------------

def test_opt81_a1_gap_exactly_at_the_floor_fires():
    """The floor is "at least", not "more than": a gap exactly at 30s (slow
    median under 120s, so the absolute floor rules) and exactly at 25% (slow
    median 200s, floor 50s) both fire."""
    found, withheld, *_ = _a1(_runs(slow=[110] * 8, fast=[80] * 8))
    assert len(found) == 1, withheld
    fr = found[0]["faster_runner"]
    assert fr["gap_s"] == 30.0 and fr["floor_s"] == 30.0
    found, withheld, *_ = _a1(_runs(slow=[200] * 8, fast=[150] * 8))
    assert len(found) == 1, withheld
    fr = found[0]["faster_runner"]
    assert fr["gap_s"] == 50.0 and fr["floor_s"] == 50.0


def test_opt81_a1_short_job_is_held_to_the_absolute_30s_floor():
    """60s vs 40s is a third faster, well past 25%, but only 20s: below the
    absolute 30s floor, so it is a verdict, not a finding."""
    found, withheld, cands, *_ = _a1(_runs(slow=[60] * 8, fast=[40] * 8))
    assert found == [] and withheld.get("gap_below_floor") == 1, withheld
    assert cands == []


# ---- A2 unit gates ----------------------------------------------------------

def test_opt81_a2_fires_on_a_build_dominant_pole():
    out, withheld, _ = _a2(runs=_a2_runs(work="Build"))
    assert len(out) == 1, withheld
    assert out[0]["faster_runner"]["dominant_category"] == "build"


@pytest.mark.parametrize("work", [
    "Run tests and sleep", "Run tests then poll", "Download and run tests",
    "Run tests and upload", "Fetch and run tests", "Run tests via curl",
    # case-insensitive: the only matching word is capitalised
    "Poll test results", "Run tests, Upload",
    # deploy / publish on a step the categoriser files under build / test
    "Build and deploy", "Run tests and publish",
])
def test_opt81_a2_waits_or_moves_bytes_word_list(work):
    out, withheld, _ = _a2(runs=_a2_runs(work=work))
    assert out == [] and withheld.get("a2_dominant_step_waits_or_moves_bytes") == 1, (
        work, withheld)


def test_opt81_a2_an_advisory_finding_is_not_a_credited_lever():
    """An advisory carries no credited saving, so even a large stamped
    wall-clock on one does not count as a lever covering the pole."""
    out, withheld, _ = _a2([_f("OPT17", wc=150.0, advisory=True)])
    assert len(out) == 1, withheld
    out, withheld, _ = _a2([_f("OPT17", wc=150.0)])
    assert out == [] and withheld.get("a2_credited_lever_covers_the_pole") == 1


def test_opt81_a2_uncredited_cache_exactly_at_the_bar_suppresses():
    unc = [{"workflow_file": _CI, "job": "test", "waste_s": 30.0}]
    out, withheld, _ = _a2(unc=unc)
    assert out == [] and withheld.get("a2_cache_lever_on_the_pole") == 1, withheld


# ---- Card lines -------------------------------------------------------------

def test_opt81_a1_credited_card_states_the_merge_wait():
    f = _a1_credited_finding()
    assert f["wall_clock_p50_s"] == 60.0
    card = "\n".join(bp._opt81_card(f, "u"))
    assert "- **Merge wait:** this job is on the merge-gating path" in card, card
    assert "not credited" not in card


def test_opt81_a2_card_lists_the_cheaper_levers_checked():
    a2 = _a2_finding()
    card = "\n".join(bp._opt81_card(a2, "u"))
    line = next((ln for ln in card.splitlines()
                 if ln.startswith("- **Cheaper levers checked (none fired):** ")),
                None)
    assert line is not None, card
    for c in a2["faster_runner"]["cheaper_levers_checked"]:
        assert c["lever"] in line, c


# ---- Rendering routes -------------------------------------------------------

def _pole(check: str, job: str = "bench") -> dict:
    return {"check": check, "p50_s": 150.0, "workflow_file": _WF, "job": job,
            "dominant_step": "Run benchmarks", "dominant_p50_s": 139.0,
            "steps": [{"step": "Run benchmarks", "category": "test", "p50_s": 139.0}]}


def _render_doc(findings: list[dict], poles: list[dict]) -> str:
    doc = {"repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
           "data_sources": {"runs_sampled": 16, "jobs_sampled": 16,
                            "workflows_analyzed": 1},
           "findings": findings,
           "pr_critical_path": {"sampled_pr_count": 20, "sample_target": 20,
                                "sample_complete": True, "poles": poles}}
    return bp.render(doc, {}, {}, {}, "2026-06-08")


def test_opt81_renders_exactly_once_when_two_poles_map_to_one_finding():
    f = _a1_finding()
    md = _render_doc([f], [_pole("bench"), _pole("Bench / bench")])
    assert md.count('<a id="opt81-f7"></a>') == 1, md


def test_opt81_static_only_render_keeps_the_runner_class_section():
    """No measured pole, so the static-only body renders (it has a hygiene
    finding to show): the OPT81 card still gets its own section there."""
    f = _a1_finding()
    hygiene = {"id": "f8", "pattern": "OPT17", "title": "Dependency cache missing",
               "severity": "MEDIUM", "workflow_file": _WF, "affected_jobs": ["lint"],
               "evidence": "no cache step", "runner_min_saving": 5.0,
               "wall_clock_p50_s": 0.0}
    md = _render_doc([f, hygiene], [])
    assert "No measured critical path" not in md.splitlines()[0], md
    assert "## 🏎️ Runner class comparisons" in md, md
    assert md.count('<a id="opt81-f7"></a>') == 1


def test_opt81_is_never_listed_in_also_noticed():
    f = _a1_finding()
    f["wall_clock_p50_s"] = 0.0
    lines, n, *_ = bp._also_noticed_block([f], "u")
    assert n == 0 and not any("OPT81" in ln for ln in lines), lines


# ---- Held-back phrases: renderer and verifier twins ---------------------------

def test_opt81_held_back_phrase_table_matches_the_verifier_twin():
    vr = _load_verify_report()
    assert bp._OPT81_WITHHOLD_PHRASES == vr._VR_OPT81_WITHHOLD_PHRASES
    assert (bp._WITHHELD_PHRASES_BY_KEY[bp._OPT81_WITHHELD_DOC_KEY]
            == vr._VR_WITHHELD_PHRASES_BY_KEY[vr._VR_OPT81_WITHHELD_DOC_KEY])


def test_opt81_held_back_row_reaches_the_coverage_check(tmp_path):
    """A rendered OPT81 held-back row is re-derived by `check_coverage_disclosed`
    from the verifier's own phrase copy: a renderer-only wording edit reddens."""
    vr = _load_verify_report()
    doc = {"repo": "o/r", "findings": [], "pr_critical_path": {"poles": []},
           "data_sources": {},
           bp._OPT81_WITHHELD_DOC_KEY: [
               {"workflow_file": _WF, "job": "bench",
                "gate": "not_interleaved", "half": "A1"}]}
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "runner class: held back" in md, md
    path = tmp_path / "f.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    chk = vr.check_coverage_disclosed(md, path)
    assert chk.ok, chk.detail


# ---- The verifier: one tamper per sub-check -----------------------------------

def _restamp_a1(f: dict) -> None:
    """Re-stamp every derived A1 number from the rows, so only the property under
    test is wrong."""
    fr = f["faster_runner"]
    for side in ("slow", "fast"):
        st = fr[side]
        durs = [r["duration_s"] for r in fr["rows"] if r["label"] == st["label"]]
        st["n"] = len(durs)
        st["p50_s"] = round(cr._percentile(durs, 50), 1)
    fr["gap_s"] = round(fr["slow"]["p50_s"] - fr["fast"]["p50_s"], 1)
    fr["floor_s"] = round(max(30.0, 0.25 * fr["slow"]["p50_s"]), 1)
    fr["credited_pre_cascade_s"] = min(fr["credited_pre_cascade_s"], fr["gap_s"])
    f["wall_clock_p50_s"] = min(f["wall_clock_p50_s"], fr["credited_pre_cascade_s"])


def _tamper_min_samples(f):
    fr = f["faster_runner"]
    fast = fr["fast"]["label"]
    idx = [i for i, r in enumerate(fr["rows"]) if r["label"] == fast]
    del fr["rows"][idx[-1]]
    _restamp_a1(f)


def _tamper_os(f):
    fr = f["faster_runner"]
    for r in fr["rows"]:
        if r["label"] == fr["fast"]["label"]:
            r["label"] = "windows-latest-8-cores"
    fr["fast"].update(label="windows-latest-8-cores", os="windows")


def _tamper_step_names(f):
    f["faster_runner"]["step_names"] = list(f["faster_runner"]["step_names"]) + ["Extra"]


def _tamper_gap_below_floor(f):
    fr = f["faster_runner"]
    for r in fr["rows"]:
        if r["label"] == fr["fast"]["label"]:
            r["duration_s"] = r["duration_s"] + 40.0
    _restamp_a1(f)


def _tamper_count_tie(f):
    """Drop one slow-label row so the two labels tie (8 vs 8), keeping the credit."""
    fr = f["faster_runner"]
    idx = [i for i, r in enumerate(fr["rows"]) if r["label"] == fr["slow"]["label"]]
    del fr["rows"][idx[-1]]
    _restamp_a1(f)


def _tamper_matrix(f):
    """Make every fast-label row share a slow row's run id: a runner matrix that
    still carries its credit."""
    fr = f["faster_runner"]
    slow_ids = [r["run_id"] for r in fr["rows"] if r["label"] == fr["slow"]["label"]]
    for r, rid in zip([r for r in fr["rows"] if r["label"] == fr["fast"]["label"]],
                      slow_ids):
        r["run_id"] = rid


@pytest.mark.parametrize("mutate,needle", [
    (_tamper_min_samples, "below the 8"),
    (_tamper_os, "different operating systems"),
    (_tamper_step_names, "does not match the stamped step names"),
    (_tamper_gap_below_floor, "below its"),
    (lambda f: f["faster_runner"].__setitem__("min_gap_s", 20.0), "stamped min_gap_s"),
    (lambda f: f["faster_runner"].__setitem__("min_samples_per_label", 5),
     "stamped min_samples_per_label"),
    (lambda f: f["faster_runner"].__setitem__("job_is_workflow_long_pole", False),
     "not its workflow's long pole"),
    (_tamper_count_tie, "not its workflow's long pole"),
    (_tamper_matrix, "runner matrix"),
])
def test_opt81_verifier_a1_sub_checks_each_redden(mutate, needle):
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_credited_finding())
    mutate(f)
    problems = vr._opt81_a1_rederived(f)
    assert any(needle in p for p in problems), problems


def test_opt81_verifier_a1_restamp_helper_is_clean():
    """The tampers above re-stamp derived numbers; untampered, the re-stamp must
    leave a finding the verifier accepts, or the needles above prove nothing."""
    vr = _load_verify_report()
    for f in (copy.deepcopy(_a1_finding()), copy.deepcopy(_a1_credited_finding())):
        _restamp_a1(f)
        assert vr._opt81_a1_rederived(f) == []


def test_opt81_verifier_a2_requires_the_advisory_flag():
    vr = _load_verify_report()
    f = copy.deepcopy(_a2_finding())
    f["advisory"] = False
    assert any("not marked advisory" in p for p in vr._opt81_a2_rederived(f, _doc(f)))


def test_opt81_verifier_a2_sees_an_opt79_finding_on_the_pole():
    vr = _load_verify_report()
    f = _a2_finding()
    problems = vr._opt81_a2_rederived(f, _doc(f, _f("OPT79", wc=30.0)))
    assert any("net-negative cache already addresses" in p for p in problems), problems
    assert vr._opt81_a2_rederived(f, _doc(f, _f("OPT79", wc=29.0))) == []


def _cards_doc(tmp_path, *findings):
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(*findings)), encoding="utf-8")
    return path


def test_opt81_verifier_report_level_tampers(tmp_path):
    vr = _load_verify_report()
    a1, a2 = _a1_finding(), _a2_finding()
    path = _cards_doc(tmp_path, a1, a2)
    c1, c2 = bp._opt81_card(a1, "u"), bp._opt81_card(a2, "u")
    good = _report_with(c1 + c2)
    assert vr.check_opt81_runner_comparison_rederived(good, path).ok

    # the A1 prompt fence lost its disclosure, the card kept it
    t1 = "\n".join(c1)
    at = t1.index("```text")
    t1 = t1[:at] + t1[at:].replace(cr._OPT81_DISCLOSURE, "")
    r = vr.check_opt81_runner_comparison_rederived(_report_with([t1] + c2), path)
    assert not r.ok and "agent prompt does not carry" in r.detail, r.detail

    # the A1 card no longer states the slow median
    bad = good.replace("at p50 150s", "at p50 151s")
    r = vr.check_opt81_runner_comparison_rederived(bad, path)
    assert not r.ok and "does not state `at p50 150s`" in r.detail, r.detail

    # a card rendered twice
    r = vr.check_opt81_runner_comparison_rederived(_report_with(c1 + c1 + c2), path)
    assert not r.ok and "rendered 2 time(s)" in r.detail, r.detail


def test_opt81_verifier_accepts_a_held_back_a2_card(tmp_path):
    """A held-back advisory has no recipe and no prompt: the verifier must read
    it as held back, not as an A2 card that lost its fence and install line."""
    vr = _load_verify_report()
    a1, a2 = _a1_finding(), _a2_finding()
    path = _cards_doc(tmp_path, a1, a2)
    held = bp._opt81_card(a2, "u", held_back_by="vitest-isolate-pool")
    report = _report_with(bp._opt81_card(a1, "u") + held)
    r = vr.check_opt81_runner_comparison_rederived(report, path)
    assert r.ok, r.detail


@pytest.mark.parametrize("mutate,needle", [
    (lambda f: f.__setitem__("evidence", f["evidence"].replace(cr._OPT81_DISCLOSURE, "")),
     "evidence does not carry the disclosure"),
    (lambda f: f["faster_runner"].__setitem__("disclosure", "altered"),
     "stamped disclosure is missing or altered"),
])
def test_opt81_verifier_disclosure_stamps(tmp_path, mutate, needle):
    vr = _load_verify_report()
    a1 = copy.deepcopy(_a1_finding())
    report = _report_with(bp._opt81_card(a1, "u"))
    mutate(a1)
    path = _cards_doc(tmp_path, a1)
    r = vr.check_opt81_runner_comparison_rederived(report, path)
    assert not r.ok and needle in r.detail, r.detail


# ---- collect(): the real cascade and the A2 wiring ----------------------------

_COLLECT_STEPS = ["Set up job", "Run actions/checkout@v4", "Run tests",
                  "Post Run actions/checkout@v4", "Complete job"]
_COLLECT_YAML = ("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                 "    steps:\n      - uses: actions/checkout@v4\n      - run: make test\n")


def _cts(s: int) -> str:
    return f"2026-01-01T{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z"


class _Opt81Client:
    """A fake GhClient: one pull-request workflow, `ci.yml`, whose single job
    `test` is the merge-gating pole. `plan[i]` is run i's (runner label,
    duration). `yaml=None` makes the workflow file unreadable."""

    gave_up = False

    def __init__(self, plan, yaml=_COLLECT_YAML):
        self.plan, self.yaml = plan, yaml
        self.queries = self.errors = 0

    def available(self):
        return True

    def _job(self, i):
        label, dur = self.plan[i]
        fixed = {"Set up job": 2, "Run actions/checkout@v4": 8,
                 "Post Run actions/checkout@v4": 1, "Complete job": 0}
        t0 = t = i * 1000
        steps = []
        for k, n in enumerate(_COLLECT_STEPS, 1):
            d = fixed.get(n, dur - 11)
            steps.append({"name": n, "number": k, "status": "completed",
                          "conclusion": "success", "started_at": _cts(t),
                          "completed_at": _cts(t + d)})
            t += d
        return {"id": 5000 + i, "run_id": 100 + i, "name": "test", "status": "completed",
                "conclusion": "success", "started_at": _cts(t0),
                "completed_at": _cts(t0 + dur), "labels": [label],
                "runner_name": label, "steps": steps}

    def json(self, endpoint, allow_missing=False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _CI, "name": "CI"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            if re.search(r"per_page=1(?![0-9])", m.group(2)):
                return {"total_count": 300}
            return {"workflow_runs": [
                {"id": 100 + i, "event": "pull_request", "head_sha": f"s{i}",
                 "created_at": _cts(i * 1000), "run_started_at": _cts(i * 1000),
                 "updated_at": _cts(i * 1000 + d), "conclusion": "success",
                 "status": "completed", "run_attempt": 1}
                for i, (_lb, d) in enumerate(self.plan)]}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            i = int(m.group(1)) - 100
            return {"jobs": [self._job(i)] if 0 <= i < len(self.plan) else []}
        m = re.match(r"repos/o/r/commits/s(\d+)/check-runs", endpoint)
        if m:
            j = self._job(int(m.group(1)))
            return {"check_runs": [{"name": "test", "started_at": j["started_at"],
                                    "completed_at": j["completed_at"]}]}
        if endpoint.startswith(f"repos/o/r/contents/{_CI}"):
            if self.yaml is None:
                return None
            return {"content": base64.b64encode(self.yaml.encode()).decode()}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None

    def text(self, endpoint, allow_missing=False):
        self.queries += 1
        return ""


# One leg per run, the two labels alternating in time (the interleaving gate),
# `ubuntu-latest` run strictly more often (9 vs 8; the credit rule).
_TWO_LABEL_PLAN = ([leg for pair in zip([("ubuntu-latest", d) for d in _SLOW],
                                        [("ubuntu-latest-8-cores", d) for d in _FAST])
                    for leg in pair]
                   + [("ubuntu-latest", 150)])
_ONE_LABEL_PLAN = [("ubuntu-latest", 197)] * 10


def _collect(monkeypatch, plan, yaml=_COLLECT_YAML, seed=None):
    client = _Opt81Client(plan, yaml)
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"repo": "o/r", "findings": [
        {"id": "f1", "pattern": "OPT1", "workflow_file": _CI}]}
    doc.update(seed or {})
    return cr.collect(doc, "o/r", max_runs=len(plan), shallow_runs=len(plan))


def _o81(doc, half=None):
    return [f for f in doc["findings"] if f.get("pattern") == "OPT81"
            and (half is None or f["faster_runner"]["half"] == half)]


def test_opt81_a1_credit_survives_the_real_cascade_renders_and_verifies(
        monkeypatch, tmp_path):
    """A pull-request pole run 9 times on `ubuntu-latest` (p50 150s) and 8 on
    `ubuntu-latest-8-cores` (p50 90s): every generic bound in collect() leaves
    the 60s credit standing, the card says so, and the verifier accepts it."""
    vr = _load_verify_report()
    doc = _collect(monkeypatch, _TWO_LABEL_PLAN)
    assert [p["job"] for p in doc["pr_critical_path"]["poles"]] == ["test"]
    a1s = _o81(doc, "A1")
    assert len(a1s) == 1, doc.get("opt81_withheld_by_gate")
    f = a1s[0]
    assert f["faster_runner"]["credited_pre_cascade_s"] == 60.0
    assert f["wall_clock_p50_s"] == 60.0, f
    # A1 data exists for this job, so the advisory half never fires on it.
    assert _o81(doc, "A2") == []
    assert doc["opt81_withheld_by_gate"].get("a2_measured_runner_data_exists") == 1
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert md.count(f'<a id="opt81-{f["id"]}"></a>') == 1
    assert "- **Merge wait:** this job is on the merge-gating path" in md
    path = tmp_path / "f.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    r = vr.check_opt81_runner_comparison_rederived(md, path)
    assert r.ok, r.detail


def test_opt81_a2_fires_through_collect_on_a_pull_request_pole(monkeypatch):
    doc = _collect(monkeypatch, _ONE_LABEL_PLAN)
    a2s = _o81(doc, "A2")
    assert len(a2s) == 1, doc.get("opt81_withheld_by_gate")
    assert a2s[0]["affected_jobs"] == ["test"] and a2s[0]["advisory"] is True


def test_opt81_a2_reads_the_uncredited_cache_list_through_collect(monkeypatch):
    seed = {cr._OPT79_UNCREDITED_DOC_KEY: [
        {"workflow_file": _CI, "job": "test", "waste_s": 40.0}]}
    doc = _collect(monkeypatch, _ONE_LABEL_PLAN, seed=seed)
    assert _o81(doc, "A2") == []
    assert doc["opt81_withheld_by_gate"].get("a2_cache_lever_on_the_pole") == 1


def test_opt81_a2_needs_a_declared_pull_request_trigger_through_collect(monkeypatch):
    """The workflow file is unreadable: its pole still gates (observed PR runs),
    but A2 fires only on a workflow whose file declares a pull-request trigger."""
    doc = _collect(monkeypatch, _ONE_LABEL_PLAN, yaml=None)
    assert [p["job"] for p in doc["pr_critical_path"]["poles"]] == ["test"]
    assert _o81(doc, "A2") == []
    assert doc["opt81_withheld_by_gate"].get("a2_not_a_pull_request_workflow") == 1


# ---- Runner matrix: the documented threshold ----------------------------------

def _rows(*run_ids):
    return [{"run_id": r} for r in run_ids]


def test_opt81_runner_matrix_needs_half_the_smaller_population_shared():
    """A matrix is "at least half of the SMALLER population's runs also ran the
    other label". One shared run among eight does not make a matrix; exactly half
    does; the smaller population, not the larger, is the denominator."""
    m = cr._opt81_is_runner_matrix
    # partial overlap: 1 of 8 shared
    assert m(_rows(*range(8)), _rows(7, *range(100, 107))) is False
    # exactly half of the smaller (2 of 4) is a matrix
    assert m(_rows(1, 2, 3, 4), _rows(3, 4, 5, 6)) is True
    # one short of half (1 of 4) is not
    assert m(_rows(1, 2, 3, 4), _rows(4, 5, 6, 7)) is False
    # the smaller population is the denominator: 1 of 2 shared, against 8
    assert m(_rows(*range(8)), _rows(0, 50)) is True
    # nothing shared is never a matrix, and rows without a run id do not count
    assert m(_rows(1, 2), _rows(3, 4)) is False
    assert m(_rows(None, None), _rows(None)) is False


def test_opt81_a1_one_shared_run_is_not_a_matrix_and_stays_creditable():
    """Interleaved single-leg runs where one run happens to carry both labels:
    still a comparison across runs (not a matrix), so the long-pole credit
    stands."""
    runs = _alt_runs(slow=_SLOW + [150, 150])
    runs[0].append(_job("bench", "ubuntu-latest-8-cores", 90, runs[0][0]["run_id"]))
    found, withheld, *_ = _a1(runs)
    assert len(found) == 1, withheld
    fr = found[0]["faster_runner"]
    assert fr["runner_matrix"] is False
    assert found[0]["wall_clock_p50_s"] > 0, fr["credit_reason"]


# ---- Interleaving: touching ranges and unreadable run times ---------------------

def test_opt81_spans_touching_at_one_instant_overlap():
    t = "2026-06-01T00:00:00Z"
    assert cr._opt81_spans_overlap(("2026-05-01T00:00:00Z", t),
                                   (t, "2026-07-01T00:00:00Z")) is True
    assert cr._opt81_spans_overlap(("2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z"),
                                   (t, "2026-07-01T00:00:00Z")) is False


def test_opt81_an_unreadable_run_time_fails_closed():
    """A population whose run times cannot all be read cannot be shown to
    overlap the other one: its span is unknown (not narrowed to the readable
    rows) and an unknown span never overlaps."""
    ok = {"at": "2026-06-01T00:00:00Z"}
    assert cr._opt81_time_span([ok, {"at": "not a time"}]) == ("", "")
    assert cr._opt81_time_span([ok, {"at": ""}]) == ("", "")
    assert cr._opt81_time_span([]) == ("", "")
    span = ("2026-05-01T00:00:00Z", "2026-07-01T00:00:00Z")
    assert cr._opt81_spans_overlap(("", ""), span) is False
    assert cr._opt81_spans_overlap(span, ("", "")) is False
    assert cr._opt81_spans_overlap(span, ("bad", "2026-06-01T00:00:00Z")) is False


def test_opt81_a1_unreadable_run_time_is_held_back_never_compared():
    """One fast-label run whose time cannot be read: the pair is held back as
    not shown to be interleaved and listed, never compared on the rest."""
    runs = _alt_runs()
    bad = next(j for run in runs for j in run if j["labels"] == ["ubuntu-latest-8-cores"])
    bad["_run_created_at"] = "not a time"
    found, withheld, cands, *_ = _a1(runs)
    assert found == [], [f["faster_runner"]["fast"] for f in found]
    assert withheld.get("not_interleaved") == 1, withheld
    assert cands == [{"workflow_file": _WF, "job": "bench", "gate": "not_interleaved",
                      "half": "A1"}]


def test_opt81_a1_interleaving_reads_the_run_time_before_the_job_time():
    """The run's created_at (when it was triggered) is the time compared; a job's
    own created_at is only a fallback. Here the run times interleave while the
    job times look like a `runs-on` switch: the pair is compared."""
    runs = _alt_runs()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for run in runs:
        for j in run:
            j["_run_created_at"] = _iso(base + timedelta(hours=j["run_id"]))
            fast = j["labels"] == ["ubuntu-latest-8-cores"]
            j["created_at"] = _iso(base + timedelta(days=400 if fast else 0,
                                                    hours=j["run_id"]))
    found, withheld, *_ = _a1(runs)
    assert len(found) == 1, withheld
    for r in found[0]["faster_runner"]["rows"]:
        assert r["at"] == _iso(base + timedelta(hours=r["run_id"])), r


# ---- The verifier: matrix flag, time-range stamps, and A2's workflow filter -----

def test_opt81_verifier_rederives_the_runner_matrix_flag():
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_credited_finding())
    f["faster_runner"]["runner_matrix"] = True
    assert any("runner_matrix" in p and "re-derive" in p
               for p in vr._opt81_a1_rederived(f)), vr._opt81_a1_rederived(f)
    g = copy.deepcopy(_a1_finding())
    assert g["faster_runner"]["runner_matrix"] is True
    g["faster_runner"]["runner_matrix"] = False
    assert any("runner_matrix" in p and "re-derive" in p
               for p in vr._opt81_a1_rederived(g)), vr._opt81_a1_rederived(g)


@pytest.mark.parametrize("side,key", [("slow", "first_run_at"), ("fast", "last_run_at")])
def test_opt81_verifier_rederives_the_time_range_stamps(side, key):
    vr = _load_verify_report()
    f = copy.deepcopy(_a1_credited_finding())
    f["faster_runner"][side][key] = "2020-01-01T00:00:00Z"
    problems = vr._opt81_a1_rederived(f)
    assert any(f"{side} first_run_at/last_run_at do not re-derive" in p
               for p in problems), problems


def test_opt81_verifier_a2_reads_cheaper_levers_only_in_its_own_workflow():
    """A same-named job in ANOTHER workflow carrying a structural, sharding,
    covering or cache lever does not address this pole: a correct advisory
    still verifies."""
    vr = _load_verify_report()
    a2 = _a2_finding()
    other = ".github/workflows/nightly.yml"
    extra = [_f("OPT72", structural=True, wf=other), _f("OPT24", wf=other),
             _f("OPT17", wc=150.0, wf=other), _f("OPT79", wc=60.0, wf=other)]
    assert vr._opt81_a2_rederived(a2, _doc(a2, *extra)) == []


# ---- Rendering: the lever list, pointers and the generic prompt -----------------

def test_opt81_levers_checked_keeps_the_pattern_ids_literally():
    rendered = dict(bp._opt81_levers_checked(_a2_finding()["faster_runner"]))
    assert "sharding (OPT24)" in rendered, rendered
    assert "a cache that costs more than it saves (OPT79)" in rendered, rendered
    # a pattern list already spelled out in the lever text is not repeated
    structural = next(k for k in rendered if k.startswith("structural:"))
    assert structural.count("OPT72") == 1, structural


def test_opt81_pointer_to_a_mixed_match_names_both_places():
    card = "its **OPT81** runner-class card below this pole's prompt"
    assert bp._dd_where(["OPT81"]) == card
    assert bp._dd_where(["OPT24", "OPT81"]) == (
        f"the **Also noticed** section below (and {card})")
    assert bp._dd_where(["OPT24", "OPT81"], entry=True) == (
        f"its entry in the **Also noticed** section below (and {card})")
    assert bp._dd_where(["OPT24"]) == "the **Also noticed** section below"


def test_opt81_generic_prompt_names_the_runner_class_match():
    a1 = _a1_finding()
    p = bp._build_generic_agent_prompt(_pole("bench"), [], None, "o/r", None, 1, 1,
                                       None, data_driven=[a1])
    assert "RUNNER-CLASS COMPARISON MATCHED" in p, p
    assert "see its **OPT81** card below this prompt" in p
    assert "DATA-DRIVEN CATALOG PATTERN MATCHED" not in p
    assert "NO CATALOG PATTERN MATCHED" not in p


# ---- collect(): held-back candidates reach the doc and the report ---------------

def test_opt81_a1_held_back_candidate_reaches_the_doc_through_collect(monkeypatch):
    """A pull-request pole run on `ubuntu-latest` and on a label no row of the
    taxonomy sizes: A1 cannot compare them, so collect() writes the held-back
    candidate onto the findings doc and the report renders its row."""
    plan = [("ubuntu-latest" if i % 2 else "my-big-box", 150) for i in range(17)]
    doc = _collect(monkeypatch, plan)
    assert doc[cr._OPT81_WITHHELD_DOC_KEY] == [
        {"workflow_file": _CI, "job": "test",
         "gate": "runner_label_not_classifiable_by_size", "half": "A1"}], (
        doc.get("opt81_withheld_by_gate"))
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "runner class: held back" in md, md


def test_opt81_a2_held_back_candidate_reaches_the_doc_through_collect(monkeypatch):
    """The advisory cannot resolve the pole's dominant step (no step timings):
    collect() writes that held-back candidate onto the findings doc."""
    real = cr._detect_opt81_runner_size_advisory

    def stepless(wf_path, jobs_per_run, *a, **k):
        bare = [[{**j, "steps": []} for j in run] for run in jobs_per_run]
        return real(wf_path, bare, *a, **k)

    monkeypatch.setattr(cr, "_detect_opt81_runner_size_advisory", stepless)
    doc = _collect(monkeypatch, _ONE_LABEL_PLAN)
    assert _o81(doc, "A2") == []
    assert doc[cr._OPT81_WITHHELD_DOC_KEY] == [
        {"workflow_file": _CI, "job": "test", "gate": "a2_dominant_step_unresolved",
         "half": "A2"}], doc.get("opt81_withheld_by_gate")
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "runner class: held back" in md, md
