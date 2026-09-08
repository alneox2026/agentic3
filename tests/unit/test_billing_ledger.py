from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.services.billing_ledger import (
    BillingLedgerRepository,
)


class FakeDocument:
    def __init__(self, collection: str, document_id: str) -> None:
        self.collection = collection
        self.document_id = document_id


class FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self.name, document_id)


class FakeClient:
    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(name)


class FakeBatch:
    def __init__(self) -> None:
        self.creates = []

    def create(self, document: FakeDocument, payload: dict) -> None:
        self.creates.append((document, payload))


def _event(usage: dict) -> TurnCompletedEvent:
    return TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
        usage=usage,
    )


def test_billing_ledger_creates_immutable_priced_entry() -> None:
    batch = FakeBatch()
    repository = BillingLedgerRepository(collection="agent_billing_ledger")

    repository.add_create_to_batch(
        batch,
        FakeClient(),
        _event(
            {
                "pricing_model": "gemini-2.5-flash",
                "pricing_unit": "usd_per_1m_tokens",
                "pricing": {"input_text_image_video": 0.3},
                "billable_tokens": {"input_text_image_video": 147},
                "token_counts": {"total_token_count": 201},
                "estimated_cost_usd": 0.0001791,
            }
        ),
    )

    document, payload = batch.creates[0]
    assert document.collection == "agent_billing_ledger"
    assert document.document_id == "turn-1"
    assert payload["cost_status"] == "estimated"
    assert payload["estimated_cost_usd_nanos"] == 179100
    assert payload["pricing_model"] == "gemini-2.5-flash"


def test_billing_ledger_records_unavailable_cost_without_guessing() -> None:
    batch = FakeBatch()
    repository = BillingLedgerRepository(collection="agent_billing_ledger")

    repository.add_create_to_batch(batch, FakeClient(), _event({"total_token_count": 200}))

    _, payload = batch.creates[0]
    assert payload["cost_status"] == "unavailable"
    assert payload["estimated_cost_usd"] is None
    assert payload["estimated_cost_usd_nanos"] is None
