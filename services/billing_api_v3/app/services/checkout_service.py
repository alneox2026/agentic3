"""Server-owned Stripe Checkout creation for top-ups and the first subscription."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from common.billing import customer_billing_account_document_id
from services.billing_api_v3.app.core.config import BillingApiSettings, get_settings
from services.billing_api_v3.app.core.errors import BillingApiError
from services.billing_api_v3.app.services.billing_catalog import (
    BillingCatalog,
    BillingCatalogError,
    TopupPackage,
    get_billing_catalog,
)
from services.billing_api_v3.app.services.firestore_client import (
    get_firestore_client,
    get_transaction_document_snapshot,
)
from services.billing_api_v3.app.services.firestore_records import (
    build_initial_billing_account_document,
)
from services.billing_api_v3.app.services.stripe_gateway import (
    StripeGateway,
    StripeGatewayError,
    get_stripe_gateway,
)


TransactionRunner = Callable[[Any, Callable[[Any], Any]], Any]


@dataclass(frozen=True)
class CheckoutReservation:
    billing_account_id: str
    billing_subject_id: str
    owner_uid: str
    stripe_customer_id: str | None
    checkout_request_id: str
    topup_package_id: str
    starts_subscription: bool
    expires_at: datetime
    existing_checkout_session_id: str | None
    existing_checkout_url: str | None


@dataclass(frozen=True)
class CheckoutSessionResult:
    checkout_session_id: str
    checkout_url: str
    topup_package_id: str
    starts_subscription: bool


class CheckoutService:
    """Create one safe, short-lived Checkout Session per active billing account."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        stripe_gateway: StripeGateway | None = None,
        settings: BillingApiSettings | None = None,
        catalog: BillingCatalog | None = None,
        transaction_runner: TransactionRunner | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._catalog = catalog or get_billing_catalog()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._stripe_gateway = stripe_gateway or get_stripe_gateway()
        self._transaction_runner = transaction_runner or _run_firestore_transaction
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    async def create_topup_checkout(
        self,
        *,
        owner_uid: str,
        topup_package_id: str,
    ) -> CheckoutSessionResult:
        return await asyncio.to_thread(
            self.create_topup_checkout_sync,
            owner_uid=owner_uid,
            topup_package_id=topup_package_id,
        )

    def create_topup_checkout_sync(
        self,
        *,
        owner_uid: str,
        topup_package_id: str,
    ) -> CheckoutSessionResult:
        package = self._get_topup_package(topup_package_id)
        self._validate_checkout_urls()
        now = _as_utc(self._now_factory())
        reservation = self._reserve_checkout(
            owner_uid=owner_uid,
            package=package,
            now=now,
        )
        if reservation.existing_checkout_session_id and reservation.existing_checkout_url:
            return CheckoutSessionResult(
                checkout_session_id=reservation.existing_checkout_session_id,
                checkout_url=reservation.existing_checkout_url,
                topup_package_id=reservation.topup_package_id,
                starts_subscription=reservation.starts_subscription,
            )

        stripe_customer_id = self._ensure_stripe_customer(reservation)
        params = self._checkout_params(
            reservation=reservation,
            package=package,
            stripe_customer_id=stripe_customer_id,
        )
        try:
            checkout_session = self._stripe_gateway.create_checkout_session(
                params=params,
                idempotency_key=reservation.checkout_request_id,
            )
        except StripeGatewayError as exc:
            raise BillingApiError(
                502,
                "stripe_checkout_unavailable",
                "Stripe Checkout is temporarily unavailable. Please try again.",
            ) from exc

        checkout_session_id = _required_string(
            checkout_session.get("id"),
            "Stripe Checkout Session id",
        )
        checkout_url = _required_string(
            checkout_session.get("url"),
            "Stripe Checkout Session URL",
        )
        self._persist_checkout_session(
            reservation=reservation,
            checkout_session_id=checkout_session_id,
            checkout_url=checkout_url,
        )
        return CheckoutSessionResult(
            checkout_session_id=checkout_session_id,
            checkout_url=checkout_url,
            topup_package_id=reservation.topup_package_id,
            starts_subscription=reservation.starts_subscription,
        )

    def _get_topup_package(self, package_id: str) -> TopupPackage:
        try:
            return self._catalog.get_topup_package(package_id)
        except BillingCatalogError as exc:
            raise BillingApiError(
                400,
                "unknown_topup_package",
                "The selected top-up package is not available.",
            ) from exc

    def _validate_checkout_urls(self) -> None:
        if not self._settings.checkout_success_url or not self._settings.checkout_cancel_url:
            raise BillingApiError(
                503,
                "checkout_return_urls_not_configured",
                "Billing Checkout is not configured yet.",
            )
        if "{CHECKOUT_SESSION_ID}" not in self._settings.checkout_success_url:
            raise BillingApiError(
                503,
                "checkout_success_url_invalid",
                "Billing Checkout is not configured yet.",
            )

    def _reserve_checkout(
        self,
        *,
        owner_uid: str,
        package: TopupPackage,
        now: datetime,
    ) -> CheckoutReservation:
        if self._settings.checkout_session_ttl_seconds < 1800:
            raise RuntimeError("BILLING_CHECKOUT_SESSION_TTL_SECONDS must be at least 1800.")
        billing_subject_id = _required_string(owner_uid, "owner_uid")
        billing_account_id = customer_billing_account_document_id(billing_subject_id)
        expires_at = now + timedelta(seconds=self._settings.checkout_session_ttl_seconds)
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            billing_account_id
        )

        def operation(transaction: Any) -> CheckoutReservation:
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            account_exists = account_snapshot.exists
            account = account_snapshot.to_dict() or {}
            if account_exists:
                self._validate_billing_account(
                    account,
                    billing_account_id=billing_account_id,
                    owner_uid=owner_uid,
                )
                active_expiry = account.get("active_checkout_expires_at")
                active_request_id = account.get("active_checkout_request_id")
                if (
                    isinstance(active_expiry, datetime)
                    and _as_utc(active_expiry) > now
                    and isinstance(active_request_id, str)
                    and active_request_id
                ):
                    active_package_id = account.get("active_checkout_topup_package_id")
                    if active_package_id == package.package_id and account.get("active_checkout_url"):
                        return CheckoutReservation(
                            billing_account_id=billing_account_id,
                            billing_subject_id=billing_subject_id,
                            owner_uid=owner_uid,
                            stripe_customer_id=_optional_string(account.get("stripe_customer_id")),
                            checkout_request_id=active_request_id,
                            topup_package_id=active_package_id,
                            starts_subscription=account.get("active_checkout_mode") == "subscription",
                            expires_at=_as_utc(active_expiry),
                            existing_checkout_session_id=_optional_string(
                                account.get("active_checkout_session_id")
                            ),
                            existing_checkout_url=_optional_string(account.get("active_checkout_url")),
                        )
                starts_subscription = self._starts_subscription(account)

            else:
                starts_subscription = True

            checkout_request_id = f"checkout-{uuid.uuid4().hex}"
            updates = {
                "active_checkout_request_id": checkout_request_id,
                "active_checkout_session_id": None,
                "active_checkout_url": None,
                "active_checkout_mode": "subscription" if starts_subscription else "payment",
                "active_checkout_topup_package_id": package.package_id,
                "active_checkout_created_at": now,
                "active_checkout_expires_at": expires_at,
                "updated_at": now,
            }
            if account_exists:
                transaction.update(account_ref, updates)
                stripe_customer_id = _optional_string(account.get("stripe_customer_id"))
            else:
                transaction.create(
                    account_ref,
                    {
                        **build_initial_billing_account_document(
                            billing_account_id=billing_account_id,
                            billing_subject_id=billing_subject_id,
                            owner_uid=owner_uid,
                            catalog_environment=self._catalog.environment,
                            created_at=now,
                        ),
                        **updates,
                    },
                )
                stripe_customer_id = None
            return CheckoutReservation(
                billing_account_id=billing_account_id,
                billing_subject_id=billing_subject_id,
                owner_uid=owner_uid,
                stripe_customer_id=stripe_customer_id,
                checkout_request_id=checkout_request_id,
                topup_package_id=package.package_id,
                starts_subscription=starts_subscription,
                expires_at=expires_at,
                existing_checkout_session_id=None,
                existing_checkout_url=None,
            )

        return self._transaction_runner(client, operation)

    def _ensure_stripe_customer(self, reservation: CheckoutReservation) -> str:
        if reservation.stripe_customer_id:
            return reservation.stripe_customer_id
        try:
            customer = self._stripe_gateway.create_customer(
                metadata={
                    "billing_account_id": reservation.billing_account_id,
                    "catalog_environment": self._catalog.environment,
                },
                idempotency_key=f"customer-{reservation.billing_account_id}",
            )
        except StripeGatewayError as exc:
            raise BillingApiError(
                502,
                "stripe_customer_unavailable",
                "Stripe customer setup is temporarily unavailable. Please try again.",
            ) from exc
        created_customer_id = _required_string(customer.get("id"), "Stripe Customer id")
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            reservation.billing_account_id
        )

        def operation(transaction: Any) -> str:
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            if not account_snapshot.exists:
                raise RuntimeError("Billing account disappeared during Stripe Customer creation.")
            account = account_snapshot.to_dict() or {}
            self._validate_billing_account(
                account,
                billing_account_id=reservation.billing_account_id,
                owner_uid=reservation.owner_uid,
            )
            existing_customer_id = _optional_string(account.get("stripe_customer_id"))
            if existing_customer_id:
                return existing_customer_id
            transaction.update(
                account_ref,
                {
                    "stripe_customer_id": created_customer_id,
                    "stripe_customer_status": "ready",
                    "updated_at": self._now_factory(),
                },
            )
            return created_customer_id

        return self._transaction_runner(client, operation)

    def _checkout_params(
        self,
        *,
        reservation: CheckoutReservation,
        package: TopupPackage,
        stripe_customer_id: str,
    ) -> dict[str, Any]:
        checkout_kind = (
            "initial_subscription_topup" if reservation.starts_subscription else "topup"
        )
        metadata = {
            "billing_account_id": reservation.billing_account_id,
            "catalog_environment": self._catalog.environment,
            "checkout_kind": checkout_kind,
            "topup_package_id": package.package_id,
        }
        line_items = [{"price": package.stripe_price_id, "quantity": 1}]
        mode = "payment"
        params: dict[str, Any] = {
            "mode": mode,
            "customer": stripe_customer_id,
            "client_reference_id": reservation.billing_account_id,
            "line_items": line_items,
            "success_url": self._settings.checkout_success_url,
            "cancel_url": self._settings.checkout_cancel_url,
            "expires_at": int(reservation.expires_at.timestamp()),
            "metadata": metadata,
        }
        if reservation.starts_subscription:
            mode = "subscription"
            line_items.append(
                {
                    "price": self._catalog.monthly_service_fee.stripe_price_id,
                    "quantity": 1,
                }
            )
            params["mode"] = mode
            params["subscription_data"] = {"metadata": metadata}
        else:
            params["payment_intent_data"] = {"metadata": metadata}
        return params

    def _persist_checkout_session(
        self,
        *,
        reservation: CheckoutReservation,
        checkout_session_id: str,
        checkout_url: str,
    ) -> None:
        client = self._firestore_client_factory()
        account_ref = client.collection(self._settings.billing_accounts_collection).document(
            reservation.billing_account_id
        )

        def operation(transaction: Any) -> None:
            account_snapshot = get_transaction_document_snapshot(transaction, account_ref)
            if not account_snapshot.exists:
                raise RuntimeError("Billing account disappeared during Checkout creation.")
            account = account_snapshot.to_dict() or {}
            self._validate_billing_account(
                account,
                billing_account_id=reservation.billing_account_id,
                owner_uid=reservation.owner_uid,
            )
            active_request_id = _optional_string(account.get("active_checkout_request_id"))
            if active_request_id != reservation.checkout_request_id:
                existing_session_id = _optional_string(account.get("active_checkout_session_id"))
                if existing_session_id == checkout_session_id:
                    return
                raise BillingApiError(
                    409,
                    "checkout_session_conflict",
                    "Another Checkout attempt is already active for this billing account.",
                )
            existing_session_id = _optional_string(account.get("active_checkout_session_id"))
            if existing_session_id and existing_session_id != checkout_session_id:
                raise RuntimeError("Billing account has a conflicting Stripe Checkout Session.")
            transaction.update(
                account_ref,
                {
                    "active_checkout_session_id": checkout_session_id,
                    "active_checkout_url": checkout_url,
                    "updated_at": self._now_factory(),
                },
            )

        self._transaction_runner(client, operation)

    def _starts_subscription(self, account: Mapping[str, Any]) -> bool:
        subscription_id = _optional_string(account.get("stripe_subscription_id"))
        status = str(account.get("stripe_subscription_status", "not_started"))
        if subscription_id is None:
            return True
        if status in {"active", "trialing"}:
            return False
        raise BillingApiError(
            403,
            "monthly_service_fee_required",
            "Your monthly service-fee subscription must be active before adding more credit.",
        )

    def _validate_billing_account(
        self,
        account: Mapping[str, Any],
        *,
        billing_account_id: str,
        owner_uid: str,
    ) -> None:
        if (
            account.get("billing_account_id") != billing_account_id
            or account.get("billing_subject_id") != owner_uid
            or account.get("owner_uid") != owner_uid
            or account.get("currency") != "USD"
            or account.get("catalog_environment") != self._catalog.environment
        ):
            raise BillingApiError(
                403,
                "billing_account_ownership_mismatch",
                "This billing account is not available for the authenticated user.",
            )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} is missing from a server-side Stripe response.")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


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
