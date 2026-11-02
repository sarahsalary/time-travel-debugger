"""Parser for syslog messages in RFC 3164 and RFC 5424 form.

RFC 3164 (BSD syslog) is the older format, still emitted by many Unix
daemons::

    <PRI>MMM DD HH:MM:SS HOSTNAME TAG: MESSAGE

RFC 5424 (IETF syslog) is the newer, structured format::

    <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG

Both formats begin with a priority value wrapped in angle brackets. The
priority encodes the facility (0-23) and severity (0-7) as
``facility * 8 + severity``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import LogEvent
from .base import FormatParser


# ---------------------------------------------------------------------------
# Priority / severity tables
# ---------------------------------------------------------------------------

# RFC 5424 §6.2.1
_SEVERITY_NAMES = [
    "EMERGENCY",
    "ALERT",
    "CRITICAL",
    "ERROR",
    "WARNING",
    "NOTICE",
    "INFO",
    "DEBUG",
]

# Map syslog severities onto the levels ttd uses elsewhere.
_SEVERITY_TO_LEVEL = {
    0: "CRITICAL",
    1: "CRITICAL",
    2: "CRITICAL",
    3: "ERROR",
    4: "WARNING",
    5: "INFO",
    6: "INFO",
    7: "DEBUG",
}

_FACILITY_NAMES = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "ntp", "audit", "alert", "clock",
    "local0", "local1", "local2", "local3", "local4", "local5", "local6",
    "local7",
]


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP PROCID MSGID SD MSG
_RFC5424_RE = re.compile(
    r"""
    ^<(?P<pri>\d{1,3})>
    (?P<version>\d{1,2})
    \s
    (?P<timestamp>\S+)
    \s
    (?P<hostname>\S+)
    \s
    (?P<app>\S+)
    \s
    (?P<procid>\S+)
    \s
    (?P<msgid>\S+)
    \s
    (?P<sd>(?:-|\[.*?\]))
    (?:\s(?P<msg>.*))?
    $
    """,
    re.VERBOSE | re.DOTALL,
)

# RFC 3164: <PRI>MMM DD HH:MM:SS HOSTNAME TAG: MSG
_RFC3164_RE = re.compile(
    r"""
    ^<(?P<pri>\d{1,3})>
    (?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})
    \s+
    (?P<hostname>\S+)
    \s+
    (?P<tag>[^:]+?)(?:\[(?P<procid>\d+)\])?:
    \s*
    (?P<msg>.*)
    $
    """,
    re.VERBOSE | re.DOTALL,
)

# A bare PRI at the very start, used by can_parse.
_PRI_RE = re.compile(r"^<\d{1,3}>")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def decode_priority(pri: int) -> tuple[int, int]:
    """Return ``(facility, severity)`` from a raw PRI value."""
    facility = pri // 8
    severity = pri % 8
    return facility, severity


def _severity_to_level(severity: int) -> str:
    return _SEVERITY_TO_LEVEL.get(severity, "INFO")


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an RFC 5424 timestamp (RFC 3339)."""
    if value in ("-", ""):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            from dateutil import parser as dateparser

            dt = dateparser.parse(value)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_bsd_timestamp(value: str, *, year: int | None = None) -> datetime | None:
    """Parse an RFC 3164 timestamp like ``Oct 26 14:30:00``.

    BSD syslog omits the year, so we default to the current year. If the
    resulting timestamp is in the future by more than a day, we subtract a
    year to keep events in the past, which is what an operator expects.
    """
    match = re.match(
        r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})",
        value,
    )
    if not match:
        return None
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return None

    now = datetime.now(timezone.utc)
    chosen_year = year if year is not None else now.year

    try:
        dt = datetime(
            chosen_year,
            month,
            int(match.group("day")),
            int(match.group("h")),
            int(match.group("m")),
            int(match.group("s")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None

    # Guard against future dates caused by missing year information.
    if year is None and dt - now > __import__("datetime").timedelta(days=1):
        try:
            dt = dt.replace(year=chosen_year - 1)
        except ValueError:
            pass
    return dt


def _parse_structured_data(raw: str) -> dict[str, object]:
    """Parse the RFC 5424 STRUCTURED-DATA field into a nested dict.

    The format is a sequence of ``[SD-ID param="value" ...]`` elements.
    Unknown shapes are returned under a ``_raw`` key so no information is
    lost.
    """
    if raw == "-" or not raw:
        return {}

    result: dict[str, object] = {}
    # Match each [SD-ID ...] block.
    for block in re.finditer(r"\[([^\[\]]*)\]", raw):
        content = block.group(1)
        parts = content.split(" ", 1)
        sd_id = parts[0]
        params: dict[str, str] = {}
        if len(parts) > 1:
            for m in re.finditer(r'([A-Za-z0-9_.\-@]+)="((?:[^"\\]|\\.)*)"', parts[1]):
                params[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        result[sd_id] = params

    if not result:
        result["_raw"] = raw
    return result


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class SyslogParser(FormatParser):
    """Parser for RFC 3164 and RFC 5424 syslog messages.

    The parser tries RFC 5424 first (it is more structured), and falls back
    to RFC 3164. This mirrors the behaviour of established tools such as
    syslog-ng.
    """

    name = "syslog"
    description = "Syslog (RFC 3164 BSD and RFC 5424 IETF)"

    def can_parse(self, sample: str) -> bool:
        if not sample or not _PRI_RE.match(sample):
            return False
        return bool(
            _RFC5424_RE.match(sample) or _RFC3164_RE.match(sample)
        )

    def parse_line(self, line: str) -> LogEvent | None:
        stripped = line.strip()
        if not stripped or not _PRI_RE.match(stripped):
            return None

        match = _RFC5424_RE.match(stripped)
        if match:
            return self._parse_rfc5424(match)

        match = _RFC3164_RE.match(stripped)
        if match:
            return self._parse_rfc3164(match)

        return None

    # -- RFC 5424 ----------------------------------------------------------

    def _parse_rfc5424(self, match: re.Match[str]) -> LogEvent | None:
        pri = int(match.group("pri"))
        facility, severity = decode_priority(pri)
        ts = _parse_timestamp(match.group("timestamp"))
        if ts is None:
            return None

        structured = _parse_structured_data(match.group("sd"))
        app = _nil_to_none(match.group("app"))
        hostname = _nil_to_none(match.group("hostname"))
        procid = _nil_to_none(match.group("procid"))
        msgid = _nil_to_none(match.group("msgid"))
        version = match.group("version")

        message = match.group("msg")
        if message is None or message == "":
            message = msgid or app or "syslog"

        extra: dict[str, object] = {
            "syslog.facility": _FACILITY_NAMES[facility] if facility < len(_FACILITY_NAMES) else facility,
            "syslog.facility_code": facility,
            "syslog.severity": _SEVERITY_NAMES[severity],
            "syslog.severity_code": severity,
            "syslog.version": int(version),
            "syslog.hostname": hostname,
            "syslog.app": app,
            "syslog.procid": procid,
            "syslog.msgid": msgid,
        }
        if structured:
            extra["syslog.structured_data"] = structured

        # Flatten a few well-known structured data fields for convenience.
        for sd_id, params in structured.items():
            if isinstance(params, dict):
                for key, value in params.items():
                    extra.setdefault(f"{sd_id}.{key}", value)

        return LogEvent.from_raw(
            {
                "timestamp": ts.isoformat(),
                "level": _severity_to_level(severity),
                "message": message,
                "service": app,
                "extra": {k: v for k, v in extra.items() if v is not None},
            }
        )

    # -- RFC 3164 ----------------------------------------------------------

    def _parse_rfc3164(self, match: re.Match[str]) -> LogEvent | None:
        pri = int(match.group("pri"))
        facility, severity = decode_priority(pri)

        ts = _parse_bsd_timestamp(match.group("timestamp"))
        if ts is None:
            return None

        hostname = match.group("hostname")
        tag = match.group("tag").strip()
        procid = match.group("procid")
        message = match.group("msg") or ""

        extra: dict[str, object] = {
            "syslog.facility": _FACILITY_NAMES[facility] if facility < len(_FACILITY_NAMES) else facility,
            "syslog.facility_code": facility,
            "syslog.severity": _SEVERITY_NAMES[severity],
            "syslog.severity_code": severity,
            "syslog.hostname": hostname,
            "syslog.tag": tag,
        }
        if procid:
            extra["syslog.procid"] = int(procid)

        return LogEvent.from_raw(
            {
                "timestamp": ts.isoformat(),
                "level": _severity_to_level(severity),
                "message": message,
                "service": tag or None,
                "extra": extra,
            }
        )


def _nil_to_none(value: str | None) -> str | None:
    if value is None or value == "-":
        return None
    return value