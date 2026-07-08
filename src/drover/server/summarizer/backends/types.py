"""Shared types for summarizer backends — split out to break import cycles."""

from __future__ import annotations


class BackendError(RuntimeError):
    """Backend was unable to produce a usable summary."""


class BackendReadinessError(BackendError):
    """Backend is temporarily not ready to accept work.

    Workers should treat this as retryable readiness/cold-start state rather
    than as a failed generation attempt.
    """
