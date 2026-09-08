"""Typed configuration for the Billing API service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class BillingApiSettings:
    project_id: str
    region: str
    log_level: str
    allowed_origins: list[str]
    catalog_path: Path
    billing_accounts_collection: str
    stripe_webhook_events_collection: str
    wallets_collection: str
    wallet_transactions_collection: str
    customer_billing_periods_collection: str
    checkout_success_url: str
    checkout_cancel_url: str
    checkout_session_ttl_seconds: int
    stripe_webhook_tolerance_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> BillingApiSettings:
    catalog_path = Path(
        os.getenv("BILLING_CATALOG_PATH", "config/billing.test.yaml").strip()
        or "config/billing.test.yaml"
    ).resolve()
    return BillingApiSettings(
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", "ceo-dev123").strip() or "ceo-dev123",
        region=os.getenv("GOOGLE_CLOUD_REGION", "us-central1").strip() or "us-central1",
        log_level=os.getenv("BILLING_API_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        allowed_origins=_parse_csv(os.getenv("BILLING_ALLOWED_ORIGINS")),
        catalog_path=catalog_path,
        billing_accounts_collection=os.getenv(
            "FIRESTORE_CUSTOMER_BILLING_ACCOUNTS_COLLECTION",
            "customer_billing_accounts_v3",
        ).strip()
        or "customer_billing_accounts_v3",
        stripe_webhook_events_collection=os.getenv(
            "FIRESTORE_STRIPE_WEBHOOK_EVENTS_COLLECTION",
            "stripe_webhook_events_v3",
        ).strip()
        or "stripe_webhook_events_v3",
        wallets_collection=os.getenv(
            "FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
            "customer_wallets_v3",
        ).strip()
        or "customer_wallets_v3",
        wallet_transactions_collection=os.getenv(
            "FIRESTORE_WALLET_TRANSACTIONS_COLLECTION",
            "wallet_transactions_v3",
        ).strip()
        or "wallet_transactions_v3",
        customer_billing_periods_collection=os.getenv(
            "FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION",
            "customer_billing_periods_v3",
        ).strip()
        or "customer_billing_periods_v3",

        checkout_success_url=os.getenv("BILLING_CHECKOUT_SUCCESS_URL", "").strip(),
        checkout_cancel_url=os.getenv("BILLING_CHECKOUT_CANCEL_URL", "").strip(),
        checkout_session_ttl_seconds=int(
            os.getenv("BILLING_CHECKOUT_SESSION_TTL_SECONDS", "1800")
        ),
        stripe_webhook_tolerance_seconds=int(
            os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300")
        ),
    )
