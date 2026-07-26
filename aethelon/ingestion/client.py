# -*- coding: utf-8 -*-
"""
aethelon.ingestion.client — Resilient asynchronous HTTP client (Stage B)
========================================================================
Production HTTP transport for RSS, calendar, and macro API ingestion.

Features
--------
* ``httpx.AsyncClient`` session lifecycle (``async with`` / ``aclose``)
* Exponential backoff with full jitter on 429, 5xx, timeouts, and connect errors
* Configurable ``RetryPolicy`` (Pydantic v2)
* Domain exceptions: ``IngestionNetworkError``, ``RateLimitExceededError``
* Structured logging via ``aethelon.core.logger`` (AppData ISO-8601 UTC)

Example
-------
::

    async with AsyncHttpClient() as client:
        response = await client.get("https://example.com/feed.xml")
        body = response.text
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from functools import wraps
from typing import Any, Optional, TypeVar, Union
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aethelon.core.logger import get_logger
from aethelon.ingestion.exceptions import (
    IngestionNetworkError,
    RateLimitExceededError,
)

__all__ = [
    "RetryPolicy",
    "AsyncHttpClient",
    "async_retry",
]

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# Default browser-like identity (many financial feeds block bare clients)
DEFAULT_USER_AGENT = (
    "AethelonMacroEngine/0.2 (+https://localhost; research; async-httpx)"
)
DEFAULT_ACCEPT = "application/json, application/xml, text/xml, text/html, */*;q=0.8"

# Status codes that warrant a retry under normal network resiliency policy
_RETRYABLE_STATUS: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)


# =============================================================================
# Retry policy (Pydantic v2)
# =============================================================================

class RetryPolicy(BaseModel):
    """
    Exponential-backoff configuration for transient HTTP failures.

    delay_n = min(max_delay, initial_delay * backoff_factor ** (attempt - 1))
    With ``jitter=True``, the sleep is drawn uniformly from ``[0, delay_n]``
    (full jitter — avoids synchronized thundering herds).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_retries: int = Field(
        default=3,
        ge=0,
        le=20,
        description="Retries after the first attempt (total tries = max_retries + 1)",
    )
    initial_delay: float = Field(
        default=0.5,
        gt=0.0,
        description="Base delay in seconds before the first retry",
    )
    backoff_factor: float = Field(
        default=2.0,
        ge=1.0,
        description="Exponential multiplier applied per retry attempt",
    )
    max_delay: float = Field(
        default=30.0,
        gt=0.0,
        description="Hard cap on computed backoff delay (seconds)",
    )
    jitter: bool = Field(
        default=True,
        description="If True, sleep U(0, delay) instead of the raw delay",
    )
    retry_on_status: tuple[int, ...] = Field(
        default=tuple(sorted(_RETRYABLE_STATUS)),
        description="HTTP status codes treated as transient",
    )

    @field_validator("max_delay")
    @classmethod
    def _max_ge_initial(cls, v: float, info: Any) -> float:
        initial = (info.data or {}).get("initial_delay")
        if initial is not None and v < float(initial):
            raise ValueError("max_delay must be >= initial_delay")
        return v

    def compute_delay(self, attempt: int) -> float:
        """
        Compute sleep seconds for the given 1-based retry attempt index.

        attempt=1 → first retry after the initial failure.
        """
        if attempt < 1:
            attempt = 1
        raw = self.initial_delay * (self.backoff_factor ** (attempt - 1))
        delay = min(self.max_delay, raw)
        if self.jitter:
            return random.uniform(0.0, delay)
        return delay


# =============================================================================
# Retry decorator / utility
# =============================================================================

def async_retry(
    policy: Optional[RetryPolicy] = None,
    *,
    operation: str = "http_request",
) -> Callable[[F], F]:
    """
    Decorator: retry an async callable under ``RetryPolicy``.

    The wrapped function must raise ``IngestionNetworkError`` /
    ``RateLimitExceededError`` (or subclasses) for retryable failures,
    or return an ``httpx.Response`` that the wrapper inspects for status.

    Prefer ``AsyncHttpClient`` methods in production code; this decorator
    is exported for custom ingestion coroutines that share the same policy.
    """
    pol = policy or RetryPolicy()

    def decorator(fn: F) -> F:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[BaseException] = None
            total_attempts = pol.max_retries + 1

            for attempt in range(1, total_attempts + 1):
                try:
                    result = await fn(*args, **kwargs)
                    if isinstance(result, httpx.Response):
                        if result.status_code in pol.retry_on_status:
                            raise _status_to_error(
                                result,
                                attempt=attempt,
                                method=str(kwargs.get("method") or getattr(result, "request", None)),
                            )
                    return result
                except RateLimitExceededError as exc:
                    last_exc = exc
                    if attempt > pol.max_retries:
                        log.error(
                            "%s rate-limited exhausted attempts=%s url=%s retry_after=%s",
                            operation,
                            attempt,
                            exc.url,
                            exc.retry_after,
                        )
                        raise
                    delay = _resolve_retry_after(exc.retry_after, pol, attempt)
                    log.warning(
                        "%s rate-limited attempt=%s/%s sleep=%.3fs url=%s",
                        operation,
                        attempt,
                        total_attempts,
                        delay,
                        exc.url,
                    )
                    await asyncio.sleep(delay)
                except IngestionNetworkError as exc:
                    last_exc = exc
                    if attempt > pol.max_retries:
                        log.error(
                            "%s failed permanently attempts=%s url=%s status=%s err=%s",
                            operation,
                            attempt,
                            exc.url,
                            exc.status_code,
                            exc.message,
                        )
                        raise
                    delay = pol.compute_delay(attempt)
                    log.warning(
                        "%s retry attempt=%s/%s sleep=%.3fs url=%s status=%s err=%s",
                        operation,
                        attempt,
                        total_attempts,
                        delay,
                        exc.url,
                        exc.status_code,
                        exc.message,
                    )
                    await asyncio.sleep(delay)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt > pol.max_retries:
                        log.error(
                            "%s transport exhausted attempts=%s err=%s",
                            operation,
                            attempt,
                            exc,
                        )
                        raise IngestionNetworkError(
                            f"Transport failure after {attempt} attempt(s): {exc}",
                            attempts=attempt,
                            cause=exc,
                        ) from exc
                    delay = pol.compute_delay(attempt)
                    log.warning(
                        "%s transport retry attempt=%s/%s sleep=%.3fs err=%s",
                        operation,
                        attempt,
                        total_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


def _resolve_retry_after(
    retry_after: Optional[float],
    policy: RetryPolicy,
    attempt: int,
) -> float:
    """Honour server Retry-After when present; otherwise exponential backoff."""
    if retry_after is not None and retry_after >= 0:
        return min(policy.max_delay, float(retry_after))
    return policy.compute_delay(attempt)


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Parse Retry-After header as seconds (delta or HTTP-date)."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            # Convert absolute date to delay; clamp negative to 0
            import datetime as _dt

            now = _dt.datetime.now(tz=_dt.timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return max(0.0, (dt - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _status_to_error(
    response: httpx.Response,
    *,
    attempt: int,
    method: Optional[str] = None,
) -> IngestionNetworkError:
    url = str(response.url)
    code = response.status_code
    meth = method or (response.request.method if response.request else "GET")
    if code == 429:
        return RateLimitExceededError(
            f"HTTP 429 Too Many Requests for {meth} {url}",
            url=url,
            method=meth,
            retry_after=_parse_retry_after(response),
            attempts=attempt,
        )
    return IngestionNetworkError(
        f"HTTP {code} for {meth} {url}",
        url=url,
        method=meth,
        status_code=code,
        attempts=attempt,
    )


# =============================================================================
# Async HTTP client
# =============================================================================

class AsyncHttpClient:
    """
    Resilient ``httpx.AsyncClient`` wrapper for Aethelon ingestion.

    Lifecycle
    ---------
    Prefer async context management::

        async with AsyncHttpClient() as http:
            r = await http.get(url)

    Or explicit open/close::

        http = AsyncHttpClient()
        await http.open()
        try:
            ...
        finally:
            await http.aclose()
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: Union[float, httpx.Timeout] = 30.0,
        headers: Optional[Mapping[str, str]] = None,
        retry: Optional[RetryPolicy] = None,
        follow_redirects: bool = True,
        verify: bool = True,
        http2: bool = False,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        user_agent: str = DEFAULT_USER_AGENT,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = (
            timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(timeout, connect=min(10.0, float(timeout)))
        )
        self._retry = retry or RetryPolicy()
        self._follow_redirects = follow_redirects
        self._verify = verify
        self._http2 = http2
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._default_headers: dict[str, str] = {
            "User-Agent": user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if headers:
            self._default_headers.update(dict(headers))

        self._client = client
        self._owns_client = client is None

    # ----- lifecycle ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._client is not None and not self._client.is_closed

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry

    async def open(self) -> "AsyncHttpClient":
        """Create the underlying ``httpx.AsyncClient`` if not already open."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._default_headers,
                follow_redirects=self._follow_redirects,
                verify=self._verify,
                http2=self._http2,
                limits=self._limits,
            )
            self._owns_client = True
            log.debug(
                "AsyncHttpClient opened base_url=%s timeout=%s",
                self._base_url or "(none)",
                self._timeout,
            )
        return self

    async def aclose(self) -> None:
        """Close the session when this wrapper owns it."""
        if self._client is not None and self._owns_client and not self._client.is_closed:
            await self._client.aclose()
            log.debug("AsyncHttpClient closed")
        self._client = None

    # Alias for callers that expect close()
    async def close(self) -> None:
        await self.aclose()

    async def __aenter__(self) -> "AsyncHttpClient":
        return await self.open()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            raise IngestionNetworkError(
                "AsyncHttpClient is not open; use 'async with' or await open()",
            )
        return self._client

    # ----- public verbs ------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        follow_redirects: Optional[bool] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> httpx.Response:
        """
        Perform a resilient HTTP GET.

        Returns
        -------
        httpx.Response
            Successful response (2xx/3xx after redirects as configured).
            Non-retryable 4xx (except 408/425/429) are returned as-is so
            callers can branch on status without try/except.

        Raises
        ------
        RateLimitExceededError
            Persistent HTTP 429.
        IngestionNetworkError
            Exhausted retries on timeouts, connect errors, or retryable 5xx.
        """
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            retry=retry,
        )

    async def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        content: Optional[Union[str, bytes]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        follow_redirects: Optional[bool] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> httpx.Response:
        """
        Perform a resilient HTTP POST.

        Same return / raise contract as :meth:`get`.
        """
        return await self.request(
            "POST",
            url,
            data=data,
            json=json,
            content=content,
            params=params,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            retry=retry,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json: Any = None,
        content: Optional[Union[str, bytes]] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        follow_redirects: Optional[bool] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> httpx.Response:
        """
        Core request path with exponential-backoff retries.

        Retryable: connect/timeout errors, HTTP 408/425/429/5xx.
        Non-retryable 4xx are returned to the caller without raising.
        """
        client = self._ensure_client()
        policy = retry or self._retry
        method_u = method.upper().strip()
        total_attempts = policy.max_retries + 1
        last_error: Optional[BaseException] = None
        host = _safe_host(url)

        for attempt in range(1, total_attempts + 1):
            try:
                log.debug(
                    "HTTP %s %s attempt=%s/%s host=%s",
                    method_u,
                    url,
                    attempt,
                    total_attempts,
                    host,
                )
                response = await client.request(
                    method_u,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    json=json,
                    content=content,
                    timeout=timeout if timeout is not None else self._timeout,
                    follow_redirects=(
                        self._follow_redirects
                        if follow_redirects is None
                        else follow_redirects
                    ),
                )

                if response.status_code in policy.retry_on_status:
                    err = _status_to_error(
                        response, attempt=attempt, method=method_u
                    )
                    # Consume body to release connection before retry
                    try:
                        await response.aread()
                    except Exception:
                        pass
                    raise err

                # Success or non-retryable client error — hand back cleanly
                if response.status_code >= 400:
                    log.info(
                        "HTTP %s %s status=%s (non-retryable) attempt=%s",
                        method_u,
                        url,
                        response.status_code,
                        attempt,
                    )
                else:
                    log.debug(
                        "HTTP %s %s status=%s bytes=%s",
                        method_u,
                        url,
                        response.status_code,
                        response.headers.get("content-length", "?"),
                    )
                return response

            except RateLimitExceededError as exc:
                last_error = exc
                if attempt > policy.max_retries:
                    log.error(
                        "HTTP %s %s rate-limit exhausted attempts=%s retry_after=%s",
                        method_u,
                        url,
                        attempt,
                        exc.retry_after,
                    )
                    raise RateLimitExceededError(
                        exc.message,
                        url=url,
                        method=method_u,
                        retry_after=exc.retry_after,
                        attempts=attempt,
                        cause=exc,
                    ) from exc
                delay = _resolve_retry_after(exc.retry_after, policy, attempt)
                log.warning(
                    "HTTP %s %s 429 backoff attempt=%s/%s sleep=%.3fs host=%s",
                    method_u,
                    url,
                    attempt,
                    total_attempts,
                    delay,
                    host,
                )
                await asyncio.sleep(delay)

            except IngestionNetworkError as exc:
                last_error = exc
                if attempt > policy.max_retries:
                    log.error(
                        "HTTP %s %s failed attempts=%s status=%s err=%s",
                        method_u,
                        url,
                        attempt,
                        exc.status_code,
                        exc.message,
                    )
                    raise IngestionNetworkError(
                        exc.message,
                        url=url,
                        method=method_u,
                        status_code=exc.status_code,
                        attempts=attempt,
                        cause=exc,
                    ) from exc
                delay = policy.compute_delay(attempt)
                log.warning(
                    "HTTP %s %s retry attempt=%s/%s sleep=%.3fs status=%s host=%s",
                    method_u,
                    url,
                    attempt,
                    total_attempts,
                    delay,
                    exc.status_code,
                    host,
                )
                await asyncio.sleep(delay)

            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt > policy.max_retries:
                    log.error(
                        "HTTP %s %s timeout exhausted attempts=%s err=%s",
                        method_u,
                        url,
                        attempt,
                        exc,
                    )
                    raise IngestionNetworkError(
                        f"Timeout after {attempt} attempt(s): {exc}",
                        url=url,
                        method=method_u,
                        attempts=attempt,
                        cause=exc,
                    ) from exc
                delay = policy.compute_delay(attempt)
                log.warning(
                    "HTTP %s %s timeout retry attempt=%s/%s sleep=%.3fs host=%s",
                    method_u,
                    url,
                    attempt,
                    total_attempts,
                    delay,
                    host,
                )
                await asyncio.sleep(delay)

            except httpx.TransportError as exc:
                last_error = exc
                if attempt > policy.max_retries:
                    log.error(
                        "HTTP %s %s transport exhausted attempts=%s err=%s",
                        method_u,
                        url,
                        attempt,
                        exc,
                    )
                    raise IngestionNetworkError(
                        f"Connection/transport failure after {attempt} attempt(s): {exc}",
                        url=url,
                        method=method_u,
                        attempts=attempt,
                        cause=exc,
                    ) from exc
                delay = policy.compute_delay(attempt)
                log.warning(
                    "HTTP %s %s transport retry attempt=%s/%s sleep=%.3fs host=%s err=%s",
                    method_u,
                    url,
                    attempt,
                    total_attempts,
                    delay,
                    host,
                    exc,
                )
                await asyncio.sleep(delay)

        # Defensive — loop always returns or raises
        raise IngestionNetworkError(
            f"HTTP {method_u} {url} failed with no response",
            url=url,
            method=method_u,
            attempts=total_attempts,
            cause=last_error,
        )


def _safe_host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url
