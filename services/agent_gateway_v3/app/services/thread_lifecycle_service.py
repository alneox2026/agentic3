"""Backend-owned thread lifecycle orchestration for archive and delete actions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from common.constants import (
    RUNTIME_SESSION_STATUS_DELETE_PENDING,
    RUNTIME_SESSION_STATUS_DELETED,
    STATUS_ARCHIVED,
    STATUS_DELETED,
)
from common.schemas import (
    AgentConfig,
    ThreadDeleteRequestedEvent,
    ThreadLifecycleRequest,
    ThreadLifecycleResponse,
)
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.firestore_client import get_firestore_client
from services.agent_gateway_v3.app.services.pubsub_publisher import get_pubsub_publisher
from services.agent_gateway_v3.app.services.request_context import RequestContext
from services.agent_gateway_v3.app.services.thread_repository import ThreadRepository
from services.agent_gateway_v3.app.services.turn_event_builder import (
    build_thread_delete_requested_event,
)


@dataclass(frozen=True)
class ThreadLifecycleResult:
    thread_id: str
    status: str
    session_id: str | None = None
    runtime_session_status: str | None = None


class ThreadLifecycleService:
    def __init__(
        self,
        *,
        thread_repository: ThreadRepository | None = None,
        firestore_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._thread_repository = thread_repository or ThreadRepository()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client

    async def archive_thread(
        self,
        *,
        request_context: RequestContext,
        agent_config: AgentConfig,
        thread_id: str,
        user_id: str,
        payload: ThreadLifecycleRequest,
    ) -> ThreadLifecycleResponse:
        result = await asyncio.to_thread(
            self._archive_thread_sync,
            thread_id,
            user_id,
            agent_config.agent_id,
            payload,
        )
        return ThreadLifecycleResponse(
            ok=True,
            agent_id=agent_config.agent_id,
            thread_id=result.thread_id,
            status=result.status,
            runtime_session_status=result.runtime_session_status,
        )

    async def delete_thread(
        self,
        *,
        request_context: RequestContext,
        agent_config: AgentConfig,
        thread_id: str,
        user_id: str,
        payload: ThreadLifecycleRequest,
    ) -> ThreadLifecycleResponse:
        result = await asyncio.to_thread(
            self._mark_delete_sync,
            thread_id,
            user_id,
            agent_config.agent_id,
            payload,
        )
        if result.runtime_session_status != RUNTIME_SESSION_STATUS_DELETED:
            publisher = await get_pubsub_publisher()
            event = build_thread_delete_requested_event(
                request_context=request_context,
                agent_config=agent_config,
                user_id=user_id,
                thread_id=thread_id,
                session_id=result.session_id or "",
                payload=payload,
            )
            await publisher.publish_thread_delete_requested(event)
            runtime_status = RUNTIME_SESSION_STATUS_DELETE_PENDING
        else:
            runtime_status = result.runtime_session_status

        return ThreadLifecycleResponse(
            ok=True,
            agent_id=agent_config.agent_id,
            thread_id=result.thread_id,
            status=result.status,
            runtime_session_status=runtime_status,
        )

    def _archive_thread_sync(
        self,
        thread_id: str,
        user_id: str,
        agent_id: str,
        payload: ThreadLifecycleRequest,
    ) -> ThreadLifecycleResult:
        client = self._firestore_client_factory()
        thread = self._thread_repository.assert_owned_thread(
            client,
            thread_id=thread_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        current_status = str(thread.get("status", "")).strip()
        if current_status == STATUS_DELETED:
            raise ApiError(
                409,
                "thread_already_deleted",
                "Deleted threads cannot be archived.",
                {"thread_id": thread_id},
            )
        if current_status == STATUS_ARCHIVED:
            return ThreadLifecycleResult(
                thread_id=thread_id,
                status=STATUS_ARCHIVED,
                session_id=str(thread.get("session_id", "")).strip() or None,
                runtime_session_status=str(thread.get("runtime_session_status", "")).strip()
                or None,
            )

        archived_at = datetime.now(timezone.utc)
        self._thread_repository.archive_thread(
            client,
            thread_id=thread_id,
            archived_at=archived_at,
            reason=payload.reason,
            metadata=payload.metadata,
        )
        return ThreadLifecycleResult(
            thread_id=thread_id,
            status=STATUS_ARCHIVED,
            session_id=str(thread.get("session_id", "")).strip() or None,
            runtime_session_status=str(thread.get("runtime_session_status", "")).strip()
            or None,
        )

    def _mark_delete_sync(
        self,
        thread_id: str,
        user_id: str,
        agent_id: str,
        payload: ThreadLifecycleRequest,
    ) -> ThreadLifecycleResult:
        client = self._firestore_client_factory()
        thread = self._thread_repository.assert_owned_thread(
            client,
            thread_id=thread_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        current_status = str(thread.get("status", "")).strip()
        runtime_session_status = str(thread.get("runtime_session_status", "")).strip() or None
        current_session_id = str(thread.get("session_id", "")).strip()
        if current_status == STATUS_DELETED:
            return ThreadLifecycleResult(
                thread_id=thread_id,
                status=STATUS_DELETED,
                session_id=current_session_id or None,
                runtime_session_status=runtime_session_status or RUNTIME_SESSION_STATUS_DELETED,
            )

        deleted_at = datetime.now(timezone.utc)
        target_runtime_status = self._thread_repository.current_delete_runtime_status(thread)
        self._thread_repository.mark_delete(
            client,
            thread_id=thread_id,
            deleted_at=deleted_at,
            user_id=user_id,
            runtime_session_status=target_runtime_status,
            reason=payload.reason,
            metadata=payload.metadata,
        )
        return ThreadLifecycleResult(
            thread_id=thread_id,
            status=STATUS_DELETED,
            session_id=current_session_id or None,
            runtime_session_status=target_runtime_status,
        )


_service_singleton: ThreadLifecycleService | None = None
_service_lock = asyncio.Lock()


async def get_thread_lifecycle_service() -> ThreadLifecycleService:
    global _service_singleton
    if _service_singleton is None:
        async with _service_lock:
            if _service_singleton is None:
                _service_singleton = ThreadLifecycleService()
    return _service_singleton
