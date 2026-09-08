"""Thread delete lifecycle handling for Pub/Sub-delivered events in Managed Agents middleware."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from common.constants import (
    RUNTIME_SESSION_STATUS_DELETED,
    RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
)
from common.schemas import ThreadDeleteRequestedEvent
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)
from services.agent_persistence_worker_v3.app.services.firestore_threads import (
    FirestoreThreadsRepository,
)
from services.agent_persistence_worker_v3.app.services.idempotency import IdempotencyStore


@dataclass(frozen=True)
class DeleteThreadResult:
    event_id: str
    thread_id: str
    runtime_session_status: str


class DeleteThreadService:
    def __init__(
        self,
        *,
        idempotency_store: IdempotencyStore | None = None,
        threads_repository: FirestoreThreadsRepository | None = None,
        firestore_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.idempotency_store = idempotency_store or IdempotencyStore()
        self.threads_repository = threads_repository or FirestoreThreadsRepository()
        self.firestore_client_factory = firestore_client_factory or get_firestore_client

    async def delete_requested(
        self,
        event: ThreadDeleteRequestedEvent,
    ) -> DeleteThreadResult:
        duplicate_result = await asyncio.to_thread(
            self._processed_duplicate_result_sync,
            event,
        )
        if duplicate_result is not None:
            return duplicate_result

        outcome = (RUNTIME_SESSION_STATUS_NOT_APPLICABLE, "runtime_cleanup_not_applicable")
        return await asyncio.to_thread(self._persist_outcome_sync, event, outcome)

    def _processed_duplicate_result_sync(
        self,
        event: ThreadDeleteRequestedEvent,
    ) -> DeleteThreadResult | None:
        client = self.firestore_client_factory()
        if not self.idempotency_store.exists(client, event.event_id):
            return None
        return DeleteThreadResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            runtime_session_status=RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
        )

    def _persist_outcome_sync(
        self,
        event: ThreadDeleteRequestedEvent,
        outcome: tuple[str, str | None],
    ) -> DeleteThreadResult:
        runtime_status, error_code = outcome
        client = self.firestore_client_factory()
        batch = client.batch()
        self.idempotency_store.add_create_to_batch(batch, client, event)

        self.threads_repository.add_delete_completed_to_batch(
            batch,
            client,
            thread_id=event.thread_id,
            completed_at=datetime.now(timezone.utc),
            runtime_session_status=runtime_status,
            error_code=error_code,
        )

        try:
            batch.commit()
        except Exception as exc:
            if self._is_conflict_error(exc):
                return DeleteThreadResult(
                    event_id=event.event_id,
                    thread_id=event.thread_id,
                    runtime_session_status=runtime_status,
                )
            raise RetryableWorkerError(
                f"Failed to persist delete lifecycle event {event.event_id}: {exc}"
            ) from exc

        return DeleteThreadResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            runtime_session_status=runtime_status,
        )

    def _is_conflict_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "already exists" in message or "conflict" in message or "409" in message
