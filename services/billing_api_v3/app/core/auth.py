"""Firebase authentication for FlutterFlow-facing Billing API routes."""

from __future__ import annotations

import asyncio
import threading

from fastapi import Request

from services.billing_api_v3.app.core.errors import BillingApiError


_firebase_ready = False
_firebase_lock = threading.Lock()


def _extract_bearer_token(authorization_header: str | None) -> str:
    if not authorization_header:
        raise BillingApiError(
            401,
            "missing_bearer_token",
            "A Firebase ID token is required in the Authorization header.",
        )
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise BillingApiError(
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
        if not _firebase_ready:
            firebase_admin.initialize_app()
            _firebase_ready = True


async def authenticate_request(request: Request) -> str:
    """Return the verified Firebase UID; never accept a UID from the request body."""

    token = _extract_bearer_token(request.headers.get("Authorization"))
    try:
        from firebase_admin import auth as firebase_auth

        await asyncio.to_thread(_ensure_firebase_initialized)
        decoded_token = await asyncio.to_thread(firebase_auth.verify_id_token, token)
    except BillingApiError:
        raise
    except ModuleNotFoundError as exc:
        raise BillingApiError(
            500,
            "firebase_admin_missing",
            "firebase-admin is not installed in the Billing API runtime.",
        ) from exc
    except Exception as exc:
        raise BillingApiError(
            401,
            "invalid_firebase_token",
            "The Firebase ID token could not be verified.",
        ) from exc

    user_id = str(decoded_token.get("uid", "")).strip()
    if not user_id:
        raise BillingApiError(
            401,
            "invalid_firebase_token",
            "The Firebase ID token did not include a valid user id.",
        )
    return user_id
