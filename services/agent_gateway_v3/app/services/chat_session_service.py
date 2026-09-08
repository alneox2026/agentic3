"""Server-owned chat thread and Agent Runtime session resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.chat_backend_resolver import ChatBackendClient
from services.agent_gateway_v3.app.services.firestore_client import get_firestore_client
from services.agent_gateway_v3.app.services.thread_repository import ThreadRepository


@dataclass(frozen=True)
class ResolvedChatSession:
    session_id: str
    thread_id: str
    created_new: bool


class ChatSessionService:
    def __init__(
        self,
        *,
        thread_repository: ThreadRepository | None = None,
        firestore_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._thread_repository = thread_repository or ThreadRepository()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client

    async def resolve(
        self,
        *,
        runtime_client: ChatBackendClient,
        agent_config: AgentConfig,
        user_id: str,
        request: ChatRequest,
    ) -> ResolvedChatSession:
        if request.thread_id:
            return await asyncio.to_thread(
                self._resolve_existing_sync,
                agent_config,
                user_id,
                request,
            )

        if request.session_id:
            raise ApiError(
                400,
                "session_id_without_thread_id",
                "A session_id cannot be supplied without a backend-owned thread_id.",
            )

        created = await runtime_client.ensure_session(
            agent_config=agent_config,
            user_id=user_id,
            request=request,
        )
        return ResolvedChatSession(
            session_id=created.session_id,
            thread_id=created.thread_id,
            created_new=True,
        )

    def _resolve_existing_sync(
        self,
        agent_config: AgentConfig,
        user_id: str,
        request: ChatRequest,
    ) -> ResolvedChatSession:
        client = self._firestore_client_factory()
        thread = self._thread_repository.assert_active_owned_thread(
            client,
            thread_id=request.thread_id or "",
            user_id=user_id,
            agent_id=agent_config.agent_id,
        )
        stored_session_id = str(thread.get("session_id", "")).strip()
        if request.session_id and request.session_id != stored_session_id:
            raise ApiError(
                409,
                "session_id_mismatch",
                "The supplied session_id does not match the backend-owned thread session.",
                {"thread_id": request.thread_id},
            )
        return ResolvedChatSession(
            session_id=stored_session_id,
            thread_id=request.thread_id or "",
            created_new=False,
        )


_service_singleton: ChatSessionService | None = None
_service_lock = asyncio.Lock()


async def get_chat_session_service() -> ChatSessionService:
    global _service_singleton
    if _service_singleton is None:
        async with _service_lock:
            if _service_singleton is None:
                _service_singleton = ChatSessionService()
    return _service_singleton
