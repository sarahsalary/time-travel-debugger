"""TUI screen that displays events arriving over OTLP in real time."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from ..models import LogEvent
from ..otlp_receiver import LiveStore
from ..state import reconstruct_state


class LiveScreen(App):
    """A standalone TUI for watching OTLP events as they arrive."""

    CSS = """
    Screen { layout: vertical; }

    #live-header {
        height: 3;
        padding: 1 1 0 1;
        background: $boost;
    }

    #live-table {
        height: 1fr;
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "clear", "Clear"),
    ]

    def __init__(self, store: LiveStore, *, host: str, port: int) -> None:
        super().__init__()
        self.store = store
        self.host = host
        self.port = port
        self._buffer: list[LogEvent] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self._header_text(), id="live-header")
        yield DataTable(id="live-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#live-table", DataTable)
        table.add_column("Time", width=12)
        table.add_column("Level", width=8)
        table.add_column("Service", width=14)
        table.add_column("Trace", width=10)
        table.add_column("Message", ratio=1)

        self.store.subscribe(self._on_new_events)
        # Seed with whatever is already in the store.
        self._buffer.extend(self.store.snapshot()[-500:])
        self._render()
        self.set_interval(1.0, self._refresh_header)

    def _on_new_events(self, events: list[LogEvent]) -> None:
        """Called from the receiver thread; schedule on the UI thread."""
        self.call_from_thread(self._append, events)

    def _append(self, events: list[LogEvent]) -> None:
        self._buffer.extend(events)
        # Keep the buffer bounded.
        if len(self._buffer) > 2000:
            self._buffer = self._buffer[-2000:]
        self._render()

    def _render(self) -> None:
        table = self.query_one("#live-table", DataTable)
        table.clear()
        for event in self._buffer[-500:]:
            table.add_row(
                event.timestamp.strftime("%H:%M:%S.%f")[:-3],
                event.level,
                event.service or "-",
                (event.trace_id or "-")[:8],
                event.short_message,
            )

    def _refresh_header(self) -> None:
        self.query_one("#live-header", Static).update(self._header_text())

    def _header_text(self) -> str:
        return (
            f"[bold]OTLP Live[/]  "
            f"[dim]listening on {self.host}:{self.port}[/]  "
            f"[cyan]received {self.store.total_received} events[/]"
        )

    def action_clear(self) -> None:
        self.store.clear()
        self._buffer.clear()
        self._render()