"""Lazy Firestore client access for Billing API operations."""

from __future__ import annotations

import threading
from typing import Any

from services.billing_api_v3.app.core.config import get_settings


_firestore_client: Any | None = None
_firestore_client_lock = threading.Lock()


def get_firestore_client() -> Any:
    global _firestore_client
    if _firestore_client is None:
        with _firestore_client_lock:
            if _firestore_client is None:
                from google.cloud import firestore

                _firestore_client = firestore.Client(project=get_settings().project_id)
    return _firestore_client


def get_transaction_document_snapshot(transaction: Any, document_ref: Any) -> Any:
    """Read one document within a Firestore transaction.

    ``google-cloud-firestore`` returns an iterator from ``Transaction.get``
    even when the requested reference is a single document.  Keep that SDK
    detail at this boundary while continuing to support the direct snapshot
    shape used by lightweight tests and older client releases.
    """

    result = transaction.get(document_ref)
    if hasattr(result, "exists") and callable(getattr(result, "to_dict", None)):
        return result

    try:
        snapshot = next(iter(result))
    except StopIteration as exc:
        raise RuntimeError("Firestore transaction read returned no document snapshot.") from exc

    if not hasattr(snapshot, "exists") or not callable(getattr(snapshot, "to_dict", None)):
        raise RuntimeError("Firestore transaction read returned an invalid document snapshot.")
    return snapshot
