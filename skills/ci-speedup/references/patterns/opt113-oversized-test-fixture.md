---
pattern: OPT113
impact: MEDIUM
class: static
detector: go-test-source-correlated
affected_files: '**/*_test.go'
fix_strategy: small-contract-fixture
title_template: 'A Test Fixture Is Much Larger Than Its Contract Needs'
category: 2
---

# OPT113 — A Test Fixture Is Much Larger Than Its Contract Needs

**Anti-pattern**: A test archives, compresses or hashes the linked Go test
executable, or a binary it just built, when the contract under test only needs
ordinary file bytes. The linked test binary can be many megabytes and grows
further under instrumentation such as the race detector, so every repeated
archive pays to move bytes the assertion does not distinguish.

**Detection heuristic**: In a Go `_test.go` file, find `os.Executable()` or an
output path passed to an explicit `go build -o`, then require an archive,
compression or cryptographic/hash writer in the same test source. An
`os.Executable()` call used only for `os.Stat`, mode bits or other executable
metadata stays quiet. A small byte fixture going through the same archive path
also stays quiet. If the source cannot identify the fixture origin, withhold the
finding rather than guessing that a path is a build output.

**Fix recipe (safest first)**:

1. State the contract the test exercises: ordinary bytes and their digest,
   archive layout, executable mode, platform metadata, or the built program's
   actual behavior.
2. For byte, digest and layout checks, create one small shared fixture with
   deterministic bytes and pass it through every existing archive/compression
   assertion. Keep the modes, inventory entries, stale-hash rejection and
   extraction checks intact.
3. Keep a real executable for cases that inspect executable metadata, execute
   the program, or validate a property that a byte fixture cannot represent.
4. Compare the affected test lane before and after on the same commit, runner
   class and instrumentation mode; confirm the test and coverage inventory is
   unchanged.

**Sizing**: The static finding credits neither wall-clock nor runner-minutes.
Runner-minute saving is **unsized** until run timings identify the affected
test and its monthly execution count. As a public single-run observation, not a
general estimate, [openclaw/crabbox#2876](https://github.com/openclaw/crabbox/pull/2876)
reported its package's race lane falling from 256.401 seconds to 61.720 seconds
and the enclosing race job from 16m45s to 11m04s — a runner-time saving of
5m41s for that one observed run. Total CI elapsed time in that comparison did
not improve, so the detector does not claim a merge-wait saving from source.

**Risk**: MEDIUM. Replacing a real artifact can weaken a test that checks
permissions, architecture, signing, executable metadata or actual execution.
Keep those cases on real artifacts and shrink only the fixtures whose contract
is byte content, hashing or archive shape.
