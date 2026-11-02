"""Load alert rules from a TOML configuration file.

Example ``rules.toml``::

    [[rules]]
    type = "threshold"
    name = "slow-requests"
    field = "duration_ms"
    gt = 5000
    min_occurrences = 3
    severity = "high"

    [[rules]]
    type = "frequency"
    name = "error-burst"
    num_events = 10
    timeframe_seconds = 60
    query = "level=ERROR"
    severity = "high"

    [[rules]]
    type = "pattern"
    name = "oom-killer"
    pattern = "out of memory"
    min_occurrences = 1
    severity = "high"
"""

from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path

from .rules import (
    AlertRule,
    FrequencyRule,
    PatternRule,
    ThresholdRule,
)


def default_rules_path() -> Path:
    override = os.environ.get("TTD_RULES_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ttd" / "rules.toml"


_FACTORIES = {
    "threshold": ThresholdRule,
    "frequency": FrequencyRule,
    "pattern": PatternRule,
}


def load_rules(path: Path | None = None) -> list[AlertRule]:
    """Load every rule defined in a TOML file.

    Returns an empty list if the file does not exist so callers do not
    need to special-case a missing config.
    """
    path = path or default_rules_path()
    if not path.exists():
        return []

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Could not parse {path}: {exc}") from exc

    rules: list[AlertRule] = []
    for spec in raw.get("rules", []):
        if not isinstance(spec, dict):
            continue
        rule_type = spec.pop("type", None)
        if rule_type not in _FACTORIES:
            raise ValueError(
                f"Unknown rule type {rule_type!r} in {path}"
            )
        factory = _FACTORIES[rule_type]
        try:
            rules.append(factory(**spec))
        except TypeError as exc:
            raise ValueError(
                f"Invalid rule configuration ({rule_type}): {exc}"
            ) from exc
    return rules