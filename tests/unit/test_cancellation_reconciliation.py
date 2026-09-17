"""Unit tests for the subscription cancellation intent reconciliation service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from common.billing import customer_billing_account_document_id
from services.billing_api_v3.app.core.config import BillingApiSettings
from services.billing_api_v3.app.services.cancellation_reconciliation import (
    CancellationReconciliationService,
)
from services.billing_api_v3.app.services.firestore_records import (
    build_initial_billing_account_document,
)
from services.billing_api_v3.app.services.stripe_gateway import StripeGatewayError


class FakeSnapshot:
    def __init__(self, data: Any, doc_id: str | None = None) -> None:
        self._data = data
        self.id = doc_id
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client: Any, collection: str, document_id: str) -> None:
        self.client = client
        self.collection = collection
        self.document_id = document_id

    @property
    def key(self) -> tuple[str, str]:
        return self.collection, self.document_id

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self.client.documents.get(self.key), self.document_id)

    def update(self, updates: dict[str, Any]) -> None:
        if self.key not in self.client.documents:
            self.client.documents[self.key] = {}
        self.client.documents[self.key].update(updates)


class FakeCollectionReference:
    def __init__(self, client: Any, collection: str) -> None:
        self.client = client
        self.collection_name = collection

    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self.client, self.collection_name, document_id)

    def stream(self) -> list[FakeSnapshot]:
        snapshots = []
        for (col, doc_id), data in self.client.documents.items():
            if col == self.collection_name:
                snapshots.append(FakeSnapshot(data, doc_id))
        return snapshots


class FakeTransaction:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._wrote = False

    def get(self, document_ref: FakeDocumentReference) -> Any:
        if self._wrote:
            raise AssertionError("Firestore transaction read occurred after a write")
        yield FakeSnapshot(self.client.documents.get(document_ref.key), document_ref.document_id)

    def create(self, document_ref: FakeDocumentReference, data: dict[str, Any]) -> None:
        self._wrote = True
        self.client.documents[document_ref.key] = dict(data)

    def update(self, document_ref: FakeDocumentReference, updates: dict[str, Any]) -> None:
        self._wrote = True
        if document_ref.key not in self.client.documents:
            self.client.documents[document_ref.key] = {}
        self.client.documents[document_ref.key].update(updates)


class FakeFirestore:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}

    def collection(self, collection: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, collection)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeStripeGateway:
    def __init__(self) -> None:
        self.cancelled_subscriptions: list[str] = []
        self.cancel_error: Exception | None = None

    def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled_subscriptions.append(subscription_id)
        return {"id": subscription_id, "status": "canceled"}


def _settings() -> BillingApiSettings:
    return BillingApiSettings(
        project_id="ceo-dev123",
        region="us-central1",
        log_level="INFO",
        allowed_origins=[],
        catalog_path=Path("config/billing.test.yaml"),
        billing_accounts_collection="customer_billing_accounts",
        stripe_webhook_events_collection="stripe_webhook_events",
        wallets_collection="customer_wallets",
        wallet_transactions_collection="wallet_transactions",
        customer_billing_periods_collection="customer_billing_periods",
        checkout_success_url="https://example.test/success",
        checkout_cancel_url="https://example.test/cancelled",
        checkout_session_ttl_seconds=1800,
        stripe_webhook_tolerance_seconds=300,
        subscription_cancellation_requests_collection="subscription_cancellation_requests",
    )


def test_reconcile_resolves_unresolved_intent_when_account_has_subscription_id() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_arrived_later",
        "subscription_cancellation_pending": True,
        "unresolved_cancellation_request_id": "re_unresolved_1",
    }
    client.documents[("subscription_cancellation_requests", "re_unresolved_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_unresolved_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": None,
        "status": "unresolved",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    assert result.scanned_intents == 1
    assert result.resolved_intents == 1
    assert result.completed_cancellations == 1
    assert result.failed_cancellations == 0
    assert result.skipped_intents == 0

    assert "sub_arrived_later" in stripe.cancelled_subscriptions
    intent_doc = client.documents[("subscription_cancellation_requests", "re_unresolved_1")]
    assert intent_doc["status"] == "completed"
    assert intent_doc["stripe_subscription_id"] == "sub_arrived_later"

    account_doc = client.documents[("customer_billing_accounts", account_id)]
    assert account_doc["subscription_status"] == "canceled"
    assert account_doc["stripe_subscription_status"] == "canceled"
    assert account_doc["subscription_cancellation_pending"] is False
    assert account_doc["unresolved_cancellation_request_id"] is None


def test_reconcile_completes_pending_intent() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_pending_1",
        "subscription_cancellation_pending": True,
    }
    client.documents[("subscription_cancellation_requests", "re_pending_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_pending_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_pending_1",
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    assert result.scanned_intents == 1
    assert result.completed_cancellations == 1
    assert "sub_pending_1" in stripe.cancelled_subscriptions
    intent_doc = client.documents[("subscription_cancellation_requests", "re_pending_1")]
    assert intent_doc["status"] == "completed"


def test_reconcile_skips_unresolved_when_account_still_missing_subscription_id() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": None,
    }
    client.documents[("subscription_cancellation_requests", "re_unresolved_missing")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_unresolved_missing",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": None,
        "status": "unresolved",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    assert result.scanned_intents == 1
    assert result.skipped_intents == 1
    assert result.completed_cancellations == 0
    intent_doc = client.documents[("subscription_cancellation_requests", "re_unresolved_missing")]
    assert intent_doc["status"] == "unresolved"


def test_reconcile_records_failure_and_increments_attempts_on_stripe_error() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_fail_1",
    }
    client.documents[("subscription_cancellation_requests", "re_fail_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_fail_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_fail_1",
        "status": "pending",
        "attempts": 1,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    stripe.cancel_error = StripeGatewayError("Stripe API connection timed out")
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    assert result.scanned_intents == 1
    assert result.failed_cancellations == 1
    assert result.completed_cancellations == 0

    intent_doc = client.documents[("subscription_cancellation_requests", "re_fail_1")]
    assert intent_doc["status"] == "pending"
    assert intent_doc["attempts"] == 2
    assert "Stripe API connection timed out" in intent_doc["last_error"]
    assert intent_doc["next_attempt_at"] > now
    assert intent_doc["leased_until"] is None


def test_reconcile_skips_intent_with_active_lease() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_leased_1",
    }
    # Intent actively leased by another worker until now + 30s
    client.documents[("subscription_cancellation_requests", "re_leased_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_leased_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_leased_1",
        "status": "pending",
        "leased_until": now + timedelta(seconds=30),
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()
    assert result.scanned_intents == 1
    assert result.skipped_intents == 1
    assert result.completed_cancellations == 0
    assert stripe.cancelled_subscriptions == []


def test_reconcile_skips_intent_with_future_next_attempt_at() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_backoff_1",
    }
    # Intent backed off until now + 120s
    client.documents[("subscription_cancellation_requests", "re_backoff_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_backoff_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_backoff_1",
        "status": "pending",
        "next_attempt_at": now + timedelta(seconds=120),
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()
    assert result.scanned_intents == 1
    assert result.skipped_intents == 1
    assert result.completed_cancellations == 0
    assert stripe.cancelled_subscriptions == []


def test_reconcile_prioritizes_ready_intents_over_backed_off_backlog() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_ready_1",
        "subscription_cancellation_pending": True,
    }

    # Populate 25 backed-off intents with future next_attempt_at (would exceed batch_size=10)
    for i in range(25):
        doc_id = f"re_delayed_{i:02d}"
        client.documents[("subscription_cancellation_requests", doc_id)] = {
            "schema_version": 1,
            "cancellation_request_id": doc_id,
            "billing_account_id": account_id,
            "billing_subject_id": "user-1",
            "owner_uid": "user-1",
            "stripe_subscription_id": "sub_delayed",
            "status": "pending",
            "next_attempt_at": now + timedelta(hours=1),
            "created_at": now - timedelta(hours=2),
            "updated_at": now - timedelta(hours=2),
        }

    # Populate 1 ready intent created later but with next_attempt_at in the past
    client.documents[("subscription_cancellation_requests", "re_ready_target")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_ready_target",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_ready_1",
        "status": "pending",
        "next_attempt_at": now - timedelta(seconds=10),
        "created_at": now - timedelta(seconds=10),
        "updated_at": now - timedelta(seconds=10),
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync(batch_size=10)

    # The ready intent should be processed and completed, not starved by the 25 delayed intents
    assert result.completed_cancellations == 1
    assert "sub_ready_1" in stripe.cancelled_subscriptions
    ready_doc = client.documents[("subscription_cancellation_requests", "re_ready_target")]
    assert ready_doc["status"] == "completed"


def test_reconcile_fencing_token_prevents_stale_worker_finalization() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_stale_1",
        "subscription_cancellation_pending": True,
    }
    client.documents[("subscription_cancellation_requests", "re_stale_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_stale_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_stale_1",
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()

    # Intercept transaction runner: after claim_lease_op runs, simulate another worker
    # acquiring the lease by changing the lease_owner_token in Firestore before finalization
    original_runner = service = None

    def intercepted_runner(firestore_client: Any, operation: Callable[[Any], Any]) -> Any:
        res = operation(firestore_client.transaction())
        # If this was claim_lease_op, tamper with the lease_owner_token
        doc = client.documents.get(("subscription_cancellation_requests", "re_stale_1"))
        if doc and doc.get("lease_owner_token") and doc.get("status") == "pending":
            doc["lease_owner_token"] = "different_worker_token_abc"
        return res

    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        transaction_runner=intercepted_runner,
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    # Worker calls stripe, but finalization is rejected because lease_owner_token mismatched
    assert "sub_stale_1" in stripe.cancelled_subscriptions
    assert result.completed_cancellations == 0
    doc = client.documents[("subscription_cancellation_requests", "re_stale_1")]
    assert doc["status"] == "pending"
    assert doc["lease_owner_token"] == "different_worker_token_abc"


def test_reconcile_backfills_legacy_intents_missing_next_attempt_at() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_legacy_1",
        "subscription_cancellation_pending": True,
    }
    # Legacy intent missing next_attempt_at entirely
    client.documents[("subscription_cancellation_requests", "re_legacy_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_legacy_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_legacy_1",
        "status": "pending",
        "attempts": 0,
        "created_at": now - timedelta(minutes=5),
        "updated_at": now - timedelta(minutes=5),
    }

    stripe = FakeStripeGateway()
    service = CancellationReconciliationService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
    )

    result = service.reconcile_intents_sync()

    assert result.completed_cancellations == 1
    assert "sub_legacy_1" in stripe.cancelled_subscriptions
    doc = client.documents[("subscription_cancellation_requests", "re_legacy_1")]
    assert doc["status"] == "completed"
    assert doc.get("next_attempt_at") == now - timedelta(minutes=5)


def test_webhook_pending_cancellation_skips_when_reconciler_holds_lease() -> None:
    from services.billing_api_v3.app.services.webhook_service import StripeWebhookService

    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_subscription_id": "sub_race_1",
        "subscription_cancellation_pending": True,
    }
    # Intent already leased by reconciler worker
    client.documents[("subscription_cancellation_requests", "re_race_1")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_race_1",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "stripe_subscription_id": "sub_race_1",
        "status": "pending",
        "leased_until": now + timedelta(seconds=120),
        "lease_owner_token": "reconciler_token_123",
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway()
    cancel_ref = client.collection("subscription_cancellation_requests").document("re_race_1")
    acc_ref = client.collection("customer_billing_accounts").document(account_id)

    webhook_svc = StripeWebhookService(
        stripe_gateway=stripe,
        settings=_settings(),
        now_factory=lambda: now,
        firestore_client_factory=lambda: client,
        transaction_runner=lambda cl, op: op(cl.transaction()),
    )

    webhook_svc._execute_pending_cancellation(
        client,
        {
            "stripe_subscription_id": "sub_race_1",
            "cancellation_ref": cancel_ref,
            "account_ref": acc_ref,
            "refund_id": "re_race_1",
        },
    )

    # Webhook execution should skip because reconciler holds the lease
    assert stripe.cancelled_subscriptions == []
    doc = client.documents[("subscription_cancellation_requests", "re_race_1")]
    assert doc["status"] == "pending"
    assert doc["lease_owner_token"] == "reconciler_token_123"


