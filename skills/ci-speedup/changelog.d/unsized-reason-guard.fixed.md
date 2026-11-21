- **2026-11-21** — A finding whose runner-minute saving is real but unsized now
  says why in its group heading, `runner-minute saving: unsized (<reason>)`,
  instead of `no bill saving`. This covers OPT16's test-source build repeat,
  OPT25's positional splitter (whose card no longer calls the saving roughly
  neutral), every unsized `_size_finding` branch, the self-sizing detectors, and
  findings that an early collection exit never sized. A fix that removes no
  compute (OPT25 shard moves, OPT21 `needs:` edges, a measured zero OPT32 hit
  rate) keeps `no bill saving`, and a new verifier check fails any report that
  says otherwise (#455).
