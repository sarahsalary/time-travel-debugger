"""OpenTelemetry Protocol (OTLP) decoder.

Translates OTLP payloads (both JSON and protobuf-over-HTTP) into the
internal ``LogEvent`` model so that every downstream feature — TTDQL,
anomaly detection, clustering, the TUI and the MCP server — works
unchanged against OTLP data.
"""

from __future__ import annotations

import json
from typing import Any

from .models import LogEvent


# ---------------------------------------------------------------------------
# Attribute conversion
# ---------------------------------------------------------------------------


def _attr_value(value: dict[str, Any]) -> Any:
    """Unwrap a single OTLP ``AnyValue`` into a plain Python value."""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "bytesValue" in value:
        import base64

        try:
            return base64.b64decode(value["bytesValue"]).decode("utf-8", "replace")
        except Exception:
            return value["bytesValue"]
    if "arrayValue" in value:
        items = value["arrayValue"].get("values", [])
        return [_attr_value(item) for item in items]
    if "kvlistValue" in value:
        entries = value["kvlistValue"].get("values", [])
        return {e.get("key"): _attr_value(e.get("value", {})) for e in entries}
    return None


def attrs_to_dict(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert an OTLP attribute list to a flat Python dictionary."""
    result: dict[str, Any] = {}
    for attr in attributes or []:
        key = attr.get("key")
        if key is None:
            continue
        result[key] = _attr_value(attr.get("value", {}))
    return result


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------


def _nanos_to_iso(nanos: int | str | None) -> str | None:
    """Convert Unix nanoseconds (as int or string) to an ISO timestamp."""
    if nanos is None:
        return None
    try:
        nanos_int = int(nanos)
    except (TypeError, ValueError):
        return None
    if nanos_int == 0:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(nanos_int / 1e9, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Log records
# ---------------------------------------------------------------------------


def _log_record_to_event(
    record: dict[str, Any],
    *,
    resource_attrs: dict[str, Any],
    scope_attrs: dict[str, Any],
) -> LogEvent | None:
    """Convert a single OTLP ``LogRecord`` into a ``LogEvent``."""
    body = record.get("body", {})
    message = _attr_value(body) if isinstance(body, dict) else str(body)
    if message is None:
        message = ""

    ts = _nanos_to_iso(record.get("timeUnixNano")) or _nanos_to_iso(
        record.get("observedTimeUnixNano")
    )
    if ts is None:
        return None

    log_attrs = attrs_to_dict(record.get("attributes", []))
    service = (
        log_attrs.get("service.name")
        or resource_attrs.get("service.name")
        or resource_attrs.get("service_name")
    )

    extra: dict[str, Any] = {}
    extra.update(resource_attrs)
    extra.update(log_attrs)
    if scope_attrs:
        extra["otel.scope.name"] = scope_attrs.get("name")
        extra["otel.scope.version"] = scope_attrs.get("version")

    return LogEvent.from_raw(
        {
            "timestamp": ts,
            "level": (record.get("severityText") or "INFO").upper(),
            "message": message,
            "service": service,
            "trace_id": record.get("traceId") or None,
            "span_id": record.get("spanId") or None,
            "extra": {k: v for k, v in extra.items() if v is not None},
        }
    )


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


def _span_to_event(
    span: dict[str, Any],
    *,
    resource_attrs: dict[str, Any],
    scope_attrs: dict[str, Any],
) -> LogEvent | None:
    """Convert a single OTLP ``Span`` into a ``LogEvent``."""
    start = span.get("startTimeUnixNano")
    end = span.get("endTimeUnixNano")

    ts = _nanos_to_iso(start)
    if ts is None:
        return None

    duration_ms: float | None = None
    if start and end:
        try:
            duration_ms = (int(end) - int(start)) / 1e6
        except (TypeError, ValueError):
            duration_ms = None

    span_attrs = attrs_to_dict(span.get("attributes", []))
    service = (
        span_attrs.get("service.name")
        or resource_attrs.get("service.name")
        or resource_attrs.get("service_name")
    )

    # Derive a level from the span status.
    status = span.get("status", {}) or {}
    status_code = status.get("code")
    level = "INFO"
    if status_code in (2, "STATUS_CODE_ERROR", "ERROR"):
        level = "ERROR"
    elif span_attrs.get("otel.status_code") == "ERROR":
        level = "ERROR"

    message = span.get("name") or span.get("kind") or "span"

    extra: dict[str, Any] = {}
    extra.update(resource_attrs)
    extra.update(span_attrs)
    if scope_attrs:
        extra["otel.scope.name"] = scope_attrs.get("name")
        extra["otel.scope.version"] = scope_attrs.get("version")
    if status.get("message"):
        extra["otel.status_message"] = status["message"]
    kind = span.get("kind")
    if kind is not None:
        extra["otel.span.kind"] = kind

    return LogEvent.from_raw(
        {
            "timestamp": ts,
            "level": level,
            "message": message,
            "service": service,
            "trace_id": span.get("traceId") or None,
            "span_id": span.get("spanId") or None,
            "parent_span_id": span.get("parentSpanId") or None,
            "duration_ms": duration_ms,
            "extra": {k: v for k, v in extra.items() if v is not None},
        }
    )


# ---------------------------------------------------------------------------
# Envelope walkers
# ---------------------------------------------------------------------------


def decode_logs_payload(payload: dict[str, Any]) -> list[LogEvent]:
    """Decode an OTLP ``ExportLogsServiceRequest`` into events."""
    events: list[LogEvent] = []
    for resource_log in payload.get("resourceLogs", []) or []:
        resource_attrs = attrs_to_dict(
            resource_log.get("resource", {}).get("attributes", [])
        )
        for scope_log in resource_log.get("scopeLogs", []) or []:
            scope_attrs = scope_log.get("scope", {}) or {}
            for record in scope_log.get("logRecords", []) or []:
                event = _log_record_to_event(
                    record,
                    resource_attrs=resource_attrs,
                    scope_attrs=scope_attrs,
                )
                if event is not None:
                    events.append(event)
    return events


def decode_traces_payload(payload: dict[str, Any]) -> list[LogEvent]:
    """Decode an OTLP ``ExportTraceServiceRequest`` into events."""
    events: list[LogEvent] = []
    for resource_span in payload.get("resourceSpans", []) or []:
        resource_attrs = attrs_to_dict(
            resource_span.get("resource", {}).get("attributes", [])
        )
        for scope_span in resource_span.get("scopeSpans", []) or []:
            scope_attrs = scope_span.get("scope", {}) or {}
            for span in scope_span.get("spans", []) or []:
                event = _span_to_event(
                    span,
                    resource_attrs=resource_attrs,
                    scope_attrs=scope_attrs,
                )
                if event is not None:
                    events.append(event)
    return events


def decode_any_payload(payload: dict[str, Any]) -> list[LogEvent]:
    """Decode an OTLP payload regardless of whether it carries logs or traces."""
    if "resourceLogs" in payload:
        return decode_logs_payload(payload)
    if "resourceSpans" in payload:
        return decode_traces_payload(payload)
    return []


def decode_jsonl_stream(text: str) -> list[LogEvent]:
    """Decode a JSONL stream where each line is one OTLP export envelope."""
    events: list[LogEvent] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        events.extend(decode_any_payload(payload))
    return events