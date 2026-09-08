"""Defense-in-depth authentication checks for Eventarc-delivered worker events."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

from services.agent_persistence_worker_v3.app.core.config import get_settings


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Authorization must be a Bearer token.")
    return token.strip()


async def verify_eventarc_request(request: Request) -> None:
    settings = get_settings()
    if not settings.eventarc_auth_required:
        return

    token = _extract_bearer_token(request.headers.get("Authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="Missing Eventarc bearer token.")
    if not settings.eventarc_audience:
        raise HTTPException(status_code=500, detail="Eventarc audience is not configured.")

    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token,
            GoogleAuthRequest(),
            settings.eventarc_audience,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Eventarc bearer token.") from exc

    allowed_service_account = settings.eventarc_allowed_service_account
    if allowed_service_account:
        token_email = str(claims.get("email", "")).strip()
        if token_email != allowed_service_account:
            raise HTTPException(status_code=403, detail="Unexpected Eventarc service account.")
