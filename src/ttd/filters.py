"""Composable filters for log events."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .models import LogEvent

Predicate = Callable[[LogEvent], bool]


class Filters:
    """Chainable predicates applied to streams of log events.

    Usage::

        filters = Filters().level("ERROR").service("api").contains("timeout")
        matching = filters.apply(events)
    """

    def __init__(self) -> None:
        self._predicates: list[Predicate] = []

    def _add(self, predicate: Predicate) -> "Filters":
        self._predicates.append(predicate)
        return self

    def level(self, *levels: str) -> "Filters":
        """Keep events whose level matches any of ``levels`` (case-insensitive)."""
        wanted = {lvl.upper() for lvl in levels}
        return self._add(lambda e: e.level.upper() in wanted)

    def min_level(self, level: str) -> "Filters":
        """Keep events at or above the given severity."""
        order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "WARN": 2,
                 "ERROR": 3, "CRITICAL": 4, "FATAL": 4}
        threshold = order.get(level.upper(), 1)
        return self._add(lambda e: order.get(e.level.upper(), 1) >= threshold)

    def service(self, *services: str) -> "Filters":
        """Keep events emitted by any of the named services."""
        wanted = set(services)
        return self._add(lambda e: e.service in wanted)

    def trace_id(self, *trace_ids: str) -> "Filters":
        """Keep events belonging to any of the given trace IDs."""
        wanted = set(trace_ids)
        return self._add(lambda e: e.trace_id in wanted)

    def contains(self, text: str, *, case_sensitive: bool = False) -> "Filters":
        """Keep events whose message contains ``text``."""
        needle = text if case_sensitive else text.lower()
        if case_sensitive:
            return self._add(lambda e: needle in e.message)
        return self._add(lambda e: needle in e.message.lower())

    def matches(self, pattern: str) -> "Filters":
        """Keep events whose message matches a regex pattern."""
        regex = re.compile(pattern)
        return self._add(lambda e: regex.search(e.message) is not None)

    def field(self, key: str, value: object | None = None) -> "Filters":
        """Keep events that have ``key`` in ``extra`` (optionally equal to ``value``)."""
        if value is None:
            return self._add(lambda e: key in e.extra)
        return self._add(lambda e: e.extra.get(key) == value)

    def slow(self, threshold_ms: float = 1000.0) -> "Filters":
        """Keep events whose duration exceeds ``threshold_ms``."""
        return self._add(
            lambda e: e.duration_ms is not None and e.duration_ms >= threshold_ms
        )

    def apply(self, events: Iterable[LogEvent]) -> list[LogEvent]:
        """Return every event satisfying all registered predicates."""
        return [e for e in events if all(p(e) for p in self._predicates)]

    @property
    def is_empty(self) -> bool:
        return not self._predicates