# Demo recording checklist (~5 minutes)

**No video has been recorded.** This environment has no screen-recording software
installed (`ffmpeg`, `wf-recorder`, `obs`, `asciinema` are all absent) and no way to
capture narration audio. This file is the shot-by-shot script to record it yourself.

Do not add a video link to the README until a file actually exists and plays.

---

## 0. Setup (before recording)

The session is **Wayland** (`XDG_SESSION_TYPE=wayland`), so install one of:

```bash
sudo apt install wf-recorder     # simplest, Wayland-native
# or
sudo apt install obs-studio      # if you want webcam + scene control
```

Record screen + microphone with `wf-recorder`:

```bash
wf-recorder -a -f demo/collections-agent-demo.mp4
# Ctrl-C to stop
```

Preparation:

```bash
cd collections-voice-agent-challenge
source .venv/bin/activate
rm -f data/collections.db          # start with an empty DLQ so the demo is clean
clear                              # and set the terminal font large enough to read
```

**Before you hit record, check the frame for:** AWS keys, `.env` contents,
`terraform.tfvars`, your account ID, and any personal paths you would rather not
publish. The repo is public under Apache 2.0.

---

## 1. Project and architecture — 40s

Show `README.md` top section and `docs/architecture.md`.

Say: a collections voice agent calls an STT → LLM → TTS pipeline that fails about
20% of the time. The job is to never silently lose a call, because a dropped
collections call is a compliance gap, not just a bad user experience.

Point at the four layers: retry with backoff and jitter → circuit breaker →
dead-letter queue → idempotency ledger.

## 2. Repository tour — 30s

```bash
git ls-files | head -40
```

Call out: `app/` (service), `mock_dependency/` (Appendix B), `load_tests/`
(Appendix C), `infra/terraform/` (AWS stack), `docs/`, `evidence/`.

## 3. Start it and show a success — 40s

Two terminals, or `docker compose up --build`:

```bash
uvicorn mock_dependency.api:app --port 8001
APP_DEPENDENCY_URL=http://localhost:8001 uvicorn app.api:app_from_env --factory --port 8000
```

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/calls -H 'content-type: application/json' -d '{
  "event_id": "demo-0001", "campaign_id": "CAMP-2026-10-A", "borrower_bucket": "0-30 DPD",
  "product_type": "bnpl", "timestamp": "2026-10-01T09:03:12Z", "outcome": "connected"}'
```

**Expect:** `{"status":"processed", ...}` with HTTP 200.

Send it a second time with the same `event_id` — **expect `"duplicate"`** and the
stored result returned, not a second pipeline run. That is the idempotency ledger.

## 4. Failure and retry — 40s

Force the mock to fail every request:

```bash
curl -s -X PUT localhost:8001/admin/failure-rate \
  -H 'content-type: application/json' -d '{"failure_rate": 1.0}'

curl -s -X POST localhost:8000/calls -H 'content-type: application/json' -d '{
  "event_id": "demo-0002", "campaign_id": "CAMP-2026-10-A", "borrower_bucket": "0-30 DPD",
  "product_type": "bnpl", "timestamp": "2026-10-01T09:04:00Z", "outcome": "connected"}'
```

**Expect:** HTTP 202, `"status":"dead_lettered"`, `"attempts":3`. Show the service
log lines for the retry attempts and the backoff gaps between them.

## 5. Circuit breaker opens and recovers — 45s

Keep firing while the dependency is down:

```bash
for i in $(seq 1 30); do
  curl -s -o /dev/null -X POST localhost:8000/calls -H 'content-type: application/json' \
    -d "{\"event_id\":\"cb-$i\",\"campaign_id\":\"CAMP-2026-10-A\",\"borrower_bucket\":\"0-30 DPD\",
         \"product_type\":\"bnpl\",\"timestamp\":\"2026-10-01T09:05:00Z\",\"outcome\":\"connected\"}"
done
curl -s localhost:8000/health
```

**Expect:** `"circuit_state":"open"`. Make the point that open is the *good* state —
we fail fast in milliseconds instead of hammering a dead dependency with retries.

Bring the dependency back and wait for `APP_CB_OPEN_S` (default 10s):

```bash
curl -s -X PUT localhost:8001/admin/failure-rate \
  -H 'content-type: application/json' -d '{"failure_rate": 0.2}'
sleep 11
curl -s -X POST localhost:8000/calls -H 'content-type: application/json' -d '{
  "event_id": "demo-0003", "campaign_id": "CAMP-2026-10-A", "borrower_bucket": "0-30 DPD",
  "product_type": "bnpl", "timestamp": "2026-10-01T09:06:00Z", "outcome": "connected"}'
curl -s localhost:8000/health
```

**Expect:** breaker goes `half_open`, the trial call succeeds, state returns to `closed`.

## 6. DLQ inspection and replay — 50s

```bash
curl -s localhost:8000/dlq | python3 -m json.tool | head -30
curl -s localhost:8000/dlq/demo-0002 | python3 -m json.tool
```

Show the stored record: `failure_reason`, `attempts`, `retry_count`, `status`,
timestamps, and the full original payload. Nothing was lost.

Replay:

```bash
curl -s -X POST localhost:8000/dlq/replay -H 'content-type: application/json' \
  -d '{"limit": 100, "concurrency": 4}' | python3 -m json.tool
curl -s localhost:8000/metrics | python3 -c "import sys,json; print(json.load(sys.stdin)['dlq'])"
```

**Expect:** `resolved` equals the number that were pending, and `dlq.depth` is `0`.

Mention replay safety: each record is claimed with a conditional write so two
replayers cannot take the same one, and the ledger blocks reprocessing anything
that already succeeded.

Same thing from the CLI, if you prefer:

```bash
python -m app.cli dlq stats
python -m app.cli dlq list --status pending
python -m app.cli dlq replay --limit 100
```

## 7. Tests — 30s

```bash
pytest -q
```

**Expect:** `221 passed`. Mention the parity suite runs every storage assertion
against both SQLite and DynamoDB, so the two backends cannot drift.

## 8. Load test and measured numbers — 50s

Either run it live (about 3 real minutes):

```bash
scripts/run_load_test.sh --start 07:30 --duration-min 180 --compression 60 \
    --rate-scale 10 --outage-start 09:00 --outage-min 10
```

…or, to stay inside five minutes, show the recorded evidence instead:

```bash
cat evidence/round1/load-tests/full-day-24h/report.json | python3 -m json.tool | head -40
```

Land these two numbers:

- Injected dependency failure rate **0.201** → final call failure rate **0.0079**,
  which is the theoretical 0.20³ for three bounded attempts. A 25× reduction.
- **Zero** calls lost across both runs; DLQ drained 29 → 0 and 105 → 0.

## 9. Cost, AWS and limitations — 40s

Show `docs/cost-analysis.md`: Config A ≈ $122/mo vs Config B ≈ $107/mo, of which
~$72 is fixed ALB + NAT + IPv4 overhead that does not scale with traffic.
Recommend B — lower floor, finer scale-out granularity, multi-AZ for the price of
A's single task.

Then be explicit about scope, reading from `README.md` → *Scope and limitations*:

- Round 1 is complete and reproducible locally with no AWS account.
- Round 2 is finalists-only; eligibility was not confirmed, so the GPU track was
  not attempted. **No open-weight model and no GPU were ever deployed.**
- The Terraform stack is real and was applied, verified healthy in `ap-south-1`,
  and then destroyed — evidence in `evidence/aws-deployment/`. It ran against a
  placeholder dependency URL, so the traffic profile was never replayed against AWS.
- Cost figures are Price List API estimates, not a measured bill.

If you show AWS at all, show it from `evidence/aws-deployment/` and **say on camera
that this is recorded evidence from a stack that has since been torn down**, not a
live deployment.

## 10. Close — 15s

Show the GitHub repository and `docs/runbook.md`. Point out the runbook is written
so a teammate could pick up an incident from it alone.

---

## After recording

```bash
ls -lh demo/collections-agent-demo.mp4     # confirm the file exists and has size
```

Play it back start to finish. Check: audio is audible, terminal text is legible at
the recorded resolution, and no secrets appear in any frame.

GitHub rejects files over 100 MB. If the recording is larger, upload it to Drive or
YouTube (unlisted) and put the link in the README — do not commit the file. Only add
the link once you have opened it in a private browser window and confirmed the
intended reviewer can actually access it.
