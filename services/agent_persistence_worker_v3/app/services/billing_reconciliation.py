"""Reconciliation for reservations whose completed-turn event never arrived."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from common.billing import customer_wallet_document_id, nonnegative_int
from common.firestore import get_transaction_document_snapshot
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)


@dataclass(frozen=True)
class BillingReconciliationResult:
    scanned_reservations: int
    settled_reservations: int
    released_reservations: int
    skipped_reservations: int


class BillingReconciliationService:
    """Settle finalized ledgers, or release genuinely orphaned expired holds."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        settlement_service: BillingSettlementService | None = None,
    ) -> None:
        self._settings = get_settings()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._settlement_service = settlement_service or BillingSettlementService(
            firestore_client_factory=self._firestore_client_factory,
        )

    async def reconcile_expired(self) -> BillingReconciliationResult:
        return await asyncio.to_thread(self.reconcile_expired_sync)

    def reconcile_expired_sync(self) -> BillingReconciliationResult:
        client = self._firestore_client_factory()
        now = datetime.now(timezone.utc)
        reservations = self._expired_reservations(client, now)
        scanned = settled = released = skipped = 0
        for reservation_snapshot in reservations:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            if reservation.get("status") != "reserved":
                skipped += 1
                continue
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            if ledger_snapshot.exists:
                event = _event_from_ledger_and_reservation(
                    ledger_snapshot.to_dict() or {},
                    reservation,
                )
                self._settlement_service.settle_if_required_sync(event)
                settled += 1
                continue
            if self._release_expired_reservation_sync(client, reservation, now):
                released += 1
            else:
                skipped += 1
        return BillingReconciliationResult(
            scanned_reservations=scanned,
            settled_reservations=settled,
            released_reservations=released,
            skipped_reservations=skipped,
        )

    def _expired_reservations(self, client: Any, now: datetime):
        from google.cloud.firestore_v1.base_query import FieldFilter

        return (
            client.collection(self._settings.billing_reservations_collection)
            .where(filter=FieldFilter("expires_at", "<=", now))
            .limit(self._settings.billing_reconciliation_batch_size)
            .stream()
        )

    def _release_expired_reservation_sync(
        self,
        client: Any,
        reservation: dict[str, Any],
        released_at: datetime,
    ) -> bool:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        billing_subject_id = _required_string(
            reservation.get("billing_subject_id"),
            "billing_subject_id",
        )
        owner_uid = _required_string(reservation.get("owner_uid"), "owner_uid")
        reservation_ref = client.collection(
            self._settings.billing_reservations_collection
        ).document(turn_id)
        wallet_ref = client.collection(self._settings.wallets_collection).document(
            customer_wallet_document_id(billing_subject_id)
        )
        transaction_ref = client.collection(
            self._settings.wallet_transactions_collection
        ).document(f"reservation_expired_{turn_id}")

        def operation(transaction: Any) -> bool:
            # Keep all reads before writes for Firestore transaction validity.
            current_reservation_snapshot = get_transaction_document_snapshot(transaction, reservation_ref)
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)
            if transaction_snapshot.exists or not current_reservation_snapshot.exists:
                return False
            current_reservation = current_reservation_snapshot.to_dict() or {}
            if current_reservation.get("status") != "reserved":
                return False
            if not wallet_snapshot.exists:
                raise RetryableWorkerError(
                    f"Wallet is missing for expired reservation {turn_id}."
                )
            wallet = wallet_snapshot.to_dict() or {}
            if (
                wallet.get("status") != "active"
                or wallet.get("billing_subject_id") != billing_subject_id
                or wallet.get("owner_uid") != owner_uid
            ):
                raise RetryableWorkerError(
                    f"Wallet does not match expired reservation {turn_id}."
                )
            try:
                reserved_amount_nanos = nonnegative_int(
                    current_reservation.get("reserved_amount_nanos"),
                    field_name="reserved_amount_nanos",
                )
                available_credit_nanos = nonnegative_int(
                    wallet.get("available_credit_nanos"),
                    field_name="available_credit_nanos",
                )
                reserved_credit_nanos = nonnegative_int(
                    wallet.get("reserved_credit_nanos"),
                    field_name="reserved_credit_nanos",
                )
            except ValueError as exc:
                raise RetryableWorkerError(
                    f"Wallet data is invalid for expired reservation {turn_id}."
                ) from exc
            if reserved_credit_nanos < reserved_amount_nanos:
                raise RetryableWorkerError(
                    f"Wallet reserved credit is invalid for expired reservation {turn_id}."
                )
            transaction.create(
                transaction_ref,
                {
                    "schema_version": 1,
                    "transaction_id": f"reservation_expired_{turn_id}",
                    "transaction_type": "reservation_expiry_release",
                    "status": "released",
                    "billing_subject_id": billing_subject_id,
                    "owner_uid": owner_uid,
                    "wallet_document_id": customer_wallet_document_id(billing_subject_id),
                    "currency": current_reservation.get("currency", "USD"),
                    "turn_id": turn_id,
                    "request_id": current_reservation.get("request_id"),
                    "agent_id": current_reservation.get("agent_id"),
                    "reservation_id": turn_id,
                    "reserved_amount_nanos": reserved_amount_nanos,
                    "amount_nanos": 0,
                    "released_amount_nanos": reserved_amount_nanos,
                    "created_at": released_at,
                    "reason": "completed_turn_not_received_before_reservation_expiry",
                },
            )
            transaction.update(
                wallet_ref,
                {
                    "available_credit_nanos": available_credit_nanos + reserved_amount_nanos,
                    "reserved_credit_nanos": reserved_credit_nanos - reserved_amount_nanos,
                    "updated_at": released_at,
                    "last_reservation_release_at": released_at,
                },
            )
            transaction.update(
                reservation_ref,
                {
                    "status": "expired_released",
                    "released_at": released_at,
                    "released_amount_nanos": reserved_amount_nanos,
                    "release_reason": "completed_turn_not_received_before_reservation_expiry",
                },
            )
            return True

        return _run_firestore_transaction(client, operation)


def _event_from_ledger_and_reservation(
    ledger: dict[str, Any],
    reservation: dict[str, Any],
) -> TurnCompletedEvent:
    return TurnCompletedEvent(
        event_id=_required_string(ledger.get("event_id"), "event_id"),
        turn_id=_required_string(ledger.get("turn_id"), "turn_id"),
        agent_id=_required_string(ledger.get("agent_id"), "agent_id"),
        user_id=_required_string(ledger.get("uid"), "uid"),
        thread_id=_required_string(ledger.get("thread_id"), "thread_id"),
        session_id=_required_string(ledger.get("session_id"), "session_id"),
        user_message="",
        assistant_message="",
        created_at=_required_datetime(ledger.get("created_at"), "created_at"),
        metadata={
            "billing": {
                "reservation_id": reservation.get("reservation_id"),
                "billing_subject_id": reservation.get("billing_subject_id"),
            }
        },
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value.strip()


def _required_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value


def _run_firestore_transaction(client: Any, operation: Callable[[Any], bool]) -> bool:
    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> bool:
        return operation(transaction)

    return run(transaction)
