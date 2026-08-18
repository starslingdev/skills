#!/usr/bin/env bash
# Materialize the 'inject' fixture workflows into this case's eval sandbox.
# All the work is in ../_scaffold_common.sh; this file only names the fixture.
set -euo pipefail
FIXTURE_SLUG=inject
# shellcheck source=../_scaffold_common.sh
. "$(dirname "$0")/../_scaffold_common.sh"
