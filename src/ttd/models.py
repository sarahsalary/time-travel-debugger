"""Core data models for the RootRewind."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


class LogEvent(BaseModel):
    """A single structured log event.

    This is the atomic unit of the RootRewind. Every line in a
    JSONL file is parsed into one ``LogEvent`` instance.
    """

    timestamp: datetime
    level: str = "INFO"
    message: str = ""
    service: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    duration_ms: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, value: Any) -> datetime:
        """Accept ISO-8601 strings, epoch numbers, or datetime objects."""
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError:
                from dateutil import parser as dateparser

                dt = dateparser.parse(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        raise TypeError(f"Unsupported timestamp type: {type(value)!r}")

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> str:
        return str(value).upper() if value else "INFO"

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "LogEvent":
        """Build a ``LogEvent`` from an arbitrary JSON dictionary.

        Unknown keys are collected into the ``extra`` field so that no
        information is lost during parsing.
        """
        known_keys = {
            "timestamp", "time", "ts", "@timestamp",
            "level", "severity", "lvl",
            "message", "msg",
            "service", "logger", "component",
            "trace_id", "traceId", "trace",
            "span_id", "spanId",
            "parent_span_id", "parentSpanId",
            "duration_ms", "duration", "extra",
        }

        timestamp = (
            raw.get("timestamp")
            or raw.get("time")
            or raw.get("ts")
            or raw.get("@timestamp")
        )
        if timestamp is None:
            raise ValueError("Log event has no timestamp field")

        return cls(
            timestamp=timestamp,
            level=raw.get("level") or raw.get("severity") or raw.get("lvl") or "INFO",
            message=raw.get("message") or raw.get("msg") or "",
            service=raw.get("service") or raw.get("logger") or raw.get("component"),
            trace_id=raw.get("trace_id") or raw.get("traceId") or raw.get("trace"),
            span_id=raw.get("span_id") or raw.get("spanId"),
            parent_span_id=raw.get("parent_span_id") or raw.get("parentSpanId"),
            duration_ms=(raw.get("duration_ms") if raw.get("duration_ms") is not None else raw.get("duration")),
            extra={**(raw.get("extra") if isinstance(raw.get("extra"), dict) else {}), **{k: v for k, v in raw.items() if k not in known_keys}},
        )

    @property
    def short_message(self) -> str:
        """Truncated message suitable for one-line display."""
        return self.message if len(self.message) <= 80 else self.message[:77] + "..."