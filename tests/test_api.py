"""PersonaX 2.8 FastAPI / SSE 边界回归。"""
from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from api import API_VERSION, create_app
from core.assistant import AssistantOrchestrator
from core.types import AssistantRequest, AssistantResponse


def _response(request: AssistantRequest) -> AssistantResponse:
    return AssistantResponse(
        answer="这是回答。",
        route="direct",
        trace_id=request.trace_id,
        checkpoint_id=f"{request.thread_id}:1",
    )


class _FakeStream:
    def __init__(self, request: AssistantRequest, *, fail: bool = False):
        self.request = request
        self.fail = fail
        self.response = None

    def __iter__(self):
        yield "这是"
        if self.fail:
            raise RuntimeError("不应泄漏的内部详情")
        yield "回答。"
        self.response = _response(self.request)


class _FakeService:
    def __init__(self, *, fail_reply: bool = False, fail_stream: bool = False):
        self.requests: list[AssistantRequest] = []
        self.fail_reply = fail_reply
        self.fail_stream = fail_stream

    def reply(self, request: AssistantRequest) -> AssistantResponse:
        self.requests.append(request)
        if self.fail_reply:
            raise RuntimeError("不应泄漏的内部详情")
        return _response(request)

    def reply_stream(self, request: AssistantRequest) -> _FakeStream:
        self.requests.append(request)
        return _FakeStream(request, fail=self.fail_stream)


def test_health_is_public_and_does_not_initialize_model(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    calls = []
    app = create_app(lambda: calls.append(True) or _FakeService())

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "version": API_VERSION,
            "service": "personax-api",
            "publish_api_exposed": False,
            "api_token_required": False,
        }
        assert calls == []
        assert client.get("/readyz").status_code == 200
        assert calls == [True]


def test_json_reply_uses_server_trace_and_forbids_unknown_fields(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    service = _FakeService()
    with TestClient(create_app(lambda: service)) as client:
        result = client.post("/v1/assistant/reply", json={
            "question": "你好",
            "thread_id": "api-test-1",
            "use_knowledge": False,
        })
        assert result.status_code == 200
        body = result.json()
        assert body["answer"] == "这是回答。"
        assert body["trace_id"].startswith("trace-")
        assert result.headers["x-trace-id"] == body["trace_id"]
        assert service.requests[0].thread_id == "api-test-1"

        rejected = client.post("/v1/assistant/reply", json={
            "question": "你好", "trace_id": "client-spoofed",
        })
        assert rejected.status_code == 422


def test_api_bearer_token_is_optional_locally_and_enforced_when_set(monkeypatch):
    monkeypatch.setenv("PERSONAX_API_TOKEN", "local-test-token")
    with TestClient(create_app(lambda: _FakeService())) as client:
        missing = client.post("/v1/assistant/reply", json={"question": "你好"})
        assert missing.status_code == 401
        invalid = client.post(
            "/v1/assistant/reply",
            headers={"Authorization": "Bearer wrong"},
            json={"question": "你好"},
        )
        assert invalid.status_code == 401
        valid = client.post(
            "/v1/assistant/reply",
            headers={"Authorization": "Bearer local-test-token"},
            json={"question": "你好"},
        )
        assert valid.status_code == 200


def test_api_rate_limit_is_global_and_returns_retry_after(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    monkeypatch.setenv("PERSONAX_API_REQUESTS_PER_MINUTE", "1")
    with TestClient(create_app(lambda: _FakeService())) as client:
        first = client.post("/v1/assistant/reply", json={
            "question": "你好", "thread_id": "thread-a",
        })
        second = client.post("/v1/assistant/reply", json={
            "question": "你好", "thread_id": "thread-b",
        })
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["detail"]["code"] == "api_rate_limited"
        assert int(second.headers["retry-after"]) >= 1


def test_request_limits_history_memory_and_thread_id(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    monkeypatch.setenv("PERSONAX_API_REQUESTS_PER_MINUTE", "60")
    with TestClient(create_app(lambda: _FakeService())) as client:
        too_much_history = [
            {"role": "user", "content": f"message-{index}"}
            for index in range(21)
        ]
        assert client.post("/v1/assistant/reply", json={
            "question": "继续", "history": too_much_history,
        }).status_code == 422
        assert client.post("/v1/assistant/reply", json={
            "question": "继续", "memories": ["x" * 501],
        }).status_code == 422
        assert client.post("/v1/assistant/reply", json={
            "question": "继续", "thread_id": "../unsafe path",
        }).status_code == 422


def test_sse_stream_has_token_done_events_and_no_proxy_buffering(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    with TestClient(create_app(lambda: _FakeService())) as client:
        with client.stream("POST", "/v1/assistant/stream", json={
            "question": "你好", "thread_id": "stream-1", "use_knowledge": False,
        }) as result:
            body = "".join(result.iter_text())
            assert result.status_code == 200
            assert result.headers["content-type"].startswith("text/event-stream")
            assert result.headers["cache-control"] == "no-cache, no-transform"
            assert result.headers["x-accel-buffering"] == "no"
            assert result.headers["x-trace-id"].startswith("trace-")
        assert body.count("event: token") == 2
        assert "event: done" in body
        done_data = body.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0]
        assert json.loads(done_data)["answer"] == "这是回答。"


def test_api_errors_are_traceable_without_leaking_internal_messages(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    with TestClient(create_app(lambda: _FakeService(fail_reply=True))) as client:
        result = client.post("/v1/assistant/reply", json={"question": "你好"})
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "assistant_unavailable"
        assert result.headers["x-trace-id"].startswith("trace-")
        assert "不应泄漏" not in result.text

    with TestClient(create_app(lambda: _FakeService(fail_stream=True))) as client:
        body = client.post("/v1/assistant/stream", json={"question": "你好"}).text
        assert "event: error" in body
        assert "assistant_stream_failed" in body
        assert "不应泄漏" not in body


def test_openapi_exposes_no_publish_route(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    paths = create_app(lambda: _FakeService()).openapi()["paths"]
    assert set(paths) == {
        "/healthz", "/readyz", "/v1/assistant/reply", "/v1/assistant/stream",
    }
    assert not any("publish" in path for path in paths)


def test_root_and_docs_redirect_to_simplified_chinese(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    with TestClient(create_app(lambda: _FakeService())) as client:
        root = client.get("/", follow_redirects=False)
        docs = client.get("/docs", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/docs/zh-CN"
        assert docs.status_code == 307
        assert docs.headers["location"] == "/docs/zh-CN"
        assert client.get("/favicon.ico").status_code == 204


def test_localized_docs_and_openapi_switch_language(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    with TestClient(create_app(lambda: _FakeService())) as client:
        zh_page = client.get("/docs/zh-CN")
        en_page = client.get("/docs/en")
        assert zh_page.status_code == 200
        assert zh_page.headers["content-language"] == "zh-CN"
        assert "简体中文" in zh_page.text
        assert "English" in zh_page.text
        assert "/openapi/zh-CN.json" in zh_page.text
        assert en_page.status_code == 200
        assert en_page.headers["content-language"] == "en"
        assert "/openapi/en.json" in en_page.text

        zh_schema = client.get("/openapi/zh-CN.json").json()
        en_schema = client.get("/openapi/en.json").json()
        assert zh_schema["info"]["title"] == "PersonaX 助手 API"
        assert en_schema["info"]["title"] == "PersonaX Assistant API"
        assert zh_schema["paths"]["/v1/assistant/reply"]["post"]["summary"] == "获取完整回答"
        assert en_schema["paths"]["/v1/assistant/reply"]["post"]["summary"] == "Get a complete reply"
        zh_request = zh_schema["components"]["schemas"]["AssistantAPIRequest"]
        en_request = en_schema["components"]["schemas"]["AssistantAPIRequest"]
        assert zh_request["properties"]["question"]["description"] == "用户本轮问题。"
        assert en_request["properties"]["question"]["description"] == "The current user question."
        assert zh_request["examples"][0]["question"].startswith("请介绍")
        assert not any("publish" in path for path in zh_schema["paths"])


def test_unsupported_docs_language_returns_404(monkeypatch):
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    with TestClient(create_app(lambda: _FakeService())) as client:
        assert client.get("/docs/fr").status_code == 404
        assert client.get("/openapi/fr.json").status_code == 404


def test_rag_lazy_initialization_is_singleton_under_threads(monkeypatch):
    marker = object()
    calls = 0
    calls_lock = threading.Lock()

    def fake_build(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return marker

    monkeypatch.setattr("core.assistant.build_rag_from_dir", fake_build)
    service = AssistantOrchestrator()
    results = []
    threads = [threading.Thread(target=lambda: results.append(service._rag())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert results == [marker] * 8
