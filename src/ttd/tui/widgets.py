"""Custom widgets used by the ttd TUI."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..models import LogEvent
from ..state import AppState

LEVEL_STYLES: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "WARN": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
    "FATAL": "bold white on red",
}


def level_style(level: str) -> str:
    """Return a Rich style string for the given log level."""
    return LEVEL_STYLES.get(level.upper(), "white")


class DetailPanel(Static):
    """Shows the details of the currently selected event."""

    def show_event(self, event: LogEvent | None) -> None:
        if event is None:
            self.update(Text("No event selected.", style="dim"))
            return

        text = Text()
        text.append("Timestamp  ", style="bold cyan")
        text.append(event.timestamp.isoformat() + "\n")

        text.append("Level      ", style="bold cyan")
        text.append(event.level, style=level_style(event.level))
        text.append("\n")

        text.append("Service    ", style="bold cyan")
        text.append((event.service or "-") + "\n")

        text.append("Message    ", style="bold cyan")
        text.append(event.message + "\n")

        if event.trace_id:
            text.append("Trace ID   ", style="bold cyan")
            text.append(event.trace_id + "\n")
        if event.span_id:
            text.append("Span ID    ", style="bold cyan")
            text.append(event.span_id + "\n")
        if event.parent_span_id:
            text.append("Parent     ", style="bold cyan")
            text.append(event.parent_span_id + "\n")
        if event.duration_ms is not None:
            text.append("Duration   ", style="bold cyan")
            text.append(f"{event.duration_ms} ms\n")

        if event.extra:
            text.append("\nExtra:\n", style="bold cyan")
            for key, value in event.extra.items():
                text.append(f"  {key} = ", style="cyan")
                text.append(f"{value}\n")

        self.update(text)


class StatePanel(Static):
    """Shows the reconstructed application state at the current moment."""

    def update_state(
        self,
        state: AppState,
        total: int,
        *,
        matched: int,
        error: str | None = None,
    ) -> None:
        health_style = {
            "OK": "green",
            "WARNING": "yellow",
            "ERROR": "bold red",
        }.get(state.health, "white")

        text = Text()
        text.append("Health ", style="bold")
        text.append(state.health, style=health_style)
        text.append("   ")

        text.append("Events ", style="bold")
        text.append(f"{matched}/{total}")
        text.append("   ")

        text.append("Services ", style="bold")
        text.append(", ".join(state.services) or "-")
        text.append("   ")

        text.append("Errors ", style="bold")
        text.append(
            str(state.error_count),
            style="red" if state.error_count else "dim",
        )
        text.append("   ")

        text.append("Warnings ", style="bold")
        text.append(
            str(state.warning_count),
            style="yellow" if state.warning_count else "dim",
        )

        if error:
            text.append("   ")
            text.append("query error: ", style="bold red")
            text.append(error, style="red")

        self.update(text)