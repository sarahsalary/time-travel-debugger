"""Live log watching with streaming anomaly and novel-pattern detection.

The watch engine follows a JSONL log file, applies an optional TTDQL
filter, and feeds the surviving events into the existing anomaly detector
and Drain template miner. New anomalies and previously-unseen error
templates are surfaced as alerts, which can be rendered in a TUI or
printed to stdout in silent mode.

This module deliberately avoids external file-watching dependencies: it
uses a small polling tailer that works identically on every platform and
handles file rotation by reopening the file when its inode changes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .anomaly import Anomaly, AnomalyDetector
from .cluster import DrainTemplateMiner, TemplateCluster
from .models import LogEvent
from .query import Query, QuerySyntaxError


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------


class AlertKind(str, Enum := __import__("enum").Enum):
    """The kinds of alerts the watch engine can emit."""

    ANOMALY = "anomaly"
    NOVEL_ERROR = "novel_error"
    ERROR_BURST = "error_burst"


@dataclass
class Alert:
    """A single notification emitted by the watch engine."""

    kind: AlertKind
    timestamp: datetime
    message: str
    severity: str = "info"
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# File tailer
# ---------------------------------------------------------------------------


class FileTailer:
    """Poll a JSONL file and yield new ``LogEvent`` objects as they appear.

    The tailer handles three tricky cases:

    * **Truncation** — if the file shrinks, the read position resets to 0.
    * **Rotation** — if the inode changes, the file is reopened.
    * **Partial writes** — an incomplete trailing line is kept in a buffer
      until the next newline arrives.
    """

    def __init__(self, path: Path, *, poll_interval: float = 0.25) -> None:
        self.path = Path(path)
        self.poll_interval = poll_interval
        self._position = 0
        self._inode: int | None = None
        self._buffer = ""

    def start_from_end(self) -> None:
        """Skip everything that already exists in the file."""
        try:
            self._position = self.path.stat().st_size
            self._inode = self.path.stat().st_ino
        except FileNotFoundError:
            self._position = 0
            self._inode = None

    def read_new(self) -> list[LogEvent]:
        """Return any new events appended since the last call."""
        if not self.path.exists():
            return []

        stat = self.path.stat()

        # Detect rotation (different inode) and reset.
        if self._inode is not None and stat.st_ino != self._inode:
            self._position = 0
            self._buffer = ""

        # Detect truncation.
        if stat.st_size < self._position:
            self._position = 0
            self._buffer = ""

        self._inode = stat.st_ino

        if stat.st_size == self._position:
            return []

        events: list[LogEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._position)
            chunk = handle.read()
            self._position = handle.tell()

        data = self._buffer + chunk
        lines = data.split("\n")
        # The last element is either empty (if the chunk ended with a newline)
        # or an incomplete line that we keep in the buffer.
        self._buffer = lines.pop() if lines else ""

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            try:
                events.append(LogEvent.from_raw(raw))
            except Exception:
                continue

        return events


# ---------------------------------------------------------------------------
# Watch engine
# ---------------------------------------------------------------------------


class WatchEngine:
    """Continuously tail a log file and emit alerts for interesting events.

    The engine maintains a bounded ring buffer of recent events, feeds them
    into the anomaly detector and the template miner, and emits ``Alert``
    objects whenever:

    * a statistical anomaly crosses the sensitivity threshold,
    * a new error template appears that the miner has never seen before,
    * the error rate in a window exceeds the configured burst threshold.
    """

    def __init__(
        self,
        path: Path,
        *,
        query: str = "",
        window_seconds: int = 60,
        sensitivity: float = 2.5,
        error_burst_threshold: int = 10,
        poll_interval: float = 0.25,
        buffer_size: int = 10_000,
    ) -> None:
        self.path = Path(path)
        self.query_text = query
        self._query: Query | None = None
        if query:
            try:
                self._query = Query(query)
            except QuerySyntaxError as exc:
                raise ValueError(f"Invalid TTDQL expression: {exc}") from exc

        self.window_seconds = window_seconds
        self.sensitivity = sensitivity
        self.error_burst_threshold = error_burst_threshold
        self.poll_interval = poll_interval

        self.tailer = FileTailer(self.path, poll_interval=poll_interval)
        self.detector = AnomalyDetector(
            window_seconds=window_seconds,
            sensitivity=sensitivity,
        )
        self.miner = DrainTemplateMiner()
        self._known_templates: set[str] = set()

        self._buffer: deque[LogEvent] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Alert], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._total_seen = 0
        self._total_alerts = 0
        self._last_analysis = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, from_end: bool = True) -> None:
        """Start tailing in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        if from_end:
            self.tailer.start_from_end()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ttd-watch-engine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the engine to stop and join the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def total_seen(self) -> int:
        return self._total_seen

    @property
    def total_alerts(self) -> int:
        return self._total_alerts

    def snapshot(self) -> list[LogEvent]:
        with self._lock:
            return list(self._buffer)

    # -- listeners ---------------------------------------------------------

    def subscribe(self, callback: Callable[[Alert], None]) -> None:
        """Register a callback invoked for every new alert."""
        self._listeners.append(callback)

    def _emit(self, alert: Alert) -> None:
        self._total_alerts += 1
        for callback in list(self._listeners):
            try:
                callback(alert)
            except Exception:
                pass

    # -- main loop ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                new_events = self.tailer.read_new()
            except Exception:
                new_events = []

            if new_events:
                self._process(new_events)

            # Periodically re-run the anomaly detector over the buffer.
            now = time.monotonic()
            if now - self._last_analysis >= self.window_seconds:
                self._analyze_buffer()
                self._last_analysis = now

            self._stop.wait(self.poll_interval)

    def _process(self, events: list[LogEvent]) -> None:
        accepted: list[LogEvent] = []
        for event in events:
            if self._query is not None and not self._query.matches(event):
                continue
            accepted.append(event)

        if not accepted:
            return

        with self._lock:
            self._buffer.extend(accepted)
        self._total_seen += len(accepted)

        # Novel error templates: feed errors through the miner and flag
        # templates we have never seen before.
        error_events = [
            e for e in accepted
            if e.level.upper() in {"ERROR", "CRITICAL", "FATAL"}
        ]
        for event in error_events:
            cluster = self.miner.add_event(event)
            if cluster.template not in self._known_templates:
                self._known_templates.add(cluster.template)
                self._emit(
                    Alert(
                        kind=AlertKind.NOVEL_ERROR,
                        timestamp=event.timestamp,
                        severity="high",
                        message=f"Novel error template: {cluster.template}",
                        payload={
                            "template": cluster.template,
                            "sample": event.message,
                            "service": event.service,
                            "cluster_id": cluster.cluster_id,
                        },
                    )
                )

        # Error burst: too many errors in a short window.
        if len(error_events) >= self.error_burst_threshold:
            self._emit(
                Alert(
                    kind=AlertKind.ERROR_BURST,
                    timestamp=error_events[-1].timestamp,
                    severity="high",
                    message=(
                        f"Error burst: {len(error_events)} errors arrived "
                        f"in the last poll cycle"
                    ),
                    payload={"count": len(error_events)},
                )
            )

    def _analyze_buffer(self) -> None:
        with self._lock:
            events = list(self._buffer)
        if len(events) < 10:
            return

        # Only analyze the tail of the buffer to keep the work bounded.
        recent = events[-min(len(events), 5000):]
        report = self.detector.analyze(recent)
        for anomaly in report.top(5):
            if abs(anomaly.z_score) < self.sensitivity:
                continue
            self._emit(
                Alert(
                    kind=AlertKind.ANOMALY,
                    timestamp=anomaly.window.start,
                    severity=anomaly.severity.value,
                    message=anomaly.describe(),
                    payload={
                        "metric": anomaly.metric.value,
                        "value": anomaly.value,
                        "z_score": anomaly.z_score,
                        "window_start": anomaly.window.start.isoformat(),
                    },
                )
            )


# ---------------------------------------------------------------------------
# Plain stdout runner
# ---------------------------------------------------------------------------


def run_watch_stdout(
    path: Path,
    *,
    query: str = "",
    window_seconds: int = 60,
    sensitivity: float = 2.5,
    silent: bool = False,
) -> None:
    """Run the watch engine and print events and alerts to stdout.

    When ``silent`` is True only alerts are printed; the raw event stream
    is suppressed. This is useful for running the watcher as a daemon and
    forwarding alerts to another system.
    """
    from rich.console import Console
    from rich.text import Text

    from .render import format_event

    console = Console()
    engine = WatchEngine(
        path,
        query=query,
        window_seconds=window_seconds,
        sensitivity=sensitivity,
    )

    def on_alert(alert: Alert) -> None:
        style = {
            "low": "yellow",
            "medium": "bold yellow",
            "high": "bold red",
        }.get(alert.severity, "white")
        header = Text()
        header.append(alert.timestamp.strftime("%H:%M:%S"), style="dim")
        header.append("  ")
        header.append(f"ALERT {alert.kind.value.upper()}", style=style)
        header.append("  ")
        header.append(alert.message)
        console.print(header)

    engine.subscribe(on_alert)

    # Stream the raw events too unless we are in silent mode.
    if not silent:
        def stream_events() -> None:
            seen = 0
            while engine.is_running:
                snapshot = engine.snapshot()
                if len(snapshot) > seen:
                    for event in snapshot[seen:]:
                        console.print(format_event(event))
                    seen = len(snapshot)
                time.sleep(0.2)

        thread = threading.Thread(target=stream_events, daemon=True)
        thread.start()

    engine.start(from_end=True)
    console.print(
        f"[cyan]watching[/] {path}  [dim](Ctrl+C to stop)[/]"
    )
    if query:
        console.print(f"[cyan]filter[/] {query}")
    try:
        while engine.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    console.print(
        f"[dim]stopped — {engine.total_seen} events, "
        f"{engine.total_alerts} alerts[/]"
    )