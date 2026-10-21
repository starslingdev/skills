#!/usr/bin/env bash
# Materialise the synthetic repository into this case's eval sandbox.
# All the work is in ../_scaffold_common.sh.
set -euo pipefail
# shellcheck source=../_scaffold_common.sh
. "$(dirname "$0")/../_scaffold_common.sh"
