"""Server-side agent registry loading and lookup."""

from __future__ import annotations

import os
from functools import lru_cache

import yaml

from common.ids import validate_agent_id
from common.schemas import AgentConfig
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError


def _agent_env_key(agent_id: str, suffix: str) -> str:
    normalized_agent_id = "".join(
        character if character.isalnum() else "_"
        for character in agent_id.upper()
    )
    return f"AGENT_{normalized_agent_id}_{suffix}"


def _env_override(agent_id: str, suffix: str) -> str | None:
    value = os.getenv(_agent_env_key(agent_id, suffix))
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


@lru_cache(maxsize=1)
def load_registry() -> dict[str, AgentConfig]:
    settings = get_settings()
    with settings.agent_registry_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}

    agents = parsed.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise RuntimeError("Agent registry must contain a non-empty 'agents' mapping.")

    registry: dict[str, AgentConfig] = {}
    for raw_agent_id, config in agents.items():
        if not isinstance(config, dict):
            raise RuntimeError(f"Agent config for '{raw_agent_id}' must be an object.")
        config = dict(config)
        agent_id = str(config.get("agent_id") or raw_agent_id)

        resource_name_override = _env_override(agent_id, "RESOURCE_NAME")
        if resource_name_override:
            config["resource_name"] = resource_name_override

        region_override = _env_override(agent_id, "REGION")
        if region_override:
            config["region"] = region_override

        backend_override = _env_override(agent_id, "BACKEND")
        if backend_override:
            config["backend"] = backend_override

        base_url_override = _env_override(agent_id, "BASE_URL")
        if base_url_override:
            config["base_url"] = base_url_override

        app_name_override = _env_override(agent_id, "APP_NAME")
        if app_name_override:
            config["app_name"] = app_name_override

        audience_override = _env_override(agent_id, "AUDIENCE")
        if audience_override:
            config["audience"] = audience_override

        runtime_cleanup_override = _env_override(agent_id, "RUNTIME_SESSION_CLEANUP")
        if runtime_cleanup_override:
            config["runtime_session_cleanup"] = runtime_cleanup_override

        model_override = _env_override(agent_id, "MODEL")
        if model_override:
            config["model"] = model_override

        agent_config = AgentConfig(**config)
        registry[agent_config.agent_id] = agent_config
    return registry


def get_agent_config(agent_id: str) -> AgentConfig:
    cleaned_agent_id = validate_agent_id(agent_id)
    registry = load_registry()
    agent_config = registry.get(cleaned_agent_id)
    if agent_config is None:
        raise ApiError(
            404,
            "unknown_agent",
            "The requested agent is not registered in this middleware.",
            {"agent_id": cleaned_agent_id},
        )
    return agent_config
