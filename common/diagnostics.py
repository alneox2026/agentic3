"""Safe diagnostics helpers for structured logs and error payloads."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


DEFAULT_EXCERPT_LIMIT = 600
SENSITIVE_CONTENT_KEYS = {
    "assistant_message",
    "content",
    "detail",
    "details",
    "error",
    "errors",
    "input",
    "message",
    "new_message",
    "output",
    "parts",
    "prompt",
    "reply_text",
    "response",
    "text",
    "user_message",
}


def safe_url_host(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or parsed.path or None


def safe_truncate(value: Any, *, limit: int = DEFAULT_EXCERPT_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def sanitize_for_diagnostics(value: Any, *, depth: int = 0, max_depth: int = 6) -> Any:
    if depth > max_depth:
        return "<max_depth_exceeded>"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SENSITIVE_CONTENT_KEYS and not isinstance(item, dict):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = sanitize_for_diagnostics(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
        return sanitized
    if isinstance(value, list):
        return [
            sanitize_for_diagnostics(item, depth=depth + 1, max_depth=max_depth)
            for item in value[:20]
        ]
    if isinstance(value, str):
        return safe_truncate(value)
    return value


def safe_response_body_details(
    *,
    status_code: int,
    content_type: str,
    text: str,
    parsed_body: Any | None = None,
    parse_failed: bool = False,
    limit: int = DEFAULT_EXCERPT_LIMIT,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "status_code": status_code,
        "content_type": content_type,
        "body_length": len(text),
    }
    if parsed_body is not None:
        sanitized_body = sanitize_for_diagnostics(parsed_body)
        details["body_json_type"] = type(parsed_body).__name__
        if isinstance(parsed_body, dict):
            details["body_json_keys"] = sorted(str(key) for key in parsed_body.keys())[:20]
        details["body_excerpt"] = safe_truncate(
            json.dumps(sanitized_body, default=str, sort_keys=True),
            limit=limit,
        )
        return details
    details["body_parse_failed"] = parse_failed
    details["body_excerpt"] = "<non_json_body_omitted>"
    return details


def safe_http_response_details(
    response: Any,
    *,
    limit: int = DEFAULT_EXCERPT_LIMIT,
) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    text = response.text or ""
    try:
        parsed_body = response.json()
    except ValueError:
        return safe_response_body_details(
            status_code=response.status_code,
            content_type=content_type,
            text=text,
            parse_failed=True,
            limit=limit,
        )
    return safe_response_body_details(
        status_code=response.status_code,
        content_type=content_type,
        text=text,
        parsed_body=parsed_body,
        limit=limit,
    )


def is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599
