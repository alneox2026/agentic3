"""Idempotency handling for worker event persistence."""

from __future__ import annotations

from typing import Any

from common.schemas import ThreadDeleteRequestedEvent, TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings


class IdempotencyStore:
    def __init__(self) -> None:
        self._settings = get_settings()

    def document(self, client: Any, event_id: str):
        return client.collection(self._settings.idempotency_collection).document(event_id)

    def exists(self, client: Any, event_id: str) -> bool:
        return bool(self.document(client, event_id).get().exists)

    def add_create_to_batch(
        self,
        batch: Any,
        client: Any,
        event: TurnCompletedEvent | ThreadDeleteRequestedEvent,
    ) -> None:
        batch.create(
            self.document(client, event.event_id),
            {
                "event_id": event.event_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
                "agent_id": event.agent_id,
                "user_id": event.user_id,
                "created_at": event.created_at,
                "event_type": event.event_type,
                "turn_id": getattr(event, "turn_id", None),
                "status": getattr(event, "status", None),
            },
        )
