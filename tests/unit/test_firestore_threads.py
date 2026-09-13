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
    assert payload["title"].startswith("word")


def test_thread_upsert_populates_title_when_existing_thread_lacks_title() -> None:
    repository = FirestoreThreadsRepository()
    batch = FakeBatch()
    event = TurnCompletedEvent(
        event_id="evt-2",
        turn_id="turn-2",
        agent_id="constitution_expert",
        user_id="user-1",
        thread_id="thread-2",
        session_id="session-2",
        user_message="When was the constitution adopted?",
        assistant_message="The constitution was adopted...",
        created_at=datetime.now(timezone.utc),
    )

    # Simulate thread created earlier by gateway saving interaction state
    existing_thread_without_title = {
        "last_interaction_id": "int-1",
        "environment_id": "env-1",
    }
    repository.add_upsert_to_batch(
        batch,
        FakeClient(),
        event,
        existing_thread=existing_thread_without_title,
    )

    payload = batch.payloads[0]
    assert payload["title"] == "When was the constitution adopted?"
    assert payload["created_at"] == event.created_at


def test_thread_upsert_preserves_existing_title() -> None:
    repository = FirestoreThreadsRepository()
    batch = FakeBatch()
    event = TurnCompletedEvent(
        event_id="evt-3",
        turn_id="turn-3",
        agent_id="constitution_expert",
        user_id="user-1",
        thread_id="thread-3",
        session_id="session-3",
        user_message="Second message in conversation",
        assistant_message="Second response",
        created_at=datetime.now(timezone.utc),
    )

    existing_thread_with_title = {
        "title": "First Question Title",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    repository.add_upsert_to_batch(
        batch,
        FakeClient(),
        event,
        existing_thread=existing_thread_with_title,
    )

    payload = batch.payloads[0]
    assert "title" not in payload
    assert "created_at" not in payload

