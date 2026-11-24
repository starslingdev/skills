- **2026-11-24** — OPT45's concurrency recipe gives every default-branch and tag run
  its own `github.run_id` group, so a newer merge can no longer replace a
  waiting one; other branches keep the repository-qualified branch group. Its
  push-only routing now points at OPT46's combined block, and OPT45, OPT46 and
  the OPT46 finding's guardrail say the one-pending-run limit is GitHub's
  default (`queue: single`) (#493).
