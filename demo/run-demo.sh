#!/usr/bin/env bash
# Drives the whole demo end to end against the real service, paced for screen
# recording. Start your recorder, run this, narrate over it.
#
#   demo/run-demo.sh              # ~5 minutes, normal pacing
#   PAUSE=0.5 demo/run-demo.sh    # faster, for a dry run
#
# Everything here is real: a real service, the real Appendix B mock, real HTTP
# calls. Nothing is pre-recorded or faked. Localhost only - no credentials and
# no AWS are involved.

set -uo pipefail
cd "$(dirname "$0")/.."

PAUSE="${PAUSE:-2}"
PY="${PYTHON:-.venv/bin/python}"
MOCK_PORT="${MOCK_PORT:-8201}"
APP_PORT="${APP_PORT:-8200}"
MOCK="http://127.0.0.1:$MOCK_PORT"
SVC="http://127.0.0.1:$APP_PORT"
DB="$(mktemp -d)/demo.db"

bold=$'\e[1m'; dim=$'\e[2m'; grn=$'\e[32m'; ylw=$'\e[33m'; cyn=$'\e[36m'; off=$'\e[0m'

step() { printf '\n%s\n%s  %s%s\n%s\n' "${cyn}────────────────────────────────────────────────────────────${off}" \
         "${bold}${cyn}" "$*" "${off}" "${cyn}────────────────────────────────────────────────────────────${off}"; sleep "$PAUSE"; }
say()  { printf '%s>> %s%s\n' "$dim" "$*" "$off"; sleep "$PAUSE"; }
run()  { printf '%s$ %s%s\n' "$ylw" "$*" "$off"; eval "$@"; echo; sleep "$PAUSE"; }

cleanup() { kill "${MOCK_PID:-}" "${APP_PID:-}" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

wait_up() { for _ in $(seq 1 50); do curl -sf "$1/health" >/dev/null 2>&1 && return 0; sleep 0.3; done; return 1; }

event() { # $1=id $2=time
  printf '{"event_id":"%s","campaign_id":"CAMP-2026-10-A","borrower_bucket":"0-30 DPD",' "$1"
  printf '"product_type":"bnpl","timestamp":"%s","outcome":"connected"}' "$2"
}

clear
cat <<'BANNER'
  Collections Voice-Agent Challenge
  Resilient inference service for an STT -> LLM -> TTS pipeline

  The dependency fails ~20% of the time. The service must never
  silently lose a call - in collections that is a compliance gap,
  not just a bad user experience.
BANNER
sleep "$PAUSE"

step "1. The repository"
run "git log --oneline -3"
run "ls app/"
say "app/ holds the service. mock_dependency/ is the Appendix B mock."
say "load_tests/ replays the Appendix C profile. infra/terraform/ is the AWS stack."

step "2. Start the mock dependency and the service"
say "Mock: 20% injected failures, lognormal latency. Service: retries, breaker, DLQ."
$PY -m uvicorn mock_dependency.api:app --port "$MOCK_PORT" --log-level warning >/tmp/demo-mock.log 2>&1 &
MOCK_PID=$!
APP_DEPENDENCY_URL="$MOCK" APP_DB_PATH="$DB" APP_CB_OPEN_S=8 APP_LOG_LEVEL=WARNING \
  $PY -m uvicorn app.api:app_from_env --factory --port "$APP_PORT" --log-level warning >/tmp/demo-app.log 2>&1 &
APP_PID=$!
wait_up "$SVC" || { echo "service failed to start"; tail -20 /tmp/demo-app.log; exit 1; }
run "curl -s $SVC/health"

step "3. A successful call"
run "curl -s -X POST $SVC/calls -H 'content-type: application/json' -d '$(event demo-0001 2026-10-01T09:03:12Z)'"
say "HTTP 200, status processed."

step "4. The same event again - idempotency"
run "curl -s -X POST $SVC/calls -H 'content-type: application/json' -d '$(event demo-0001 2026-10-01T09:03:12Z)'"
say "status duplicate, and the stored result comes back from the ledger."
say "The pipeline did NOT run a second time."

step "5. Downstream goes down - retries then dead-letter"
run "curl -s -X PUT $MOCK/admin/failure-rate -H 'content-type: application/json' -d '{\"failure_rate\": 1.0}'"
say "The dependency now fails 100% of requests."
run "curl -s -X POST $SVC/calls -H 'content-type: application/json' -d '$(event demo-0002 2026-10-01T09:04:00Z)'"
say "HTTP 202 dead_lettered after 3 bounded attempts. Captured, not dropped."

step "6. The circuit breaker opens"
say "Keep calling a dependency that is down. The breaker should stop us hammering it."
printf '%s$ for i in $(seq 1 30); do curl -s -o /dev/null -X POST %s/calls ... ; done%s\n' "$ylw" "$SVC" "$off"
for i in $(seq 1 30); do
  curl -s -o /dev/null -X POST "$SVC/calls" -H 'content-type: application/json' \
    -d "$(event "cb-$i" 2026-10-01T09:05:00Z)"
done
echo
run "curl -s $SVC/health"
say "circuit_state is OPEN. That is the good outcome: we now fail fast in"
say "milliseconds instead of queueing retries against a dead dependency."

step "7. Recovery - half-open, then closed"
run "curl -s -X PUT $MOCK/admin/failure-rate -H 'content-type: application/json' -d '{\"failure_rate\": 0.0}'"
say "Dependency is healthy again. Waiting for the breaker's open window..."
sleep 9
run "curl -s -X POST $SVC/calls -H 'content-type: application/json' -d '$(event demo-0003 2026-10-01T09:06:00Z)'"
run "curl -s $SVC/health"
say "A trial call succeeded in half-open, so the breaker closed. Automatic recovery."

step "8. The dead-letter queue is inspectable"
run "curl -s '$SVC/dlq?status=pending&limit=3' | $PY -m json.tool | head -28"
say "Full original payload, failure reason, attempt count, timestamps."
run "curl -s $SVC/dlq/demo-0002 | $PY -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in (\"event_id\",\"status\",\"failure_reason\",\"attempts\",\"failure_count\")}, indent=2))'"

step "9. Replay drains it"
run "curl -s $SVC/metrics | $PY -c 'import sys,json; print(\"dlq before:\", json.load(sys.stdin)[\"dlq\"])'"
run "curl -s -X POST $SVC/dlq/replay -H 'content-type: application/json' -d '{\"limit\":500,\"concurrency\":4}' | $PY -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in (\"attempted\",\"resolved\",\"already_processed\",\"failed_again\")}, indent=2))'"
run "curl -s $SVC/metrics | $PY -c 'import sys,json; print(\"dlq after: \", json.load(sys.stdin)[\"dlq\"])'"
say "Depth is zero. Replay is safe to run repeatedly: each record is claimed with"
say "a conditional write, and the ledger blocks anything that already succeeded."

step "10. Tests"
say "Running the resilience and DLQ unit tests live (the full suite is 221 tests)."
run "$PY -m pytest -q tests/unit/test_circuit_breaker.py tests/unit/test_dlq.py tests/unit/test_resilience.py 2>&1 | tail -4"
say "Full recorded run, every test, in evidence/round1/test-run.txt:"
run "tail -2 evidence/round1/test-run.txt"

step "11. Load test against the Appendix C profile"
say "A full simulated day, 3,670 calls, recorded earlier:"
run "$PY -c \"
import json
s=json.load(open('evidence/round1/load-tests/full-day-24h/report.json'))['summary']
for k in ('calls_sent','lost_or_errored','initial_dependency_failure_rate','final_call_failure_rate_before_replay','retries','dlq_depth_after_run','replayed_ok','dlq_depth_after_replay'):
    print(f'  {k:38} {s[k]}')
\""
say "An injected dependency failure rate of 0.201 becomes a call failure rate of"
say "0.0079 - the theoretical 0.20 cubed for three attempts. Zero calls lost."
say "The outage run also shows open -> half_open -> closed and 105 replayed to zero."

step "12. Cost, and what this does NOT do"
run "head -14 docs/cost-analysis.md"
say "Config B recommended: lower floor, finer scale-out, multi-AZ for A's price."
say "About \$72/month of that is fixed ALB + NAT + IPv4 that never scales down."
printf '\n%sScope - stated honestly:%s\n' "$bold" "$off"
printf '  %s- Round 1 is complete and reproducible locally with no AWS account.%s\n' "$grn" "$off"
printf '  %s- Round 2 is finalists-only; eligibility was never confirmed.%s\n' "$ylw" "$off"
printf '  %s- NO GPU and NO open-weight model were ever deployed. This is the mock.%s\n' "$ylw" "$off"
printf '  %s- The Terraform stack was applied, verified, then destroyed. Evidence in%s\n' "$ylw" "$off"
printf '  %s  evidence/aws-deployment/, including teardown confirmation.%s\n' "$ylw" "$off"
printf '  %s- Cost figures are Price List API estimates, not a measured bill.%s\n' "$ylw" "$off"
sleep "$PAUSE"

step "Done"
echo "  github.com/Girish7010/collections-voice-agent-challenge"
echo "  docs/runbook.md  -  written so a teammate can run an incident from it alone"
echo
