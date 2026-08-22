"""High-level application services for refresh and report generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.domain.errors import DataQualityError
from app.models.schemas import (
    AlertEvent,
    DataDelay,
    DataFreshnessState,
    OhlcvBar,
    QuoteSnapshot,
)
from app.providers.base import MarketDataProvider
from app.analytics.calculations import (
    cumulative_range_position,
    drawdown_current,
    drawdown_maximum,
    moving_average,
    period_high,
    period_low,
    percentage_change,
    volume_average,
)
from app.analytics.analysis import Explanation, trend_explanations
from app.historical_store.repository import HistoricalStore


@dataclass(frozen=True)
class SymbolRangeReport:
    symbol: str
    period_label: str
    bars: list[OhlcvBar]
    quote: QuoteSnapshot
    provider_name: str
    quote_provider_id: str
    history_provider_id: str
    delay: DataDelay
    freshness: DataFreshnessState
    insights: list[Explanation]
    metrics: dict[str, Any]


def default_range_window(days: int = 365) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc), datetime.combine(end, datetime.min.time()).replace(tzinfo=timezone.utc)


def refresh_symbol_report(
    symbol: str,
    market_data: MarketDataProvider,
    store: HistoricalStore,
    range_days: int = 365,
) -> SymbolRangeReport:
    instrument = market_data.resolve_instrument(symbol)
    quote_result = market_data.fetch_quote(symbol)
    quote = quote_result.payload
    start, end = default_range_window(range_days)
    hist_result = market_data.fetch_historical(instrument.identifier, start=start, end=end)
    bars, actions = hist_result.payload
    winning_provider_id = hist_result.metadata.provider_id
    winning_provider_name = hist_result.metadata.provider_name
    winning_delay = hist_result.metadata.delay_label
    store.upsert_bars(bars, winning_provider_id)
    cached = store.get_bars(instrument.identifier, winning_provider_id, start=start.date(), end=end.date())

    metrics = _build_metrics(cached)
    explanations = trend_explanations(
        bars=cached,
        provider_name=winning_provider_name,
        range_label=f"{range_days}d",
        data_delay=winning_delay,
        freshness=DataFreshnessState.LIVE,
    )
    if actions:
        explanations.append(
            Explanation(
                text=f"Corporate actions present: {len(actions)} event(s).",
                computed_at=datetime.now(timezone.utc),
                details={"provider": winning_provider_name},
            )
        )
    return SymbolRangeReport(
        symbol=instrument.identifier.symbol,
        period_label=f"{range_days}d",
        bars=cached,
        quote=quote,
        provider_name=winning_provider_name,
        quote_provider_id=quote_result.metadata.provider_id,
        history_provider_id=winning_provider_id,
        delay=winning_delay,
        freshness=DataFreshnessState.LIVE,
        insights=explanations,
        metrics=metrics,
    )


def _build_metrics(bars: list[OhlcvBar]) -> dict[str, Decimal | tuple]:
    if not bars:
        raise DataQualityError("No bars to build analytics.")
    first = bars[0].open
    latest = bars[-1].close
    high, high_date = period_high(bars)
    low, low_date = period_low(bars)
    pct = percentage_change(bars)
    return {
        "first_open": first,
        "latest_close": latest,
        "period_high": high,
        "period_high_date": high_date,
        "period_low": low,
        "period_low_date": low_date,
        "range_position_pct": cumulative_range_position(bars),
        "pct_change": pct,
        "avg_volume": volume_average(bars),
        "max_drawdown_pct": drawdown_maximum(bars)[0],
        "current_drawdown_pct": drawdown_current(bars)[0],
        "ma_20_latest": moving_average(bars, 20)[-1][1] if len(bars) >= 20 else None,
        "ma_50_latest": moving_average(bars, 50)[-1][1] if len(bars) >= 50 else None,
        "ma_200_latest": moving_average(bars, 200)[-1][1] if len(bars) >= 200 else None,
    }
