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
    def __init__(self, events, checkout_sessions, invoices, subscriptions, charges=None):
        self.events = events
        self.checkout_sessions = checkout_sessions
        self.invoices = invoices
        self.subscriptions = subscriptions
        self.charges = charges or {}

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

    def retrieve_charge(self, charge_id):
        return self.charges[charge_id]

    def cancel_subscription(self, subscription_id):
        if getattr(self, "cancel_error", None):
            raise self.cancel_error
        if not hasattr(self, "cancelled_subscriptions"):
            self.cancelled_subscriptions = []
        self.cancelled_subscriptions.append(subscription_id)
        sub = dict(self.subscriptions.get(subscription_id, {"id": subscription_id}))
        sub["status"] = "canceled"
        self.subscriptions[subscription_id] = sub
        return sub


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
        subscription_cancellation_requests_collection="subscription_cancellation_requests",
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


def test_subsequent_topup_credits_existing_wallet_successfully():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    topup_event = {
        "id": "evt_topup_second",
        "type": "checkout.session.completed",
        "created": 1786492900,
        "livemode": False,
        "data": {"object": {"id": "cs_second_123"}},
    }
    checkout_session = {
        "id": "cs_second_123",
        "livemode": False,
        "mode": "payment",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "payment_intent": "pi_second_123",
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "checkout_kind": "topup",
            "topup_package_id": "credit_10_usd",
        },
        "line_items": {
            "data": [
                {"price": "price_1U3ZKOB5Es3VU3maflfGkdrX", "quantity": 1},
            ]
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
        "last_credit_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"second_topup": topup_event},
        checkout_sessions={"cs_second_123": checkout_session},
        invoices={},
        subscriptions={},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"second_topup", stripe_signature="signature")

    assert result.outcome == "topup_credited"
    assert result.duplicate is False

    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["available_credit_nanos"] == 15_000_000_000
    assert wallet["lifetime_credited_nanos"] == 15_000_000_000


def test_charge_refunded_is_reconciliation_only_and_does_not_change_balance():
    """charge.refunded is now reconciliation-only; it must NOT debit the wallet."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_refund_123",
        "type": "charge.refunded",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "ch_test_123",
                "customer": "cus_test_123",
                "amount": 1000,
                "amount_refunded": 1000,
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                    "topup_package_id": "credit_10_usd",
                },
            }
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 5_000_000_000,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"refund_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"refund_payload", stripe_signature="signature")

    assert result.outcome == "ignored"
    assert result.duplicate is False
    # Wallet balance must NOT have changed
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["available_credit_nanos"] == 5_000_000_000
    assert wallet["status"] == "active"


def test_charge_dispute_created_suspends_wallet():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    dispute_event = {
        "id": "evt_dispute_123",
        "type": "charge.dispute.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test_123",
                "charge": "ch_test_123",
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 10_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"dispute_payload": dispute_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"dispute_payload", stripe_signature="signature")

    assert result.outcome == "charge_disputed"
    assert result.duplicate is False
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert wallet["suspension_reason"] == "dispute"


def test_refund_created_processes_individual_amount_and_keys_on_refund_id():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_refund_123",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_test_part1",
                "charge": "ch_test_123",
                "amount": 200,
            }
        },
    }
    charge_obj = {
        "id": "ch_test_123",
        "customer": "cus_test_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "topup_package_id": "credit_10_usd",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 10_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"refund_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_test_123": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"refund_payload", stripe_signature="signature")

    assert result.outcome == "charge_refunded"
    assert result.duplicate is False
    wallet = client.documents[("customer_wallets", wallet_id)]
    # $2 out of $10 refund should reverse 2_000_000_000 nanos out of 10_000_000_000
    assert wallet["available_credit_nanos"] == 8_000_000_000
    assert ("wallet_transactions", "stripe_refund_re_test_part1") in client.documents


def test_refund_created_suspends_wallet_with_reason_on_negative_balance():
    """refund.created should set suspension_reason='refund_debt' when balance goes negative."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_refund_neg",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_full_refund",
                "charge": "ch_test_full",
                "amount": 1000,
            }
        },
    }
    charge_obj = {
        "id": "ch_test_full",
        "customer": "cus_test_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "topup_package_id": "credit_10_usd",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 2_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 8_000_000_000,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"refund_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_test_full": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"refund_payload", stripe_signature="signature")

    assert result.outcome == "charge_refunded"
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert wallet["suspension_reason"] == "refund_debt"


def test_refund_created_retrieve_charge_failure_propagates_as_error():
    """If retrieve_charge() fails, the error must propagate (5xx) so Stripe retries."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    refund_event = {
        "id": "evt_refund_err",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_test_err",
                "charge": "ch_missing",
                "amount": 500,
            }
        },
    }
    client = FakeFirestore()
    stripe = FakeStripeGateway(
        events={b"refund_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={},  # ch_missing not in the dict => KeyError
    )
    service = _service(client, stripe, now)

    with pytest.raises(KeyError):
        service.handle_sync(raw_payload=b"refund_payload", stripe_signature="signature")


def test_charge_dispute_retrieves_charge_metadata_when_dispute_metadata_empty():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    dispute_event = {
        "id": "evt_dispute_456",
        "type": "charge.dispute.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test_456",
                "charge": "ch_parent_456",
                "metadata": {},  # Empty metadata on dispute object
            }
        },
    }
    charge_obj = {
        "id": "ch_parent_456",
        "customer": "cus_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 10_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"dispute_payload": dispute_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_parent_456": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"dispute_payload", stripe_signature="signature")

    assert result.outcome == "charge_disputed"
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert wallet["suspension_reason"] == "dispute"


def test_charge_dispute_closed_won_reinstates_wallet():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    dispute_event = {
        "id": "evt_dispute_closed_789",
        "type": "charge.dispute.closed",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test_789",
                "charge": "ch_parent_789",
                "status": "won",
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_parent_789",
        "customer": "cus_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "suspended",
        "suspension_reason": "dispute",
        "available_credit_nanos": 10_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"dispute_won_payload": dispute_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_parent_789": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"dispute_won_payload", stripe_signature="signature")

    assert result.outcome == "dispute_resolved"
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "active"
    assert wallet.get("suspension_reason") is None


def test_charge_dispute_closed_won_does_not_reinstate_refund_debt_suspended_wallet():
    """A dispute-won must NOT reinstate a wallet suspended for refund_debt."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    dispute_event = {
        "id": "evt_dispute_closed_no_reinstate",
        "type": "charge.dispute.closed",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test_no_reinstate",
                "charge": "ch_parent_no_reinstate",
                "status": "won",
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_parent_no_reinstate",
        "customer": "cus_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "suspended",
        "suspension_reason": "refund_debt",
        "available_credit_nanos": -2_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 10_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"dispute_won_payload": dispute_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_parent_no_reinstate": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"dispute_won_payload", stripe_signature="signature")

    assert result.outcome == "dispute_resolved"
    wallet = client.documents[("customer_wallets", wallet_id)]
    # Must remain suspended — the suspension was for refund_debt, not dispute
    assert wallet["status"] == "suspended"
    assert wallet["suspension_reason"] == "refund_debt"


def test_production_catalog_with_stripe_mode_test_accepts_test_events():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    event = {
        "id": "evt_ignored_test_mode",
        "type": "unhandled.event",
        "created": 1786492800,
        "livemode": False,
        "data": {"object": {"id": "obj_123"}},
    }
    client = FakeFirestore()
    stripe = FakeStripeGateway(
        events={b"test_payload": event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
    )
    prod_catalog = load_billing_catalog(Path("config/billing.prod.yaml"))
    assert prod_catalog.environment == "production"
    assert prod_catalog.stripe_mode == "test"

    service = StripeWebhookService(
        firestore_client_factory=lambda: client,
        stripe_gateway=stripe,
        settings=_settings(),
        catalog=prod_catalog,
        transaction_runner=_run_transaction,
        now_factory=lambda: now,
        webhook_signing_secret="whsec_test",
    )

    result = service.handle_sync(raw_payload=b"test_payload", stripe_signature="signature")
    assert result.outcome == "ignored"


def test_refund_created_full_combined_topup_caps_debit_at_credit_nanos():
    """Full refund of $10 combined charge ($5 credit + $5 fee) debits only $5 credit, not raw $10."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_refund_combined_full",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_comb_full",
                "charge": "ch_comb_full",
                "amount": 1000,  # $10 refunded
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_comb_full",
        "customer": "cus_test_123",
        "amount": 1000,  # $10 total ($5 credit + $5 fee)
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "credit_5_usd",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"refund_comb_full_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_comb_full": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"refund_comb_full_payload", stripe_signature="signature")

    assert result.outcome == "charge_refunded"
    wallet = client.documents[("customer_wallets", wallet_id)]
    # Debited exactly 5_000_000_000 (credit_5_usd), so balance is 0, NOT -5_000_000_000
    assert wallet["available_credit_nanos"] == 0
    assert wallet["status"] == "active"
    tx = client.documents[("wallet_transactions", "stripe_refund_re_comb_full")]
    assert tx["amount_nanos"] == -5_000_000_000
    assert tx["stripe_amount_cents"] == -1000
    assert tx["service_fee_reversed_cents"] == 500


def test_refund_created_partial_combined_topup_suspends_and_flags_for_review():
    """Partial refund of combined charge suspends wallet and flags for review without debiting arbitrary nanos."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_refund_combined_partial",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_comb_part",
                "charge": "ch_comb_part",
                "amount": 500,  # $5 partial refund on $10 charge
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_comb_part",
        "customer": "cus_test_123",
        "amount": 1000,  # $10 total
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "credit_5_usd",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"refund_comb_part_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_comb_part": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"refund_comb_part_payload", stripe_signature="signature")

    assert result.outcome == "charge_refunded"
    wallet = client.documents[("customer_wallets", wallet_id)]
    # Balance must NOT be debited arbitrarily; wallet suspended and flagged for review
    assert wallet["available_credit_nanos"] == 5_000_000_000
    assert wallet["status"] == "suspended"
    assert wallet["suspension_reason"] == "partial_combined_refund"
    assert wallet["review_required"] is True
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["review_required"] is True
    assert account["review_reason"] == "partial_combined_refund"


def test_charge_dispute_created_does_not_overwrite_refund_debt_suspension():
    """Dispute on a wallet already suspended for refund_debt preserves refund_debt as primary reason."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    dispute_event = {
        "id": "evt_dispute_on_debt",
        "type": "charge.dispute.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test_debt",
                "charge": "ch_debt_parent",
                "status": "needs_response",
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_debt_parent",
        "customer": "cus_123",
        "amount": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "suspended",
        "suspension_reason": "refund_debt",
        "suspension_reasons": ["refund_debt"],
        "available_credit_nanos": -2_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"dispute_on_debt_payload": dispute_event},
        checkout_sessions={},
        invoices={},
        subscriptions={},
        charges={"ch_debt_parent": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"dispute_on_debt_payload", stripe_signature="signature")

    assert result.outcome == "charge_disputed"
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    # Refuses to overwrite refund_debt
    assert wallet["suspension_reason"] == "refund_debt"
    assert "dispute" in wallet["suspension_reasons"]
    assert "dispute:dp_test_debt" in wallet["suspension_reasons"]
    assert "refund_debt" in wallet["suspension_reasons"]


def test_topup_recovers_wallet_from_negative_balance_and_clears_refund_debt():
    """A topup on a wallet with negative balance from refund_debt successfully credits the wallet and clears the suspension."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    topup_event = {
        "id": "evt_topup_recovery",
        "type": "checkout.session.completed",
        "created": 1786492800,
        "livemode": False,
        "data": {"object": {"id": "cs_topup_recovery"}},
    }
    checkout_session = {
        "id": "cs_topup_recovery",
        "livemode": False,
        "mode": "payment",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "payment_intent": "pi_recovery_123",
        "metadata": {
            "billing_account_id": account_id,
            "topup_package_id": "credit_10_usd",
            "checkout_kind": "topup",
            "catalog_environment": "test",
        },
        "line_items": {
            "data": [
                {"price": "price_1U3ZKOB5Es3VU3maflfGkdrX", "quantity": 1},
            ]
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "suspended",
        "suspension_reason": "refund_debt",
        "suspension_reasons": ["refund_debt"],
        "available_credit_nanos": -2_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"topup_recovery_payload": topup_event},
        checkout_sessions={"cs_topup_recovery": checkout_session},
        invoices={},
        subscriptions={},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"topup_recovery_payload", stripe_signature="signature")

    assert result.outcome == "topup_credited"
    wallet = client.documents[("customer_wallets", wallet_id)]
    # -2B + 10B = +8B
    assert wallet["available_credit_nanos"] == 8_000_000_000
    assert wallet["status"] == "active"
    assert wallet["suspension_reason"] is None
    assert "refund_debt" not in wallet["suspension_reasons"]


def test_simultaneous_disputes_tracked_independently():
    """Two concurrent disputes write distinct tags; resolving one won does not reactivate the wallet while the other is open."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")

    event_dp1 = {
        "id": "evt_dp1_created",
        "type": "charge.dispute.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_1",
                "charge": "ch_1",
                "status": "needs_response",
                "metadata": {"billing_account_id": account_id, "catalog_environment": "test"},
            }
        },
    }
    event_dp2 = {
        "id": "evt_dp2_created",
        "type": "charge.dispute.created",
        "created": 1786492810,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_2",
                "charge": "ch_2",
                "status": "needs_response",
                "metadata": {"billing_account_id": account_id, "catalog_environment": "test"},
            }
        },
    }
    event_dp1_won = {
        "id": "evt_dp1_closed_won",
        "type": "charge.dispute.closed",
        "created": 1786492900,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_1",
                "charge": "ch_1",
                "status": "won",
                "metadata": {"billing_account_id": account_id, "catalog_environment": "test"},
            }
        },
    }
    event_dp2_won = {
        "id": "evt_dp2_closed_won",
        "type": "charge.dispute.closed",
        "created": 1786493000,
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_2",
                "charge": "ch_2",
                "status": "won",
                "metadata": {"billing_account_id": account_id, "catalog_environment": "test"},
            }
        },
    }

    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "suspension_reason": None,
        "suspension_reasons": [],
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }

    stripe = FakeStripeGateway(
        events={
            b"dp1_created": event_dp1,
            b"dp2_created": event_dp2,
            b"dp1_won": event_dp1_won,
            b"dp2_won": event_dp2_won,
        },
        checkout_sessions={},
        invoices={},
        subscriptions={},
    )
    service = _service(client, stripe, now)

    # 1. Dispute 1 created -> suspended
    service.handle_sync(raw_payload=b"dp1_created", stripe_signature="signature")
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert "dispute:dp_1" in wallet["suspension_reasons"]

    # 2. Dispute 2 created -> suspended, both tags present
    service.handle_sync(raw_payload=b"dp2_created", stripe_signature="signature")
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert "dispute:dp_1" in wallet["suspension_reasons"]
    assert "dispute:dp_2" in wallet["suspension_reasons"]

    # 3. Dispute 1 won -> dp_1 removed, but dp_2 still open -> wallet REMAINS suspended
    service.handle_sync(raw_payload=b"dp1_won", stripe_signature="signature")
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "suspended"
    assert "dispute:dp_1" not in wallet["suspension_reasons"]
    assert "dispute:dp_2" in wallet["suspension_reasons"]
    assert wallet["suspension_reason"] == "dispute"

    # 4. Dispute 2 won -> dp_2 removed -> no disputes remain -> wallet REINSTATED to active
    service.handle_sync(raw_payload=b"dp2_won", stripe_signature="signature")
    wallet = client.documents[("customer_wallets", wallet_id)]
    assert wallet["status"] == "active"
    assert wallet["suspension_reason"] is None
    assert "dispute:dp_2" not in wallet["suspension_reasons"]


def test_refund_created_full_combined_topup_cancels_subscription():
    """A full refund of a combined initial checkout cancels the Stripe subscription."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_full_combined_cancel_sub",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_combined_cancel",
                "charge": "ch_combined_cancel",
                "amount": 1000,
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_combined_cancel",
        "customer": "cus_test_123",
        "amount": 1000,
        "amount_refunded": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "package_500",
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_billing_accounts", account_id)]["stripe_subscription_id"] = "sub_combined_123"
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"combined_refund_cancel_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={"sub_combined_123": {"id": "sub_combined_123", "status": "active"}},
        charges={"ch_combined_cancel": charge_obj},
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"combined_refund_cancel_payload", stripe_signature="signature")

    assert result.outcome == "charge_refunded"
    assert "sub_combined_123" in getattr(stripe, "cancelled_subscriptions", [])
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["subscription_cancellation_pending"] is False
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_combined_cancel")]
    assert cancellation_req["status"] == "completed"


def test_refund_created_full_combined_topup_cancellation_failure_preserves_pending_intent():
    """If Stripe cancel_subscription fails, account is NOT marked canceled, intent stays pending, and 502 is raised."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    wallet_id = customer_wallet_document_id("user-1")
    refund_event = {
        "id": "evt_combined_cancel_fail",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_combined_cancel_fail",
                "charge": "ch_combined_cancel_fail",
                "amount": 1000,
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_combined_cancel_fail",
        "customer": "cus_test_123",
        "amount": 1000,
        "amount_refunded": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "package_500",
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_billing_accounts", account_id)]["stripe_subscription_id"] = "sub_combined_fail"
    client.documents[("customer_billing_accounts", account_id)]["subscription_status"] = "active"
    client.documents[("customer_wallets", wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 5_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "lifetime_credited_nanos": 5_000_000_000,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"combined_refund_cancel_fail_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={"sub_combined_fail": {"id": "sub_combined_fail", "status": "active"}},
        charges={"ch_combined_cancel_fail": charge_obj},
    )
    stripe.cancel_error = StripeGatewayError("Stripe API connection timed out")
    service = _service(client, stripe, now)

    with pytest.raises(BillingApiError) as exc_info:
        service.handle_sync(raw_payload=b"combined_refund_cancel_fail_payload", stripe_signature="signature")

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "stripe_subscription_cancellation_failed"

    # Account must NOT be marked canceled
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] != "canceled"
    assert account["subscription_cancellation_pending"] is True

    # Durable cancellation intent must be recorded and pending
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_combined_cancel_fail")]
    assert cancellation_req["status"] == "pending"
    assert "Stripe API connection timed out" in cancellation_req["last_error"]
    assert cancellation_req["attempts"] == 1

    # Simulate Stripe retry: Stripe redelivers the event after transient failure clears
    stripe.cancel_error = None
    retry_result = service.handle_sync(raw_payload=b"combined_refund_cancel_fail_payload", stripe_signature="signature")

    assert retry_result.outcome == "charge_refunded"
    assert "sub_combined_fail" in getattr(stripe, "cancelled_subscriptions", [])
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["subscription_cancellation_pending"] is False
    assert cancellation_req["status"] == "completed"


def test_refund_created_before_checkout_session_creates_unresolved_intent_and_later_cancels():
    """If refund.created arrives before checkout.session.completed, it stores an unresolved intent

    which is subsequently resolved and cancelled once checkout.session.completed delivers the subscription ID.
    """
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    # 1. Full refund arrives before checkout.session.completed has populated stripe_subscription_id
    refund_event = {
        "id": "evt_refund_before_checkout",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_early_refund",
                "charge": "ch_early_charge",
                "amount": 1000,
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_early_charge",
        "customer": "cus_test_123",
        "amount": 1000,
        "amount_refunded": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "package_500",
            "catalog_environment": "test",
        },
    }

    # 2. Later, checkout.session.completed arrives with the subscription ID
    topup_event = {
        "id": "evt_topup_after_refund",
        "type": "checkout.session.completed",
        "created": 1786492810,
        "livemode": False,
        "data": {"object": {"id": "cs_late_topup"}},
    }
    checkout_session = {
        "id": "cs_late_topup",
        "livemode": False,
        "mode": "subscription",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "subscription": "sub_late_123",
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

    client = FakeFirestore()
    _seed_account(client, account_id, now)
    # Account starts with NO stripe_subscription_id
    assert client.documents[("customer_billing_accounts", account_id)].get("stripe_subscription_id") is None

    stripe = FakeStripeGateway(
        events={
            b"early_refund_payload": refund_event,
            b"late_topup_payload": topup_event,
        },
        checkout_sessions={"cs_late_topup": checkout_session},
        invoices={},
        subscriptions={"sub_late_123": {"id": "sub_late_123", "status": "active"}},
        charges={"ch_early_charge": charge_obj},
    )
    service = _service(client, stripe, now)

    # Step 1: Process early refund.created
    refund_result = service.handle_sync(raw_payload=b"early_refund_payload", stripe_signature="signature")
    assert refund_result.outcome == "charge_refunded"

    # Cancellation cannot happen yet without subscription ID:
    assert getattr(stripe, "cancelled_subscriptions", []) == []

    # Unresolved cancellation intent must be durably recorded:
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_early_refund")]
    assert cancellation_req["status"] == "unresolved"
    assert cancellation_req["stripe_subscription_id"] is None

    # Billing account reflects pending cancellation with unresolved request id:
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_cancellation_pending"] is True
    assert account["unresolved_cancellation_request_id"] == "re_early_refund"

    # Step 2: Now checkout.session.completed arrives with the subscription ID
    topup_result = service.handle_sync(raw_payload=b"late_topup_payload", stripe_signature="signature")
    assert topup_result.outcome == "topup_credited"

    # The unresolved intent must be resolved and executed:
    assert "sub_late_123" in getattr(stripe, "cancelled_subscriptions", [])
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_early_refund")]
    assert cancellation_req["status"] == "completed"
    assert cancellation_req["stripe_subscription_id"] == "sub_late_123"

    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["stripe_subscription_status"] == "canceled"
    assert account["subscription_cancellation_pending"] is False
    assert account["unresolved_cancellation_request_id"] is None


def test_refund_created_cancellation_idempotent_when_stripe_already_canceled():
    """Cancellation is strictly idempotent: if Stripe indicates subscription is already canceled,

    the gateway treats it as success and the local cancellation intent completes without raising 502.
    """
    from unittest.mock import MagicMock
    from services.billing_api_v3.app.services.stripe_gateway import StripeSdkGateway

    # 1. Direct unit test of StripeSdkGateway.cancel_subscription idempotency
    gateway = StripeSdkGateway.__new__(StripeSdkGateway)
    gateway._client = MagicMock()
    gateway._client.v1.subscriptions.cancel.side_effect = Exception(
        "Invalid request: This subscription has already been canceled."
    )
    result = gateway.cancel_subscription("sub_already_canceled_123")
    assert result["id"] == "sub_already_canceled_123"
    assert result["status"] == "canceled"

    # 2. End-to-end webhook test: re-delivery / retry after cancellation already succeeded in Stripe
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")
    refund_event = {
        "id": "evt_idempotent_cancel",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_idempotent_cancel",
                "charge": "ch_idempotent_cancel",
                "amount": 1000,
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_idempotent_cancel",
        "customer": "cus_test_123",
        "amount": 1000,
        "amount_refunded": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "package_500",
            "catalog_environment": "test",
        },
    }
    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_billing_accounts", account_id)]["stripe_subscription_id"] = "sub_already_canc"
    client.documents[("customer_billing_accounts", account_id)]["subscription_cancellation_pending"] = True
    client.documents[("subscription_cancellation_requests", "re_idempotent_cancel")] = {
        "schema_version": 1,
        "cancellation_request_id": "re_idempotent_cancel",
        "stripe_subscription_id": "sub_already_canc",
        "billing_account_id": account_id,
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "status": "pending",
        "attempts": 1,
        "created_at": now,
        "updated_at": now,
    }
    stripe = FakeStripeGateway(
        events={b"idempotent_cancel_payload": refund_event},
        checkout_sessions={},
        invoices={},
        subscriptions={"sub_already_canc": {"id": "sub_already_canc", "status": "canceled"}},
        charges={"ch_idempotent_cancel": charge_obj},
    )
    service = _service(client, stripe, now)

    retry_res = service.handle_sync(raw_payload=b"idempotent_cancel_payload", stripe_signature="signature")
    assert retry_res.outcome == "charge_refunded"
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_idempotent_cancel")]
    assert cancellation_req["status"] == "completed"
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["subscription_cancellation_pending"] is False


def test_refund_created_before_subscription_updated_creates_unresolved_intent_and_later_cancels():
    """If refund.created arrives before customer.subscription.updated, the unresolved intent

    is resolved and cancelled once customer.subscription.updated delivers the subscription ID.
    """
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    refund_event = {
        "id": "evt_refund_before_sub_updated",
        "type": "refund.created",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "re_early_refund_sub",
                "charge": "ch_early_charge_sub",
                "amount": 1000,
                "metadata": {},
            }
        },
    }
    charge_obj = {
        "id": "ch_early_charge_sub",
        "customer": "cus_test_123",
        "amount": 1000,
        "amount_refunded": 1000,
        "metadata": {
            "billing_account_id": account_id,
            "checkout_kind": "initial_subscription_topup",
            "topup_package_id": "package_500",
            "catalog_environment": "test",
        },
    }

    sub_event = {
        "id": "evt_sub_after_refund",
        "type": "customer.subscription.updated",
        "created": 1786492810,
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_late_from_event",
                "status": "active",
                "customer": "cus_test_123",
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
    }

    client = FakeFirestore()
    _seed_account(client, account_id, now)

    stripe = FakeStripeGateway(
        events={
            b"early_refund_payload_sub": refund_event,
            b"late_sub_payload": sub_event,
        },
        checkout_sessions={},
        invoices={},
        subscriptions={
            "sub_late_from_event": {
                "id": "sub_late_from_event",
                "status": "active",
                "livemode": False,
                "customer": "cus_test_123",
                "current_period_start": 1786492800,
                "current_period_end": 1789171200,
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
        charges={"ch_early_charge_sub": charge_obj},
    )
    service = _service(client, stripe, now)

    # 1. Process refund first
    refund_result = service.handle_sync(raw_payload=b"early_refund_payload_sub", stripe_signature="signature")
    assert refund_result.outcome == "charge_refunded"
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_early_refund_sub")]
    assert cancellation_req["status"] == "unresolved"
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_cancellation_pending"] is True
    assert account["unresolved_cancellation_request_id"] == "re_early_refund_sub"

    # 2. Process subscription update later
    sub_result = service.handle_sync(raw_payload=b"late_sub_payload", stripe_signature="signature")
    assert sub_result.outcome == "subscription_state_updated"

    # Intent must now be executed and completed
    assert "sub_late_from_event" in getattr(stripe, "cancelled_subscriptions", [])
    cancellation_req = client.documents[("subscription_cancellation_requests", "re_early_refund_sub")]
    assert cancellation_req["status"] == "completed"
    assert cancellation_req["stripe_subscription_id"] == "sub_late_from_event"

    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["subscription_cancellation_pending"] is False
    assert account["unresolved_cancellation_request_id"] is None


def test_stale_subscription_updated_event_cannot_resurrect_active_status_after_cancellation():
    """A stale or concurrent customer.subscription.updated reporting active status

    for a subscription that was already canceled cannot overwrite stripe_subscription_status to active.
    """
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    _seed_account(client, account_id, now)
    # Account has completed cancellation for subscription sub_test_123
    client.documents[("customer_billing_accounts", account_id)].update(
        {
            "stripe_subscription_id": "sub_test_123",
            "subscription_status": "canceled",
            "stripe_subscription_status": "canceled",
            "subscription_cancellation_pending": False,
            "unresolved_cancellation_request_id": None,
        }
    )

    stale_sub_event = {
        "id": "evt_stale_sub_update",
        "type": "customer.subscription.updated",
        "created": 1786492800,
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_test_123",
                "status": "active",
                "customer": "cus_test_123",
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
    }

    stripe = FakeStripeGateway(
        events={b"stale_sub_payload": stale_sub_event},
        checkout_sessions={},
        invoices={},
        subscriptions={
            "sub_test_123": {
                "id": "sub_test_123",
                "status": "active",
                "livemode": False,
                "customer": "cus_test_123",
                "current_period_start": 1786492800,
                "current_period_end": 1789171200,
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"stale_sub_payload", stripe_signature="signature")
    assert result.outcome == "subscription_state_updated"

    # Terminal cancellation state must be guarded: stripe_subscription_status remains canceled
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["stripe_subscription_status"] == "canceled"


def test_stale_invoice_paid_event_cannot_resurrect_active_status_after_cancellation():
    """A delayed invoice.paid event for an initial subscription checkout

    cannot overwrite stripe_subscription_status to active once cancellation is complete.
    """
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    account_id = customer_billing_account_document_id("user-1")

    client = FakeFirestore()
    _seed_account(client, account_id, now)
    client.documents[("customer_billing_accounts", account_id)].update(
        {
            "stripe_subscription_id": "sub_test_123",
            "subscription_status": "canceled",
            "stripe_subscription_status": "canceled",
            "subscription_cancellation_pending": False,
        }
    )

    invoice_event = {
        "id": "evt_stale_invoice_paid",
        "type": "invoice.paid",
        "created": 1786492801,
        "livemode": False,
        "data": {"object": {"id": "in_stale_123"}},
    }
    invoice = {
        "id": "in_stale_123",
        "livemode": False,
        "customer": "cus_test_123",
        "subscription": "sub_test_123",
        "status_transitions": {"paid_at": 1786492801},
        "lines": {"data": [{"price": "price_1U3ZYBB5Es3VU3maSP6qq6sg", "amount": 500}]},
    }
    stripe = FakeStripeGateway(
        events={b"stale_invoice_payload": invoice_event},
        checkout_sessions={},
        invoices={"in_stale_123": invoice},
        subscriptions={
            "sub_test_123": {
                "id": "sub_test_123",
                "status": "active",
                "livemode": False,
                "customer": "cus_test_123",
                "current_period_start": 1786492800,
                "current_period_end": 1789171200,
                "metadata": {
                    "billing_account_id": account_id,
                    "catalog_environment": "test",
                },
            }
        },
    )
    service = _service(client, stripe, now)

    result = service.handle_sync(raw_payload=b"stale_invoice_payload", stripe_signature="signature")
    assert result.outcome == "service_fee_collected"

    # Terminal cancellation state must be guarded
    account = client.documents[("customer_billing_accounts", account_id)]
    assert account["subscription_status"] == "canceled"
    assert account["stripe_subscription_status"] == "canceled"







