# Frozen control checkouts (B4 layer a)

Minimal input-surface snapshots of the two calibration positive-control
repos — exactly the three file classes the scorer reads (workflow YAML,
composite actions, root build-tool configs), nothing else. The exact-match
cells in `test_b4_controls.py` materialize each into a throwaway git repo and
assert the collector reproduces the frozen calibration grade byte-for-byte.
These trees CANNOT drift (unlike live clones); refresh them only deliberately,
with a new upstream SHA recorded here and the expected grades re-derived.

## Cloaked storage layout

These are real third-party workflow files. Stored verbatim under a
`.github/workflows/` path, registry security scanners parse them as THIS
repo's live automation and attribute their contents here. So every file ships
"cloaked": each `.github` path segment is renamed to `dot-github`, and every
file is given a `.fixture` suffix — no shipped path is a workflow path and no
shipped file has a bare workflow-parseable name. The cloak is a pure,
invertible rename; not a byte of any scored file changes.

At test time `_fixture_checkouts.materialize(name, dest)` restores each
fixture to its exact original tree (`.github/...`, original filenames) so the
collector scores precisely what it scored before. `checkouts-manifest.json`,
built from `origin/main` before the rename, records every original path and
its SHA-256; `test_fixture_cloak.py` proves the round trip is lossless
byte-for-byte and that no un-cloaked path leaks back in. To refresh a fixture,
re-run the relocation so the manifest is regenerated from the new bytes.

| fixture | source repo | upstream SHA (2026-07-27) | expected |
|---|---|---|---|
| mastra/ | mastra-ai/mastra (Apache-2.0) | 5718a229281dcfd36bcd1f42a242e3717e510a33 | 82 (B+) |
| better-auth/ | better-auth/better-auth (MIT) | e2c73fbec87f5e19f6a2b5ac371bc5bba9bd49ff | 82 (B+) |

Both grades are the v0.1.2 calibration rows (OD-CS18 removed
`ci.trigger.draft-gate`, 12 → 11 checks), recomputed over these frozen
trees; `test_b4_controls.py` asserts the collector reproduces them. Under
v0.1.1 these were mastra 83 (B+) and better-auth 75 (B), verified live on
these SHAs during the B4 sweep before freezing; removing the draft-gate
check moves both to 9/11.
