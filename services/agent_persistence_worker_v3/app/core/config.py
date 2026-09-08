"""Typed configuration for the persistence worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class WorkerSettings:
    project_id: str
    threads_collection: str
    messages_subcollection: str
    idempotency_collection: str
    billing_ledger_collection: str
    wallets_collection: str
    billing_reservations_collection: str
    wallet_transactions_collection: str
    customer_billing_periods_collection: str
    monthly_service_fee_nanos: int
    billing_reconciliation_batch_size: int
    runtime_delete_timeout_seconds: float
    log_level: str
    eventarc_auth_required: bool
    eventarc_allowed_service_account: str
    eventarc_audience: str


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings(
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", "ceo-dev123").strip() or "ceo-dev123",
        threads_collection=os.getenv("FIRESTORE_THREADS_COLLECTION", "agent_threads_v3").strip() or "agent_threads_v3",
        messages_subcollection=os.getenv(
            "FIRESTORE_MESSAGES_SUBCOLLECTION",
            "messages_v3",
        ).strip()
        or "messages_v3",

        idempotency_collection=os.getenv("FIRESTORE_IDEMPOTENCY_COLLECTION", "processed_events_v3").strip()
        or "processed_events_v3",
        billing_ledger_collection=os.getenv(
            "FIRESTORE_BILLING_LEDGER_COLLECTION",
            "agent_billing_ledger_v3",
        ).strip()
        or "agent_billing_ledger_v3",
        wallets_collection=os.getenv(
            "FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
            "customer_wallets_v3",
        ).strip()
        or "customer_wallets_v3",
        billing_reservations_collection=os.getenv(
            "FIRESTORE_BILLING_RESERVATIONS_COLLECTION",
            "billing_reservations_v3",
        ).strip()
        or "billing_reservations_v3",
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

        monthly_service_fee_nanos=int(
            os.getenv("MONTHLY_SERVICE_FEE_NANOS", "5000000000")
        ),
        billing_reconciliation_batch_size=int(
            os.getenv("BILLING_RECONCILIATION_BATCH_SIZE", "100")
        ),
        runtime_delete_timeout_seconds=float(os.getenv("RUNTIME_DELETE_TIMEOUT_SECONDS", "30")),
        log_level=os.getenv("WORKER_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        eventarc_auth_required=_parse_bool(
            os.getenv("WORKER_REQUIRE_EVENTARC_AUTH"),
            default=False,
        ),
        eventarc_allowed_service_account=os.getenv(
            "WORKER_EVENTARC_ALLOWED_SERVICE_ACCOUNT",
            "",
        ).strip(),
        eventarc_audience=os.getenv("WORKER_EVENTARC_AUDIENCE", "").strip(),
    )
