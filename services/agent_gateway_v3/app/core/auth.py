"""Firebase authentication helpers for the agent gateway."""

from __future__ import annotations

import asyncio
import threading

from fastapi import Request

from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError


_firebase_ready = False
_firebase_lock = threading.Lock()


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(
            401,
            "invalid_authorization_header",
            "Authorization must be a Bearer token.",
        )
    return token.strip()


def _ensure_firebase_initialized() -> None:
    import firebase_admin

    global _firebase_ready
    if _firebase_ready:
        return
    with _firebase_lock:
        if _firebase_ready:
            return
        firebase_admin.initialize_app()
        _firebase_ready = True


async def authenticate_request(request: Request) -> str:
    settings = get_settings()
    token = _extract_bearer_token(request.headers.get("Authorization"))

    if not token:
        if settings.firebase_auth_required:
            raise ApiError(
                401,
                "missing_bearer_token",
                "A Firebase ID token is required in the Authorization header.",
            )
        return "anonymous"

    try:
        from firebase_admin import auth as firebase_auth

        await asyncio.to_thread(_ensure_firebase_initialized)
        decoded_token = await asyncio.to_thread(firebase_auth.verify_id_token, token)
    except ModuleNotFoundError as exc:
        raise ApiError(
            500,
            "firebase_admin_missing",
            "firebase-admin is not installed in the gateway runtime environment.",
            {"reason": str(exc)},
        ) from exc
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            401,
            "invalid_firebase_token",
            "The Firebase ID token could not be verified.",
            {"reason": str(exc)},
        ) from exc

    user_id = str(decoded_token.get("uid", "")).strip()
    if not user_id:
        raise ApiError(
            401,
            "invalid_firebase_token",
            "The Firebase ID token did not include a valid user id.",
        )
    return user_id
