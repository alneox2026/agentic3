import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from common.schemas import AgentConfig
from services.agent_gateway_v3.app.main import app
from services.agent_gateway_v3.app.api import routes_chat
from services.agent_gateway_v3.app.api import routes_stream
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.backend_types import UpstreamStreamEvent


client = TestClient(app)


class FakeAgentRuntimeClient:
    def __init__(
        self,
        upstream_events=None,
        delay_seconds=0.0,
        stream_error=None,
        fallback_reply_text=None,
        fallback_error=None,
    ):
        self._upstream_events = upstream_events or [
            UpstreamStreamEvent(
                event_name="message",
                payload={
                    "content": {
                        "role": "model",
                        "parts": [{"text": "echo:hello"}],
                    },
                    "usage_metadata": {"total_token_count": 12},
                },
            )
        ]
        self._delay_seconds = delay_seconds
        self._stream_error = stream_error
        self._fallback_reply_text = fallback_reply_text
        self._fallback_error = fallback_error
        self.chat_calls: list[dict[str, object]] = []
        self.buffered_query_calls: list[dict[str, object]] = []
        self.stream_calls: list[dict[str, object]] = []

    async def ensure_session(self, *, agent_config, user_id, request):
        return SimpleNamespace(
            session_id=request.session_id or "session-fake",
            thread_id=request.thread_id or "thread-fake",
        )

    async def chat(self, *, agent_config, user_id, session_id, message):
        self.chat_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        raise AssertionError("buffered route must use chat_buffered_query")

    async def chat_buffered_query(self, *, agent_config, user_id, session_id, message):
        self.buffered_query_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        if self._fallback_error is not None:
            raise self._fallback_error
        reply_text = self._fallback_reply_text or f"echo:{message}"
        return SimpleNamespace(
            reply_text=reply_text,
            raw_events=[
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": reply_text}],
                    },
                    "usage_metadata": {"total_token_count": 12},
                }
            ],
            usage={"total_token_count": 12, "estimated_cost_usd": 0.00003},
        )

    async def stream_chat_events(self, *, agent_config, user_id, session_id, message):
        self.stream_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        if self._stream_error is not None:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            raise self._stream_error

        for upstream_event in self._upstream_events:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            yield upstream_event

    def extract_text_fragments(self, event):
        parts = event.get("content", {}).get("parts", [])
        return [part["text"] for part in parts if isinstance(part.get("text"), str)]


class FakePublisher:
    async def publish_turn_completed(self, event):
        return SimpleNamespace(message_id="msg-fake")


class FakeWalletReservationService:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    async def reserve(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            event_metadata=lambda: {
                "reservation_id": kwargs["turn_id"],
                "billing_subject_id": kwargs["user_id"],
                "reserved_amount_nanos": 500_000_000,
                "currency": "USD",
            }
        )


async def _fake_authenticate_request(request) -> str:
    return "user-test"


async def _fake_get_agent_runtime_client() -> FakeAgentRuntimeClient:
    return FakeAgentRuntimeClient()


async def _fake_get_pubsub_publisher() -> FakePublisher:
    return FakePublisher()


def _fake_get_wallet_reservation_service(service):
    async def _factory():
        return service

    return _factory


def _fake_get_streaming_agent_config(agent_id: str) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        backend="gemini_managed",
        remote_agent_id="antigravity-preview-05-2026",
        region="us-central1",
        streaming_enabled=True,
    )


def _enable_streaming_route(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "get_agent_config", _fake_get_streaming_agent_config)


def _make_runtime_client(
    upstream_events=None,
    delay_seconds=0.0,
    stream_error=None,
    fallback_reply_text=None,
    fallback_error=None,
):
    async def _fake_runtime_client():
        return FakeAgentRuntimeClient(
            upstream_events=upstream_events,
            delay_seconds=delay_seconds,
            stream_error=stream_error,
            fallback_reply_text=fallback_reply_text,
            fallback_error=fallback_error,
        )

    return _fake_runtime_client


def test_buffered_chat_returns_structured_success(monkeypatch) -> None:
    runtime_client = FakeAgentRuntimeClient()

    async def _runtime_client():
        return runtime_client

    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(routes_chat, "get_chat_backend_client", lambda _agent_config: _runtime_client())
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/antigravity_agent/chat", json={"message": "hello"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["agent_id"] == "antigravity_agent"
    assert payload["thread_id"] == "thread-fake"
    assert payload["session_id"] == "session-fake"
    assert payload["reply_text"] == "echo:hello"
    assert payload["usage"] == {
        "total_token_count": 12,
        "estimated_cost_usd": 0.00003,
    }
    assert len(runtime_client.chat_calls) == 0
    assert len(runtime_client.stream_calls) == 0
    assert len(runtime_client.buffered_query_calls) == 1
    assert runtime_client.buffered_query_calls[0]["message"] == "hello"


def test_buffered_chat_rejects_unknown_agent(monkeypatch) -> None:
    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(
        routes_chat,
        "get_chat_backend_client",
        lambda _agent_config: _fake_get_agent_runtime_client(),
    )
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/unknown/chat", json={"message": "hello"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_agent"





def test_buffered_chat_propagates_the_server_created_billing_reservation(monkeypatch) -> None:
    runtime_client = FakeAgentRuntimeClient()
    published_events = []
    reservation_service = FakeWalletReservationService()

    async def _backend_client(_agent_config):
        return runtime_client

    class CapturingPublisher:
        async def publish_turn_completed(self, event):
            published_events.append(event)
            return SimpleNamespace(message_id="msg-billing")

    async def _publisher():
        return CapturingPublisher()

    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(routes_chat, "get_chat_backend_client", _backend_client)
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _publisher)
    monkeypatch.setattr(
        routes_chat,
        "get_wallet_reservation_service",
        _fake_get_wallet_reservation_service(reservation_service),
    )

    response = client.post("/v1/agents/antigravity_agent/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert len(reservation_service.calls) == 1
    assert len(published_events) == 1
    billing = published_events[0].metadata["billing"]
    assert billing["reservation_id"] == published_events[0].turn_id
    assert billing["billing_subject_id"] == "user-test"


def test_stream_chat_rejects_when_streaming_disabled(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    disabled_config = AgentConfig(
        agent_id="antigravity_agent",
        backend="gemini_managed",
        remote_agent_id="antigravity-preview-05-2026",
        region="us-central1",
        streaming_enabled=False,
    )
    monkeypatch.setattr(routes_stream, "get_agent_config", lambda _id: disabled_config)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "streaming_not_enabled"
    assert "/v1/agents/antigravity_agent/chat" in payload["error"]["message"]


def test_stream_chat_returns_normalized_sse_contract(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _fake_get_agent_runtime_client(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "event: metadata" in response.text
    assert 'data: {"text": "echo:hello"}' in response.text
    assert "event: done" in response.text
    assert '"reply_text": "echo:hello"' in response.text
    assert '"pubsub_message_id": "msg-fake"' in response.text





def test_stream_chat_emits_status_while_waiting_for_upstream(monkeypatch) -> None:
    upstream_events = [
        UpstreamStreamEvent(
            event_name="message",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "echo:hello"}],
                }
            },
        )
    ]

    monkeypatch.setattr(routes_stream, "STREAM_STATUS_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            upstream_events=upstream_events,
            delay_seconds=0.08,
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: status") >= 2
    assert response.text.index("event: metadata") < response.text.index("event: status")
    assert response.text.index("event: status") < response.text.index("event: token")
    assert '"phase": "waiting_for_runtime"' in response.text
    assert '"reply_text": "echo:hello"' in response.text


def test_stream_chat_emits_status_error_and_done_on_upstream_read_timeout(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    stream_error = ApiError(
        504,
        "agent_runtime_stream_read_timeout",
        "The gateway timed out while waiting for Agent Runtime to send stream data.",
        {
            "timeout_type": "read",
            "timeout_seconds": 240,
            "reason": "read timed out",
        },
    )

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=stream_error,
            fallback_error=RuntimeError("fallback unavailable"),
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(
            stream_debug=False,
            upstream_connect_timeout_seconds=10,
            upstream_read_timeout_seconds=240,
        ),
    )

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert "event: metadata" in response.text
    assert "event: status" in response.text
    assert "event: error" in response.text
    assert "event: done" in response.text
    assert '"code": "agent_runtime_stream_read_timeout"' in response.text
    assert '"ok": false' in response.text

    failure_log = next(
        call for call in log_calls if call["event"] == "gateway_stream_failed"
    )
    assert failure_log["code"] == "agent_runtime_stream_read_timeout"
    assert failure_log["timeout_type"] == "read"
    assert failure_log["upstream_read_timeout_seconds"] == 240
    assert failure_log["upstream_elapsed_ms"] is not None


def test_stream_chat_falls_back_to_buffered_before_any_token(monkeypatch) -> None:
    stream_error = ApiError(
        504,
        "agent_runtime_stream_read_timeout",
        "The gateway timed out while waiting for Agent Runtime to send stream data.",
        {"timeout_type": "read", "timeout_seconds": 240},
    )

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=stream_error,
            fallback_reply_text="fallback response",
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert 'data: {"text": "fallback response"}' in response.text
    assert '"fallback": true' in response.text
    assert '"fallback_from_code": "agent_runtime_stream_read_timeout"' in response.text


def test_stream_chat_does_not_fallback_after_token(monkeypatch) -> None:
    stream_error = ApiError(
        502,
        "agent_runtime_stream_unreachable",
        "The gateway could not open a streaming connection to Agent Runtime.",
    )

    class TokenThenErrorClient(FakeAgentRuntimeClient):
        async def stream_chat_events(self, *, agent_config, user_id, session_id, message):
            yield UpstreamStreamEvent(
                event_name="message",
                payload={
                    "content": {
                        "role": "model",
                        "parts": [{"text": "partial"}],
                    }
                },
            )
            raise stream_error

        async def chat_buffered_query(self, *, agent_config, user_id, session_id, message):
            raise AssertionError("fallback must not run after a token was emitted")

    async def _runtime_client():
        return TokenThenErrorClient()

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _runtime_client(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert 'data: {"text": "partial"}' in response.text
    assert "event: error" in response.text
    assert '"ok": false' in response.text


def test_stream_chat_logs_fragment_counters_for_multiple_text_events(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    upstream_events = [
        UpstreamStreamEvent(
            event_name="message_start",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "echo:"}],
                }
            },
        ),
        UpstreamStreamEvent(
            event_name="message_delta",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "hello"}],
                },
                "usage_metadata": {"total_token_count": 12},
            },
        ),
    ]

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 2
    assert '"reply_text": "echo:hello"' in response.text

    completion_log = next(
        call for call in log_calls if call["event"] == "gateway_stream_completed"
    )
    assert completion_log["upstream_sse_message_count"] == 2
    assert completion_log["upstream_text_event_count"] == 2
    assert completion_log["upstream_text_fragment_count"] == 2
    assert completion_log["normalized_token_event_count"] == 2
    assert completion_log["reply_text_char_count"] == len("echo:hello")
    assert completion_log["first_token_latency_ms"] is not None


def test_stream_chat_emits_only_new_suffix_for_cumulative_partials(monkeypatch) -> None:
    upstream_events = [
        UpstreamStreamEvent(
            event_name="message_start",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "echo:"}],
                }
            },
        ),
        UpstreamStreamEvent(
            event_name="message_delta",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "echo:hello"}],
                },
                "usage_metadata": {"total_token_count": 12},
            },
        ),
    ]

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 2
    assert 'data: {"text": "echo:"}' in response.text
    assert 'data: {"text": "hello"}' in response.text
    assert '"reply_text": "echo:hello"' in response.text


def test_stream_chat_debug_log_captures_upstream_shape_without_text(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    upstream_events = [
        UpstreamStreamEvent(
            event_name="metadata",
            payload={"type": "metadata", "sequence": 1},
        ),
        UpstreamStreamEvent(
            event_name="message",
            payload={
                "content": {
                    "role": "model",
                    "parts": [{"text": "echo:hello"}],
                },
                "usage_metadata": {"total_token_count": 12},
            },
        ),
    ]

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(stream_debug=True),
    )

    response = client.post("/v1/agents/antigravity_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 1

    debug_log = next(
        call for call in log_calls
        if call["event"] == "gateway_stream_upstream_diagnostics"
    )
    assert debug_log["outcome"] == "completed"
    assert debug_log["upstream_sse_message_count"] == 2
    assert debug_log["upstream_text_event_count"] == 1
    assert debug_log["upstream_text_fragment_count"] == 1
    assert debug_log["normalized_token_event_count"] == 1
    assert debug_log["upstream_event_names"] == ["metadata", "message"]
    assert debug_log["upstream_fragment_counts"] == [0, 1]
    assert debug_log["upstream_payload_keys"] == [
        ["sequence", "type"],
        ["content", "usage_metadata"],
    ]
    assert debug_log["upstream_fragment_lengths"] == [[], [len("echo:hello")]]
    assert "echo:hello" not in str(debug_log)
