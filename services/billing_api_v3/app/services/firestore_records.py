"""Validated Firestore record builders for private Stripe billing state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.billing import customer_billing_account_document_id


CUSTOMER_BILLING_ACCOUNTS_COLLECTION = "customer_billing_accounts"
STRIPE_WEBHOOK_EVENTS_COLLECTION = "stripe_webhook_events"
_WEBHOOK_OUTCOMES = frozenset(
    {
        "ignored",
        "topup_credited",
        "service_fee_collected",
        "subscription_state_updated",
    }
)


class BillingRecordError(ValueError):
    """Raised when private billing record data is incomplete or unsafe."""


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BillingRecordError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name=field_name)


def _require_environment(value: Any) -> str:
    environment = _required_string(value, field_name="catalog_environment")
    if environment not in {"test", "production"}:
        raise BillingRecordError("catalog_environment must be test or production.")
    return environment


def build_initial_billing_account_document(
    *,
    billing_account_id: str,
    billing_subject_id: str,
    owner_uid: str,
    catalog_environment: str,
    created_at: datetime,
) -> dict[str, Any]:
    """Create the private record established before a Stripe Customer exists.

    The future Checkout endpoint creates this document transactionally before it
    calls Stripe. Stripe Customer creation will use this deterministic account
    id as its Stripe idempotency key, so concurrent first top-up attempts do
    not produce duplicate Stripe Customers.
    """

    normalized_subject_id = _required_string(
        billing_subject_id,
        field_name="billing_subject_id",
    )
    normalized_account_id = _required_string(
        billing_account_id,
        field_name="billing_account_id",
    )
    if normalized_account_id != customer_billing_account_document_id(normalized_subject_id):
        raise BillingRecordError(
            "billing_account_id must be the deterministic id for billing_subject_id."
        )

    return {
        "schema_version": 1,
        "billing_account_id": normalized_account_id,
        "billing_subject_id": normalized_subject_id,
        "owner_uid": _required_string(owner_uid, field_name="owner_uid"),
        "currency": "USD",
        "catalog_environment": _require_environment(catalog_environment),
        "stripe_customer_id": None,
        "stripe_customer_status": "pending",
        "stripe_subscription_id": None,
        "stripe_subscription_status": "not_started",
        "stripe_subscription_current_period_start": None,
        "stripe_subscription_current_period_end": None,
        "active_checkout_request_id": None,
        "active_checkout_session_id": None,
        "active_checkout_url": None,
        "active_checkout_mode": None,
        "active_checkout_topup_package_id": None,
        "active_checkout_created_at": None,
        "active_checkout_expires_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def build_stripe_webhook_event_document(
    *,
    stripe_event_id: str,
    stripe_event_type: str,
    stripe_event_created_at: datetime,
    stripe_livemode: bool,
    catalog_environment: str,
    payload_sha256: str,
    outcome: str,
    processed_at: datetime,
    billing_account_id: str | None = None,
    billing_subject_id: str | None = None,
    owner_uid: str | None = None,
    stripe_customer_id: str | None = None,
    stripe_checkout_session_id: str | None = None,
    stripe_payment_intent_id: str | None = None,
    stripe_invoice_id: str | None = None,
    stripe_subscription_id: str | None = None,
    wallet_transaction_id: str | None = None,
) -> dict[str, Any]:
    """Build one immutable receipt for a successfully processed Stripe event.

    The raw webhook body, payment-method details, and webhook signature are
    deliberately excluded. The future handler will create this record in the
    same Firestore transaction as its wallet/subscription accounting changes.
    """

    normalized_outcome = _required_string(outcome, field_name="outcome")
    if normalized_outcome not in _WEBHOOK_OUTCOMES:
        raise BillingRecordError("outcome is not supported for a Stripe webhook event.")
    normalized_hash = _required_string(payload_sha256, field_name="payload_sha256")
    if len(normalized_hash) != 64 or any(char not in "0123456789abcdef" for char in normalized_hash):
        raise BillingRecordError("payload_sha256 must be a lowercase SHA-256 digest.")
    if not isinstance(stripe_livemode, bool):
        raise BillingRecordError("stripe_livemode must be a boolean.")
    if not isinstance(stripe_event_created_at, datetime) or not isinstance(processed_at, datetime):
        raise BillingRecordError("Stripe event timestamps must be datetime values.")

    return {
        "schema_version": 1,
        "stripe_event_id": _required_string(stripe_event_id, field_name="stripe_event_id"),
        "stripe_event_type": _required_string(stripe_event_type, field_name="stripe_event_type"),
        "stripe_event_created_at": stripe_event_created_at,
        "stripe_livemode": stripe_livemode,
        "catalog_environment": _require_environment(catalog_environment),
        "payload_sha256": normalized_hash,
        "outcome": normalized_outcome,
        "billing_account_id": _optional_string(
            billing_account_id,
            field_name="billing_account_id",
        ),
        "billing_subject_id": _optional_string(
            billing_subject_id,
            field_name="billing_subject_id",
        ),
        "owner_uid": _optional_string(owner_uid, field_name="owner_uid"),
        "stripe_customer_id": _optional_string(
            stripe_customer_id,
            field_name="stripe_customer_id",
        ),
        "stripe_checkout_session_id": _optional_string(
            stripe_checkout_session_id,
            field_name="stripe_checkout_session_id",
        ),
        "stripe_payment_intent_id": _optional_string(
            stripe_payment_intent_id,
            field_name="stripe_payment_intent_id",
        ),
        "stripe_invoice_id": _optional_string(
            stripe_invoice_id,
            field_name="stripe_invoice_id",
        ),
        "stripe_subscription_id": _optional_string(
            stripe_subscription_id,
            field_name="stripe_subscription_id",
        ),
        "wallet_transaction_id": _optional_string(
            wallet_transaction_id,
            field_name="wallet_transaction_id",
        ),
        "processed_at": processed_at,
    }
