import asyncio
from datetime import datetime, timezone

import pytest

from common.billing import (
    customer_billing_period_document_id,
    customer_wallet_document_id,
)
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)

from tests.unit.test_wallet_reservations import FakeClient


@pytest.fixture(autouse=True)
def _reset_worker_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_transaction(client, operation):
    return operation(client.transaction())


def _enable_worker_billing(monkeypatch):
    monkeypatch.setenv("MONTHLY_SERVICE_FEE_NANOS", "5000000000")
    get_settings.cache_clear()


def _event():
    return TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
        created_at=datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
        metadata={
            "request_id": "req-1",
            "billing": {
                "reservation_id": "turn-1",
                "billing_subject_id": "user-1",
                "reserved_amount_nanos": 500_000_000,
                "currency": "USD",
            },
        },
    )


def _seed_reservation_and_ledger(client):
    settings = get_settings()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 500_000_000,
        "reserved_credit_nanos": 500_000_000,
        "settled_usage_nanos": 0,
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "request_id": "req-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "agent_id": "maxima",
        "currency": "USD",
        "reserved_amount_nanos": 500_000_000,
        "status": "reserved",
    }
    client.documents[(settings.billing_ledger_collection, "turn-1")] = {
        "turn_id": "turn-1",
        "uid": user_id,
        "billing_subject_id": user_id,
        "billing_reservation_id": "turn-1",
        "estimated_cost_usd_nanos": 100_000_000,
        "pricing_model": "gemini-2.5-flash",
        "pricing_version": "test-price-v1",
    }


def test_settlement_debits_actual_usage_releases_the_rest_and_is_idempotent(monkeypatch):
    _enable_worker_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    _seed_reservation_and_ledger(client)
    service = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    result = asyncio.run(service.settle_if_required(_event()))
    duplicate_result = asyncio.run(service.settle_if_required(_event()))

    assert result is not None
    assert result.status == "settled"
    assert result.settled_amount_nanos == 100_000_000
    assert result.released_amount_nanos == 400_000_000
    assert duplicate_result == result
    wallet = client.documents[(settings.wallets_collection, customer_wallet_document_id("user-1"))]
    assert wallet["available_credit_nanos"] == 900_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["settled_usage_nanos"] == 100_000_000
    wallet_transaction = client.documents[(settings.wallet_transactions_collection, "usage_turn-1")]
    assert wallet_transaction["amount_nanos"] == 100_000_000
    period_id = customer_billing_period_document_id("user-1", "2026-08")
    period = client.documents[(settings.customer_billing_periods_collection, period_id)]
    assert period["usage_estimated_nanos"] == 100_000_000
    assert period["monthly_service_fee_nanos"] == 5_000_000_000
    assert period["monthly_service_fee_status"] == "pending_collection"

