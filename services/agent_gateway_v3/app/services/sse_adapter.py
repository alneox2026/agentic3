"""SSE event formatting helpers."""

from __future__ import annotations

import json
from typing import Any

from common.constants import (
    SSE_EVENT_DONE,
    SSE_EVENT_ERROR,
    SSE_EVENT_METADATA,
    SSE_EVENT_STATUS,
    SSE_EVENT_TOKEN,
)


def format_sse(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"


def build_metadata_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_METADATA, payload)


def build_status_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_STATUS, payload)


def build_token_event(text: str) -> str:
    return format_sse(SSE_EVENT_TOKEN, {"text": text})


def build_done_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_DONE, payload)


def build_error_event(code: str, message: str, details: dict[str, Any] | None = None) -> str:
    return format_sse(
        SSE_EVENT_ERROR,
        {
            "code": code,
            "message": message,
            "details": details or {},
        },
    )
