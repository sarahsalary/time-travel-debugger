"""Textual dashboard for live log watching."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from ..models import LogEvent
from ..render import format_event
from ..watch import Alert, AlertKind, WatchEngine


class WatchScreen(App):
    """A TUI dashboard that streams events and alerts in real time."""

    CSS = """
    Screen { layout: vertical; }

    #watch-query {
        height: 3;
        dock: top;
        background: $boost;
        padding: 0 1;
    }

    #watch-main {
        height: 1fr;
    }

    #watch-events {
        width: 2fr;
        border: solid $accent;
    }

    #watch-alerts {
        width: 1fr;
        border: solid $warning;
    }

    #watch-status {
        height: 3;
        dock: bottom;
        background: $panel;
        padding: 1 1 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "clear", "Clear"),
        Binding("slash", "focus_query", "Filter"),
    ]

    def __init__(
        self,
        path: Path,
        *,
        query: str = "",
        window_seconds: int = 60,
        sensitivity: float = 2.5,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.engine = WatchEngine(
            path,
            query=query,
            window_seconds=window_seconds,
            sensitivity=sensitivity,
        )
        self._events_seen = 0
        self._alerts: list[Alert] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="watch-query"):
            yield Static("/", id="watch-query-prefix")
            yield Input(
                placeholder="Live TTDQL filter — press Enter to apply",
                id="watch-query-input",
                value=self.engine.query_text,
            )
        with Horizontal(id="watch-main"):
            yield DataTable(id="watch-events", cursor_type="row")
            yield RichLog(id="watch-alerts", highlight=True, markup=True)
        yield Static("", id="watch-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#watch-events", DataTable)
        table.add_column("Time", width=12)
        table.add_column("Level", width=8)
        table.add_column("Service", width=14)
        table.add_column("Message", ratio=1)

        self.engine.subscribe(self._on_alert)
        self.engine.start(from_end=True)
        self.set_interval(0.5, self._refresh)
        self.set_interval(1.0, self._refresh_status)

    # -- callbacks ---------------------------------------------------------

    def _on_alert(self, alert: Alert) -> None:
        self.call_from_thread(self._append_alert, alert)

    def _append_alert(self, alert: Alert) -> None:
        self._alerts.append(alert)
        log = self.query_one("#watch-alerts", RichLog)
        style = {
            "low": "yellow",
            "medium": "bold yellow",
            "high": "bold red",
        }.get(alert.severity, "white")
        log.write(
            f"[dim]{alert.timestamp.strftime('%H:%M:%S')}[/] "
            f"[{style}]{alert.kind.value.upper()}[/] {alert.message}"
        )

    def _refresh(self) -> None:
        snapshot = self.engine.snapshot()
        if len(snapshot) <= self._events_seen:
            return
        table = self.query_one("#watch-events", DataTable)
        for event in snapshot[self._events_seen:]:
            table.add_row(
                event.timestamp.strftime("%H:%M:%S.%f")[:-3],
                event.level,
                event.service or "-",
                event.short_message,
            )
        self._events_seen = len(snapshot)

    def _refresh_status(self) -> None:
        status = self.query_one("#watch-status", Static)
        status.update(
            f"[bold]watching[/] {self.path.name}   "
            f"[cyan]events {self.engine.total_seen}[/]   "
            f"[yellow]alerts {self.engine.total_alerts}[/]   "
            f"[dim]q to quit[/]"
        )

    # -- input handling ----------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "watch-query-input":
            return
        new_query = event.value.strip()
        try:
            # Rebuild the engine with the new filter, preserving the buffer.
            self.engine.stop()
            self.engine = WatchEngine(
                self.path,
                query=new_query,
                window_seconds=self.engine.window_seconds,
                sensitivity=self.engine.sensitivity,
            )
            self.engine.subscribe(self._on_alert)
            self.engine.start(from_end=True)
            self.notify(f"Filter applied: {new_query or '(none)'}")
        except ValueError as exc:
            self.notify(f"Invalid filter: {exc}", severity="error")

    def action_focus_query(self) -> None:
        self.query_one("#watch-query-input", Input).focus()

    def action_clear(self) -> None:
        self.query_one("#watch-events", DataTable).clear()
        self.query_one("#watch-alerts", RichLog).clear()
        self._events_seen = 0
        self._alerts.clear()

    def on_unmount(self) -> None:
        self.engine.stop()