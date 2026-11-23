#!/usr/bin/env bash
# CI entry point for the public-output privacy gate (AGENTS.md). PR text arrives only
# through env vars, never interpolated into the workflow, and is scanned from temp files.
# PR_AUTHOR is github.event.pull_request.user.login (the event payload, not PR text); unset
# or empty means no Dependabot exemption.
set -euo pipefail
title="$(mktemp)"
body="$(mktemp)"
printf '%s\n' "$PR_TITLE" > "$title"
printf '%s\n' "$PR_BODY" > "$body"
exec python3 .github/scripts/check_public_output.py --target "$GITHUB_REPOSITORY" \
  --text-file "$title" --text-file "$body" --base "origin/$BASE_REF" \
  --pr-author "${PR_AUTHOR:-}" \
  --allow home-assistant/core --allow vercel/next.js \
  --allow ethereum/go-ethereum --allow microsoft/playwright \
  --allow openclaw/openclaw --allow openclaw/crabbox
