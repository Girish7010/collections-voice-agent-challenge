"""HTTP adapter: maps the mock's HTTP responses onto PipelineResult / DependencyError."""

import httpx
import pytest

from app.dependency_adapter import (
    DependencyError,
    HttpPipelineAdapter,
    PipelineRequest,
    PipelineResult,
)

REQ = PipelineRequest(event_id="evt-1", audio_ref="synthetic")


def adapter_returning(handler) -> HttpPipelineAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock")
    return HttpPipelineAdapter(base_url="http://mock", timeout_s=1.0, client=client)


async def test_success_maps_to_pipeline_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={
            "status": "ok", "transcript": "synthetic transcript",
            "response_audio_ref": "synthetic", "latency_ms": 321,
        })

    result = await adapter_returning(handler).process(REQ)

    assert result == PipelineResult(transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=321)
    assert seen["path"] == "/pipeline/process"
    assert b'"event_id":"evt-1"' in seen["body"].replace(b" ", b"")


async def test_503_is_retryable_and_keeps_reason():
    adapter = adapter_returning(lambda r: httpx.Response(503, json={"status": "error", "reason": "downstream_timeout"}))

    with pytest.raises(DependencyError) as exc:
        await adapter.process(REQ)

    assert exc.value.retryable is True
    assert exc.value.reason == "downstream_timeout"
    assert exc.value.status_code == 503


@pytest.mark.parametrize("status", [429, 500, 502, 504])
async def test_other_transient_statuses_are_retryable(status):
    adapter = adapter_returning(lambda r: httpx.Response(status, text="boom"))

    with pytest.raises(DependencyError) as exc:
        await adapter.process(REQ)

    assert exc.value.retryable is True
    assert exc.value.reason == f"http_{status}"


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_client_errors_are_not_retryable(status):
    adapter = adapter_returning(lambda r: httpx.Response(status, json={"detail": "bad"}))

    with pytest.raises(DependencyError) as exc:
        await adapter.process(REQ)

    assert exc.value.retryable is False
    assert exc.value.reason == f"http_{status}"


async def test_http_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DependencyError) as exc:
        await adapter_returning(handler).process(REQ)

    assert exc.value.retryable is True
    assert exc.value.reason == "timeout"


async def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DependencyError) as exc:
        await adapter_returning(handler).process(REQ)

    assert exc.value.retryable is True
    assert exc.value.reason == "connection_error"


async def test_malformed_success_body_is_not_retryable():
    adapter = adapter_returning(lambda r: httpx.Response(200, json={"status": "weird"}))

    with pytest.raises(DependencyError) as exc:
        await adapter.process(REQ)

    assert exc.value.retryable is False
    assert exc.value.reason == "bad_response"
