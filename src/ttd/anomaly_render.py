"""Terminal rendering for anomaly reports."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .anomaly import Anomaly, AnomalyReport, MetricKind, Severity

console = Console()

# Braille blocks give sub-character resolution for the sparkline.
SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"

SEVERITY_STYLES: dict[Severity, str] = {
    Severity.LOW: "yellow",
    Severity.MEDIUM: "bold yellow",
    Severity.HIGH: "bold red",
}

METRIC_LABELS: dict[MetricKind, str] = {
    MetricKind.TOTAL_VOLUME: "Volume",
    MetricKind.ERROR_COUNT: "Errors",
    MetricKind.WARNING_COUNT: "Warnings",
    MetricKind.AVG_DURATION: "Avg duration (ms)",
}


def render_sparkline(values: list[float], width: int = 60) -> Text:
    """Render a compact density sparkline using Braille-like blocks."""
    if not values:
        return Text("(no data)", style="dim")

    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = list(values)

    peak = max(sampled) or 1.0
    text = Text()
    for value in sampled:
        level = int((value / peak) * (len(SPARK_BLOCKS) - 1))
        text.append(SPARK_BLOCKS[level], style="cyan")
    return text


def render_anomaly_report(report: AnomalyReport, *, limit: int = 20) -> None:
    """Render a full anomaly report to the terminal."""
    if not report.windows:
        console.print("[yellow]No events to analyze.[/]")
        return

    console.print(
        Panel(
            f"[bold]Windows:[/] {len(report.windows)} × {report.window_seconds}s\n"
            f"[bold]Anomalies:[/] {len(report.anomalies)}",
            title="Anomaly Report",
            border_style="cyan",
        )
    )

    if not report.has_anomalies:
        console.print("[green]No anomalies detected.[/]")
        return

    # Group anomalies by metric for the sparkline overview
    metrics_by_kind: dict[MetricKind, list[float]] = {}
    for kind in MetricKind:
        metrics_by_kind[kind] = [w.metrics[kind] for w in report.windows]

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold cyan", no_wrap=True)
    overview.add_column()
    for kind, series in metrics_by_kind.items():
        overview.add_row(METRIC_LABELS[kind], render_sparkline(series))
    console.print(Panel(overview, title="Metric Distribution", border_style="dim"))

    # Detailed anomaly table
    table = Table(
        title=f"Top {min(limit, len(report.anomalies))} Anomalies",
        show_header=True,
        header_style="bold",
    )
    table.add_column("When", style="dim", width=20)
    table.add_column("Metric", width=18)
    table.add_column("Value", justify="right", width=10)
    table.add_column("Baseline", justify="right", width=18)
    table.add_column("Z", justify="right", width=8)
    table.add_column("Severity", width=10)
    table.add_column("Direction", width=6)

    for anomaly in report.top(limit):
        table.add_row(
            anomaly.window.start.strftime("%Y-%m-%d %H:%M:%S"),
            METRIC_LABELS[anomaly.metric],
            f"{anomaly.value:.1f}",
            f"{anomaly.baseline_mean:.1f} ± {anomaly.baseline_std:.1f}",
            f"{anomaly.z_score:+.2f}",
            Text(anomaly.severity.value, style=SEVERITY_STYLES[anomaly.severity]),
            "▲ up" if anomaly.is_spike else "▼ down",
        )

    console.print(table)


def render_anomaly_summary(report: AnomalyReport) -> Text:
    """Return a one-line summary suitable for a TUI status bar."""
    if not report.has_anomalies:
        return Text("no anomalies", style="green")

    counts = {sev: 0 for sev in Severity}
    for anomaly in report.anomalies:
        counts[anomaly.severity] += 1

    text = Text()
    text.append("anomalies: ", style="bold")
    text.append(f"{counts[Severity.HIGH]}H ", style="bold red")
    text.append(f"{counts[Severity.MEDIUM]}M ", style="bold yellow")
    text.append(f"{counts[Severity.LOW]}L", style="yellow")
    return text