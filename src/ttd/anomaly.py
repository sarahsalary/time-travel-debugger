"""Statistical anomaly detection for structured log streams.

The detector buckets log events into fixed-size time windows, computes a
small set of metrics per window (total volume, error count, warning count),
and scores each window against a rolling baseline using a z-score. Windows
whose z-score exceeds the configured threshold are reported as anomalies.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .models import LogEvent


class MetricKind(str, Enum):
    """The kinds of metrics the detector tracks per window."""

    TOTAL_VOLUME = "total_volume"
    ERROR_COUNT = "error_count"
    WARNING_COUNT = "warning_count"
    AVG_DURATION = "avg_duration"


class Severity(str, Enum):
    """How strongly a window deviates from the baseline."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def from_z_score(cls, z: float) -> "Severity":
        magnitude = abs(z)
        if magnitude >= 4.0:
            return cls.HIGH
        if magnitude >= 3.0:
            return cls.MEDIUM
        return cls.LOW


@dataclass
class TimeWindow:
    """A fixed-size time bucket of log events."""

    start: datetime
    end: datetime
    events: list[LogEvent] = field(default_factory=list)

    @property
    def metrics(self) -> dict[MetricKind, float]:
        total = float(len(self.events))
        errors = float(
            sum(1 for e in self.events if e.level in {"ERROR", "CRITICAL", "FATAL"})
        )
        warnings = float(
            sum(1 for e in self.events if e.level in {"WARNING", "WARN"})
        )
        durations = [e.duration_ms for e in self.events if e.duration_ms is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        return {
            MetricKind.TOTAL_VOLUME: total,
            MetricKind.ERROR_COUNT: errors,
            MetricKind.WARNING_COUNT: warnings,
            MetricKind.AVG_DURATION: avg_duration,
        }


@dataclass
class Anomaly:
    """A window whose metric deviates significantly from the baseline."""

    window: TimeWindow
    metric: MetricKind
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    severity: Severity
    direction: str  # "up" or "down"

    @property
    def is_spike(self) -> bool:
        return self.direction == "up"

    @property
    def is_dip(self) -> bool:
        return self.direction == "down"

    def describe(self) -> str:
        arrow = "▲" if self.is_spike else "▼"
        return (
            f"{arrow}{self.metric.value} = {self.value:.1f} "
            f"(baseline {self.baseline_mean:.1f} ± {self.baseline_std:.1f}, "
            f"z={self.z_score:+.2f}, {self.severity.value})"
        )


@dataclass
class AnomalyReport:
    """The full result of running the detector over an event stream."""

    windows: list[TimeWindow]
    anomalies: list[Anomaly]
    window_seconds: int

    @property
    def has_anomalies(self) -> bool:
        return bool(self.anomalies)

    def by_window(self) -> dict[datetime, list[Anomaly]]:
        """Group anomalies by their window start time."""
        grouped: dict[datetime, list[Anomaly]] = defaultdict(list)
        for anomaly in self.anomalies:
            grouped[anomaly.window.start].append(anomaly)
        return dict(grouped)

    def top(self, n: int = 10) -> list[Anomaly]:
        """Return the ``n`` anomalies with the largest absolute z-score."""
        return sorted(self.anomalies, key=lambda a: abs(a.z_score), reverse=True)[:n]


class AnomalyDetector:
    """Detect statistically significant deviations in a log stream.

    Parameters
    ----------
    window_seconds:
        Size of each time bucket in seconds. Defaults to 60 (one minute).
    sensitivity:
        Minimum absolute z-score for a window to be flagged. Defaults to 2.5.
    baseline_windows:
        How many preceding windows to use as the rolling baseline. Defaults
        to 20.
    min_baseline_windows:
        Minimum number of preceding windows required before scoring starts.
        Defaults to 5.
    """

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        sensitivity: float = 2.5,
        baseline_windows: int = 20,
        min_baseline_windows: int = 5,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if sensitivity <= 0:
            raise ValueError("sensitivity must be positive")
        if baseline_windows < 2:
            raise ValueError("baseline_windows must be at least 2")
        if min_baseline_windows < 2:
            raise ValueError("min_baseline_windows must be at least 2")

        self.window_seconds = window_seconds
        self.sensitivity = sensitivity
        self.baseline_windows = baseline_windows
        self.min_baseline_windows = min_baseline_windows

    # -- public API --------------------------------------------------------

    def analyze(self, events: list[LogEvent]) -> AnomalyReport:
        """Run the detector over a list of events."""
        if not events:
            return AnomalyReport(windows=[], anomalies=[], window_seconds=self.window_seconds)

        windows = self._bucket(events)
        anomalies = self._score(windows)
        anomalies.sort(key=lambda a: (a.window.start, -abs(a.z_score)))

        return AnomalyReport(
            windows=windows,
            anomalies=anomalies,
            window_seconds=self.window_seconds,
        )

    # -- internals ---------------------------------------------------------

    def _bucket(self, events: list[LogEvent]) -> list[TimeWindow]:
        """Group events into fixed-size, gap-free time windows."""
        ordered = sorted(events, key=lambda e: e.timestamp)
        first = ordered[0].timestamp
        last = ordered[-1].timestamp

        # Align the first window to the nearest second boundary so that
        # two runs over overlapping data produce the same buckets.
        aligned_start = first.replace(microsecond=0)
        step = timedelta(seconds=self.window_seconds)

        windows: list[TimeWindow] = []
        cursor = aligned_start
        index_by_start: dict[datetime, TimeWindow] = {}

        while cursor <= last + step:
            window = TimeWindow(start=cursor, end=cursor + step)
            windows.append(window)
            index_by_start[cursor] = window
            cursor += step

        for event in ordered:
            epoch = event.timestamp.timestamp()
            bucket_epoch = epoch - (epoch % self.window_seconds)
            bucket_start = datetime.fromtimestamp(
                bucket_epoch, tz=event.timestamp.tzinfo
            )
            window = index_by_start.get(bucket_start)
            if window is not None:
                window.events.append(event)

        return windows

    def _score(self, windows: list[TimeWindow]) -> list[Anomaly]:
        """Score every window against its rolling baseline."""
        if len(windows) < self.min_baseline_windows + 1:
            return []

        metrics_by_kind: dict[MetricKind, list[float]] = {
            kind: [w.metrics[kind] for w in windows] for kind in MetricKind
        }

        anomalies: list[Anomaly] = []
        for kind, values in metrics_by_kind.items():
            anomalies.extend(self._score_series(kind, values, windows))
        return anomalies

    def _score_series(
        self,
        kind: MetricKind,
        values: list[float],
        windows: list[TimeWindow],
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        for index, value in enumerate(values):
            if index < self.min_baseline_windows:
                continue

            baseline = self._baseline(values, index)
            if baseline is None:
                continue

            mean, std = baseline
            if std == 0.0:
                # A perfectly flat baseline cannot produce a z-score.
                # Fall back to a small epsilon derived from the mean so that
                # a single non-zero window still registers as anomalous.
                if mean == 0.0 and value == 0.0:
                    continue
                std = max(abs(mean) * 0.05, 0.5)

            z = (value - mean) / std
            if abs(z) < self.sensitivity:
                continue

            anomalies.append(
                Anomaly(
                    window=windows[index],
                    metric=kind,
                    value=value,
                    baseline_mean=mean,
                    baseline_std=std,
                    z_score=z,
                    severity=Severity.from_z_score(z),
                    direction="up" if z > 0 else "down",
                )
            )
        return anomalies

    def _baseline(
        self, values: list[float], index: int
    ) -> tuple[float, float] | None:
        """Compute the rolling (mean, std) for the window before ``index``."""
        lo = max(0, index - self.baseline_windows)
        sample = values[lo:index]
        if len(sample) < self.min_baseline_windows:
            return None
        mean = sum(sample) / len(sample)
        variance = sum((v - mean) ** 2 for v in sample) / len(sample)
        return mean, variance ** 0.5