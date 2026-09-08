"""Security helpers used by logging and request handling."""

from __future__ import annotations

import hashlib


def hash_user_id_for_logs(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return digest[:16]


def truncate_for_logs(value: str, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def redact_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"

