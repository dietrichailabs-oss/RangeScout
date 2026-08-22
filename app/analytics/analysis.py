"""Human-readable trend explanations built from deterministic computations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from app.analytics.calculations import cumulative_range_position, drawdown_current, percentage_change
from app.domain.errors import DataQualityError
from app.models.schemas import DataDelay, DataFreshnessState, OhlcvBar


@dataclass(frozen=True)
class Explanation:
    text: str
    computed_at: datetime
    details: dict


def trend_explanations(
    bars: Sequence[OhlcvBar],
    provider_name: str,
    range_label: str,
    data_delay: DataDelay,
    freshness: DataFreshnessState,
) -> list[Explanation]:
    out: list[Explanation] = []
    computed_at = datetime.now(timezone.utc)

    try:
        pct = percentage_change(bars)
        out.append(
            Explanation(
                text=f"Price moved {pct:.2f}% across the selected {range_label} range.",
                computed_at=computed_at,
                details={"provider": provider_name, "range": range_label, "formula": "((latest close - first open)/first open)*100"},
            )
        )
        position = cumulative_range_position(bars)
        out.append(
            Explanation(
                text=f"Latest close is {position:.2f}% through the historical high-low band.",
                computed_at=computed_at,
                details={"provider": provider_name, "formula": "(latest-high_low)/(high-low)*100"},
            )
        )
        current_dd, date = drawdown_current(bars)
        out.append(
            Explanation(
                text=f"Current drawdown from latest peak is {current_dd:.2f}% (as of {date}).",
                computed_at=computed_at,
                details={"provider": provider_name, "formula": "(latest-peak)/peak"},
            )
        )
    except DataQualityError as exc:
        out.append(
            Explanation(
                text=f"Insufficient data for full trend explanation: {exc}",
                computed_at=computed_at,
                details={"provider": provider_name},
            )
        )

    delay_msg = "live"
    if data_delay.name != "REALTIME":
        delay_msg = f"delayed ({data_delay.value})"
    out.append(
        Explanation(
            text=f"Data state: {freshness.value}, freshness delay: {delay_msg}.",
            computed_at=computed_at,
            details={"provider": provider_name, "state": freshness.value, "delay": data_delay.value},
        )
    )
    return out
