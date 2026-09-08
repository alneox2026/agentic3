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


TransactionRunner = Callable[[Any, Callable[[Any], WalletReservation]], WalletReservation]


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
        )

    def _reserve_sync(
        self,
        *,
        user_id: str,
        agent_id: str,
        request_id: str,
        turn_id: str,
    ) -> WalletReservation:
        billing_subject_id = user_id
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._settings.billing_reservation_ttl_seconds)
        reserved_amount_nanos = self._settings.billing_reservation_nanos
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
                available_credit_nanos = nonnegative_int(
                    wallet.get("available_credit_nanos"),
                    field_name="available_credit_nanos",
                )
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

    @staticmethod
    def _existing_reservation(
        reservation: dict[str, Any],
        *,
        expected_turn_id: str,
        expected_subject_id: str,
        expected_user_id: str,
        expected_agent_id: str,
    ) -> WalletReservation:
        if (
            reservation.get("reservation_id") != expected_turn_id
            or reservation.get("turn_id") != expected_turn_id
            or reservation.get("billing_subject_id") != expected_subject_id
            or reservation.get("owner_uid") != expected_user_id
            or reservation.get("agent_id") != expected_agent_id
            or reservation.get("status") != "reserved"
        ):
            raise ApiError(
                409,
                "billing_reservation_conflict",
                "The request already has a conflicting billing reservation.",
            )
        try:
            reserved_amount_nanos = nonnegative_int(
                reservation.get("reserved_amount_nanos"),
                field_name="reserved_amount_nanos",
            )
        except ValueError as exc:
            raise ApiError(
                503,
                "billing_reservation_invalid",
                "The request billing reservation is temporarily unavailable.",
            ) from exc
        expires_at = reservation.get("expires_at")
        if not isinstance(expires_at, datetime):
            raise ApiError(
                503,
                "billing_reservation_invalid",
                "The request billing reservation is temporarily unavailable.",
            )
        return WalletReservation(
            reservation_id=expected_turn_id,
            billing_subject_id=expected_subject_id,
            user_id=expected_user_id,
            agent_id=expected_agent_id,
            request_id=str(reservation.get("request_id", "")),
            reserved_amount_nanos=reserved_amount_nanos,
            currency=str(reservation.get("currency", "USD")),
            expires_at=expires_at,
        )


def _run_firestore_transaction(
    client: Any,
    operation: Callable[[Any], WalletReservation],
) -> WalletReservation:
    """Run a Firestore transaction with retry-on-contention semantics."""

    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> WalletReservation:
        return operation(transaction)

    return run(transaction)


async def get_wallet_reservation_service() -> WalletReservationService:
    return WalletReservationService()
