"""Circuit breaker for the Loki client.

The breaker guards against cascading failures when Loki becomes
unavailable. It implements the classic three-state machine:

* **CLOSED** — requests pass through normally. Consecutive failures are
  counted; when the count reaches ``failure_threshold`` the breaker
  transitions to OPEN.

* **OPEN** — every request is rejected immediately with a
  ``CircuitOpenError``. No HTTP call is attempted, which frees workers
  to do other useful work (or fail fast so the user sees the problem
  right away). After ``reset_timeout_seconds`` the breaker transitions
  to HALF_OPEN.

* **HALF_OPEN** — a limited number of trial requests are allowed through.
  If ``success_threshold`` consecutive successes occur, the breaker
  returns to CLOSED. A single failure sends it back to OPEN and the
  reset timer restarts.

The breaker is thread-safe. It also exposes an ``on_state_change``
callback so callers can log transitions or surface them in the TUI.

Failure classification is delegated to a ``FailureClassifier`` so that
different callers can decide what counts as a failure. The default
classifier treats 5xx responses, 429, timeouts, and network errors as
failures, and everything else as a success for accounting purposes.
"""

from __future__ import annotations

import threading
import time
import urllib.error
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


# ---------------------------------------------------------------------------
# States and errors
# ---------------------------------------------------------------------------


class CircuitState(str, Enum):
    """The three states a circuit breaker can be in."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a request is rejected because the circuit is OPEN."""

    def __init__(self, retry_in_seconds: float) -> None:
        self.retry_in_seconds = retry_in_seconds
        super().__init__(
            f"Circuit breaker is OPEN; retry in {retry_in_seconds:.1f}s"
        )


# ---------------------------------------------------------------------------
# State transition record
# ---------------------------------------------------------------------------


@dataclass
class StateTransition:
    """A record of one breaker state change."""

    timestamp: float
    old_state: CircuitState
    new_state: CircuitState
    reason: str
    failure_count: int = 0
    success_count: int = 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CircuitConfig:
    """Tunables for the circuit breaker."""

    #: Consecutive failures that trip the breaker from CLOSED to OPEN.
    failure_threshold: int = 5

    #: Consecutive successes in HALF_OPEN that close the breaker.
    success_threshold: int = 2

    #: Seconds the breaker stays OPEN before allowing trial requests.
    reset_timeout_seconds: float = 60.0

    #: Maximum concurrent trial requests in HALF_OPEN state. Prevents a
    #: thundering herd of trial requests when the breaker half-opens.
    half_open_max_requests: int = 3

    #: How many state transitions to keep in history (for observability).
    history_limit: int = 100

    def validate(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be >= 1")
        if self.reset_timeout_seconds < 0:
            raise ValueError("reset_timeout_seconds must be >= 0")
        if self.half_open_max_requests < 1:
            raise ValueError("half_open_max_requests must be >= 1")
        if self.history_limit < 1:
            raise ValueError("history_limit must be >= 1")


# ---------------------------------------------------------------------------
# Failure classifier
# ---------------------------------------------------------------------------


class FailureClassifier:
    """Decide whether an exception should count as a circuit failure.

    The default implementation treats these as failures:

    * HTTP 5xx responses
    * HTTP 429 (rate limit)
    * HTTP 408 (request timeout)
    * ``URLError`` (connection refused, DNS failure)
    * ``TimeoutError`` and ``ConnectionError``

    Everything else is treated as a success for accounting purposes.
    The idea is that client errors (400, 404) indicate a bug in *us*,
    not an outage in Loki, so they should not trip the breaker.
    """

    def __init__(
        self,
        *,
        failure_status_codes: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504),
    ) -> None:
        self.failure_status_codes = failure_status_codes

    def is_failure(self, exc: BaseException) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in self.failure_status_codes
        if isinstance(exc, urllib.error.URLError):
            return True
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        # RetryExhausted and CircuitOpenError are handled separately by
        # the caller and never reach this classifier.
        return False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """A thread-safe circuit breaker with observable state transitions.

    Usage::

        cb = CircuitBreaker(CircuitConfig(failure_threshold=3))

        try:
            cb.before_request()
        except CircuitOpenError as exc:
            # Fail fast without touching the network.
            handle_unavailable(exc.retry_in_seconds)
            return

        try:
            result = do_http_call()
        except Exception as exc:
            if cb.on_failure(exc):
                raise
            return
        cb.on_success()
        return result

    The breaker uses a monotonic clock so system clock changes do not
    affect the reset timer.
    """

    def __init__(
        self,
        config: CircuitConfig | None = None,
        *,
        classifier: FailureClassifier | None = None,
        on_state_change: Callable[[StateTransition], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or CircuitConfig()
        self.config.validate()
        self.classifier = classifier or FailureClassifier()
        self._on_state_change = on_state_change
        self._clock = clock

        self._lock = threading.RLock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float = 0.0
        self._half_open_in_flight = 0
        self._history: list[StateTransition] = []

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._refresh_state()
            return self._state

    @property
    def history(self) -> list[StateTransition]:
        with self._lock:
            return list(self._history)

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def before_request(self) -> None:
        """Assert that a request is allowed to proceed.

        Raises ``CircuitOpenError`` if the breaker is OPEN or if the
        HALF_OPEN trial quota has been used up. Otherwise returns
        silently, having reserved a trial slot if appropriate.
        """
        with self._lock:
            self._refresh_state()

            if self._state is CircuitState.OPEN:
                retry_in = max(
                    0.0,
                    self.config.reset_timeout_seconds
                    - (self._clock() - self._opened_at),
                )
                raise CircuitOpenError(retry_in)

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self.config.half_open_max_requests:
                    # All trial slots are in use; treat this as if the
                    # breaker were still open.
                    raise CircuitOpenError(self.config.reset_timeout_seconds)
                self._half_open_in_flight += 1

    def on_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            self._refresh_state()

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight > 0:
                    self._half_open_in_flight -= 1
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition(
                        CircuitState.CLOSED,
                        reason=f"{self._success_count} consecutive successes in HALF_OPEN",
                    )
                    self._reset_counters()
                return

            # CLOSED: reset the failure streak on any success.
            if self._failure_count > 0:
                self._failure_count = 0

    def on_failure(self, exc: BaseException | None = None) -> bool:
        """Record a failed request.

        Returns True if the exception is classified as a circuit failure
        and was counted, False otherwise. This lets callers distinguish
        "the breaker ignored this error" from "the breaker counted it".
        """
        if exc is not None and not self.classifier.is_failure(exc):
            return False

        with self._lock:
            self._refresh_state()

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight > 0:
                    self._half_open_in_flight -= 1
                # A single failure in HALF_OPEN trips back to OPEN.
                self._transition(
                    CircuitState.OPEN,
                    reason="failure during HALF_OPEN trial",
                )
                self._failure_count = self.config.failure_threshold
                self._success_count = 0
                self._opened_at = self._clock()
                return True

            # CLOSED: count the failure.
            self._failure_count += 1
            if self._failure_count >= self.config.failure_threshold:
                self._transition(
                    CircuitState.OPEN,
                    reason=(
                        f"{self._failure_count} consecutive failures "
                        f"reached threshold"
                    ),
                )
                self._opened_at = self._clock()
                self._success_count = 0
                return True

            return True

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED.

        Useful in tests and for admin commands that want to force a
        recovery after fixing an external problem.
        """
        with self._lock:
            self._transition(
                CircuitState.CLOSED, reason="manual reset"
            )
            self._reset_counters()

    def snapshot(self) -> dict:
        """Return a snapshot of the breaker's state for logging or UI."""
        with self._lock:
            self._refresh_state()
            now = self._clock()
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "half_open_in_flight": self._half_open_in_flight,
                "opened_seconds_ago": (
                    now - self._opened_at if self._opened_at else 0.0
                ),
                "reset_timeout_seconds": self.config.reset_timeout_seconds,
                "history_size": len(self._history),
            }

    # -- internals ---------------------------------------------------------

    def _refresh_state(self) -> None:
        """Promote OPEN to HALF_OPEN when the reset timer elapses."""
        if self._state is not CircuitState.OPEN:
            return
        elapsed = self._clock() - self._opened_at
        if elapsed >= self.config.reset_timeout_seconds:
            self._transition(
                CircuitState.HALF_OPEN,
                reason=f"reset timeout elapsed ({elapsed:.1f}s)",
            )
            self._success_count = 0
            self._half_open_in_flight = 0

    def _reset_counters(self) -> None:
        self._failure_count = 0
        self._success_count = 0
        self._half_open_in_flight = 0

    def _transition(self, new_state: CircuitState, *, reason: str) -> None:
        if new_state is self._state:
            return
        old_state = self._state
        self._state = new_state
        record = StateTransition(
            timestamp=self._clock(),
            old_state=old_state,
            new_state=new_state,
            reason=reason,
            failure_count=self._failure_count,
            success_count=self._success_count,
        )
        self._history.append(record)
        if len(self._history) > self.config.history_limit:
            self._history = self._history[-self.config.history_limit:]
        if self._on_state_change is not None:
            try:
                self._on_state_change(record)
            except Exception:
                # A misbehaving callback must never break the breaker.
                pass