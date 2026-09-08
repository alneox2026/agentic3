from fastapi.testclient import TestClient

from services.billing_api_v3.app.api import routes_billing
from services.billing_api_v3.app.main import app
from services.billing_api_v3.app.services.checkout_service import CheckoutSessionResult
from services.billing_api_v3.app.services.webhook_service import WebhookResult


client = TestClient(app)


def test_authenticated_topup_route_sends_only_the_catalog_package_to_the_service(monkeypatch):
    observed = {}

    async def fake_authenticate_request(_request):
        return "user-1"

    class FakeCheckoutService:
        async def create_topup_checkout(self, *, owner_uid, topup_package_id):
            observed["owner_uid"] = owner_uid
            observed["topup_package_id"] = topup_package_id
            return CheckoutSessionResult(
                checkout_session_id="cs_test_123",
                checkout_url="https://checkout.stripe.test/session",
                topup_package_id=topup_package_id,
                starts_subscription=True,
            )

    monkeypatch.setattr(routes_billing, "authenticate_request", fake_authenticate_request)
    monkeypatch.setattr(routes_billing, "CheckoutService", FakeCheckoutService)

    response = client.post(
        "/v1/billing/topups/checkout-session",
        json={"topup_package_id": "credit_5_usd"},
    )

    assert response.status_code == 201
    assert observed == {"owner_uid": "user-1", "topup_package_id": "credit_5_usd"}
    assert response.json()["starts_monthly_service_subscription"] is True


def test_webhook_uses_the_untouched_raw_body_and_stripe_signature(monkeypatch):
    observed = {}

    class FakeWebhookService:
        async def handle(self, *, raw_payload, stripe_signature):
            observed["raw_payload"] = raw_payload
            observed["stripe_signature"] = stripe_signature
            return WebhookResult(
                stripe_event_id="evt_test_123",
                stripe_event_type="checkout.session.completed",
                outcome="topup_credited",
                duplicate=False,
            )

    monkeypatch.setattr(routes_billing, "StripeWebhookService", FakeWebhookService)
    payload = b'{"id":"evt_test_123", "type":"checkout.session.completed"}'

    response = client.post(
        "/v1/billing/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": "t=123,v1=signature", "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert observed == {"raw_payload": payload, "stripe_signature": "t=123,v1=signature"}
    assert response.json() == {"ok": True, "outcome": "topup_credited", "duplicate": False}
