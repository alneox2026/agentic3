import asyncio
from types import SimpleNamespace

import pytest

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.chat_session_service import ChatSessionService


def _agent_config() -> AgentConfig:
    return AgentConfig(
        agent_id="maxima",
        resource_name="projects/test/locations/us-central1/reasoningEngines/123",
        region="us-central1",
    )


class FakeRuntimeClient:
    async def ensure_session(self, *, agent_config, user_id, request):
        return SimpleNamespace(session_id="new-session", thread_id="new-thread")


class FakeThreadRepository:
    def __init__(self, thread):
        self.thread = thread
        self.calls = []

    def assert_active_owned_thread(self, client, *, thread_id, user_id, agent_id):
        self.calls.append(
            {
                "thread_id": thread_id,
                "user_id": user_id,
                "agent_id": agent_id,
            }
        )
        return self.thread


def test_existing_thread_uses_backend_owned_session_id() -> None:
    repository = FakeThreadRepository({"session_id": "stored-session"})
    service = ChatSessionService(
        thread_repository=repository,
        firestore_client_factory=lambda: object(),
    )

    result = asyncio.run(
        service.resolve(
            runtime_client=FakeRuntimeClient(),
            agent_config=_agent_config(),
            user_id="user-1",
            request=ChatRequest(message="hello", thread_id="thread-1"),
        )
    )

    assert result.thread_id == "thread-1"
    assert result.session_id == "stored-session"
    assert result.created_new is False
    assert repository.calls == [
        {"thread_id": "thread-1", "user_id": "user-1", "agent_id": "maxima"}
    ]


def test_existing_thread_rejects_client_session_mismatch() -> None:
    service = ChatSessionService(
        thread_repository=FakeThreadRepository({"session_id": "stored-session"}),
        firestore_client_factory=lambda: object(),
    )

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            service.resolve(
                runtime_client=FakeRuntimeClient(),
                agent_config=_agent_config(),
                user_id="user-1",
                request=ChatRequest(
                    message="hello",
                    thread_id="thread-1",
                    session_id="wrong-session",
                ),
            )
        )

    assert exc_info.value.code == "session_id_mismatch"


def test_new_thread_rejects_orphan_client_session_id() -> None:
    service = ChatSessionService(
        thread_repository=FakeThreadRepository({"session_id": "stored-session"}),
        firestore_client_factory=lambda: object(),
    )

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            service.resolve(
                runtime_client=FakeRuntimeClient(),
                agent_config=_agent_config(),
                user_id="user-1",
                request=ChatRequest(message="hello", session_id="client-session"),
            )
        )

    assert exc_info.value.code == "session_id_without_thread_id"
