"""Generate self-contained HTML reports from log analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from ..anomaly import AnomalyDetector
from ..cluster import DrainTemplateMiner
from ..graph import detect_dependencies
from ..indexer import TimeIndex
from ..models import LogEvent
from .template import REPORT_TEMPLATE


# ---------------------------------------------------------------------------
# SVG sparkline
# ---------------------------------------------------------------------------


def build_sparkline_svg(
    values: list[float],
    *,
    width: int = 1200,
    height: int = 60,
    color: str = "#5eb8ff",
) -> str:
    """Render a tiny inline SVG sparkline for the event timeline.

    The SVG is generated directly as a string so no external plotting
    library is required and the report stays completely self-contained.
    """
    if not values or len(values) < 2:
        return ""

    max_value = max(values) or 1.0
    step_x = width / (len(values) - 1)

    points = []
    for i, value in enumerate(values):
        x = i * step_x
        y = height - (value / max_value) * (height - 4) - 2
        points.append(f"{x:.1f},{y:.1f}")

    polyline = " ".join(points)
    area = f"0,{height} " + polyline + f" {width},{height}"

    return (
        f'<svg class="sparkline" viewBox="0 0 {width}{height}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{color}" fill-opacity="0.12"/>'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# Report data
# ---------------------------------------------------------------------------


@dataclass
class ReportOptions:
    """Controls which sections appear in the generated report."""

    include_anomalies: bool = True
    include_clusters: bool = True
    include_dependencies: bool = True
    include_recent_errors: bool = True
    include_timeline: bool = True
    anomaly_window: int = 60
    anomaly_sensitivity: float = 2.5
    recent_errors_limit: int = 50


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class HTMLReporter:
    """Produce a self-contained HTML report from a ``TimeIndex``."""

    def __init__(self, index: TimeIndex, *, log_file: str = "") -> None:
        self.index = index
        self.log_file = log_file or "(unknown)"
        self.events = list(index.events)

    # -- public API --------------------------------------------------------

    def generate(self, options: ReportOptions | None = None) -> str:
        """Return the complete HTML document as a string."""
        options = options or ReportOptions()

        try:
            from jinja2 import Template
        except ImportError as exc:
            raise RuntimeError(
                "HTML reports require the 'jinja2' package. "
                "Install it with: pip install 'ttd[report]'"
            ) from exc

        context = {
            "log_file": escape(self.log_file),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "ttd_version": _ttd_version(),
            "stats": self._build_stats(),
            "timeline_svg": (
                self._build_timeline_svg() if options.include_timeline else ""
            ),
            "anomalies": (
                self._build_anomalies(options)
                if options.include_anomalies
                else []
            ),
            "clusters": (
                self._build_clusters() if options.include_clusters else []
            ),
            "dependencies": (
                self._build_dependencies() if options.include_dependencies else []
            ),
            "recent_errors": (
                self._build_recent_errors(options.recent_errors_limit)
                if options.include_recent_errors
                else []
            ),
        }

        return Template(REPORT_TEMPLATE).render(**context)

    def write(
        self,
        output: Path,
        options: ReportOptions | None = None,
    ) -> Path:
        """Render the report and write it to ``output``."""
        html = self.generate(options)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        return output

    # -- section builders --------------------------------------------------

    def _build_stats(self) -> dict:
        levels: dict[str, int] = {}
        services: set[str] = set()
        errors = 0
        warnings = 0

        for event in self.events:
            levels[event.level] = levels.get(event.level, 0) + 1
            if event.service:
                services.add(event.service)
            if event.level.upper() in {"ERROR", "CRITICAL", "FATAL"}:
                errors += 1
            elif event.level.upper() in {"WARNING", "WARN"}:
                warnings += 1

        if self.events:
            start = self.events[0].timestamp
            end = self.events[-1].timestamp
            delta = end - start
            seconds = int(delta.total_seconds())
            if seconds < 60:
                span = f"{seconds}s"
            elif seconds < 3600:
                span = f"{seconds // 60}m {seconds % 60}s"
            else:
                span = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
        else:
            span = "-"

        ordered_levels = sorted(
            levels.items(),
            key=lambda kv: _level_rank(kv[0]),
        )

        return {
            "total_events": len(self.events),
            "services": sorted(services),
            "error_count": errors,
            "warning_count": warnings,
            "time_span": span,
            "levels": ordered_levels,
            "max_level": max(levels.values()) if levels else 1,
        }

    def _build_timeline_svg(self) -> str:
        if not self.events:
            return ""
        # Bucket events into 80 evenly-sized bins across the time range.
        buckets = self._bucket_events(count=80)
        return build_sparkline_svg(buckets)

    def _bucket_events(self, *, count: int = 80) -> list[float]:
        if not self.events:
            return []
        start = self.events[0].timestamp.timestamp()
        end = self.events[-1].timestamp.timestamp()
        if end <= start:
            return [float(len(self.events))]

        span = end - start
        buckets = [0.0] * count
        for event in self.events:
            offset = event.timestamp.timestamp() - start
            index = min(int(offset / span * count), count - 1)
            buckets[index] += 1.0
        return buckets

    def _build_anomalies(self, options: ReportOptions) -> list[dict]:
        if not self.events:
            return []
        detector = AnomalyDetector(
            window_seconds=options.anomaly_window,
            sensitivity=options.anomaly_sensitivity,
        )
        report = detector.analyze(self.events)

        results = []
        for anomaly in report.top(20):
            results.append(
                {
                    "window_start": anomaly.window.start.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "metric": anomaly.metric.value,
                    "value": f"{anomaly.value:.1f}",
                    "baseline": (
                        f"{anomaly.baseline_mean:.1f} "
                        f"&plusmn; {anomaly.baseline_std:.1f}"
                    ),
                    "z_score": f"{anomaly.z_score:+.2f}",
                    "severity": anomaly.severity.value,
                    "severity_badge": _severity_badge(anomaly.severity.value),
                }
            )
        return results

    def _build_clusters(self) -> list[dict]:
        error_events = [
            e for e in self.events
            if e.level.upper() in {"ERROR", "CRITICAL", "FATAL"}
        ]
        if not error_events:
            return []

        miner = DrainTemplateMiner()
        miner.add_events(error_events)
        report = miner.build_report()

        results = []
        for cluster in report.top(25):
            services = ", ".join(
                f"{svc}({n})" for svc, n in cluster.service_counts.most_common(3)
            ) or "-"
            results.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "count": cluster.count,
                    "error_count": cluster.error_count,
                    "services": escape(services),
                    "template": escape(cluster.template),
                }
            )
        return results

    def _build_dependencies(self) -> list[tuple[str, str]]:
        return [
            (escape(a), escape(b))
            for a, b in detect_dependencies(self.events)
        ]

    def _build_recent_errors(self, limit: int) -> list[dict]:
        errors = [
            e for e in self.events
            if e.level.upper() in {"ERROR", "CRITICAL", "FATAL"}
        ]
        results = []
        for event in errors[-limit:]:
            results.append(
                {
                    "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "level": escape(event.level.upper()),
                    "service": escape(event.service or "-"),
                    "message": escape(event.short_message),
                }
            )
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LEVEL_RANK = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "WARN": 2,
    "ERROR": 3,
    "CRITICAL": 4,
    "FATAL": 4,
}


def _level_rank(level: str) -> int:
    return _LEVEL_RANK.get(level.upper(), 5)


def _severity_badge(severity: str) -> str:
    return {
        "low": "INFO",
        "medium": "WARNING",
        "high": "ERROR",
    }.get(severity, "INFO")


def _ttd_version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "unknown"