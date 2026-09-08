"""Shared Pydantic schemas for the gateway and persistence worker."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.constants import (
    EVENT_TYPE_THREAD_DELETE_REQUESTED,
    EVENT_TYPE_TURN_COMPLETED,
    STATUS_COMPLETED,
)
from common.ids import validate_agent_id, validate_session_id, validate_thread_id


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    backend: Literal["gemini_managed", "agent_runtime", "cloud_run_adk"] = "gemini_managed"
    remote_agent_id: str | None = None
    max_total_tokens: int | None = Field(default=50000, ge=100)
    system_instruction: str | None = None
    api_key_secret_id: str | None = None
    environment: dict[str, Any] | str | None = None
    resource_name: str | None = None
    region: str = "us-central1"
    base_url: str | None = None
    app_name: str | None = None
    audience: str | None = None
    runtime_session_cleanup: Literal["gemini_managed", "agent_runtime", "cloud_run_adk", "none"] | None = None
    streaming_enabled: bool = True
    persistence_enabled: bool = True
    auth_policy: str = "firebase"
    model: str | None = "gemini-3.8-flash"

    @field_validator("agent_id")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        return validate_agent_id(value)

    @field_validator(
        "base_url",
        "app_name",
        "audience",
        "resource_name",
        "model",
        "remote_agent_id",
        "api_key_secret_id",
        "system_instruction",
    )
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_backend_config(self) -> "AgentConfig":
        if self.backend == "gemini_managed":
            if not self.remote_agent_id:
                self.remote_agent_id = "antigravity-preview-05-2026"
            if not self.model:
                self.model = "gemini-3.8-flash"
            if self.runtime_session_cleanup is None:
                self.runtime_session_cleanup = "none"
            return self

        if self.backend == "agent_runtime":
            if not self.resource_name:
                raise ValueError("resource_name is required for agent_runtime agents.")
            if self.runtime_session_cleanup is None:
                self.runtime_session_cleanup = "agent_runtime"
            return self

        if not self.base_url:
            raise ValueError("base_url is required for cloud_run_adk agents.")
        if not self.app_name:
            raise ValueError("app_name is required for cloud_run_adk agents.")
        self.base_url = self.base_url.rstrip("/")
        if self.runtime_session_cleanup is None:
            self.runtime_session_cleanup = "cloud_run_adk"
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20000)
    thread_id: str | None = None
    session_id: str | None = None
    client_turn_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message must not be empty.")
        return cleaned

    @field_validator("thread_id")
    @classmethod
    def validate_thread(cls, value: str | None) -> str | None:
        return validate_thread_id(value)

    @field_validator("session_id")
    @classmethod
    def validate_session(cls, value: str | None) -> str | None:
        return validate_session_id(value)

    @field_validator("client_turn_id")
    @classmethod
    def validate_client_turn_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if "/" in cleaned:
            raise ValueError("client_turn_id must not contain '/'.")
        return cleaned


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    agent_id: str
    thread_id: str
    session_id: str
    turn_id: str
    reply_text: str
    usage: dict[str, Any] = Field(default_factory=dict)


class ThreadLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ThreadLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    agent_id: str
    thread_id: str
    status: str
    runtime_session_status: str | None = None


class TurnCompletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = EVENT_TYPE_TURN_COMPLETED
    event_id: str
    turn_id: str
    agent_id: str
    user_id: str
    thread_id: str
    session_id: str
    user_message: str
    assistant_message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = STATUS_COMPLETED
    usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        return validate_agent_id(value)


class ThreadDeleteRequestedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = EVENT_TYPE_THREAD_DELETE_REQUESTED
    event_id: str
    agent_id: str
    agent_backend: Literal["gemini_managed", "agent_runtime", "cloud_run_adk"] = "gemini_managed"
    agent_region: str
    agent_resource_name: str | None = None
    agent_base_url: str | None = None
    agent_app_name: str | None = None
    agent_audience: str | None = None
    runtime_session_cleanup: Literal["gemini_managed", "agent_runtime", "cloud_run_adk", "none"] = "none"
    user_id: str
    thread_id: str
    session_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        return validate_agent_id(value)

    @field_validator("thread_id")
    @classmethod
    def validate_thread(cls, value: str) -> str:
        validated = validate_thread_id(value)
        if validated is None:
            raise ValueError("thread_id is required.")
        return validated

    @field_validator("session_id")
    @classmethod
    def validate_session(cls, value: str) -> str:
        validated = validate_session_id(value)
        if validated is None:
            raise ValueError("session_id is required.")
        return validated

    @field_validator("agent_base_url", "agent_app_name", "agent_audience")
    @classmethod
    def normalize_optional_delete_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_cleanup_config(self) -> "ThreadDeleteRequestedEvent":
        if self.runtime_session_cleanup == "agent_runtime" and not self.agent_resource_name:
            raise ValueError("agent_resource_name is required for Agent Runtime cleanup.")
        if self.runtime_session_cleanup == "cloud_run_adk":
            if not self.agent_base_url:
                raise ValueError("agent_base_url is required for Cloud Run ADK cleanup.")
            if not self.agent_app_name:
                raise ValueError("agent_app_name is required for Cloud Run ADK cleanup.")
            self.agent_base_url = self.agent_base_url.rstrip("/")
        return self
