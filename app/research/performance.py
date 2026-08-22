from __future__ import annotations

from app.research.financial_health import _ratio
from app.research.models import ResearchValue
from app.models.schemas import OhlcvBar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import pstdev


def build_performance(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    return {
        "Price performance": ResearchValue.unavailable("Market data", "Load a compatible market-price history for performance analytics."),
        "Net margin": _ratio(values["Net income"], values["Revenue"], "net margin"),
        "Return on assets": _ratio(values["Net income"], values["Assets"], "return on assets"),
    }


def calculate_price_performance(bars: list[OhlcvBar], *, as_of: date | None = None) -> dict[str, ResearchValue]:
    ordered = sorted(bars, key=lambda bar: bar.date)
    if not ordered:
        return {"Price performance": ResearchValue.unavailable("Market history", "No historical bars are loaded.")}
    as_of = as_of or ordered[-1].date
    latest = ordered[-1]
    source = latest.provider
    now = datetime.now(timezone.utc)

    def period_return(label: str, cutoff: date) -> ResearchValue:
        eligible = [bar for bar in ordered if bar.date <= cutoff]
        if not eligible or eligible[-1].close == 0:
            return ResearchValue.unavailable(source, f"Insufficient history for {label} performance.")
        start = eligible[-1]
        return ResearchValue(
            (latest.close - start.close) / start.close * Decimal(100),
            source,
            period=f"{start.date.isoformat()} to {latest.date.isoformat()}",
            units="percent",
            calculated_at=now,
            selection_reason=f"Close-to-close {label} return from locally cached provider bars.",
        )

    starts = {
        "1M": as_of - timedelta(days=30),
        "3M": as_of - timedelta(days=91),
        "YTD": date(as_of.year, 1, 1),
        "1Y": as_of - timedelta(days=365),
        "3Y": as_of - timedelta(days=365 * 3),
        "5Y": as_of - timedelta(days=365 * 5),
        "Max": ordered[0].date,
    }
    result = {label: period_return(label, cutoff) for label, cutoff in starts.items()}
    peak = ordered[0].close
    max_drawdown = Decimal(0)
    returns: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        peak = max(peak, current.close)
        if peak:
            max_drawdown = min(max_drawdown, (current.close - peak) / peak * Decimal(100))
        if previous.close:
            returns.append(float((current.close - previous.close) / previous.close))
    result["Maximum drawdown"] = ResearchValue(
        max_drawdown, source, period=f"{ordered[0].date.isoformat()} to {latest.date.isoformat()}", units="percent",
        calculated_at=now, selection_reason="Largest peak-to-trough close drawdown in loaded history.",
    )
    if len(returns) >= 2:
        result["Annualized volatility"] = ResearchValue(
            Decimal(str(pstdev(returns) * (252 ** 0.5) * 100)), source, period="loaded daily history", units="percent",
            calculated_at=now, selection_reason="Population standard deviation of daily close returns annualized by square root of 252.",
        )
    else:
        result["Annualized volatility"] = ResearchValue.unavailable(source, "At least three closes are required for volatility.")
    result["Benchmark-relative performance"] = ResearchValue.unavailable("Market history", "Load a benchmark comparison with a compatible period.")
    return result
