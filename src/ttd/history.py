"""Persistent alert history for auditing and post-incident review.

Alert history is stored as JSON Lines so it is append-only, cheap to
write from any thread, and trivially greppable. An optional SQLite index
can be built later; for now the file itself is the source of truth.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .watch import Alert, AlertKind


def default_history_dir() -> Path:
    override = os.environ.get("TTD_HISTORY_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ttd" / "history"


def default_history_file() -> Path:
    return default_history_dir() / "alerts.jsonl"


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


@dataclass
class HistoryRecord:
    """A single persisted alert, including delivery metadata."""

    timestamp: str
    kind: str
    severity: str
    message: str
    source: str = ""
    rule_name: str = ""
    payload: dict = field(default_factory=dict)
    delivered_to: list[str] = field(default_factory=list)
    delivery_errors: list[str] = field(default_factory=list)

    @classmethod
    def from_alert(
        cls,
        alert: Alert,
        *,
        source: str = "",
        rule_name: str = "",
        delivered_to: list[str] | None = None,
        delivery_errors: list[str] | None = None,
    ) -> "HistoryRecord":
        return cls(
            timestamp=alert.timestamp.isoformat(),
            kind=alert.kind.value,
            severity=alert.severity,
            message=alert.message,
            source=source,
            rule_name=rule_name,
            payload=dict(alert.payload or {}),
            delivered_to=list(delivered_to or []),
            delivery_errors=list(delivery_errors or []),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryRecord":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class HistoryStore:
    """Append-only JSON Lines store for alert history."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_history_file()
        self._lock = threading.Lock()

    def append(
        self,
        alert: Alert,
        *,
        source: str = "",
        rule_name: str = "",
        delivered_to: list[str] | None = None,
        delivery_errors: list[str] | None = None,
    ) -> HistoryRecord:
        record = HistoryRecord.from_alert(
            alert,
            source=source,
            rule_name=rule_name,
            delivered_to=delivered_to,
            delivery_errors=delivery_errors,
        )
        self._write(record)
        return record

    def _write(self, record: HistoryRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    # -- reading -----------------------------------------------------------

    def read(
        self,
        *,
        limit: int | None = None,
        since: datetime | None = None,
        min_severity: str | None = None,
        kind: str | None = None,
        rule_name: str | None = None,
        contains: str | None = None,
    ) -> list[HistoryRecord]:
        if not self.path.exists():
            return []

        severity_rank = {"low": 0, "medium": 1, "high": 2}
        min_rank = severity_rank.get(min_severity or "low", 0)

        records: list[HistoryRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                record = HistoryRecord.from_dict(raw)

                if since is not None:
                    try:
                        ts = datetime.fromisoformat(record.timestamp)
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                if severity_rank.get(record.severity, 0) < min_rank:
                    continue
                if kind and record.kind != kind:
                    continue
                if rule_name and record.rule_name != rule_name:
                    continue
                if contains and contains.lower() not in record.message.lower():
                    continue

                records.append(record)

        if limit is not None:
            records = records[-limit:]
        return records

    def stats(self) -> dict:
        records = self.read()
        by_kind: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for record in records:
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
            by_severity[record.severity] = by_severity.get(record.severity, 0) + 1
            if record.rule_name:
                by_rule[record.rule_name] = by_rule.get(record.rule_name, 0) + 1
        return {
            "total": len(records),
            "by_kind": by_kind,
            "by_severity": by_severity,
            "by_rule": by_rule,
            "first": records[0].timestamp if records else None,
            "last": records[-1].timestamp if records else None,
        }

    def clear(self) -> int:
        if not self.path.exists():
            return 0
        count = sum(1 for _ in self.path.open("r", encoding="utf-8"))
        self.path.unlink()
        return count

    def export_csv(self, output: Path) -> Path:
        """Export history to a CSV file for spreadsheet analysis."""
        import csv

        records = self.read()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["timestamp", "kind", "severity", "rule_name",
                 "message", "source", "delivered_to"]
            )
            for record in records:
                writer.writerow(
                    [
                        record.timestamp,
                        record.kind,
                        record.severity,
                        record.rule_name,
                        record.message,
                        record.source,
                        ",".join(record.delivered_to),
                    ]
                )
        return output