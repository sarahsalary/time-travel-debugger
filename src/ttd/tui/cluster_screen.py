"""Modal screen showing cluster results inside the replay TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static

from ..cluster import ClusterReport, TemplateCluster


class ClusterScreen(Screen):
    """A modal screen listing clusters with drill-down details."""

    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Close"),
        Binding("e", "toggle_errors_only", "Errors only"),
        Binding("enter", "select_cluster", "Select", show=False),
    ]

    class ClusterSelected(Message):
        """Posted when the user picks a cluster to inspect."""

        def __init__(self, cluster: TemplateCluster) -> None:
            self.cluster = cluster
            super().__init__()

    DEFAULT_CSS = """
    ClusterScreen {
        layout: vertical;
    }

    ClusterScreen #cluster-header {
        height: 3;
        padding: 1 1 0 1;
        background: $boost;
    }

    ClusterScreen #cluster-table {
        height: 1fr;
        border: solid $accent;
    }

    ClusterScreen #cluster-detail {
        height: 10;
        border: solid $success;
        padding: 0 1;
    }
    """

    def __init__(self, report: ClusterReport) -> None:
        super().__init__()
        self._report = report
        self._errors_only = False
        self._rows: list[TemplateCluster] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(self._header_text(), id="cluster-header")
        yield DataTable(id="cluster-table", cursor_type="row", zebra_stripes=True)
        yield Static("Select a cluster to see its details.", id="cluster-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#cluster-table", DataTable)
        table.add_column("#", width=4)
        table.add_column("Count", width=7)
        table.add_column("Errors", width=7)
        table.add_column("First", width=10)
        table.add_column("Last", width=10)
        table.add_column("Template", ratio=1)
        self._refresh_rows()

    # -- helpers -----------------------------------------------------------

    def _header_text(self) -> str:
        mode = "errors only" if self._errors_only else "all clusters"
        return (
            f"[bold]Cluster Report[/]  "
            f"[dim]{self._report.total_events} events → "
            f"{self._report.total_clusters} clusters[/]  "
            f"[cyan]{mode}[/]  [dim](e to toggle)[/]"
        )

    def _refresh_rows(self) -> None:
        table = self.query_one("#cluster-table", DataTable)
        table.clear()
        self._rows = (
            self._report.errors(200)
            if self._errors_only
            else self._report.top(200)
        )
        for cluster in self._rows:
            table.add_row(
                str(cluster.cluster_id),
                str(cluster.count),
                str(cluster.error_count),
                cluster.first_seen.strftime("%H:%M:%S") if cluster.first_seen else "-",
                cluster.last_seen.strftime("%H:%M:%S") if cluster.last_seen else "-",
                cluster.template,
                key=str(cluster.cluster_id),
            )
        if self._rows:
            table.move_cursor(row=0)
            self._show_detail(self._rows[0])

    def _show_detail(self, cluster: TemplateCluster) -> None:
        services = ", ".join(
            f"{svc}={n}" for svc, n in cluster.service_counts.most_common()
        )
        levels = ", ".join(
            f"{lvl}={n}" for lvl, n in cluster.level_counts.most_common()
        )
        sample = cluster.sample_event.message if cluster.sample_event else "-"
        text = (
            f"[bold cyan]Template[/]  {cluster.template}\n"
            f"[bold cyan]Count[/]     {cluster.count}  "
            f"[bold cyan]Errors[/] {cluster.error_count}\n"
            f"[bold cyan]Levels[/]    {levels or '-'}\n"
            f"[bold cyan]Services[/]  {services or '-'}\n"
            f"[bold cyan]Sample[/]    {sample}"
        )
        self.query_one("#cluster-detail", Static).update(text)

    # -- actions -----------------------------------------------------------

    def action_toggle_errors_only(self) -> None:
        self._errors_only = not self._errors_only
        self.query_one("#cluster-header", Label).update(self._header_text())
        self._refresh_rows()

    def action_select_cluster(self) -> None:
        table = self.query_one("#cluster-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._rows):
            self.post_message(self.ClusterSelected(self._rows[row]))

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        row = event.cursor_row
        if 0 <= row < len(self._rows):
            self._show_detail(self._rows[row])