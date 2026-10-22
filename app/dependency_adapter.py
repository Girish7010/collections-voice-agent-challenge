"""Model-agnostic boundary between the service and the STT -> LLM -> TTS pipeline.

Everything above this module (retries, circuit breaker, DLQ) only knows about
`PipelineAdapter`, `PipelineRequest`, `PipelineResult` and `DependencyError`.
Swapping the mock for real open-weight models means writing another class with
the same `process()` method; the resilience layer does not change.

Error contract for adapters:
    raise DependencyError(reason, retryable=True)   transient (timeouts, 503, overload)
    raise DependencyError(reason, retryable=False)  permanent (bad input, bad response)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class PipelineRequest:
    event_id: str
    audio_ref: str = "synthetic"


@dataclass(frozen=True)
class PipelineResult:
    transcript: str
    response_audio_ref: str
    latency_ms: int | None = None


class DependencyError(Exception):
    def __init__(self, reason: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code


class PipelineAdapter(Protocol):
    async def process(self, request: PipelineRequest) -> PipelineResult: ...


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpPipelineAdapter:
    """Calls the Appendix B mock (or any service with the same HTTP contract)."""

    def __init__(self, base_url: str, timeout_s: float, client: httpx.AsyncClient | None = None,
                 max_connections: int = 200):
        # httpx keeps only 20 idle connections by default. Under ~60 concurrent calls
        # that caused constant connect/teardown churn and saturated the CPU (measured
        # with cProfile), turning 0.3 s mock calls into 2 s+ timeouts. Keep-alive
        # capacity is therefore sized to the service's own concurrency limit.
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s, limits=limits)
        self._timeout_s = timeout_s

    async def process(self, request: PipelineRequest) -> PipelineResult:
        try:
            resp = await self._client.post(
                "/pipeline/process",
                json={"event_id": request.event_id, "audio_ref": request.audio_ref},
                timeout=self._timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise DependencyError("timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise DependencyError("connection_error", retryable=True) from exc

        if resp.status_code == 200:
            return self._parse_success(resp)
        raise DependencyError(
            self._error_reason(resp),
            retryable=resp.status_code in RETRYABLE_STATUS,
            status_code=resp.status_code,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_success(resp: httpx.Response) -> PipelineResult:
        try:
            body = resp.json()
            if body.get("status") != "ok":
                raise ValueError("status != ok")
            return PipelineResult(
                transcript=str(body["transcript"]),
                response_audio_ref=str(body["response_audio_ref"]),
                latency_ms=body.get("latency_ms"),
            )
        except (ValueError, KeyError, AttributeError) as exc:
            raise DependencyError("bad_response", retryable=False, status_code=200) from exc

    @staticmethod
    def _error_reason(resp: httpx.Response) -> str:
        if resp.status_code in RETRYABLE_STATUS:
            try:
                reason = resp.json().get("reason")
                if isinstance(reason, str) and reason:
                    return reason
            except (ValueError, AttributeError):
                pass
        return f"http_{resp.status_code}"
