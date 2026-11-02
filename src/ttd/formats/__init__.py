"""Pluggable log format parsers.

Each format lives in its own module and implements the ``FormatParser``
interface. The ``detect_format`` helper picks the right parser by looking
at a small sample of the file, and ``open_log_file`` returns an object
whose ``stream()`` method yields ``LogEvent`` instances.
"""

from .base import FormatParser, detect_format, open_log_file
from .logfmt import LogfmtParser
from .syslog import SyslogParser
from .csv_format import CSVParser
from .evtx_format import EVTXParser

__all__ = [
    "FormatParser",
    "LogfmtParser",
    "SyslogParser",
    "CSVParser",
    "EVTXParser",
    "detect_format",
    "open_log_file",
]