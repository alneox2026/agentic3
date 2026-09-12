import pytest

from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.agent_registry import get_agent_config, load_registry


@pytest.fixture(autouse=True)
def clear_registry_cache():
    get_settings.cache_clear()
    load_registry.cache_clear()
    yield
    get_settings.cache_clear()
    load_registry.cache_clear()


def test_get_agent_config_returns_antigravity_agent() -> None:
    config = get_agent_config("antigravity_agent")
    assert config.agent_id == "antigravity_agent"
    assert config.backend == "gemini_managed"
    assert config.remote_agent_id == "antigravity-preview-05-2026"
    assert config.model == "gemini-3.8-flash"
    assert config.streaming_enabled is True


def test_get_agent_config_returns_data_analyst() -> None:
    config = get_agent_config("data_analyst")
    assert config.agent_id == "data_analyst"
    assert config.backend == "gemini_managed"
    assert config.remote_agent_id == "antigravity-preview-05-2026"
    assert config.model == "gemini-3.8-flash"
    assert config.streaming_enabled is True


def test_get_agent_config_returns_code_assistant() -> None:
    config = get_agent_config("code_assistant")
    assert config.agent_id == "code_assistant"
    assert config.backend == "gemini_managed"
    assert config.remote_agent_id == "antigravity-preview-05-2026"
    assert config.model == "gemini-3.8-flash"
    assert config.max_total_tokens == 50000


def test_get_agent_config_returns_constitution_expert() -> None:
    config = get_agent_config("constitution_expert")
    assert config.agent_id == "constitution_expert"
    assert config.backend == "gemini_managed"
    assert config.remote_agent_id == "antigravity-preview-05-2026"
    assert config.model == "gemini-3.8-flash"
    assert isinstance(config.environment, dict)
    assert config.environment["type"] == "remote"
    assert len(config.environment["sources"]) == 1
    assert config.environment["sources"][0]["source"] == "https://github.com/alneox2026/ragagent1"
    assert config.environment["sources"][0]["target"] == "/workspace/constitution"


def test_get_agent_config_rejects_unknown_agent() -> None:
    with pytest.raises(ApiError) as exc_info:
        get_agent_config("unknown-agent")
    assert exc_info.value.code == "unknown_agent"


def test_get_agent_config_applies_runtime_env_override(tmp_path, monkeypatch) -> None:
    registry_path = tmp_path / "agents.yaml"
    registry_path.write_text(
        """
agents:
  maxima_v3:
    agent_id: maxima_v3
    resource_name: projects/test/locations/us-east1/reasoningEngines/old
    region: us-east1
    streaming_enabled: false
    persistence_enabled: true
    auth_policy: firebase
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "AGENT_MAXIMA_V3_RESOURCE_NAME",
        "projects/test/locations/us-central1/reasoningEngines/new",
    )
    monkeypatch.setenv("AGENT_MAXIMA_V3_REGION", "us-central1")

    agent_config = get_agent_config("maxima_v3")

    assert agent_config.resource_name == (
        "projects/test/locations/us-central1/reasoningEngines/new"
    )
    assert agent_config.region == "us-central1"


def test_get_agent_config_applies_cloud_run_env_overrides(tmp_path, monkeypatch) -> None:
    registry_path = tmp_path / "agents.yaml"
    registry_path.write_text(
        """
agents:
  maxima_cloudrun_v3:
    agent_id: maxima_cloudrun_v3
    backend: cloud_run_adk
    base_url: https://old.example.run.app
    app_name: old_app
    region: us-east1
    streaming_enabled: false
    persistence_enabled: true
    auth_policy: firebase
    runtime_session_cleanup: none
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "AGENT_MAXIMA_CLOUDRUN_V3_BASE_URL",
        "https://maxima-cloudrun-canary.example.run.app",
    )
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_APP_NAME", "maxima_cloudrun")
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_AUDIENCE", "https://audience.example")
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_REGION", "us-central1")

    agent_config = get_agent_config("maxima_cloudrun_v3")

    assert agent_config.base_url == "https://maxima-cloudrun-canary.example.run.app"
    assert agent_config.app_name == "maxima_cloudrun"
    assert agent_config.audience == "https://audience.example"
    assert agent_config.region == "us-central1"
