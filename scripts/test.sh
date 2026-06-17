#!/usr/bin/env bash
#
# Run Relay's test suite so a FAILING suite ALWAYS fails the command -- even when
# the output is piped (e.g. `scripts/test.sh | tail`).
#
# The hazard this guards against: the convenience pattern `pytest ... | tail`
# returns *tail's* exit status (0), so a red suite gets masked as green and can be
# committed. This script runs pytest, shows a tail of the output, and then exits
# with PYTEST'S status -- so its own exit code is correct no matter how its output
# is piped. (We also set `pipefail` for any internal pipelines.)
#
# Usage:
#   scripts/test.sh                 # whole suite
#   scripts/test.sh tests/test_x.py # forward any pytest args (-k EXPR, a path, ...)
#
set -euo pipefail

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

# Capture full output; do NOT let a pipe swallow pytest's exit status.
set +e
uv run --extra dev python -m pytest "$@" >"$log" 2>&1
status=$?
set -e

# Show a tail for a quick read; the FULL log is in "$log" until this script exits.
tail -n 25 "$log"

if [ "$status" -ne 0 ]; then
  echo "FAILED: pytest exited with status ${status} (suite is RED -- do not commit)" >&2
fi
exit "$status"
