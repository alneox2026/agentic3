"""Firebase authentication for FlutterFlow-facing Billing API routes."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

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


_reconciliation_token_verifier: Callable[[str, str], dict[str, Any]] | None = None


def set_reconciliation_token_verifier(
    verifier: Callable[[str, str], dict[str, Any]] | None,
) -> None:
    """Override OIDC token verifier for testing."""
    global _reconciliation_token_verifier
    _reconciliation_token_verifier = verifier


async def authenticate_reconciliation_request(request: Request) -> dict[str, Any]:
    """Verify Cloud Scheduler Google OIDC token protecting internal reconciliation."""
    from services.billing_api_v3.app.core.config import get_settings

    settings = get_settings()
    if not settings.reconciliation_auth_required:
        return {"sub": "anonymous", "email": "anonymous"}

    expected_audience = (settings.reconciliation_audience or "").strip()
    expected_service_account = (settings.reconciliation_allowed_service_account or "").strip()

    if not expected_audience or not expected_service_account:
        raise BillingApiError(
            500,
            "reconciliation_auth_misconfigured",
            "Reconciliation OIDC audience and allowed service account must be configured when reconciliation auth is enabled.",
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise BillingApiError(
            401,
            "missing_reconciliation_token",
            "A Google OIDC token is required in the Authorization header.",
        )

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise BillingApiError(
            401,
            "invalid_authorization_header",
            "Authorization must be a Bearer token.",
        )

    try:
        if _reconciliation_token_verifier is not None:
            claims = _reconciliation_token_verifier(token.strip(), expected_audience)
        else:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import id_token

            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                token.strip(),
                GoogleAuthRequest(),
                audience=expected_audience,
            )
    except BillingApiError:
        raise
    except Exception as exc:
        raise BillingApiError(
            401,
            "invalid_reconciliation_token",
            f"The Google OIDC token could not be verified: {exc}",
        ) from exc

    token_audience = str(claims.get("aud", "")).strip()
    if token_audience != expected_audience:
        raise BillingApiError(
            401,
            "invalid_reconciliation_audience",
            f"Scheduler OIDC token audience mismatch (expected {expected_audience}).",
        )

    token_email = str(claims.get("email", "")).strip()
    if token_email != expected_service_account:
        raise BillingApiError(
            403,
            "forbidden_service_account",
            f"Scheduler OIDC token service account mismatch (expected {expected_service_account}).",
        )

    return claims

