"""Usage metadata normalization and multi-model cost estimates."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import os
from pathlib import Path
from typing import Any
import yaml


DEFAULT_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "gemini-3.8-flash": {
        "pricing_version": "gemini-3.8-flash-usd-on-demand-2026-08-24",
        "input_text_image_video": 0.75,
        "input_audio": 1.50,
        "output_including_thinking": 3.75,
    },
    "gemini-3.7-flash": {
        "pricing_version": "gemini-3.7-flash-usd-on-demand-2026-08-24",
        "input_text_image_video": 0.75,
        "input_audio": 1.50,
        "output_including_thinking": 3.75,
    },
    "gemini-3.5-flash": {
        "pricing_version": "gemini-3.5-flash-usd-on-demand-2026-08-23",
        "input_text_image_video": 1.50,
        "input_audio": 3.00,
        "output_including_thinking": 9.00,
    },
    "gemini-2.5-flash": {
        "pricing_version": "gemini-2.5-flash-usd-on-demand-2026-08-11",
        "input_text_image_video": 0.30,
        "input_audio": 1.00,
        "output_including_thinking": 2.50,
    },
    "gemini-2.5-pro": {
        "pricing_version": "gemini-2.5-pro-usd-on-demand-2026-08-11",
        "input_text_image_video": 1.25,
        "input_audio": 1.25,
        "output_including_thinking": 10.00,
    },
    "gemini-1.5-flash": {
        "pricing_version": "gemini-1.5-flash-usd-on-demand-2026-08-11",
        "input_text_image_video": 0.075,
        "input_audio": 0.075,
        "output_including_thinking": 0.30,
    },
    "gemini-1.5-pro": {
        "pricing_version": "gemini-1.5-pro-usd-on-demand-2026-08-11",
        "input_text_image_video": 1.25,
        "input_audio": 1.25,
        "output_including_thinking": 5.00,
    },
}
DEFAULT_MODEL_NAME = os.getenv("DEFAULT_MODEL_NAME", "gemini-3.8-flash")

_USAGE_KEYS = ("usage_metadata", "usageMetadata", "usage")


@lru_cache(maxsize=1)
def _load_models_from_yaml_catalog() -> dict[str, dict[str, Any]]:
    """Dynamically load model rates from billing YAML catalog files if available."""
    catalog: dict[str, dict[str, Any]] = {}
    custom_path = os.getenv("BILLING_CATALOG_PATH")
    if custom_path:
        search_paths = [custom_path]
    else:
        search_paths = [
            "config/billing.prod.yaml",
            "config/billing.test.yaml",
            "/app/config/billing.prod.yaml",
            "/app/config/billing.test.yaml",
        ]
    for path_str in search_paths:
        if not path_str:
            continue
        try:
            path = Path(path_str).resolve()
            if path.exists() and path.is_file():
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "models" in raw and isinstance(raw["models"], dict):
                    for model_id, rates in raw["models"].items():
                        if isinstance(rates, dict):
                            cleaned_id = str(model_id).strip().lower()
                            default_rates = DEFAULT_MODEL_PRICING.get(cleaned_id, {})
                            input_usd = float(
                                rates.get(
                                    "input_usd_per_million",
                                    default_rates.get("input_text_image_video", 0.30),
                                )
                            )
                            output_usd = float(
                                rates.get(
                                    "output_usd_per_million",
                                    default_rates.get("output_including_thinking", 2.50),
                                )
                            )
                            audio_usd = float(
                                rates.get(
                                    "input_audio_usd_per_million",
                                    default_rates.get("input_audio", input_usd * 2.0),
                                )
                            )
                            catalog[cleaned_id] = {
                                "pricing_version": default_rates.get(
                                    "pricing_version", f"{cleaned_id}-usd-on-demand-catalog"
                                ),
                                "input_text_image_video": input_usd,
                                "input_audio": audio_usd,
                                "output_including_thinking": output_usd,
                            }
                    if catalog:
                        break
        except Exception:
            pass
    return catalog


def resolve_model_pricing(raw_model: str | None = None) -> tuple[str, dict[str, Any]]:
    """Resolves model name and pricing catalog rates from YAML catalog and presets."""
    active_catalog = dict(DEFAULT_MODEL_PRICING)
    yaml_catalog = _load_models_from_yaml_catalog()
    active_catalog.update(yaml_catalog)

    if not raw_model:
        env_default = os.getenv("DEFAULT_MODEL_NAME")
        if env_default:
            cleaned_env = env_default.strip().lower()
            if cleaned_env in active_catalog:
                return cleaned_env, active_catalog[cleaned_env]
        # If a custom billing catalog exists and DEFAULT_MODEL_NAME is not in it,
        # prefer the first model declared in the custom catalog.
        if yaml_catalog and DEFAULT_MODEL_NAME not in yaml_catalog:
            catalog_first_model = next(iter(yaml_catalog.keys()))
            return catalog_first_model, yaml_catalog[catalog_first_model]
        return DEFAULT_MODEL_NAME, active_catalog.get(
            DEFAULT_MODEL_NAME, DEFAULT_MODEL_PRICING.get(DEFAULT_MODEL_NAME, {})
        )

    cleaned = str(raw_model).strip().lower()
    if cleaned.startswith("models/"):
        cleaned = cleaned[7:]

    # Exact match
    if cleaned in active_catalog:
        return cleaned, active_catalog[cleaned]

    # Substring / variant match (e.g. "gemini-3.7-flash-001" -> "gemini-3.7-flash")
    for key, pricing in active_catalog.items():
        if key in cleaned or cleaned in key:
            return key, pricing

    # Preserve custom model identifier with default fallback rates
    return cleaned, {
        "pricing_version": f"{cleaned}-usd-on-demand-fallback",
        "input_text_image_video": 0.30,
        "input_audio": 1.00,
        "output_including_thinking": 2.50,
    }


def extract_usage_metadata(event: dict[str, Any]) -> dict[str, Any] | None:
    """Find ADK/Gemini usage metadata in common event envelope shapes."""

    def walk(value: Any, depth: int = 0) -> dict[str, Any] | None:
        if depth > 8:
            return None
        if isinstance(value, dict):
            for key in _USAGE_KEYS:
                usage = value.get(key)
                if isinstance(usage, dict):
                    return usage
            for nested_value in value.values():
                usage = walk(nested_value, depth + 1)
                if usage is not None:
                    return usage
        elif isinstance(value, list):
            for item in value:
                usage = walk(item, depth + 1)
                if usage is not None:
                    return usage
        return None

    return walk(event)


def normalize_usage_metadata(
    usage_metadata: dict[str, Any],
    model_name: str | None = None,
) -> dict[str, Any]:
    """Preserve raw usage fields while adding normalized counts and dynamic cost fields."""

    usage = deepcopy(usage_metadata)
    token_counts = _token_counts(usage_metadata)
    if token_counts:
        usage["token_counts"] = token_counts

    prompt_token_count = token_counts.get("prompt_token_count")
    candidates_token_count = token_counts.get("candidates_token_count")
    thoughts_token_count = token_counts.get("thoughts_token_count", 0)
    total_token_count = token_counts.get("total_token_count")

    input_text_image_video_tokens, input_audio_tokens = _input_token_split(
        usage_metadata,
        prompt_token_count,
    )
    output_tokens = _output_token_count(
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
        thoughts_token_count=thoughts_token_count,
        total_token_count=total_token_count,
    )

    if (
        input_text_image_video_tokens is None
        and input_audio_tokens is None
        and output_tokens is None
    ):
        return usage

    model_candidate = (
        model_name
        or usage_metadata.get("pricing_model")
        or usage_metadata.get("model_version")
        or usage_metadata.get("model_name")
        or usage_metadata.get("model")
    )
    resolved_model, pricing_info = resolve_model_pricing(model_candidate)

    input_rate = pricing_info["input_text_image_video"]
    audio_rate = pricing_info["input_audio"]
    output_rate = pricing_info["output_including_thinking"]
    pricing_version = pricing_info["pricing_version"]

    billable_tokens = {
        "input_text_image_video": input_text_image_video_tokens or 0,
        "input_audio": input_audio_tokens or 0,
        "output_including_thinking": output_tokens or 0,
    }
    usage["pricing_model"] = resolved_model
    usage["pricing_version"] = pricing_version
    usage["pricing_unit"] = "usd_per_1m_tokens"
    usage["pricing"] = {
        "input_text_image_video": input_rate,
        "input_audio": audio_rate,
        "output_including_thinking": output_rate,
    }
    usage["billable_tokens"] = billable_tokens
    usage["estimated_cost_usd"] = _round_usd(
        billable_tokens["input_text_image_video"] * input_rate / 1_000_000
        + billable_tokens["input_audio"] * audio_rate / 1_000_000
        + billable_tokens["output_including_thinking"] * output_rate / 1_000_000
    )
    usage["estimated_cost_breakdown_usd"] = {
        "input_text_image_video": _round_usd(
            billable_tokens["input_text_image_video"] * input_rate / 1_000_000
        ),
        "input_audio": _round_usd(
            billable_tokens["input_audio"] * audio_rate / 1_000_000
        ),
        "output_including_thinking": _round_usd(
            billable_tokens["output_including_thinking"] * output_rate / 1_000_000
        ),
    }
    return usage


def _token_counts(usage_metadata: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for normalized_key, key_options in {
        "prompt_token_count": (
            "prompt_token_count",
            "promptTokenCount",
            "total_input_tokens",
            "totalInputTokens",
        ),
        "candidates_token_count": (
            "candidates_token_count",
            "candidatesTokenCount",
            "total_output_tokens",
            "totalOutputTokens",
        ),
        "thoughts_token_count": (
            "thoughts_token_count",
            "thoughtsTokenCount",
            "total_thought_tokens",
            "totalThoughtTokens",
        ),
        "total_token_count": (
            "total_token_count",
            "totalTokenCount",
            "total_tokens",
            "totalTokens",
        ),
        "cached_content_token_count": (
            "cached_content_token_count",
            "cachedContentTokenCount",
        ),
        "tool_use_prompt_token_count": (
            "tool_use_prompt_token_count",
            "toolUsePromptTokenCount",
        ),
    }.items():
        value = _first_int(usage_metadata, key_options)
        if value is not None:
            counts[normalized_key] = value
    return counts


def _input_token_split(
    usage_metadata: dict[str, Any],
    prompt_token_count: int | None,
) -> tuple[int | None, int | None]:
    if prompt_token_count is None:
        return None, None

    audio_tokens = 0
    non_audio_tokens = 0
    details_found = False
    for detail in _prompt_token_details(usage_metadata):
        modality = str(
            detail.get("modality")
            or detail.get("Modality")
            or detail.get("type")
            or ""
        ).upper()
        token_count = _first_int(detail, ("token_count", "tokenCount"))
        if token_count is None:
            continue
        details_found = True
        if modality == "AUDIO":
            audio_tokens += token_count
        else:
            non_audio_tokens += token_count

    if not details_found:
        return prompt_token_count, 0

    residual_tokens = max(prompt_token_count - audio_tokens - non_audio_tokens, 0)
    return non_audio_tokens + residual_tokens, audio_tokens


def _prompt_token_details(usage_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("prompt_tokens_details", "promptTokensDetails"):
        value = usage_metadata.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _output_token_count(
    *,
    prompt_token_count: int | None,
    candidates_token_count: int | None,
    thoughts_token_count: int | None,
    total_token_count: int | None,
) -> int | None:
    if candidates_token_count is not None or thoughts_token_count:
        return (candidates_token_count or 0) + (thoughts_token_count or 0)
    if total_token_count is not None and prompt_token_count is not None:
        return max(total_token_count - prompt_token_count, 0)
    return None


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, float) and value.is_integer():
            return max(int(value), 0)
        if isinstance(value, str):
            try:
                parsed = int(value)
            except ValueError:
                continue
            return max(parsed, 0)
    return None


def _round_usd(value: float) -> float:
    return round(value, 9)
