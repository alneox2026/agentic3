"""Async Google Gemini Interactions API / Managed Agents client for the gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import logging
import os
from typing import Any

import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import httpx

from common.ids import new_session_id, new_thread_id
from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.backend_types import (
    BufferedAgentResponse,
    SessionResult,
    UpstreamStreamEvent,
)
from services.agent_gateway_v3.app.services.firestore_client import get_firestore_client
from services.agent_gateway_v3.app.services.thread_repository import ThreadRepository

logger = logging.getLogger(__name__)

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

_client_singleton: "GeminiManagedClient | None" = None
_client_lock = asyncio.Lock()


class GeminiManagedClient:
    """Production client for Gemini Managed & Antigravity agents via the Interactions API."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 600.0,  # 10 minutes for long autonomous agentic runs
        http_client: httpx.AsyncClient | None = None,
        thread_repository: ThreadRepository | None = None,
    ) -> None:
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self._thread_repository = thread_repository or ThreadRepository()
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=30.0,
                pool=connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        self._credentials: Credentials | None = None

    async def close(self) -> None:
        """Closes the underlying HTTP client session."""
        await self._http_client.aclose()

    async def ensure_session(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        request: ChatRequest,
    ) -> SessionResult:
        """Ensures a session and thread exist for the managed agent conversation."""
        thread_id = request.thread_id or new_thread_id()
        session_id = request.session_id or thread_id
        return SessionResult(session_id=session_id, thread_id=thread_id)

    async def chat_buffered_query(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        session_id: str,
        message: str,
        thread_id: str | None = None,
    ) -> BufferedAgentResponse:
        """Executes a non-streaming interaction with the managed agent."""
        headers = await self._get_auth_headers()
        url = f"{GEMINI_API_BASE_URL}/interactions"

        target_thread_id = thread_id or session_id
        state = await self._lookup_thread_state(target_thread_id, user_id, agent_config.agent_id)
        payload = self._build_interaction_payload(
            agent_config=agent_config,
            message=message,
            stream=False,
            previous_interaction_id=state.get("last_interaction_id"),
            environment_id=state.get("environment_id"),
        )

        response = await self._send_request(url, headers, payload)
        data = response.json()

        interaction = data.get("interaction", data)
        interaction_id = interaction.get("id")
        env_id = interaction.get("environment_id") or interaction.get("environment")
        if interaction_id and target_thread_id:
            await self._save_thread_state(target_thread_id, interaction_id, env_id)

        reply_text = self._extract_text_from_interaction(interaction)
        usage = interaction.get("usage") or {}

        return BufferedAgentResponse(
            reply_text=reply_text,
            raw_events=[data],
            usage=usage,
        )

    async def stream_chat_events(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        session_id: str,
        message: str,
        thread_id: str | None = None,
    ) -> AsyncIterator[UpstreamStreamEvent]:
        """Streams real-time SSE events from the Gemini Interactions API."""
        headers = await self._get_auth_headers()
        url = f"{GEMINI_API_BASE_URL}/interactions"

        target_thread_id = thread_id or session_id
        state = await self._lookup_thread_state(target_thread_id, user_id, agent_config.agent_id)
        payload = self._build_interaction_payload(
            agent_config=agent_config,
            message=message,
            stream=True,
            previous_interaction_id=state.get("last_interaction_id"),
            environment_id=state.get("environment_id"),
        )

        headers["Accept"] = "text/event-stream"

        try:
            async with self._http_client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    raise ApiError(
                        response.status_code,
                        "managed_agent_upstream_error",
                        f"Gemini Interactions API returned error {response.status_code}: {error_text}",
                        {"status_code": response.status_code, "detail": error_text, "reason": error_text},
                    )

                event_name: str | None = None
                data_buffer: list[str] = []

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        if data_buffer:
                            data_str = "\n".join(data_buffer)
                            parsed_payload = self._parse_sse_data(data_str)
                            if parsed_payload is not None:
                                # Capture thread state if interaction completes
                                interaction = parsed_payload.get("interaction", parsed_payload)
                                if isinstance(interaction, dict):
                                    interaction_id = interaction.get("id")
                                    env_id = interaction.get("environment_id") or interaction.get("environment")
                                    if interaction_id and target_thread_id:
                                        await self._save_thread_state(target_thread_id, interaction_id, env_id)

                                yield UpstreamStreamEvent(
                                    event_name=event_name or "message",
                                    payload=parsed_payload,
                                )
                            data_buffer = []
                            event_name = None
                        continue

                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buffer.append(line[5:].strip())

                # Flush remaining buffer if any
                if data_buffer:
                    data_str = "\n".join(data_buffer)
                    parsed_payload = self._parse_sse_data(data_str)
                    if parsed_payload is not None:
                        yield UpstreamStreamEvent(
                            event_name=event_name or "message",
                            payload=parsed_payload,
                        )

        except httpx.TimeoutException as exc:
            raise ApiError(
                504,
                "managed_agent_timeout",
                "Gemini Managed Agent timed out while generating a response.",
                {"detail": str(exc)},
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                502,
                "managed_agent_connection_error",
                "Failed to connect to Google Gemini Interactions API.",
                {"detail": str(exc)},
            ) from exc

    def extract_text_fragments(self, event_payload: dict[str, object]) -> list[str]:
        """Extracts text fragments from Gemini stream event payloads."""
        fragments: list[str] = []
        if not isinstance(event_payload, dict):
            return fragments

        # Direct text or output field
        if "text" in event_payload and isinstance(event_payload["text"], str):
            fragments.append(event_payload["text"])
        elif "output_text" in event_payload and isinstance(event_payload["output_text"], str):
            fragments.append(event_payload["output_text"])

        # Delta shape (e.g. step.delta event: {"delta": {"text": "..."}})
        delta = event_payload.get("delta")
        if isinstance(delta, dict):
            if "text" in delta and isinstance(delta["text"], str):
                fragments.append(delta["text"])
            elif "output" in delta and isinstance(delta["output"], str):
                fragments.append(delta["output"])

        # Standard Gemini Candidates shape (if present)
        candidates = event_payload.get("candidates")
        if isinstance(candidates, list):
            for cand in candidates:
                if isinstance(cand, dict):
                    content = cand.get("content")
                    if isinstance(content, dict):
                        parts = content.get("parts")
                        if isinstance(parts, list):
                            for part in parts:
                                if isinstance(part, dict) and "text" in part:
                                    fragments.append(str(part["text"]))

        # Nested interaction wrapper (e.g. interaction.completed or full interaction payload)
        interaction = event_payload.get("interaction")
        if isinstance(interaction, dict):
            if "output_text" in interaction and isinstance(interaction["output_text"], str):
                fragments.append(interaction["output_text"])
            elif "text" in interaction and isinstance(interaction["text"], str):
                fragments.append(interaction["text"])
            outputs = interaction.get("outputs")
            if isinstance(outputs, list):
                for out in outputs:
                    if isinstance(out, dict) and "text" in out and isinstance(out["text"], str):
                        fragments.append(out["text"])

        # Step wrapper (e.g. step.completed or step delta)
        step = event_payload.get("step")
        if isinstance(step, dict):
            if "output_text" in step and isinstance(step["output_text"], str):
                fragments.append(step["output_text"])
            elif "text" in step and isinstance(step["text"], str):
                fragments.append(step["text"])
            step_delta = step.get("delta")
            if isinstance(step_delta, dict) and "text" in step_delta and isinstance(step_delta["text"], str):
                fragments.append(step_delta["text"])

        # Outputs list directly on event_payload
        outputs = event_payload.get("outputs")
        if isinstance(outputs, list):
            for out in outputs:
                if isinstance(out, dict) and "text" in out and isinstance(out["text"], str):
                    fragments.append(out["text"])

        return fragments

    def _build_interaction_payload(
        self,
        *,
        agent_config: AgentConfig,
        message: str,
        stream: bool,
        previous_interaction_id: str | None = None,
        environment_id: str | None = None,
    ) -> dict[str, Any]:
        """Constructs the JSON request body for POST /v1beta/interactions."""
        agent_identifier = agent_config.remote_agent_id or "antigravity-preview-05-2026"
        model_name = agent_config.model or "gemini-3.8-flash"
        max_tokens = agent_config.max_total_tokens or 300000

        payload: dict[str, Any] = {
            "agent": agent_identifier,
            "input": message,
            "stream": stream,
            "agent_config": {
                "type": "antigravity",
                "model": model_name,
                "max_total_tokens": max_tokens,
            },
        }

        # Thread continuation: preserve state, files, and workspace
        if previous_interaction_id:
            payload["previous_interaction_id"] = previous_interaction_id
        if environment_id:
            payload["environment"] = environment_id
        elif agent_config.environment:
            payload["environment"] = agent_config.environment
        else:
            payload["environment"] = "remote"

        if agent_config.system_instruction:
            payload["system_instruction"] = agent_config.system_instruction

        return payload

    async def _get_auth_headers(self) -> dict[str, str]:
        """Resolves API key or OAuth Bearer token for the request."""
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            return {
                "x-goog-api-key": api_key.strip(),
                "Content-Type": "application/json",
            }

        # Fallback to Google Application Default Credentials
        token = await asyncio.to_thread(self._get_google_oauth_token)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _get_google_oauth_token(self) -> str:
        if not self._credentials:
            credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
            self._credentials = credentials
        if not self._credentials.valid:
            self._credentials.refresh(GoogleAuthRequest())
        return str(self._credentials.token)

    async def _send_request(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        """Sends an HTTP request with retry logic for transient 429/503 errors."""
        max_retries = 3
        backoff = 1.0

        for attempt in range(max_retries):
            try:
                response = await self._http_client.post(url, headers=headers, json=payload)
                if response.status_code in {429, 503} and attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue

                if response.status_code != 200:
                    raise ApiError(
                        response.status_code,
                        "managed_agent_upstream_error",
                        f"Gemini Interactions API error {response.status_code}: {response.text}",
                        {"status_code": response.status_code, "detail": response.text, "reason": response.text},
                    )
                return response
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise ApiError(
                    504,
                    "managed_agent_connection_failure",
                    f"Failed connecting to Gemini Interactions API after {max_retries} attempts: {exc}",
                ) from exc

        raise ApiError(500, "managed_agent_unknown_error", "Unexpected error during interaction request.")

    def _parse_sse_data(self, data_str: str) -> dict[str, Any] | None:
        if not data_str or data_str == "[DONE]":
            return None
        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return {"text": data_str}

    def _extract_text_from_interaction(self, interaction: dict[str, Any]) -> str:
        if "output_text" in interaction:
            return str(interaction["output_text"])
        if "text" in interaction:
            return str(interaction["text"])
        outputs = interaction.get("outputs", [])
        if isinstance(outputs, list):
            texts = [str(o.get("text", "")) for o in outputs if isinstance(o, dict) and "text" in o]
            if texts:
                return "\n".join(texts)
        return ""

    async def _lookup_thread_state(
        self,
        thread_id: str | None,
        user_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Looks up the last interaction and environment ID from the thread in Firestore."""
        if not thread_id:
            return {}
        try:
            client = get_firestore_client()
            thread = await asyncio.to_thread(
                self._thread_repository.get_thread,
                client,
                thread_id,
            )
            if thread and str(thread.get("uid")) == user_id:
                return {
                    "last_interaction_id": thread.get("last_interaction_id"),
                    "environment_id": thread.get("environment_id"),
                }
        except Exception as exc:
            logger.warning("Failed to lookup thread state for thread_id=%s: %s", thread_id, exc)
        return {}

    async def _save_thread_state(
        self,
        thread_id: str,
        interaction_id: str,
        environment_id: str | None,
    ) -> None:
        """Persists the interaction ID and environment ID into Firestore for the thread."""
        try:
            client = get_firestore_client()
            await asyncio.to_thread(
                self._thread_repository.update_interaction_state,
                client,
                thread_id=thread_id,
                interaction_id=interaction_id,
                environment_id=environment_id,
            )
        except Exception as exc:
            logger.warning("Failed to save thread state for thread_id=%s: %s", thread_id, exc)


async def get_gemini_managed_client() -> GeminiManagedClient:
    global _client_singleton
    if _client_singleton is None:
        async with _client_lock:
            if _client_singleton is None:
                _client_singleton = GeminiManagedClient()
    return _client_singleton


async def close_gemini_managed_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.close()
        _client_singleton = None

