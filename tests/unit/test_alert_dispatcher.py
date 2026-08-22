from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.alerts.dispatcher import AlertDispatcher, AlertNotification, AlertPreferences, AlertType


def test_every_required_alert_type_is_available() -> None:
    assert {value.value for value in AlertType} == {"trade_halt", "trade_resume", "sec_filing", "government_catalyst", "watchlist_news", "volume_spike", "vwap_cross", "opening_range_break", "new_day_high", "new_day_low", "stale_stream", "provider_disconnect", "provider_reconnect"}


def test_channels_and_duplicate_suppression() -> None:
    now = [datetime(2026, 8, 17, tzinfo=timezone.utc)]
    visual, sound, desktop = [], [], []
    dispatcher = AlertDispatcher(AlertPreferences(sound=True, desktop=True), visual=visual.append, sound=sound.append, desktop=desktop.append, clock=lambda: now[0])
    notice = AlertNotification("same-event", AlertType.TRADE_HALT, "HALT", "ACME halted", "ACME", now[0])
    assert dispatcher.dispatch(notice) is True
    assert dispatcher.dispatch(notice) is False
    assert len(visual) == len(sound) == len(desktop) == 1
    now[0] += timedelta(seconds=61)
    assert dispatcher.dispatch(notice) is True


def test_disabled_type_does_not_emit() -> None:
    sent = []
    prefs = AlertPreferences(enabled=frozenset({AlertType.SEC_FILING}))
    dispatcher = AlertDispatcher(prefs, visual=sent.append)
    notice = AlertNotification("x", AlertType.TRADE_HALT, "HALT", "halted", "X", datetime.now(timezone.utc))
    assert dispatcher.dispatch(notice) is False
    assert sent == []
