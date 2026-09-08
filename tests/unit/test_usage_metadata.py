from services.agent_gateway_v3.app.services.usage_metadata import (
    _load_models_from_yaml_catalog,
    extract_usage_metadata,
    normalize_usage_metadata,
    resolve_model_pricing,
)


def test_normalize_usage_metadata_prices_gemini_flash_text_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 1000,
            "candidates_token_count": 200,
            "thoughts_token_count": 50,
            "total_token_count": 1250,
        }
    )

    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["token_counts"] == {
        "prompt_token_count": 1000,
        "candidates_token_count": 200,
        "thoughts_token_count": 50,
        "total_token_count": 1250,
    }
    assert usage["billable_tokens"] == {
        "input_text_image_video": 1000,
        "input_audio": 0,
        "output_including_thinking": 250,
    }
    assert usage["estimated_cost_usd"] == 0.0016875
    assert usage["estimated_cost_breakdown_usd"] == {
        "input_text_image_video": 0.00075,
        "input_audio": 0.0,
        "output_including_thinking": 0.0009375,
    }


def test_normalize_usage_metadata_splits_audio_prompt_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "promptTokenCount": 100,
            "candidatesTokenCount": 10,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 40},
                {"modality": "AUDIO", "tokenCount": 60},
            ],
        }
    )

    assert usage["token_counts"]["prompt_token_count"] == 100
    assert usage["billable_tokens"] == {
        "input_text_image_video": 40,
        "input_audio": 60,
        "output_including_thinking": 10,
    }
    assert usage["estimated_cost_usd"] == 0.0001575


def test_extract_usage_metadata_finds_nested_agent_runtime_shape() -> None:
    event = {
        "payload": {
            "event": {
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 3,
                }
            }
        }
    }

    assert extract_usage_metadata(event) == {
        "promptTokenCount": 5,
        "candidatesTokenCount": 3,
    }


def test_normalize_usage_metadata_does_not_price_total_only_usage() -> None:
    usage = normalize_usage_metadata({"total_token_count": 21})

    assert usage["total_token_count"] == 21
    assert usage["token_counts"] == {"total_token_count": 21}
    assert "estimated_cost_usd" not in usage
    assert "billable_tokens" not in usage


def test_normalize_usage_metadata_prices_gemini_3_5_flash_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 132,
            "candidates_token_count": 72,
            "thoughts_token_count": 128,
            "total_token_count": 332,
        },
        model_name="gemini-3.5-flash",
    )

    assert usage["pricing_model"] == "gemini-3.5-flash"
    assert usage["pricing"]["input_text_image_video"] == 1.50
    assert usage["pricing"]["output_including_thinking"] == 9.00
    assert usage["billable_tokens"]["input_text_image_video"] == 132
    assert usage["billable_tokens"]["output_including_thinking"] == 200
    # 132 * 1.50 / 1M = 0.000198; 200 * 9.00 / 1M = 0.001800; total = 0.001998
    assert usage["estimated_cost_usd"] == 0.001998


def test_normalize_usage_metadata_prices_gemini_3_7_flash_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 280,
            "candidates_token_count": 50,
            "thoughts_token_count": 29,
            "total_token_count": 359,
        },
        model_name="gemini-3.7-flash",
    )

    assert usage["pricing_model"] == "gemini-3.7-flash"
    assert usage["pricing"]["input_text_image_video"] == 0.75
    assert usage["pricing"]["output_including_thinking"] == 3.75
    assert usage["billable_tokens"]["input_text_image_video"] == 280
    assert usage["billable_tokens"]["output_including_thinking"] == 79
    # 280 * 0.75 / 1M = 0.000210; 79 * 3.75 / 1M = 0.00029625; total = 0.00050625
    assert usage["estimated_cost_usd"] == 0.00050625


def test_normalize_usage_metadata_prices_gemini_3_8_flash_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 280,
            "candidates_token_count": 50,
            "thoughts_token_count": 29,
            "total_token_count": 359,
        },
        model_name="gemini-3.8-flash",
    )

    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["pricing"]["input_text_image_video"] == 0.75
    assert usage["pricing"]["output_including_thinking"] == 3.75
    assert usage["billable_tokens"]["input_text_image_video"] == 280
    assert usage["billable_tokens"]["output_including_thinking"] == 79
    assert usage["estimated_cost_usd"] == 0.00050625


def test_resolve_model_pricing_respects_env_default(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_MODEL_NAME", "gemini-3.8-flash")
    model, pricing = resolve_model_pricing(None)
    assert model == "gemini-3.8-flash"
    assert pricing["input_text_image_video"] == 0.75
    assert pricing["output_including_thinking"] == 3.75


def test_resolve_model_pricing_uses_catalog_when_default_absent(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "billing.yaml"
    catalog_path.write_text(
        """
models:
  gemini-3.8-flash:
    input_usd_per_million: 0.75
    output_usd_per_million: 3.75
""",
        encoding="utf-8",
    )
    _load_models_from_yaml_catalog.cache_clear()
    monkeypatch.setenv("BILLING_CATALOG_PATH", str(catalog_path))
    model, pricing = resolve_model_pricing(None)
    _load_models_from_yaml_catalog.cache_clear()
    assert model == "gemini-3.8-flash"
    assert pricing["input_text_image_video"] == 0.75


def test_normalize_usage_metadata_interactions_api_schema() -> None:
    raw_usage = {
        "total_input_tokens": 1000,
        "total_output_tokens": 200,
        "total_thought_tokens": 150,
        "total_tokens": 1350,
    }
    usage = normalize_usage_metadata(raw_usage, model_name="gemini-3.8-flash")
    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["token_counts"]["prompt_token_count"] == 1000
    assert usage["token_counts"]["candidates_token_count"] == 200
    assert usage["token_counts"]["thoughts_token_count"] == 150
    assert usage["token_counts"]["total_token_count"] == 1350
    assert usage["billable_tokens"]["input_text_image_video"] == 1000
    assert usage["billable_tokens"]["output_including_thinking"] == 350
    # 1000 * 0.75/1M + 350 * 3.75/1M = 0.00075 + 0.0013125 = 0.0020625
    assert usage["estimated_cost_usd"] == 0.0020625


