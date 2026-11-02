"""Reconstruct the application state at any point in time."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .indexer import TimeIndex
from .models import LogEvent


@dataclass
class AppState:
    """A snapshot of everything we know about the application at a moment."""

    moment: datetime
    event_count: int = 0
    services: list[str] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=dict)
    fields: dict[str, object] = field(default_factory=dict)
    last_event: LogEvent | None = None
    error_count: int = 0
    warning_count: int = 0

    @property
    def health(self) -> str:
        if self.error_count:
            return "ERROR"
        if self.warning_count:
            return "WARNING"
        return "OK"


def reconstruct_state(index: TimeIndex, moment: datetime) -> AppState:
    """Build an ``AppState`` describing the app at ``moment``."""
    relevant = index.at(moment)
    services: set[str] = set()
    levels: Counter[str] = Counter()
    fields: dict[str, object] = {}
    last_event: LogEvent | None = None
    errors = 0
    warnings = 0

    for event in relevant:
        last_event = event
        if event.service:
            services.add(event.service)
        levels[event.level] += 1
        if event.level in {"ERROR", "CRITICAL", "FATAL"}:
            errors += 1
        elif event.level in {"WARNING", "WARN"}:
            warnings += 1
        fields.update(event.extra)

    return AppState(
        moment=moment,
        event_count=len(relevant),
        services=sorted(services),
        levels=dict(levels),
        fields=fields,
        last_event=last_event,
        error_count=errors,
        warning_count=warnings,
    )


def service_activity(index: TimeIndex, moment: datetime) -> dict[str, int]:
    """Count how many events each service emitted up to ``moment``."""
    activity: dict[str, int] = defaultdict(int)
    for event in index.at(moment):
        if event.service:
            activity[event.service] += 1
    return dict(activity)