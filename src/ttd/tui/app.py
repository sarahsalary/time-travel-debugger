"""Interactive TUI for time-travel debugging."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static

from ..graph import build_trace_tree, detect_dependencies
from ..indexer import TimeIndex
from ..models import LogEvent
from ..query import Query, QuerySyntaxError
from ..state import reconstruct_state
from .screens import DepsScreen, HelpScreen, TraceScreen
from .widgets import DetailPanel, StatePanel, level_style


class ReplayApp(App):
    """Interactive time-travel replay of a JSONL log file."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #query-bar {
        height: 3;
        dock: top;
        background: $boost;
        padding: 0 1;
    }

    #query-prefix {
        width: 3;
        content-align: right middle;
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
    }

    #query-input {
        width: 1fr;
    }

    #main {
        height: 1fr;
    }

    #event-table {
        width: 2fr;
        border: solid $accent;
    }

    #detail-panel {
        width: 1fr;
        border: solid $success;
        padding: 0 1;
        overflow-y: auto;
    }

    #state-panel {
        height: 3;
        dock: bottom;
        background: $panel;
        padding: 1 1 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_query", "Query"),
        Binding("escape", "blur_query", "Blur"),
        Binding("ctrl+l", "clear_query", "Clear"),
        Binding("t", "show_tree", "Trace tree"),
        Binding("d", "show_deps", "Dependencies"),
        Binding("question_mark", "show_help", "Help"),
        Binding("home", "go_first", "First", show=False),
        Binding("end", "go_last", "Last", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(
        self,
        index: TimeIndex,
        *,
        initial_query: str = "",
        initial_index: int | None = None,
        log_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.all_events: list[LogEvent] = list(index.events)
        self.filtered_events: list[LogEvent] = list(self.all_events)
        self._initial_query = initial_query
        self.log_file = log_file
        self._initial_index = initial_index

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="query-bar"):
            yield Static("/", id="query-prefix")
            yield Input(
                placeholder="TTDQL query — e.g. level=ERROR AND service=api",
                id="query-input",
            )
        with Horizontal(id="main"):
            yield DataTable(id="event-table", cursor_type="row", zebra_stripes=True)
            yield DetailPanel(id="detail-panel")
        yield StatePanel(id="state-panel")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#event-table", DataTable)
        table.add_column("Time", width=12, key="time")
        table.add_column("Level", width=8, key="level")
        table.add_column("Service", width=14, key="service")
        table.add_column("Message", key="message")

        if self._initial_query:
            input_widget = self.query_one("#query-input", Input)
            input_widget.value = self._initial_query

        self._apply_query(self._initial_query)

        if self._initial_index is not None and self.filtered_events:
            row = min(self._initial_index, len(self.filtered_events) - 1)
            table.move_cursor(row=row)

        self._update_detail()
        table.focus()

    def on_unmount(self) -> None:
        """Persist the latest query and event cursor when the UI closes."""
        if self.log_file is None:
            return
        from ..session import SessionManager, SessionState
        try:
            query_text = self.query_one("#query-input", Input).value
            cursor_row = self.query_one("#event-table", DataTable).cursor_row
            SessionManager().save(SessionState(
                log_file=str(self.log_file.resolve()),
                query=query_text,
                cursor_row=max(0, cursor_row),
            ))
        except Exception:
            pass

    # -- query handling ----------------------------------------------------

    def _apply_query(self, query_text: str) -> None:
        error: str | None = None
        if query_text.strip():
            try:
                query_obj = Query(query_text)
                self.filtered_events = query_obj.filter(self.all_events)
            except QuerySyntaxError as exc:
                error = str(exc)
                self.filtered_events = list(self.all_events)
        else:
            self.filtered_events = list(self.all_events)

        self._populate_table()
        self._update_detail()
        self._update_state_panel(error)

    def _populate_table(self) -> None:
        table = self.query_one("#event-table", DataTable)
        table.clear()
        for event in self.filtered_events:
            table.add_row(
                event.timestamp.strftime("%H:%M:%S.%f")[:-3],
                event.level,
                event.service or "-",
                event.short_message,
                key=str(id(event)),
            )

    def _update_state_panel(self, error: str | None) -> None:
        panel = self.query_one("#state-panel", StatePanel)

        if self.filtered_events:
            moment = self.filtered_events[-1].timestamp
        elif self.all_events:
            moment = self.all_events[-1].timestamp
        else:
            moment = datetime.now(timezone.utc)

        state = reconstruct_state(self.index, moment)
        panel.update_state(
            state,
            total=len(self.all_events),
            matched=len(self.filtered_events),
            error=error,
        )

    def _update_detail(self) -> None:
        event = self._current_event()
        detail = self.query_one("#detail-panel", DetailPanel)
        detail.show_event(event)

        if event is not None:
            state = reconstruct_state(self.index, event.timestamp)
            panel = self.query_one("#state-panel", StatePanel)
            panel.update_state(
                state,
                total=len(self.all_events),
                matched=len(self.filtered_events),
                error=None,
            )

    def _current_event(self) -> LogEvent | None:
        table = self.query_one("#event-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self.filtered_events):
            return self.filtered_events[row]
        return None

    # -- input events ------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "query-input":
            self._apply_query(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#event-table", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_detail()

    # -- actions -----------------------------------------------------------

    def action_focus_query(self) -> None:
        self.query_one("#query-input", Input).focus()

    def action_blur_query(self) -> None:
        self.query_one("#event-table", DataTable).focus()

    def action_clear_query(self) -> None:
        widget = self.query_one("#query-input", Input)
        widget.value = ""
        widget.focus()

    def action_show_tree(self) -> None:
        event = self._current_event()
        if event is None or not event.trace_id:
            self.notify("Selected event has no trace_id", severity="warning")
            return

        forest = build_trace_tree(self.all_events)
        tree_obj = forest.get(event.trace_id)
        if tree_obj is None:
            self.notify(f"Trace {event.trace_id!r} not found", severity="warning")
            return

        self.push_screen(TraceScreen(tree_obj))

    def action_show_deps(self) -> None:
        edges = detect_dependencies(self.all_events)
        self.push_screen(DepsScreen(edges))

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_go_first(self) -> None:
        self.query_one("#event-table", DataTable).move_cursor(row=0)

    def action_go_last(self) -> None:
        table = self.query_one("#event-table", DataTable)
        if self.filtered_events:
            table.move_cursor(row=len(self.filtered_events) - 1)

    def action_cursor_up(self) -> None:
        self.query_one("#event-table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#event-table", DataTable).action_cursor_down()


def run_replay(
    logfile: Path,
    *,
    initial_query: str = "",
    at: str | None = None,
    restore: bool = True,
) -> None:
    """Entry point for ``ttd replay`` with optional session restoration."""
    index = TimeIndex.from_file(logfile)
    if not index:
        print(f"[ttd] {logfile} contains no valid events.")
        return

    initial_index: int | None = None
    if restore:
        from ..session import SessionManager
        sessions = SessionManager()
        if sessions.exists(logfile):
            previous = sessions.load(logfile)
            if not initial_query:
                initial_query = previous.query
            if not at:
                initial_index = previous.cursor_row
    if at:
        from dateutil import parser as dateparser

        moment = dateparser.parse(at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        before = index.at(moment)
        initial_index = max(0, len(before) - 1)

    app = ReplayApp(index, initial_query=initial_query, initial_index=initial_index, log_file=logfile)
    app.run()