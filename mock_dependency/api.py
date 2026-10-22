"""Mock STT -> LLM -> TTS dependency (challenge Appendix B).

Contract:
    POST /pipeline/process  {"event_id": "...", "audio_ref": "synthetic"}
    200 -> {"status": "ok", "transcript": "synthetic transcript",
            "response_audio_ref": "synthetic", "latency_ms": <sampled>}
    503 -> {"status": "error", "reason": "downstream_timeout"}   (~20% of requests)

Each request fails independently with probability `failure_rate`. Successful
requests sleep for a lognormally distributed latency before answering.

Latency note: Appendix B suggests lognormvariate(mu=5.7, sigma=0.35), but that
gives p90 ~ 470 ms and p99 ~ 675 ms, short of the stated p90 ~ 700 / p99 ~ 1200 ms
targets. sigma=0.60 gives p50 ~ 299, p90 ~ 645, p99 ~ 1207 ms, so it is the
default here. Both values are configurable via environment variables.

Test hook (not part of Appendix B): PUT /admin/failure-rate {"failure_rate": 1.0}
changes the failure rate at runtime, so a load test can simulate an outage.

Run:
    uvicorn mock_dependency.api:app --port 8001
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("mock_dependency")

SleepFn = Callable[[float], Awaitable[None]]

FAILURE_BODY = {"status": "error", "reason": "downstream_timeout"}


@dataclass(frozen=True)
class MockConfig:
    failure_rate: float = 0.20
    latency_mu: float = 5.7
    latency_sigma: float = 0.60
    failure_latency_ms: int = 0
    seed: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.failure_rate <= 1.0:
            raise ValueError(f"failure_rate must be in [0, 1], got {self.failure_rate}")
        if self.latency_sigma < 0:
            raise ValueError(f"latency_sigma must be >= 0, got {self.latency_sigma}")
        if self.failure_latency_ms < 0:
            raise ValueError(f"failure_latency_ms must be >= 0, got {self.failure_latency_ms}")

    @classmethod
    def from_env(cls) -> MockConfig:
        seed = os.getenv("MOCK_SEED")
        return cls(
            failure_rate=float(os.getenv("MOCK_FAILURE_RATE", cls.failure_rate)),
            latency_mu=float(os.getenv("MOCK_LATENCY_MU", cls.latency_mu)),
            latency_sigma=float(os.getenv("MOCK_LATENCY_SIGMA", cls.latency_sigma)),
            failure_latency_ms=int(os.getenv("MOCK_FAILURE_LATENCY_MS", cls.failure_latency_ms)),
            seed=int(seed) if seed else None,
        )


class FailureRateUpdate(BaseModel):
    failure_rate: float = Field(ge=0.0, le=1.0, strict=True)


class ProcessRequest(BaseModel):
    event_id: str = Field(min_length=1, strict=True)
    audio_ref: str = "synthetic"


def sample_latency_ms(rng: random.Random, mu: float, sigma: float) -> int:
    """Sample one successful-call latency in whole milliseconds (minimum 1)."""
    return max(1, round(rng.lognormvariate(mu, sigma)))


def create_app(
    config: MockConfig | None = None,
    rng: random.Random | None = None,
    sleep: SleepFn = asyncio.sleep,
) -> FastAPI:
    initial = config or MockConfig()
    rng = rng or random.Random(initial.seed)
    current = {"config": initial}  # replaced (not mutated) by the admin endpoint

    app = FastAPI(title="Mock STT/LLM/TTS dependency", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", **asdict(current["config"])}

    @app.put("/admin/failure-rate")
    async def set_failure_rate(update: FailureRateUpdate) -> dict:
        """Test hook: e.g. 1.0 simulates a full outage, then restore 0.2."""
        current["config"] = replace(current["config"], failure_rate=update.failure_rate)
        logger.warning("failure_rate set to %s", update.failure_rate)
        return {"failure_rate": update.failure_rate}

    @app.post("/pipeline/process")
    async def process(req: ProcessRequest):
        config = current["config"]
        # Decide outcome and latency up front, before awaiting, so a seeded RNG
        # yields a reproducible sequence regardless of request interleaving.
        failed = rng.random() < config.failure_rate
        if failed:
            if config.failure_latency_ms:
                await sleep(config.failure_latency_ms / 1000)
            logger.info("event_id=%s outcome=injected_failure", req.event_id)
            return JSONResponse(status_code=503, content=FAILURE_BODY)

        latency_ms = sample_latency_ms(rng, config.latency_mu, config.latency_sigma)
        await sleep(latency_ms / 1000)
        logger.info("event_id=%s outcome=ok latency_ms=%d", req.event_id, latency_ms)
        return {
            "status": "ok",
            "transcript": "synthetic transcript",
            "response_audio_ref": "synthetic",
            "latency_ms": latency_ms,
        }

    return app


app = create_app(MockConfig.from_env())
