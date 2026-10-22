#!/usr/bin/env bash
# Reproducible local load test: fresh mock + fresh service + traffic generator.
#
# Usage:
#   scripts/run_load_test.sh [traffic_generator args...]
#
# Examples:
#   # PDF example compression (1 sim hour = 1 real minute), campaign window A, with an outage
#   scripts/run_load_test.sh --start 07:30 --duration-min 180 --compression 60 --rate-scale 10 \
#       --outage-start 09:00 --outage-min 10
#
#   # full simulated day in 6 real minutes
#   scripts/run_load_test.sh --start 00:00 --duration-min 1440 --compression 240 --rate-scale 40
#
# Everything for a run (report JSON, console output, service logs, SQLite DB)
# is written to results/<timestamp>/. Ports can be changed with MOCK_PORT / APP_PORT.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
MOCK_PORT="${MOCK_PORT:-8101}"
APP_PORT="${APP_PORT:-8100}"
RUN_DIR="results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT

wait_healthy() {
  for _ in $(seq 1 100); do
    curl -sf "$1/health" >/dev/null && return 0
    sleep 0.2
  done
  echo "service at $1 did not become healthy" >&2
  exit 1
}

MOCK_SEED="${MOCK_SEED:-2026}" "$PY" -m uvicorn mock_dependency.api:app --port "$MOCK_PORT" \
  --log-level warning >"$RUN_DIR/mock.log" 2>&1 &
pids+=($!)
APP_DEPENDENCY_URL="http://127.0.0.1:$MOCK_PORT" APP_DB_PATH="$RUN_DIR/collections.db" \
  "$PY" -m uvicorn app.api:app_from_env --factory --port "$APP_PORT" \
  --log-level warning >"$RUN_DIR/service.log" 2>&1 &
pids+=($!)

wait_healthy "http://127.0.0.1:$MOCK_PORT"
wait_healthy "http://127.0.0.1:$APP_PORT"

"$PY" -m load_tests.traffic_generator \
  --target "http://127.0.0.1:$APP_PORT" --mock-url "http://127.0.0.1:$MOCK_PORT" \
  --output "$RUN_DIR/report.json" "$@" | tee "$RUN_DIR/console.txt"

echo "run directory: $RUN_DIR"
