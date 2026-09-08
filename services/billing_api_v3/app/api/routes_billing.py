"""Private-payment-backed endpoints exposed to the authenticated FlutterFlow app."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from services.billing_api_v3.app.core.auth import authenticate_request
from services.billing_api_v3.app.core.errors import BillingApiError
from services.billing_api_v3.app.services.checkout_service import CheckoutService
from services.billing_api_v3.app.services.webhook_service import StripeWebhookService


router = APIRouter(prefix="/v1/billing", tags=["billing"])
_MAX_STRIPE_WEBHOOK_BYTES = 1_000_000


class CreateTopupCheckoutRequest(BaseModel):
    """The client chooses only a server-catalog package identifier."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topup_package_id: str = Field(min_length=3, max_length=64)


class CreateTopupCheckoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    checkout_session_id: str
    checkout_url: str
    topup_package_id: str
    starts_monthly_service_subscription: bool


class StripeWebhookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    outcome: str
    duplicate: bool


@router.post(
    "/topups/checkout-session",
    response_model=CreateTopupCheckoutResponse,
    status_code=201,
)
async def create_topup_checkout_session(
    payload: CreateTopupCheckoutRequest,
    request: Request,
) -> CreateTopupCheckoutResponse:
    """Create or return the caller's one active server-owned Checkout Session."""

    owner_uid = await authenticate_request(request)
    result = await CheckoutService().create_topup_checkout(
        owner_uid=owner_uid,
        topup_package_id=payload.topup_package_id,
    )
    return CreateTopupCheckoutResponse(
        checkout_session_id=result.checkout_session_id,
        checkout_url=result.checkout_url,
        topup_package_id=result.topup_package_id,
        starts_monthly_service_subscription=result.starts_subscription,
    )


@router.post(
    "/stripe/webhook",
    response_model=StripeWebhookResponse,
    include_in_schema=False,
)
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> StripeWebhookResponse:
    """Verify the Stripe signature over the untouched HTTP request body."""

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_STRIPE_WEBHOOK_BYTES:
        raise BillingApiError(413, "stripe_webhook_too_large", "Stripe webhook payload is too large.")
    raw_payload = await request.body()
    if len(raw_payload) > _MAX_STRIPE_WEBHOOK_BYTES:
        raise BillingApiError(413, "stripe_webhook_too_large", "Stripe webhook payload is too large.")
    result = await StripeWebhookService().handle(
        raw_payload=raw_payload,
        stripe_signature=stripe_signature,
    )
    return StripeWebhookResponse(outcome=result.outcome, duplicate=result.duplicate)
