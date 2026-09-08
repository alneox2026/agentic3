"""Correlation metadata helpers for gateway requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from common.ids import new_turn_id


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    turn_id: str
    started_at: datetime
    agent_id: str


def build_request_context(agent_id: str, client_turn_id: str | None = None) -> RequestContext:
    # Client turn ids are correlation metadata only; persisted document ids stay server-owned.
    turn_id = new_turn_id()
    return RequestContext(
        request_id=f"req-{uuid.uuid4().hex}",
        turn_id=turn_id,
        started_at=datetime.now(timezone.utc),
        agent_id=agent_id,
    )
