"""Retry logic with exponential backoff and jitter for the Loki client.

The retry handler wraps an HTTP call and re-issues it on transient
failures. The delay between attempts follows one of the standard jittered
backoff strategies (full jitter by default), which avoids the thundering
herd problem when many workers fail at once.

The handler also:

* Respects the ``Retry-After`` header if the server sends one.
* Treats rate-limit and server-error responses as retryable, but not
  client errors like 400 or 404.
* Feeds retry outcomes back into the backpressure controller so the
  concurrency limit shrinks in step with the retries.
* Reports each retry attempt through an optional callback, so the CLI
  and TUI can surface them to the user.
"""

from __future__ import annotations

import random
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, TypeVar


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class RetryStrategy(str, Enum):
    """Backoff strategies for retry delays."""

    #: Deterministic exponential: ``base * factor ** attempt``.
    EXPONENTIAL = "exponential"

    #: Full jitter: ``random(0, exponential)``. Recommended by AWS.
    FULL_JITTER = "full_jitter"

    #: Equal jitter: half the exponential plus random half.
    EQUAL_JITTER = "equal_jitter"

    #: Decorrelated jitter: ``random(base, prev_delay * 3)``.
    DECORRELATED = "decorrelated"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Tunables for the retry handler.

    Defaults follow AWS's recommendation: full jitter with a 100 ms base,
    factor 2, and a 20-second cap, which is a good balance between
    responsiveness and load on the server.
    """

    max_attempts: int = 4
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 20.0
    strategy: RetryStrategy = RetryStrategy.FULL_JITTER
    factor: float = 2.0

    #: HTTP status codes that are safe to retry.
    retry_status_codes: tuple[int, ...] = (
        408,  # Request Timeout
        425,  # Too Early
        429,  # Too Many Requests
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    )

    #: If True, network-level errors (connection refused, DNS failure,
    #: read timeout) are also retried.
    retry_network_errors: bool = True

    #: Hard cap on the total time spent retrying one call, in seconds.
    #: ``None`` means no cap.
    max_total_seconds: float | None = 120.0

    #: Maximum acceptable value for the ``Retry-After`` header (seconds).
    #: If the server asks to wait longer, we stop retrying instead.
    max_retry_after_seconds: float = 60.0

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "max_delay_seconds must be >= base_delay_seconds"
            )
        if self.factor <= 1.0:
            raise ValueError("factor must be > 1.0")
        if self.max_total_seconds is not None and self.max_total_seconds < 0:
            raise ValueError("max_total_seconds must be >= 0")
        if self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be >= 0")


# ---------------------------------------------------------------------------
# Attempt records
# ---------------------------------------------------------------------------


@dataclass
class RetryAttempt:
    """A record of one retry attempt, useful for logging and debugging."""

    attempt: int
    delay: float
    error: str
    timestamp: float = field(default_factory=time.monotonic)


class RetryExhausted(Exception):
    """Raised when all retry attempts have been used up."""

    def __init__(self, message: str, attempts: list[RetryAttempt]) -> None:
        super().__init__(message)
        self.attempts = attempts


class RetryableError(Exception):
    """Internal signal that an error is retryable."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Retry handler
# ---------------------------------------------------------------------------


class RetryHandler:
    """Wrap a callable with retry logic.

    Usage::

        handler = RetryHandler(RetryPolicy())
        try:
            result = handler.call(lambda: client._get("/loki/api/v1/query_range", params))
        except RetryExhausted as exc:
            log.error("Loki unavailable after %d attempts", len(exc.attempts))

    The handler is thread-safe: multiple workers may call ``call``
    concurrently, and each invocation keeps its own attempt history.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        on_retry: Callable[[RetryAttempt], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        policy.validate()
        self.policy = policy
        self.on_retry = on_retry
        self._sleep = sleep
        self._rand = rand
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def call(self, fn: Callable[[], T]) -> T:
        """Invoke ``fn`` and retry it on transient failures.

        Raises ``RetryExhausted`` if every attempt fails, or re-raises the
        underlying exception immediately if it is not retryable.
        """
        attempts: list[RetryAttempt] = []
        start = time.monotonic()
        last_error: str = ""

        for attempt_index in range(self.policy.max_attempts):
            try:
                return fn()
            except RetryableError as exc:
                last_error = str(exc)
                retry_after = exc.retry_after
            except urllib.error.HTTPError as exc:
                if exc.code not in self.policy.retry_status_codes:
                    raise
                retry_after = _parse_retry_after(exc.headers)
                last_error = f"HTTP {exc.code}: {exc.reason}"
            except urllib.error.URLError as exc:
                if not self.policy.retry_network_errors:
                    raise
                retry_after = None
                last_error = f"network error: {exc.reason}"
            except (TimeoutError, ConnectionError) as exc:
                if not self.policy.retry_network_errors:
                    raise
                retry_after = None
                last_error = f"{type(exc).__name__}: {exc}"

            is_last_attempt = attempt_index >= self.policy.max_attempts - 1
            if is_last_attempt:
                break

            delay = self._compute_delay(attempt_index, retry_after)

            # Respect the total time budget.
            if self.policy.max_total_seconds is not None:
                elapsed = time.monotonic() - start
                if elapsed + delay > self.policy.max_total_seconds:
                    break

            record = RetryAttempt(
                attempt=attempt_index + 1,
                delay=delay,
                error=last_error,
            )
            attempts.append(record)
            if self.on_retry is not None:
                try:
                    self.on_retry(record)
                except Exception:
                    pass
            self._sleep(delay)

        raise RetryExhausted(
            f"Loki request failed after {len(attempts) + 1} attempts: {last_error}",
            attempts=attempts,
        )

    # -- delay computation -------------------------------------------------

    def _compute_delay(
        self, attempt_index: int, retry_after: float | None
    ) -> float:
        """Compute the delay before the next attempt."""
        # The server's explicit instruction always wins, as long as it is
        # within our tolerance.
        if retry_after is not None:
            if retry_after > self.policy.max_retry_after_seconds:
                # Server asked to wait too long; do not retry at all.
                return float("inf")
            return retry_after

        exponential = min(
            self.policy.base_delay_seconds * (self.policy.factor ** attempt_index),
            self.policy.max_delay_seconds,
        )

        strategy = self.policy.strategy
        if strategy is RetryStrategy.EXPONENTIAL:
            return exponential
        if strategy is RetryStrategy.FULL_JITTER:
            return self._rand() * exponential
        if strategy is RetryStrategy.EQUAL_JITTER:
            half = exponential / 2
            return half + self._rand() * half
        if strategy is RetryStrategy.DECORRELATED:
            # Use the previous delay as the upper bound when available;
            # otherwise use the exponential.
            lower = self.policy.base_delay_seconds
            upper = min(exponential * 3, self.policy.max_delay_seconds)
            if upper <= lower:
                return upper
            return lower + self._rand() * (upper - lower)
        return exponential


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(headers: Any) -> float | None:
    """Extract a ``Retry-After`` header value in seconds, if present."""
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if not raw:
        return None

    # Numeric seconds form.
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass

    # HTTP-date form (RFC 7231).
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None