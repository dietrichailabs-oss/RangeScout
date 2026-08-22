"""Configurable multi-channel alerts with duplicate-spam suppression."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable


class AlertType(str, Enum):
    TRADE_HALT = "trade_halt"
    TRADE_RESUME = "trade_resume"
    SEC_FILING = "sec_filing"
    GOVERNMENT_CATALYST = "government_catalyst"
    WATCHLIST_NEWS = "watchlist_news"
    VOLUME_SPIKE = "volume_spike"
    VWAP_CROSS = "vwap_cross"
    OPENING_RANGE_BREAK = "opening_range_break"
    NEW_DAY_HIGH = "new_day_high"
    NEW_DAY_LOW = "new_day_low"
    STALE_STREAM = "stale_stream"
    PROVIDER_DISCONNECT = "provider_disconnect"
    PROVIDER_RECONNECT = "provider_reconnect"


@dataclass(frozen=True, slots=True)
class AlertPreferences:
    enabled: frozenset[AlertType] = frozenset(AlertType)
    visual: bool = True
    sound: bool = False
    desktop: bool = False
    duplicate_cooldown_seconds: int = 60


@dataclass(frozen=True, slots=True)
class AlertNotification:
    event_id: str
    alert_type: AlertType
    title: str
    message: str
    symbol: str | None
    occurred_at: datetime


class AlertDispatcher:
    def __init__(self, preferences: AlertPreferences, *, visual: Callable[[AlertNotification], None] | None = None, sound: Callable[[AlertNotification], None] | None = None, desktop: Callable[[AlertNotification], None] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.preferences = preferences
        self._visual, self._sound, self._desktop, self._clock = visual, sound, desktop, clock
        self._sent: dict[str, datetime] = {}

    def dispatch(self, notification: AlertNotification) -> bool:
        if notification.alert_type not in self.preferences.enabled:
            return False
        now = self._clock()
        previous = self._sent.get(notification.event_id)
        cooldown = timedelta(seconds=max(0, self.preferences.duplicate_cooldown_seconds))
        if previous is not None and now - previous < cooldown:
            return False
        self._sent[notification.event_id] = now
        self._prune(now, cooldown)
        if self.preferences.visual and self._visual: self._visual(notification)
        if self.preferences.sound and self._sound: self._sound(notification)
        if self.preferences.desktop and self._desktop: self._desktop(notification)
        return True

    def _prune(self, now: datetime, cooldown: timedelta) -> None:
        horizon = max(cooldown * 10, timedelta(hours=1))
        self._sent = {key: value for key, value in self._sent.items() if now - value <= horizon}
