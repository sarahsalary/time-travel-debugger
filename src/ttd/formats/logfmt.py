"""Parser for the logfmt format.

Logfmt is a compact, human-friendly format pioneered by Heroku and widely
adopted by Go services via ``go-kit/log``, by Kubernetes via Loki, and by
Grafana. Each line is a sequence of ``key=value`` pairs separated by ASCII
whitespace. Values may be quoted with double quotes and may contain
backslash escapes.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import LogEvent
from .base import FormatParser


# A logfmt line looks like:
#     level=info msg="hello world" user_id=42 duration=120ms
#
# We tokenize on whitespace, but respect double-quoted strings so that
# values containing spaces stay together.
_TOKEN_RE = re.compile(
    r"""
    \s*
    (?:
        (?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)
        =
        (?:
            "(?P<quoted>(?:[^"\\]|\\.)*)"
            |
            (?P<bare>[^\s"]+)
        )
        |
        (?P<flag>[A-Za-z_][A-Za-z0-9_.\-]*)
    )
    """,
    re.VERBOSE,
)

# Recognised timestamp keys, in priority order.
_TIMESTAMP_KEYS = ("time", "timestamp", "ts", "@timestamp", "t")

# Recognised level keys.
_LEVEL_KEYS = ("level", "lvl", "severity", "log_level")

# Recognised message keys.
_MESSAGE_KEYS = ("msg", "message", "log", "event")

# Recognised service keys.
_SERVICE_KEYS = ("service", "svc", "component", "logger", "app", "name")


def parse_logfmt_line(line: str) -> dict[str, Any]:
    """Parse a single logfmt line into a dictionary of raw values.

    Duplicate keys are preserved in the order they were seen; the last
    occurrence wins for scalar extraction.
    """
    result: dict[str, Any] = {}
    for match in _TOKEN_RE.finditer(line):
        if match.group("flag") is not None:
            # A bare key with no value: store it as True.
            result.setdefault(match.group("flag"), True)
            continue

        key = match.group("key")
        if key is None:
            continue

        if match.group("quoted") is not None:
            value: Any = _unescape(match.group("quoted"))
        else:
            value = match.group("bare")
            value = _coerce_scalar(value)

        result[key] = value

    return result


def _unescape(raw: str) -> str:
    """Resolve backslash escapes in a quoted logfmt value."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(
                {
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    "\\": "\\",
                    '"': '"',
                }.get(nxt, nxt)
            )
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _coerce_scalar(value: str) -> Any:
    """Turn a bare logfmt value into an int, float, bool, or string."""
    if value == "":
        return ""
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if _NUMBER_RE.match(value):
        return float(value) if "." in value else int(value)
    return value


class LogfmtParser(FormatParser):
    """Parser for Heroku-style ``key=value`` log lines."""

    name = "logfmt"
    description = "Logfmt (Heroku-style key=value pairs)"

    def can_parse(self, sample: str) -> bool:
        """A line looks like logfmt if it has at least one ``key=value``."""
        if not sample or sample.lstrip().startswith(("{", "[")):
            return False
        # Require at least one key=value pair with a plausible key name.
        return bool(
            re.search(r"[A-Za-z_][A-Za-z0-9_.\-]*\s*=\s*\S", sample)
        )

    def parse_line(self, line: str) -> LogEvent | None:
        stripped = line.strip()
        if not stripped:
            return None

        fields = parse_logfmt_line(stripped)
        if not fields:
            return None

        timestamp = _first_present(fields, _TIMESTAMP_KEYS)
        if timestamp is None:
            # Logfmt does not mandate a timestamp. Use the current time so
            # the event can still participate in time-based queries.
            from datetime import datetime, timezone

            timestamp = datetime.now(timezone.utc).isoformat()

        level = _first_present(fields, _LEVEL_KEYS) or "INFO"
        message = _first_present(fields, _MESSAGE_KEYS)
        service = _first_present(fields, _SERVICE_KEYS)

        if message is None:
            # No explicit message field: reconstruct one from the raw line.
            message = stripped

        # Everything not consumed by the well-known keys goes into extra.
        consumed = set(_TIMESTAMP_KEYS + _LEVEL_KEYS + _MESSAGE_KEYS + _SERVICE_KEYS)
        extra = {k: v for k, v in fields.items() if k not in consumed}

        try:
            return LogEvent.from_raw(
                {
                    "timestamp": timestamp,
                    "level": level,
                    "message": str(message),
                    "service": str(service) if service else None,
                    "extra": extra,
                }
            )
        except Exception:
            return None


def _first_present(fields: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in fields:
            return fields[key]
    return None