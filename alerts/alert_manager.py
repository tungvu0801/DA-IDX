"""
alerts/alert_manager.py — Alert evaluation and dispatch.

This module decides which computed signals are "alert-worthy" and sends
them through one or more AlertChannel implementations. Only a console
channel is implemented for this first version; the AlertChannel interface
is intentionally small so Discord/email/desktop-notification channels can
be added later without touching the rest of the agent.

Alerts here are informational only. Nothing in this module (or anywhere
else in this project) places trades — it only prints/logs notable signals.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional

from analysis.indicators import TickerMetrics

logger = logging.getLogger(__name__)

# Signals considered notable enough to raise an alert. Edit this set to
# change what triggers an alert without touching the signal engine itself.
ALERT_WORTHY_SIGNALS = {
    "STRONG MOMENTUM",
    "UNUSUAL VOLUME",
    "BREAKOUT WATCH",
    "LARGE GAIN",
    "LARGE DROP",
}


@dataclass
class Alert:
    """A single alert event, ready to be formatted by any AlertChannel."""

    symbol: str
    signal: str
    pct_change: float
    price: float
    attention_score: int

    def message(self) -> str:
        sign = "+" if self.pct_change >= 0 else ""
        return (
            f"[ALERT] {self.symbol}: {self.signal} "
            f"(price ${self.price:,.2f}, {sign}{self.pct_change:.2f}%, "
            f"attention {self.attention_score}/100)"
        )


class AlertChannel(ABC):
    """Base interface for anything that can deliver an Alert somewhere."""

    @abstractmethod
    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class ConsoleAlertChannel(AlertChannel):
    """Prints alerts to the terminal. The only channel enabled by default."""

    def send(self, alert: Alert) -> None:
        print(alert.message())


# ---------------------------------------------------------------------------
# Future channels (not implemented yet) — kept here as a clear extension
# point. To enable one, implement `send()` and add an instance to the list
# passed into AlertManager in main.py.
# ---------------------------------------------------------------------------
class DiscordAlertChannel(AlertChannel):
    """Placeholder for posting alerts to a Discord webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, alert: Alert) -> None:
        raise NotImplementedError(
            "DiscordAlertChannel is not implemented yet. Add a webhook POST "
            "here (e.g. using `requests`) when you're ready to enable it."
        )


class EmailAlertChannel(AlertChannel):
    """Placeholder for emailing alerts."""

    def __init__(self, smtp_host: str, smtp_port: int, from_addr: str, to_addr: str):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_addr = from_addr
        self.to_addr = to_addr

    def send(self, alert: Alert) -> None:
        raise NotImplementedError(
            "EmailAlertChannel is not implemented yet. Wire up smtplib here "
            "when you're ready to enable it."
        )


class DesktopNotificationChannel(AlertChannel):
    """Placeholder for native OS desktop notifications."""

    def send(self, alert: Alert) -> None:
        raise NotImplementedError(
            "DesktopNotificationChannel is not implemented yet. Wire up a "
            "library such as `plyer` or `win10toast` here when ready."
        )


class AlertManager:
    """Evaluates metrics against ALERT_WORTHY_SIGNALS and fans out to channels."""

    def __init__(self, channels: Optional[Iterable[AlertChannel]] = None):
        self.channels: List[AlertChannel] = list(channels) if channels else [ConsoleAlertChannel()]

    def build_alerts(self, metrics: Iterable[TickerMetrics]) -> List[Alert]:
        alerts = []
        for m in metrics:
            if m.signal in ALERT_WORTHY_SIGNALS:
                alerts.append(
                    Alert(
                        symbol=m.symbol,
                        signal=m.signal,
                        pct_change=m.pct_change,
                        price=m.price,
                        attention_score=m.attention_score,
                    )
                )
        return alerts

    def dispatch(self, metrics: Iterable[TickerMetrics]) -> List[Alert]:
        """Evaluate metrics for alert-worthy signals and send through all channels."""
        alerts = self.build_alerts(metrics)
        for alert in alerts:
            for channel in self.channels:
                try:
                    channel.send(alert)
                except NotImplementedError:
                    logger.debug("Alert channel %s is not implemented yet; skipping.", channel)
                except Exception as exc:  # noqa: BLE001 - a broken channel must not crash the agent
                    logger.warning("Alert channel %s failed to send: %s", channel, exc)
        return alerts
