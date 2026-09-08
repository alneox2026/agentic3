"""Firestore-backed thread lifecycle helpers for the gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.constants import (
    RUNTIME_SESSION_STATUS_DELETE_PENDING,
    RUNTIME_SESSION_STATUS_DELETED,
    RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DELETED,
)
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError


class ThreadRepository:
    def __init__(self) -> None:
        self._settings = get_settings()

    def document(self, client: Any, thread_id: str):
        return client.collection(self._settings.threads_collection).document(thread_id)

    def get_thread(self, client: Any, thread_id: str) -> dict[str, Any] | None:
        snapshot = self.document(client, thread_id).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    def assert_owned_thread(
        self,
        client: Any,
        *,
        thread_id: str,
        user_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        thread = self.get_thread(client, thread_id)
        if thread is None:
            raise ApiError(
                404,
                "thread_not_found",
                "The requested thread does not exist.",
                {"thread_id": thread_id},
            )
        if str(thread.get("uid", "")).strip() != user_id:
            raise ApiError(
                403,
                "thread_access_denied",
                "The current user does not own this thread.",
                {"thread_id": thread_id},
            )
        if str(thread.get("agent_id", "")).strip() != agent_id:
            raise ApiError(
                404,
                "thread_not_found",
                "The requested thread does not exist for this agent.",
                {"thread_id": thread_id, "agent_id": agent_id},
            )
        return thread

    def assert_active_owned_thread(
        self,
        client: Any,
        *,
        thread_id: str,
        user_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        thread = self.assert_owned_thread(
            client,
            thread_id=thread_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        status = str(thread.get("status", "")).strip() or STATUS_ACTIVE
        if status == STATUS_ARCHIVED:
            raise ApiError(
                409,
                "thread_archived",
                "Archived threads are read-only and cannot receive new chat turns.",
                {"thread_id": thread_id},
            )
        if status == STATUS_DELETED:
            raise ApiError(
                409,
                "thread_deleted",
                "Deleted threads cannot be reopened.",
                {"thread_id": thread_id},
            )
        if status != STATUS_ACTIVE:
            raise ApiError(
                409,
                "thread_not_active",
                "Only active threads can receive new chat turns.",
                {"thread_id": thread_id, "status": status},
            )
        session_id = str(thread.get("session_id", "")).strip()
        last_interaction_id = str(thread.get("last_interaction_id", "")).strip()
        backend = str(thread.get("backend", "")).strip()
        if not session_id and not last_interaction_id and backend != "gemini_managed":
            raise ApiError(
                409,
                "thread_missing_session_id",
                "The requested thread does not have an active session.",
                {"thread_id": thread_id},
            )
        return thread

    def update_interaction_state(
        self,
        client: Any,
        *,
        thread_id: str,
        interaction_id: str,
        environment_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "last_interaction_id": interaction_id,
            "updated_at": datetime.now(),
        }
        if environment_id:
            payload["environment_id"] = environment_id
        self.document(client, thread_id).set(payload, merge=True)

    def archive_thread(
        self,
        client: Any,
        *,
        thread_id: str,
        archived_at: datetime,
        reason: str | None,
        metadata: dict[str, Any],
    ) -> None:
        payload: dict[str, Any] = {
            "status": STATUS_ARCHIVED,
            "archived_at": archived_at,
            "updated_at": archived_at,
        }
        if reason:
            payload["archive_reason"] = reason
        if metadata:
            payload["archive_metadata"] = metadata
        self.document(client, thread_id).set(payload, merge=True)

    def mark_delete(
        self,
        client: Any,
        *,
        thread_id: str,
        deleted_at: datetime,
        user_id: str,
        runtime_session_status: str,
        reason: str | None,
        metadata: dict[str, Any],
    ) -> None:
        payload: dict[str, Any] = {
            "status": STATUS_DELETED,
            "deleted_at": deleted_at,
            "delete_requested_at": deleted_at,
            "deleted_by_uid": user_id,
            "runtime_session_status": runtime_session_status,
            "updated_at": deleted_at,
            "last_runtime_error_code": None,
            "last_runtime_error_message": None,
        }
        if runtime_session_status in {
            RUNTIME_SESSION_STATUS_DELETED,
            RUNTIME_SESSION_STATUS_NOT_APPLICABLE,
        }:
            payload["delete_completed_at"] = deleted_at
        if reason:
            payload["delete_reason"] = reason
        if metadata:
            payload["delete_metadata"] = metadata
        self.document(client, thread_id).set(payload, merge=True)

    def current_delete_runtime_status(self, thread: dict[str, Any]) -> str:
        session_id = str(thread.get("session_id", "")).strip()
        if not session_id:
            return RUNTIME_SESSION_STATUS_DELETED
        return RUNTIME_SESSION_STATUS_DELETE_PENDING
