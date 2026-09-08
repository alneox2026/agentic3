"""FastAPI entrypoint for the new reusable middleware gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.agent_gateway_v3.app.api.routes_chat import (
    CHAT_ROUTE_DIAGNOSTICS_VERSION,
    router as chat_router,
)
from services.agent_gateway_v3.app.api.routes_health import router as health_router
from services.agent_gateway_v3.app.api.routes_stream import router as stream_router
from services.agent_gateway_v3.app.api.routes_threads import router as threads_router
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import register_exception_handlers
from services.agent_gateway_v3.app.core.logging import configure_logging
from services.agent_gateway_v3.app.services.gemini_managed_client import close_gemini_managed_client
from services.agent_gateway_v3.app.services.pubsub_publisher import close_pubsub_publisher


LOGGER = logging.getLogger(__name__)
SETTINGS = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(SETTINGS.log_level)
    LOGGER.info(
        "agent_gateway_startup",
        extra={
            "payload": {
                "event": "agent_gateway_startup",
                "project_id": SETTINGS.project_id,
                "region": SETTINGS.region,
                "agent_registry_path": str(SETTINGS.agent_registry_path),
                "build_sha": os.getenv("GATEWAY_BUILD_SHA", ""),
                "image_tag": os.getenv("GATEWAY_IMAGE_TAG", ""),
                "chat_route_diagnostics_version": CHAT_ROUTE_DIAGNOSTICS_VERSION,
            }
        },
    )
    try:
        yield
    finally:
        await close_gemini_managed_client()
        await close_pubsub_publisher()
        LOGGER.info(
            "agent_gateway_shutdown",
            extra={"payload": {"event": "agent_gateway_shutdown"}},
        )


app = FastAPI(
    title="CEOsystem Agent Gateway",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=SETTINGS.allowed_headers,
)
app.include_router(health_router)
app.include_router(chat_router)
app.include_router(stream_router)
app.include_router(threads_router)
register_exception_handlers(app)
