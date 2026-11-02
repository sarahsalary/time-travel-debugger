"""Correlation rule definitions for multi-event pattern detection.

Correlation rules look across *multiple* events within a time window and
fire when a pattern is observed. This is different from the single-event
rules in ``ttd.rules`` which evaluate one event at a time.

Three rule shapes are supported:

* **SequenceRule** — match events A, B, C in order within a window.
* **ThresholdCorrelationRule** — match N events matching a query within
  a window (a.k.a. "frequency" rules in ElastAlert2).
* **WindowRule** — match a sliding count over a fixed time bucket
  (useful for "more than 10 errors in any 1-minute period").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..models import LogEvent
from ..query import Query, QuerySyntaxError


# ---------------------------------------------------------------------------
# Match result
# ---------------------------------------------------------------------------


@dataclass
class CorrelationMatch:
    """A successful correlation, ready to be turned into an alert."""

    rule_name: str
    severity: str
    message: str
    matched_events: list[LogEvent] = field(default_factory=list)
    groups: dict[str, Any] = field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None

    @property
    def event_count(self) -> int:
        return len(self.matched_events)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class CorrelationRule(ABC):
    """Abstract base for multi-event correlation rules."""

    name: str = "correlation"
    description: str = ""

    def __init__(
        self,
        *,
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 300.0,
    ) -> None:
        if name:
            self.name = name
        self.severity = severity
        self.cooldown = cooldown
        self._last_fired: float = 0.0

    @abstractmethod
    def evaluate(self, events: list[LogEvent]) -> list[CorrelationMatch]:
        """Return every match found in the given window of events."""

    def _cooldown_active(self) -> bool:
        import time

        return (time.monotonic() - self._last_fired) < self.cooldown

    def _mark_fired(self) -> None:
        import time

        self._last_fired = time.monotonic()

    @staticmethod
    def _compile(query: str) -> Query:
        try:
            return Query(query) if query else Query("")
        except QuerySyntaxError as exc:
            raise ValueError(f"Invalid correlation query {query!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# SequenceRule
# ---------------------------------------------------------------------------


class SequenceRule(CorrelationRule):
    """Match a sequence of queries in order within a time window.

    Example: detect a brute-force attempt followed by a success::

        SequenceRule(
            name="bruteforce-then-success",
            steps=[
                "message ~ 'login failed'",
                "message ~ 'login failed'",
                "message ~ 'login failed'",
                "message ~ 'login success'",
            ],
            timeframe_seconds=300,
        )
    """

    def __init__(
        self,
        steps: list[str],
        *,
        timeframe_seconds: float = 300.0,
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 300.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        if len(steps) < 2:
            raise ValueError("SequenceRule requires at least two steps")
        self.step_queries = [self._compile(s) for s in steps]
        self.step_text = list(steps)
        self.timeframe_seconds = max(1.0, timeframe_seconds)
        if name is None:
            self.name = f"sequence:{len(steps)}-steps/{int(timeframe_seconds)}s"

    def evaluate(self, events: list[LogEvent]) -> list[CorrelationMatch]:
        if self._cooldown_active():
            return []

        ordered = sorted(events, key=lambda e: e.timestamp)
        matches: list[CorrelationMatch] = []

        # Greedy scan: for each possible start, try to satisfy all steps.
        i = 0
        n = len(ordered)
        while i < n:
            start_event = ordered[i]
            window_end = start_event.timestamp + timedelta(
                seconds=self.timeframe_seconds
            )
            step_index = 0
            matched: list[LogEvent] = []
            j = i
            while j < n and step_index < len(self.step_queries):
                event = ordered[j]
                if event.timestamp > window_end:
                    break
                if self.step_queries[step_index].matches(event):
                    matched.append(event)
                    step_index += 1
                j += 1

            if step_index == len(self.step_queries):
                matches.append(
                    CorrelationMatch(
                        rule_name=self.name,
                        severity=self.severity,
                        message=(
                            f"{self.name}: sequence of {len(self.step_queries)} "
                            f"events matched within {int(self.timeframe_seconds)}s"
                        ),
                        matched_events=matched,
                        window_start=matched[0].timestamp,
                        window_end=matched[-1].timestamp,
                    )
                )
                self._mark_fired()
                # Continue scanning after the first matched event.
                i = j
                continue
            i += 1
        return matches


# ---------------------------------------------------------------------------
# ThresholdCorrelationRule
# ---------------------------------------------------------------------------


class ThresholdCorrelationRule(CorrelationRule):
    """Fire when a query matches N events within a time window."""

    def __init__(
        self,
        query: str,
        *,
        threshold: int,
        timeframe_seconds: float = 60.0,
        group_by: str | None = None,
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 120.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        self.query_text = query
        self.query = self._compile(query)
        self.threshold = max(1, threshold)
        self.timeframe_seconds = max(1.0, timeframe_seconds)
        self.group_by = group_by
        if name is None:
            self.name = f"threshold:{threshold}/{int(timeframe_seconds)}s"

    def evaluate(self, events: list[LogEvent]) -> list[CorrelationMatch]:
        if self._cooldown_active():
            return []

        ordered = sorted(events, key=lambda e: e.timestamp)
        matching = [e for e in ordered if self.query.matches(e)]
        if not matching:
            return []

        # Group by a field if requested (e.g. by user, by IP).
        groups: dict[str, list[LogEvent]] = defaultdict(list)
        for event in matching:
            key = self._group_key(event)
            groups[key].append(event)

        matches: list[CorrelationMatch] = []
        for key, group in groups.items():
            for start_index in range(len(group)):
                start_ts = group[start_index].timestamp
                end_ts = start_ts + timedelta(seconds=self.timeframe_seconds)
                window = [e for e in group[start_index:] if e.timestamp <= end_ts]
                if len(window) >= self.threshold:
                    matches.append(
                        CorrelationMatch(
                            rule_name=self.name,
                            severity=self.severity,
                            message=(
                                f"{self.name}: {len(window)} matching events "
                                f"for group {key!r} within "
                                f"{int(self.timeframe_seconds)}s"
                            ),
                            matched_events=window,
                            groups={"key": key},
                            window_start=window[0].timestamp,
                            window_end=window[-1].timestamp,
                        )
                    )
                    self._mark_fired()
                    break  # one match per group is enough
        return matches

    def _group_key(self, event: LogEvent) -> str:
        if not self.group_by:
            return "all"
        parts = self.group_by.split(".")
        current: Any = event
        for part in parts:
            if part in ("extra",):
                current = current.extra
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
            if current is None:
                return "unknown"
        return str(current)


# ---------------------------------------------------------------------------
# WindowRule
# ---------------------------------------------------------------------------


class WindowRule(CorrelationRule):
    """Sliding-window count over fixed-size time buckets."""

    def __init__(
        self,
        query: str,
        *,
        threshold: int,
        bucket_seconds: float = 60.0,
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 120.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        self.query_text = query
        self.query = self._compile(query)
        self.threshold = max(1, threshold)
        self.bucket_seconds = max(1.0, bucket_seconds)
        if name is None:
            self.name = f"window:{threshold}/{int(bucket_seconds)}s"

    def evaluate(self, events: list[LogEvent]) -> list[CorrelationMatch]:
        if self._cooldown_active():
            return []

        matching = sorted(
            (e for e in events if self.query.matches(e)),
            key=lambda e: e.timestamp,
        )
        if not matching:
            return []

        matches: list[CorrelationMatch] = []
        i = 0
        while i < len(matching):
            start_ts = matching[i].timestamp
            # Align to a fixed boundary so buckets are consistent.
            epoch = start_ts.timestamp()
            aligned = epoch - (epoch % self.bucket_seconds)
            end_ts = datetime.fromtimestamp(aligned + self.bucket_seconds, tz=start_ts.tzinfo)
            window = [e for e in matching[i:] if e.timestamp < end_ts]
            if len(window) >= self.threshold:
                matches.append(
                    CorrelationMatch(
                        rule_name=self.name,
                        severity=self.severity,
                        message=(
                            f"{self.name}: {len(window)} events matched in "
                            f"the {int(self.bucket_seconds)}s window "
                            f"starting {start_ts.isoformat()}"
                        ),
                        matched_events=window,
                        window_start=window[0].timestamp,
                        window_end=window[-1].timestamp,
                    )
                )
                self._mark_fired()
                i += len(window)
                continue
            i += 1
        return matches