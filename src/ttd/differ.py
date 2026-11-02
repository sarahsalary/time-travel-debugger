"""Compare two points in time and report what changed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .indexer import TimeIndex
from .models import LogEvent
from .state import AppState, reconstruct_state


@dataclass
class FieldChange:
    key: str
    before: object
    after: object

    def __str__(self) -> str:
        return f"{self.key}: {self.before!r} -> {self.after!r}"


@dataclass
class DiffResult:
    before_moment: datetime
    after_moment: datetime
    before: AppState
    after: AppState
    added_services: list[str] = field(default_factory=list)
    removed_services: list[str] = field(default_factory=list)
    field_changes: list[FieldChange] = field(default_factory=list)
    new_errors: list[LogEvent] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added_services
            or self.removed_services
            or self.field_changes
            or self.new_errors
        )


def diff_states(
    index: TimeIndex, before_moment: datetime, after_moment: datetime
) -> DiffResult:
    """Compare the application state at two moments."""
    before = reconstruct_state(index, before_moment)
    after = reconstruct_state(index, after_moment)

    before_services = set(before.services)
    after_services = set(after.services)

    field_changes: list[FieldChange] = []
    for key in set(before.fields) | set(after.fields):
        old = before.fields.get(key)
        new = after.fields.get(key)
        if old != new:
            field_changes.append(FieldChange(key=key, before=old, after=new))

    new_errors = [
        e
        for e in index.between(before_moment, after_moment)
        if e.level in {"ERROR", "CRITICAL", "FATAL"}
    ]

    return DiffResult(
        before_moment=before_moment,
        after_moment=after_moment,
        before=before,
        after=after,
        added_services=sorted(after_services - before_services),
        removed_services=sorted(before_services - after_services),
        field_changes=sorted(field_changes, key=lambda c: c.key),
        new_errors=new_errors,
    )