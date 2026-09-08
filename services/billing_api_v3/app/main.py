"""FastAPI entrypoint for the Stripe-backed Billing API."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.billing_api_v3.app.api.routes_billing import router as billing_router
from services.billing_api_v3.app.api.routes_health import router as health_router
from services.billing_api_v3.app.core.config import get_settings
from services.billing_api_v3.app.core.errors import register_exception_handlers
from services.billing_api_v3.app.core.logging import configure_logging
from services.billing_api_v3.app.services.billing_catalog import get_billing_catalog


LOGGER = logging.getLogger(__name__)
SETTINGS = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(SETTINGS.log_level)
    catalog = get_billing_catalog()
    LOGGER.info(
        "billing_api_startup",
        extra={
            "payload": {
                "event": "billing_api_startup",
                "project_id": SETTINGS.project_id,
                "region": SETTINGS.region,
                "catalog_environment": catalog.environment,
                "catalog_schema_version": catalog.schema_version,
                "build_sha": os.getenv("BILLING_API_BUILD_SHA", ""),
                "image_tag": os.getenv("BILLING_API_IMAGE_TAG", ""),
            }
        },
    )
    try:
        yield
    finally:
        LOGGER.info("billing_api_shutdown", extra={"payload": {"event": "billing_api_shutdown"}})


app = FastAPI(
    title="CEOsystem Billing API",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
app.include_router(health_router)
app.include_router(billing_router)
register_exception_handlers(app)
