"""One-transaction local symbol snapshot and last-known quote persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
import sqlite3

from app.models.schemas import (
    AssetType, DataDelay, DataFreshnessState, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot,
)


@dataclass(frozen=True, slots=True)
class LocalCompanyIdentity:
    instrument_id: int | None
    symbol: str
    security_name: str
    asset_class: str
    security_type: str | None
    exchange: str | None
    mic_code: str | None
    currency: str
    country: str | None
    cik: str | None
    sector: str | None
    industry: str | None
    website_domain: str | None
    aliases: tuple[str, ...]
    local_logo_path: str | None
    logo_source_id: str | None


@dataclass(frozen=True, slots=True)
class LocalSymbolSnapshot:
    symbol: str
    identity: LocalCompanyIdentity
    quote: QuoteSnapshot | None
    quote_provider_id: str | None
    quote_received_at: datetime | None
    bars: tuple[OhlcvBar, ...]
    loaded_at: datetime
    elapsed_ms: float
    query_count: int

    @property
    def meaningful(self) -> bool:
        return self.quote is not None or bool(self.bars) or self.identity.instrument_id is not None


class LocalSnapshotRepository:
    """Reads identity, quote, and bars in one bounded SQLite transaction."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def load(self, symbol: str, *, bar_limit: int = 365) -> LocalSymbolSnapshot:
        began = perf_counter()
        normalized = str(symbol).strip().upper()
        limit = max(1, min(3650, int(bar_limit)))
        with self._connect() as connection:
            connection.execute("BEGIN")
            identity_row = connection.execute(
                """SELECT i.*,GROUP_CONCAT(DISTINCT a.alias_symbol) AS aliases
                   FROM rs_instruments i LEFT JOIN rs_instrument_aliases a ON a.instrument_id=i.instrument_id
                   WHERE UPPER(i.canonical_symbol)=? OR i.instrument_id=(
                       SELECT instrument_id FROM rs_instrument_aliases WHERE UPPER(alias_symbol)=?
                       ORDER BY alias_id DESC LIMIT 1)
                   GROUP BY i.instrument_id ORDER BY i.is_active DESC,i.updated_at_utc DESC LIMIT 1""",
                (normalized, normalized),
            ).fetchone()
            quote_row = connection.execute(
                """SELECT q.*,i.canonical_symbol,i.security_name,i.asset_class,i.primary_venue,i.currency AS instrument_currency,
                          i.country_code,i.sector
                   FROM rs_last_quotes q JOIN rs_instruments i ON i.instrument_id=q.instrument_id
                   WHERE i.instrument_id=COALESCE(?,(
                       SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? ORDER BY is_active DESC LIMIT 1))""",
                (identity_row["instrument_id"] if identity_row else None, normalized),
            ).fetchone()
            bars_rows = connection.execute(
                """SELECT b.*,i.currency FROM ohlcv_bars b JOIN instruments i
                   ON i.symbol=b.symbol AND i.provider=b.provider
                   WHERE b.symbol=? AND b.provider=(
                       SELECT provider FROM ohlcv_bars WHERE symbol=? GROUP BY provider
                       ORDER BY MAX(bar_date) DESC LIMIT 1)
                   ORDER BY b.bar_date DESC LIMIT ?""",
                (normalized, normalized, limit),
            ).fetchall()
            connection.commit()

        identity = self._identity(normalized, identity_row, bars_rows)
        bars = tuple(self._bar(normalized, row) for row in reversed(bars_rows))
        quote = self._quote(identity, quote_row)
        received = _dt(quote_row["received_at_utc"]) if quote_row else None
        if quote is None and bars:
            latest = bars[-1]
            previous = bars[-2].close if len(bars) > 1 else None
            stamp = latest.provider_timestamp or datetime.combine(latest.date, datetime.min.time(), timezone.utc)
            quote = QuoteSnapshot(
                Instrument(InstrumentIdentifier(normalized, identity.exchange), identity.security_name,
                           _asset_type(identity.asset_class), identity.currency, latest.provider,
                           identity.country, identity.sector),
                latest.close, previous, latest.volume, stamp, latest.provider_timestamp,
                DataDelay.OFFLINE, None, latest.source_timezone, identity.currency, DataFreshnessState.CACHED,
            )
            received = stamp
        return LocalSymbolSnapshot(
            normalized, identity, quote, quote_row["provider_id"] if quote_row else (bars[-1].provider if bars else None),
            received, bars, datetime.now(timezone.utc), (perf_counter() - began) * 1000.0, 3,
        )

    def save_quote(self, quote: QuoteSnapshot, provider_id: str) -> None:
        symbol = quote.instrument.identifier.symbol.strip().upper()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? ORDER BY is_active DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,security_type,
                       primary_venue,currency,country_code,is_active,first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, quote.instrument.name or symbol, quote.instrument.asset_type.value, None,
                     quote.instrument.identifier.exchange or "", quote.currency, quote.instrument.country,
                     1, now, now, now, now),
                )
                instrument_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            else:
                instrument_id = int(row[0])
            values = (
                instrument_id, str(quote.last), _decimal(quote.previous_close), quote.volume, quote.currency,
                quote.delay_label.value, provider_id, _iso(quote.provider_timestamp), now, quote.delay_label.value,
                quote.source_timezone, _decimal(quote.day_low), _decimal(quote.day_high),
                _decimal(quote.fifty_two_week_low), _decimal(quote.fifty_two_week_high), quote.average_volume,
                _decimal(quote.market_cap), _decimal(quote.pre_market_price), _decimal(quote.pre_market_change),
                _decimal(quote.pre_market_change_percent), _decimal(quote.after_hours_price),
                _decimal(quote.after_hours_change), _decimal(quote.after_hours_change_percent),
            )
            connection.execute(
                """INSERT OR REPLACE INTO rs_last_quotes(
                   instrument_id,last_price,previous_close,volume,currency,session_label,provider_id,
                   provider_timestamp_utc,received_at_utc,delay_label,source_timezone,day_low,day_high,
                   fifty_two_week_low,fifty_two_week_high,average_volume,market_cap,pre_market_price,
                   pre_market_change,pre_market_change_percent,after_hours_price,after_hours_change,
                   after_hours_change_percent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            connection.commit()

    def index_report(self) -> dict[str, object]:
        with self._connect() as connection:
            indexes = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
            ).fetchall()]
            plan = [tuple(row) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='AAPL'"
            ).fetchall()]
            bars_plan = [tuple(row) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM ohlcv_bars WHERE symbol='AAPL' AND provider='yahoo' ORDER BY bar_date DESC LIMIT 365"
            ).fetchall()]
        return {"indexes": indexes, "symbol_lookup_plan": plan, "bars_lookup_plan": bars_plan, "wal": True}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _identity(symbol: str, row: sqlite3.Row | None, bars: list[sqlite3.Row]) -> LocalCompanyIdentity:
        if row is None:
            exchange = bars[0]["exchange"] if bars else None
            currency = bars[0]["currency"] if bars and bars[0]["currency"] else "USD"
            return LocalCompanyIdentity(None, symbol, symbol, "stock", None, exchange, None, currency,
                                        None, None, None, None, None, (), None, None)
        aliases = tuple(sorted(filter(None, str(row["aliases"] or "").split(","))))
        return LocalCompanyIdentity(
            int(row["instrument_id"]), row["canonical_symbol"], row["security_name"] or symbol,
            row["asset_class"], row["security_type"], row["primary_venue"] or None, row["mic_code"],
            row["currency"] or "USD", row["country_code"], row["cik"], row["sector"], row["industry"],
            row["website_domain"], aliases, row["local_logo_path"], row["logo_source_id"],
        )

    @staticmethod
    def _bar(symbol: str, row: sqlite3.Row) -> OhlcvBar:
        return OhlcvBar(
            InstrumentIdentifier(symbol, row["exchange"]), date.fromisoformat(row["bar_date"]),
            Decimal(str(row["open"])), Decimal(str(row["high"])), Decimal(str(row["low"])),
            Decimal(str(row["close"])), int(row["volume"]), row["provider"], bool(row["adjusted"]),
            row["source"], _dt(row["provider_timestamp"]), row["source_timezone"] or "UTC",
        )

    @staticmethod
    def _quote(identity: LocalCompanyIdentity, row: sqlite3.Row | None) -> QuoteSnapshot | None:
        if row is None:
            return None
        return QuoteSnapshot(
            Instrument(InstrumentIdentifier(identity.symbol, identity.exchange), identity.security_name,
                       _asset_type(identity.asset_class), identity.currency, row["provider_id"],
                       identity.country, identity.sector),
            Decimal(row["last_price"]), _decimal_value(row["previous_close"]), row["volume"],
            _dt(row["received_at_utc"]) or datetime.now(timezone.utc), _dt(row["provider_timestamp_utc"]),
            _delay(row["delay_label"]), None, row["source_timezone"] or "UTC", row["currency"] or "USD",
            DataFreshnessState.CACHED, _decimal_value(row["day_low"]), _decimal_value(row["day_high"]),
            _decimal_value(row["fifty_two_week_low"]), _decimal_value(row["fifty_two_week_high"]),
            row["average_volume"], _decimal_value(row["market_cap"]), None, None,
            _decimal_value(row["pre_market_price"]), _decimal_value(row["pre_market_change"]),
            _decimal_value(row["pre_market_change_percent"]), _decimal_value(row["after_hours_price"]),
            _decimal_value(row["after_hours_change"]), _decimal_value(row["after_hours_change_percent"]),
        )


def _asset_type(value: str) -> AssetType:
    try:
        return AssetType(value)
    except ValueError:
        return AssetType.UNKNOWN


def _delay(value: str) -> DataDelay:
    try:
        return DataDelay(value)
    except ValueError:
        return DataDelay.OFFLINE


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal_value(value: str | None) -> Decimal | None:
    return Decimal(value) if value not in (None, "") else None
