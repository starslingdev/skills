- **2026-11-23** — Report an install whose directory option embeds an
  expression (`pnpm -C apps/${{ matrix.package }} install`,
  `yarn --cwd ${{ matrix.package }}`) with its target pin unverifiable instead
  of dropping it, and keep that statement in the pnpm note when a committed
  build allowlist is present (#477).
