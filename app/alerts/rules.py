"""Alert rule evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from app.models.schemas import AlertEvent, OhlcvBar
from app.analytics.calculations import percentage_change, cumulative_range_position, drawdown_current
from app.models.schemas import InstrumentIdentifier


@dataclass(frozen=True)
class AlertRule:
    id: str
    symbol: str
    mode: str
    threshold: Decimal


def evaluate_alerts(rules: Sequence[AlertRule], bars_by_symbol: dict[str, list[OhlcvBar]]) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    now = datetime.now(timezone.utc)
    for rule in rules:
        bars = bars_by_symbol.get(rule.symbol, [])
        if not bars:
            if rule.mode == "stale_data":
                events.append(
                    AlertEvent(
                        alert_id=rule.id,
                        instrument=InstrumentIdentifier(symbol=rule.symbol),
                        message=f"{rule.symbol} has no data for stale-data alert.",
                        triggered_at=now,
                        severity="warn",
                    )
                )
            continue
        if rule.mode == "percent_change":
            pct = percentage_change(bars)
            if abs(pct) >= rule.threshold:
                events.append(
                    AlertEvent(
                        alert_id=rule.id,
                        instrument=bars[0].instrument,
                        message=f"{rule.symbol} percent change {pct:.2f}% vs threshold {rule.threshold}%.",
                        triggered_at=now,
                    )
                )
        elif rule.mode == "relative_to_high":
            pos = cumulative_range_position(bars)
            if pos >= rule.threshold:
                events.append(
                    AlertEvent(
                        alert_id=rule.id,
                        instrument=bars[0].instrument,
                        message=f"{rule.symbol} is {pos:.2f}% within high-low range.",
                        triggered_at=now,
                    )
                )
        elif rule.mode == "drawdown":
            dd, _ = drawdown_current(bars)
            if abs(dd) >= rule.threshold:
                events.append(
                    AlertEvent(
                        alert_id=rule.id,
                        instrument=bars[0].instrument,
                        message=f"{rule.symbol} drawdown {dd:.2f}% threshold {rule.threshold}%.",
                        triggered_at=now,
                    )
                )
        elif rule.mode == "stale_data":
            last_ts = bars[-1].date
            if (datetime.now(timezone.utc).date() - last_ts).days > int(rule.threshold):
                events.append(
                    AlertEvent(
                        alert_id=rule.id,
                        instrument=bars[0].instrument,
                        message=f"{rule.symbol} stale by {(datetime.now(timezone.utc).date() - last_ts).days} days.",
                        triggered_at=now,
                        severity="warn",
                    )
                )
    return events
