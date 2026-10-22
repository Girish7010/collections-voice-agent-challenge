# Runbook: Collections Voice-Agent Inference Service

Follows the Appendix I template, filled in with this service's actual endpoints,
metric names and configuration. Commands assume `$SVC` is the service base URL
(`http://localhost:8000` locally, or the ALB DNS name when deployed).

```bash
export SVC=http://localhost:8000
```

> **Scope note.** The AWS deployment this runbook was written against has been torn
> down (Round 2 eligibility was not confirmed). The AWS sections below are written
> from the Terraform in `infra/terraform/` and from evidence captured while the stack
> was live (`evidence/aws-deployment/`). Sections marked **NOT BUILT** were never
> deployed and are listed so a reader is not misled.

---

## Trigger

p99 latency > 3× baseline during a live campaign window, or any of:

| Signal | Where | Healthy baseline |
|---|---|---|
| `latency.p99_ms` | `GET /metrics` | ~1200 ms (mock dependency p99) |
| `dependency.failure_rate` | `GET /metrics` | ~0.20 by design (Appendix B) |
| `final_call_failure_rate` | `GET /metrics` | < 0.01 after retries |
| `dlq.depth` | `GET /metrics` | 0 when healthy |
| `circuit_state` | `GET /health` | `closed` |

---

## First 5 minutes

**1. Get the whole picture in one call.**

```bash
curl -s $SVC/metrics | python3 -m json.tool
```

Read in this order: `circuit_state` → `dependency.failure_rate` →
`final_call_failure_rate` → `dlq.depth` → `latency.p99_ms` → `in_flight`.

**2. Is this ours or theirs?**

The service reports the dependency's failure rate *separately* from the final
call failure rate. That distinction is the whole diagnosis:

- `dependency.failure_rate` high **and** `final_call_failure_rate` low
  → the downstream is failing and **retries are absorbing it**. Working as designed.
- **Both** high → retries are exhausting. Go to *Downstream is the cause*.
- `dependency.failure_rate` normal but latency high → look at us, not them:
  saturation or scaling lag.

**3. Check the circuit breaker.**

```bash
curl -s $SVC/health
```

`"circuit_state":"open"` is **good** under a downstream outage — it means we are
failing fast instead of hammering a dead dependency. `closed` while the dependency
is clearly down is the bad case: the breaker is not tripping (see below).

---

## If downstream is the cause

Confirm the breaker tripped. If it did not, check the thresholds actually in force:

| Setting | Env var | Default |
|---|---|---|
| Failure-rate threshold | `APP_CB_FAILURE_THRESHOLD` | 0.5 |
| Rolling window | `APP_CB_WINDOW` | 50 |
| Minimum calls before evaluating | `APP_CB_MIN_CALLS` | 20 |
| Open duration before half-open | `APP_CB_OPEN_S` | 10 s |

A breaker that never trips is usually `APP_CB_MIN_CALLS` set above the actual
call volume — the window never fills, so the rate is never evaluated.

Then confirm nothing is being dropped:

```bash
curl -s "$SVC/dlq?status=pending&limit=20" | python3 -m json.tool
```

Every exhausted call must appear here. `lost_or_errored` in a load-test report
must be `0`; if it is not, that is a correctness bug, not an incident.

---

## If autoscaling is the cause

**NOT BUILT — honest limitation.** The deployed stack scaled on
`ALBRequestCountPerTarget`, a proxy. The PDF asks for scaling on *concurrent
in-flight calls or queue depth*. The service already exposes `in_flight` at
`/metrics`, but nothing published it to CloudWatch, so a target-tracking policy
could not consume it. See "Known gaps" in `infra/terraform/README.md`.

When a stack is running, check:

```bash
aws ecs describe-services --cluster collections-agent --services collections-agent \
  --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount}'
aws ecs describe-services --cluster collections-agent --services collections-agent \
  --query 'services[0].events[0:10].message' --output text
```

- **Running < desired, pending > 0 for minutes** → tasks cannot start. Usually an
  image pull failure or subnet IP exhaustion. Check the stopped tasks' reason:
  `aws ecs list-tasks --cluster collections-agent --desired-status STOPPED`.
- **Desired stuck at the floor during a ramp** → the scaling policy is not firing.
  Fargate tasks take 30–60 s to start, so a 15-minute ramp needs the policy to
  react on a *leading* signal. `scale_out_cooldown` is 60 s; `scale_in_cooldown`
  is 300 s deliberately, so a mid-campaign lull does not drop tasks.
- **Service quota** → `aws service-quotas get-service-quota --service-code fargate
  --quota-code L-3032A538` (concurrent Fargate tasks).

---

## If the store is the cause

The dead-letter queue and idempotency ledger run on either SQLite (single process)
or DynamoDB (shared). `GET /metrics` reads both, so store failures surface as
errors on `/metrics` rather than silently.

- **`/metrics` returns 500 on DynamoDB** → almost always IAM. The task role is
  scoped to exactly two tables plus the one GSI; a `Query` on
  `status-seq-index` is authorized on the *index* ARN, not the table ARN.
- **`/dlq` looks stale right after a write** → expected. `list()`, `counts()` and
  `depth()` read a global secondary index, which cannot be strongly consistent.
  `GET /dlq/{event_id}` uses a strongly consistent `GetItem`. Correctness does not
  depend on the listing: `claim()` is a conditional write, so a replayer acting on
  a stale list still cannot double-process.
- **Duplicate processing across tasks** → check `APP_STORE_BACKEND`. If it is
  `sqlite` with more than one task, each task has its own private file. The
  Terraform `guard.tf` precondition refuses that combination; if it was bypassed,
  this is the cause.

---

## Escalation

*[Fill in before going live: who gets paged, and after how long unresolved.]*

Suggested: page the on-call owner if `final_call_failure_rate` stays above 0.05
for 10 minutes, or if `dlq.depth` is still growing 15 minutes after the downstream
dependency has recovered.

---

## Recovery and replay

Once the dependency is healthy again, drain the dead-letter queue:

```bash
# Replay everything pending (safe to run repeatedly, and in parallel)
curl -s -X POST $SVC/dlq/replay -H 'Content-Type: application/json' \
  -d '{"limit": 500, "concurrency": 4}' | python3 -m json.tool

# Or replay specific events
curl -s -X POST $SVC/dlq/replay -H 'Content-Type: application/json' \
  -d '{"event_ids": ["evt-1", "evt-2"]}'
```

Replay is safe by construction:

- Each record must be **claimed** first (a conditional write), so two concurrent
  replayers cannot both take the same record. A crashed replayer's claim expires
  after its lease and the record becomes claimable again.
- The **idempotency ledger** stops an event that already succeeded from being
  processed twice; it comes back as `duplicate` with the stored result and the
  record is resolved as `already_processed`.
- Replay **stops early** if the circuit breaker opens, rather than churning the
  queue against a dependency that is still down. Check `stopped_reason` in the
  response.

Confirm the queue drained:

```bash
curl -s $SVC/metrics | python3 -c "import sys,json; print(json.load(sys.stdin)['dlq'])"
```

---

## Post-incident

- Replay the DLQ once downstream recovers (above) and confirm `depth` returns to 0.
- Record actual vs expected latency and failure numbers for the next revision of
  this runbook.
- If the breaker did not trip when it should have, re-tune `APP_CB_*` against the
  observed call volume and add a regression test in `tests/unit/test_circuit_breaker.py`.
- If anything was lost, treat it as a compliance issue, not just a bug: a dropped
  or unlogged collections call is a regulatory gap, which is the premise of this
  challenge.
