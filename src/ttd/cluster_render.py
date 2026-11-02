"""Terminal rendering for cluster reports."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .cluster import ClusterReport, TemplateCluster

console = Console()


def _level_style(level: str) -> str:
    return {
        "DEBUG": "dim",
        "INFO": "cyan",
        "WARNING": "yellow",
        "WARN": "yellow",
        "ERROR": "bold red",
        "CRITICAL": "bold white on red",
        "FATAL": "bold white on red",
    }.get(level.upper(), "white")


def render_cluster_report(
    report: ClusterReport,
    *,
    limit: int = 30,
    errors_only: bool = False,
) -> None:
    """Render a cluster report to the terminal."""
    if report.total_events == 0:
        console.print("[yellow]No events to cluster.[/]")
        return

    compression = (
        f"{report.total_events} events collapsed into "
        f"{report.total_clusters} clusters"
    )
    console.print(
        Panel(
            f"[bold]{compression}[/]\n"
            f"[dim]Unique ratio: {report.unique_ratio:.2%}[/]",
            title="Cluster Report",
            border_style="cyan",
        )
    )

    if errors_only:
        clusters = report.errors(limit)
        title = f"Top {len(clusters)} Error Clusters"
    else:
        clusters = report.top(limit)
        title = f"Top {len(clusters)} Clusters"

    if not clusters:
        console.print("[green]No matching clusters.[/]")
        return

    table = Table(
        title=title,
        show_header=True,
        header_style="bold",
        expand=True,
    )
    table.add_column("#", width=4, justify="right")
    table.add_column("Count", width=8, justify="right")
    table.add_column("Errors", width=7, justify="right")
    table.add_column("Services", width=18)
    table.add_column("First seen", width=12)
    table.add_column("Last seen", width=12)
    table.add_column("Template", ratio=1)

    for cluster in clusters:
        error_style = "bold red" if cluster.error_count else "dim"
        services = ", ".join(
            f"{svc}({n})" for svc, n in cluster.service_counts.most_common(3)
        ) or "-"
        table.add_row(
            str(cluster.cluster_id),
            str(cluster.count),
            Text(str(cluster.error_count), style=error_style),
            services,
            cluster.first_seen.strftime("%H:%M:%S") if cluster.first_seen else "-",
            cluster.last_seen.strftime("%H:%M:%S") if cluster.last_seen else "-",
            cluster.template,
        )

    console.print(table)


def render_cluster_detail(cluster: TemplateCluster) -> None:
    """Render the details of a single cluster as a panel."""
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold cyan", no_wrap=True)
    body.add_column()

    body.add_row("Cluster ID", str(cluster.cluster_id))
    body.add_row("Template", cluster.template)
    body.add_row("Count", str(cluster.count))
    body.add_row("Errors", str(cluster.error_count))

    if cluster.first_seen:
        body.add_row("First seen", cluster.first_seen.isoformat())
    if cluster.last_seen:
        body.add_row("Last seen", cluster.last_seen.isoformat())

    if cluster.level_counts:
        levels = ", ".join(
            f"{lvl}={n}" for lvl, n in cluster.level_counts.most_common()
        )
        body.add_row("Levels", levels)

    if cluster.service_counts:
        services = ", ".join(
            f"{svc}={n}" for svc, n in cluster.service_counts.most_common()
        )
        body.add_row("Services", services)

    if cluster.sample_event is not None:
        body.add_row("Sample", cluster.sample_event.message)

    console.print(Panel(body, title="Cluster Detail", border_style="cyan"))