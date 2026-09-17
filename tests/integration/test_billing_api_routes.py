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


def test_reconcile_route_fails_closed_when_audience_is_empty(monkeypatch):
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    response = client.post(
        "/v1/billing/internal/cancellation/reconcile",
        headers={"Authorization": "Bearer fake_token"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "reconciliation_auth_misconfigured"


def test_reconcile_route_fails_closed_when_service_account_is_empty(monkeypatch):
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "")

    response = client.post(
        "/v1/billing/internal/cancellation/reconcile",
        headers={"Authorization": "Bearer fake_token"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "reconciliation_auth_misconfigured"


def test_reconcile_route_rejects_missing_authorization_header(monkeypatch):
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    response = client.post("/v1/billing/internal/cancellation/reconcile")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_reconciliation_token"


def test_reconcile_route_rejects_invalid_bearer_token(monkeypatch):
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    response = client.post(
        "/v1/billing/internal/cancellation/reconcile",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_authorization_header"


def test_reconcile_route_rejects_wrong_audience(monkeypatch):
    from services.billing_api_v3.app.core import auth
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    auth.set_reconciliation_token_verifier(
        lambda token, aud: {"aud": "https://wrong-audience.run.app", "email": "reconciler@proj.iam.gserviceaccount.com"}
    )
    try:
        response = client.post(
            "/v1/billing/internal/cancellation/reconcile",
            headers={"Authorization": "Bearer fake_token"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_reconciliation_audience"
    finally:
        auth.set_reconciliation_token_verifier(None)


def test_reconcile_route_rejects_wrong_service_account(monkeypatch):
    from services.billing_api_v3.app.core import auth
    from services.billing_api_v3.app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    auth.set_reconciliation_token_verifier(
        lambda token, aud: {"aud": "https://expected-billing-api.run.app", "email": "attacker@proj.iam.gserviceaccount.com"}
    )
    try:
        response = client.post(
            "/v1/billing/internal/cancellation/reconcile",
            headers={"Authorization": "Bearer fake_token"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_service_account"
    finally:
        auth.set_reconciliation_token_verifier(None)


def test_reconcile_route_accepts_valid_oidc_token(monkeypatch):
    from services.billing_api_v3.app.core import auth
    from services.billing_api_v3.app.core.config import get_settings
    from services.billing_api_v3.app.services.cancellation_reconciliation import CancellationReconciliationResult
    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_RECONCILIATION_REQUIRE_AUTH", "true")
    monkeypatch.setenv("BILLING_RECONCILIATION_AUDIENCE", "https://expected-billing-api.run.app")
    monkeypatch.setenv("BILLING_RECONCILIATION_ALLOWED_SERVICE_ACCOUNT", "reconciler@proj.iam.gserviceaccount.com")

    auth.set_reconciliation_token_verifier(
        lambda token, aud: {"aud": "https://expected-billing-api.run.app", "email": "reconciler@proj.iam.gserviceaccount.com"}
    )

    class FakeReconciliationService:
        async def reconcile_intents(self, *, batch_size=50):
            return CancellationReconciliationResult(
                scanned_intents=3,
                resolved_intents=1,
                completed_cancellations=1,
                failed_cancellations=0,
                skipped_intents=1,
            )

    monkeypatch.setattr(routes_billing, "CancellationReconciliationService", FakeReconciliationService)

    try:
        response = client.post(
            "/v1/billing/internal/cancellation/reconcile",
            headers={"Authorization": "Bearer valid_oidc_token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["scanned_intents"] == 3
        assert data["resolved_intents"] == 1
        assert data["completed_cancellations"] == 1
        assert data["failed_cancellations"] == 0
        assert data["skipped_intents"] == 1
    finally:
        auth.set_reconciliation_token_verifier(None)

