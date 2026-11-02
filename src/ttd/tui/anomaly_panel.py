"""Textual widget that visualizes anomalies inline in the replay TUI."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Label, Static

from ..anomaly import Anomaly, AnomalyReport, Severity


SEVERITY_STYLES: dict[Severity, str] = {
    Severity.LOW: "yellow",
    Severity.MEDIUM: "bold yellow",
    Severity.HIGH: "bold red",
}


class AnomalyPanel(Static):
    """A scrollable list of detected anomalies.

    Clicking or pressing Enter on an anomaly posts an
    ``AnomalyPanel.AnomalySelected`` message that the parent app can handle
    by jumping the event table to the corresponding time window.
    """

    class AnomalySelected(Message):
        """Posted when the user activates an anomaly row."""

        def __init__(self, anomaly: Anomaly) -> None:
            self.anomaly = anomaly
            super().__init__()

    DEFAULT_CSS = """
    AnomalyPanel {
        height: 12;
        border: solid $warning;
        padding: 0 1;
    }

    AnomalyPanel > Vertical {
        height: 100%;
    }

    AnomalyPanel DataTable {
        height: 1fr;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._report: AnomalyReport | None = None
        self._anomalies: list[Anomaly] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Anomalies[/]", id="anomaly-title")
            yield DataTable(id="anomaly-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#anomaly-table", DataTable)
        table.add_column("When", width=16, key="when")
        table.add_column("Metric", width=14, key="metric")
        table.add_column("Z", width=7, key="z")
        table.add_column("Severity", width=9, key="severity")

    def update_report(self, report: AnomalyReport | None) -> None:
        """Replace the panel contents with a new report."""
        self._report = report
        self._anomalies = list(report.anomalies) if report else []

        title = self.query_one("#anomaly-title", Label)
        if not self._anomalies:
            title.update("[bold]Anomalies[/] [dim]— none detected[/]")
        else:
            title.update(f"[bold]Anomalies[/] ({len(self._anomalies)})")

        table = self.query_one("#anomaly-table", DataTable)
        table.clear()
        for anomaly in self._anomalies[:50]:
            table.add_row(
                anomaly.window.start.strftime("%m-%d %H:%M:%S"),
                anomaly.metric.value,
                f"{anomaly.z_score:+.2f}",
                Text(anomaly.severity.value, style=SEVERITY_STYLES[anomaly.severity]),
                key=str(id(anomaly)),
            )

    def selected_anomaly(self) -> Anomaly | None:
        table = self.query_one("#anomaly-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._anomalies):
            return self._anomalies[row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        anomaly = self.selected_anomaly()
        if anomaly is not None:
            self.post_message(self.AnomalySelected(anomaly))