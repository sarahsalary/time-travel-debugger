"""Parser for Windows Event Log (EVTX) files.

Windows stores its event logs in a proprietary binary format with a
``.evtx`` extension. The ``python-evtx`` library parses these files on
any platform without needing a Windows host, which is what lets ``ttd``
run on Linux and macOS against logs exported from Windows.

The parser extracts the fields ``ttd`` cares about — ``EventID``,
``TimeCreated``, ``Level``, ``Provider``, and ``Channel`` — and stuffs
the rest of the event data into ``extra``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

from ..models import LogEvent
from .base import FormatParser


# Windows Event Log XML namespace.
_EVENT_NS = "http://schemas.microsoft.com/win/2004/08/events/event"
_NS = {"ns": _EVENT_NS}


# ---------------------------------------------------------------------------
# Level mapping
# ---------------------------------------------------------------------------
#
# Windows uses numeric levels 0-5 in the <Level> element of the System
# block. The mapping below follows the convention used by Wazuh and other
# SIEM normalisers.

_WINDOWS_LEVEL_MAP: dict[int, str] = {
    0: "CRITICAL",   # LogAlways
    1: "CRITICAL",   # Critical
    2: "ERROR",      # Error
    3: "WARNING",    # Warning
    4: "INFO",       # Information
    5: "DEBUG",      # Verbose
}


def _map_level(value: Any) -> str:
    if value is None:
        return "INFO"
    try:
        code = int(value)
    except (TypeError, ValueError):
        # Some providers emit a textual level.
        return {
            "CRITICAL": "CRITICAL",
            "ERROR": "ERROR",
            "WARNING": "WARNING",
            "INFORMATION": "INFO",
            "INFO": "INFO",
            "VERBOSE": "DEBUG",
            "DEBUG": "DEBUG",
        }.get(str(value).upper(), "INFO")
    return _WINDOWS_LEVEL_MAP.get(code, "INFO")


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def _parse_time_created(value: str | None) -> datetime | None:
    """Parse a ``SystemTime`` attribute into a datetime.

    Windows emits timestamps as RFC 3339 strings with a trailing ``Z``.
    """
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Windows emits very high-precision fractions that Python drops; that
    # is fine for our purposes.
    return dt


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class EVTXParser(FormatParser):
    """Parser for Windows EVTX event logs.

    Requires the optional ``python-evtx`` dependency. Install with
    ``pip install 'ttd[evtx]'``.
    """

    name = "evtx"
    description = "Windows Event Log (EVTX)"

    def can_parse(self, sample: str) -> bool:
        """EVTX is binary, so the file extension is the only reliable signal.

        ``can_parse`` is never called by ``detect_format`` for EVTX files
        (that path uses ``_looks_like_evtx`` instead), but we keep it
        returning False so no text sample accidentally matches.
        """
        return False

    def parse_line(self, line: str) -> LogEvent | None:
        return None

    # -- file-level parsing ------------------------------------------------

    def parse_file(self, path):  # type: ignore[override]
        """Parse every record in an EVTX file."""
        try:
            import Evtx.Evtx as evtx  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "EVTX parsing requires the 'python-evtx' package. "
                "Install it with: pip install 'ttd[evtx]'"
            ) from exc

        with evtx.Evtx(str(path)) as log:
            for record in log.records():
                event = self._record_to_event(record)
                if event is not None:
                    yield event

    # -- record conversion -------------------------------------------------

    def _record_to_event(self, record) -> LogEvent | None:
        try:
            xml = record.xml()
        except Exception:
            return None
        if not xml:
            return None

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None

        system = root.find("ns:System", _NS)
        if system is None:
            return None

        event_id_el = system.find("ns:EventID", _NS)
        event_id = event_id_el.text if event_id_el is not None else None

        time_created_el = system.find("ns:TimeCreated", _NS)
        system_time = (
            time_created_el.get("SystemTime") if time_created_el is not None else None
        )
        ts = _parse_time_created(system_time)
        if ts is None:
            ts = datetime.now(timezone.utc)

        level_el = system.find("ns:Level", _NS)
        level = _map_level(level_el.text if level_el is not None else None)

        provider_el = system.find("ns:Provider", _NS)
        provider = provider_el.get("Name") if provider_el is not None else None

        channel_el = system.find("ns:Channel", _NS)
        channel = channel_el.text if channel_el is not None else None

        computer_el = system.find("ns:Computer", _NS)
        computer = computer_el.text if computer_el is not None else None

        # Pull EventData pairs into a dictionary.
        event_data: dict[str, Any] = {}
        for data_el in root.findall(".//ns:EventData/ns:Data", _NS):
            name = data_el.get("Name")
            if name:
                event_data[name] = data_el.text

        # Also honour UserData (used by some providers).
        for user_data in root.findall(".//ns:UserData", _NS):
            for child in list(user_data):
                tag = _strip_ns(child.tag)
                if child.text and child.text.strip():
                    event_data.setdefault(tag, child.text.strip())

        # Build a human-readable message.
        message = self._compose_message(
            event_id=event_id,
            provider=provider,
            channel=channel,
            event_data=event_data,
        )

        extra: dict[str, Any] = {
            "evtx.event_id": int(event_id) if event_id and event_id.isdigit() else event_id,
            "evtx.provider": provider,
            "evtx.channel": channel,
            "evtx.computer": computer,
        }
        for key, value in event_data.items():
            extra[f"evtx.{key}"] = value

        try:
            return LogEvent.from_raw(
                {
                    "timestamp": ts.isoformat(),
                    "level": level,
                    "message": message,
                    "service": provider or channel,
                    "extra": {k: v for k, v in extra.items() if v is not None},
                }
            )
        except Exception:
            return None

    @staticmethod
    def _compose_message(
        *,
        event_id: str | None,
        provider: str | None,
        channel: str | None,
        event_data: dict[str, Any],
    ) -> str:
        """Build a compact one-line summary of an event."""
        parts = []
        if provider:
            parts.append(provider)
        if event_id:
            parts.append(f"EventID={event_id}")
        if channel:
            parts.append(f"Channel={channel}")

        header = " ".join(parts) if parts else "Windows event"

        # Append a short selection of high-signal EventData fields.
        highlights = []
        for key in (
            "TargetUserName", "SubjectUserName", "LogonType", "IpAddress",
            "ProcessName", "CommandLine", "ServiceName", "ImagePath",
        ):
            if key in event_data and event_data[key]:
                highlights.append(f"{key}={event_data[key]}")

        if highlights:
            return f"{header} | " + " ".join(highlights[:4])
        return header


def _strip_ns(tag: str) -> str:
    """Remove the ``{namespace}`` prefix from an ElementTree tag."""
    return tag.split("}", 1)[-1] if "}" in tag else tag