# Demo recording checklist (~5 minutes)

**No video has been recorded yet.** Recording on this machine cannot be fully
automated — see *Why* below. Everything else is prepared: `demo/run-demo.sh` drives
the entire demonstration against the real service, so recording is three actions:
start the recorder, run one command, narrate.

---

## Why `wf-recorder` fails here

```
$ wf-recorder -f out.mp4
compositor doesn't support wlr-screencopy-unstable-v1
```

This is not a path or working-directory problem. `wf-recorder` captures via
`wlr-screencopy-unstable-v1`, a **wlroots** protocol used by sway, Hyprland, Wayfire
and river. This machine runs **GNOME Shell 50.1 (Mutter)** on Ubuntu, which does not
implement that protocol and never will — GNOME captures through
`xdg-desktop-portal` + PipeWire instead. No configuration makes `wf-recorder` work
here; it is the wrong tool for this compositor.

The legacy `org.gnome.Shell.Screencast` D-Bus API, which used to allow scripted
recording, exports no interfaces in GNOME 50. And the ScreenCast portal deliberately
requires an interactive consent dialog before any application may capture the
screen. **That consent click is a security boundary — it is why an agent cannot
start the recording for you, and it should not be bypassed.**

---

## Recommended: GNOME's built-in recorder (nothing to install)

Verified present on this system (`show-screen-recording-ui = ['<Ctrl><Shift><Alt>R']`).

1. Press **Ctrl + Alt + Shift + R**. A red dot appears in the top bar.
2. Run the demo (below).
3. Press **Ctrl + Alt + Shift + R** again to stop.
4. The file lands in `~/Videos/Screencasts/` as `.webm`.

**Limitation: the built-in recorder captures no microphone audio.** For a narrated
demo you need one of the alternatives below.

## If you want narration

| Tool | Install | Notes |
|---|---|---|
| **Kooha** | `sudo apt install kooha` | Simplest GUI with a mic toggle. Uses the portal, so expect one consent click. |
| **OBS Studio** | `sudo apt install obs-studio` | Add a *Screen Capture (PipeWire)* source plus *Audio Input Capture*. More setup, far more control. |

### OBS setup (chosen option)

```bash
sudo apt install obs-studio ffmpeg
```

Then, first launch:

1. Skip the auto-configuration wizard, or choose **Optimize for recording**.
2. In **Sources**, click **+** → **Screen Capture (PipeWire)**. A GNOME dialog asks
   which screen to share — pick your monitor and click **Share**. That consent step
   is required by the portal and cannot be scripted away.
3. **+** → **Audio Input Capture** → your microphone. Speak and confirm the level
   meter moves, *before* you record anything.
4. **Settings → Output → Recording Format → MP4**, and set the recording path. If
   you record MP4 directly you can skip the conversion step entirely.
5. **Start Recording**, run the demo, **Stop Recording**.

A caution specific to OBS: if it crashes mid-recording, an MKV file is recoverable
but an MP4 is usually not. Recording to MKV and converting afterwards with
`demo/convert-to-mp4.sh` is the safer path.

## Converting to MP4

The task asks for `demo/collections-agent-demo.mp4`, but GNOME records `.webm`.
`ffmpeg` is **not installed** on this machine. Either:

```bash
sudo apt install ffmpeg
ffmpeg -i ~/Videos/Screencasts/Screencast*.webm -c:v libx264 -crf 23 -c:a aac \
       demo/collections-agent-demo.mp4
```

…or use the helper, which also verifies the result:

```bash
demo/convert-to-mp4.sh
```

It finds the newest recording in `~/Videos`, converts it, then reports duration,
resolution, whether an audio track is present, and the file size — so you are not
taking the output on trust. It warns if the result is under two minutes or would
exceed GitHub's 100MB limit.

---

## Running the demo

A single script drives all of it against the real service — real HTTP calls, real
retries, a real circuit breaker, a real DLQ. Nothing pre-recorded:

```bash
cd /home/neo/SRE/collections-voice-agent-challenge
demo/run-demo.sh
```

It prints a captioned banner for each section and paces itself for narration
(`PAUSE=0.5 demo/run-demo.sh` for a quick dry run first — do one, so you know the
rhythm before recording). It starts the mock and the service on ports 8201/8200,
uses a temporary database, and cleans up on exit. It touches no credentials and no
AWS.

**Before you hit record:** maximise the terminal, increase the font size until the
text is comfortably legible, and check nothing sensitive is on screen or in your
shell history.

The twelve sections the script walks through map to the shots below. Narrate over
it using these notes.

## 1. Project and architecture — 40s

*(The script covers sections 1-2 here.)*

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
