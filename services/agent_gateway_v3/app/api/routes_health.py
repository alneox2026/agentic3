"""Health and readiness endpoints for the agent gateway."""

from __future__ import annotations

from fastapi import APIRouter


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "agent-gateway",
        "status": "healthy",
    }


@router.get("/ready")
async def ready() -> dict[str, object]:
    return {
        "ok": True,
        "service": "agent-gateway",
        "status": "ready",
    }
