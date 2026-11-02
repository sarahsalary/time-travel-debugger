"""HTTP receiver for OTLP/HTTP data.

This module implements a minimal, dependency-free OTLP/HTTP receiver on top
of ``http.server``. It accepts protobuf or JSON payloads on the standard
OTLP endpoints (``/v1/logs`` and ``/v1/traces``) and pushes decoded
``LogEvent`` objects into a thread-safe live store.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .models import LogEvent
from .otlp import decode_logs_payload, decode_traces_payload


OTLP_LOGS_PATH = "/v1/logs"
OTLP_TRACES_PATH = "/v1/traces"


# ---------------------------------------------------------------------------
# Live store
# ---------------------------------------------------------------------------


class LiveStore:
    """A bounded, thread-safe buffer of recently received events."""

    def __init__(self, *, max_events: int = 100_000) -> None:
        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[list[LogEvent]], None]] = []
        self._total_received = 0

    @property
    def total_received(self) -> int:
        with self._lock:
            return self._total_received

    def append(self, events: list[LogEvent]) -> None:
        if not events:
            return
        with self._lock:
            self._events.extend(events)
            self._total_received += len(events)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(events)
            except Exception:
                pass

    def snapshot(self) -> list[LogEvent]:
        with self._lock:
            return list(self._events)

    def subscribe(self, callback: Callable[[list[LogEvent]], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _OTLPRequestHandler(BaseHTTPRequestHandler):
    """Handle OTLP/HTTP export requests."""

    store: LiveStore  # injected by the server
    on_payload: Callable[[str, dict[str, Any]], None] | None = None

    protocol_version = "HTTP/1.1"

    # Silence the default request logging.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._respond(200, b"ok")
            return
        self._respond(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            self._respond(400, b"invalid content-length")
            return

        body = self.rfile.read(length) if length else b""

        try:
            payload = self._decode_body(body)
        except Exception as exc:  # noqa: BLE001
            self._respond(400, f"decode error: {exc}".encode())
            return

        if self.path == OTLP_LOGS_PATH:
            events = decode_logs_payload(payload)
        elif self.path == OTLP_TRACES_PATH:
            events = decode_traces_payload(payload)
        else:
            self._respond(404, b"not found")
            return

        self.store.append(events)
        if self.on_payload is not None:
            try:
                self.on_payload(self.path, payload)
            except Exception:
                pass

        # OTLP requires an empty ExportResponse; JSON {} is acceptable.
        self._respond(200, b"{}")

    # -- helpers -----------------------------------------------------------

    def _decode_body(self, body: bytes) -> dict[str, Any]:
        """Decode the request body, accepting JSON or protobuf."""
        content_type = self.headers.get("Content-Type", "")

        if not body:
            return {}

        if "json" in content_type:
            return json.loads(body.decode("utf-8"))

        # Protobuf requires an external dependency. If it is not available,
        # fall back to trying to parse the body as JSON, which is what
        # ``curl`` and many SDKs send when the content type is not set.
        try:
            from opentelemetry.proto.collector.logs.v1 import (  # type: ignore
                logs_service_pb2,
            )
            from opentelemetry.proto.collector.trace.v1 import (  # type: ignore
                trace_service_pb2,
            )
        except ImportError:
            return json.loads(body.decode("utf-8"))

        if self.path == OTLP_LOGS_PATH:
            message = logs_service_pb2.ExportLogsServiceRequest()
            message.ParseFromString(body)
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(message, preserving_proto_field_name=False)

        if self.path == OTLP_TRACES_PATH:
            message = trace_service_pb2.ExportTraceServiceRequest()
            message.ParseFromString(body)
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(message, preserving_proto_field_name=False)

        return {}

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class OTLPReceiver:
    """A threaded OTLP/HTTP receiver bound to a configurable port."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4318,
        store: LiveStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.store = store or LiveStore()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, daemon: bool = True) -> None:
        """Start the receiver in a background thread."""
        if self.is_running:
            return

        handler = type(
            "_BoundHandler",
            (_OTLPRequestHandler,),
            {"store": self.store},
        )

        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ttd-otlp-receiver",
            daemon=daemon,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the receiver and join the background thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    def __enter__(self) -> "OTLPReceiver":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()