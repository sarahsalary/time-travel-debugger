"""Modal screens for the ttd TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, Tree

from ..graph import SpanNode, TraceTree


class HelpScreen(Screen):
    """A modal screen listing key bindings and query syntax."""

    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    HELP_TEXT = """\
[bold cyan]ttd replay — keyboard shortcuts[/]

  [bold]q[/]         Quit
  [bold]/[/]         Focus the query bar
  [bold]Escape[/]    Return focus to the event table
  [bold]Ctrl+L[/]    Clear the query
  [bold]t[/]         Show the trace tree of the selected event
  [bold]d[/]         Show service dependencies
  [bold]?[/]         Show this help
  [bold]j / k[/]     Move down / up in the event list
  [bold]Home/End[/]  Jump to first / last event

[bold cyan]TTDQL — query language[/]

  level=ERROR                     Equality (level matching is case-insensitive)
  duration_ms > 500               Numeric comparison
  message ~ 'time.*out'           Regex match
  service IN (api, db)            Membership
  level NOT IN (DEBUG, INFO)      Negated membership
  message LIKE 'timeout%'         SQL-style pattern
  message ILIKE '%error%'         Case-insensitive pattern
  extra.user_id = 42              Access fields from the extra payload

  Combine with [bold]AND[/], [bold]OR[/], [bold]NOT[/], and parentheses:

    level=ERROR AND (service=api OR service=db) AND duration_ms > 100
"""

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(self.HELP_TEXT, id="help-content")
        yield Footer()


class TraceScreen(Screen):
    """A modal screen showing a single execution tree."""

    BINDINGS = [Binding("escape,q", "app.pop_screen", "Close")]

    def __init__(self, tree: TraceTree) -> None:
        super().__init__()
        self.tree_data = tree

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree(f"trace_id: {self.tree_data.trace_id}", id="trace-tree")
        yield Footer()

    def on_mount(self) -> None:
        widget = self.query_one("#trace-tree", Tree)
        for root in self.tree_data.roots:
            self._add_node(widget.root, root)
        widget.root.expand_all()

    def _add_node(self, parent, node: SpanNode) -> None:
        event = node.event
        service = event.service or "-"
        label = f"[{service}] {event.short_message}"
        branch = parent.add(label)
        for child in node.children:
            self._add_node(branch, child)


class DepsScreen(Screen):
    """A modal screen showing service-to-service dependencies."""

    BINDINGS = [Binding("escape,q", "app.pop_screen", "Close")]

    def __init__(self, edges: list[tuple[str, str]]) -> None:
        super().__init__()
        self.edges = edges

    def compose(self) -> ComposeResult:
        yield Header()
        if self.edges:
            yield DataTable(id="deps-table", cursor_type="row")
        else:
            yield Static("No service dependencies detected.", id="deps-empty")
        yield Footer()

    def on_mount(self) -> None:
        if not self.edges:
            return
        table = self.query_one("#deps-table", DataTable)
        table.add_column("Caller", key="caller")
        table.add_column("Callee", key="callee")
        for caller, callee in self.edges:
            table.add_row(caller, callee)