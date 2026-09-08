"""Firestore helper utilities."""

from __future__ import annotations

from typing import Any


def get_transaction_document_snapshot(transaction: Any, document_ref: Any) -> Any:
    """Read one document within a Firestore transaction safely across SDK versions.

    ``google-cloud-firestore`` returns an iterator/generator from ``Transaction.get``
    when passed a document reference in modern SDK versions. Keep that SDK
    detail encapsulated here while continuing to support the direct snapshot
    shape used by mocks and unit tests.
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
