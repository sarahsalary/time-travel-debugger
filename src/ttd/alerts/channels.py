"""Notification channels that deliver alerts to external services.

Every channel implements the ``NotificationChannel`` protocol: it takes an
``Alert`` and delivers it to a remote endpoint. Channels are intentionally
stateless and side-effect-free beyond the network call, so they can be
tested in isolation with a fake transport.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Protocol

from ..watch import Alert, AlertKind


class NotificationError(Exception):
    """Raised when a channel fails to deliver an alert."""


class NotificationChannel(Protocol):
    """Protocol implemented by every alert channel."""

    name: str

    def send(self, alert: Alert, *, context: dict[str, Any] | None = None) -> None:
        """Deliver ``alert`` to the remote service."""
        ...


# ---------------------------------------------------------------------------
# Base channel
# ---------------------------------------------------------------------------


class _HTTPChannel(ABC):
    """Common HTTP POST plumbing shared by webhook-based channels."""

    name: str = "http"
    timeout: float = 10.0

    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        if not webhook_url:
            raise ValueError(f"{type(self).__name__} requires a webhook URL")
        self.webhook_url = webhook_url
        self.timeout = timeout

    # -- public API --------------------------------------------------------

    def send(self, alert: Alert, *, context: dict[str, Any] | None = None) -> None:
        payload = self.build_payload(alert, context=context)
        self._post(payload)

    @abstractmethod
    def build_payload(
        self, alert: Alert, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return the JSON body to POST to the webhook."""

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ttd-alerts/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                if status >= 400:
                    body = response.read().decode("utf-8", "replace")
                    raise NotificationError(
                        f"{self.name} returned HTTP {status}: {body[:200]}"
                    )
        except urllib.error.HTTPError as exc:
            raise NotificationError(
                f"{self.name} returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise NotificationError(
                f"{self.name} unreachable: {exc.reason}"
            ) from exc


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------


_SEVERITY_COLORS = {
    "low": "#36a64f",       # green
    "medium": "#ffcc00",    # amber
    "high": "#e01e5a",      # red
}

_SEVERITY_EMOJI = {
    "low": ":information_source:",
    "medium": ":warning:",
    "high": ":rotating_light:",
}

_DISCORD_COLORS = {
    "low": 0x36A64F,
    "medium": 0xFFCC00,
    "high": 0xE01E5A,
}


def _severity(alert: Alert) -> str:
    return alert.severity if alert.severity in _SEVERITY_COLORS else "medium"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class SlackChannel(_HTTPChannel):
    """Send alerts to a Slack incoming webhook.

    Slack's Incoming Webhooks accept a JSON body with ``text`` and optional
    ``attachments``. We use attachments to add colour-coded severity and a
    structured field layout.
    """

    name = "slack"

    def build_payload(
        self, alert: Alert, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        severity = _severity(alert)
        fields = [
            {"title": "Kind", "value": alert.kind.value, "short": True},
            {"title": "Severity", "value": severity, "short": True},
            {
                "title": "Time",
                "value": alert.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "short": True,
            },
        ]
        for key, value in (alert.payload or {}).items():
            fields.append(
                {"title": key, "value": str(value), "short": True}
            )

        return {
            "text": f"{_SEVERITY_EMOJI[severity]} ttd alert",
            "attachments": [
                {
                    "color": _SEVERITY_COLORS[severity],
                    "title": alert.message,
                    "fields": fields,
                    "footer": context.get("source", "ttd") if context else "ttd",
                }
            ],
        }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class DiscordChannel(_HTTPChannel):
    """Send alerts to a Discord webhook using rich embeds."""

    name = "discord"

    def build_payload(
        self, alert: Alert, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        severity = _severity(alert)

        embed_fields = [
            {"name": "Kind", "value": alert.kind.value, "inline": True},
            {"name": "Severity", "value": severity, "inline": True},
        ]
        for key, value in (alert.payload or {}).items():
            embed_fields.append(
                {"name": key, "value": str(value)[:1024], "inline": True}
            )

        return {
            "embeds": [
                {
                    "title": f"ttd alert: {alert.kind.value}",
                    "description": alert.message,
                    "color": _DISCORD_COLORS[severity],
                    "fields": embed_fields,
                    "timestamp": alert.timestamp.isoformat(),
                    "footer": {
                        "text": context.get("source", "ttd") if context else "ttd"
                    },
                }
            ]
        }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramChannel(_HTTPChannel):
    """Send alerts via the Telegram Bot API.

    Telegram expects the bot token in the URL path and the chat ID in the
    JSON body. We build the full URL from ``bot_token`` and ``chat_id``.
    """

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramChannel requires bot_token and chat_id")
        self.bot_token = bot_token
        self.chat_id = chat_id
        # Telegram MarkdownV2 requires escaping; we use HTML instead for
        # simplicity and predictable behaviour.
        webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        super().__init__(webhook_url, timeout=timeout)

    def build_payload(
        self, alert: Alert, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        severity = _severity(alert)
        lines = [
            f"<b>ttd alert</b>",
            f"<b>Kind:</b> {_html_escape(alert.kind.value)}",
            f"<b>Severity:</b> {_html_escape(severity)}",
            f"<b>Time:</b> {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"<b>{_html_escape(alert.message)}</b>",
        ]
        for key, value in (alert.payload or {}).items():
            lines.append(f"<b>{_html_escape(str(key))}:</b> {_html_escape(str(value))}")

        return {
            "chat_id": self.chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Generic webhook
# ---------------------------------------------------------------------------


class GenericWebhookChannel(_HTTPChannel):
    """Send the raw alert as JSON to any HTTP endpoint.

    This is the escape hatch for integrating with services that are not
    covered by a dedicated channel: PagerDuty, Opsgenie, n8n, Zapier, or a
    custom internal endpoint.
    """

    name = "webhook"

    def build_payload(
        self, alert: Alert, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "source": "ttd",
            "kind": alert.kind.value,
            "severity": alert.severity,
            "timestamp": alert.timestamp.isoformat(),
            "message": alert.message,
            "payload": alert.payload or {},
            "context": context or {},
        }