"""Health and readiness endpoints for the Billing API."""

from __future__ import annotations

from fastapi import APIRouter

from services.billing_api_v3.app.services.billing_catalog import get_billing_catalog


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    catalog = get_billing_catalog()
    return {
        "ok": True,
        "service": "billing-api",
        "status": "healthy",
        "catalog_schema_version": catalog.schema_version,
        "catalog_environment": catalog.environment,
    }


@router.get("/ready")
async def ready() -> dict[str, object]:
    get_billing_catalog()
    return {
        "ok": True,
        "service": "billing-api",
        "status": "ready",
    }
