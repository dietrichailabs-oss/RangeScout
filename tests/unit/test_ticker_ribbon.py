from __future__ import annotations

import json

from app.configuration.settings import AppSettings, load_user_settings, save_user_settings
from app.streaming.ticker import plan_ticker_subscriptions


def test_subscription_plan_dedupes_and_degrades_at_provider_limit() -> None:
    plan = plan_ticker_subscriptions(["aapl", "MSFT", "AAPL", "NVDA"], 2)
    assert plan.subscribed == ("AAPL", "MSFT")
    assert plan.overflow == ("NVDA",)
    assert plan.limit == 2


def test_ticker_position_persists_and_invalid_value_migrates(tmp_path) -> None:
    save_user_settings(str(tmp_path), AppSettings(ticker_position="bottom"))
    assert load_user_settings(str(tmp_path)).ticker_position == "bottom"
    path = tmp_path / "settings.json"
    payload = json.loads(path.read_text(encoding="utf-8")); payload["ticker_position"] = "sideways"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_user_settings(str(tmp_path)).ticker_position == "top"
