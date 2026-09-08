import pytest

from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.thread_repository import ThreadRepository


class FakeSnapshot:
    def __init__(self, payload):
        self.exists = payload is not None
        self._payload = payload

    def to_dict(self):
        return self._payload


class FakeDocument:
    def __init__(self, payload):
        self._payload = payload

    def get(self):
        return FakeSnapshot(self._payload)


class FakeCollection:
    def __init__(self, payload):
        self._payload = payload

    def document(self, thread_id):
        return FakeDocument(self._payload)


class FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def collection(self, name):
        return FakeCollection(self._payload)


def test_assert_active_owned_thread_returns_thread_for_active_owner() -> None:
    repository = ThreadRepository()
    thread = repository.assert_active_owned_thread(
        FakeClient(
            {
                "uid": "user-1",
                "agent_id": "maxima",
                "status": "active",
                "session_id": "session-1",
            }
        ),
        thread_id="thread-1",
        user_id="user-1",
        agent_id="maxima",
    )

    assert thread["session_id"] == "session-1"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("archived", "thread_archived"),
        ("deleted", "thread_deleted"),
    ],
)
def test_assert_active_owned_thread_rejects_inactive_status(status, code) -> None:
    repository = ThreadRepository()

    with pytest.raises(ApiError) as exc_info:
        repository.assert_active_owned_thread(
            FakeClient(
                {
                    "uid": "user-1",
                    "agent_id": "maxima",
                    "status": status,
                    "session_id": "session-1",
                }
            ),
            thread_id="thread-1",
            user_id="user-1",
            agent_id="maxima",
        )

    assert exc_info.value.code == code
