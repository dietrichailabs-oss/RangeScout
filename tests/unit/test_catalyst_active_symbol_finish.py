from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timezone

from app.application.catalyst_runtime import CatalystRuntime, CatalystSource, POLL_INTERVALS_SECONDS
from app.catalysts.feeds.nasdaq_halts import FEED_URL as NASDAQ_URL, MINIMUM_POLL_SECONDS, parse_halt_rss
from app.catalysts.feeds.white_house import FEED_URL as WHITE_HOUSE_URL, parse_feed
from app.catalysts.storage import CatalystStore
from app.catalysts.symbol_mapping import SymbolCatalog


NOW = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        future.set_result(function(*args))
        return future


def _official_events():
    halts = parse_halt_rss(
        b"<rss><channel>"
        b"<item><title>BA Trading Halt</title><link>https://www.nasdaqtrader.com/ba</link><IssueSymbol>BA</IssueSymbol></item>"
        b"<item><title>RTX Trading Halt</title><link>https://www.nasdaqtrader.com/rtx</link><IssueSymbol>RTX</IssueSymbol></item>"
        b"</channel></rss>",
        NOW,
    )
    broad = parse_feed(
        b"<rss><channel><item><title>National infrastructure policy update</title>"
        b"<link>https://www.whitehouse.gov/briefing-room/policy/</link>"
        b"<category>Policy</category></item></channel></rss>",
        NOW,
    )
    return [*halts, *broad]


def test_context_switch_recorrelates_retained_official_events_without_poll(tmp_path) -> None:
    collected: list[set[str]] = []
    emissions = []
    source = CatalystSource("nasdaq", 60.0, lambda symbols: collected.append(symbols) or [])
    runtime = CatalystRuntime(
        [source],
        ImmediateExecutor(),
        lambda callback: callback(),
        SymbolCatalog(),
        CatalystStore(tmp_path / "catalysts.json"),
        lambda events: emissions.append(events),
        lambda *args: None,
        lambda *args: None,
        clock=lambda: NOW,
    )
    runtime.set_context("BA", {"BA", "RTX"})
    runtime.ingest(_official_events())
    ba_ranked = emissions[-1]
    assert ba_ranked[0].event.symbols == ("BA",)
    assert {event.event.source_url for event in ba_ranked} == {
        "https://www.nasdaqtrader.com/ba",
        "https://www.nasdaqtrader.com/rtx",
        "https://www.whitehouse.gov/briefing-room/policy/",
    }
    emission_count = len(emissions)
    runtime.set_context("RTX", {"BA", "RTX"})
    assert len(emissions) == emission_count + 1
    assert emissions[-1][0].event.symbols == ("RTX",)
    assert collected == []


def test_official_rss_sources_and_cadences_remain_frozen() -> None:
    assert NASDAQ_URL == "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
    assert WHITE_HOUSE_URL == "https://www.whitehouse.gov/feed/"
    assert MINIMUM_POLL_SECONDS == 60.0
    assert POLL_INTERVALS_SECONDS == {"sec": 900.0, "nasdaq": 60.0, "white_house": 900.0, "congress": 900.0}
