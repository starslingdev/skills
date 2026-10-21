#!/usr/bin/env bash
# Materialise the synthetic repository into this case's eval sandbox, so the
# security ask lands in a real checkout with real workflows to audit and the
# routing decision is the only thing under test. All the work is in
# ../_scaffold_common.sh.
set -euo pipefail
# shellcheck source=../_scaffold_common.sh
. "$(dirname "$0")/../_scaffold_common.sh"
