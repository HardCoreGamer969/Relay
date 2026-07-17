#!/usr/bin/env bash
#
# Run Relay's hermetic test suite so a FAILING suite ALWAYS fails the command
# -- even when the output is piped (e.g. `scripts/test.sh | tail`).
#
# The hazard this guards against: the convenience pattern `pytest ... | tail`
# returns *tail's* exit status (0), so a red suite gets masked as green and can
# be committed. This script runs pytest, shows a tail of the output, and then
# exits with PYTEST'S status.
#
# Usage:
#   scripts/test.sh                      # whole hermetic suite
#   scripts/test.sh tests/test_x.py      # forward any pytest args
#   RELAY_ALLOW_NETWORK=1 scripts/test.sh tests/live -m live   # live canary
#
# Env:
#   TTY_COMPATIBLE=0   (default here) — stable Rich output under dumb/pty hosts
#   RELAY_ALLOW_NETWORK=1 — skip the loopback-only socket block (needed for live)
#
set -euo pipefail

# Match CI / Cloud agent contract unless the caller already set it.
export TTY_COMPATIBLE="${TTY_COMPATIBLE:-0}"
# Hermetic catalog: don't attempt models.dev unless the caller opted into network.
if [[ "${RELAY_ALLOW_NETWORK:-}" != "1" ]]; then
  export RELAY_DISABLE_MODELS_FETCH="${RELAY_DISABLE_MODELS_FETCH:-1}"
fi

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

# Hermetic by default: Textual/asyncio loopback stays allowed; outbound blocked.
# Live tests (and anyone debugging provider calls) opt out with RELAY_ALLOW_NETWORK=1.
extra=()
if [[ "${RELAY_ALLOW_NETWORK:-}" != "1" ]]; then
  extra+=(--allow-hosts=127.0.0.1,::1,localhost)
fi

set +e
uv run --extra dev python -m pytest "${extra[@]}" "$@" >"$log" 2>&1
status=$?
set -e

tail -n 25 "$log"

if [ "$status" -ne 0 ]; then
  echo "FAILED: pytest exited with status ${status} (suite is RED -- do not commit)" >&2
fi
exit "$status"
