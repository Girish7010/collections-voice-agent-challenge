"""Collections inference service HTTP API.

    POST /calls              process one call event (Appendix A schema)
                               200 processed | 200 duplicate | 202 dead_lettered | 409 in_progress
    GET  /health             liveness + circuit state
    GET  /metrics            counters, failure rates, latency percentiles, DLQ depth
    GET  /dlq                list dead letters (?status=pending&limit=50&offset=0)
    GET  /dlq/{event_id}     one dead letter
    POST /dlq/replay         replay pending dead letters ({"event_ids": [...]} optional)

Run:
    uvicorn app.api:app_from_env --factory --port 8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.dependency_adapter import PipelineAdapter
from app.event_processor import ProcessStatus, replay_dead_letters
from app.logging_config import configure_logging
from app.models import CallEvent
from app.wiring import build

STATUS_CODES = {
    ProcessStatus.PROCESSED: 200,
    ProcessStatus.DUPLICATE: 200,
    ProcessStatus.DEAD_LETTERED: 202,   # accepted and durably stored for replay
    ProcessStatus.IN_PROGRESS: 409,
}


class ReplayRequest(BaseModel):
    event_ids: list[str] | None = None
    limit: int = Field(default=100, ge=1, le=10_000)
    concurrency: int = Field(default=4, ge=1, le=50)


def create_app(settings: Settings | None = None, adapter: PipelineAdapter | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    c = build(settings, adapter)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await c.aclose()

    app = FastAPI(title="Collections voice-agent inference service", version="0.1.0", lifespan=lifespan)
    app.state.components = c

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "circuit_state": c.breaker.state.value}

    @app.get("/metrics")
    async def metrics() -> dict:
        depth, counts = await asyncio.gather(c.processor.db(c.dlq.depth), c.processor.db(c.dlq.counts))
        return c.metrics.snapshot(circuit_state=c.breaker.state.value, dlq={"depth": depth, "counts": counts})

    @app.post("/calls")
    async def process_call(event: CallEvent):
        outcome = await c.processor.process(event)
        body = {"status": outcome.status.value, "event_id": outcome.event_id, "attempts": outcome.attempts,
                "failure_reason": outcome.failure_reason, "result": outcome.result}
        return JSONResponse(status_code=STATUS_CODES[outcome.status], content=body)

    @app.get("/dlq")
    async def list_dlq(status: str | None = Query(default=None, pattern="^(pending|replaying|resolved)$"),
                       limit: int = Query(default=50, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        items = await c.processor.db(c.dlq.list, status, limit, offset)
        counts = await c.processor.db(c.dlq.counts)
        return {"counts": counts, "items": [i.to_dict() for i in items]}

    @app.get("/dlq/{event_id}")
    async def get_dlq(event_id: str):
        row = await c.processor.db(c.dlq.get, event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not in dead-letter queue")
        return row.to_dict()

    @app.post("/dlq/replay")
    async def replay(req: ReplayRequest | None = None):
        req = req or ReplayRequest()
        return await replay_dead_letters(c.processor, c.dlq, event_ids=req.event_ids,
                                         limit=req.limit, concurrency=req.concurrency)

    return app


def app_from_env() -> FastAPI:
    """Factory for uvicorn --factory: nothing is built or written at import time."""
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    return create_app(settings)
