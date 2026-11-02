"""Coordinate rule evaluation over a rolling window of log events.

The ``RuleEngine`` is designed to be driven by the watch engine: it
receives batches of events, keeps a bounded history, and re-evaluates
every rule each time a batch arrives. Rules are also evaluated on a
timer so that frequency rules can fire even when no new events arrive
(the window simply empties out).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from ..models import LogEvent
from .rules import AlertRule, RuleResult


class RuleEngine:
    """Evaluate a set of rules against a rolling event window."""

    def __init__(
        self,
        rules: list[AlertRule],
        *,
        window_seconds: float = 300.0,
        max_events: int = 10_000,
        on_rule_fired: Callable[[RuleResult], None] | None = None,
    ) -> None:
        self.rules = list(rules)
        self.window_seconds = window_seconds
        self.max_events = max_events
        self.on_rule_fired = on_rule_fired

        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_eval = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, eval_interval: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(eval_interval,),
            name="ttd-rule-engine",
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
        self.evaluate_all()

    def snapshot(self) -> list[LogEvent]:
        with self._lock:
            return list(self._events)

    # -- evaluation --------------------------------------------------------

    def evaluate_all(self, *, now: datetime | None = None) -> list[RuleResult]:
        events = self.snapshot()
        if not events:
            return []

        # Drop events outside the rolling window.
        reference = now or events[-1].timestamp
        cutoff = reference - timedelta(seconds=self.window_seconds)
        window = [e for e in events if e.timestamp >= cutoff]

        fired: list[RuleResult] = []
        for rule in self.rules:
            try:
                result = rule.evaluate(window, now=reference)
            except Exception:
                continue
            if result.fired:
                fired.append(result)
                if self.on_rule_fired is not None:
                    try:
                        self.on_rule_fired(result)
                    except Exception:
                        pass
        return fired

    # -- background loop ---------------------------------------------------

    def _run(self, eval_interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.evaluate_all()
            except Exception:
                pass
            self._stop.wait(eval_interval)