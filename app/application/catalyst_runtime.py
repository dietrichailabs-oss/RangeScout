"""Bounded, worker-backed production runtime for official catalyst sources."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.alerts.dispatcher import AlertType
from app.catalysts.correlation import CatalystCorrelator, CorrelatedEvent
from app.catalysts.entities import CatalystEvent
from app.catalysts.feeds.congress import API_URL, authentication_headers, parse_bills
from app.catalysts.feeds.http import OfficialFeedClient
from app.catalysts.feeds.nasdaq_halts import FEED_URL as NASDAQ_URL, MINIMUM_POLL_SECONDS, parse_halt_rss
from app.catalysts.feeds.sec import parse_submissions, submissions_url
from app.catalysts.feeds.white_house import FEED_URL as WHITE_HOUSE_URL, parse_feed
from app.catalysts.sec_resolver import SecSymbolResolver
from app.catalysts.storage import CatalystStore
from app.catalysts.symbol_mapping import SymbolCatalog
from app.security.credentials import CredentialStore


CATALYST_EVENT_LIMIT = 1000
POLL_INTERVALS_SECONDS = {"sec": 900.0, "nasdaq": 60.0, "white_house": 900.0, "congress": 900.0}


@dataclass(frozen=True, slots=True)
class CatalystSource:
    name: str
    interval_seconds: float
    collect: Callable[[set[str]], list[CatalystEvent]]


class CatalystRuntime:
    def __init__(
        self,
        sources: list[CatalystSource],
        executor: Executor,
        post_to_owner: Callable[[Callable[[], None]], None],
        catalog: SymbolCatalog,
        store: CatalystStore,
        on_events: Callable[[list[CorrelatedEvent]], None],
        on_alert: Callable[[AlertType, str, str, str, str | None], None],
        on_halt: Callable[[str, str | None], None],
        *,
        event_limit: int = CATALYST_EVENT_LIMIT,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.sources = sources
        self._executor = executor
        self._post = post_to_owner
        self._catalog = catalog
        self._store = store
        self._on_events = on_events
        self._on_alert = on_alert
        self._on_halt = on_halt
        self._event_limit = max(100, event_limit)
        self._clock = clock
        self._last_poll: dict[str, datetime] = {}
        self._in_flight: dict[str, Future] = {}
        self._events: OrderedDict[str, CatalystEvent] = OrderedDict()
        self._active_symbol = "AAPL"
        self._watchlist: set[str] = set()
        self._sectors: set[str] = set()
        self._shutdown = False
        self.source_status: dict[str, str] = {source.name: "not polled" for source in sources}

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def in_flight_count(self) -> int:
        return sum(not future.done() for future in self._in_flight.values())

    def set_context(self, active_symbol: str, watchlist: set[str], sectors: set[str] | None = None) -> None:
        self._active_symbol = active_symbol.strip().upper() or self._active_symbol
        self._watchlist = {value.strip().upper() for value in watchlist if value.strip()}
        self._sectors = set(sectors or ())
        self._publish_context()

    def poll_due(self, *, force: bool = False) -> None:
        if self._shutdown:
            return
        now = self._clock()
        symbols = {self._active_symbol, *self._watchlist}
        for source in self.sources:
            prior = self._last_poll.get(source.name)
            if not force and prior is not None and (now - prior).total_seconds() < source.interval_seconds:
                continue
            existing = self._in_flight.get(source.name)
            if existing is not None and not existing.done():
                continue
            self._last_poll[source.name] = now
            future = self._executor.submit(source.collect, set(symbols))
            self._in_flight[source.name] = future
            future.add_done_callback(lambda completed, name=source.name: self._post(lambda: self._finish(name, completed)))

    def ingest(self, events: list[CatalystEvent]) -> None:
        if self._shutdown:
            return
        new_events: list[CatalystEvent] = []
        for event in events:
            if event.event_id not in self._events:
                new_events.append(event)
            self._events[event.event_id] = event.without_restricted_content()
            self._events.move_to_end(event.event_id)
        while len(self._events) > self._event_limit:
            self._events.popitem(last=False)
        retained = list(self._events.values())
        self._store.save(retained)
        self._publish_context()
        self._dispatch_new(new_events)

    def _publish_context(self) -> None:
        correlated = CatalystCorrelator(self._catalog).correlate(
            list(self._events.values()),
            self._active_symbol,
            self._watchlist,
            self._sectors,
        )
        self._on_events(correlated)

    def shutdown(self) -> None:
        self._shutdown = True
        for future in self._in_flight.values():
            future.cancel()

    def _finish(self, name: str, future: Future) -> None:
        if self._shutdown:
            return
        try:
            events = future.result()
        except Exception:
            self.source_status[name] = "temporarily unavailable; retained prior events"
            return
        self.source_status[name] = f"ok; {len(events)} event(s)"
        self.ingest(events)

    def _dispatch_new(self, events: list[CatalystEvent]) -> None:
        for event in events:
            symbol = event.symbols[0] if event.symbols else None
            if event.category == "halt":
                status = event.metadata.get("official_status")
                for value in event.symbols:
                    self._on_halt(value, status)
                alert_type = AlertType.TRADE_RESUME if status == "RESUMED" else AlertType.TRADE_HALT
            elif event.source == "SEC" or event.category == "sec_filing":
                alert_type = AlertType.SEC_FILING
            elif event.source in {"White House", "Congress.gov"}:
                alert_type = AlertType.GOVERNMENT_CATALYST
            else:
                alert_type = AlertType.WATCHLIST_NEWS
            self._on_alert(alert_type, event.event_id, event.source, event.title, symbol)


def build_official_sources(
    credential_store: CredentialStore,
    data_dir: Path,
    user_agent: str,
) -> list[CatalystSource]:
    sec_client = OfficialFeedClient(user_agent, 0.2)
    sec_resolver = SecSymbolResolver(sec_client, data_dir / "sec_symbol_cik_cache.json")
    nasdaq_client = OfficialFeedClient(user_agent, MINIMUM_POLL_SECONDS)
    white_house_client = OfficialFeedClient(user_agent, 1.0)
    congress_client = OfficialFeedClient(user_agent, 1.0)

    def sec_collect(symbols: set[str]) -> list[CatalystEvent]:
        values: list[CatalystEvent] = []
        for symbol, cik in sec_resolver.resolve(symbols).items():
            values.extend(parse_submissions(sec_client.get_json(submissions_url(cik)), symbol, datetime.now(timezone.utc)))
        return values

    def nasdaq_collect(_symbols: set[str]) -> list[CatalystEvent]:
        return parse_halt_rss(nasdaq_client.get(NASDAQ_URL), datetime.now(timezone.utc))

    def white_house_collect(_symbols: set[str]) -> list[CatalystEvent]:
        return parse_feed(white_house_client.get(WHITE_HOUSE_URL), datetime.now(timezone.utc))

    sources = [
        CatalystSource("sec", POLL_INTERVALS_SECONDS["sec"], sec_collect),
        CatalystSource("nasdaq", POLL_INTERVALS_SECONDS["nasdaq"], nasdaq_collect),
        CatalystSource("white_house", POLL_INTERVALS_SECONDS["white_house"], white_house_collect),
    ]
    congress_credentials = credential_store.load("congress")
    if congress_credentials is not None:
        api_key = congress_credentials.values["api_key"]

        def congress_collect(_symbols: set[str]) -> list[CatalystEvent]:
            payload = congress_client.get_json(API_URL, headers=authentication_headers(api_key))
            return parse_bills(payload, datetime.now(timezone.utc))

        sources.append(CatalystSource("congress", POLL_INTERVALS_SECONDS["congress"], congress_collect))
    return sources
