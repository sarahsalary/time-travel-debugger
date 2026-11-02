"""Alert rule definitions.

Each rule inspects a rolling window of ``LogEvent`` objects and returns a
``RuleResult`` when its condition fires. Rules are intentionally simple:
they do not call external services or emit alerts directly. The
``RuleEngine`` is responsible for coordinating them and producing alerts.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import LogEvent


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class RuleResult:
    """The outcome of evaluating a rule against a window of events."""

    rule_name: str
    fired: bool
    severity: str = "medium"
    message: str = ""
    matched_events: list[LogEvent] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base rule
# ---------------------------------------------------------------------------


class AlertRule(ABC):
    """Abstract base for alert rules."""

    name: str = "rule"
    description: str = ""

    def __init__(
        self,
        *,
        name: str | None = None,
        severity: str = "medium",
        cooldown: float = 60.0,
    ) -> None:
        if name:
            self.name = name
        self.severity = severity
        self.cooldown = cooldown
        self._last_fired: float = 0.0

    @abstractmethod
    def evaluate(
        self, events: list[LogEvent], *, now: datetime | None = None
    ) -> RuleResult:
        """Evaluate the rule against a list of events."""

    def _cooldown_active(self, now_monotonic: float) -> bool:
        return (now_monotonic - self._last_fired) < self.cooldown

    def _mark_fired(self, now_monotonic: float) -> None:
        self._last_fired = now_monotonic


# ---------------------------------------------------------------------------
# ThresholdRule
# ---------------------------------------------------------------------------


class ThresholdRule(AlertRule):
    """Fire when a numeric field crosses a threshold.

    Example: fire when ``duration_ms > 5000`` occurs in any event.

    The rule counts matching events and fires when the count meets or
    exceeds ``min_occurrences`` within the evaluation window.
    """

    def __init__(
        self,
        field: str,
        *,
        gt: float | None = None,
        lt: float | None = None,
        gte: float | None = None,
        lte: float | None = None,
        min_occurrences: int = 1,
        name: str | None = None,
        severity: str = "medium",
        cooldown: float = 60.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        self.field = field
        self.gt = gt
        self.lt = lt
        self.gte = gte
        self.lte = lte
        self.min_occurrences = max(1, min_occurrences)
        if name is None:
            op = _describe_operator(gt, lt, gte, lte)
            self.name = f"threshold:{field}{op}"

    def evaluate(
        self, events: list[LogEvent], *, now: datetime | None = None
    ) -> RuleResult:
        import time

        matched = [
            e for e in events
            if self._event_matches(e)
        ]
        if len(matched) < self.min_occurrences:
            return RuleResult(rule_name=self.name, fired=False)

        if self._cooldown_active(time.monotonic()):
            return RuleResult(rule_name=self.name, fired=False)

        self._mark_fired(time.monotonic())
        op = _describe_operator(self.gt, self.lt, self.gte, self.lte)
        return RuleResult(
            rule_name=self.name,
            fired=True,
            severity=self.severity,
            message=(
                f"{self.name}: {len(matched)} event(s) matched "
                f"'{self.field}{op}'"
            ),
            matched_events=matched[-5:],
            metrics={"matches": float(len(matched))},
        )

    def _event_matches(self, event: LogEvent) -> bool:
        value = _resolve_field(event, self.field)
        if value is None:
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        if self.gt is not None and not (numeric > self.gt):
            return False
        if self.lt is not None and not (numeric < self.lt):
            return False
        if self.gte is not None and not (numeric >= self.gte):
            return False
        if self.lte is not None and not (numeric <= self.lte):
            return False
        return True


def _describe_operator(
    gt: float | None,
    lt: float | None,
    gte: float | None,
    lte: float | None,
) -> str:
    if gt is not None:
        return f">{gt}"
    if lt is not None:
        return f"<{lt}"
    if gte is not None:
        return f">={gte}"
    if lte is not None:
        return f"<={lte}"
    return ""


# ---------------------------------------------------------------------------
# FrequencyRule
# ---------------------------------------------------------------------------


class FrequencyRule(AlertRule):
    """Fire when a query matches more than ``num_events`` in ``timeframe``.

    Mirrors the ``frequency`` rule type from ElastAlert2[reference:3]:
    "alert when this many documents matching the query occur within a
    timeframe".
    """

    def __init__(
        self,
        *,
        num_events: int,
        timeframe_seconds: float,
        query: str = "",
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 120.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        self.num_events = max(1, num_events)
        self.timeframe_seconds = max(1.0, timeframe_seconds)
        self.query = query
        if name is None:
            self.name = f"frequency:{num_events}/{int(timeframe_seconds)}s"

    def evaluate(
        self, events: list[LogEvent], *, now: datetime | None = None
    ) -> RuleResult:
        import time

        if not events:
            return RuleResult(rule_name=self.name, fired=False)

        now_dt = now or events[-1].timestamp
        cutoff = now_dt - timedelta(seconds=self.timeframe_seconds)

        window = [e for e in events if e.timestamp >= cutoff]
        if self.query:
            from ..query import Query, QuerySyntaxError

            try:
                window = Query(self.query).filter(window)
            except QuerySyntaxError:
                window = []

        if len(window) < self.num_events:
            return RuleResult(rule_name=self.name, fired=False)

        if self._cooldown_active(time.monotonic()):
            return RuleResult(rule_name=self.name, fired=False)

        self._mark_fired(time.monotonic())
        return RuleResult(
            rule_name=self.name,
            fired=True,
            severity=self.severity,
            message=(
                f"{self.name}: {len(window)} events matched in the last "
                f"{int(self.timeframe_seconds)}s (threshold: {self.num_events})"
            ),
            matched_events=window[-5:],
            metrics={"matches": float(len(window))},
        )


# ---------------------------------------------------------------------------
# PatternRule
# ---------------------------------------------------------------------------


class PatternRule(AlertRule):
    """Fire when a message matches a regular expression.

    Useful for detecting specific error codes, HTTP statuses, or message
    prefixes without needing a full query language.
    """

    def __init__(
        self,
        pattern: str,
        *,
        field: str = "message",
        case_sensitive: bool = False,
        min_occurrences: int = 1,
        name: str | None = None,
        severity: str = "high",
        cooldown: float = 60.0,
    ) -> None:
        super().__init__(name=name, severity=severity, cooldown=cooldown)
        flags = 0 if case_sensitive else re.IGNORECASE
        self._regex = re.compile(pattern, flags)
        self.pattern = pattern
        self.field = field
        self.min_occurrences = max(1, min_occurrences)
        if name is None:
            self.name = f"pattern:{pattern[:40]}"

    def evaluate(
        self, events: list[LogEvent], *, now: datetime | None = None
    ) -> RuleResult:
        import time

        matched = [e for e in events if self._event_matches(e)]
        if len(matched) < self.min_occurrences:
            return RuleResult(rule_name=self.name, fired=False)
        if self._cooldown_active(time.monotonic()):
            return RuleResult(rule_name=self.name, fired=False)

        self._mark_fired(time.monotonic())
        return RuleResult(
            rule_name=self.name,
            fired=True,
            severity=self.severity,
            message=(
                f"{self.name}: {len(matched)} event(s) matched "
                f"/{self.pattern}/"
            ),
            matched_events=matched[-5:],
            metrics={"matches": float(len(matched))},
        )

    def _event_matches(self, event: LogEvent) -> bool:
        value = _resolve_field(event, self.field)
        if value is None:
            return False
        return bool(self._regex.search(str(value)))


# ---------------------------------------------------------------------------
# Field resolution (shared with query.py semantics)
# ---------------------------------------------------------------------------


_RESERVED = {
    "timestamp", "level", "message", "service",
    "trace_id", "span_id", "parent_span_id", "duration_ms",
}


def _resolve_field(event: LogEvent, path: str):
    if path in _RESERVED:
        return getattr(event, path)
    extra_path = path[6:] if path.startswith("extra.") else path
    current = event.extra
    for part in extra_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current