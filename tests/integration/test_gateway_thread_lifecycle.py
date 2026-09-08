from fastapi.testclient import TestClient

from common.schemas import ThreadLifecycleResponse
from services.agent_gateway_v3.app.api import routes_threads
from services.agent_gateway_v3.app.main import app


client = TestClient(app)


class FakeThreadLifecycleService:
    async def archive_thread(
        self,
        *,
        request_context,
        agent_config,
        thread_id,
        user_id,
        payload,
    ):
        return ThreadLifecycleResponse(
            ok=True,
            agent_id=agent_config.agent_id,
            thread_id=thread_id,
            status="archived",
            runtime_session_status="active",
        )

    async def delete_thread(
        self,
        *,
        request_context,
        agent_config,
        thread_id,
        user_id,
        payload,
    ):
        return ThreadLifecycleResponse(
            ok=True,
            agent_id=agent_config.agent_id,
            thread_id=thread_id,
            status="deleted",
            runtime_session_status="delete_pending",
        )


async def _fake_authenticate_request(request) -> str:
    return "user-test"


async def _fake_get_thread_lifecycle_service() -> FakeThreadLifecycleService:
    return FakeThreadLifecycleService()


def test_archive_thread_returns_structured_success(monkeypatch) -> None:
    monkeypatch.setattr(routes_threads, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(
        routes_threads,
        "get_thread_lifecycle_service",
        _fake_get_thread_lifecycle_service,
    )

    response = client.post(
        "/v1/agents/antigravity_agent/threads/thread-fake/archive",
        json={"reason": "user_action"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["thread_id"] == "thread-fake"
    assert payload["status"] == "archived"


def test_delete_thread_returns_structured_success(monkeypatch) -> None:
    monkeypatch.setattr(routes_threads, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(
        routes_threads,
        "get_thread_lifecycle_service",
        _fake_get_thread_lifecycle_service,
    )

    response = client.post(
        "/v1/agents/antigravity_agent/threads/thread-fake/delete",
        json={"reason": "user_action"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["thread_id"] == "thread-fake"
    assert payload["status"] == "deleted"
    assert payload["runtime_session_status"] == "delete_pending"
