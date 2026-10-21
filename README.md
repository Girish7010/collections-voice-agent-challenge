# Collections Voice-Agent Challenge

A resilient inference service for a collections voice agent (STT → LLM → TTS),
built for the Predixion AI × TalentX Open-Weight Collections Agent Challenge.

> Status: **work in progress.** Milestone 1 (mock dependency) is done. The resilience
> layer, dead-letter queue, traffic generator and cost write-up are next.

All data is synthetic. Licensed under [Apache 2.0](LICENSE).

## Prerequisites

- Python 3.11+ (developed on 3.14)
- No AWS account needed for the local track

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Mock STT/LLM/TTS dependency (Appendix B)

`mock_dependency/api.py` stands in for the real speech/LLM pipeline.

| | |
|---|---|
| Endpoint | `POST /pipeline/process` with `{"event_id": "...", "audio_ref": "synthetic"}` |
| Success (~80%) | `200 {"status":"ok","transcript":"synthetic transcript","response_audio_ref":"synthetic","latency_ms":N}` |
| Failure (~20%) | `503 {"status":"error","reason":"downstream_timeout"}` |
| Failure model | Independent per request: `random() < failure_rate` |
| Latency | Successful requests sleep `lognormvariate(mu, sigma)` ms before replying |
| Health | `GET /health` returns the active config |

Run it:

```bash
uvicorn mock_dependency.api:app --port 8001

# in another terminal
curl -s -X POST localhost:8001/pipeline/process \
  -H 'content-type: application/json' \
  -d '{"event_id":"demo-1","audio_ref":"synthetic"}'
```

### Configuration

All settings are environment variables (see [.env.example](.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `MOCK_FAILURE_RATE` | `0.20` | Probability of an injected 503 |
| `MOCK_LATENCY_MU` | `5.7` | Lognormal mu (log-ms) |
| `MOCK_LATENCY_SIGMA` | `0.60` | Lognormal sigma |
| `MOCK_FAILURE_LATENCY_MS` | `0` | Delay before returning a 503 |
| `MOCK_SEED` | unset | Seed for reproducible runs |

### Latency calibration: why sigma is 0.60, not 0.35

Appendix B suggests `random.lognormvariate(mu=5.7, sigma=0.35)` and asks entrants to
check the percentiles. The check shows that sigma=0.35 gives a distribution that is too
narrow. Output of `python scripts/check_mock_latency.py` (20,000 samples, seed 2026):

| Percentile | Target | sigma = 0.35 | sigma = 0.60 (default) |
|---|---|---|---|
| p50 | 300 ms | 299 ms (−0.3%) | 299 ms (−0.3%) |
| p90 | 700 ms | 468 ms (−33.1%) | 645 ms (−7.9%) |
| p99 | 1200 ms | 678 ms (−43.5%) | 1218 ms (+1.5%) |

No single lognormal with p50 = 300 hits both p90 = 700 and p99 = 1200 exactly. sigma=0.60
keeps the median exact, is within 2% at p99 and within 8% at p90. Since timeouts and
retries are driven by tail latency, I chose to match p99 closely. To reproduce the PDF's
suggested value, run `MOCK_LATENCY_SIGMA=0.35`.

### Testability

`create_app(config, rng, sleep)` accepts an injected `random.Random` and sleep function,
so tests can force success or failure (`failure_rate=0.0` / `1.0`), use seeds for
reproducible sequences, and skip real waiting.

## Tests

```bash
pytest                 # everything (~30 s)
pytest tests/unit      # fast, no network
pytest -m integration  # real uvicorn server over HTTP
```

Mock dependency coverage: response contract, 503 body, simulated sleep equal to reported
latency, ~20% failure rate over 2,000 requests, no failure bursts (independence),
seed reproducibility, input validation (422), env config, and the latency percentile
targets.

## Repository layout

```text
mock_dependency/   Appendix B mock (STT -> LLM -> TTS stand-in)
scripts/           Utility scripts (latency calibration)
tests/unit/        Fast, deterministic tests
tests/integration/ Tests against real local HTTP servers
```
