- **2026-11-21** (#480) — **Fixed**: Score real CI whose workflow directly
  invokes a repository script containing an unambiguous test-runner command,
  while keeping incidental command names in automation scripts refused.
- **2026-11-23** (#480) — **Fixed**: The delegated-test signal also follows
  `bash scripts/ci.sh` / `sh ./test.sh`, scripts given arguments or piped to a
  log, `npm run` / `yarn` / `pnpm run` / `bun run` package.json scripts, and
  runners behind `uv run`, `poetry run` or `bundle exec`. It resolves the script
  from the step's (or job's default) working directory, and keeps refusing a
  runner name in heredoc text, a non-shell script, a dynamic working directory,
  or a script outside the checkout or unreadable.
