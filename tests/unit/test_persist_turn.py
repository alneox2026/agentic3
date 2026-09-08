import asyncio

from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.services.persist_turn import (
    PersistTurnService,
)


class Conflict(Exception):
    pass


class FakeBatch:
    def __init__(self, *, raise_conflict: bool = False) -> None:
        self.raise_conflict = raise_conflict
        self.actions = []

    def commit(self) -> None:
        if self.raise_conflict:
            raise Conflict("duplicate")


class FakeClient:
    def __init__(self, *, raise_conflict: bool = False) -> None:
        self.raise_conflict = raise_conflict
        self.batch_instance = FakeBatch(raise_conflict=raise_conflict)

    def batch(self) -> FakeBatch:
        return self.batch_instance


class FakeIdempotencyStore:
    def add_create_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("idempotency", event.event_id))


class FakeBillingLedgerRepository:
    def add_create_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("billing_ledger", event.turn_id))


class FakeThreadsRepository:
    def __init__(self, existing_thread=None, ignored_reason=None) -> None:
        self.existing_thread = existing_thread
        self.ignored_reason = ignored_reason

    def load_existing(self, client, thread_id):
        return self.existing_thread

    def validate_existing_for_turn(self, existing_thread, event):
        return self.ignored_reason

    def add_upsert_to_batch(self, batch, client, event, *, existing_thread=None) -> None:
        batch.actions.append(("thread", event.thread_id))


class FakeMessagesRepository:
    def add_turn_messages_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("messages", event.turn_id))


def _build_event() -> TurnCompletedEvent:
    return TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )


def test_persist_turn_commits_batch_once() -> None:
    client = FakeClient()
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is True
    assert client.batch_instance.actions == [
        ("idempotency", "evt-1"),
        ("billing_ledger", "turn-1"),
        ("thread", "thread-1"),
        ("messages", "turn-1"),
    ]


def test_persist_turn_treats_conflict_as_duplicate() -> None:
    client = FakeClient(raise_conflict=True)
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is False


def test_persist_turn_records_idempotency_only_for_deleted_thread() -> None:
    client = FakeClient()
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(ignored_reason="thread_deleted"),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is False
    assert result.ignored_reason == "thread_deleted"
    assert client.batch_instance.actions == [("idempotency", "evt-1")]
