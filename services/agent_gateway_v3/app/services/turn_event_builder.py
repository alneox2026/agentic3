"""Helpers to build normalized persistence events from gateway turns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from common.ids import new_event_id
from common.schemas import (
    AgentConfig,
    ChatRequest,
    ThreadDeleteRequestedEvent,
    ThreadLifecycleRequest,
    TurnCompletedEvent,
)
from services.agent_gateway_v3.app.services.request_context import RequestContext


def build_turn_completed_event(
    *,
    request_context: RequestContext,
    agent_config: AgentConfig,
    user_id: str,
    payload: ChatRequest,
    thread_id: str,
    session_id: str,
    assistant_message: str,
    usage: dict[str, Any] | None = None,
    billing_metadata: dict[str, Any] | None = None,
) -> TurnCompletedEvent:
    metadata: dict[str, Any] = {
        "request_id": request_context.request_id,
    }
    if payload.client_turn_id:
        metadata["client_turn_id"] = payload.client_turn_id
    if payload.metadata:
        metadata["client_metadata"] = payload.metadata
    if billing_metadata:
        metadata["billing"] = dict(billing_metadata)

    return TurnCompletedEvent(
        event_id=new_event_id(),
        turn_id=request_context.turn_id,
        agent_id=agent_config.agent_id,
        user_id=user_id,
        thread_id=thread_id,
        session_id=session_id,
        user_message=payload.message,
        assistant_message=assistant_message,
        created_at=datetime.now(timezone.utc),
        usage=usage or {},
        metadata=metadata,
    )


def build_thread_delete_requested_event(
    *,
    request_context: RequestContext,
    agent_config: AgentConfig,
    user_id: str,
    thread_id: str,
    session_id: str,
    payload: ThreadLifecycleRequest,
) -> ThreadDeleteRequestedEvent:
    metadata: dict[str, Any] = {
        "request_id": request_context.request_id,
    }
    if payload.metadata:
        metadata["client_metadata"] = payload.metadata

    return ThreadDeleteRequestedEvent(
        event_id=new_event_id(),
        agent_id=agent_config.agent_id,
        agent_backend=agent_config.backend,
        agent_region=agent_config.region,
        agent_resource_name=agent_config.resource_name,
        agent_base_url=agent_config.base_url,
        agent_app_name=agent_config.app_name,
        agent_audience=agent_config.audience,
        runtime_session_cleanup=agent_config.runtime_session_cleanup or "agent_runtime",
        user_id=user_id,
        thread_id=thread_id,
        session_id=session_id,
        created_at=datetime.now(timezone.utc),
        reason=payload.reason,
        metadata=metadata,
    )
