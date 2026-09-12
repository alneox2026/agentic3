"""Unit tests for Gemini Managed & Antigravity agents client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.services.gemini_managed_client import (
    GeminiManagedClient,
)
from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler


@pytest.fixture
def managed_agent_config() -> AgentConfig:
    return AgentConfig(
        agent_id="antigravity_agent",
        backend="gemini_managed",
        remote_agent_id="antigravity-preview-05-2026",
        model="gemini-3.8-flash",
        max_total_tokens=50000,
        streaming_enabled=True,
        persistence_enabled=True,
        auth_policy="firebase",
    )


@pytest.mark.anyio
async def test_ensure_session_creates_session_and_thread(managed_agent_config: AgentConfig):
    client = GeminiManagedClient()
    req = ChatRequest(message="Hello world")
    result = await client.ensure_session(
        agent_config=managed_agent_config,
        user_id="user_123",
        request=req,
    )
    assert result.thread_id.startswith("thread-")
    assert result.session_id == result.thread_id


@pytest.mark.anyio
async def test_build_interaction_payload(managed_agent_config: AgentConfig):
    client = GeminiManagedClient()
    payload = client._build_interaction_payload(
        agent_config=managed_agent_config,
        message="Analyze this CSV",
        stream=True,
        previous_interaction_id="interactions/prev_123",
        environment_id="environments/env_456",
    )
    assert payload["agent"] == "antigravity-preview-05-2026"
    assert payload["input"] == "Analyze this CSV"
    assert payload["stream"] is True
    assert payload["agent_config"]["model"] == "gemini-3.8-flash"
    assert payload["agent_config"]["max_total_tokens"] == 50000
    assert payload["previous_interaction_id"] == "interactions/prev_123"
    assert payload["environment"] == "environments/env_456"


@pytest.mark.anyio
async def test_build_interaction_payload_default_environment(managed_agent_config: AgentConfig):
    client = GeminiManagedClient()
    payload = client._build_interaction_payload(
        agent_config=managed_agent_config,
        message="Analyze this CSV",
        stream=True,
    )
    assert payload["environment"] == "remote"


@pytest.mark.anyio
async def test_chat_buffered_query(managed_agent_config: AgentConfig, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345")
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "interaction": {
            "id": "interactions/int_999",
            "environment_id": "environments/env_999",
            "status": "completed",
            "output_text": "Here is the summary analysis.",
            "usage": {
                "total_input_tokens": 120,
                "total_output_tokens": 45,
                "total_tokens": 165,
            },
        }
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=mock_response)

    client = GeminiManagedClient(http_client=mock_http)
    client._save_thread_state = AsyncMock()

    res = await client.chat_buffered_query(
        agent_config=managed_agent_config,
        user_id="user_123",
        session_id="th_test_123",
        message="Please summarize.",
        thread_id="th_test_123",
    )

    assert res.reply_text == "Here is the summary analysis."
    assert res.usage["total_input_tokens"] == 120
    assert res.usage["total_output_tokens"] == 45
    client._save_thread_state.assert_awaited_once_with(
        "th_test_123", "interactions/int_999", "environments/env_999"
    )


@pytest.mark.anyio
async def test_stream_chat_events(managed_agent_config: AgentConfig, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345")

    sse_lines = [
        "event: step.delta",
        'data: {"delta": {"text": "Step 1: "}}',
        "",
        "event: step.delta",
        'data: {"delta": {"text": "Loading data."}}',
        "",
        "event: interaction.completed",
        'data: {"interaction": {"id": "int_abc", "environment_id": "env_abc", "usage": {"total_input_tokens": 50, "total_output_tokens": 25, "total_tokens": 75}}}',
        "",
        "data: [DONE]",
        "",
    ]

    async def aiter_lines():
        for line in sse_lines:
            yield line

    mock_stream_context = MagicMock()
    mock_stream_context.status_code = 200
    mock_stream_context.aiter_lines = aiter_lines
    mock_stream_context.__aenter__ = AsyncMock(return_value=mock_stream_context)
    mock_stream_context.__aexit__ = AsyncMock(return_value=None)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_context)

    client = GeminiManagedClient(http_client=mock_http)
    client._save_thread_state = AsyncMock()

    events = []
    assembler = TurnAssembler()

    async for event in client.stream_chat_events(
        agent_config=managed_agent_config,
        user_id="user_123",
        session_id="th_stream_123",
        message="Run the task",
        thread_id="th_stream_123",
    ):
        events.append(event)
        fragments = client.extract_text_fragments(event.payload)
        for fragment in fragments:
            assembler.add_text(fragment)
        assembler.add_event(event.payload)

    assert len(events) == 3
    assert assembler.reply_text() == "Step 1: Loading data."
    assert assembler.usage["token_counts"]["prompt_token_count"] == 50
    assert assembler.usage["token_counts"]["candidates_token_count"] == 25
    assert assembler.usage["estimated_cost_usd"] > 0


def test_extract_text_fragments_nested_formats():
    client = GeminiManagedClient()

    # Interaction with output_text
    payload_interaction_output_text = {
        "interaction": {
            "id": "int_1",
            "status": "completed",
            "output_text": "The Constitution of the Kyrgyz Republic was adopted...",
        }
    }
    assert client.extract_text_fragments(payload_interaction_output_text) == [
        "The Constitution of the Kyrgyz Republic was adopted..."
    ]

    # Interaction with outputs list
    payload_interaction_outputs = {
        "interaction": {
            "id": "int_2",
            "outputs": [{"text": "Part 1: Article 1."}, {"text": "Part 2: Article 2."}],
        }
    }
    assert client.extract_text_fragments(payload_interaction_outputs) == [
        "Part 1: Article 1.",
        "Part 2: Article 2.",
    ]

    # Step with output_text
    payload_step = {
        "step": {
            "id": "step_1",
            "output_text": "Step output text.",
        }
    }
    assert client.extract_text_fragments(payload_step) == ["Step output text."]

    # Direct outputs list
    payload_direct_outputs = {
        "outputs": [{"text": "Direct output."}]
    }
    assert client.extract_text_fragments(payload_direct_outputs) == ["Direct output."]

