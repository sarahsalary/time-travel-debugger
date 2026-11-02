"""Base class and format detection for log parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from ..models import LogEvent


class FormatParser(ABC):
    """Abstract base for log format parsers.

    A parser knows how to read one line of a particular format and turn it
    into a ``LogEvent``. It also knows how to recognise whether a given
    sample of text belongs to its format, which is what ``detect_format``
    relies on.
    """

    name: str = "base"
    description: str = ""

    @abstractmethod
    def can_parse(self, sample: str) -> bool:
        """Return True if ``sample`` looks like this format."""

    @abstractmethod
    def parse_line(self, line: str) -> LogEvent | None:
        """Parse a single line, returning ``None`` if it cannot be parsed."""

    def parse_file(self, path: Path) -> Iterator[LogEvent]:
        """Yield ``LogEvent`` objects from every line in ``path``."""
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if not stripped.strip():
                    continue
                event = self.parse_line(stripped)
                if event is not None:
                    yield event


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_format(path: Path, *, sample_lines: int = 10) -> FormatParser:
    """Pick the best parser for a file by looking at a small sample.

    Parsers are tried in a fixed order (most specific first) so that e.g.
    JSONL is preferred over logfmt when a file happens to contain both.
    """
    # Imported lazily to avoid circular imports.
    from ..parser import JSONLLogParser
    from .logfmt import LogfmtParser
    from .syslog import SyslogParser
    from .csv_format import CSVParser
    from .evtx_format import EVTXParser

    if path.suffix.lower() == ".evtx":
        return EVTXParser()
    if path.suffix.lower() in {".csv", ".tsv"}:
        return CSVParser()

    candidates: list[FormatParser] = [
        JSONLLogParser(),
        SyslogParser(),
        LogfmtParser(),
        CSVParser(),
    ]

    samples = _read_samples(path, sample_lines)
    if not samples:
        return candidates[0]

    for parser in candidates:
        matches = sum(1 for s in samples if parser.can_parse(s))
        # Require at least half of the sampled lines to match.
        if matches >= max(1, len(samples) // 2):
            return parser

    # Nothing matched: fall back to the generic line parser.
    return PlainTextParser()


def _read_samples(path: Path, count: int) -> list[str]:
    samples: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if stripped.strip():
                    samples.append(stripped)
                if len(samples) >= count:
                    break
    except OSError:
        return []
    return samples


# ---------------------------------------------------------------------------
# Plain-text fallback
# ---------------------------------------------------------------------------


class PlainTextParser(FormatParser):
    """A last-resort parser that treats every line as an unstructured event.

    This exists so that ``ttd`` never crashes on an unknown format. Every
    line becomes a ``LogEvent`` with the current time and the whole line as
    the message.
    """

    name = "plain"
    description = "Plain text (one event per line, no structure)"

    def can_parse(self, sample: str) -> bool:
        return True

    def parse_line(self, line: str) -> LogEvent | None:
        from datetime import datetime, timezone

        stripped = line.strip()
        if not stripped:
            return None
        return LogEvent.from_raw(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "INFO",
                "message": stripped,
            }
        )


# ---------------------------------------------------------------------------
# File opener
# ---------------------------------------------------------------------------


class _FileStream:
    """Thin wrapper that streams events from a chosen parser."""

    def __init__(self, path: Path, parser: FormatParser) -> None:
        self.path = path
        self.parser = parser

    def stream(self) -> Iterator[LogEvent]:
        yield from self.parser.parse_file(self.path)


def open_log_file(path: str | Path, *, strict: bool = False) -> _FileStream:
    """Return a stream of events for a log file of any supported format.

    The format is auto-detected from the first few lines. Pass ``strict``
    to raise on malformed lines instead of skipping them.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Log file not found: {p}")

    parser = detect_format(p)
    if strict and parser.name == "jsonl":
        from ..parser import LogParser
        return LogParser(p, strict=True)
    return _FileStream(p, parser)