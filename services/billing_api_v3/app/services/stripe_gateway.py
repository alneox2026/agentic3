"""Thin, mockable adapter around Stripe's server-side Python SDK."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import os
from typing import Any, Protocol

from services.billing_api_v3.app.core.errors import BillingApiError


class StripeGateway(Protocol):
    """The only Stripe operations used by Billing API domain services."""

    def create_customer(self, *, metadata: dict[str, str], idempotency_key: str) -> Mapping[str, Any]: ...

    def create_checkout_session(
        self,
        *,
        params: dict[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def retrieve_checkout_session(self, checkout_session_id: str) -> Mapping[str, Any]: ...

    def retrieve_invoice(self, invoice_id: str) -> Mapping[str, Any]: ...

    def retrieve_subscription(self, subscription_id: str) -> Mapping[str, Any]: ...

    def construct_webhook_event(
        self,
        *,
        payload: bytes,
        signature: str,
        signing_secret: str,
        tolerance_seconds: int,
    ) -> Mapping[str, Any]: ...


class StripeGatewayError(RuntimeError):
    """An upstream Stripe failure that should not expose provider internals."""


def stripe_object_to_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize Stripe SDK objects to ordinary mappings at this boundary."""

    if isinstance(value, Mapping):
        return value
    converter = getattr(value, "to_dict_recursive", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return converted
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return converted
    raise StripeGatewayError("Stripe returned an unexpected response shape.")


class StripeSdkGateway:
    """StripeClient implementation using request idempotency and two retries."""

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise BillingApiError(
                503,
                "stripe_not_configured",
                "Billing payments are not configured yet.",
            )
        try:
            from stripe import StripeClient
        except ModuleNotFoundError as exc:
            raise BillingApiError(
                500,
                "stripe_sdk_missing",
                "The Stripe SDK is not installed in the Billing API runtime.",
            ) from exc
        self._client = StripeClient(secret_key, max_network_retries=2)

    def create_customer(
        self,
        *,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.v1.customers.create(
                    params={"metadata": metadata},
                    options={"idempotency_key": idempotency_key},
                )
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe Customer creation failed.") from exc

    def create_checkout_session(
        self,
        *,
        params: dict[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.v1.checkout.sessions.create(
                    params=params,
                    options={"idempotency_key": idempotency_key},
                )
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe Checkout Session creation failed.") from exc

    def retrieve_checkout_session(self, checkout_session_id: str) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.v1.checkout.sessions.retrieve(
                    checkout_session_id,
                    params={"expand": ["line_items.data.price"]},
                )
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe Checkout Session retrieval failed.") from exc

    def retrieve_invoice(self, invoice_id: str) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.v1.invoices.retrieve(
                    invoice_id,
                    params={"expand": ["lines.data.price"]},
                )
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe invoice retrieval failed.") from exc

    def retrieve_subscription(self, subscription_id: str) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.v1.subscriptions.retrieve(subscription_id)
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe subscription retrieval failed.") from exc

    def construct_webhook_event(
        self,
        *,
        payload: bytes,
        signature: str,
        signing_secret: str,
        tolerance_seconds: int,
    ) -> Mapping[str, Any]:
        try:
            return stripe_object_to_mapping(
                self._client.construct_event(
                    payload,
                    signature,
                    signing_secret,
                    tolerance=tolerance_seconds,
                )
            )
        except Exception as exc:
            raise StripeGatewayError("Stripe webhook signature verification failed.") from exc


@lru_cache(maxsize=1)
def get_stripe_gateway() -> StripeGateway:
    return StripeSdkGateway(os.getenv("STRIPE_SECRET_KEY", "").strip())
