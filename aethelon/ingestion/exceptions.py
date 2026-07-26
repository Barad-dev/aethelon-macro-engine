# -*- coding: utf-8 -*-
"""Domain exceptions for the Aethelon ingestion plane."""

from __future__ import annotations

from typing import Any, Optional


class IngestionError(Exception):
    """Base class for ingestion-layer failures."""

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}


class IngestionNetworkError(IngestionError):
    """
    Raised when an HTTP request ultimately fails after retries
    (timeouts, connection errors, persistent 5xx, or unexpected transport faults).
    """

    def __init__(
        self,
        message: str,
        *,
        url: Optional[str] = None,
        method: Optional[str] = None,
        status_code: Optional[int] = None,
        attempts: Optional[int] = None,
        cause: Optional[BaseException] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        payload = dict(details or {})
        if url is not None:
            payload.setdefault("url", url)
        if method is not None:
            payload.setdefault("method", method)
        if status_code is not None:
            payload.setdefault("status_code", status_code)
        if attempts is not None:
            payload.setdefault("attempts", attempts)
        super().__init__(message, details=payload)
        self.url = url
        self.method = method
        self.status_code = status_code
        self.attempts = attempts
        self.__cause__ = cause


class RateLimitExceededError(IngestionNetworkError):
    """
    Raised when HTTP 429 responses persist beyond the configured retry budget,
    or when a Retry-After directive cannot be satisfied within policy limits.
    """

    def __init__(
        self,
        message: str,
        *,
        url: Optional[str] = None,
        method: Optional[str] = None,
        retry_after: Optional[float] = None,
        attempts: Optional[int] = None,
        cause: Optional[BaseException] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        payload = dict(details or {})
        if retry_after is not None:
            payload.setdefault("retry_after", retry_after)
        super().__init__(
            message,
            url=url,
            method=method,
            status_code=429,
            attempts=attempts,
            cause=cause,
            details=payload,
        )
        self.retry_after = retry_after
