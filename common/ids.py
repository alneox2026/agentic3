"""Shared ID generation and validation helpers."""

from __future__ import annotations

import re
import uuid


AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def new_thread_id() -> str:
    return f"thread-{uuid.uuid4().hex}"


def new_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex}"


def new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex}"


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex}"


def validate_agent_id(agent_id: str) -> str:
    cleaned = agent_id.strip().lower()
    if not cleaned:
        raise ValueError("agent_id must not be empty.")
    if not AGENT_ID_PATTERN.fullmatch(cleaned):
        raise ValueError("agent_id contains invalid characters.")
    return cleaned


def validate_thread_id(thread_id: str | None) -> str | None:
    if thread_id is None:
        return None
    cleaned = thread_id.strip()
    if not cleaned:
        return None
    if len(cleaned) > 128:
        raise ValueError("thread_id must be 128 characters or fewer.")
    if "/" in cleaned:
        raise ValueError("thread_id must not contain '/'.")
    return cleaned


def validate_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    cleaned = session_id.strip()
    if not cleaned:
        return None
    if len(cleaned) > 256:
        raise ValueError("session_id must be 256 characters or fewer.")
    if "/" in cleaned:
        raise ValueError("session_id must not contain '/'.")
    return cleaned
