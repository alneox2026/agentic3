"""Worker error types."""

from __future__ import annotations


class RetryableWorkerError(Exception):
    """Signals that the event should be retried."""


class NonRetryableWorkerError(Exception):
    """Signals that the event should be acknowledged and dropped."""

