from datetime import datetime, timedelta, timezone

import blocking_path as bp


def _stamp(at: datetime, message: str) -> str:
    return f"{at.isoformat().replace('+00:00', 'Z')} {message}"


def _vendored_build_log(*, completions: bool = True) -> str:
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lines = [_stamp(started, "##[group]Run make contrib")]
    for index in range(25):
        library = f"lib{index:02d}"
        at = started + timedelta(seconds=index * 56)
        lines.append(_stamp(at, f"make[2]: Entering directory '/src/contrib/{library}'"))
        if completions:
            lines.append(_stamp(
                at + timedelta(seconds=56),
                f"touch contrib/{library}/.stamp.{library}.build",
            ))
    lines.append(_stamp(started + timedelta(seconds=2000), "build step complete"))
    lines.append(_stamp(started + timedelta(seconds=2000), "##[group]Run tests"))
    lines.append("Cache not found for input keys: deps-linux")
    return "\n".join(lines)


def test_many_vendored_libraries_dominate_the_build_and_beat_cache_advice():
    leaf = bp._parse_log(_vendored_build_log())

    assert leaf is not None
    assert leaf["fix_key"] == "vendored-lib-build"
    assert leaf["magnitude"] == {
        "label": "vendored library build share",
        "unit": "%",
        "value": 70.0,
    }
    assert len(leaf["deeper"][0]["rows"]) == 25
    assert sum(row[1] for row in leaf["deeper"][0]["rows"]) == 1400.0


def test_restored_vendored_libraries_without_build_stamps_stay_quiet():
    leaf = bp._parse_log(_vendored_build_log(completions=False))

    assert leaf is not None
    assert leaf["fix_key"] == "dependency-cache"


def test_repeated_intervals_are_grouped_by_vendored_library_root():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lines = [_stamp(started, "##[group]Run make dependencies")]
    offset = 0
    for root, library, duration in (
            ("contrib", "codec", 30),
            ("third_party", "parser", 40),
            ("vendor", "codec", 20),
            ("external", "crypto", 50),
            ("contrib", "codec", 10)):
        lines.append(_stamp(
            started + timedelta(seconds=offset),
            f"make: Entering directory '/src/{root}/{library}'",
        ))
        offset += duration
        lines.append(_stamp(
            started + timedelta(seconds=offset),
            f"touch {root}/{library}/.stamp.{library}.build",
        ))
    lines.append(_stamp(started + timedelta(seconds=200), "build step complete"))
    lines.append(_stamp(started + timedelta(seconds=200), "##[group]Run tests"))

    leaf = bp._parse_log("\n".join(lines))

    assert leaf is not None and leaf["fix_key"] == "vendored-lib-build"
    assert leaf["deeper"][0]["rows"] == [
        ("external/crypto", 50.0, None),
        ("contrib/codec", 40.0, None),
        ("third_party/parser", 40.0, None),
        ("vendor/codec", 20.0, None),
    ]


def test_vendored_build_share_below_half_of_the_step_stays_quiet():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = "\n".join((
        _stamp(started, "##[group]Run make dependencies"),
        _stamp(started + timedelta(seconds=1),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=50),
               "touch contrib/codec/.stamp.codec.build"),
        _stamp(started + timedelta(seconds=100), "##[group]Run tests"),
    ))

    leaf = bp._parse_log(log)

    assert leaf is None or leaf["fix_key"] != "vendored-lib-build"


def test_overlapping_vendored_build_intervals_stay_quiet():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = "\n".join((
        _stamp(started, "##[group]Run make dependencies"),
        _stamp(started + timedelta(seconds=1),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=10),
               "make: Entering directory '/src/vendor/parser'"),
        _stamp(started + timedelta(seconds=60),
               "touch contrib/codec/.stamp.codec.build"),
        _stamp(started + timedelta(seconds=70),
               "touch vendor/parser/.stamp.parser.build"),
        _stamp(started + timedelta(seconds=100), "##[group]Run tests"),
    ))

    leaf = bp._parse_log(log)

    assert leaf is None or leaf["fix_key"] != "vendored-lib-build"


def test_vendored_markers_in_different_steps_do_not_pair():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = "\n".join((
        _stamp(started, "##[group]Run configure dependencies"),
        _stamp(started + timedelta(seconds=10),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=60), "##[group]Run package"),
        _stamp(started + timedelta(seconds=70),
               "touch contrib/codec/.stamp.codec.build"),
    ))

    leaf = bp._parse_log(log)

    assert leaf is None or leaf["fix_key"] != "vendored-lib-build"


def test_latest_directory_entry_bounds_the_completed_build():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = "\n".join((
        _stamp(started, "##[group]Run make dependencies"),
        _stamp(started + timedelta(seconds=1),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=80),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=90),
               "touch contrib/codec/.stamp.codec.build"),
        _stamp(started + timedelta(seconds=100), "##[group]Run tests"),
    ))

    leaf = bp._parse_log(log)

    assert leaf is None or leaf["fix_key"] != "vendored-lib-build"


def test_completed_stamp_does_not_reuse_a_directory_visit_that_already_left():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log = "\n".join((
        _stamp(started, "##[group]Run make dependencies"),
        _stamp(started + timedelta(seconds=1),
               "make: Entering directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=2),
               "make: Leaving directory '/src/contrib/codec'"),
        _stamp(started + timedelta(seconds=90),
               "touch contrib/codec/.stamp.codec.build"),
        _stamp(started + timedelta(seconds=100), "##[group]Run tests"),
    ))

    leaf = bp._parse_log(log)

    assert leaf is None or leaf["fix_key"] != "vendored-lib-build"
