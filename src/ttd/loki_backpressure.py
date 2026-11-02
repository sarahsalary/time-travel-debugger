"""Adaptive concurrency control for the Loki client.

The controller borrows the AIMD (Additive Increase, Multiplicative
Decrease) algorithm from TCP congestion control. It starts at a modest
concurrency level, ramps up linearly while requests succeed quickly, and
cuts back sharply when the server signals overload — either with an
explicit HTTP 429, or implicitly via rising latency.

The implementation is deliberately simple and observable: every
adjustment is recorded with a reason, so users can log or inspect why the
concurrency changed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class BackpressureSignal(str, Enum):
    """Reasons the controller might adjust its limit."""

    SUCCESS = "success"
    SUCCESS_FAST = "success_fast"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    SLOW = "slow"
    ERROR = "error"
    COOLDOWN_EXPIRED = "cooldown_expired"


@dataclass
class Adjustment:
    """A single concurrency change, with the reason for it."""

    timestamp: float
    old_limit: int
    new_limit: int
    signal: BackpressureSignal
    latency: float = 0.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class BackpressureConfig:
    """Tunables for the adaptive concurrency controller."""

    #: Concurrency to start at. Should be low enough to be safe.
    initial_limit: int = 2

    #: Hard bounds on concurrency. ``min_limit`` is always maintained so
    #: the pool never starves; ``max_limit`` caps resource use.
    min_limit: int = 1
    max_limit: int = 16

    #: Additive increase applied on each successful, fast request.
    increase_rate: float = 0.5

    #: Multiplicative decrease applied on rate-limit or timeout.
    decrease_factor: float = 0.5

    #: Latency (seconds) above which a successful request is considered
    #: "slow" and triggers a small decrease instead of an increase.
    slow_latency_seconds: float = 2.0

    #: Latency (seconds) below which a successful request is considered
    #: "fast" and triggers an increase.
    fast_latency_seconds: float = 0.5

    #: Number of completed requests to consider before making another
    #: adjustment. Higher values make the controller less twitchy.
    measurement_window: int = 5

    #: Minimum seconds between two adjustments. Prevents oscillation when
    #: latency is noisy.
    cooldown_seconds: float = 1.0

    #: Success-rate threshold above which an increase is allowed.
    success_threshold: float = 0.95

    #: Success-rate threshold below which a decrease is applied.
    failure_threshold: float = 0.80

    def validate(self) -> None:
        if self.min_limit < 1:
            raise ValueError("min_limit must be >= 1")
        if self.max_limit < self.min_limit:
            raise ValueError("max_limit must be >= min_limit")
        if not self.min_limit <= self.initial_limit <= self.max_limit:
            raise ValueError(
                "initial_limit must be between min_limit and max_limit"
            )
        if self.increase_rate <= 0:
            raise ValueError("increase_rate must be > 0")
        if not 0.0 < self.decrease_factor < 1.0:
            raise ValueError("decrease_factor must be in (0, 1)")
        if self.slow_latency_seconds <= self.fast_latency_seconds:
            raise ValueError(
                "slow_latency_seconds must be > fast_latency_seconds"
            )
        if self.measurement_window < 1:
            raise ValueError("measurement_window must be >= 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        if not 0.0 <= self.failure_threshold <= self.success_threshold <= 1.0:
            raise ValueError(
                "thresholds must satisfy 0 <= failure <= success <= 1"
            )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


@dataclass
class ConcurrencyController:
    """AIMD-based concurrency controller with hysteresis."""

    config: BackpressureConfig
    on_adjust: Callable[[Adjustment], None] | None = None

    _limit: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _window: deque[tuple[float, bool]] = field(
        default_factory=deque, init=False
    )
    _last_adjust: float = field(default=0.0, init=False)
    _history: list[Adjustment] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.config.validate()
        self._limit = float(self.config.initial_limit)

    # -- public API --------------------------------------------------------

    @property
    def current_limit(self) -> int:
        """The currently allowed concurrency (rounded down)."""
        with self._lock:
            return max(self.config.min_limit, int(self._limit))

    @property
    def history(self) -> list[Adjustment]:
        with self._lock:
            return list(self._history)

    def record_success(self, latency: float) -> None:
        """Record a successful request and possibly adjust the limit."""
        with self._lock:
            self._window.append((latency, True))
            self._maybe_adjust()

    def record_failure(self, *, rate_limited: bool = False) -> None:
        """Record a failure. ``rate_limited`` triggers an immediate cut."""
        with self._lock:
            self._window.append((0.0, False))
            if rate_limited:
                # Rate-limit is a strong signal: cut immediately, do not
                # wait for the measurement window to fill up.
                self._apply_decrease(BackpressureSignal.RATE_LIMITED)
                self._window.clear()
            else:
                self._maybe_adjust()

    # -- internals ---------------------------------------------------------

    def _maybe_adjust(self) -> None:
        if len(self._window) < self.config.measurement_window:
            return

        now = time.monotonic()
        if now - self._last_adjust < self.config.cooldown_seconds:
            return

        successes = sum(1 for _, ok in self._window if ok)
        total = len(self._window)
        success_rate = successes / total if total else 0.0

        # Compute average latency of successful requests.
        latencies = [lat for lat, ok in self._window if ok]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        if success_rate < self.config.failure_threshold:
            self._apply_decrease(BackpressureSignal.ERROR, avg_latency)
        elif success_rate >= self.config.success_threshold:
            if avg_latency <= self.config.fast_latency_seconds:
                self._apply_increase(BackpressureSignal.SUCCESS_FAST, avg_latency)
            elif avg_latency <= self.config.slow_latency_seconds:
                self._apply_increase(BackpressureSignal.SUCCESS, avg_latency)
            else:
                self._apply_decrease(BackpressureSignal.SLOW, avg_latency)

        self._window.clear()

    def _apply_increase(
        self, signal: BackpressureSignal, latency: float = 0.0
    ) -> None:
        old = self._limit
        new = min(self._limit + self.config.increase_rate, self.config.max_limit)
        if abs(new - old) < 1e-9:
            return
        self._limit = new
        self._record(old, new, signal, latency)

    def _apply_decrease(
        self, signal: BackpressureSignal, latency: float = 0.0
    ) -> None:
        old = self._limit
        new = max(
            self._limit * self.config.decrease_factor,
            float(self.config.min_limit),
        )
        if abs(new - old) < 1e-9:
            return
        self._limit = new
        self._record(old, new, signal, latency)

    def _record(
        self,
        old: float,
        new: float,
        signal: BackpressureSignal,
        latency: float,
    ) -> None:
        self._last_adjust = time.monotonic()
        adjustment = Adjustment(
            timestamp=self._last_adjust,
            old_limit=max(self.config.min_limit, int(old)),
            new_limit=max(self.config.min_limit, int(new)),
            signal=signal,
            latency=latency,
        )
        self._history.append(adjustment)
        # Keep the history bounded.
        if len(self._history) > 200:
            self._history = self._history[-200:]
        if self.on_adjust is not None:
            try:
                self.on_adjust(adjustment)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Adaptive work pool
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveWorkPool:
    """A worker pool whose concurrency is controlled by a controller.

    Unlike ``ThreadPoolExecutor``, the pool here is *pull-based*: workers
    are spawned lazily and retire themselves when the controller shrinks
    the limit. This lets us adapt in both directions without killing
    in-flight work.
    """

    controller: ConcurrencyController

    _active: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def active_workers(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> bool:
        """Attempt to claim a worker slot. Returns True on success."""
        limit = self.controller.current_limit
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def wait_for_slot(
        self, *, poll_interval: float = 0.05, timeout: float | None = None
    ) -> bool:
        """Block until a slot is available. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval)