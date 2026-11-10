"""The `lighthouse-serial-audits` log leaf (OPT103).

Every log here is synthetic. The line shapes are the ones Lighthouse CI's collect and
assert commands print: `Running Lighthouse N time(s) on <url>`, `Run #k...done.`,
`Done running Lighthouse!` and `Checking assertions against U URL(s), M total run(s)`.
"""
import time

import blocking_path as bp
import collect_runs as cr


_STAMP = "2026-01-01T00:00:00.0000000Z "


def _lhci_pass(cmd: str, urls: list[str], runs: int, *, closed: bool = True,
               header: bool = True) -> list[str]:
    out: list[str] = []
    if header:
        out += [f"##[group]Run {cmd}", f"\x1b[36;1m{cmd}\x1b[0m",
                "shell: /usr/bin/bash -e {0}", "##[endgroup]"]
    out += ["Healthcheck passed!",
            'Started a web server with "node serve.mjs --port 8080"...']
    for url in urls:
        out.append(f"Running Lighthouse {runs} time(s) on {url}")
        out += [f"Run #{k}...done." for k in range(1, runs + 1)]
    if closed:
        out += ["Done running Lighthouse!",
                f"Checking assertions against {len(urls)} URL(s), "
                f"{len(urls) * runs} total run(s)",
                "All results processed!", "Done running autorun."]
    return [_STAMP + line for line in out]


_URLS = [f"http://localhost:8080/{page}" for page in ("", "alpha/", "beta/", "gamma/")]
_PHONE = "npx --yes @lhci/cli@0.15.x autorun --config=lighthouserc.phone.json"
_WIDE = "npx --yes @lhci/cli@0.15.x autorun --config=lighthouserc.wide.json"
_TWO_PASS_LOG = "\n".join(_lhci_pass(_PHONE, _URLS, 5) + _lhci_pass(_WIDE, _URLS, 5))


def test_two_serial_passes_report_the_audit_product_and_repeat_share():
    leaf = bp._parse_log(_TWO_PASS_LOG)

    assert leaf is not None and leaf["fix_key"] == "lighthouse-serial-audits"
    assert "40 full page audits" in leaf["unit_label"]
    assert "2 collect pass(es) x 4 URL(s) x 5 run(s) per URL" in leaf["unit_label"]
    # 40 audits over 4 distinct URLs: 36 audit a page the job already audited.
    assert leaf["magnitude"]["value"] == 90.0
    assert leaf["magnitude"]["unit"] == "%"
    assert len(leaf["deeper"]) == 1
    level = leaf["deeper"][0]
    assert level["pct_of"] == "sum"
    assert level["rows"] == [
        ("pass 1: 4 URLs x 5 runs (phone)", 20.0, "20"),
        ("pass 2: 4 URLs x 5 runs (wide)", 20.0, "20"),
    ]
    assert all(len(row[0]) <= bp._LBLW for row in level["rows"])
    assert "36 re-audit an already-audited URL" in level["blocker_note"]
    assert "BIGGEST LEVER" in level["blocker_note"]


def test_evidence_is_verbatim_tool_output_then_the_command():
    leaf = bp._parse_log(_TWO_PASS_LOG)

    assert leaf["evidence"] == [
        "Running Lighthouse 5 time(s) on http://localhost:8080/",
        "Checking assertions against 4 URL(s), 20 total run(s)",
        "Running Lighthouse 5 time(s) on http://localhost:8080/",
        "Checking assertions against 4 URL(s), 20 total run(s)",
        _PHONE,
    ]


def test_default_smoke_audit_stays_silent():
    # lhci's default of 3 runs over two URLs is 6 audits, below the floor.
    log = "\n".join(_lhci_pass("lhci autorun", _URLS[:2], 3))
    assert bp._parse_log(log) is None


def test_floor_is_twenty_audits_from_both_sides():
    assert bp._LHCI_AUDITS_FLOOR == 20
    below = "\n".join(_lhci_pass("lhci autorun", _URLS[:1], 19))
    at = "\n".join(_lhci_pass("lhci autorun", _URLS[:1], 20))
    assert bp._parse_log(below) is None
    assert bp._parse_log(at)["fix_key"] == "lighthouse-serial-audits"


def test_one_run_over_many_urls_is_a_different_lever():
    # 25 audits, but one per URL in one pass: nothing is repeated.
    many = [f"http://localhost:8080/p{i}/" for i in range(25)]
    assert bp._parse_log("\n".join(_lhci_pass("lhci autorun", many, 1))) is None


def test_two_passes_of_one_run_each_count_as_repeats():
    # One run per URL, but a second pass re-audits every URL: 2 audits per URL.
    ten = [f"http://localhost:8080/p{i}/" for i in range(10)]
    log = "\n".join(_lhci_pass("lhci collect --settings.preset=desktop", ten, 1)
                    + _lhci_pass("lhci collect", ten, 1))
    leaf = bp._parse_log(log)
    assert leaf["fix_key"] == "lighthouse-serial-audits"
    assert leaf["magnitude"]["value"] == 50.0
    assert leaf["deeper"][0]["rows"][0][0] == "pass 1: 10 URLs x 1 runs (desktop)"
    assert leaf["deeper"][0]["rows"][1][0] == "pass 2: 10 URLs x 1 runs"


def test_unfinished_pass_is_not_counted():
    # No `Done running Lighthouse!`: a cancelled or crashed collect gated nothing.
    crashed = _lhci_pass("lhci autorun", _URLS, 6, closed=False)
    assert bp._parse_log("\n".join(crashed)) is None
    # The same audits in a completed pass fire: 1 x 4 x 6 = 24.
    leaf = bp._parse_log("\n".join(_lhci_pass("lhci autorun", _URLS, 6)))
    assert "1 collect pass(es) x 4 URL(s) x 6 run(s) per URL" in leaf["unit_label"]
    assert leaf["magnitude"]["value"] == round(100 * 20 / 24, 2)
    # A finished pass followed by a crashed one counts the finished pass only.
    mixed = _lhci_pass(_PHONE, _URLS, 6) + _lhci_pass(_WIDE, _URLS, 6, closed=False)
    leaf = bp._parse_log("\n".join(mixed))
    assert "24 full page audits" in leaf["unit_label"]
    assert len(leaf["deeper"][0]["rows"]) == 1


def test_uneven_passes_are_summed_and_named_from_the_preset_flag():
    six = [f"http://localhost:8080/s{i}/" for i in range(6)]
    first = [line for line in _lhci_pass(
        "lhci autorun --collect.settings.preset=desktop", six, 3) if "Run #" not in line]
    second = _lhci_pass("lhci autorun --config ./ci/lighthouserc.json", six[:2], 5)
    leaf = bp._parse_log("\n".join(first + second))

    assert "28 full page audits" in leaf["unit_label"]
    assert "2 collect pass(es) over 6 distinct URL(s)" in leaf["unit_label"]
    assert leaf["deeper"][0]["rows"] == [
        ("pass 1: 6 URLs x 3 runs (desktop)", 18.0, "18"),
        # A bare `lighthouserc.json` has nothing that tells it apart: no tag.
        ("pass 2: 2 URLs x 5 runs", 10.0, "10"),
    ]
    assert leaf["magnitude"]["value"] == round(100 * (28 - 6) / 28, 2)


def test_pass_tag_keeps_only_the_distinguishing_part():
    assert bp._lhci_pass_tag("lighthouserc.mobile.json") == "mobile"
    assert bp._lhci_pass_tag("'./config/.lighthouserc.desktop.cjs'") == "desktop"
    assert bp._lhci_pass_tag("config/lhci.desktop.yml") == "lhci.desktop"
    assert bp._lhci_pass_tag("desktop") == "desktop"
    assert bp._lhci_pass_tag("lighthouserc.json") == ""
    assert bp._lhci_pass_tag(".lighthouserc.js") == ""


def test_passes_in_one_multi_line_step_are_named_in_command_order():
    block = [_STAMP + line for line in (
        "##[group]Run npm run build", "npm run build",
        "npx lhci autorun --config=lighthouserc.phone.json",
        "npx lhci autorun --config=lighthouserc.wide.json",
        "shell: /usr/bin/bash -e {0}", "##[endgroup]")]
    log = "\n".join(block + _lhci_pass(_PHONE, _URLS, 5, header=False)
                    + _lhci_pass(_WIDE, _URLS, 5, header=False))
    rows = bp._parse_log(log)["deeper"][0]["rows"]
    assert [row[0] for row in rows] == [
        "pass 1: 4 URLs x 5 runs (phone)", "pass 2: 4 URLs x 5 runs (wide)"]


def test_passes_stay_untagged_when_commands_and_passes_do_not_pair_up():
    # One echoed command, two passes (a script loops over presets): no honest mapping.
    block = [_STAMP + line for line in (
        "##[group]Run ./scripts/audit.sh", "./scripts/audit.sh", "##[endgroup]",
        "+ lhci autorun --config=lighthouserc.phone.json")]
    log = "\n".join(block + _lhci_pass(_PHONE, _URLS, 5, header=False)
                    + _lhci_pass(_WIDE, _URLS, 5, header=False))
    rows = bp._parse_log(log)["deeper"][0]["rows"]
    assert [row[0] for row in rows] == [
        "pass 1: 4 URLs x 5 runs", "pass 2: 4 URLs x 5 runs"]


def test_prose_that_mentions_the_lines_does_not_fire():
    prose = [f"note: Running Lighthouse 9 time(s) on http://localhost/{i}" for i in range(9)]
    prose.append("log says Done running Lighthouse!")
    assert bp._parse_log("\n".join(prose)) is None


def test_a_cache_line_elsewhere_in_the_job_does_not_hide_the_audits():
    log = "Cache not found for input keys: deps-linux\n" + _TWO_PASS_LOG
    assert bp._parse_log(log)["fix_key"] == "lighthouse-serial-audits"


def test_leaf_is_registered_on_every_surface_a_crowned_leaf_needs():
    key = "lighthouse-serial-audits"
    assert set(bp._FIX_META[key]) == {"cause", "look", "constraints", "docs", "deliver"}
    assert bp._LEAF_STEP_CATEGORY[key] == frozenset({"scan", "test"})
    assert key in bp._LEAF_DOMINANT_STEP_TOKEN
    # The generic line for job-expanding leaves asks to fill idle cores first. Two
    # Lighthouse audits must not share a runner, so this leaf carries its own wording.
    assert key not in bp._JOB_EXPANDING_LEAVES
    constraints = bp._FIX_META[key]["constraints"]
    assert "never within one" in constraints
    assert "runner minutes go up" in constraints


def test_leaf_crowns_only_a_pole_whose_dominant_step_is_the_lighthouse_step():
    leaf = bp._parse_log(_TWO_PASS_LOG)
    # The category comes from the real step classifier, not from a literal.
    for step, want in (("Lighthouse CI", "scan"), ("Run lhci autorun", "scan"),
                       ("Lighthouse audit (desktop)", "scan"),
                       ("Build and run Lighthouse", "scan"),
                       ("Perf budget via lighthouse-ci", "scan"),
                       ("Lighthouse tests", "test")):
        category = cr._step_category(step)
        assert category == want, (step, category)
        assert bp._offcategory_leaf(
            leaf, {"dominant_category": category, "dominant_step": step}) is False, step
    # The category matches but the dominant step is another tool: demoted.
    for step in ("Lint", "Typecheck", "Unit tests"):
        assert bp._offcategory_leaf(leaf, {
            "dominant_category": cr._step_category(step), "dominant_step": step}) is True
    # Another category dominates: demoted whatever the step is called.
    assert bp._offcategory_leaf(
        leaf, {"dominant_category": "build", "dominant_step": "Lighthouse CI"}) is True
    assert bp._offcategory_leaf(
        leaf, {"dominant_category": "install", "dominant_step": "Install lhci"}) is True
    # No decomposition: fails open, as for every leaf.
    assert bp._offcategory_leaf(leaf, {}) is False


def test_lighthouse_regexes_stay_linear_on_hostile_lines():
    hostile = [
        " " * 200_000 + "Running Lighthouse",
        "Running Lighthouse " + "9" * 200_000 + " time(s) on x",
        "Running Lighthouse 5 time(s) on " + "a" * 200_000,
        "Checking assertions against " + "1" * 200_000,
        "lhci" + " " * 200_000 + "autoru",
        "@lhci/cli@" + "1." * 100_000 + " autoru",
        "lhci autorun " + "--config=" * 50_000,
        ("lhci autorun --config=" + "a" * 250 + " ") * 2_000,
    ]
    started = time.perf_counter()
    for line in hostile:
        for regex in (bp._LHCI_RUNNING_RE, bp._LHCI_PASS_DONE_RE, bp._LHCI_ASSERT_RE,
                      bp._LHCI_CMD_RE, bp._LHCI_PASS_NAME_RE, bp._LHCI_CONFIG_EXT_RE):
            regex.search(line)
        bp._lhci_pass_tag(line)
        bp._lhci_section_commands([line], 0, 1)
    assert bp._lighthouse_audit_leaf(hostile) is None
    assert time.perf_counter() - started < 0.5
