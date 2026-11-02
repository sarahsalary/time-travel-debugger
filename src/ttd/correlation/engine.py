"""Correlation engine that runs rules over a rolling event window."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from ..models import LogEvent
from .rules import CorrelationMatch, CorrelationRule


class CorrelationEngine:
    """Evaluate correlation rules against a sliding window of events.

    Unlike ``RuleEngine`` which evaluates on every batch, the correlation
    engine is designed to run periodically (e.g. every 5 seconds) because
    correlation windows are typically tens of seconds to minutes long.
    """

    def __init__(
        self,
        rules: list[CorrelationRule],
        *,
        window_seconds: float = 900.0,
        max_events: int = 50_000,
        on_match: Callable[[CorrelationMatch], None] | None = None,
    ) -> None:
        self.rules = list(rules)
        self.window_seconds = window_seconds
        self.max_events = max_events
        self.on_match = on_match

        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, eval_interval: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(eval_interval,),
            name="ttd-correlator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- ingestion ---------------------------------------------------------

    def add_events(self, events: list[LogEvent]) -> None:
        if not events:
            return
        with self._lock:
            self._events.extend(events)

    def snapshot(self) -> list[LogEvent]:
        with self._lock:
            return list(self._events)

    # -- evaluation --------------------------------------------------------

    def evaluate(self, *, now: datetime | None = None) -> list[CorrelationMatch]:
        events = self.snapshot()
        if not events:
            return []

        reference = now or events[-1].timestamp
        cutoff = reference - timedelta(seconds=self.window_seconds)
        window = [e for e in events if e.timestamp >= cutoff]

        matches: list[CorrelationMatch] = []
        for rule in self.rules:
            try:
                for match in rule.evaluate(window):
                    matches.append(match)
                    if self.on_match is not None:
                        try:
                            self.on_match(match)
                        except Exception:
                            pass
            except Exception:
                continue
        return matches

    # -- background --------------------------------------------------------

    def _run(self, eval_interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.evaluate()
            except Exception:
                pass
            self._stop.wait(eval_interval)