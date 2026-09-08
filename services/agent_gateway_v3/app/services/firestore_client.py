"""Lazy Firestore client access for gateway lifecycle operations."""

from __future__ import annotations

import threading
from typing import Any

from services.agent_gateway_v3.app.core.config import get_settings


_firestore_client: Any | None = None
_firestore_client_lock = threading.Lock()


def get_firestore_client():
    global _firestore_client
    if _firestore_client is None:
        with _firestore_client_lock:
            if _firestore_client is None:
                from google.cloud import firestore

                settings = get_settings()
                _firestore_client = firestore.Client(project=settings.project_id)
    return _firestore_client
