"""Backend data structures for the agent gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SessionResult:
    """Represents the resolved session and thread IDs for an agent interaction."""

    session_id: str
    thread_id: str


@dataclass(frozen=True)
class BufferedAgentResponse:
    """Represents a non-streaming buffered response from an agent backend."""

    reply_text: str
    raw_events: list[dict[str, Any]]
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamStreamEvent:
    """Represents a discrete streaming event received from an agent backend."""

    event_name: str | None
    payload: dict[str, Any]
