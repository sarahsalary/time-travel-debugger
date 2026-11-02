"""Rich-powered terminal rendering for every command."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .differ import DiffResult
from .graph import SpanNode, TraceTree
from .models import LogEvent
from .state import AppState

console = Console()

LEVEL_STYLES: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "WARN": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
    "FATAL": "bold white on red",
}

HEALTH_STYLES: dict[str, str] = {
    "OK": "green",
    "WARNING": "yellow",
    "ERROR": "red",
}


def _level_style(level: str) -> str:
    return LEVEL_STYLES.get(level.upper(), "white")


def format_event(event: LogEvent, *, show_service: bool = True) -> Text:
    """Render a single event as a ``rich.Text`` instance."""
    text = Text()
    text.append(event.timestamp.strftime("%H:%M:%S.%f")[:-3], style="dim")
    text.append("  ")
    text.append(f"{event.level:<8}", style=_level_style(event.level))
    if show_service and event.service:
        text.append(f"{event.service:<12}", style="magenta")
    text.append(event.short_message)
    return text


def render_events(
    events: list[LogEvent],
    *,
    title: str = "Timeline",
    max_events: int = 200,
) -> None:
    """Print a timeline of events."""
    if not events:
        console.print("[yellow]No events to display.[/]")
        return

    shown = events[-max_events:]
    tree = Tree(f"[bold]{title}[/] [dim]({len(events)} events)[/]")

    for event in shown:
        node = tree.add(format_event(event))
        if event.extra:
            for key, value in event.extra.items():
                node.add(Text(f"{key} = {value}", style="dim"))

    if len(events) > max_events:
        console.print(
            f"[dim]... showing last {max_events} of {len(events)} events[/]"
        )
    console.print(tree)


def render_state(state: AppState) -> None:
    """Render an ``AppState`` snapshot as a panel."""
    header = Text()
    header.append("Health: ", style="bold")
    header.append(state.health, style=HEALTH_STYLES.get(state.health, "white"))

    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold cyan", no_wrap=True)
    body.add_column()

    body.add_row("Moment", state.moment.isoformat())
    body.add_row("Events", str(state.event_count))
    body.add_row("Services", ", ".join(state.services) or "-")
    body.add_row("Errors", str(state.error_count))
    body.add_row("Warnings", str(state.warning_count))

    if state.last_event is not None:
        body.add_row("Last event", state.last_event.short_message)

    if state.fields:
        field_lines = "\n".join(f"{k} = {v}" for k, v in state.fields.items())
        body.add_row("Fields", field_lines)

    console.print(Panel(Group(header, "", body), title="App State", border_style="cyan"))


def render_dependencies(edges: list[tuple[str, str]]) -> None:
    """Render a dependency table between services."""
    if not edges:
        console.print("[yellow]No service dependencies detected.[/]")
        return

    table = Table(title="Service Dependencies", show_header=True, header_style="bold")
    table.add_column("Caller", style="cyan")
    table.add_column("")
    table.add_column("Callee", style="magenta")
    for caller, callee in edges:
        table.add_row(caller, "->", callee)
    console.print(table)


def render_tree(tree: TraceTree) -> None:
    """Render a single execution tree."""
    console.print(f"[bold]trace_id[/]: {tree.trace_id} [dim]({tree.total_spans} spans)[/]")

    root = Tree("[bold]root[/]")
    for node in tree.roots:
        _add_span(root, node)
    console.print(root)


def _add_span(parent: Tree, node: SpanNode) -> None:
    branch = parent.add(format_event(node.event, show_service=True))
    for child in node.children:
        _add_span(branch, child)


def render_diff(result: DiffResult) -> None:
    """Render the result of a state diff."""
    console.print(
        Panel(
            f"[bold]From[/] {result.before_moment.isoformat()}\n"
            f"[bold]To  [/] {result.after_moment.isoformat()}",
            title="Time Diff",
            border_style="cyan",
        )
    )

    if not result.has_changes:
        console.print("[green]No changes detected.[/]")
        return

    if result.added_services:
        console.print(f"[green]+ services:[/] {', '.join(result.added_services)}")
    if result.removed_services:
        console.print(f"[red]- services:[/] {', '.join(result.removed_services)}")

    if result.field_changes:
        table = Table(title="Field Changes", show_header=True, header_style="bold")
        table.add_column("Key", style="cyan")
        table.add_column("Before", style="yellow")
        table.add_column("After", style="green")
        for change in result.field_changes:
            table.add_row(change.key, repr(change.before), repr(change.after))
        console.print(table)

    if result.new_errors:
        console.print(f"[bold red]New errors ({len(result.new_errors)}):[/]")
        render_events(result.new_errors, title="New Errors", max_events=50)


def render_error(message: str) -> None:
    """Render a user-facing error."""
    console.print(f"[bold red]error:[/] {message}")


def render_info(message: str) -> None:
    """Render an informational message."""
    console.print(f"[cyan]info:[/] {message}")