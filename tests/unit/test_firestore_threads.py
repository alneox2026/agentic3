from datetime import datetime, timezone

from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.services.firestore_threads import (
    THREAD_PREVIEW_MAX_CHARS,
    FirestoreThreadsRepository,
)


class FakeBatch:
    def __init__(self) -> None:
        self.payloads = []

    def set(self, document, payload, merge):
        self.payloads.append(payload)


class FakeMessages:
    def document(self, thread_id):
        return object()


class FakeClient:
    def collection(self, name):
        return FakeMessages()


def test_thread_summary_stores_bounded_message_previews() -> None:
    repository = FirestoreThreadsRepository()
    batch = FakeBatch()
    long_text = "word " * 200
    event = TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message=long_text,
        assistant_message=long_text,
        created_at=datetime.now(timezone.utc),
    )

    repository.add_upsert_to_batch(batch, FakeClient(), event)

    payload = batch.payloads[0]
    assert len(payload["last_user_message"]) == THREAD_PREVIEW_MAX_CHARS
    assert len(payload["last_assistant_message"]) == THREAD_PREVIEW_MAX_CHARS
    assert payload["last_user_message"].endswith("...")
