"""Health and readiness endpoints for the persistence worker."""

from __future__ import annotations

from fastapi import APIRouter

from services.agent_persistence_worker_v3.app.core.config import get_settings


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "agent-persistence-worker",
        "status": "healthy",
    }


@router.get("/ready")
async def ready() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "service": "agent-persistence-worker",
        "status": "ready",
        "threads_collection": settings.threads_collection,
        "messages_subcollection": settings.messages_subcollection,
    }
