import asyncio
from datetime import datetime, timezone

import pytest

from common.billing import customer_wallet_document_id
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.wallet_reservations import (
    WalletReservationService,
)


@pytest.fixture(autouse=True)
def _reset_gateway_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client, collection, document_id):
        self.client = client
        self.collection = collection
        self.document_id = document_id

    @property
    def key(self):
        return self.collection, self.document_id

    def get(self):
        return FakeSnapshot(self.client.documents.get(self.key))


class FakeCollectionReference:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection

    def document(self, document_id):
        return FakeDocumentReference(self.client, self.collection_name, document_id)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def get(self, document_ref):
        return FakeSnapshot(self.client.documents.get(document_ref.key))

    def create(self, document_ref, data):
        if document_ref.key in self.client.documents:
            raise RuntimeError("duplicate create")
        self.client.documents[document_ref.key] = dict(data)

    def update(self, document_ref, updates):
        self.client.documents[document_ref.key].update(updates)


class FakeClient:
    def __init__(self):
        self.documents = {}

    def collection(self, collection):
        return FakeCollectionReference(self, collection)

    def transaction(self):
        return FakeTransaction(self)


def _run_transaction(client, operation):
    return operation(client.transaction())


def _enable_billing(monkeypatch):
    monkeypatch.setenv("BILLING_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("BILLING_RESERVATION_NANOS", "500000000")
    monkeypatch.setenv("BILLING_RESERVATION_TTL_SECONDS", "3600")
    get_settings.cache_clear()


def _wallet(user_id, available_credit_nanos=1_000_000_000):
    return {
        "schema_version": 1,
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": available_credit_nanos,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def test_reserve_moves_credit_to_a_per_turn_hold(monkeypatch):
    _enable_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = _wallet(user_id)
    service = WalletReservationService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    reservation = asyncio.run(
        service.reserve(
            user_id=user_id,
            agent_id="maxima",
            request_id="req-1",
            turn_id="turn-1",
        )
    )

    assert reservation is not None
    assert reservation.reserved_amount_nanos == 500_000_000
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 500_000_000
    assert wallet["reserved_credit_nanos"] == 500_000_000
    reservation_doc = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert reservation_doc["status"] == "reserved"
    assert reservation_doc["request_id"] == "req-1"


def test_reserve_rejects_insufficient_credit_before_model_execution(monkeypatch):
    _enable_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = _wallet(
        user_id,
        available_credit_nanos=499_999_999,
    )
    service = WalletReservationService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            service.reserve(
                user_id=user_id,
                agent_id="maxima",
                request_id="req-1",
                turn_id="turn-1",
            )
        )

    assert exc_info.value.status_code == 402
    assert exc_info.value.code == "insufficient_credit"
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 499_999_999
    assert wallet["reserved_credit_nanos"] == 0

