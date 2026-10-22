# Collections Voice-Agent Challenge: resilient inference service

A resilient inference service for a collections voice agent (STT → LLM → TTS), built for
the Predixion AI × TalentX Open-Weight Collections Agent Challenge (Round 1, local track).

It calls a pipeline that fails about 20% of the time and **never silently loses a call**:
- Failures are retried with jittered exponential backoff and per-attempt timeouts.
- A circuit breaker stops traffic to a dependency that is clearly down.
- Calls that still fail go to an inspectable SQLite dead-letter queue (DLQ). The DLQ can
  be replayed, and replay is idempotent (no event is processed twice).

All data is synthetic. Licensed under [Apache 2.0](LICENSE).

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [API](#api)
- [Dead-letter queue](#dead-letter-queue-inspect-and-replay)
- [Load test and results](#load-test-traffic-profile-appendix-c)
- [Tests](#tests)
- [Mock dependency](#mock-dependency-appendix-b)
- [Scope and limitations](#scope-and-limitations)
- [Measured results](docs/results.md) · [Runbook](docs/runbook.md) · [Evidence](evidence/)
- [Cost analysis](docs/cost-analysis.md) · [Architecture and model swap](docs/architecture.md)

## Quick start

Prerequisites: Python 3.11+ (developed on 3.14, CI on 3.12) and `curl`. Docker is
optional. No AWS account is needed.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                   # unit + integration + end-to-end tests
```

Run the two services in two terminals:

```bash
uvicorn mock_dependency.api:app --port 8001                          # fake STT/LLM/TTS
APP_DEPENDENCY_URL=http://localhost:8001 \
  uvicorn app.api:app_from_env --factory --port 8000                 # inference service
```

Send a call:

```bash
curl -s -X POST localhost:8000/calls -H 'content-type: application/json' -d '{
  "event_id": "demo-0001", "campaign_id": "CAMP-2026-10-A", "borrower_bucket": "0-30 DPD",
  "product_type": "bnpl", "timestamp": "2026-10-01T09:03:12Z", "outcome": "connected"}'
```

Or with Docker: `docker compose up --build` starts the mock on `:8001` and the service on `:8000`.

Interactive API docs: http://localhost:8000/docs (service) and http://localhost:8001/docs (mock).

## How it works

```text
POST /calls ─▶ duplicate check (ledger) ─▶ circuit breaker ─▶ adapter ─▶ mock /pipeline/process
                    │ already done                │ open            │ fail (retryable)
                    ▼                             ▼                 ▼
               200 duplicate                fail fast       backoff + retry (max 3 attempts)
                                                  │                 │ still failing
                                                  └──────▶ DLQ (SQLite) ◀┘ ─▶ 202 dead_lettered
                                                          │
                                                 replay ──┘ (same path, idempotent)
```

| Piece | File | In one line |
|---|---|---|
| Model-agnostic boundary | [app/dependency_adapter.py](app/dependency_adapter.py) | `PipelineAdapter.process()`. The only thing that knows about the mock's HTTP API. |
| Retries + timeouts | [app/resilience.py](app/resilience.py) | 3 attempts, full-jitter exponential backoff, 2 s per-attempt timeout, breaker checked before every attempt. |
| Circuit breaker | [app/circuit_breaker.py](app/circuit_breaker.py) | Failure rate over the last 50 attempts; opens at 50% (min 20 attempts); 10 s open; half-open probe. |
| Dead-letter queue | [app/dlq.py](app/dlq.py) | SQLite table with claim/lease replay. |
| Idempotency | [app/idempotency.py](app/idempotency.py) | Atomic per-event claim, so each event succeeds at most once. |
| Orchestration | [app/event_processor.py](app/event_processor.py) | Ledger → resilient call → complete, or DLQ. Also replay. |
| API / CLI | [app/api.py](app/api.py), [app/cli.py](app/cli.py) | HTTP endpoints and the `dlq` command-line tool. |
| Metrics | [app/metrics.py](app/metrics.py) | Dependency failure rate vs final call failure rate, latencies, in-flight calls, breaker transitions. |

Retryable errors are 503/502/504/500/429, timeouts and connection errors. 4xx responses
and malformed replies are **not** retried, and they don't count against the breaker.
The reasoning for every number above (including a breaker window we changed after a
measured false trip) is in [docs/architecture.md](docs/architecture.md).

## API

| Method | Path | Result |
|---|---|---|
| POST | `/calls` | Body: an Appendix A event. Returns `200 processed`, `200 duplicate` (stored result, pipeline not called), `202 dead_lettered` (stored for replay), `409 in_progress` (same id being processed right now), or `422` (invalid input). A `500` means the event could **not** be persisted, so the caller should resend. |
| GET | `/health` | `{"status":"ok","circuit_state":"closed"}` |
| GET | `/metrics` | Counters, `dependency.failure_rate`, `final_call_failure_rate`, latency p50/p90/p99, `in_flight`, DLQ depth, circuit transitions |
| GET | `/dlq?status=pending&limit=50` | List dead letters with counts by status |
| GET | `/dlq/{event_id}` | One dead letter |
| POST | `/dlq/replay` | `{}` replays all pending; `{"event_ids":[...]}` replays specific ones |

Configuration uses `APP_*` environment variables; see [.env.example](.env.example).

## Dead-letter queue: inspect and replay

Each failed event is one row in `dead_letters`, with these fields: event_id, campaign_id,
full payload, failure_reason, attempts, retry_count, failure_count, first/last failed time,
status (`pending` → `replaying` → `resolved`), replay_attempts, and resolved_by
(`replay` / `resubmission` / `already_processed`).

```bash
python -m app.cli dlq stats                       # {"depth": 3, "counts": {"pending": 3}}
python -m app.cli dlq list --status pending       # table
python -m app.cli dlq show <event_id>             # full JSON
python -m app.cli dlq replay                      # replay all pending (uses APP_DEPENDENCY_URL)
python -m app.cli dlq replay --event-id <id>      # replay one
sqlite3 data/collections.db 'select event_id, failure_reason, status from dead_letters'
```

**Why replay can't double-process an event.**
1. A replayer must first *claim* a row. Claiming is a single conditional `UPDATE` that
   only one caller can win. The claim carries a lease, so a crashed replayer doesn't
   leave a row stuck.
2. Every event, whether it arrives live, is resubmitted or is replayed, goes through the
   **idempotency ledger**. An `INSERT OR IGNORE` claim means only one worker processes an
   event, and once it has succeeded, later attempts return the stored result without
   calling the pipeline.
3. Replay stops early if the circuit breaker is open, so it doesn't churn the queue
   during an outage.

Tests cover two replayers running at once and the same event replayed twice
([tests/unit/test_event_processor.py](tests/unit/test_event_processor.py)).

## Load test: traffic profile (Appendix C)

[load_tests/traffic_generator.py](load_tests/traffic_generator.py) replays the daily
profile with Poisson arrivals: 10 calls/min, ramping to 50 calls/min over 15 minutes in
the two campaign windows 08:00–10:00 and 14:00–16:00. The events are synthetic Appendix A
records.

**Time compression is explicit:**
- `--compression 60` means **1 simulated hour = 1 real minute**.
- `--rate-scale` sets how many real calls are sent per simulated call-per-minute. With
  `1`, the profile's 10–50 calls/min are sent per real minute. With a value equal to
  `--compression`, the full day's volume is sent in compressed time.

The report converts everything back into calls per simulated minute, so you can compare
achieved and target rates directly.

`--outage-start/--outage-min` set the mock to 100% failure for a simulated window (through
the mock's test-only `PUT /admin/failure-rate`). This shows the breaker opening and the
DLQ filling and then draining.

```bash
# Campaign window A with a 10-minute outage, 3 real minutes (PDF's 1 h = 1 min compression)
scripts/run_load_test.sh --start 07:30 --duration-min 180 --compression 60 --rate-scale 10 \
    --outage-start 09:00 --outage-min 10

# A full simulated day in 6 real minutes
scripts/run_load_test.sh --start 00:00 --duration-min 1440 --compression 240 --rate-scale 40
```

The script starts a fresh mock and service on ports 8100/8101 and runs the generator. It
writes `report.json`, `console.txt` and the service logs to `results/<timestamp>/`, then
replays the DLQ.

### Measured results (local laptop, 2026-10-21)

Two recorded runs, full numbers and evidence in [docs/results.md](docs/results.md).

**Full simulated day, no outage** (3,670 calls):

| Metric | Measured |
|---|---|
| Calls lost or errored | **0** |
| Dependency failure rate (injected) | 0.201 |
| Final call failure rate after retries | **0.0079** — theory for 3 attempts is 0.20³ = 0.008 |
| DLQ after run → after replay | 29 → **0** |
| Client latency | p50 390 ms · p90 749 ms · p99 1354 ms |

**Campaign window with a 10-minute total outage** (997 calls):

| Metric | Measured |
|---|---|
| Calls lost or errored | **0** |
| Circuit transitions | **`open → half_open → closed`** |
| Circuit rejections (failed fast) | 95 |
| DLQ after run → after replay | 105 → **0** |

The first run is the headline: a 0.201 dependency failure rate becomes a 0.0079 call
failure rate — a 25× reduction that lands on the theoretical value for three bounded
attempts. The second shows the breaker opening under a total outage, recovering
automatically, and a replay draining the queue to zero.

## Tests

```bash
pytest -q                      # everything
pytest tests/unit -q           # fast, deterministic, no network
pytest -m integration -q       # real uvicorn servers + traffic generator
```

| Required behaviour | Where it's tested |
|---|---|
| Successful processing | `test_event_processor.py::test_successful_call_is_processed`, `test_api.py` |
| Retry then success | `test_resilience.py::test_retry_then_success` |
| Exhausted retries → DLQ | `test_resilience.py::test_exhausted_retries_report_last_reason`, `test_event_processor.py::test_exhausted_call_is_dead_lettered_with_details` |
| Circuit opening / fail fast | `test_circuit_breaker.py`, `test_resilience.py::test_failures_open_the_circuit_and_later_calls_fail_fast` |
| Half-open recovery | `test_circuit_breaker.py::test_half_open_success_closes_and_resets_window`, `test_resilience.py::test_half_open_probe_success_recovers` |
| Timeouts | `test_resilience.py::test_slow_attempt_times_out_and_is_retried` |
| DLQ persistence | `test_dlq.py` (fresh instance reads the row back from disk; plain `sqlite3` can read it) |
| DLQ replay | `test_event_processor.py::test_replay_*`, `test_api.py::test_replay_*` |
| Idempotency | `test_event_processor.py::test_concurrent_duplicates_only_call_dependency_once`, `test_replaying_twice_does_not_process_twice`, `test_concurrent_replays_process_each_event_once` |
| Load test (reproducible, seeded) | `tests/integration/test_end_to_end.py`: ramp with 20% failures loses 0 calls; outage opens the breaker and replay drains the DLQ |

## Mock dependency (Appendix B)

[mock_dependency/api.py](mock_dependency/api.py): `POST /pipeline/process` returns `200`
with a synthetic transcript after a lognormal delay, or, independently on ~20% of
requests, `503 {"status":"error","reason":"downstream_timeout"}`. Configure it with
`MOCK_FAILURE_RATE`, `MOCK_LATENCY_MU`, `MOCK_LATENCY_SIGMA`, `MOCK_FAILURE_LATENCY_MS`
and `MOCK_SEED`.

**Latency calibration.** The PDF suggests `lognormvariate(5.7, 0.35)` and asks entrants
to check the percentiles. Measured over 20,000 samples
(`python scripts/check_mock_latency.py`):

| Percentile | Target | sigma 0.35 | **sigma 0.60 (default)** |
|---|---|---|---|
| p50 | 300 ms | 299 | 299 |
| p90 | 700 ms | 468 (−33%) | 645 (−8%) |
| p99 | 1200 ms | 678 (−44%) | 1218 (+1.5%) |

No single lognormal with a 300 ms median hits both tails exactly. sigma = 0.60 matches the
p99, which drives timeout and retry behaviour. To reproduce the PDF's value, set
`MOCK_LATENCY_SIGMA=0.35`.

## Other deliverables

- **Measured results** (every number backed by a recorded run):
  [docs/results.md](docs/results.md), with raw artifacts in [evidence/](evidence/).
- **Runbook** (Appendix I): [docs/runbook.md](docs/runbook.md) — triggers, first-5-minutes
  triage, downstream vs autoscaling vs store, recovery and replay.
- **Cost analysis** (Fargate config A vs B, current ap-south-1 prices):
  [docs/cost-analysis.md](docs/cost-analysis.md). Reproduce it with
  `python scripts/cost_estimate.py`.
- **Swapping the mock for real open-weight models**: [docs/architecture.md](docs/architecture.md#swapping-the-mock-for-real-open-weight-models)
- **Sample data**: [sample_data/call_events.jsonl](sample_data/call_events.jsonl) has 500
  synthetic Appendix A records (`python scripts/generate_sample_events.py`).

## Repository layout

```text
app/               inference service (API, resilience, breaker, DLQ, ledger, metrics, CLI)
mock_dependency/   Appendix B mock STT -> LLM -> TTS
load_tests/        Appendix C traffic profile + generator
scripts/           load-test runner, latency calibration, cost estimate, sample data
docs/              architecture, cost analysis, measured results, runbook
infra/terraform/   Round 2 AWS stack (ECS Fargate, ALB, DynamoDB, IAM) - see limitations
evidence/          recorded test and load-test artifacts
tests/             unit / integration / load-profile tests
```

## Scope and limitations

Stated up front so nothing in this repository is over-claimed.

**Round 1 (local track) is complete.** Everything in [Measured results](docs/results.md)
is reproducible on a laptop with no AWS account.

**Round 2 (AWS GPU track) was not completed.** The challenge PDF describes it as
finalists-only by invitation, and eligibility was never confirmed, so the GPU track was
not attempted. Specifically:

- **No open-weight model and no GPU were ever deployed.** The STT → LLM → TTS pipeline is
  the Appendix B mock throughout. `docs/architecture.md` describes how a real model would
  be swapped in, and why it needs an EC2/ECS GPU capacity provider rather than Fargate.
- The Terraform in `infra/terraform/` is a complete, applied-and-verified ECS Fargate
  stack (`terraform validate` passes; it was deployed to ap-south-1, confirmed healthy,
  and then destroyed). Evidence: [evidence/aws-deployment/](evidence/aws-deployment/).
- It ran with a **placeholder dependency URL**, so the Appendix C traffic profile was
  never replayed against the deployed service. All load results here are local.
- **Not built:** CloudWatch dashboard, and custom-metric autoscaling on in-flight calls
  (the stack scaled on `ALBRequestCountPerTarget`, a proxy). The service already exposes
  `in_flight` at `/metrics`; nothing published it to CloudWatch.
- **Cost figures are Price List API estimates, not a measured bill.**
