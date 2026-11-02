"""Dispatch alerts to every configured notification channel.

The dispatcher runs delivery in a small thread pool so that a slow
webhook (or a network timeout) never blocks the watch engine. Failures
are collected and reported through a callback, and the dispatcher keeps a
small deduplication window to avoid hammering a channel with the same
alert repeated in quick succession.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..watch import Alert, AlertKind
from .channels import NotificationChannel, NotificationError
from .config import AlertConfig, severity_passes


@dataclass
class DeliveryResult:
    """The outcome of delivering one alert to one channel."""

    channel: str
    alert_kind: str
    success: bool
    error: str = ""


@dataclass
class AlertDispatcher:
    """Deliver alerts to a set of channels, asynchronously.

    Parameters
    ----------
    channels:
        The channels to deliver to.
    max_workers:
        Thread pool size. One worker per channel is plenty.
    dedup_window:
        Number of seconds during which a duplicate alert (same kind and
        message) is suppressed.
    on_result:
        Optional callback invoked after every delivery attempt.
    """

    channels: list[NotificationChannel]
    min_severity: str = "low"
    include_kinds: set[str] = field(default_factory=set)
    exclude_kinds: set[str] = field(default_factory=set)
    max_workers: int = 4
    dedup_window: float = 60.0
    on_result: Callable[[DeliveryResult], None] | None = None

    _executor: ThreadPoolExecutor | None = None
    _dedup: deque[tuple[float, str]] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: AlertConfig,
        *,
        on_result: Callable[[DeliveryResult], None] | None = None,
    ) -> "AlertDispatcher":
        return cls(
            channels=list(config.channels),
            min_severity=config.min_severity,
            include_kinds=set(config.include_kinds),
            exclude_kinds=set(config.exclude_kinds),
            on_result=on_result,
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._executor is None and self.channels:
            self._executor = ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(self.channels)),
                thread_name_prefix="ttd-alert",
            )

    def stop(self, *, wait: bool = True) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, alert: Alert, *, context: dict | None = None) -> None:
        """Queue an alert for delivery to every matching channel."""
        if not self.channels:
            return

        if not severity_passes(alert.severity, self.min_severity):
            return
        if self.include_kinds and alert.kind.value not in self.include_kinds:
            return
        if self.exclude_kinds and alert.kind.value in self.exclude_kinds:
            return
        if self._is_duplicate(alert):
            return

        self.start()
        for channel in self.channels:
            assert self._executor is not None
            self._executor.submit(self._deliver, channel, alert, context)

    # -- internals ---------------------------------------------------------

    def _deliver(
        self,
        channel: NotificationChannel,
        alert: Alert,
        context: dict | None,
    ) -> None:
        result: DeliveryResult
        try:
            channel.send(alert, context=context)
            result = DeliveryResult(
                channel=channel.name,
                alert_kind=alert.kind.value,
                success=True,
            )
        except NotificationError as exc:
            result = DeliveryResult(
                channel=channel.name,
                alert_kind=alert.kind.value,
                success=False,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — never let a channel crash the engine
            result = DeliveryResult(
                channel=channel.name,
                alert_kind=alert.kind.value,
                success=False,
                error=f"unexpected: {exc}",
            )

        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                pass

    def _is_duplicate(self, alert: Alert) -> bool:
        """Suppress the same (kind, message) within the dedup window."""
        now = time.monotonic()
        key = f"{alert.kind.value}:{alert.message}"

        with self._lock:
            while self._dedup and now - self._dedup[0][0] > self.dedup_window:
                self._dedup.popleft()
            if any(k == key for _, k in self._dedup):
                return True
            self._dedup.append((now, key))
        return False