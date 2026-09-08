"""Pub/Sub push envelope models."""

from __future__ import annotations

import base64
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PubSubMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: str
    messageId: str
    publishTime: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    def decode_json(self) -> dict[str, Any]:
        raw_bytes = base64.b64decode(self.data)
        return json.loads(raw_bytes.decode("utf-8"))


class PubSubPushEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: PubSubMessage
    subscription: str

