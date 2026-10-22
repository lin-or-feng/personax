"""Exercise disconnects against ASGI, not a buffering TestClient response.

The fake OpenAI-compatible transport blocks during connection/first token or
between tokens. Tests verify cancellation crosses API, orchestrator and SDK
boundaries, closes the upstream stream and makes its permit reusable.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest

import core.llm as llm
from api import create_app
from core.assistant import AssistantCheckpointStore, AssistantOrchestrator
from core.types import AssistantRequest


def _token(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class _Upstream:
    def __init__(self, transport, *, block_at: int | None = None, failure=None):
        self.transport = transport
        self.block_at = block_at
        self.failure = failure
        self.index = 0
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index == self.block_at:
            self.transport.waiting.set()
            await asyncio.Event().wait()
        if self.failure is not None:
            raise self.failure
        if self.index >= 2:
            raise StopAsyncIteration
        text = ("流式", "回答")[self.index]
        self.index += 1
        await asyncio.sleep(0)
        return _token(text)

    async def close(self):
        # An await is essential: unshielded cleanup fails under AnyIO cancel.
        await asyncio.sleep(0)
        self.close_calls += 1


class _Transport:
    def __init__(self, phase: str = "normal"):
        self.phase = phase
        self.calls = 0
        self.waiting = asyncio.Event()
        self.open_cancelled = False
        self.streams: list[_Upstream] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self.phase == "opening":
            self.waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.open_cancelled = True
        pause = {"first_token": 0, "between_tokens": 1}.get(self.phase) if self.calls == 1 else None
        stream = _Upstream(self, block_at=pause)
        self.streams.append(stream)
        return stream


@pytest.fixture
def install_transport(monkeypatch):
    monkeypatch.setenv("PERSONAX_API_REQUESTS_PER_MINUTE", "600")
    monkeypatch.delenv("PERSONAX_API_TOKEN", raising=False)
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "600")
    monkeypatch.setattr(llm, "_backend", lambda: "deepseek")
    monkeypatch.setattr(llm, "_OVERRIDES", {})
    monkeypatch.setattr(llm, "_ASYNC_SEMAPHORES", weakref.WeakKeyDictionary())
    monkeypatch.setattr(llm, "_SYNC_SEMAPHORES", {})
    monkeypatch.setattr(llm, "_REQUEST_LIMITERS", {})

    def install(transport):
        monkeypatch.setattr(llm, "_get_async_client", lambda: transport)
    return install


def _exchange(app, *, spec="2.4", fail_send=False):
    disconnected = asyncio.Event()
    sent = []
    received_body = False
    payload = json.dumps({
        "question": "你好", "thread_id": "disconnect-test", "use_knowledge": False,
    }).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/assistant/stream", "raw_path": b"/v1/assistant/stream",
        "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 9000), "server": ("test", 80),
    }

    async def receive():
        nonlocal received_body
        if not received_body:
            received_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if fail_send and message["type"] == "http.response.body" and message.get("body"):
            raise OSError("peer reset")
        sent.append(message)

    return asyncio.create_task(app(scope, receive, send)), disconnected, sent


def _body(sent):
    return b"".join(message.get("body", b"") for message in sent).decode()


async def _assert_permit_reusable():
    async def collect():
        return "".join([chunk async for chunk in llm.acomplete_stream("next", max_retries=1)])
    # A leaked concurrency=1 permit would make this subsequent request hang.
    assert await asyncio.wait_for(collect(), 1) == "流式回答"


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["opening", "first_token", "between_tokens"])
def test_http_disconnect_cancels_model_and_releases_permit(
    spec, phase, install_transport, tmp_path,
):
    async def run():
        transport = _Transport(phase)
        install_transport(transport)
        checkpoints = AssistantCheckpointStore(tmp_path / "turns.sqlite")
        service = AssistantOrchestrator(checkpoint_store=checkpoints)
        app = create_app(lambda: service)
        task, disconnected, sent = _exchange(app, spec=spec)
        await asyncio.wait_for(transport.waiting.wait(), 2)
        disconnected.set()
        await asyncio.wait_for(task, 2)
        assert transport.calls == 1  # cancellation must never become a retry
        if phase == "opening":
            assert transport.open_cancelled
        else:
            assert transport.streams[0].close_calls == 1
        assert "event: done" not in _body(sent)
        assert "event: error" not in _body(sent)
        assert _body(sent).count("event: token") == (1 if phase == "between_tokens" else 0)
        assert checkpoints.load("disconnect-test") == []
        assert any(row["event"] == "assistant_stream_cancelled" for row in service.harness.audit.entries)
        await _assert_permit_reusable()
    asyncio.run(run())


def test_server_task_cancellation_also_closes_upstream(install_transport):
    async def run():
        transport = _Transport("between_tokens")
        install_transport(transport)
        service = AssistantOrchestrator()
        task, _, sent = _exchange(create_app(lambda: service))
        await asyncio.wait_for(transport.waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.streams[0].close_calls == 1
        assert "event: error" not in _body(sent)
        assert "event: done" not in _body(sent)
        await _assert_permit_reusable()
    asyncio.run(run())


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
def test_send_failure_closes_generator_suspended_at_yield(spec, install_transport):
    async def run():
        transport = _Transport()
        install_transport(transport)
        task, _, sent = _exchange(create_app(AssistantOrchestrator), spec=spec, fail_send=True)
        await asyncio.wait_for(task, 2)
        assert transport.streams[0].close_calls == 1
        assert "event: done" not in _body(sent)
        await _assert_permit_reusable()
    asyncio.run(run())


def test_normal_async_sse_closes_upstream_and_persists_complete_turn(install_transport, tmp_path):
    async def run():
        transport = _Transport()
        install_transport(transport)
        checkpoints = AssistantCheckpointStore(tmp_path / "complete.sqlite")
        service = AssistantOrchestrator(checkpoint_store=checkpoints)
        task, _, sent = _exchange(create_app(lambda: service))
        await asyncio.wait_for(task, 2)
        assert transport.streams[0].close_calls == 1
        assert _body(sent).count("event: token") == 2
        assert _body(sent).count("event: done") == 1
        assert "event: error" not in _body(sent)
        assert checkpoints.load("disconnect-test")[-1].content == "流式回答"
    asyncio.run(run())


def test_retry_closes_failed_upstream_before_next_attempt(install_transport, monkeypatch):
    class RetryableError(Exception):
        pass

    async def run():
        transport = _Transport()
        original_create = transport.create

        async def create(**kwargs):
            stream = await original_create(**kwargs)
            if transport.calls == 1:
                stream.failure = RetryableError("temporary transport failure")
            return stream

        transport.create = create
        install_transport(transport)
        monkeypatch.setattr(llm, "_RETRYABLE", (RetryableError,))
        text = "".join([part async for part in llm.acomplete_stream(
            "retry", max_retries=2, base_delay=0,
        )])
        assert text == "流式回答" and transport.calls == 2
        assert [stream.close_calls for stream in transport.streams] == [1, 1]
    asyncio.run(run())


def test_sync_consumer_close_propagates_through_assistant_to_sdk(install_transport, monkeypatch):
    streams = []

    class SyncUpstream:
        closed = False

        def __iter__(self):
            yield _token("流式")
            yield _token("回答")

        def close(self):
            self.closed = True

    def create(**kwargs):
        stream = SyncUpstream()
        streams.append(stream)
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    service = AssistantOrchestrator()
    stream = service.reply_stream(AssistantRequest(question="你好", use_knowledge=False))
    iterator = iter(stream)
    assert next(iterator) == "流式"
    iterator.close()
    assert streams[0].closed and stream.response is None
    assert "".join(llm.complete_stream("next")) == "流式回答"
    assert streams[1].closed


@contextmanager
def _serve_local(app):
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="error", access_log=False, lifespan="on", ws="none",
    ))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("local test server failed to start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive(), "local test server did not stop"


def test_real_http_client_close_reaches_openai_stream_and_frees_capacity(install_transport, monkeypatch):
    """Real TCP on both sides; OpenAI SDK talks only to a local fake provider."""
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from openai import AsyncOpenAI

    provider = FastAPI()
    upstream_closed = threading.Event()
    upstream_waiting = threading.Event()
    requests = 0

    @provider.post("/v1/chat/completions")
    async def completion():
        nonlocal requests
        requests += 1
        number = requests

        async def tokens():
            try:
                yield "data: " + json.dumps({
                    "id": "local-test", "object": "chat.completion.chunk", "created": 0,
                    "model": "local-test", "choices": [{"index": 0,
                    "delta": {"content": "真实HTTP测试"}, "finish_reason": None}],
                }) + "\n\n"
                if number == 1:
                    upstream_waiting.set()
                    await asyncio.Event().wait()
                yield "data: [DONE]\n\n"
            finally:
                if number == 1:
                    upstream_closed.set()
        return StreamingResponse(tokens(), media_type="text/event-stream")

    with _serve_local(provider) as upstream_url:
        sdk = AsyncOpenAI(api_key="local-test-only", base_url=upstream_url + "/v1", max_retries=0)
        monkeypatch.setattr(llm, "_get_async_client", lambda: sdk)
        app = create_app(AssistantOrchestrator)

        @asynccontextmanager
        async def lifespan(_app):
            yield
            # Close the shared client on the same server loop that used it.
            await sdk.close()

        app.router.lifespan_context = lifespan
        with _serve_local(app) as api_url:
            async def run():
                async with httpx.AsyncClient(timeout=3) as client:
                    async with client.stream("POST", api_url + "/v1/assistant/stream", json={
                        "question": "你好", "use_knowledge": False,
                    }) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                assert "真实HTTP测试" in line
                                break
                        assert await asyncio.to_thread(upstream_waiting.wait, 2)
                    # Closing the downstream socket must close the SDK's
                    # upstream socket, not just stop local token rendering.
                    assert await asyncio.to_thread(upstream_closed.wait, 2)
                    followup = await client.post(api_url + "/v1/assistant/stream", json={
                        "question": "你好", "use_knowledge": False,
                    })
                    assert followup.status_code == 200
                    assert "event: done" in followup.text
                    assert requests == 2
            asyncio.run(run())
