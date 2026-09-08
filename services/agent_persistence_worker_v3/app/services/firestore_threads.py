"""Thread persistence helpers for the worker."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.constants import (
    RUNTIME_SESSION_STATUS_DELETE_FAILED,
    RUNTIME_SESSION_STATUS_DELETED,
    RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DELETED,
)
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings

THREAD_TITLE_MAX_CHARS = 120
THREAD_PREVIEW_MAX_CHARS = 280


class FirestoreThreadsRepository:
    def __init__(self) -> None:
        self._settings = get_settings()

    def document(self, client: Any, thread_id: str):
        return client.collection(self._settings.threads_collection).document(thread_id)

    def load_existing(self, client: Any, thread_id: str) -> dict[str, Any] | None:
        snapshot = self.document(client, thread_id).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    def add_upsert_to_batch(
        self,
        batch: Any,
        client: Any,
        event: TurnCompletedEvent,
        *,
        existing_thread: dict[str, Any] | None = None,
    ) -> None:
        document = self.document(client, event.thread_id)
        existing_status = ""
        if existing_thread:
            existing_status = str(existing_thread.get("status", "")).strip()
        payload: dict[str, Any] = {
            "uid": event.user_id,
            "agent_id": event.agent_id,
            "thread_id": event.thread_id,
            "status": existing_status or STATUS_ACTIVE,
        }
        if not existing_thread:
            payload["created_at"] = event.created_at
            payload["title"] = self._preview(event.user_message, THREAD_TITLE_MAX_CHARS)

        if self._should_update_summary(existing_thread, event.created_at):
            payload.update(
                {
                    "session_id": event.session_id,
                    "updated_at": event.created_at,
                    "last_message_at": event.created_at,
                    "last_user_message": self._preview(event.user_message),
                    "last_assistant_message": self._preview(event.assistant_message),
                }
            )

        batch.set(document, payload, merge=True)

    def _preview(self, value: str, limit: int = THREAD_PREVIEW_MAX_CHARS) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def validate_existing_for_turn(
        self,
        existing_thread: dict[str, Any] | None,
        event: TurnCompletedEvent,
    ) -> str | None:
        if not existing_thread:
            return None

        existing_uid = str(existing_thread.get("uid", "")).strip()
        existing_agent_id = str(existing_thread.get("agent_id", "")).strip()
        existing_session_id = str(existing_thread.get("session_id", "")).strip()
        existing_status = str(existing_thread.get("status", "")).strip()

        if existing_uid and existing_uid != event.user_id:
            return "uid_mismatch"
        if existing_agent_id and existing_agent_id != event.agent_id:
            return "agent_id_mismatch"
        if existing_session_id and existing_session_id != event.session_id:
            return "session_id_mismatch"
        if existing_status == STATUS_DELETED:
            return "thread_deleted"
        if existing_status not in {"", STATUS_ACTIVE, STATUS_ARCHIVED}:
            return "unsupported_thread_status"
        return None

    def _should_update_summary(
        self,
        existing_thread: dict[str, Any] | None,
        event_time: datetime,
    ) -> bool:
        if not existing_thread:
            return True
        existing_time = existing_thread.get("last_message_at")
        if existing_time is None:
            return True
        return event_time >= existing_time

    def add_delete_completed_to_batch(
        self,
        batch: Any,
        client: Any,
        *,
        thread_id: str,
        completed_at: datetime,
        runtime_session_status: str = RUNTIME_SESSION_STATUS_DELETED,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "runtime_session_status": runtime_session_status,
            "delete_completed_at": completed_at,
            "updated_at": completed_at,
            "last_runtime_error_message": None,
        }
        if runtime_session_status not in {
            RUNTIME_SESSION_STATUS_DELETED,
            RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
        }:
            payload["runtime_session_status"] = RUNTIME_SESSION_STATUS_DELETED
        payload["last_runtime_error_code"] = error_code
        batch.set(self.document(client, thread_id), payload, merge=True)

    def add_delete_failed_to_batch(
        self,
        batch: Any,
        client: Any,
        *,
        thread_id: str,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        batch.set(
            self.document(client, thread_id),
            {
                "runtime_session_status": RUNTIME_SESSION_STATUS_DELETE_FAILED,
                "last_runtime_error_code": error_code,
                "last_runtime_error_message": error_message,
                "updated_at": failed_at,
            },
            merge=True,
        )
