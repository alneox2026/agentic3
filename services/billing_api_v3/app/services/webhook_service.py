"""Verified, idempotent Stripe webhook settlement for wallet and fee records."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from typing import Any
import uuid

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
        if event_type == "refund.created":
            return self._handle_refund_created(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type == "charge.refunded":
            # Reconciliation-only: charge.refunded reports cumulative
            # amount_refunded which would cause double-debit if both
            # refund.created and charge.refunded are processed.
            return self._record_ignored_event(
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type == "charge.dispute.created":
            return self._handle_charge_dispute_created(
                event=event,
                stripe_event_id=event_id,
                stripe_event_type=event_type,
                stripe_event_created_at=event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_hash,
            )
        if event_type == "charge.dispute.closed":
            return self._handle_charge_dispute_closed(
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

        def operation(transaction: Any) -> tuple[WebhookResult, dict[str, Any] | None]:
            # Keep every read before the first write. Firestore retries this
            # function on concurrent changes, preserving financial correctness.
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)

            unresolved_id = None
            unresolved_cancel_ref = None
            unresolved_cancel_snapshot = None
            if account_snapshot.exists:
                account_temp = account_snapshot.to_dict() or {}
                unresolved_id = account_temp.get("unresolved_cancellation_request_id")
                if unresolved_id:
                    cancellation_collection_name = getattr(
                        self._settings,
                        "subscription_cancellation_requests_collection",
                        "subscription_cancellation_requests_v3",
                    )
                    unresolved_cancel_ref = client.collection(cancellation_collection_name).document(unresolved_id)
                    unresolved_cancel_snapshot = get_transaction_document_snapshot(transaction, unresolved_cancel_ref)

            if event_snapshot.exists:
                if (
                    unresolved_cancel_ref is not None
                    and unresolved_cancel_snapshot is not None
                    and unresolved_cancel_snapshot.exists
                ):
                    cancel_data = unresolved_cancel_snapshot.to_dict() or {}
                    if cancel_data.get("status") in ("pending", "unresolved"):
                        sub_id = cancel_data.get("stripe_subscription_id") or fulfillment.stripe_subscription_id
                        if sub_id:
                            return (
                                WebhookResult(
                                    stripe_event_id=fulfillment.stripe_event_id,
                                    stripe_event_type=fulfillment.stripe_event_type,
                                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "topup_credited")),
                                    duplicate=True,
                                ),
                                {
                                    "cancellation_ref": unresolved_cancel_ref,
                                    "account_ref": account_ref,
                                    "stripe_subscription_id": sub_id,
                                    "refund_id": unresolved_id,
                                },
                            )
                return (
                    WebhookResult(
                        stripe_event_id=fulfillment.stripe_event_id,
                        stripe_event_type=fulfillment.stripe_event_type,
                        outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                        duplicate=True,
                    ),
                    None,
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
                pending_cancel: dict[str, Any] | None = None
                if (
                    unresolved_cancel_ref is not None
                    and unresolved_cancel_snapshot is not None
                    and unresolved_cancel_snapshot.exists
                ):
                    cancel_data = unresolved_cancel_snapshot.to_dict() or {}
                    if cancel_data.get("status") in ("pending", "unresolved"):
                        sub_id = cancel_data.get("stripe_subscription_id") or fulfillment.stripe_subscription_id
                        if sub_id:
                            pending_cancel = {
                                "cancellation_ref": unresolved_cancel_ref,
                                "account_ref": account_ref,
                                "stripe_subscription_id": sub_id,
                                "refund_id": unresolved_id,
                            }
                return (
                    WebhookResult(
                        stripe_event_id=fulfillment.stripe_event_id,
                        stripe_event_type=fulfillment.stripe_event_type,
                        outcome="topup_credited",
                        duplicate=True,
                    ),
                    pending_cancel,
                )

            wallet = wallet_snapshot.to_dict() or {}
            if wallet_snapshot.exists:
                self._validate_wallet(wallet, owner_uid)
                available_credit = int(wallet.get("available_credit_nanos", 0))
                lifetime_credited = nonnegative_int(
                    wallet.get("lifetime_credited_nanos", 0),
                    field_name="lifetime_credited_nanos",
                )
                new_available = available_credit + package.credit_nanos
                existing_reasons = list(wallet.get("suspension_reasons") or [])
                existing_reason = wallet.get("suspension_reason")
                if existing_reason and existing_reason not in existing_reasons:
                    existing_reasons.append(existing_reason)

                wallet_updates: dict[str, Any] = {
                    "available_credit_nanos": new_available,
                    "lifetime_credited_nanos": lifetime_credited + package.credit_nanos,
                    "updated_at": processed_at,
                    "last_credit_at": processed_at,
                }
                # When balance is brought non-negative, clear refund_debt
                if new_available >= 0:
                    if "refund_debt" in existing_reasons:
                        existing_reasons.remove("refund_debt")
                    wallet_updates["suspension_reasons"] = existing_reasons
                    if not existing_reasons:
                        wallet_updates["status"] = "active"
                        wallet_updates["suspension_reason"] = None
                    elif wallet.get("suspension_reason") == "refund_debt":
                        wallet_updates["suspension_reason"] = existing_reasons[0]

                transaction.update(wallet_ref, wallet_updates)
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
            pending_cancellation: dict[str, Any] | None = None
            if (
                unresolved_cancel_ref is not None
                and unresolved_cancel_snapshot is not None
                and unresolved_cancel_snapshot.exists
                and fulfillment.stripe_subscription_id
            ):
                cancel_data = unresolved_cancel_snapshot.to_dict() or {}
                if cancel_data.get("status") in ("unresolved", "pending"):
                    transaction.update(
                        unresolved_cancel_ref,
                        {
                            "stripe_subscription_id": fulfillment.stripe_subscription_id,
                            "status": "pending",
                            "updated_at": processed_at,
                        },
                    )
                    pending_cancellation = {
                        "cancellation_ref": unresolved_cancel_ref,
                        "account_ref": account_ref,
                        "stripe_subscription_id": fulfillment.stripe_subscription_id,
                        "refund_id": unresolved_id,
                    }

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
            return (
                WebhookResult(
                    stripe_event_id=fulfillment.stripe_event_id,
                    stripe_event_type=fulfillment.stripe_event_type,
                    outcome="topup_credited",
                    duplicate=False,
                ),
                pending_cancellation,
            )

        result, pending_cancellation = self._transaction_runner(client, operation)
        if pending_cancellation and pending_cancellation.get("stripe_subscription_id"):
            self._execute_pending_cancellation(client, pending_cancellation)
        return result

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
            is_same_sub = (account.get("stripe_subscription_id") == fulfillment.stripe_subscription_id)
            is_locally_canceled = (account.get("subscription_status") == "canceled")
            is_cancel_pending = bool(account.get("subscription_cancellation_pending"))

            last_sub_event_ts = account.get("last_subscription_event_created_at")
            is_stale_fee_event = bool(
                last_sub_event_ts
                and fulfillment.stripe_event_created_at
                and fulfillment.stripe_event_created_at < last_sub_event_ts
            )

            account_updates: dict[str, Any] = {
                "stripe_customer_id": fulfillment.stripe_customer_id,
                "stripe_customer_status": "ready",
                "last_service_fee_invoice_id": fulfillment.stripe_invoice_id,
                "last_service_fee_paid_at": fulfillment.paid_at,
                "updated_at": processed_at,
            }
            if not is_stale_fee_event:
                resolved_stripe_sub_status = fulfillment.subscription_status
                if is_same_sub and (is_locally_canceled or is_cancel_pending) and fulfillment.subscription_status != "canceled":
                    resolved_stripe_sub_status = "canceled" if is_locally_canceled else account.get("stripe_subscription_status", "canceled")

                account_updates.update({
                    "stripe_subscription_id": fulfillment.stripe_subscription_id,
                    "stripe_subscription_status": resolved_stripe_sub_status,
                    "stripe_subscription_current_period_start": fulfillment.subscription_period_start,
                    "stripe_subscription_current_period_end": fulfillment.subscription_period_end,
                })

            transaction.update(account_ref, account_updates)
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

        def operation(transaction: Any) -> tuple[WebhookResult, dict[str, Any] | None]:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)

            unresolved_id = None
            unresolved_cancel_ref = None
            unresolved_cancel_snapshot = None
            if account_snapshot.exists:
                account_temp = account_snapshot.to_dict() or {}
                unresolved_id = account_temp.get("unresolved_cancellation_request_id")
                if unresolved_id:
                    cancellation_collection_name = getattr(
                        self._settings,
                        "subscription_cancellation_requests_collection",
                        "subscription_cancellation_requests_v3",
                    )
                    unresolved_cancel_ref = client.collection(cancellation_collection_name).document(unresolved_id)
                    unresolved_cancel_snapshot = get_transaction_document_snapshot(transaction, unresolved_cancel_ref)

            if event_snapshot.exists:
                if (
                    unresolved_cancel_ref is not None
                    and unresolved_cancel_snapshot is not None
                    and unresolved_cancel_snapshot.exists
                ):
                    cancel_data = unresolved_cancel_snapshot.to_dict() or {}
                    if cancel_data.get("status") in ("pending", "unresolved"):
                        sub_id = cancel_data.get("stripe_subscription_id") or stripe_subscription_id
                        if sub_id:
                            return (
                                WebhookResult(
                                    stripe_event_id=stripe_event_id,
                                    stripe_event_type=stripe_event_type,
                                    outcome=str((event_snapshot.to_dict() or {}).get("outcome", "subscription_state_updated")),
                                    duplicate=True,
                                ),
                                {
                                    "cancellation_ref": unresolved_cancel_ref,
                                    "account_ref": account_ref,
                                    "stripe_subscription_id": sub_id,
                                    "refund_id": unresolved_id,
                                },
                            )
                return (
                    WebhookResult(
                        stripe_event_id=stripe_event_id,
                        stripe_event_type=stripe_event_type,
                        outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                        duplicate=True,
                    ),
                    None,
                )
            if not account_snapshot.exists:
                raise BillingApiError(400, "billing_account_missing", "Stripe event has no billing account.")
            account = account_snapshot.to_dict() or {}
            owner_uid = self._validate_account_for_stripe(
                account,
                billing_account_id=billing_account_id,
                stripe_customer_id=stripe_customer_id,
            )

            # Guard terminal cancellation state: if this subscription was already
            # canceled (or cancellation is pending), do not allow stale concurrent
            # updates to set stripe_subscription_status back to active/trialing/past_due.
            is_same_sub = (account.get("stripe_subscription_id") == stripe_subscription_id)
            is_locally_canceled = (account.get("subscription_status") == "canceled")
            is_cancel_pending = bool(account.get("subscription_cancellation_pending"))

            last_event_ts = account.get("last_subscription_event_created_at")
            is_stale_event = bool(last_event_ts and stripe_event_created_at and stripe_event_created_at < last_event_ts)

            if is_stale_event:
                outcome = "ignored"
            else:
                outcome = "subscription_state_updated"
                resolved_stripe_sub_status = subscription_status
                if is_same_sub and (is_locally_canceled or is_cancel_pending) and subscription_status != "canceled":
                    resolved_stripe_sub_status = "canceled" if is_locally_canceled else account.get("stripe_subscription_status", "canceled")

                account_updates = {
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_customer_status": "ready",
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_subscription_status": resolved_stripe_sub_status,
                    "stripe_subscription_current_period_start": period_start,
                    "stripe_subscription_current_period_end": period_end,
                    "last_subscription_event_created_at": stripe_event_created_at,
                    "updated_at": processed_at,
                }
                if last_invoice_id:
                    account_updates["last_service_fee_invoice_id"] = last_invoice_id
                transaction.update(account_ref, account_updates)

            pending_cancellation: dict[str, Any] | None = None
            if (
                not is_stale_event
                and unresolved_cancel_ref is not None
                and unresolved_cancel_snapshot is not None
                and unresolved_cancel_snapshot.exists
                and stripe_subscription_id
            ):
                cancel_data = unresolved_cancel_snapshot.to_dict() or {}
                if cancel_data.get("status") in ("unresolved", "pending"):
                    transaction.update(
                        unresolved_cancel_ref,
                        {
                            "stripe_subscription_id": stripe_subscription_id,
                            "status": "pending",
                            "updated_at": processed_at,
                        },
                    )
                    pending_cancellation = {
                        "cancellation_ref": unresolved_cancel_ref,
                        "account_ref": account_ref,
                        "stripe_subscription_id": stripe_subscription_id,
                        "refund_id": unresolved_id,
                    }

            transaction.create(
                event_ref,
                build_stripe_webhook_event_document(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    stripe_event_created_at=stripe_event_created_at,
                    stripe_livemode=stripe_livemode,
                    catalog_environment=self._catalog.environment,
                    payload_sha256=payload_sha256,
                    outcome=outcome,
                    processed_at=processed_at,
                    billing_account_id=billing_account_id,
                    billing_subject_id=owner_uid,
                    owner_uid=owner_uid,
                    stripe_customer_id=stripe_customer_id,
                    stripe_invoice_id=last_invoice_id,
                    stripe_subscription_id=stripe_subscription_id,
                ),
            )
            return (
                WebhookResult(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    outcome=outcome,
                    duplicate=False,
                ),
                pending_cancellation,
            )

        result, pending_cancellation = self._transaction_runner(client, operation)
        if pending_cancellation and pending_cancellation.get("stripe_subscription_id"):
            self._execute_pending_cancellation(client, pending_cancellation)
        return result

    def _handle_refund_created(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        refund_obj = _event_object(event)
        refund_id = _required_id(refund_obj.get("id"), "Refund id")
        charge_id = _optional_id(refund_obj.get("charge")) or ""
        # retrieve_charge must succeed so we can locate the billing account;
        # if Stripe is temporarily unavailable let the error propagate as 5xx
        # so Stripe retries the webhook delivery.
        charge: Mapping[str, Any] = {}
        if charge_id:
            charge = self._stripe_gateway.retrieve_charge(charge_id)
        metadata = charge.get("metadata") or refund_obj.get("metadata") or {}
        customer_id = _optional_id(charge.get("customer")) or _optional_id(refund_obj.get("customer"))
        amount_refunded_cents = refund_obj.get("amount") or 0
        amount_cents = charge.get("amount") or amount_refunded_cents
        transaction_id = f"stripe_refund_{refund_id}"

        billing_account_id = _optional_id(metadata.get("billing_account_id"))
        if not billing_account_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        if metadata.get("catalog_environment") != self._catalog.environment:
            raise BillingApiError(400, "stripe_environment_mismatch", "Stripe event is for another environment.")

        # For combined charges (subscription fee + initial top-up credit),
        # never calculate a wallet reversal from raw cents.
        checkout_kind = metadata.get("checkout_kind", "")
        is_combined_charge = checkout_kind == "initial_subscription_topup"
        topup_package_id = _optional_id(metadata.get("topup_package_id"))

        service_fee_reversed_cents = 0
        requires_manual_review = False
        review_reason: str | None = None

        if is_combined_charge:
            package = None
            if topup_package_id:
                try:
                    package = self._catalog.get_topup_package(topup_package_id)
                except Exception:
                    package = None

            if amount_refunded_cents >= amount_cents:
                # Full refund of combined checkout:
                # Reverse at most the credited token amount; separately record service-fee portion.
                if package is not None:
                    reversed_nanos = package.credit_nanos
                    service_fee_reversed_cents = max(0, amount_refunded_cents - package.amount_cents)
                else:
                    reversed_nanos = 0
                    service_fee_reversed_cents = amount_refunded_cents
                    requires_manual_review = True
                    review_reason = "missing_topup_package_on_combined_refund"
            else:
                # Partial refund of combined checkout:
                # Cannot determine whether refund applies to service fee or wallet credit.
                # Never calculate wallet reversal from raw cents. Suspend wallet and flag for review.
                reversed_nanos = 0
                service_fee_reversed_cents = 0
                requires_manual_review = True
                review_reason = "partial_combined_refund"
        elif topup_package_id:
            # Standalone topup refund: prorate or reverse exact credit nanos
            try:
                package = self._catalog.get_topup_package(topup_package_id)
                if amount_cents > 0 and amount_refunded_cents < amount_cents:
                    reversed_nanos = int(package.credit_nanos * (amount_refunded_cents / amount_cents))
                else:
                    reversed_nanos = package.credit_nanos
            except Exception:
                reversed_nanos = amount_refunded_cents * 10_000_000
        else:
            # Fee-only or other charge with no topup package: no wallet credit was ever granted,
            # so reversing wallet credit would be an over-debit.
            reversed_nanos = 0
            service_fee_reversed_cents = amount_refunded_cents

        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            billing_account_id
        )
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(stripe_event_id)
        )
        transaction_ref = client.collection(self._settings.wallet_transactions_collection).document(
            transaction_id
        )
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> tuple[WebhookResult, dict[str, Any] | None]:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)

            cancellation_ref = None
            cancellation_collection_name = getattr(
                self._settings,
                "subscription_cancellation_requests_collection",
                "subscription_cancellation_requests_v3",
            )
            if is_combined_charge and amount_refunded_cents >= amount_cents:
                cancellation_ref = client.collection(cancellation_collection_name).document(refund_id)

            if event_snapshot.exists:
                if cancellation_ref is not None:
                    cancellation_snapshot = get_transaction_document_snapshot(transaction, cancellation_ref)
                    if cancellation_snapshot.exists:
                        cancellation_data = cancellation_snapshot.to_dict() or {}
                        if cancellation_data.get("status") in ("pending", "unresolved"):
                            account_data = account_snapshot.to_dict() or {} if account_snapshot.exists else {}
                            stripe_sub_id = cancellation_data.get("stripe_subscription_id") or account_data.get("stripe_subscription_id")
                            if not stripe_sub_id and charge.get("invoice"):
                                try:
                                    inv = self._stripe_gateway.retrieve_invoice(charge["invoice"])
                                    stripe_sub_id = _optional_id(inv.get("subscription"))
                                except Exception:
                                    stripe_sub_id = None
                            if stripe_sub_id:
                                return (
                                    WebhookResult(
                                        stripe_event_id=stripe_event_id,
                                        stripe_event_type=stripe_event_type,
                                        outcome="charge_refunded",
                                        duplicate=True,
                                    ),
                                    {
                                        "cancellation_ref": cancellation_ref,
                                        "account_ref": account_ref,
                                        "stripe_subscription_id": stripe_sub_id,
                                        "refund_id": refund_id,
                                    },
                                )
                return (
                    WebhookResult(
                        stripe_event_id=stripe_event_id,
                        stripe_event_type=stripe_event_type,
                        outcome=str((event_snapshot.to_dict() or {}).get("outcome", "ignored")),
                        duplicate=True,
                    ),
                    None,
                )
            if not account_snapshot.exists:
                raise BillingApiError(400, "billing_account_missing", "Stripe event has no billing account.")

            account = account_snapshot.to_dict() or {}
            resolved_customer_id = customer_id or account.get("stripe_customer_id")
            owner_uid = self._validate_account_for_stripe(
                account,
                billing_account_id=billing_account_id,
                stripe_customer_id=_required_id(resolved_customer_id, "Stripe Customer id"),
            )
            wallet_ref = client.collection(self._settings.wallets_collection).document(
                customer_wallet_document_id(owner_uid)
            )
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)

            cancellation_snapshot = None
            if cancellation_ref is not None:
                cancellation_snapshot = get_transaction_document_snapshot(transaction, cancellation_ref)

            if transaction_snapshot.exists:
                return (
                    WebhookResult(
                        stripe_event_id=stripe_event_id,
                        stripe_event_type=stripe_event_type,
                        outcome="charge_refunded",
                        duplicate=True,
                    ),
                    None,
                )

            if wallet_snapshot.exists:
                wallet = wallet_snapshot.to_dict() or {}
                self._validate_wallet(wallet, owner_uid)
                available_credit = int(wallet.get("available_credit_nanos", 0))
                new_available = available_credit - reversed_nanos
                existing_reasons = list(wallet.get("suspension_reasons") or [])
                existing_reason = wallet.get("suspension_reason")
                if existing_reason and existing_reason not in existing_reasons:
                    existing_reasons.append(existing_reason)

                wallet_updates: dict[str, Any] = {
                    "available_credit_nanos": new_available,
                    "updated_at": processed_at,
                    "last_refund_at": processed_at,
                }
                if new_available < 0:
                    wallet_updates["status"] = "suspended"
                    if "refund_debt" not in existing_reasons:
                        existing_reasons.append("refund_debt")
                    wallet_updates["suspension_reason"] = "refund_debt"
                    wallet_updates["suspension_reasons"] = existing_reasons
                elif requires_manual_review:
                    wallet_updates["status"] = "suspended"
                    if "partial_combined_refund" not in existing_reasons:
                        existing_reasons.append("partial_combined_refund")
                    wallet_updates["suspension_reason"] = "partial_combined_refund"
                    wallet_updates["suspension_reasons"] = existing_reasons
                    wallet_updates["review_required"] = True
                    wallet_updates["review_reason"] = review_reason

                transaction.update(wallet_ref, wallet_updates)

            pending_cancellation: dict[str, Any] | None = None
            if is_combined_charge and amount_refunded_cents >= amount_cents and cancellation_ref is not None:
                stripe_sub_id = account.get("stripe_subscription_id")
                if not stripe_sub_id and charge.get("invoice"):
                    try:
                        inv = self._stripe_gateway.retrieve_invoice(charge["invoice"])
                        stripe_sub_id = _optional_id(inv.get("subscription"))
                    except Exception:
                        stripe_sub_id = None

                if cancellation_snapshot is None or not cancellation_snapshot.exists:
                    transaction.create(
                        cancellation_ref,
                        {
                            "schema_version": 1,
                            "cancellation_request_id": refund_id,
                            "stripe_subscription_id": stripe_sub_id,
                            "billing_account_id": billing_account_id,
                            "billing_subject_id": owner_uid,
                            "owner_uid": owner_uid,
                            "status": "pending" if stripe_sub_id else "unresolved",
                            "created_at": processed_at,
                            "updated_at": processed_at,
                            "attempts": 0,
                            "next_attempt_at": processed_at,
                            "leased_until": None,
                            "lease_owner_token": None,
                        },
                    )
                transaction.update(
                    account_ref,
                    {
                        "subscription_cancellation_pending": True,
                        "unresolved_cancellation_request_id": refund_id if not stripe_sub_id else None,
                        "updated_at": processed_at,
                    },
                )
                if stripe_sub_id:
                    pending_cancellation = {
                        "cancellation_ref": cancellation_ref,
                        "account_ref": account_ref,
                        "stripe_subscription_id": stripe_sub_id,
                        "refund_id": refund_id,
                    }

            if requires_manual_review:
                transaction.update(
                    account_ref,
                    {
                        "review_required": True,
                        "review_reason": review_reason,
                        "updated_at": processed_at,
                    },
                )

            tx_doc: dict[str, Any] = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "transaction_type": "stripe_charge_refund",
                "status": "posted",
                "billing_subject_id": owner_uid,
                "owner_uid": owner_uid,
                "wallet_document_id": customer_wallet_document_id(owner_uid),
                "currency": "USD",
                "amount_nanos": -reversed_nanos,
                "stripe_amount_cents": -amount_refunded_cents,
                "stripe_event_id": stripe_event_id,
                "stripe_charge_id": charge_id,
                "service_fee_reversed_cents": service_fee_reversed_cents,
                "created_at": processed_at,
            }
            if requires_manual_review:
                tx_doc["review_required"] = True
                tx_doc["review_reason"] = review_reason
            transaction.create(transaction_ref, tx_doc)
            transaction.create(
                event_ref,
                build_stripe_webhook_event_document(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    stripe_event_created_at=stripe_event_created_at,
                    stripe_livemode=stripe_livemode,
                    catalog_environment=self._catalog.environment,
                    payload_sha256=payload_sha256,
                    outcome="charge_refunded",
                    processed_at=processed_at,
                    billing_account_id=billing_account_id,
                    billing_subject_id=owner_uid,
                    owner_uid=owner_uid,
                    stripe_customer_id=_required_id(resolved_customer_id, "Stripe Customer id"),
                    wallet_transaction_id=transaction_id,
                ),
            )
            return (
                WebhookResult(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    outcome="charge_refunded",
                    duplicate=False,
                ),
                pending_cancellation,
            )

        result, pending_cancellation = self._transaction_runner(client, operation)

        if pending_cancellation and pending_cancellation.get("stripe_subscription_id"):
            self._execute_pending_cancellation(client, pending_cancellation)

        return result

    def _handle_charge_dispute_created(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        dispute_or_charge = _event_object(event)
        charge_id = _optional_id(dispute_or_charge.get("charge")) or _required_id(dispute_or_charge.get("id"), "Charge/Dispute id")
        metadata = dispute_or_charge.get("metadata") or {}
        if not metadata.get("billing_account_id") and dispute_or_charge.get("charge"):
            # retrieve_charge must succeed so we can locate the billing account;
            # let failures propagate as 5xx so Stripe retries the webhook.
            parent_charge = self._stripe_gateway.retrieve_charge(dispute_or_charge["charge"])
            metadata = parent_charge.get("metadata") or {}
        billing_account_id = _optional_id(metadata.get("billing_account_id"))
        if not billing_account_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        if metadata.get("catalog_environment") != self._catalog.environment:
            raise BillingApiError(400, "stripe_environment_mismatch", "Stripe event is for another environment.")

        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            billing_account_id
        )
        event_ref = client.collection(self._settings.stripe_webhook_events_collection).document(
            stripe_webhook_event_document_id(stripe_event_id)
        )
        transaction_id = f"stripe_dispute_{charge_id}_{stripe_event_id}"
        transaction_ref = client.collection(self._settings.wallet_transactions_collection).document(
            transaction_id
        )
        processed_at = _as_utc(self._now_factory())

        def operation(transaction: Any) -> WebhookResult:
            event_snapshot = get_transaction_document_snapshot(transaction, event_ref)
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)

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
            owner_uid = account.get("owner_uid")
            wallet_ref = client.collection(self._settings.wallets_collection).document(
                customer_wallet_document_id(owner_uid)
            )
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)

            if transaction_snapshot.exists:
                return WebhookResult(
                    stripe_event_id=stripe_event_id,
                    stripe_event_type=stripe_event_type,
                    outcome="charge_disputed",
                    duplicate=True,
                )

            if wallet_snapshot.exists:
                wallet = wallet_snapshot.to_dict() or {}
                existing_status = wallet.get("status")
                existing_reason = wallet.get("suspension_reason")
                existing_reasons = list(wallet.get("suspension_reasons") or [])
                if existing_reason and existing_reason not in existing_reasons:
                    existing_reasons.append(existing_reason)

                dispute_id = _optional_id(dispute_or_charge.get("id")) or stripe_event_id
                dispute_tag = f"dispute:{dispute_id}"
                if dispute_tag not in existing_reasons:
                    existing_reasons.append(dispute_tag)
                if "dispute" not in existing_reasons:
                    existing_reasons.append("dispute")

                wallet_updates: dict[str, Any] = {
                    "status": "suspended",
                    "suspension_reasons": existing_reasons,
                    "updated_at": processed_at,
                    "last_dispute_at": processed_at,
                }
                # Refuse to overwrite an existing non-dispute suspension reason (e.g. refund_debt)
                if existing_status == "suspended" and existing_reason and not existing_reason.startswith("dispute"):
                    wallet_updates["suspension_reason"] = existing_reason
                else:
                    wallet_updates["suspension_reason"] = "dispute"
                transaction.update(wallet_ref, wallet_updates)

            transaction.create(
                transaction_ref,
                {
                    "schema_version": 1,
                    "transaction_id": transaction_id,
                    "transaction_type": "stripe_charge_dispute",
                    "status": "disputed",
                    "billing_subject_id": owner_uid,
                    "owner_uid": owner_uid,
                    "wallet_document_id": customer_wallet_document_id(owner_uid),
                    "currency": "USD",
                    "stripe_event_id": stripe_event_id,
                    "stripe_charge_id": charge_id,
                    "created_at": processed_at,
                },
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
                    outcome="charge_disputed",
                    processed_at=processed_at,
                    billing_account_id=billing_account_id,
                    billing_subject_id=owner_uid,
                    owner_uid=owner_uid,
                    wallet_transaction_id=transaction_id,
                ),
            )
            return WebhookResult(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                outcome="charge_disputed",
                duplicate=False,
            )

        return self._transaction_runner(client, operation)

    def _handle_charge_dispute_closed(
        self,
        *,
        event: Mapping[str, Any],
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_event_created_at: datetime,
        stripe_livemode: bool,
        payload_sha256: str,
    ) -> WebhookResult:
        dispute_obj = _event_object(event)
        charge_id = _optional_id(dispute_obj.get("charge"))
        metadata = dispute_obj.get("metadata") or {}
        if not metadata.get("billing_account_id") and charge_id:
            # retrieve_charge must succeed; let failures propagate as 5xx
            # so Stripe retries the webhook delivery.
            parent_charge = self._stripe_gateway.retrieve_charge(charge_id)
            metadata = parent_charge.get("metadata") or {}
        billing_account_id = _optional_id(metadata.get("billing_account_id"))
        if not billing_account_id:
            return self._record_ignored_event(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                stripe_event_created_at=stripe_event_created_at,
                stripe_livemode=stripe_livemode,
                payload_sha256=payload_sha256,
            )
        if metadata.get("catalog_environment") != self._catalog.environment:
            raise BillingApiError(400, "stripe_environment_mismatch", "Stripe event is for another environment.")

        status = dispute_obj.get("status")
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
            owner_uid = account.get("owner_uid")
            wallet_ref = client.collection(self._settings.wallets_collection).document(
                customer_wallet_document_id(owner_uid)
            )
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)

            if status == "won" and wallet_snapshot.exists:
                wallet = wallet_snapshot.to_dict() or {}
                dispute_id = _optional_id(dispute_obj.get("id")) or stripe_event_id
                dispute_tag = f"dispute:{dispute_id}"
                existing_reasons = [
                    r for r in (wallet.get("suspension_reasons") or [])
                    if r != dispute_tag
                ]
                has_remaining_dispute = any(r.startswith("dispute:") for r in existing_reasons)
                if not has_remaining_dispute:
                    existing_reasons = [r for r in existing_reasons if r != "dispute"]

                available_credit = int(wallet.get("available_credit_nanos", 0))
                has_refund_debt = available_credit < 0 or wallet.get("suspension_reason") == "refund_debt" or "refund_debt" in existing_reasons

                if wallet.get("status") == "suspended":
                    # Only reinstate to active if no other suspension causes remain (not in debt, no remaining disputes or other reasons)
                    if (
                        not has_refund_debt
                        and not has_remaining_dispute
                        and not existing_reasons
                    ):
                        transaction.update(
                            wallet_ref,
                            {
                                "status": "active",
                                "suspension_reason": None,
                                "suspension_reasons": [],
                                "updated_at": processed_at,
                                "dispute_reinstated_at": processed_at,
                            },
                        )
                    else:
                        # Retain suspension for remaining reasons (e.g. remaining dispute or refund_debt)
                        if has_remaining_dispute:
                            retained_reason = "dispute"
                        elif has_refund_debt:
                            retained_reason = "refund_debt"
                        elif existing_reasons:
                            retained_reason = existing_reasons[0]
                        else:
                            retained_reason = wallet.get("suspension_reason")

                        transaction.update(
                            wallet_ref,
                            {
                                "suspension_reason": retained_reason,
                                "suspension_reasons": existing_reasons,
                                "updated_at": processed_at,
                                "dispute_reinstated_at": processed_at,
                            },
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
                    outcome="dispute_resolved",
                    processed_at=processed_at,
                    billing_account_id=billing_account_id,
                    billing_subject_id=owner_uid,
                    owner_uid=owner_uid,
                ),
            )
            return WebhookResult(
                stripe_event_id=stripe_event_id,
                stripe_event_type=stripe_event_type,
                outcome="dispute_resolved",
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

    def _execute_pending_cancellation(
        self,
        client: Any,
        pending_cancellation: dict[str, Any],
    ) -> None:
        stripe_sub_id = pending_cancellation["stripe_subscription_id"]
        cancel_ref = pending_cancellation["cancellation_ref"]
        acc_ref = pending_cancellation["account_ref"]
        cancel_refund_id = pending_cancellation["refund_id"]
        now_ts = _as_utc(self._now_factory())

        # Atomically claim lease with lease_owner_token before calling Stripe
        lease_seconds = max(
            180,
            int(
                getattr(
                    self._settings,
                    "cancellation_lease_seconds",
                    getattr(self._settings, "cancellation_reconciliation_lease_seconds", 180),
                )
            ),
        )
        lease_expiry = now_ts + timedelta(seconds=lease_seconds)
        lease_token = uuid.uuid4().hex

        def claim_lease_op(transaction: Any) -> bool:
            cancellation_snapshot = get_transaction_document_snapshot(transaction, cancel_ref)
            if not cancellation_snapshot.exists:
                return False
            data = cancellation_snapshot.to_dict() or {}
            if data.get("status") not in ("pending", "unresolved"):
                return False
            active_lease = data.get("leased_until")
            if active_lease and active_lease > now_ts:
                return False
            transaction.update(
                cancel_ref,
                {
                    "leased_until": lease_expiry,
                    "lease_owner_token": lease_token,
                    "updated_at": now_ts,
                },
            )
            return True

        claimed = False
        with suppress(Exception):
            claimed = self._transaction_runner(client, claim_lease_op)

        if not claimed:
            # Another worker holds active lease or intent is already terminal; skip
            return

        try:
            self._stripe_gateway.cancel_subscription(stripe_sub_id)
        except Exception as exc:
            def record_failure_op(transaction: Any) -> None:
                cancellation_snapshot = get_transaction_document_snapshot(transaction, cancel_ref)
                if not cancellation_snapshot.exists:
                    return
                current_data = cancellation_snapshot.to_dict() or {}
                if current_data.get("lease_owner_token") != lease_token:
                    return
                attempts = int(current_data.get("attempts", 0)) + 1
                backoff_seconds = min(3600, 30 * (2 ** min(attempts - 1, 6)))
                next_attempt = now_ts + timedelta(seconds=backoff_seconds)
                transaction.update(
                    cancel_ref,
                    {
                        "status": "pending",
                        "attempts": attempts,
                        "last_error": str(exc),
                        "next_attempt_at": next_attempt,
                        "leased_until": None,
                        "lease_owner_token": None,
                        "updated_at": now_ts,
                    },
                )

            with suppress(Exception):
                self._transaction_runner(client, record_failure_op)

            raise BillingApiError(
                502,
                "stripe_subscription_cancellation_failed",
                f"Failed to cancel Stripe subscription '{stripe_sub_id}' for refund '{cancel_refund_id}': {exc}",
            ) from exc

        def finalize_cancellation_op(transaction: Any) -> bool:
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
                    "completed_at": now_ts,
                    "leased_until": None,
                    "lease_owner_token": None,
                    "updated_at": now_ts,
                },
            )
            if acc_ref:
                transaction.update(
                    acc_ref,
                    {
                        "subscription_status": "canceled",
                        "stripe_subscription_status": "canceled",
                        "subscription_canceled_at": now_ts,
                        "subscription_cancellation_pending": False,
                        "unresolved_cancellation_request_id": None,
                        "updated_at": now_ts,
                    },
                )
            return True

        self._transaction_runner(client, finalize_cancellation_op)

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
            is_locally_canceled = (account.get("subscription_status") == "canceled")
            is_cancel_pending = bool(account.get("subscription_cancellation_pending"))
            sub_status = "canceled" if is_locally_canceled else ("pending_cancellation" if is_cancel_pending else "pending_activation")
            updates.update(
                {
                    "stripe_subscription_id": stripe_subscription_id,
                    "stripe_subscription_status": sub_status,
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

    def _validate_wallet(
        self,
        wallet: Mapping[str, Any],
        owner_uid: str,
    ) -> None:
        if (
            wallet.get("owner_uid") != owner_uid
            or wallet.get("billing_subject_id") != owner_uid
            or wallet.get("currency") != "USD"
        ):
            raise BillingApiError(400, "wallet_invalid", "Wallet record does not match the billing account.")

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
        expected_livemode = (
            getattr(
                self._catalog,
                "stripe_mode",
                "live" if self._catalog.environment == "production" else "test",
            )
            == "live"
        )
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
