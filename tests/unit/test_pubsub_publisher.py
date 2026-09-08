import asyncio
import json

from common.schemas import TurnCompletedEvent
from services.agent_gateway_v3.app.services.pubsub_publisher import PubSubPublisher


class FakePublishFuture:
    def result(self, timeout: float):
        assert timeout == 30.0
        return "msg-123"


class FakePublisherClient:
    def __init__(self) -> None:
        self.published = []

    def topic_path(self, project_id: str, topic_id: str) -> str:
        return f"projects/{project_id}/topics/{topic_id}"

    def publish(self, topic_path: str, data: bytes, **attributes):
        self.published.append(
            {
                "topic_path": topic_path,
                "data": json.loads(data.decode("utf-8")),
                "attributes": attributes,
            }
        )
        return FakePublishFuture()


def test_pubsub_publisher_publishes_completed_turn() -> None:
    publisher_client = FakePublisherClient()
    publisher = PubSubPublisher(publisher_client=publisher_client)
    event = TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )

    result = asyncio.run(publisher.publish_turn_completed(event))

    assert (
        publisher_client.published[0]["topic_path"]
        == "projects/ceo-dev123/topics/agent-turn-events-v3"
    )
    assert publisher_client.published[0]["data"]["event_id"] == "evt-1"
    assert publisher_client.published[0]["attributes"]["agent_id"] == "maxima"

