"""Immutable billing-ledger writes for completed agent turns."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings


NANOS_PER_USD = Decimal("1000000000")


class BillingLedgerRepository:
    """Adds one immutable cost record per completed turn to a Firestore batch."""

    def __init__(self, collection: str | None = None) -> None:
        settings = get_settings()
        self._collection = collection or settings.billing_ledger_collection

    def document(self, client: Any, turn_id: str):
        return client.collection(self._collection).document(turn_id)

    def add_create_to_batch(
        self,
        batch: Any,
        client: Any,
        event: TurnCompletedEvent,
    ) -> None:
        """Create, rather than merge, so a ledger entry can never be rewritten."""
        batch.create(self.document(client, event.turn_id), self._payload(event))

    def _payload(self, event: TurnCompletedEvent) -> dict[str, Any]:
        usage = event.usage if isinstance(event.usage, dict) else {}
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        billing = metadata.get("billing")
        billing_metadata = billing if isinstance(billing, dict) else {}
        cost_usd = self._estimated_cost_usd(usage)
        cost_usd_nanos = self._cost_usd_nanos(cost_usd)

        return {
            "schema_version": 1,
            "event_id": event.event_id,
            "turn_id": event.turn_id,
            "uid": event.user_id,
            "billing_subject_id": self._optional_string(
                billing_metadata.get("billing_subject_id")
            )
            or event.user_id,
            "billing_reservation_id": self._optional_string(
                billing_metadata.get("reservation_id")
                or billing_metadata.get("billing_reservation_id")
            ),
            "billing_reservation_nanos": self._nonnegative_int(
                billing_metadata.get("reserved_amount_nanos")
            ),
            "agent_id": event.agent_id,
            "thread_id": event.thread_id,
            "session_id": event.session_id,
            "created_at": event.created_at,
            "currency": "USD",
            "cost_status": "estimated" if cost_usd_nanos is not None else "unavailable",
            "estimated_cost_usd": cost_usd,
            "estimated_cost_usd_nanos": cost_usd_nanos,
            "pricing_model": self._optional_string(usage.get("pricing_model")),
            "pricing_version": self._optional_string(usage.get("pricing_version")),
            "pricing_unit": self._optional_string(usage.get("pricing_unit")),
            "pricing": self._mapping(usage.get("pricing")),
            "billable_tokens": self._mapping(usage.get("billable_tokens")),
            "token_counts": self._mapping(usage.get("token_counts")),
            "usage": usage,
        }

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _estimated_cost_usd(usage: dict[str, Any]) -> float | None:
        value = usage.get("estimated_cost_usd")
        if isinstance(value, bool) or value is None:
            return None
        try:
            cost = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not cost.is_finite() or cost < 0:
            return None
        return float(cost)

    @staticmethod
    def _cost_usd_nanos(cost_usd: float | None) -> int | None:
        if cost_usd is None:
            return None
        try:
            nanos = (Decimal(str(cost_usd)) * NANOS_PER_USD).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, ValueError):
            return None
        return int(nanos)
