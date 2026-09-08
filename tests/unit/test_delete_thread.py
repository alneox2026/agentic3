import asyncio

from common.schemas import ThreadDeleteRequestedEvent
from services.agent_persistence_worker_v3.app.services.delete_thread import DeleteThreadService


class FakeIdempotencyStore:
    def exists(self, client, event_id):
        return True

    def add_create_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("idempotency", event.event_id))


class FakeUnprocessedIdempotencyStore(FakeIdempotencyStore):
    def exists(self, client, event_id):
        return False


class FakeBatch:
    def __init__(self) -> None:
        self.actions = []

    def commit(self) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.batch_instance = FakeBatch()

    def batch(self) -> FakeBatch:
        return self.batch_instance


class FakeThreadsRepository:
    def add_delete_completed_to_batch(
        self,
        batch,
        client,
        *,
        thread_id,
        completed_at,
        runtime_session_status="not_applicable",
        error_code=None,
    ) -> None:
        batch.actions.append(
            ("delete_completed", thread_id, runtime_session_status, error_code)
        )


def _event() -> ThreadDeleteRequestedEvent:
    return ThreadDeleteRequestedEvent(
        event_id="evt-delete-managed",
        agent_id="antigravity_agent",
        agent_backend="gemini_managed",
        agent_region="us-central1",
        runtime_session_cleanup="none",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
    )


def test_duplicate_delete_event_returns_early() -> None:
    service = DeleteThreadService(
        idempotency_store=FakeIdempotencyStore(),
        firestore_client_factory=lambda: object(),
    )

    result = asyncio.run(service.delete_requested(_event()))

    assert result.event_id == "evt-delete-managed"
    assert result.runtime_session_status == "not_applicable"


def test_delete_thread_commits_idempotency_and_completion_batch() -> None:
    client = FakeClient()
    service = DeleteThreadService(
        idempotency_store=FakeUnprocessedIdempotencyStore(),
        threads_repository=FakeThreadsRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.delete_requested(_event()))

    assert result.event_id == "evt-delete-managed"
    assert result.runtime_session_status == "not_applicable"
    assert client.batch_instance.actions == [
        ("idempotency", "evt-delete-managed"),
        (
            "delete_completed",
            "thread-1",
            "not_applicable",
            "runtime_cleanup_not_applicable",
        ),
    ]
