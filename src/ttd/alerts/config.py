"""Configuration parsing for alert channels.

Configuration is read from a TOML file so users can keep secrets in
environment variables and channel definitions in a version-controlled
config. The default location is ``~/.config/ttd/alerts.toml`` or the path
given by ``TTD_ALERT_CONFIG``.

Example ``alerts.toml``::

    [channels.slack]
    type = "slack"
    webhook_url = "${SLACK_WEBHOOK_URL}"

    [channels.discord]
    type = "discord"
    webhook_url = "${DISCORD_WEBHOOK_URL}"

    [channels.telegram]
    type = "telegram"
    bot_token = "${TG_TOKEN}"
    chat_id = "${TG_CHAT_ID}"

    [channels.ops]
    type = "webhook"
    webhook_url = "https://internal.example/alerts"
"""

from __future__ import annotations

import os
import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channels import (
    DiscordChannel,
    GenericWebhookChannel,
    NotificationChannel,
    SlackChannel,
    TelegramChannel,
)


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` references with environment values."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def default_config_path() -> Path:
    override = os.environ.get("TTD_ALERT_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ttd" / "alerts.toml"


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


@dataclass
class AlertConfig:
    """A parsed alert configuration."""

    channels: list[NotificationChannel] = field(default_factory=list)
    min_severity: str = "low"
    include_kinds: list[str] = field(default_factory=list)
    exclude_kinds: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.channels


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

_CHANNEL_FACTORIES: dict[str, type] = {
    "slack": SlackChannel,
    "discord": DiscordChannel,
    "telegram": TelegramChannel,
    "webhook": GenericWebhookChannel,
    "generic": GenericWebhookChannel,
}


def load_alert_config(path: Path | None = None) -> AlertConfig:
    """Load alert channels from a TOML file.

    Returns an empty ``AlertConfig`` if the file does not exist, so callers
    can simply check ``config.is_empty()`` without special-casing missing
    files.
    """
    path = path or default_config_path()
    if not path.exists():
        return AlertConfig()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Could not parse {path}: {exc}") from exc

    raw = _expand_env(raw)

    channels: list[NotificationChannel] = []
    for name, spec in (raw.get("channels") or {}).items():
        if not isinstance(spec, dict):
            continue
        channel_type = spec.pop("type", name)
        factory = _CHANNEL_FACTORIES.get(channel_type)
        if factory is None:
            raise ValueError(
                f"Unknown channel type {channel_type!r} in {path}"
            )
        try:
            channels.append(factory(**spec))
        except TypeError as exc:
            raise ValueError(
                f"Invalid configuration for channel {name!r}: {exc}"
            ) from exc

    defaults = raw.get("defaults") or {}
    return AlertConfig(
        channels=channels,
        min_severity=defaults.get("min_severity", "low"),
        include_kinds=list(defaults.get("include_kinds", [])),
        exclude_kinds=list(defaults.get("exclude_kinds", [])),
    )


def severity_passes(actual: str, minimum: str) -> bool:
    """Return True if ``actual`` meets or exceeds ``minimum``."""
    return _SEVERITY_RANK.get(actual, 0) >= _SEVERITY_RANK.get(minimum, 0)