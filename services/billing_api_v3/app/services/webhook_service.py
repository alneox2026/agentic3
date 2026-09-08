"""Verified, idempotent Stripe webhook settlement for wallet and fee records."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from typing import Any

from common.billing import (
    customer_billing_account_document_id,
    customer_billing_period_document_id,
    customer_wallet_document_id,
    nonnegative_int,
    stripe_webhook_event_document_id,
)
from services.billing_api_v3.app.core.config import BillingApiSettings, get_settings
from services.billing_api_v3.app.core.errors import BillingApiError
from services.billing_api_v3.app.services.billing_catalog import BillingCatalog, get_billing_catalog
from services.billing_api_v3.app.services.firestore_client import (
    get_firestore_client,
    get_transaction_document_snapshot,
)
from services.billing_api_v3.app.services.firestore_records import (
    build_stripe_webhook_event_document,
)
from services.billing_api_v3.app.services.stripe_gateway import (
    StripeGateway,
    StripeGatewayError,
    get_stripe_gateway,
)


TransactionRunner = Callable[[Any, Callable[[Any], Any]], Any]
_TOPUP_EVENT_TYPES = frozenset(
    {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
)
_SUBSCRIPTION_EVENT_TYPES = frozenset(
    {"customer.subscription.updated", "customer.subscription.deleted"}
)


@dataclass(frozen=True)
class WebhookResult:
    stripe_event_id: str
    stripe_event_type: str
    outcome: str
    duplicate: bool


@dataclass(frozen=True)
class TopupFulfillment:
    stripe_event_id: str
    stripe_event_type: str
    stripe_event_created_at: datetime
    stripe_livemode: bool
    payload_sha256: str
    billing_account_id: str
    topup_package_id: str
    stripe_customer_id: str
    stripe_checkout_session_id: str
    stripe_payment_intent_id: str | None
    stripe_subscription_id: str | None


@dataclass(frozen=True)
class ServiceFeeFulfillment:
    stripe_event_id: str
    stripe_event_type: str
    stripe_event_created_at: datetime
    stripe_livemode: bool
    payload_sha256: str
    billing_account_id: str
    stripe_customer_id: str
    stripe_invoice_id: str
    stripe_subscription_id: str
    paid_at: datetime
    subscription_status: str
    subscription_period_start: datetime | None
    subscription_period_end: datetime | None


class StripeWebhookService:
    """Accept only verified Stripe events and settle each source id exactly once."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        stripe_gateway: StripeGateway | None = None,
        settings: BillingApiSettings | None = None,
        catalog: BillingCatalog | None = None,
        transaction_runner: TransactionRunner | None = None,
        now_factory: Callable[[], datetime] | None = None,
        webhook_signing_secret: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._catalog = catalog or get_billing_catalog()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._stripe_gateway = stripe_gateway or get_stripe_gateway()
        self._transaction_runner = transaction_runner or _run_firestore_transaction
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._webhook_signing_secret = (
            webhook_signing_secret
            if webhook_signing_secret is not None
            else os.getenv("STRIPE_WEBHOOK_SIGNING_SECRET", "").strip()
        )

    async def handle(
        self,
        *,
        raw_payload: bytes,
        stripe_signature: str | None,
    ) -> WebhookResult:
        return await asyncio.to_thread(
            self.handle_sync,
            raw_payload=raw_payload,
            stripe_signature=stripe_signature,
        )

    def handle_sync(
        self,
        *,
        raw_payload: bytes,
        stripe_signature: str | None,
    ) -> WebhookResult:
        if not 60 <= self._settings.stripe_webhook_tolerance_seconds <= 900:
            raise RuntimeError(
                "STRIPE_WEBHOOK_TOLERANCE_SECONDS must be between 60 and 900."
            )
        if not self._webhook_signing_secret:
            raise BillingApiError(
                503,
                "stripe_webhook_not_configured",
                "The Stripe webhook is not configured yet.",
            )
        if not stripe_signature:
            raise BillingApiError(
                400,
                "stripe_signature_missing",
                "The Stripe-Signature header is required.",
            )
        try:
            event = self._stripe_gateway.construct_webhook_event(
                payload=raw_payload,
                signature=stripe_signature,
                signing_secret=self._webhook_signing_secret,
                tolerance_seconds=self._settings.stripe_webhook_tolerance_seconds,
            )
        except StripeGatewayError as exc:
            raise BillingApiError(
                400,
                "stripe_signature_invalid",
                "The Stripe webhook signature could not be verified.",
            ) from exc

        event_id, event_type, event_created_at, stripe_livemode, payload_hash = self._event_identity(
            event,
            raw_payload=raw_payload,
        )
        self._validate_event_environment(stripe_livemode)
        if event_type in _TOPUP_EVENT_TYPES:
            return self._handle_topup_event(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type == "invoice.paid":
            return self._handle_invoice_paid(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type == "invoice.payment_failed":
            return self._handle_invoice_payment_failed(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type in _SUBSCRIPTION_EVENT_TYPES:
            return self._handle_subscription_state_event(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        return self._record_ignored_event(
            stripe_event_id=event_id,
            stripe_event_type=event_type,
            stripe_event_created_at=event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_hash,
        )

    def _handle_topup_event(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        event_session = _event_object(event)
        checkout_session_id = _required_id(event_session.get("id"), "Checkout Session id")
        try:
            checkout_session = self._stripe_gateway.retrieve_checkout_session(
                checkout_session_id
            )
        except StripeGatewayError as exc:
            raise BillingApiError(
                502,
                "stripe_checkout_retrieval_failed",
                "Stripe Checkout verification is temporarily unavailable.",
            ) from exc
        if checkout_session.get("payment_status") != "paid":
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        fulfillment = self._validate_topup_session(
            checkout_session=checkout_session,
            stripe_event_id=stripe_event_id,
            stripe_event_type=stripe_event_type,
            stripe_event_created_at=stripe_event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_sha256,
        )
        return self._settle_topup(fulfillment)

    def _validate_topup_session(
        self,
        *,
        checkout_session: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> TopupFulfillment:
        if checkout_session.get("payment_status") != "paid":
            raise BillingApiError(400, "stripe_checkout_unpaid", "The Checkout Session is unpaid.")
        if bool(checkout_session.get("livemode")) != stripe_livemode:
            raise BillingApiError(400, "stripe_event_invalid", "Stripe event details are inconsistent.")
        metadata = _mapping(checkout_session.get("metadata"), "Checkout Session metadata")
        billing_account_id = _required_id(
            metadata.get("billing_account_id"),
            "billing_account_id",
        )
        if metadata.get("catalog_environment") != self._catalog.environment:
            raise BillingApiError(400, "stripe_environment_mismatch", "Stripe event is for another environment.")
        topup_package_id = _required_id(metadata.get("topup_package_id"), "topup_package_id")
        try:
            package = self._catalog.get_topup_package(topup_package_id)
        except Exception as exc:
            raise BillingApiError(400, "stripe_checkout_invalid", "Checkout has an unknown top-up package.") from exc
        checkout_kind = metadata.get("checkout_kind")
        expected_mode = "subscription" if checkout_kind == "initial_subscription_topup" else "payment"
        if checkout_kind not in {"initial_subscription_topup", "topup"}:
            raise BillingApiError(400, "stripe_checkout_invalid", "Checkout has an unsupported billing flow.")
        if checkout_session.get("mode") != expected_mode:
            raise BillingApiError(400, "stripe_checkout_invalid", "Checkout mode is invalid for this billing flow.")

        expected_prices = Counter({package.stripe_price_id: 1})
        if checkout_kind == "initial_subscription_topup":
            expected_prices[self._catalog.monthly_service_fee.stripe_price_id] += 1
        if _checkout_line_item_prices(checkout_session) != expected_prices:
            raise BillingApiError(400, "stripe_checkout_invalid", "Checkout line items are invalid.")

        checkout_session_id = _required_id(checkout_session.get("id"), "Checkout Session id")
        stripe_customer_id = _required_id(checkout_session.get("customer"), "Stripe Customer id")
        return TopupFulfillment(
            stripe_event_id=stripe_event_id,
            stripe_event_type=stripe_event_type,
            stripe_event_created_at=stripe_event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_sha256,
            billing_account_id=billing_account_id,
            topup_package_id=package.package_id,
            stripe_customer_id=stripe_customer_id,
            stripe_checkout_session_id=checkout_session_id,
            stripe_payment_intent_id=_optional_id(checkout_session.get("payment_intent")),
            stripe_subscription_id=_optional_id(checkout_session.get("subscription")),
        )

    def _settle_topup(self, fulfillment: TopupFulfillment) -> WebhookResult:
        package = self._catalog.get_topup_package(fulfillment.topup_package_id)
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            fulfillment.billing_account_id
        )
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(fulfillment.stripe_event_id)
        )
        transaction_id = f"stripe_topup_{fulfillment.stripe_checkout_session_id}"
        transaction_ref = client.collection(self._settings.wallet_transactions_collection).document(
            transaction_id
        )
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> WebhookResult:
            # Keep every read before the first write. Firestore retries this
            # function on concurrent changes, preserving financial correctness.
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)
            if event_snapshot.exists:
                return WebhookResult(
                    stripe_event_id=fulfillment.stripe_event_id,
                    stripe_event_type=fulfillment.stripe_event_type,
                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                    duplicate=True,
                )
            if not account_snapshot.exists:
                raise BillingApiError(400, "billing_account_missing", "Stripe event has no billing account.")
            account = account_snapshot.to_dict() or {}
            owner_uid = self._validate_account_for_stripe(
                account,
                billing_account_id=fulfillment.billing_account_id,
                stripe_customer_id=fulfillment.stripe_customer_id,
            )
            wallet_ref = client.collection(self._settings.wallets_collection).document(
                customer_wallet_document_id(owner_uid)
            )
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)

            if transaction_snapshot.exists:
                self._create_event_receipt(
                    transaction=transaction,
                    event_ref=event_ref,
                    fulfillment=fulfillment,
                    outcome="topup_credited",
                    processed_at=processed_at,
                    owner_uid=owner_uid,
                    wallet_transaction_id=transaction_id,
                )
                return WebhookResult(
                    stripe_event_id=fulfillment.stripe_event_id,
                    stripe_event_type=fulfillment.stripe_event_type,
                    outcome="topup_credited",
                    duplicate=True,
                )

            wallet = wallet_snapshot.to_dict() or {}
            if wallet_snapshot.exists:
                self._validate_wallet(wallet, owner_uid)
                available_credit = nonnegative_int(
                    wallet.get("available_credit_nanos"),
                    field_name="available_credit_nanos",
                )
                lifetime_credited = nonnegative_int(
                    wallet.get("lifetime_credited_nanos", 0),
                    field_name="lifetime_credited_nanos",
                )
                transaction.update(
                    wallet_ref,
                    {
                        "available_credit_nanos": available_credit + package.credit_nanos,
                        "lifetime_credited_nanos": lifetime_credited + package.credit_nanos,
                        "updated_at": processed_at,
                        "last_credit_at": processed_at,
                    },
                )
            else:
                transaction.create(
                    wallet_ref,
                    {
                        "schema_version": 1,
                        "billing_subject_id": owner_uid,
                        "owner_uid": owner_uid,
                        "currency": "USD",
                        "status": "active",
                        "available_credit_nanos": package.credit_nanos,
                        "reserved_credit_nanos": 0,
                        "settled_usage_nanos": 0,
                        "lifetime_credited_nanos": package.credit_nanos,
                        "created_at": processed_at,
                        "updated_at": processed_at,
                        "last_credit_at": processed_at,
                    },
                )
            transaction.create(
                transaction_ref,
                {
                    "schema_version": 1,
                    "transaction_id": transaction_id,
                    "transaction_type": "stripe_topup_credit",
                    "status": "posted",
                    "billing_subject_id": owner_uid,
                    "owner_uid": owner_uid,
                    "wallet_document_id": customer_wallet_document_id(owner_uid),
                    "currency": "USD",
                    "amount_nanos": package.credit_nanos,
                    "stripe_price_id": package.stripe_price_id,
                    "stripe_amount_cents": package.amount_cents,
                    "stripe_event_id": fulfillment.stripe_event_id,
                    "stripe_checkout_session_id": fulfillment.stripe_checkout_session_id,
                    "stripe_payment_intent_id": fulfillment.stripe_payment_intent_id,
                    "stripe_customer_id": fulfillment.stripe_customer_id,
                    "stripe_subscription_id": fulfillment.stripe_subscription_id,
                    "created_at": processed_at,
                },
            )
            self._clear_active_checkout(
                transaction=transaction,
                account_ref=account_ref,
                account=account,
                checkout_session_id=fulfillment.stripe_checkout_session_id,
                stripe_subscription_id=fulfillment.stripe_subscription_id,
                processed_at=processed_at,
            )
            self._create_event_receipt(
                transaction=transaction,
                event_ref=event_ref,
                fulfillment=fulfillment,
                outcome="topup_credited",
                processed_at=processed_at,
                owner_uid=owner_uid,
                wallet_transaction_id=transaction_id,
            )
            return WebhookResult(
                stripe_event_id=fulfillment.stripe_event_id,
                stripe_event_type=fulfillment.stripe_event_type,
                outcome="topup_credited",
                duplicate=False,
            )

        return self._transaction_runner(client, operation)

    def _handle_invoice_paid(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        invoice_id = _required_id(_event_object(event).get("id"), "Stripe invoice id")
        try:
            invoice = self._stripe_gateway.retrieve_invoice(invoice_id)
        except StripeGatewayError as exc:
            raise BillingApiError(502, "stripe_invoice_retrieval_failed", "Stripe invoice verification is temporarily unavailable.") from exc
        billing_reason = invoice.get("billing_reason")
        if billing_reason == "subscription_create":
            # Initial subscription creation invoices are fulfilled via checkout.session.completed
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        subscription_id = _optional_stripe_object_id(invoice.get("subscription"))
        if not subscription_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        try:
            subscription = self._stripe_gateway.retrieve_subscription(subscription_id)
        except StripeGatewayError as exc:
            raise BillingApiError(502, "stripe_subscription_retrieval_failed", "Stripe subscription verification is temporarily unavailable.") from exc
        try:
            fulfillment = self._validate_service_fee_invoice(
                invoice=invoice,
                subscription=subscription,
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
            return self._settle_service_fee(fulfillment)
        except BillingApiError as exc:
            if exc.status_code == 400:
                # Initial checkout invoices or legacy test invoices are acknowledged cleanly without failing delivery
                return self._record_ignored_event(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    stripe_event_created_at=stripe_event_created_at,
                    stripe_livemode=stripe_livemode,
                    payload_sha256=payload_sha256,
                )
            raise

    def _validate_service_fee_invoice(
        self,
        *,
        invoice: Mapping[str, Any],
        subscription: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> ServiceFeeFulfillment:
        if invoice.get("livemode") is not stripe_livemode:
            raise BillingApiError(400, "stripe_event_invalid", "Stripe event details are inconsistent.")
        if subscription.get("livemode") is not stripe_livemode:
            raise BillingApiError(400, "stripe_event_invalid", "Stripe event details are inconsistent.")
        metadata = _mapping(subscription.get("metadata"), "Stripe Subscription metadata")
        billing_account_id = _required_id(metadata.get("billing_account_id"), "billing_account_id")
        if metadata.get("catalog_environment") != self._catalog.environment:
            raise BillingApiError(400, "stripe_environment_mismatch", "Stripe event is for another environment.")
        if _invoice_fee_line_count(invoice, self._catalog.monthly_service_fee.stripe_price_id) != 1:
            raise BillingApiError(400, "stripe_invoice_invalid", "Invoice does not contain exactly one monthly service fee.")
        if _invoice_fee_line_amount(invoice, self._catalog.monthly_service_fee.stripe_price_id) != self._catalog.monthly_service_fee.amount_cents:
            raise BillingApiError(400, "stripe_invoice_invalid", "Monthly service fee amount is invalid.")
        paid_at = _invoice_paid_at(invoice, fallback=stripe_event_created_at)
        return ServiceFeeFulfillment(
            stripe_event_id=stripe_event_id,
            stripe_event_type=stripe_event_type,
            stripe_event_created_at=stripe_event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_sha256,
            billing_account_id=billing_account_id,
            stripe_customer_id=_required_id(invoice.get("customer"), "Stripe Customer id"),
            stripe_invoice_id=_required_id(invoice.get("id"), "Stripe invoice id"),
            stripe_subscription_id=_required_id(subscription.get("id"), "Stripe subscription id"),
            paid_at=paid_at,
            subscription_status=_required_id(subscription.get("status"), "Stripe subscription status"),
            subscription_period_start=_optional_timestamp(subscription.get("current_period_start")),
            subscription_period_end=_optional_timestamp(subscription.get("current_period_end")),
        )

    def _settle_service_fee(self, fulfillment: ServiceFeeFulfillment) -> WebhookResult:
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            fulfillment.billing_account_id
        )
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(fulfillment.stripe_event_id)
        )
        transaction_id = f"stripe_service_fee_{fulfillment.stripe_invoice_id}"
        transaction_ref = client.collection(self._settings.wallet_transactions_collection).document(
            transaction_id
        )
        period_key, period_start, period_end = _billing_period(fulfillment.paid_at)
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> WebhookResult:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)
            if event_snapshot.exists:
                return WebhookResult(
                    stripe_event_id=fulfillment.stripe_event_id,
                    stripe_event_type=fulfillment.stripe_event_type,
                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                    duplicate=True,
                )
            if not account_snapshot.exists:
                raise BillingApiError(400, "billing_account_missing", "Stripe event has no billing account.")
            account = account_snapshot.to_dict() or {}
            owner_uid = self._validate_account_for_stripe(
                account,
                billing_account_id=fulfillment.billing_account_id,
                stripe_customer_id=fulfillment.stripe_customer_id,
            )
            period_ref = client.collection(self._settings.customer_billing_periods_collection).document(
                customer_billing_period_document_id(owner_uid, period_key)
            )
            period_snapshot = get_transaction_document_snapshot(transaction, period_ref)
            if transaction_snapshot.exists:
                self._create_service_fee_event_receipt(
                    transaction=transaction,
                    event_ref=event_ref,
                    fulfillment=fulfillment,
                    owner_uid=owner_uid,
                    wallet_transaction_id=transaction_id,
                    processed_at=processed_at,
                )
                return WebhookResult(
                    stripe_event_id=fulfillment.stripe_event_id,
                    stripe_event_type=fulfillment.stripe_event_type,
                    outcome="service_fee_collected",
                    duplicate=True,
                )
            period = period_snapshot.to_dict() or {}
            transaction.create(
                transaction_ref,
                {
                    "schema_version": 1,
                    "transaction_id": transaction_id,
                    "transaction_type": "monthly_service_fee_payment",
                    "status": "posted",
                    "billing_subject_id": owner_uid,
                    "owner_uid": owner_uid,
                    "currency": "USD",
                    "amount_nanos": self._catalog.monthly_service_fee.fee_nanos,
                    "stripe_price_id": self._catalog.monthly_service_fee.stripe_price_id,
                    "stripe_amount_cents": self._catalog.monthly_service_fee.amount_cents,
                    "stripe_event_id": fulfillment.stripe_event_id,
                    "stripe_invoice_id": fulfillment.stripe_invoice_id,
                    "stripe_customer_id": fulfillment.stripe_customer_id,
                    "stripe_subscription_id": fulfillment.stripe_subscription_id,
                    "billing_period_key": period_key,
                    "created_at": processed_at,
                },
            )
            self._write_paid_service_fee_period(
                transaction=transaction,
                period_ref=period_ref,
                period=period,
                period_exists=period_snapshot.exists,
                owner_uid=owner_uid,
                period_key=period_key,
                period_start=period_start,
                period_end=period_end,
                fulfillment=fulfillment,
                processed_at=processed_at,
            )
            transaction.update(
                account_ref,
                {
                    "stripe_customer_id": fulfillment.stripe_customer_id,
                    "stripe_customer_status": "ready",
                    "stripe_subscription_id": fulfillment.stripe_subscription_id,
                    "stripe_subscription_status": fulfillment.subscription_status,
                    "stripe_subscription_current_period_start": fulfillment.subscription_period_start,
                    "stripe_subscription_current_period_end": fulfillment.subscription_period_end,
                    "last_service_fee_invoice_id": fulfillment.stripe_invoice_id,
                    "last_service_fee_paid_at": fulfillment.paid_at,
                    "updated_at": processed_at,
                },
            )
            self._create_service_fee_event_receipt(
                transaction=transaction,
                event_ref=event_ref,
                fulfillment=fulfillment,
                owner_uid=owner_uid,
                wallet_transaction_id=transaction_id,
                processed_at=processed_at,
            )
            return WebhookResult(
                stripe_event_id=fulfillment.stripe_event_id,
                stripe_event_type=fulfillment.stripe_event_type,
                outcome="service_fee_collected",
                duplicate=False,
            )

        return self._transaction_runner(client, operation)

    def _handle_invoice_payment_failed(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        invoice = _event_object(event)
        subscription_id = _optional_stripe_object_id(invoice.get("subscription"))
        if not subscription_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        try:
            subscription = self._stripe_gateway.retrieve_subscription(subscription_id)
        except StripeGatewayError as exc:
            raise BillingApiError(502, "stripe_subscription_retrieval_failed", "Stripe subscription verification is temporarily unavailable.") from exc
        if subscription.get("livemode") is not stripe_livemode:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        metadata = _mapping(subscription.get("metadata"), "Stripe Subscription metadata")
        billing_account_id = _optional_id(metadata.get("billing_account_id"))
        if not billing_account_id or metadata.get("catalog_environment") != self._catalog.environment:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        return self._record_subscription_state(
            stripe_event_id=stripe_event_id,
            stripe_event_type=stripe_event_type,
            stripe_event_created_at=stripe_event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_sha256,
            billing_account_id=billing_account_id,
            stripe_customer_id=_required_id(invoice.get("customer"), "Stripe Customer id"),
            stripe_subscription_id=subscription_id,
            subscription_status="past_due",
            period_start=_optional_timestamp(subscription.get("current_period_start")),
            period_end=_optional_timestamp(subscription.get("current_period_end")),
            last_invoice_id=_required_id(invoice.get("id"), "Stripe invoice id"),
            payment_failed=True,
        )

    def _handle_subscription_state_event(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        event_subscription = _event_object(event)
        subscription_id = _optional_id(event_subscription.get("id"))
        if not subscription_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        try:
            subscription = self._stripe_gateway.retrieve_subscription(subscription_id)
        except StripeGatewayError as exc:
            raise BillingApiError(
                502,
                "stripe_subscription_retrieval_failed",
                "Stripe subscription verification is temporarily unavailable.",
            ) from exc
        if subscription.get("livemode") is not stripe_livemode:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        metadata = _mapping(subscription.get("metadata"), "Stripe Subscription metadata")
        billing_account_id = _optional_id(metadata.get("billing_account_id"))
        if not billing_account_id or metadata.get("catalog_environment") != self._catalog.environment:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        return self._record_subscription_state(
            stripe_event_id=stripe_event_id,
            stripe_event_type=stripe_event_type,
            stripe_event_created_at=stripe_event_created_at,
            stripe_livemode=stripe_livemode,
            payload_sha256=payload_sha256,
            billing_account_id=billing_account_id,
            stripe_customer_id=_required_id(subscription.get("customer"), "Stripe Customer id"),
            stripe_subscription_id=_required_id(subscription.get("id"), "Stripe subscription id"),
            subscription_status=_required_id(subscription.get("status"), "Stripe subscription status"),
            period_start=_optional_timestamp(subscription.get("current_period_start")),
            period_end=_optional_timestamp(subscription.get("current_period_end")),
            last_invoice_id=None,
            payment_failed=False,
        )

    def _record_subscription_state(
        self,
        *,
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
        billing_account_id: str,
        stripe_customer_id: str,
        stripe_subscription_id: str,
        subscription_status: str,
        period_start: datetime | None,
        period_end: datetime | None,
        last_invoice_id: str | None,
        payment_failed: bool,
    ) -> WebhookResult:
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            billing_account_id
        )
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(stripe_event_id)
        )
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> WebhookResult:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            if event_snapshot.exists:
                return WebhookResult(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                    duplicate=True,
                )
            if not account_snapshot.exists:
                raise BillingApiError(400, "billing_account_missing", "Stripe event has no billing account.")
            account = account_snapshot.to_dict() or {}
            owner_uid = self._validate_account_for_stripe(
                account,
                billing_account_id=billing_account_id,
                stripe_customer_id=stripe_customer_id,
            )
            account_updates = {
                "stripe_customer_id": stripe_customer_id,
                "stripe_customer_status": "ready",
                "stripe_subscription_id": stripe_subscription_id,
                "stripe_subscription_status": subscription_status,
                "stripe_subscription_current_period_start": period_start,
                "stripe_subscription_current_period_end": period_end,
                "updated_at": processed_at,
            }
            if last_invoice_id:
                account_updates["last_service_fee_invoice_id"] = last_invoice_id
            transaction.update(account_ref, account_updates)
            transaction.create(
                event_ref,
                build_stripe_webhook_event_document(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    stripe_event_created_at=stripe_event_created_at,
                    stripe_livemode=stripe_livemode,
                    catalog_environment=self._catalog.environment,
                    payload_sha256=payload_sha256,
                    outcome="subscription_state_updated",
                    processed_at=processed_at,
                    billing_account_id=billing_account_id,
                    billing_subject_id=owner_uid,
                    owner_uid=owner_uid,
                    stripe_customer_id=stripe_customer_id,
                    stripe_invoice_id=last_invoice_id,
                    stripe_subscription_id=stripe_subscription_id,
                ),
            )
            return WebhookResult(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                outcome="subscription_state_updated",
                duplicate=False,
            )

        return self._transaction_runner(client, operation)

    def _record_ignored_event(
        self,
        *,
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        client = self._firestore_client_factory()
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(stripe_event_id)
        )
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> WebhookResult:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            if event_snapshot.exists:
                return WebhookResult(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                    duplicate=True,
                )
            transaction.create(
                event_ref,
                build_stripe_webhook_event_document(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    stripe_event_created_at=stripe_event_created_at,
                    stripe_livemode=stripe_livemode,
                    catalog_environment=self._catalog.environment,
                    payload_sha256=payload_sha256,
                    outcome="ignored",
                    processed_at=processed_at,
                ),
            )
            return WebhookResult(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                outcome="ignored",
                duplicate=False,
            )

        return self._transaction_runner(client, operation)

    def _create_event_receipt(
        self,
        *,
        transaction: Any,
        event_ref: Any,
        fulfillment: TopupFulfillment,
        outcome: str,
        processed_at: datetime,
        owner_uid: str,
        wallet_transaction_id: str,
    ) -> None:
        transaction.create(
            event_ref,
            build_stripe_webhook_event_document(
                stripe_event_id=fulfillment.stripe_event_id,
                stripe_event_type=fulfillment.stripe_event_type,
                stripe_event_created_at=fulfillment.stripe_event_created_at,
                stripe_livemode=fulfillment.stripe_livemode,
                catalog_environment=self._catalog.environment,
                payload_sha256=fulfillment.payload_sha256,
                outcome=outcome,
                processed_at=processed_at,
                billing_account_id=fulfillment.billing_account_id,
                billing_subject_id=owner_uid,
                owner_uid=owner_uid,
                stripe_customer_id=fulfillment.stripe_customer_id,
                stripe_checkout_session_id=fulfillment.stripe_checkout_session_id,
                stripe_payment_intent_id=fulfillment.stripe_payment_intent_id,
                stripe_subscription_id=fulfillment.stripe_subscription_id,
                wallet_transaction_id=wallet_transaction_id,
            ),
        )

    def _create_service_fee_event_receipt(
        self,
        *,
        transaction: Any,
        event_ref: Any,
        fulfillment: ServiceFeeFulfillment,
        owner_uid: str,
        wallet_transaction_id: str,
        processed_at: datetime,
    ) -> None:
        transaction.create(
            event_ref,
            build_stripe_webhook_event_document(
                stripe_event_id=fulfillment.stripe_event_id,
                stripe_event_type=fulfillment.stripe_event_type,
                stripe_event_created_at=fulfillment.stripe_event_created_at,
                stripe_livemode=fulfillment.stripe_livemode,
                catalog_environment=self._catalog.environment,
                payload_sha256=fulfillment.payload_sha256,
                outcome="service_fee_collected",
                processed_at=processed_at,
                billing_account_id=fulfillment.billing_account_id,
                billing_subject_id=owner_uid,
                owner_uid=owner_uid,
                stripe_customer_id=fulfillment.stripe_customer_id,
                stripe_invoice_id=fulfillment.stripe_invoice_id,
                stripe_subscription_id=fulfillment.stripe_subscription_id,
                wallet_transaction_id=wallet_transaction_id,
            ),
        )

    def _write_paid_service_fee_period(
        self,
        *,
        transaction: Any,
        period_ref: Any,
        period: Mapping[str, Any],
        period_exists: bool,
        owner_uid: str,
        period_key: str,
        period_start: datetime,
        period_end: datetime,
        fulfillment: ServiceFeeFulfillment,
        processed_at: datetime,
    ) -> None:
        updates = {
            "monthly_service_fee_nanos": self._catalog.monthly_service_fee.fee_nanos,
            "monthly_service_fee_status": "paid",
            "monthly_service_fee_paid_nanos": self._catalog.monthly_service_fee.fee_nanos,
            "monthly_service_fee_invoice_id": fulfillment.stripe_invoice_id,
            "monthly_service_fee_paid_at": fulfillment.paid_at,
            "updated_at": processed_at,
        }
        if period_exists:
            transaction.update(period_ref, updates)
            return
        transaction.create(
            period_ref,
            {
                "schema_version": 1,
                "billing_subject_id": owner_uid,
                "owner_uid": owner_uid,
                "currency": "USD",
                "period_key": period_key,
                "period_start": period_start,
                "period_end": period_end,
                "status": "open",
                "usage_estimated_nanos": 0,
                "collected_usage_nanos": 0,
                "uncollected_usage_nanos": 0,
                "usage_turn_count": 0,
                "unpriced_turn_count": 0,
                "created_at": processed_at,
                **updates,
            },
        )

    def _clear_active_checkout(
        self,
        *,
        transaction: Any,
        account_ref: Any,
        account: Mapping[str, Any],
        checkout_session_id: str,
        stripe_subscription_id: str | None,
        processed_at: datetime,
    ) -> None:
        updates: dict[str, Any] = {
            "last_topup_checkout_session_id": checkout_session_id,
            "updated_at": processed_at,
        }
        if account.get("active_checkout_session_id") == checkout_session_id:
            updates.update(
                {
                    "active_checkout_request_id": None,
                    "active_checkout_session_id": None,
                    "active_checkout_url": None,
                    "active_checkout_mode": None,
                    "active_checkout_topup_package_id": None,
                    "active_checkout_created_at": None,
                    "active_checkout_expires_at": None,
                }
            )
        # An initial Checkout completion and its invoice.paid event can arrive
        # out of order. Record a pending subscription now so a fast second
        # top-up cannot start a duplicate monthly subscription.
        if stripe_subscription_id and not _optional_id(account.get("stripe_subscription_id")):
            updates.update(
                {
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_subscription_status": "pending_activation",
                }
            )
        transaction.update(account_ref, updates)

    def _validate_account_for_stripe(
        self,
        account: Mapping[str, Any],
        *,
        billing_account_id: str,
        stripe_customer_id: str,
    ) -> str:
        owner_uid = _required_id(account.get("owner_uid"), "owner_uid")
        if (
            account.get("billing_account_id") != billing_account_id
            or account.get("billing_subject_id") != owner_uid
            or customer_billing_account_document_id(owner_uid) != billing_account_id
            or account.get("currency") != "USD"
            or account.get("catalog_environment") != self._catalog.environment
        ):
            raise BillingApiError(400, "billing_account_invalid", "Stripe event billing account is invalid.")
        stored_customer_id = _optional_id(account.get("stripe_customer_id"))
        if stored_customer_id not in {stripe_customer_id, None}:
            raise BillingApiError(400, "stripe_customer_mismatch", "Stripe event customer does not match the billing account.")
        if stored_customer_id is None:
            raise BillingApiError(400, "stripe_customer_mismatch", "Stripe event customer does not match the billing account.")
        return owner_uid

    def _event_identity(
        self,
        event: Mapping[str, Any],
        *,
        raw_payload: bytes,
    ) -> tuple[str, str, datetime, bool, str]:
        stripe_event_id = _required_id(event.get("id"), "Stripe event id")
        stripe_event_type = _required_id(event.get("type"), "Stripe event type")
        created_at = _timestamp(event.get("created"), "Stripe event created")
        stripe_livemode = event.get("livemode")
        if not isinstance(stripe_livemode, bool):
            raise BillingApiError(400, "stripe_event_invalid", "Stripe event is invalid.")
        return (
            stripe_event_id,
            stripe_event_type,
            created_at,
            stripe_livemode,
            sha256(raw_payload).hexdigest(),
        )

    def _validate_event_environment(self, stripe_livemode: bool) -> None:
        expected_livemode = self._catalog.environment == "production"
        if stripe_livemode != expected_livemode:
            raise BillingApiError(
                400,
                "stripe_environment_mismatch",
                "Stripe event is for another environment.",
            )


def _event_object(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = _mapping(event.get("data"), "Stripe event data")
    return _mapping(data.get("object"), "Stripe event object")


def _checkout_line_item_prices(checkout_session: Mapping[str, Any]) -> Counter[str]:
    line_items = _mapping(checkout_session.get("line_items"), "Checkout line items")
    raw_items = line_items.get("data")
    if not isinstance(raw_items, list):
        raise BillingApiError(400, "stripe_checkout_invalid", "Checkout line items are unavailable.")
    prices: Counter[str] = Counter()
    for item in raw_items:
        item_mapping = _mapping(item, "Checkout line item")
        quantity = item_mapping.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity != 1:
            raise BillingApiError(400, "stripe_checkout_invalid", "Checkout line item quantity is invalid.")
        prices[_stripe_object_id(item_mapping.get("price"), "Checkout line item Price id")] += quantity
    return prices


def _invoice_fee_line_count(invoice: Mapping[str, Any], fee_price_id: str) -> int:
    return sum(1 for line in _invoice_lines(invoice) if _line_price_id(line) == fee_price_id)


def _invoice_fee_line_amount(invoice: Mapping[str, Any], fee_price_id: str) -> int:
    fee_lines = [line for line in _invoice_lines(invoice) if _line_price_id(line) == fee_price_id]
    if len(fee_lines) != 1:
        return -1
    value = fee_lines[0].get("amount")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _invoice_lines(invoice: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    lines = _mapping(invoice.get("lines"), "Stripe invoice lines")
    raw_lines = lines.get("data")
    if not isinstance(raw_lines, list):
        raise BillingApiError(400, "stripe_invoice_invalid", "Invoice line items are unavailable.")
    return [_mapping(line, "Stripe invoice line") for line in raw_lines]


def _line_price_id(line: Mapping[str, Any]) -> str | None:
    value = line.get("price")
    if value is None:
        pricing = line.get("pricing")
        if isinstance(pricing, Mapping):
            price_details = pricing.get("price_details")
            if isinstance(price_details, Mapping):
                value = price_details.get("price")
    return _optional_stripe_object_id(value)


def _invoice_paid_at(invoice: Mapping[str, Any], *, fallback: datetime) -> datetime:
    transitions = invoice.get("status_transitions")
    if isinstance(transitions, Mapping) and transitions.get("paid_at") is not None:
        return _timestamp(transitions.get("paid_at"), "Stripe invoice paid_at")
    return fallback


def _billing_period(value: datetime) -> tuple[str, datetime, datetime]:
    value = _as_utc(value)
    period_start = datetime(value.year, value.month, 1, tzinfo=timezone.utc)
    if value.month == 12:
        period_end = datetime(value.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        period_end = datetime(value.year, value.month + 1, 1, tzinfo=timezone.utc)
    return period_start.strftime("%Y-%m"), period_start, period_end


def _timestamp(value: Any, label: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BillingApiError(400, "stripe_event_invalid", f"{label} is invalid.")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _optional_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, "Stripe subscription period")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BillingApiError(400, "stripe_event_invalid", f"{label} is invalid.")
    return value


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BillingApiError(400, "stripe_event_invalid", f"{label} is invalid.")
    return value.strip()


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stripe_object_id(value: Any, label: str) -> str:
    resolved = _optional_stripe_object_id(value)
    if resolved is None:
        raise BillingApiError(400, "stripe_event_invalid", f"{label} is invalid.")
    return resolved


def _optional_stripe_object_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _run_firestore_transaction(client: Any, operation: Callable[[Any], Any]) -> Any:
    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> Any:
        return operation(transaction)

    return run(transaction)
