"""Client for Grafana Loki's HTTP query API with cursor-based pagination."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from .models import LogEvent


class LokiError(Exception):
    """Raised when a Loki request fails."""


@dataclass
class LokiClient:
    """A minimal Loki HTTP API client with pagination support."""

    url: str
    tenant: str | None = None
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0
    # Page size for paginated queries. Loki caps this around 5000.
    page_size: int = 1000
    retry_policy: Any = None
    circuit_config: Any = None
    on_circuit_change: Any = None

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")
        from .loki_retry import RetryHandler, RetryPolicy
        from .loki_circuit import CircuitBreaker
        self._retry_handler = RetryHandler(self.retry_policy or RetryPolicy())
        self._circuit = CircuitBreaker(self.circuit_config, on_state_change=self.on_circuit_change) if self.circuit_config else None

    # -- public API --------------------------------------------------------

    def query_range(
        self,
        logql: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
        direction: str = "forward",
    ) -> list[LogEvent]:
        """Run a LogQL range query and return events in chronological order.

        This method fetches a single page. For large result sets, use
        ``stream_range`` instead.
        """
        if end is None:
            end = datetime.now(timezone.utc)
        if start is None:
            start = end - timedelta(hours=1)

        params = {
            "query": logql,
            "start": _to_nanos(start),
            "end": _to_nanos(end),
            "limit": str(limit),
            "direction": direction,
        }
        response = self._get("/loki/api/v1/query_range", params)
        return list(self._parse_response(response))

    def stream_range(
        self,
        logql: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_events: int | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> Iterator[LogEvent]:
        """Yield events from Loki, paging forward automatically.

        The client issues repeated ``query_range`` calls with
        ``direction=forward``. After each page, the cursor advances to the
        timestamp of the last event received (plus one nanosecond to
        respect Loki's inclusive ``start`` boundary)[reference:1].

        Parameters
        ----------
        logql:
            The LogQL expression to run.
        start, end:
            Time bounds. Defaults to the last hour if ``start`` is omitted.
        max_events:
            Stop after yielding this many events. ``None`` means unlimited.
        on_progress:
            Optional callback invoked after each page with the total number
            of events yielded so far, and the expected total if Loki
            reports it (it usually does not).
        """
        if end is None:
            end = datetime.now(timezone.utc)
        if start is None:
            start = end - timedelta(hours=1)
        if start >= end:
            return

        cursor_ns = int(start.timestamp() * 1_000_000_000)
        end_ns = int(end.timestamp() * 1_000_000_000)
        yielded = 0
        page_size = self.page_size

        while cursor_ns < end_ns:
            if max_events is not None and yielded >= max_events:
                break

            # Respect the remaining budget so we never over-fetch.
            remaining = None
            if max_events is not None:
                remaining = max_events - yielded
            limit = page_size if remaining is None else min(page_size, remaining)
            if limit <= 0:
                break

            params = {
                "query": logql,
                "start": str(cursor_ns),
                "end": _to_nanos(end),
                "limit": str(limit),
                "direction": "forward",
            }
            response = self._get("/loki/api/v1/query_range", params)
            page_events = sorted(self._parse_response(response), key=lambda event: (int(event.extra.get("loki_timestamp_ns", 0)), event.message))

            if not page_events:
                break

            for event in page_events:
                yield event
                yielded += 1
                if max_events is not None and yielded >= max_events:
                    break

            if on_progress is not None:
                on_progress(yielded, max_events)

            # Advance the cursor past the last event of this page.
            last_ns = int(page_events[-1].extra.get("loki_timestamp_ns", _to_nanos(page_events[-1].timestamp)))
            if len(page_events) >= limit and (
                last_ns == cursor_ns
                or (len(page_events) > 1 and last_ns == int(page_events[-2].extra.get("loki_timestamp_ns", last_ns - 1)))
            ):
                raise LokiError("Loki pagination boundary is ambiguous: a full page shares a timestamp. Narrow the time range or increase page size.")
            cursor_ns = max(cursor_ns + 1, last_ns + 1)

            if len(page_events) < limit:
                # The page was not full: we have reached the end of the
                # available data.
                break

        if on_progress is not None:
            on_progress(yielded, yielded)

    def query_instant(
        self,
        logql: str,
        *,
        at: datetime | None = None,
        limit: int = 1000,
    ) -> list[LogEvent]:
        """Run a LogQL instant query (single point in time)."""
        if at is None:
            at = datetime.now(timezone.utc)
        params = {
            "query": logql,
            "time": _to_nanos(at),
            "limit": str(limit),
        }
        response = self._get("/loki/api/v1/query", params)
        return list(self._parse_response(response))

    def labels(self) -> list[str]:
        """Return every label name known to Loki."""
        response = self._get("/loki/api/v1/labels", {})
        return response.get("data", []) or []

    def label_values(self, label: str) -> list[str]:
        """Return every value for a given label."""
        response = self._get(f"/loki/api/v1/label/{label}/values", {})
        return response.get("data", []) or []

    def ready(self) -> bool:
        """Return True if Loki responds to ``/ready``."""
        try:
            self._get("/ready", {}, base_path="")
        except LokiError:
            return False
        return True

    # -- transport ---------------------------------------------------------

    def _raw_get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        base_path: str | None = None,
    ) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(params)
        url = f"{self.url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        headers = {"Accept": "application/json"}
        if self.tenant:
            headers["X-Scope-OrgID"] = self.tenant
        if self.username and self.password:
            import base64

            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise LokiError(
                f"Loki returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LokiError(f"Loki unreachable: {exc.reason}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LokiError(f"Loki returned invalid JSON: {exc}") from exc

    @property
    def circuit_state(self) -> str:
        return self._circuit.snapshot()["state"] if self._circuit else "disabled"

    def reset_circuit(self) -> None:
        if self._circuit:
            self._circuit.reset()

    def _get(self, path: str, params: dict[str, Any], *, base_path: str | None = None) -> dict[str, Any]:
        from .loki_retry import RetryableError
        if self._circuit:
            self._circuit.before_request()
        def request():
            try:
                return self._raw_get(path, params, base_path=base_path)
            except LokiError as exc:
                message = str(exc)
                if any(f"HTTP {code}" in message for code in (408, 425, 429, 500, 502, 503, 504)) or "unreachable" in message:
                    raise RetryableError(message) from exc
                raise
        try:
            result = self._retry_handler.call(request)
        except Exception as exc:
            if self._circuit:
                self._circuit.on_failure()
            raise
        if self._circuit:
            self._circuit.on_success()
        return result

    # -- response parsing --------------------------------------------------

    def _parse_response(self, response: dict[str, Any]) -> Iterator[LogEvent]:
        data = response.get("data", {})
        result_type = data.get("resultType")

        if result_type == "streams":
            yield from self._parse_streams(data.get("result", []))
        elif result_type == "matrix":
            yield from self._parse_matrix(data.get("result", []))
        elif result_type == "vector":
            yield from self._parse_vector(data.get("result", []))

    def _parse_streams(
        self, streams: list[dict[str, Any]]
    ) -> Iterator[LogEvent]:
        for stream in streams:
            labels = stream.get("stream") or {}
            for pair in stream.get("values", []):
                if len(pair) < 2:
                    continue
                yield self._entry_to_event(pair[0], pair[1], labels)

    def _parse_matrix(
        self, series: list[dict[str, Any]]
    ) -> Iterator[LogEvent]:
        for item in series:
            labels = item.get("metric") or {}
            for pair in item.get("values", []):
                if len(pair) < 2:
                    continue
                yield self._entry_to_event(pair[0], str(pair[1]), labels)

    def _parse_vector(
        self, items: list[dict[str, Any]]
    ) -> Iterator[LogEvent]:
        for item in items:
            labels = item.get("metric") or {}
            value = item.get("value") or []
            if len(value) < 2:
                continue
            yield self._entry_to_event(value[0], str(value[1]), labels)

    def _entry_to_event(
        self,
        timestamp_ns: Any,
        message: str,
        labels: dict[str, str],
    ) -> LogEvent:
        try:
            ts = datetime.fromtimestamp(
                int(timestamp_ns) / 1e9, tz=timezone.utc
            )
        except (TypeError, ValueError, OSError):
            ts = datetime.now(timezone.utc)

        level = (
            labels.get("level")
            or labels.get("severity")
            or labels.get("lvl")
            or "INFO"
        )
        service = (
            labels.get("service")
            or labels.get("app")
            or labels.get("job")
            or labels.get("container")
        )

        extra = dict(labels)
        for key in ("level", "severity", "lvl", "service", "app", "job", "container"):
            extra.pop(key, None)

        try:
            return LogEvent.from_raw(
                {
                    "timestamp": ts.isoformat(),
                    "level": level,
                    "message": message,
                    "service": service,
                    "extra": {**extra, "loki_timestamp_ns": str(timestamp_ns)},
                }
            )
        except Exception:
            return LogEvent.from_raw(
                {
                    "timestamp": ts.isoformat(),
                    "level": "INFO",
                    "message": message,
                    "extra": {"raw_labels": labels, "loki_timestamp_ns": str(timestamp_ns)},
                }
            )


def _to_nanos(dt: datetime) -> str:
    """Convert a datetime to the nanosecond string Loki expects."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1e9))

# Compose all adaptive mixin methods into LokiClient for internal helper calls.
from .loki_adaptive import AdaptiveLokiMixin
for _name, _member in vars(AdaptiveLokiMixin).items():
    if callable(_member) and not _name.startswith("__"):
        setattr(LokiClient, _name, _member)
