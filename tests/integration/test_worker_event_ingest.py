import base64
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from services.agent_persistence_worker_v3.app.main import app
from services.agent_persistence_worker_v3.app.api import routes_events


client = TestClient(app)


async def _fake_persist(event):
    return SimpleNamespace(
        event_id=event.event_id,
        thread_id=event.thread_id,
        persisted=True,
        ignored_reason=None,
    )


async def _fake_delete_requested(event):
    return SimpleNamespace(
        event_id=event.event_id,
        thread_id=event.thread_id,
        runtime_session_status="deleted",
    )


def test_worker_accepts_valid_pubsub_event(monkeypatch) -> None:
    monkeypatch.setattr(routes_events.PERSIST_SERVICE, "persist", _fake_persist)
    payload = {
        "event_type": "agent.turn.completed",
        "event_id": "evt-test",
        "turn_id": "turn-test",
        "agent_id": "maxima",
        "user_id": "user-123",
        "thread_id": "thread-test",
        "session_id": "session-test",
        "user_message": "hello",
        "assistant_message": "hi there",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    envelope = {
        "message": {
            "data": encoded,
            "messageId": "msg-1",
        },
        "subscription": "projects/ceo-dev123/subscriptions/test-sub",
    }

    response = client.post("/events/pubsub", json=envelope)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_worker_accepts_valid_thread_delete_event(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_events.DELETE_THREAD_SERVICE,
        "delete_requested",
        _fake_delete_requested,
    )
    payload = {
        "event_type": "agent.thread.delete_requested",
        "event_id": "evt-delete",
        "agent_id": "maxima",
        "agent_region": "us-central1",
        "agent_resource_name": "projects/ceo-dev123/locations/us-central1/reasoningEngines/123",
        "user_id": "user-123",
        "thread_id": "thread-test",
        "session_id": "session-test",
        "reason": "user_action",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    envelope = {
        "message": {
            "data": encoded,
            "messageId": "msg-2",
        },
        "subscription": "projects/ceo-dev123/subscriptions/test-sub",
    }

    response = client.post("/events/pubsub", json=envelope)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["runtime_session_status"] == "deleted"


def test_worker_rejects_invalid_pubsub_event() -> None:
    response = client.post("/events/pubsub", json={"message": {"data": "", "messageId": "x"}, "subscription": "sub"})
    assert response.status_code >= 400


def test_worker_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "unknown.event",
        "event_id": "evt-unknown",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    envelope = {
        "message": {
            "data": encoded,
            "messageId": "msg-3",
        },
        "subscription": "projects/ceo-dev123/subscriptions/test-sub",
    }

    response = client.post("/events/pubsub", json=envelope)
    assert response.status_code == 400
