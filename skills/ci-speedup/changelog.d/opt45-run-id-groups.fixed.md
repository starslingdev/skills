- **2026-11-24** — OPT45's concurrency recipe gives every default-branch and tag run
  its own `github.run_id` group, so a newer merge can no longer replace a
  waiting one; other branches keep the repository-qualified branch group (#493).
