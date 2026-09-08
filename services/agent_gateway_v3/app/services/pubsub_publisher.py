"""Pub/Sub publishing for completed turn events."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from common.schemas import ThreadDeleteRequestedEvent, TurnCompletedEvent
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError


_publisher_singleton: "PubSubPublisher | None" = None
_publisher_lock = asyncio.Lock()
_publisher_client: Any | None = None
_publisher_client_lock = threading.Lock()


@dataclass(frozen=True)
class PublishResult:
    message_id: str


def _default_publisher_client():
    global _publisher_client
    if _publisher_client is None:
        with _publisher_client_lock:
            if _publisher_client is None:
                from google.cloud import pubsub_v1

                _publisher_client = pubsub_v1.PublisherClient()
    return _publisher_client


class PubSubPublisher:
    def __init__(self, publisher_client: Any | None = None) -> None:
        self._publisher_client = publisher_client
        self._settings = get_settings()

    async def publish_turn_completed(self, event: TurnCompletedEvent) -> PublishResult:
        return await self.publish_event(
            event,
            attributes={
                "event_type": event.event_type,
                "agent_id": event.agent_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
            },
        )

    async def publish_thread_delete_requested(
        self,
        event: ThreadDeleteRequestedEvent,
    ) -> PublishResult:
        return await self.publish_event(
            event,
            attributes={
                "event_type": event.event_type,
                "agent_id": event.agent_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
            },
        )

    async def publish_event(
        self,
        event: TurnCompletedEvent | ThreadDeleteRequestedEvent,
        *,
        attributes: dict[str, str],
    ) -> PublishResult:
        publisher_client = self._publisher_client or await asyncio.to_thread(
            _default_publisher_client
        )
        topic_path = self._topic_path(publisher_client)
        payload = event.model_dump_json().encode("utf-8")

        try:
            publish_future = publisher_client.publish(
                topic_path,
                payload,
                **attributes,
            )
            message_id = await asyncio.to_thread(
                publish_future.result,
                self._settings.pubsub_publish_timeout_seconds,
            )
        except ModuleNotFoundError as exc:
            raise ApiError(
                500,
                "pubsub_library_missing",
                "google-cloud-pubsub is not installed in the gateway runtime environment.",
                {"reason": str(exc)},
            ) from exc
        except Exception as exc:
            raise ApiError(
                502,
                "pubsub_publish_failed",
                "The gateway could not publish the completed turn event.",
                {"reason": str(exc), "topic": topic_path},
            ) from exc

        return PublishResult(message_id=str(message_id))

    def _topic_path(self, publisher_client: Any) -> str:
        topic = self._settings.pubsub_topic
        if topic.startswith("projects/") and "/topics/" in topic:
            return topic
        return publisher_client.topic_path(self._settings.project_id, topic)


async def get_pubsub_publisher() -> PubSubPublisher:
    global _publisher_singleton
    if _publisher_singleton is None:
        async with _publisher_lock:
            if _publisher_singleton is None:
                _publisher_singleton = PubSubPublisher()
    return _publisher_singleton


async def close_pubsub_publisher() -> None:
    global _publisher_client
    global _publisher_singleton
    if _publisher_client is not None and hasattr(_publisher_client, "stop"):
        await asyncio.to_thread(_publisher_client.stop)
    _publisher_client = None
    _publisher_singleton = None
