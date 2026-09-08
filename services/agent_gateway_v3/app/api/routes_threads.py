"""Thread lifecycle routes for archive and delete operations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from common.schemas import ThreadLifecycleRequest, ThreadLifecycleResponse
from services.agent_gateway_v3.app.core.auth import authenticate_request
from services.agent_gateway_v3.app.core.logging import log_structured
from services.agent_gateway_v3.app.services.agent_registry import get_agent_config
from services.agent_gateway_v3.app.services.request_context import build_request_context
from services.agent_gateway_v3.app.services.thread_lifecycle_service import (
    get_thread_lifecycle_service,
)


LOGGER = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/agents/{agent_id}/threads/{thread_id}/archive")
async def archive_thread(
    request: Request,
    agent_id: str,
    thread_id: str,
    payload: ThreadLifecycleRequest,
) -> ThreadLifecycleResponse:
    agent_config = get_agent_config(agent_id)
    request_context = build_request_context(agent_id=agent_config.agent_id)
    user_id = await authenticate_request(request)
    service = await get_thread_lifecycle_service()
    response = await service.archive_thread(
        request_context=request_context,
        agent_config=agent_config,
        thread_id=thread_id,
        user_id=user_id,
        payload=payload,
    )
    log_structured(
        LOGGER,
        logging.INFO,
        "gateway_thread_archived",
        request_id=request_context.request_id,
        agent_id=agent_config.agent_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    return response


@router.post("/v1/agents/{agent_id}/threads/{thread_id}/delete")
async def delete_thread(
    request: Request,
    agent_id: str,
    thread_id: str,
    payload: ThreadLifecycleRequest,
) -> ThreadLifecycleResponse:
    agent_config = get_agent_config(agent_id)
    request_context = build_request_context(agent_id=agent_config.agent_id)
    user_id = await authenticate_request(request)
    service = await get_thread_lifecycle_service()
    response = await service.delete_thread(
        request_context=request_context,
        agent_config=agent_config,
        thread_id=thread_id,
        user_id=user_id,
        payload=payload,
    )
    log_structured(
        LOGGER,
        logging.INFO,
        "gateway_thread_delete_requested",
        request_id=request_context.request_id,
        agent_id=agent_config.agent_id,
        thread_id=thread_id,
        user_id=user_id,
        runtime_session_status=response.runtime_session_status,
    )
    return response
