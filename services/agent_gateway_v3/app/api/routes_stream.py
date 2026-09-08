"""Streaming chat route placeholder for the agent gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.auth import authenticate_request
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.core.logging import log_structured
from services.agent_gateway_v3.app.services.agent_registry import get_agent_config
from services.agent_gateway_v3.app.services.backend_types import (
    BufferedAgentResponse,
    UpstreamStreamEvent,
)
from services.agent_gateway_v3.app.services.chat_backend_resolver import (
    StreamingChatBackendClient,
    get_streaming_chat_backend_client,
)
from services.agent_gateway_v3.app.services.chat_session_service import get_chat_session_service
from services.agent_gateway_v3.app.services.pubsub_publisher import get_pubsub_publisher
from services.agent_gateway_v3.app.services.request_context import build_request_context
from services.agent_gateway_v3.app.services.sse_adapter import (
    build_done_event,
    build_error_event,
    build_metadata_event,
    build_status_event,
    build_token_event,
)
from services.agent_gateway_v3.app.services.turn_event_builder import (
    build_turn_completed_event,
)
from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler
from services.agent_gateway_v3.app.services.wallet_reservations import (
    get_wallet_reservation_service,
)


LOGGER = logging.getLogger(__name__)
router = APIRouter()
STREAM_STATUS_INTERVAL_SECONDS = 15.0
MAX_LOG_REASON_LENGTH = 500
STREAM_FALLBACKABLE_CODES = {
    "agent_runtime_stream_connect_timeout",
    "agent_runtime_stream_read_timeout",
    "agent_runtime_stream_unreachable",
    "agent_runtime_stream_error",
    "cloud_run_adk_stream_connect_timeout",
    "cloud_run_adk_stream_read_timeout",
    "cloud_run_adk_stream_unreachable",
    "cloud_run_adk_stream_error",
}


@dataclass
class StreamDiagnostics:
    upstream_sse_message_count: int = 0
    upstream_text_event_count: int = 0
    upstream_text_fragment_count: int = 0
    normalized_token_event_count: int = 0
    upstream_first_event_latency_ms: int | None = None
    first_token_latency_ms: int | None = None
    upstream_event_names: list[str] = field(default_factory=list)
    upstream_fragment_counts: list[int] = field(default_factory=list)
    upstream_payload_keys: list[list[str]] = field(default_factory=list)
    upstream_fragment_lengths: list[list[int]] = field(default_factory=list)

    def record_upstream_event(
        self,
        *,
        event_name: str | None,
        payload: dict,
        fragments: list[str],
        started_at: datetime,
        include_debug_shape: bool,
    ) -> None:
        self.upstream_sse_message_count += 1
        if self.upstream_first_event_latency_ms is None:
            self.upstream_first_event_latency_ms = int(
                (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
            )
        if fragments:
            self.upstream_text_event_count += 1
            self.upstream_text_fragment_count += len(fragments)
            if self.first_token_latency_ms is None:
                self.first_token_latency_ms = int(
                    (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
                )

        if include_debug_shape:
            self.upstream_event_names.append(event_name or "_unnamed")
            self.upstream_fragment_counts.append(len(fragments))
            self.upstream_payload_keys.append(sorted(str(key) for key in payload.keys()))
            self.upstream_fragment_lengths.append([len(fragment) for fragment in fragments])

    def record_emitted_token(self) -> None:
        self.normalized_token_event_count += 1

    def completion_fields(self, reply_text: str) -> dict[str, int | None]:
        return {
            "upstream_sse_message_count": self.upstream_sse_message_count,
            "upstream_text_event_count": self.upstream_text_event_count,
            "upstream_text_fragment_count": self.upstream_text_fragment_count,
            "normalized_token_event_count": self.normalized_token_event_count,
            "reply_text_char_count": len(reply_text),
            "upstream_first_event_latency_ms": self.upstream_first_event_latency_ms,
            "first_token_latency_ms": self.first_token_latency_ms,
        }

    def debug_fields(self) -> dict[str, list]:
        return {
            "upstream_event_names": self.upstream_event_names,
            "upstream_fragment_counts": self.upstream_fragment_counts,
            "upstream_payload_keys": self.upstream_payload_keys,
            "upstream_fragment_lengths": self.upstream_fragment_lengths,
        }


def _emit_stream_debug_log(
    *,
    enabled: bool,
    request_id: str,
    turn_id: str,
    agent_id: str,
    thread_id: str,
    session_id: str,
    diagnostics: StreamDiagnostics,
    outcome: str,
    reply_text: str = "",
) -> None:
    if not enabled:
        return
    log_structured(
        LOGGER,
        logging.INFO,
        "gateway_stream_upstream_diagnostics",
        request_id=request_id,
        turn_id=turn_id,
        agent_id=agent_id,
        thread_id=thread_id,
        session_id=session_id,
        outcome=outcome,
        **diagnostics.completion_fields(reply_text),
        **diagnostics.debug_fields(),
    )


def _elapsed_ms(started_at: datetime) -> int:
    return int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)


def _safe_log_reason(details: dict | None) -> str | None:
    if not isinstance(details, dict):
        return None
    reason = details.get("reason") or details.get("detail")
    if reason is None:
        return None
    return str(reason)[:MAX_LOG_REASON_LENGTH]


def _build_waiting_status_payload(
    *,
    request_id: str,
    turn_id: str,
    started_at: datetime,
) -> dict[str, str | int]:
    return {
        "phase": "waiting_for_runtime",
        "elapsed_ms": _elapsed_ms(started_at),
        "request_id": request_id,
        "turn_id": turn_id,
    }


async def _publish_completed_turn(
    *,
    agent_config: AgentConfig,
    request_context,
    user_id: str,
    payload: ChatRequest,
    thread_id: str,
    session_id: str,
    assistant_message: str,
    usage: dict,
    billing_metadata: dict | None = None,
):
    publish_result = None
    publish_latency_ms = 0
    if agent_config.persistence_enabled:
        publisher = await get_pubsub_publisher()
        persistence_event = build_turn_completed_event(
            request_context=request_context,
            agent_config=agent_config,
            user_id=user_id,
            payload=payload,
            thread_id=thread_id,
            session_id=session_id,
            assistant_message=assistant_message,
            usage=usage,
            billing_metadata=billing_metadata,
        )
        publish_started_at = datetime.now(timezone.utc)
        publish_result = await publisher.publish_turn_completed(persistence_event)
        publish_latency_ms = int(
            (datetime.now(timezone.utc) - publish_started_at).total_seconds() * 1000
        )
    return publish_result, publish_latency_ms


@router.post("/v1/agents/{agent_id}/chat/stream")
async def stream_chat(
    request: Request,
    agent_id: str,
    payload: ChatRequest,
) -> StreamingResponse:
    settings = get_settings()
    agent_config = get_agent_config(agent_id)
    request_context = build_request_context(
        agent_id=agent_config.agent_id,
        client_turn_id=payload.client_turn_id,
    )
    user_id = await authenticate_request(request)
    if (
        getattr(settings, "billing_enforcement_enabled", False)
        and not agent_config.persistence_enabled
    ):
        raise ApiError(
            503,
            "billing_persistence_required",
            "This agent cannot be used while prepaid billing is enabled because settlement is unavailable.",
            {"agent_id": agent_config.agent_id},
        )
    if not agent_config.streaming_enabled:
        log_structured(
            LOGGER,
            logging.INFO,
            "gateway_stream_rejected_disabled",
            request_id=request_context.request_id,
            turn_id=request_context.turn_id,
            agent_id=agent_config.agent_id,
            user_id=user_id,
        )
        raise ApiError(
            409,
            "streaming_not_enabled",
            f"This gateway is configured for non-streaming chat. Use /v1/agents/{agent_config.agent_id}/chat.",
            {"agent_id": agent_config.agent_id},
        )
    backend_client = await get_streaming_chat_backend_client(agent_config)
    session_service = await get_chat_session_service()
    session_result = await session_service.resolve(
        runtime_client=backend_client,
        agent_config=agent_config,
        user_id=user_id,
        request=payload,
    )
    wallet_reservation_service = await get_wallet_reservation_service()
    billing_reservation = await wallet_reservation_service.reserve(
        user_id=user_id,
        agent_id=agent_config.agent_id,
        request_id=request_context.request_id,
        turn_id=request_context.turn_id,
    )
    billing_metadata = (
        billing_reservation.event_metadata() if billing_reservation else None
    )
    log_structured(
        LOGGER,
        logging.INFO,
        "gateway_stream_started",
        request_id=request_context.request_id,
        turn_id=request_context.turn_id,
        agent_id=agent_config.agent_id,
        user_id=user_id,
        session_created=session_result.created_new,
    )

    async def event_stream() -> AsyncIterator[str]:
        assembler = TurnAssembler(model_name=agent_config.model)
        diagnostics = StreamDiagnostics()
        yield build_metadata_event(
            {
                "ok": True,
                "request_id": request_context.request_id,
                "turn_id": request_context.turn_id,
                "agent_id": agent_config.agent_id,
                "thread_id": session_result.thread_id,
                "session_id": session_result.session_id,
            }
        )
        yield build_status_event(
            _build_waiting_status_payload(
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                started_at=request_context.started_at,
            )
        )
        try:
            async for upstream_event in _stream_upstream_events_with_heartbeats(
                backend_client=backend_client,
                agent_config=agent_config,
                user_id=user_id,
                session_id=session_result.session_id,
                message=payload.message,
            ):
                if upstream_event is None:
                    yield build_status_event(
                        _build_waiting_status_payload(
                            request_id=request_context.request_id,
                            turn_id=request_context.turn_id,
                            started_at=request_context.started_at,
                        )
                    )
                    continue

                assembler.add_event(upstream_event.payload)
                fragments = backend_client.extract_text_fragments(upstream_event.payload)
                diagnostics.record_upstream_event(
                    event_name=upstream_event.event_name,
                    payload=upstream_event.payload,
                    fragments=fragments,
                    started_at=request_context.started_at,
                    include_debug_shape=settings.stream_debug,
                )
                for fragment in fragments:
                    emitted_fragment = assembler.add_text(fragment)
                    if not emitted_fragment:
                        continue
                    diagnostics.record_emitted_token()
                    yield build_token_event(emitted_fragment)

            publish_result, publish_latency_ms = await _publish_completed_turn(
                agent_config=agent_config,
                request_context=request_context,
                user_id=user_id,
                payload=payload,
                thread_id=session_result.thread_id,
                session_id=session_result.session_id,
                assistant_message=assembler.reply_text(),
                usage=assembler.usage,
                billing_metadata=billing_metadata,
            )
            latency_ms = int(
                (
                    datetime.now(timezone.utc) - request_context.started_at
                ).total_seconds()
                * 1000
            )
            log_structured(
                LOGGER,
                logging.INFO,
                "gateway_stream_completed",
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                thread_id=session_result.thread_id,
                session_id=session_result.session_id,
                pubsub_message_id=publish_result.message_id if publish_result else None,
                publish_latency_ms=publish_latency_ms,
                latency_ms=latency_ms,
                outcome="completed",
                **diagnostics.completion_fields(assembler.reply_text()),
            )
            _emit_stream_debug_log(
                enabled=settings.stream_debug,
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                thread_id=session_result.thread_id,
                session_id=session_result.session_id,
                diagnostics=diagnostics,
                outcome="completed",
                reply_text=assembler.reply_text(),
            )
            yield build_done_event(
                {
                    "ok": True,
                    "request_id": request_context.request_id,
                    "turn_id": request_context.turn_id,
                    "agent_id": agent_config.agent_id,
                    "thread_id": session_result.thread_id,
                    "session_id": session_result.session_id,
                    "reply_text": assembler.reply_text(),
                    "usage": assembler.usage,
                    "pubsub_message_id": (
                        publish_result.message_id if publish_result else None
                    ),
                }
            )
        except ApiError as exc:
            if (
                diagnostics.normalized_token_event_count == 0
                and exc.code in STREAM_FALLBACKABLE_CODES
            ):
                try:
                    fallback_response: BufferedAgentResponse = (
                        await backend_client.chat_buffered_query(
                            agent_config=agent_config,
                            user_id=user_id,
                            session_id=session_result.session_id,
                            message=payload.message,
                        )
                    )
                    fallback_usage = getattr(fallback_response, "usage", {}) or {}
                    publish_result, publish_latency_ms = await _publish_completed_turn(
                        agent_config=agent_config,
                        request_context=request_context,
                        user_id=user_id,
                        payload=payload,
                        thread_id=session_result.thread_id,
                        session_id=session_result.session_id,
                        assistant_message=fallback_response.reply_text,
                        usage=fallback_usage,
                        billing_metadata=billing_metadata,
                    )
                    latency_ms = _elapsed_ms(request_context.started_at)
                    log_structured(
                        LOGGER,
                        logging.INFO,
                        "gateway_stream_fallback_completed",
                        request_id=request_context.request_id,
                        turn_id=request_context.turn_id,
                        agent_id=agent_config.agent_id,
                        thread_id=session_result.thread_id,
                        session_id=session_result.session_id,
                        fallback_from_code=exc.code,
                        pubsub_message_id=(
                            publish_result.message_id if publish_result else None
                        ),
                        publish_latency_ms=publish_latency_ms,
                        latency_ms=latency_ms,
                        reply_text_char_count=len(fallback_response.reply_text),
                    )
                    if fallback_response.reply_text:
                        yield build_token_event(fallback_response.reply_text)
                    yield build_done_event(
                        {
                            "ok": True,
                            "request_id": request_context.request_id,
                            "turn_id": request_context.turn_id,
                            "agent_id": agent_config.agent_id,
                            "thread_id": session_result.thread_id,
                            "session_id": session_result.session_id,
                            "reply_text": fallback_response.reply_text,
                            "usage": fallback_usage,
                            "pubsub_message_id": (
                                publish_result.message_id if publish_result else None
                            ),
                            "fallback": True,
                            "fallback_from_code": exc.code,
                        }
                    )
                    return
                except Exception as fallback_exc:
                    log_structured(
                        LOGGER,
                        logging.WARNING,
                        "gateway_stream_fallback_failed",
                        request_id=request_context.request_id,
                        turn_id=request_context.turn_id,
                        agent_id=agent_config.agent_id,
                        stream_code=exc.code,
                        fallback_reason=str(fallback_exc)[:MAX_LOG_REASON_LENGTH],
                    )

            log_structured(
                LOGGER,
                logging.WARNING,
                "gateway_stream_failed",
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                code=exc.code,
                status_code=exc.status_code,
                timeout_type=exc.details.get("timeout_type") if exc.details else None,
                upstream_connect_timeout_seconds=settings.upstream_connect_timeout_seconds,
                upstream_read_timeout_seconds=settings.upstream_read_timeout_seconds,
                upstream_elapsed_ms=_elapsed_ms(request_context.started_at),
                upstream_first_event_latency_ms=diagnostics.upstream_first_event_latency_ms,
                first_token_latency_ms=diagnostics.first_token_latency_ms,
                upstream_sse_message_count=diagnostics.upstream_sse_message_count,
                reason=_safe_log_reason(exc.details) or (str(exc.message)[:MAX_LOG_REASON_LENGTH] if getattr(exc, "message", None) else None),
            )
            _emit_stream_debug_log(
                enabled=settings.stream_debug,
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                thread_id=session_result.thread_id,
                session_id=session_result.session_id,
                diagnostics=diagnostics,
                outcome="api_error",
            )
            yield build_error_event(exc.code, exc.message, exc.details)
            yield build_done_event(
                {
                    "ok": False,
                    "request_id": request_context.request_id,
                    "turn_id": request_context.turn_id,
                    "agent_id": agent_config.agent_id,
                    "thread_id": session_result.thread_id,
                    "session_id": session_result.session_id,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            log_structured(
                LOGGER,
                logging.ERROR,
                "gateway_stream_failed_unexpected",
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                reason=str(exc),
            )
            _emit_stream_debug_log(
                enabled=settings.stream_debug,
                request_id=request_context.request_id,
                turn_id=request_context.turn_id,
                agent_id=agent_config.agent_id,
                thread_id=session_result.thread_id,
                session_id=session_result.session_id,
                diagnostics=diagnostics,
                outcome="unexpected_error",
            )
            yield build_error_event(
                "internal_error",
                "The gateway encountered an unexpected error while streaming.",
                {"reason": str(exc)},
            )
            yield build_done_event(
                {
                    "ok": False,
                    "request_id": request_context.request_id,
                    "turn_id": request_context.turn_id,
                    "agent_id": agent_config.agent_id,
                    "thread_id": session_result.thread_id,
                    "session_id": session_result.session_id,
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _stream_upstream_events(
    *,
    backend_client: StreamingChatBackendClient,
    agent_config: AgentConfig,
    user_id: str,
    session_id: str,
    message: str,
) -> AsyncIterator[UpstreamStreamEvent]:
    async for event in backend_client.stream_chat_events(
        agent_config=agent_config,
        user_id=user_id,
        session_id=session_id,
        message=message,
    ):
        yield event


async def _stream_upstream_events_with_heartbeats(
    *,
    backend_client: StreamingChatBackendClient,
    agent_config: AgentConfig,
    user_id: str,
    session_id: str,
    message: str,
) -> AsyncIterator[UpstreamStreamEvent | None]:
    upstream = _stream_upstream_events(
        backend_client=backend_client,
        agent_config=agent_config,
        user_id=user_id,
        session_id=session_id,
        message=message,
    ).__aiter__()
    next_event = asyncio.create_task(anext(upstream))
    try:
        while True:
            heartbeat = asyncio.create_task(asyncio.sleep(STREAM_STATUS_INTERVAL_SECONDS))
            done, pending = await asyncio.wait(
                {next_event, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done and next_event not in done:
                yield None
                continue

            if heartbeat in pending:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

            try:
                event = next_event.result()
            except StopAsyncIteration:
                break
            yield event
            next_event = asyncio.create_task(anext(upstream))
    finally:
        if not next_event.done():
            next_event.cancel()
            with suppress(asyncio.CancelledError):
                await next_event
        with suppress(Exception):
            await upstream.aclose()
