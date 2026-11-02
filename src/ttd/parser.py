"""Lazy parser for JSON Lines log files."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .models import LogEvent


class LogParseError(Exception):
    """Raised when a log line cannot be parsed."""

    def __init__(self, line_number: int, reason: str) -> None:
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"Line {line_number}: {reason}")


class LogParser:
    """Stream log events from a JSONL file without loading it into memory.

    Usage::

        parser = LogParser("app.jsonl")
        for event in parser.stream():
            print(event.timestamp, event.message)
    """

    def __init__(self, path: str | Path, *, strict: bool = False) -> None:
        self.path = Path(path)
        self.strict = strict
        if not self.path.exists():
            raise FileNotFoundError(f"Log file not found: {self.path}")
        if not self.path.is_file():
            raise ValueError(f"Not a file: {self.path}")

    def stream(self) -> Iterator[LogEvent]:
        """Yield ``LogEvent`` objects one at a time.

        Malformed lines are skipped with a warning unless ``strict=True``,
        in which case a ``LogParseError`` is raised immediately.
        """
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    error = LogParseError(line_number, f"invalid JSON: {exc}")
                    if self.strict:
                        raise error from exc
                    self._warn(error)
                    continue

                if not isinstance(raw, dict):
                    error = LogParseError(line_number, "JSON value is not an object")
                    if self.strict:
                        raise error
                    self._warn(error)
                    continue

                try:
                    yield LogEvent.from_raw(raw)
                except Exception as exc:
                    error = LogParseError(line_number, str(exc))
                    if self.strict:
                        raise error from exc
                    self._warn(error)

    @staticmethod
    def _warn(error: LogParseError) -> None:
        import sys

        print(f"[ttd] skipping {error}", file=sys.stderr)

    def count_lines(self) -> int:
        """Return the number of non-empty lines in the file."""
        with self.path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

class JSONLLogParser:
    """Adapter for the format registry."""
    name = "jsonl"
    description = "JSON Lines events"

    def can_parse(self, sample: str) -> bool:
        try:
            return isinstance(json.loads(sample), dict)
        except (ValueError, TypeError):
            return False

    def parse_line(self, line: str) -> LogEvent | None:
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                return None
            if "resourceLogs" in item or "resourceSpans" in item:
                from .otlp import decode_logs_payload, decode_traces_payload
                records = (decode_logs_payload(item) if "resourceLogs" in item else decode_traces_payload(item))
                return records[0] if records else None
            return LogEvent.from_raw(item)
        except (ValueError, TypeError, KeyError):
            return None


def _jsonl_parse_file(self, path: Path):
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                raw = json.loads(line)
                if isinstance(raw, dict) and ("resourceLogs" in raw or "resourceSpans" in raw):
                    from .otlp import decode_any_payload
                    yield from decode_any_payload(raw)
                elif isinstance(raw, dict):
                    yield LogEvent.from_raw(raw)
            except (ValueError, TypeError, KeyError):
                continue

JSONLLogParser.parse_file = _jsonl_parse_file
