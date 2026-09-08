from datetime import datetime, timezone

import pytest

from common.billing import (
    customer_billing_account_document_id,
    stripe_webhook_event_document_id,
)
from services.billing_api_v3.app.services.firestore_records import (
    BillingRecordError,
    build_initial_billing_account_document,
    build_stripe_webhook_event_document,
)


NOW = datetime(2026, 8, 12, 10, 30, tzinfo=timezone.utc)
PAYLOAD_HASH = "a" * 64


def test_private_billing_account_document_starts_before_stripe_customer_creation() -> None:
    billing_account_id = customer_billing_account_document_id("user-1")

    document = build_initial_billing_account_document(
        billing_account_id=billing_account_id,
        billing_subject_id="user-1",
        owner_uid="user-1",
        catalog_environment="test",
        created_at=NOW,
    )

    assert document["billing_account_id"] == billing_account_id
    assert document["stripe_customer_id"] is None
    assert document["stripe_customer_status"] == "pending"
    assert document["stripe_subscription_status"] == "not_started"
    assert document["currency"] == "USD"


def test_stripe_event_document_is_immutable_audit_data_without_raw_payload() -> None:
    document = build_stripe_webhook_event_document(
        stripe_event_id="evt_123",
        stripe_event_type="checkout.session.completed",
        stripe_event_created_at=NOW,
        stripe_livemode=False,
        catalog_environment="test",
        payload_sha256=PAYLOAD_HASH,
        outcome="topup_credited",
        processed_at=NOW,
        billing_account_id=customer_billing_account_document_id("user-1"),
        billing_subject_id="user-1",
        owner_uid="user-1",
        stripe_customer_id="cus_123",
        stripe_checkout_session_id="cs_test_123",
        stripe_payment_intent_id="pi_123",
        wallet_transaction_id="stripe_topup_cs_test_123",
    )

    assert stripe_webhook_event_document_id("evt_123").startswith("stripe-event-")
    assert document["outcome"] == "topup_credited"
    assert document["stripe_livemode"] is False
    assert "raw_payload" not in document
    assert "stripe_signature" not in document


def test_webhook_record_rejects_a_non_sha256_payload_digest() -> None:
    with pytest.raises(BillingRecordError, match="payload_sha256"):
        build_stripe_webhook_event_document(
            stripe_event_id="evt_123",
            stripe_event_type="checkout.session.completed",
            stripe_event_created_at=NOW,
            stripe_livemode=False,
            catalog_environment="test",
            payload_sha256="not-a-digest",
            outcome="topup_credited",
            processed_at=NOW,
        )
