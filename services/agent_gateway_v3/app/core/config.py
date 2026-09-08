"""Typed configuration for the agent gateway service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_ALLOWED_HEADERS = ("Authorization", "Content-Type", "X-Request-Id")


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class GatewaySettings:
    project_id: str
    region: str
    pubsub_topic: str
    pubsub_publish_timeout_seconds: float
    firebase_auth_required: bool
    allowed_origins: list[str]
    allowed_headers: list[str]
    log_level: str
    agent_registry_path: Path
    threads_collection: str
    upstream_connect_timeout_seconds: float
    upstream_read_timeout_seconds: float
    stream_debug: bool
    billing_enforcement_enabled: bool
    wallets_collection: str
    billing_reservations_collection: str
    billing_reservation_nanos: int
    billing_reservation_ttl_seconds: int
    http_max_connections: int = 100
    http_max_keepalive_connections: int = 50
    http_keepalive_expiry_seconds: float = 30.0


@lru_cache(maxsize=1)
def get_settings() -> GatewaySettings:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "ceo-dev123").strip() or "ceo-dev123"
    region = os.getenv("GOOGLE_CLOUD_REGION", "us-central1").strip() or "us-central1"
    pubsub_topic = os.getenv("AGENT_TURN_EVENTS_TOPIC", "agent-turn-events-v3").strip() or "agent-turn-events-v3"
    log_level = os.getenv("GATEWAY_LOG_LEVEL", "INFO").strip().upper() or "INFO"

    raw_registry_path = os.getenv("AGENT_REGISTRY_PATH", "config/agents.dev.yaml").strip()
    agent_registry_path = Path(raw_registry_path).resolve()

    allowed_origins = _parse_csv(os.getenv("ALLOWED_ORIGINS"))

    allowed_headers = _parse_csv(os.getenv("ALLOWED_HEADERS"))
    if not allowed_headers:
        allowed_headers = list(DEFAULT_ALLOWED_HEADERS)

    return GatewaySettings(
        project_id=project_id,
        region=region,
        pubsub_topic=pubsub_topic,
        pubsub_publish_timeout_seconds=float(
            os.getenv("PUBSUB_PUBLISH_TIMEOUT_SECONDS", "30")
        ),
        firebase_auth_required=_parse_bool(os.getenv("REQUIRE_FIREBASE_AUTH"), default=True),
        allowed_origins=allowed_origins,
        allowed_headers=allowed_headers,
        log_level=log_level,
        agent_registry_path=agent_registry_path,
        threads_collection=os.getenv("FIRESTORE_THREADS_COLLECTION", "agent_threads_v3").strip()
        or "agent_threads_v3",
        upstream_connect_timeout_seconds=float(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SECONDS", "10")),
        upstream_read_timeout_seconds=float(os.getenv("UPSTREAM_READ_TIMEOUT_SECONDS", "60")),
        stream_debug=_parse_bool(os.getenv("GATEWAY_STREAM_DEBUG"), default=False),
        billing_enforcement_enabled=_parse_bool(
            os.getenv("BILLING_ENFORCEMENT_ENABLED"),
            default=False,
        ),
        wallets_collection=os.getenv(
            "FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
            "customer_wallets_v3",
        ).strip()
        or "customer_wallets_v3",
        billing_reservations_collection=os.getenv(
            "FIRESTORE_BILLING_RESERVATIONS_COLLECTION",
            "billing_reservations_v3",
        ).strip()
        or "billing_reservations_v3",
        billing_reservation_nanos=int(
            os.getenv("BILLING_RESERVATION_NANOS", "50000000")
        ),
        billing_reservation_ttl_seconds=int(
            os.getenv("BILLING_RESERVATION_TTL_SECONDS", "3600")
        ),
        http_max_connections=int(os.getenv("HTTP_MAX_CONNECTIONS", "100")),
        http_max_keepalive_connections=int(os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS", "50")),
        http_keepalive_expiry_seconds=float(os.getenv("HTTP_KEEPALIVE_EXPIRY_SECONDS", "30.0")),
    )

