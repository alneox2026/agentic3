"""Reconciliation worker for pending and unresolved Stripe subscription cancellation intents."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from services.billing_api_v3.app.core.config import BillingApiSettings, get_settings
from services.billing_api_v3.app.services.firestore_client import (
    get_transaction_document_snapshot,
)
from services.billing_api_v3.app.services.stripe_gateway import StripeGateway, get_stripe_gateway


@dataclass(frozen=True)
class CancellationReconciliationResult:
    scanned_intents: int
    resolved_intents: int
    completed_cancellations: int
    failed_cancellations: int
    skipped_intents: int


def _run_firestore_transaction(client: Any, operation: Callable[[Any], Any]) -> Any:
    transaction = client.transaction()
    return operation(transaction)


def _default_firestore_client(project_id: str) -> Any:
    from google.cloud import firestore

    return firestore.Client(project=project_id)


def _sort_key_next_attempt_at(snapshot: Any) -> datetime:
    data = snapshot.to_dict() or {}
    val = data.get("next_attempt_at")
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


class CancellationReconciliationService:
    """Scans and resolves pending and unresolved subscription cancellation intents.

    Handles scenarios where Stripe webhook retries were exhausted, webhook delivery
    was out of order, or transient network timeouts interrupted local completion.
    Uses lease claims, lease-owner fencing tokens, and exponential backoff on next_attempt_at
    to prevent worker races and head-of-line starvation.
    """

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        stripe_gateway: StripeGateway | None = None,
        settings: BillingApiSettings | None = None,
        transaction_runner: Callable[[Any, Callable[[Any], Any]], Any] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        lease_seconds: int = 180,
    ) -> None:
        self._settings = settings or get_settings()
        self._firestore_client_factory = firestore_client_factory or (
            lambda: _default_firestore_client(self._settings.project_id)
        )
        self._stripe_gateway = stripe_gateway or get_stripe_gateway()
        self._transaction_runner = transaction_runner or _run_firestore_transaction
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lease_seconds = max(10, lease_seconds)

    async def reconcile_intents(self, *, batch_size: int = 50) -> CancellationReconciliationResult:
        return await asyncio.to_thread(self.reconcile_intents_sync, batch_size=batch_size)

    def reconcile_intents_sync(self, *, batch_size: int = 50) -> CancellationReconciliationResult:
        client = self._firestore_client_factory()
        now_ts = self._now_factory()
        self._backfill_missing_next_attempt_at(client, now_ts)
        intents = self._pending_cancellation_intents(client, batch_size)

        scanned = 0
        resolved = 0
        completed = 0
        failed = 0
        skipped = 0

        account_collection = self._settings.billing_accounts_collection
        cancel_collection = getattr(
            self._settings,
            "subscription_cancellation_requests_collection",
            "subscription_cancellation_requests_v3",
        )

        for intent_snapshot in intents:
            current_now = self._now_factory()
            scanned += 1
            intent_data = intent_snapshot.to_dict() or {}
            intent_id = (
                getattr(intent_snapshot, "id", None)
                or getattr(intent_snapshot, "document_id", None)
                or intent_data.get("cancellation_request_id")
            )
            if not intent_id:
                skipped += 1
                continue

            status = intent_data.get("status")
            billing_account_id = intent_data.get("billing_account_id")
            stripe_sub_id = intent_data.get("stripe_subscription_id")

            if status not in ("unresolved", "pending"):
                skipped += 1
                continue

            # Check existing lease or backoff delay before attempting claim
            leased_until = intent_data.get("leased_until")
            if leased_until and leased_until > current_now:
                skipped += 1
                continue

            next_attempt_at = intent_data.get("next_attempt_at")
            if next_attempt_at and next_attempt_at > current_now:
                skipped += 1
                continue

            cancel_ref = client.collection(cancel_collection).document(intent_id)
            acc_ref = (
                client.collection(account_collection).document(billing_account_id)
                if billing_account_id
                else None
            )

            # Atomically claim lease on this cancellation request to avoid concurrent execution
            lease_expiry = current_now + timedelta(seconds=self._lease_seconds)
            lease_token = uuid.uuid4().hex

            def claim_lease_op(transaction: Any) -> bool:
                snap = get_transaction_document_snapshot(transaction, cancel_ref)
                if not snap.exists:
                    return False
                current_data = snap.to_dict() or {}
                if current_data.get("status") not in ("unresolved", "pending"):
                    return False
                active_lease = current_data.get("leased_until")
                if active_lease and active_lease > current_now:
                    return False
                active_next = current_data.get("next_attempt_at")
                if active_next and active_next > current_now:
                    return False
                transaction.update(
                    cancel_ref,
                    {
                        "leased_until": lease_expiry,
                        "lease_owner_token": lease_token,
                        "updated_at": current_now,
                    },
                )
                return True

            claimed = False
            with suppress(Exception):
                claimed = self._transaction_runner(client, claim_lease_op)

            if not claimed:
                skipped += 1
                continue

            # Case 1: Unresolved intent missing stripe_subscription_id
            if status == "unresolved" and not stripe_sub_id:
                if acc_ref:
                    acc_snap = acc_ref.get()
                    acc_data = acc_snap.to_dict() or {} if acc_snap.exists else {}
                    stripe_sub_id = acc_data.get("stripe_subscription_id")

                if stripe_sub_id:
                    def bind_sub_op(transaction: Any) -> None:
                        transaction.update(
                            cancel_ref,
                            {
                                "stripe_subscription_id": stripe_sub_id,
                                "status": "pending",
                                "updated_at": current_now,
                            },
                        )

                    self._transaction_runner(client, bind_sub_op)
                    resolved += 1
                    status = "pending"
                else:
                    # Not yet available; release lease and back off briefly (60s)
                    def release_unresolved_op(transaction: Any) -> None:
                        snap = get_transaction_document_snapshot(transaction, cancel_ref)
                        if not snap.exists:
                            return
                        current_data = snap.to_dict() or {}
                        if current_data.get("lease_owner_token") != lease_token:
                            return
                        transaction.update(
                            cancel_ref,
                            {
                                "leased_until": None,
                                "lease_owner_token": None,
                                "next_attempt_at": current_now + timedelta(seconds=60),
                                "updated_at": current_now,
                            },
                        )

                    with suppress(Exception):
                        self._transaction_runner(client, release_unresolved_op)
                    skipped += 1
                    continue

            # Case 2: Pending intent ready for Stripe cancellation
            if status == "pending" and stripe_sub_id:
                try:
                    self._stripe_gateway.cancel_subscription(stripe_sub_id)
                except Exception as exc:
                    failed += 1
                    attempts = int(intent_data.get("attempts", 0)) + 1
                    backoff_seconds = min(3600, 30 * (2 ** min(attempts - 1, 6)))
                    next_attempt = current_now + timedelta(seconds=backoff_seconds)

                    def record_failure_op(transaction: Any) -> None:
                        snap = get_transaction_document_snapshot(transaction, cancel_ref)
                        if not snap.exists:
                            return
                        current_data = snap.to_dict() or {}
                        if current_data.get("lease_owner_token") != lease_token:
                            return
                        transaction.update(
                            cancel_ref,
                            {
                                "status": "pending",
                                "attempts": attempts,
                                "last_error": str(exc),
                                "next_attempt_at": next_attempt,
                                "leased_until": None,
                                "lease_owner_token": None,
                                "updated_at": current_now,
                            },
                        )

                    with suppress(Exception):
                        self._transaction_runner(client, record_failure_op)
                    continue

                def finalize_op(transaction: Any) -> bool:
                    snap = get_transaction_document_snapshot(transaction, cancel_ref)
                    if not snap.exists:
                        return False
                    current_data = snap.to_dict() or {}
                    if current_data.get("lease_owner_token") != lease_token:
                        return False
                    transaction.update(
                        cancel_ref,
                        {
                            "status": "completed",
                            "completed_at": current_now,
                            "leased_until": None,
                            "lease_owner_token": None,
                            "updated_at": current_now,
                        },
                    )
                    if acc_ref:
                        transaction.update(
                            acc_ref,
                            {
                                "subscription_status": "canceled",
                                "stripe_subscription_status": "canceled",
                                "subscription_canceled_at": current_now,
                                "subscription_cancellation_pending": False,
                                "unresolved_cancellation_request_id": None,
                                "updated_at": current_now,
                            },
                        )
                    return True

                finalized = False
                with suppress(Exception):
                    finalized = self._transaction_runner(client, finalize_op)
                if finalized:
                    completed += 1

        return CancellationReconciliationResult(
            scanned_intents=scanned,
            resolved_intents=resolved,
            completed_cancellations=completed,
            failed_cancellations=failed,
            skipped_intents=skipped,
        )

    def _backfill_missing_next_attempt_at(self, client: Any, now_ts: datetime) -> int:
        """Populate missing next_attempt_at on legacy records so order_by query includes them."""
        collection_name = getattr(
            self._settings,
            "subscription_cancellation_requests_collection",
            "subscription_cancellation_requests_v3",
        )
        coll = client.collection(collection_name)
        backfilled = 0
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            # Scan unresolved/pending documents WITHOUT order_by to discover legacy docs lacking the field
            docs = list(
                coll.where(filter=FieldFilter("status", "in", ["unresolved", "pending"]))
                .limit(100)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            docs = []
            if hasattr(coll, "stream"):
                docs = [
                    s
                    for s in coll.stream()
                    if (s.to_dict() or {}).get("status") in ("unresolved", "pending")
                ][:100]

        for doc in docs:
            data = doc.to_dict() or {}
            if "next_attempt_at" not in data or data.get("next_attempt_at") is None:
                doc_id = (
                    getattr(doc, "id", None)
                    or getattr(doc, "document_id", None)
                    or data.get("cancellation_request_id")
                )
                if not doc_id:
                    continue
                target_time = data.get("created_at") or now_ts
                doc_ref = coll.document(doc_id)
                with suppress(Exception):
                    doc_ref.update({"next_attempt_at": target_time, "updated_at": now_ts})
                    backfilled += 1

        return backfilled

    def _pending_cancellation_intents(self, client: Any, limit: int) -> list[Any]:
        collection_name = getattr(
            self._settings,
            "subscription_cancellation_requests_collection",
            "subscription_cancellation_requests_v3",
        )
        coll = client.collection(collection_name)
        # Fetch candidate batch ordered by next_attempt_at to prevent delayed records from starving ready work
        fetch_limit = max(10, limit * 2)
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return list(
                coll.where(filter=FieldFilter("status", "in", ["unresolved", "pending"]))
                .order_by("next_attempt_at")
                .limit(fetch_limit)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            # Fallback for test fakes lacking chained query methods or when firestore is mocked
            if hasattr(coll, "stream"):
                candidates = [
                    s
                    for s in coll.stream()
                    if (s.to_dict() or {}).get("status") in ("unresolved", "pending")
                ]
                candidates.sort(key=_sort_key_next_attempt_at)
                return candidates[:fetch_limit]
            return []

