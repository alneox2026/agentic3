import base64
import json

from services.agent_persistence_worker_v3.app.models.pubsub import PubSubPushEnvelope


def test_pubsub_envelope_decodes_json_payload() -> None:
    encoded = base64.b64encode(json.dumps({"event_id": "evt-1"}).encode("utf-8")).decode("utf-8")
    envelope = PubSubPushEnvelope(
        message={"data": encoded, "messageId": "msg-1"},
        subscription="projects/x/subscriptions/y",
    )
    assert envelope.message.decode_json()["event_id"] == "evt-1"

