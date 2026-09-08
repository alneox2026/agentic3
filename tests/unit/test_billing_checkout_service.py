import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.billing import customer_billing_account_document_id
from services.billing_api_v3.app.core.config import BillingApiSettings
from services.billing_api_v3.app.core.errors import BillingApiError
from services.billing_api_v3.app.services.billing_catalog import load_billing_catalog
from services.billing_api_v3.app.services.checkout_service import CheckoutService


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client, collection, document_id):
        self.client = client
        self.collection = collection
        self.document_id = document_id

    @property
    def key(self):
        return self.collection, self.document_id


class FakeCollectionReference:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection

    def document(self, document_id):
        return FakeDocumentReference(self.client, self.collection_name, document_id)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def get(self, document_ref):
        yield FakeSnapshot(self.client.documents.get(document_ref.key))

    def create(self, document_ref, data):
        if document_ref.key in self.client.documents:
            raise RuntimeError("duplicate create")
        self.client.documents[document_ref.key] = dict(data)

    def update(self, document_ref, updates):
        self.client.documents[document_ref.key].update(updates)


class FakeFirestore:
    def __init__(self):
        self.documents = {}

    def collection(self, collection):
        return FakeCollectionReference(self, collection)

    def transaction(self):
        return FakeTransaction(self)


class FakeStripeGateway:
    def __init__(self):
        self.customer_requests = []
        self.checkout_requests = []

    def create_customer(self, *, metadata, idempotency_key):
        self.customer_requests.append((metadata, idempotency_key))
        return {"id": "cus_test_123"}

    def create_checkout_session(self, *, params, idempotency_key):
        self.checkout_requests.append((params, idempotency_key))
        return {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}


def _run_transaction(client, operation):
    return operation(client.transaction())


def _settings() -> BillingApiSettings:
    return BillingApiSettings(
        project_id="ceo-dev123",
        region="us-central1",
        log_level="INFO",
        allowed_origins=["https://ceoappdev.flutterflow.app"],
        catalog_path=Path("config/billing.test.yaml"),
        billing_accounts_collection="customer_billing_accounts",
        stripe_webhook_events_collection="stripe_webhook_events",
        wallets_collection="customer_wallets",
        wallet_transactions_collection="wallet_transactions",
        customer_billing_periods_collection="customer_billing_periods",
        checkout_success_url="https://ceoappdev.flutterflow.app/billing-complete?session_id={CHECKOUT_SESSION_ID}",
        checkout_cancel_url="https://ceoappdev.flutterflow.app/billing-cancelled",
        checkout_session_ttl_seconds=1800,
        stripe_webhook_tolerance_seconds=300,
    )


def _service(client, stripe):
    return CheckoutService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        catalog=load_billing_catalog(Path("config/billing.test.yaml")),
        transaction_runner=_run_transaction,
        now_factory=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )


def test_first_topup_uses_one_mixed_subscription_checkout_and_reuses_it():
    client = FakeFirestore()
    stripe = FakeStripeGateway()
    service = _service(client, stripe)

    result = asyncio.run(
        service.create_topup_checkout(owner_uid="user-1", topup_package_id="credit_5_usd")
    )
    same_result = asyncio.run(
        service.create_topup_checkout(owner_uid="user-1", topup_package_id="credit_5_usd")
    )

    assert result == same_result
    assert result.starts_subscription is True
    assert len(stripe.customer_requests) == 1
    assert len(stripe.checkout_requests) == 1
    params, _ = stripe.checkout_requests[0]
    assert params["mode"] == "subscription"
    assert params["line_items"] == [
        {"price": "price_1U3ZHnB5Es3VU3maoEQbMKnC", "quantity": 1},
        {"price": "price_1U3ZYBB5Es3VU3maSP6qq6sg", "quantity": 1},
    ]
    assert params["subscription_data"]["metadata"]["checkout_kind"] == "initial_subscription_topup"


def test_active_checkout_switches_package_smoothly():
    client = FakeFirestore()
    stripe = FakeStripeGateway()
    service = _service(client, stripe)
    result_5 = asyncio.run(service.create_topup_checkout(owner_uid="user-1", topup_package_id="credit_5_usd"))
    assert result_5.topup_package_id == "credit_5_usd"

    result_25 = asyncio.run(service.create_topup_checkout(owner_uid="user-1", topup_package_id="credit_25_usd"))
    assert result_25.topup_package_id == "credit_25_usd"
    assert len(stripe.checkout_requests) == 2
    assert stripe.checkout_requests[1][0]["line_items"][0]["price"] == "price_1U3ZLMB5Es3VU3maaNqqR0p9"



def test_later_topup_is_payment_only_after_monthly_subscription_is_active():
    client = FakeFirestore()
    stripe = FakeStripeGateway()
    service = _service(client, stripe)
    account_id = customer_billing_account_document_id("user-1")
    client.documents[("customer_billing_accounts", account_id)] = {
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "catalog_environment": "test",
        "stripe_customer_id": "cus_test_123",
        "stripe_subscription_id": "sub_test_123",
        "stripe_subscription_status": "active",
        "active_checkout_request_id": None,
        "active_checkout_session_id": None,
        "active_checkout_url": None,
        "active_checkout_mode": None,
        "active_checkout_topup_package_id": None,
        "active_checkout_created_at": None,
        "active_checkout_expires_at": None,
    }

    result = asyncio.run(
        service.create_topup_checkout(owner_uid="user-1", topup_package_id="credit_10_usd")
    )

    assert result.starts_subscription is False
    params, _ = stripe.checkout_requests[0]
    assert params["mode"] == "payment"
    assert params["line_items"] == [
        {"price": "price_1U3ZKOB5Es3VU3maflfGkdrX", "quantity": 1}
    ]
    assert params["payment_intent_data"]["metadata"]["checkout_kind"] == "topup"
