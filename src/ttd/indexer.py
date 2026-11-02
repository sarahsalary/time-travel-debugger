"""Time-based indexing for fast temporal queries."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from pathlib import Path

from .models import LogEvent
from .parser import LogParser


class TimeIndex:
    """An in-memory index of log events sorted by timestamp.

    Provides O(log n) lookups for ``at()``, ``between()`` and ``window()``
    operations, making it possible to time-travel through very large log
    files interactively.
    """

    def __init__(self, events: list[LogEvent]) -> None:
        self.events: list[LogEvent] = sorted(events, key=lambda e: e.timestamp)
        self._timestamps: list[datetime] = [e.timestamp for e in self.events]

    @classmethod
    def from_file(cls, path: str | Path, *, strict: bool = False) -> "TimeIndex":
        """Build an index directly from a JSONL file."""
        from .formats import open_log_file
        events = list(open_log_file(path, strict=strict).stream())
        return cls(events)

    def __len__(self) -> int:
        return len(self.events)

    def __bool__(self) -> bool:
        return bool(self.events)

    @property
    def start(self) -> datetime | None:
        """Timestamp of the earliest event, if any."""
        return self.events[0].timestamp if self.events else None

    @property
    def end(self) -> datetime | None:
        """Timestamp of the latest event, if any."""
        return self.events[-1].timestamp if self.events else None

    def at(self, moment: datetime) -> list[LogEvent]:
        """Return every event whose timestamp is <= ``moment``."""
        return self.events[: bisect_right(self._timestamps, moment)]

    def between(self, start: datetime, end: datetime) -> list[LogEvent]:
        """Return every event within the inclusive ``[start, end]`` range."""
        lo = bisect_left(self._timestamps, start)
        hi = bisect_right(self._timestamps, end)
        return self.events[lo:hi]

    def window(
        self,
        moment: datetime,
        *,
        before_s: float = 5.0,
        after_s: float = 5.0,
    ) -> list[LogEvent]:
        """Return events within a time window centered on ``moment``."""
        return self.between(
            moment - timedelta(seconds=before_s),
            moment + timedelta(seconds=after_s),
        )

    def latest(self, n: int = 20) -> list[LogEvent]:
        """Return the ``n`` most recent events."""
        return self.events[-n:] if n > 0 else []

    def first(self, n: int = 20) -> list[LogEvent]:
        """Return the ``n`` earliest events."""
        return self.events[:n]

    def nearest(self, moment: datetime, *, before: bool = True) -> LogEvent | None:
        """Find the event closest to ``moment`` (in either direction)."""
        if not self.events:
            return None
        if before:
            idx = bisect_right(self._timestamps, moment) - 1
            return self.events[idx] if idx >= 0 else None
        idx = bisect_left(self._timestamps, moment)
        return self.events[idx] if idx < len(self.events) else None