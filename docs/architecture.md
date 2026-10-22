# Architecture and design decisions

## Request path

```text
 client ──POST /calls──▶ app/api.py
                           │
                           ▼
                 EventProcessor (app/event_processor.py)
                   1. ledger.try_claim(event_id)      ── duplicate? return the stored result (200)
                   2. bulkhead semaphore (max in-flight calls)
                   3. ResilientPipelineClient (app/resilience.py)
                        for attempt in 1..max_attempts:
                          circuit_breaker.allow_request()?   ── no: fail fast "circuit_open"
                          wait_for(adapter.process(), attempt_timeout)
                          success → record_success → return
                          retryable error → record_failure → jittered backoff
                          non-retryable error → stop
                   4a. success → ledger.complete, resolve any DLQ row             (200 processed)
                   4b. failure → dlq.add (SQLite), ledger.release                 (202 dead_lettered)
                           │
                           ▼
                 PipelineAdapter (app/dependency_adapter.py)   ◀── the swappable boundary
                   HttpPipelineAdapter ──HTTP──▶ mock_dependency/api.py (Appendix B)
```

Replay (`POST /dlq/replay` or `python -m app.cli dlq replay`) claims each pending row,
then sends it through **the same** `EventProcessor.process`, with the same retries,
circuit breaker and ledger. Replay therefore can't bypass the safety rules or process
an event twice.

## Decisions

| Topic | Choice | Why |
|---|---|---|
| Retries | 3 attempts total, exponential backoff with full jitter (0–0.2 s, then 0–0.4 s, capped at 2 s) | With a 20% independent failure rate, three attempts leave about 0.2³ = 0.8% of calls failing. Jitter stops callers from retrying in lockstep. A small cap keeps voice-call latency acceptable. |
| Retryable errors | 503/502/504/500/429, timeouts, connection errors | These are transient. 4xx and malformed responses are permanent: they are not retried and not counted against the breaker. |
| Timeout | 2 s per attempt (`asyncio.wait_for`) | The mock's p99 is about 1.2 s. With sigma = 0.6, about 0.08% of successful calls take longer than 2 s. |
| Circuit breaker | Failure rate over the last 50 attempts; opens at ≥ 50% once ≥ 20 attempts are recorded; open for 10 s; 1 half-open probe | The first load test used a 20-attempt window, and it **falsely opened twice** under the normal 20% failure rate (P ≈ 0.26% per window position). At 50 the false-open probability is ≈ 2·10⁻⁶ per position, and a real outage still trips it after 20 attempts. Retries also check the breaker, so an outage stops all traffic at once. |
| Bulkhead | Semaphore, default 200 concurrent calls per process | Limits how much one process piles onto the dependency. |
| DLQ | SQLite table `dead_letters`, WAL mode | You can inspect it with any `sqlite3` client and it survives restarts. No infrastructure needed locally. Each row keeps event_id, campaign_id, payload, reason, attempts/retry_count, failure_count, timestamps, replay_attempts and status. |
| Idempotency | `event_ledger` table: atomic `INSERT OR IGNORE` claim → `succeeded` | The first writer wins, even across threads or processes sharing the file. Claims expire after a lease, so a crash can't block an event forever. A DLQ claim uses a conditional `UPDATE`, which also carries a lease. |
| Never lose an event | A failed DLQ write raises and the API returns 500; the claim is released | The caller learns the event was **not** stored and can resend it. Nothing is silently dropped. |
| Metrics | JSON `/metrics`: attempts, failures, retries, rejections, dependency failure rate vs final call failure rate, p50/p90/p99, in-flight, DLQ depth, circuit transitions | These are the numbers the challenge asks for. In-flight count is the planned autoscaling signal for Round 2. |

## Shared storage on AWS

Local SQLite works for one process. With several Fargate tasks each task would have its
own file, so the same `event_id` could be processed twice, the dead-letter queue would be
split across tasks, and all of it would be lost on the next deployment.

**Both stores are therefore DynamoDB tables**, selected by `APP_STORE_BACKEND=dynamodb`:

| Store | Table | How exclusivity is enforced |
|---|---|---|
| Idempotency ledger | `<project>-event-ledger`, PK `event_id` | `PutItem` guarded by `attribute_not_exists(event_id)` — the conditional put, evaluated atomically |
| Dead-letter records | `<project>-dead-letters`, PK `event_id`, GSI `status-seq-index` | `UpdateItem` with a `ConditionExpression` on status and lease age |

An earlier draft of this plan put the dead-letter queue in **SQS**. That does not work.
SQS is a message pipe: no lookup by key, no filter or count by status, no in-place
update. It cannot serve `GET /dlq`, `GET /dlq/{event_id}`, `depth()`/`counts()` for
`/metrics`, or the `claim → resolve`/`unclaim` lifecycle that `app/dlq.py` defines. A
dead letter is a *record to be inspected and replayed*, not a message to be consumed
once, so it needs a queryable store. SQS keeps a narrower role as an optional durable
replay trigger (`enable_replay_queue` in the Terraform stack), off by default.

Implementation notes:

- `app/stores.py` holds the two protocols. `app/dlq.py` and `app/idempotency.py` keep the
  SQLite implementations for local development, which is why the quick start in
  `README.md` still needs no AWS account.
- `tests/unit/test_store_parity.py` runs one suite against both backends, so a divergence
  in semantics fails the build rather than appearing in production.
- `seq`, a nanosecond stamp fixed at first insert, reproduces the insertion ordering that
  SQLite's `AUTOINCREMENT` id gave `list()`. It survives a re-add, so a record that fails
  a replay keeps its place.
- `list()`, `counts()` and `depth()` read the GSI, which cannot be strongly consistent, so
  they may briefly miss a very recent write. Correctness does not rest on them: `claim()`
  is a conditional write, so a replayer acting on a stale listing still cannot
  double-process a record.
- The ledger stamps `expires_at` on completion so succeeded records age out. This puts a
  horizon on the guarantee — a duplicate arriving after `APP_LEDGER_TTL_DAYS` is treated
  as new. Set it to 0 to keep records forever.
- Store calls are blocking, so they run on a thread pool. SQLite requires exactly one
  thread; DynamoDB calls are network round-trips, so `APP_DB_THREADS` widens the pool
  there instead of serializing every call.

The circuit breaker stays per task, which is intentional: each task protects itself
without coordinating with others.

## Swapping the mock for real open-weight models

The resilience layer only depends on the `PipelineAdapter` protocol:
`async process(PipelineRequest) -> PipelineResult`, raising
`DependencyError(reason, retryable=...)`. Swapping in real models means adding one class,
for example `OpenWeightPipelineAdapter`, that runs the three stages:
- **Speech to text:** faster-whisper `large-v3-turbo` (MIT).
- **LLM:** `Mistral-7B-Instruct-v0.3` (Apache 2.0), served by vLLM's OpenAI-compatible
  HTTP server. Llama 3.1 uses the Llama Community License, not Apache 2.0, so it needs a
  licence review before an Apache-2.0 release.
- **Text to speech:** Piper. Check the licences: the original `rhasspy/piper` is MIT, its
  successor `piper1-gpl` is GPL-3.0, and each voice carries its own licence. Coqui
  XTTS-v2's model licence is non-commercial.

The adapter maps its stage failures onto the existing contract:
- **Retryable:** GPU out-of-memory, the vLLM queue being full, upstream 5xx and
  per-stage timeouts.
- **Not retryable:** undecodable audio and prompts rejected by guardrails.

Nothing in `resilience.py`, `circuit_breaker.py`, `dlq.py` or `event_processor.py` changes.
Only `wiring.build()` picks the new adapter from configuration. The models run as a
separate GPU service, for example ECS on a g5.xlarge (A10G, 24 GB) EC2 capacity
provider, because Fargate has no GPUs. The adapter calls that service over HTTP, so the
per-attempt timeout, breaker and DLQ protect the GPU tier just as they protect the mock.
Hindi and other Indian-language quality should be evaluated per stage before choosing
models: Whisper supports Hindi, but 7B instruction models vary widely.
