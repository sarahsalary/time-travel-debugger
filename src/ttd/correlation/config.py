"""Load correlation rules from a TOML file.

Example ``correlations.toml``::

    [[correlations]]
    type = "sequence"
    name = "bruteforce-then-success"
    steps = [
        "message ~ 'login failed'",
        "message ~ 'login failed'",
        "message ~ 'login failed'",
        "message ~ 'login success'",
    ]
    timeframe_seconds = 300
    severity = "high"

    [[correlations]]
    type = "threshold"
    name = "error-burst-per-service"
    query = "level=ERROR"
    threshold = 10
    timeframe_seconds = 60
    group_by = "service"
    severity = "high"

    [[correlations]]
    type = "window"
    name = "oom-per-minute"
    query = "message ~ 'out of memory'"
    threshold = 3
    bucket_seconds = 60
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
    CorrelationRule,
    SequenceRule,
    ThresholdCorrelationRule,
    WindowRule,
)


def default_correlations_path() -> Path:
    override = os.environ.get("TTD_CORRELATIONS_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ttd" / "correlations.toml"


_FACTORIES = {
    "sequence": SequenceRule,
    "threshold": ThresholdCorrelationRule,
    "window": WindowRule,
}


def load_correlation_rules(path: Path | None = None) -> list[CorrelationRule]:
    path = path or default_correlations_path()
    if not path.exists():
        return []

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Could not parse {path}: {exc}") from exc

    rules: list[CorrelationRule] = []
    for spec in raw.get("correlations", []):
        if not isinstance(spec, dict):
            continue
        rule_type = spec.pop("type", None)
        if rule_type not in _FACTORIES:
            raise ValueError(
                f"Unknown correlation type {rule_type!r} in {path}"
            )
        try:
            rules.append(_FACTORIES[rule_type](**spec))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid correlation rule ({rule_type}): {exc}"
            ) from exc
    return rules