"""Server-side prepaid-credit reservations made before agent execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from common.billing import customer_wallet_document_id, nonnegative_int
from common.firestore import get_transaction_document_snapshot
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.firestore_client import get_firestore_client


@dataclass(frozen=True)
class WalletReservation:
    """The immutable reservation identity propagated with one agent turn."""

    reservation_id: str
    billing_subject_id: str
    user_id: str
    agent_id: str
    request_id: str
    reserved_amount_nanos: int
    currency: str
    expires_at: datetime

    def event_metadata(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "billing_subject_id": self.billing_subject_id,
            "reserved_amount_nanos": self.reserved_amount_nanos,
            "billing_currency": self.currency,
        }


TransactionRunner = Callable[[Any, Callable[[Any], Any]], Any]


class WalletReservationService:
    """Atomically reserve wallet credit before an upstream model invocation."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        transaction_runner: TransactionRunner | None = None,
    ) -> None:
        self._settings = get_settings()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._transaction_runner = transaction_runner or _run_firestore_transaction

    async def reserve(
        self,
        *,
        user_id: str,
        agent_id: str,
        request_id: str,
        turn_id: str,
        reservation_nanos: int | None = None,
    ) -> WalletReservation | None:
        """Reserve credit, or return ``None`` while enforcement is disabled."""

        if not self._settings.billing_enforcement_enabled:
            return None
        return await asyncio.to_thread(
            self._reserve_sync,
            user_id=user_id,
            agent_id=agent_id,
            request_id=request_id,
            turn_id=turn_id,
            reservation_nanos=reservation_nanos,
        )

    def _reserve_sync(
        self,
        *,
        user_id: str,
        agent_id: str,
        request_id: str,
        turn_id: str,
        reservation_nanos: int | None = None,
    ) -> WalletReservation:
        billing_subject_id = user_id
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._settings.billing_reservation_ttl_seconds)
        reserved_amount_nanos = (
            reservation_nanos
            if reservation_nanos is not None and reservation_nanos > 0
            else self._settings.billing_reservation_nanos
        )
        if reserved_amount_nanos <= 0:
            raise RuntimeError("BILLING_RESERVATION_NANOS must be greater than zero.")
        if self._settings.billing_reservation_ttl_seconds <= 0:
            raise RuntimeError("BILLING_RESERVATION_TTL_SECONDS must be greater than zero.")

        client = self._firestore_client_factory()
        wallet_ref = client.collection(self._settings.wallets_collection).document(
            customer_wallet_document_id(billing_subject_id)
        )
        reservation_ref = client.collection(
            self._settings.billing_reservations_collection
        ).document(turn_id)

        def operation(transaction: Any) -> WalletReservation:
            reservation_snapshot = get_transaction_document_snapshot(transaction, reservation_ref)
            if reservation_snapshot.exists:
                return self._existing_reservation(
                    reservation_snapshot.to_dict() or {},
                    expected_turn_id=turn_id,
                    expected_subject_id=billing_subject_id,
                    expected_user_id=user_id,
                    expected_agent_id=agent_id,
                )

            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)
            if not wallet_snapshot.exists:
                raise ApiError(
                    402,
                    "wallet_not_ready",
                    "A prepaid usage balance is required before using this agent.",
                )
            wallet = wallet_snapshot.to_dict() or {}
            if wallet.get("owner_uid") != user_id or wallet.get("billing_subject_id") != billing_subject_id:
                raise ApiError(
                    403,
                    "wallet_ownership_mismatch",
                    "The prepaid usage balance is not available for this account.",
                )
            if wallet.get("status") != "active":
                raise ApiError(
                    403,
                    "wallet_inactive",
                    "The prepaid usage balance is not active.",
                )

            try:
                available_credit_nanos = int(wallet.get("available_credit_nanos", 0))
                reserved_credit_nanos = nonnegative_int(
                    wallet.get("reserved_credit_nanos", 0),
                    field_name="reserved_credit_nanos",
                )
            except ValueError as exc:
                raise ApiError(
                    503,
                    "wallet_invalid",
                    "The prepaid usage balance is temporarily unavailable.",
                ) from exc

            if available_credit_nanos < reserved_amount_nanos:
                raise ApiError(
                    402,
                    "insufficient_credit",
                    "Your prepaid usage balance is too low to start this agent request.",
                    {
                        "currency": "USD",
                        "required_reservation_nanos": reserved_amount_nanos,
                        "available_credit_nanos": available_credit_nanos,
                    },
                )

            reservation = WalletReservation(
                reservation_id=turn_id,
                billing_subject_id=billing_subject_id,
                user_id=user_id,
                agent_id=agent_id,
                request_id=request_id,
                reserved_amount_nanos=reserved_amount_nanos,
                currency="USD",
                expires_at=expires_at,
            )
            transaction.create(
                reservation_ref,
                {
                    "schema_version": 1,
                    "reservation_id": reservation.reservation_id,
                    "turn_id": turn_id,
                    "request_id": request_id,
                    "billing_subject_id": billing_subject_id,
                    "owner_uid": user_id,
                    "agent_id": agent_id,
                    "currency": reservation.currency,
                    "reserved_amount_nanos": reserved_amount_nanos,
                    "status": "reserved",
                    "created_at": now,
                    "expires_at": expires_at,
                    "settled_amount_nanos": None,
                    "released_amount_nanos": None,
                },
            )
            transaction.update(
                wallet_ref,
                {
                    "available_credit_nanos": available_credit_nanos - reserved_amount_nanos,
                    "reserved_credit_nanos": reserved_credit_nanos + reserved_amount_nanos,
                    "updated_at": now,
                    "last_reservation_at": now,
                },
            )
            return reservation

        return self._transaction_runner(client, operation)

    async def release(
        self,
        reservation: WalletReservation | None,
    ) -> None:
        """Release an uncommitted reservation back to available credit immediately."""
        if reservation is None or not self._settings.billing_enforcement_enabled:
            return
        await asyncio.to_thread(
            self._release_sync,
            reservation=reservation,
        )

    def _release_sync(
        self,
        *,
        reservation: WalletReservation,
    ) -> None:
        client = self._firestore_client_factory()
        now = datetime.now(timezone.utc)
        wallet_ref = client.collection(self._settings.wallets_collection).document(
            customer_wallet_document_id(reservation.billing_subject_id)
        )
        reservation_ref = client.collection(
            self._settings.billing_reservations_collection
        ).document(reservation.reservation_id)

        def operation(transaction: Any) -> None:
            reservation_snapshot = get_transaction_document_snapshot(transaction, reservation_ref)
            if not reservation_snapshot.exists:
                return
            res_dict = reservation_snapshot.to_dict() or {}
            if res_dict.get("status") != "reserved":
                return

            reserved_amount = nonnegative_int(
                res_dict.get("reserved_amount_nanos", reservation.reserved_amount_nanos),
                field_name="reserved_amount_nanos",
            )

            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)
            if wallet_snapshot.exists:
                wallet = wallet_snapshot.to_dict() or {}
                available = int(wallet.get("available_credit_nanos", 0))
                reserved = nonnegative_int(
                    wallet.get("reserved_credit_nanos", 0),
                    field_name="reserved_credit_nanos",
                )
                new_reserved = max(0, reserved - reserved_amount)
                new_available = available + reserved_amount

                transaction.update(
                    wallet_ref,
                    {
                        "available_credit_nanos": new_available,
                        "reserved_credit_nanos": new_reserved,
                        "updated_at": now,
                    },
                )

            transaction.update(
                reservation_ref,
                {
                    "status": "released",
                    "released_amount_nanos": reserved_amount,
                    "updated_at": now,
                    "released_at": now,
                },
            )

        self._transaction_runner(client, operation)

    @staticmethod
    def _existing_reservation(
        reservation: dict[str, Any],
        *,
        expected_turn_id: str,
        expected_subject_id: str,
        expected_user_id: str,
        expected_agent_id: str,
    ) -> WalletReservation:
        status = reservation.get("status")
        if status == "reserved":
            raise ApiError(
                409,
                "turn_in_progress",
                "The turn is currently in progress.",
                {"turn_id": expected_turn_id},
            )
        if status == "settled":
            raise ApiError(
                409,
                "turn_already_settled",
                "The turn has already completed.",
                {"turn_id": expected_turn_id},
            )
        raise ApiError(
            409,
            "billing_reservation_conflict",
            "The request already has a conflicting billing reservation.",
            {"turn_id": expected_turn_id, "status": status},
        )


def _run_firestore_transaction(
    client: Any,
    operation: Callable[[Any], Any],
) -> Any:
    """Run a Firestore transaction with retry-on-contention semantics."""

    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> Any:
        return operation(transaction)

    return run(transaction)


async def get_wallet_reservation_service() -> WalletReservationService:
    return WalletReservationService()
