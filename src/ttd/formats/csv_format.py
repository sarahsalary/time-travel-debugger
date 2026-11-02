"""Parser for CSV and TSV log files.

CSV is the lowest common denominator for exported logs: every SIEM,
every spreadsheet, and every ``pandas.to_csv()`` call produces one. The
parser auto-detects the dialect from a sample of the file, reads the
first row as the header, and maps common column names onto the standard
``LogEvent`` fields.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any

from ..models import LogEvent
from .base import FormatParser


# ---------------------------------------------------------------------------
# Column name heuristics
# ---------------------------------------------------------------------------

# Keys we recognise as a timestamp, in priority order.
_TIMESTAMP_COLUMNS = (
    "timestamp", "time", "ts", "@timestamp", "datetime", "date",
    "event_time", "eventtime", "time_created", "timecreated",
    "utc_time", "utctime",
)

# Keys we recognise as a severity level.
_LEVEL_COLUMNS = (
    "level", "lvl", "severity", "log_level", "loglevel", "priority",
)

# Keys we recognise as the message body.
_MESSAGE_COLUMNS = (
    "message", "msg", "log", "event", "description", "text", "body",
)

# Keys we recognise as the service / source.
_SERVICE_COLUMNS = (
    "service", "svc", "component", "logger", "app", "application",
    "source", "provider", "provider_name", "channel",
)


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

_EPOCH_RE = re.compile(r"^\d{10}(?:\.\d+)?$")
_EPOCH_MS_RE = re.compile(r"^\d{13}$")


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort timestamp parsing for a CSV cell."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    # Numeric epoch (seconds or milliseconds).
    if _EPOCH_RE.match(text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if _EPOCH_MS_RE.match(text):
        try:
            return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        except (ValueError, OSError):
            return None

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            from dateutil import parser as dateparser

            dt = dateparser.parse(text)
        except Exception:
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize_level(value: Any) -> str:
    """Map a raw level cell to one of the standard ttd levels."""
    if value is None:
        return "INFO"
    text = str(value).strip().upper()
    if not text:
        return "INFO"

    # Numeric levels follow the syslog convention (0=highest).
    if text.isdigit():
        code = int(text)
        return {
            0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR",
            4: "WARNING", 5: "INFO", 6: "INFO", 7: "DEBUG",
        }.get(code, "INFO")

    aliases = {
        "WARN": "WARNING",
        "ERR": "ERROR",
        "FATAL": "CRITICAL",
        "EMERG": "CRITICAL",
        "EMERGENCY": "CRITICAL",
        "NOTICE": "INFO",
        "TRACE": "DEBUG",
        "VERBOSE": "DEBUG",
    }
    return aliases.get(text, text)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class CSVParser(FormatParser):
    """Parser for comma-, tab-, semicolon-, and pipe-separated log files."""

    name = "csv"
    description = "CSV/TSV (delimiter auto-detected)"

    # Delimiters we will try if the sniffer fails.
    _FALLBACK_DELIMITERS = [",", "\t", ";", "|"]

    def __init__(self, *, delimiter: str | None = None) -> None:
        self._explicit_delimiter = delimiter
        self._dialect: csv.Dialect | None = None

    # -- detection ---------------------------------------------------------

    def can_parse(self, sample: str) -> bool:
        """A line looks like CSV if it has a delimiter and at least two fields."""
        if not sample or sample.lstrip().startswith(("{", "[", "<")):
            return False
        # Require at least one consistent delimiter.
        for candidate in self._FALLBACK_DELIMITERS:
            if sample.count(candidate) >= 1:
                return True
        return False

    # -- parsing -----------------------------------------------------------

    def parse_file(self, path):  # type: ignore[override]
        """Read the whole file, detecting the dialect from the header."""
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)

            dialect = self._detect_dialect(sample)
            reader = csv.DictReader(handle, dialect=dialect)

            if not reader.fieldnames:
                return

            # Normalise header names for matching.
            header_map = {name: name for name in reader.fieldnames}
            ts_col = _find_column(header_map, _TIMESTAMP_COLUMNS)
            level_col = _find_column(header_map, _LEVEL_COLUMNS)
            msg_col = _find_column(header_map, _MESSAGE_COLUMNS)
            svc_col = _find_column(header_map, _SERVICE_COLUMNS)

            for row in reader:
                event = self._row_to_event(
                    row,
                    header_map=header_map,
                    ts_col=ts_col,
                    level_col=level_col,
                    msg_col=msg_col,
                    svc_col=svc_col,
                )
                if event is not None:
                    yield event

    def parse_line(self, line: str) -> LogEvent | None:
        """Parse a single CSV line without a header (best effort)."""
        stripped = line.strip()
        if not stripped:
            return None
        dialect = self._detect_dialect(stripped)
        try:
            row = next(csv.reader(io.StringIO(stripped), dialect=dialect))
        except (csv.Error, StopIteration):
            return None
        if not row:
            return None
        # Without a header we cannot map columns; use the first cell as the
        # message and the current time as the timestamp.
        return LogEvent.from_raw(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "INFO",
                "message": " ".join(row),
            }
        )

    # -- internals ---------------------------------------------------------

    def _detect_dialect(self, sample: str) -> csv.Dialect:
        if self._explicit_delimiter is not None:
            dialect = csv.excel()
            dialect.delimiter = self._explicit_delimiter
            return dialect

        try:
            return csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            # The sniffer is unreliable on single-column files; fall back
            # to the first delimiter that appears in the sample.
            for candidate in self._FALLBACK_DELIMITERS:
                if candidate in sample:
                    dialect = csv.excel()
                    dialect.delimiter = candidate
                    return dialect
            return csv.excel()

    def _row_to_event(
        self,
        row: dict[str, Any],
        *,
        header_map: dict[str, str],
        ts_col: str | None,
        level_col: str | None,
        msg_col: str | None,
        svc_col: str | None,
    ) -> LogEvent | None:
        # Timestamp: use the mapped column, or fall back to the current time.
        ts: datetime | None = None
        if ts_col is not None:
            ts = _parse_timestamp(row.get(ts_col))
        if ts is None:
            ts = datetime.now(timezone.utc)

        level = _normalize_level(row.get(level_col)) if level_col else "INFO"

        message = ""
        if msg_col is not None:
            message = str(row.get(msg_col, "") or "").strip()
        if not message:
            # Reconstruct a message from the remaining columns.
            parts = [
                f"{k}={v}"
                for k, v in row.items()
                if k not in {ts_col, level_col, svc_col} and v not in (None, "")
            ]
            message = " ".join(parts) or "(empty row)"

        service = None
        if svc_col is not None:
            raw_service = row.get(svc_col)
            service = str(raw_service).strip() if raw_service else None

        # Everything else goes into extra.
        consumed = {c for c in (ts_col, level_col, msg_col, svc_col) if c}
        extra: dict[str, Any] = {}
        for key, value in row.items():
            if key in consumed or key is None:
                continue
            if value is None or value == "":
                continue
            extra[str(key)] = _coerce(value)

        try:
            return LogEvent.from_raw(
                {
                    "timestamp": ts.isoformat(),
                    "level": level,
                    "message": message,
                    "service": service,
                    "extra": extra,
                }
            )
        except Exception:
            return None


def _find_column(
    header_map: dict[str, str], candidates: tuple[str, ...]
) -> str | None:
    """Return the first header name matching any candidate (case-insensitive)."""
    lowered = {name.lower(): name for name in header_map}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _coerce(value: str) -> Any:
    """Turn a CSV cell into a plausible Python scalar."""
    text = value.strip()
    if text == "":
        return ""
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() in ("null", "none", "nan"):
        return None
    if _INT_RE.match(text):
        try:
            return int(text)
        except ValueError:
            return text
    if _FLOAT_RE.match(text):
        try:
            return float(text)
        except ValueError:
            return text
    return text