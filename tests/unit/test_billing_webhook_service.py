from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.billing import (
    customer_billing_account_document_id,
    customer_billing_period_document_id,
    customer_wallet_document_id,
)
from services.billing_api_v3.app.core.config import BillingApiSettings
from services.billing_api_v3.app.core.errors import BillingApiError
from services.billing_api_v3.app.services.billing_catalog import load_billing_catalog
from services.billing_api_v3.app.services.firestore_records import (
    build_initial_billing_account_document,
)
from services.billing_api_v3.app.services.stripe_gateway import StripeGatewayError
from services.billing_api_v3.app.services.webhook_service import StripeWebhookService


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
        self._wrote = False

    def get(self, document_ref):
        if self._wrote:
            raise AssertionError("Firestore transaction read occurred after a write")
        yield FakeSnapshot(self.client.documents.get(document_ref.key))

    def create(self, document_ref, data):
        self._wrote = True
        if document_ref.key in self.client.documents:
            raise RuntimeError("duplicate create")
        self.client.documents[document_ref.key] = dict(data)

    def update(self, document_ref, updates):
        self._wrote = True
        self.client.documents[document_ref.key].update(updates)


class FakeFirestore:
    def __init__(self):
        self.documents = {}

    def collection(self, collection):
        return FakeCollectionReference(self, collection)

    def transaction(self):
        return FakeTransaction(self)


class FakeStripeGateway:
    def __init__(self, events, checkout_sessions, invoices, subscriptions):
        self.events = events
        self.checkout_sessions = checkout_sessions
        self.invoices = invoices
        self.subscriptions = subscriptions

    def construct_webhook_event(self, *, payload, signature, signing_secret, tolerance_seconds):
        if signature != "signature":
            raise StripeGatewayError("invalid")
        return self.events[payload]

    def retrieve_checkout_session(self, checkout_session_id):
        return self.checkout_sessions[checkout_session_id]

    def retrieve_invoice(self, invoice_id):
        return self.invoices[invoice_id]

    def retrieve_subscription(self, subscription_id):
        return self.subscriptions[subscription_id]


def _run_transaction(client, operation):
    return operation(client.transaction())


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
        checkout_success_url="https://example.test/success?session_id={CHECKOUT_SESSION_ID}",
        checkout_cancel_url="https://example.test/cancelled",
        checkout_session_ttl_seconds=1800,
        stripe_webhook_tolerance_seconds=300,
    )


def _subscription(account_id):
    return {
        "id": "sub_test_123",
        "livemode": False,
        "customer": "cus_test_123",
        "status": "active",
        "current_period_start": 1786492800,
        "current_period_end": 1789171200,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "credit_5_usd",
        },
    }


def _seed_account(client, account_id, now):
    client.documents[("customer_billing_accounts", account_id)] = {
        **build_initial_billing_account_document(
            billing_account_id=account_id,
            billing_subject_id="user-1",
            owner_uid="user-1",
            catalog_environment="test",
            created_at=now,
        ),
        "stripe_customer_id": "cus_test_123",
        "stripe_customer_status": "ready",
        "active_checkout_session_id": "cs_test_123",
    }


def _service(client, stripe, now):
    return StripeWebhookService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        catalog=load_billing_catalog(Path("config/billing.test.yaml")),
        transaction_runner=_run_transaction,
        now_factory=lambda: now,
        webhook_signing_secret="whsec_test",
    )


def test_paid_topup_and_service_fee_are_accounted_separately_and_replays_are_safe():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    topup_event = {
        "id": "evt_topup_123",
        "type": "checkout.session.completed",
        "created": 1786492800,
        "livemode": False,
        "data": {"object": {"id": "cs_test_123"}},
    }
    invoice_event = {
        "id": "evt_invoice_123",
        "type": "invoice.paid",
        "created": 1786492801,
        "livemode": False,
        "data": {"object": {"id": "in_test_123"}},
    }
    checkout_session = {
        "id": "cs_test_123",
        "livemode": False,
        "mode": "subscription",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "subscription": "sub_test_123",
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "credit_5_usd",
        },
        "line_items": {
            "data": [
                {"price": "price_1U3ZHnB5Es3VU3maoEQbMKnC", "quantity": 1},
                {"price": "price_1U3ZYBB5Es3VU3maSP6qq6sg", "quantity": 1},
            ]
        },
    }
    invoice = {
        "id": "in_test_123",
        "livemode": False,
        "customer": "cus_test_123",
        "subscription": "sub_test_123",
        "status_transitions": {"paid_at": 1786492801},
        "lines": {"data": [{"price": "price_1U3ZYBB5Es3VU3maSP6qq6sg", "amount": 500}]},
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    stripe = FakeStripeGateway(
        events={b"topup": topup_event, b"invoice": invoice_event},
        checkout_sessions={"cs_test_123": checkout_session},
        invoices={"in_test_123": invoice},
        subscriptions={"sub_test_123": _subscription(account_id)},
    )
    service = _service(client, stripe, now)

    topup_result = service.handle_sync(raw_payload=b"topup", stripe_signature="signature")
    replay_result = service.handle_sync(raw_payload=b"topup", stripe_signature="signature")
    fee_result = service.handle_sync(raw_payload=b"invoice", stripe_signature="signature")

    assert topup_result.outcome == "topup_credited"
    assert topup_result.duplicate is False
    assert replay_result.duplicate is True
    assert fee_result.outcome == "service_fee_collected"

    wallet = client.documents[("customer_wallets", customer_wallet_document_id("user-1"))]
    assert wallet["available_credit_nanos"] == 5_000_000_000
    assert wallet["lifetime_credited_nanos"] == 5_000_000_000
    assert ("wallet_transactions", "stripe_topup_cs_test_123") in client.documents
    fee_transaction = client.documents[("wallet_transactions", "stripe_service_fee_in_test_123")]
    assert fee_transaction["transaction_type"] == "monthly_service_fee_payment"
    assert fee_transaction["amount_nanos"] == 5_000_000_000
    assert wallet["available_credit_nanos"] == 5_000_000_000

    period = client.documents[
        ("customer_billing_periods", customer_billing_period_document_id("user-1", "2026-08"))
    ]
    assert period["monthly_service_fee_status"] == "paid"
    assert period["monthly_service_fee_paid_nanos"] == 5_000_000_000
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["stripe_subscription_id"] == "sub_test_123"
    assert account["stripe_subscription_status"] == "active"


def test_invalid_webhook_signature_is_rejected_before_any_firestore_write():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    client = FakeFirestore()
    stripe = FakeStripeGateway(events={}, checkout_sessions={}, invoices={}, subscriptions={})
    service = _service(client, stripe, now)

    with pytest.raises(BillingApiError) as exc_info:
        service.handle_sync(raw_payload=b"untrusted", stripe_signature="not-a-signature")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "stripe_signature_invalid"
    assert client.documents == {}


def test_subscription_state_uses_the_current_server_retrieved_subscription():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    stale_event = {
        "id": "evt_subscription_123",
        "type": "customer.subscription.updated",
        "created": 1786492802,
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_test_123",
                "status": "past_due",
                "customer": "cus_test_123",
                "metadata": {"billing_account_id": account_id},
            }
        },
    }
    stripe = FakeStripeGateway(
        events={b"subscription": stale_event},
        checkout_sessions={},
        invoices={},
        subscriptions={"sub_test_123": _subscription(account_id)},
    )

    result = _service(client, stripe, now).handle_sync(
        raw_payload=b"subscription",
        stripe_signature="signature",
    )

    assert result.outcome == "subscription_state_updated"
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["stripe_subscription_status"] == "active"
