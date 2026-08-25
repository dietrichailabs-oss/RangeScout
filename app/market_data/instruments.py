"""Normalized instrument, crypto, futures, and options-ready identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
import unicodedata

from app.market_data.contracts import AssetClass
from app.market_data.provider_symbols import is_placeholder_symbol


_SAFE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-/^=$]{0,63}$")


def normalize_symbol(symbol: str) -> str:
    normalized = unicodedata.normalize("NFKC", symbol).strip().upper()
    if is_placeholder_symbol(normalized) or not _SAFE_SYMBOL.fullmatch(normalized):
        raise ValueError("Symbol contains unsupported characters.")
    return normalized


@dataclass(frozen=True)
class DiscoveredInstrument:
    canonical_symbol: str
    security_name: str
    asset_class: AssetClass
    security_type: str
    primary_venue: str
    currency: str = "USD"
    country_code: str = "US"
    cik: str | None = None
    listing_date: date | None = None
    provider_symbol: str | None = None
    official_aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_symbol", normalize_symbol(self.canonical_symbol))
        if not self.security_name.strip():
            raise ValueError("Security name is required.")
        if not self.primary_venue.strip():
            raise ValueError("Primary venue is required.")
        normalized_aliases: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_alias, raw_kind in self.official_aliases:
            try:
                alias = normalize_symbol(raw_alias)
            except ValueError:
                # A malformed source variant must not discard a valid canonical listing.
                continue
            kind = str(raw_kind or "official_source_symbol_variant").strip().lower()
            key = (alias, kind)
            if key not in seen:
                seen.add(key)
                normalized_aliases.append(key)
        object.__setattr__(self, "official_aliases", tuple(normalized_aliases))


@dataclass(frozen=True)
class CryptoProduct:
    base_asset: str
    quote_asset: str
    venue: str
    product_type: str = "spot"
    provider_product_id: str | None = None
    status: str = "online"
    price_precision: int | None = None
    size_precision: int | None = None
    minimum_size: Decimal | None = None

    @property
    def canonical_symbol(self) -> str:
        return f"{normalize_symbol(self.base_asset)}-{normalize_symbol(self.quote_asset)}"


FUTURES_MONTHS = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


@dataclass(frozen=True)
class FuturesContract:
    root_symbol: str
    contract_symbol: str
    exchange: str
    month_code: str
    contract_year: int
    expiration: date | None
    currency: str = "USD"
    multiplier: Decimal | None = None
    tick_size: Decimal | None = None
    tick_value: Decimal | None = None
    delay_class: str = "delayed"

    @property
    def contract_month(self) -> int:
        try:
            return FUTURES_MONTHS[self.month_code.upper()]
        except KeyError as exc:
            raise ValueError("Invalid futures month code.") from exc


def parse_futures_symbol(symbol: str, exchange: str, expiration: date | None = None) -> FuturesContract:
    normalized = normalize_symbol(symbol)
    match = re.fullmatch(r"([A-Z0-9]{1,12})([FGHJKMNQUVXZ])(\d{1,4})", normalized)
    if not match:
        raise ValueError("Unsupported futures contract symbol.")
    root, month, year_text = match.groups()
    if len(year_text) == 1:
        year = 2030 + int(year_text) if int(year_text) < 5 else 2020 + int(year_text)
    elif len(year_text) == 2:
        year = 2000 + int(year_text)
    else:
        year = int(year_text)
    return FuturesContract(root, normalized, exchange, month, year, expiration)


def select_continuous_contract(contracts: list[FuturesContract], as_of: date, roll_days: int = 5) -> FuturesContract:
    eligible = sorted(
        (item for item in contracts if item.expiration is None or item.expiration >= as_of),
        key=lambda item: item.expiration or date.max,
    )
    if not eligible:
        raise ValueError("No non-expired futures contract is available.")
    first = eligible[0]
    if first.expiration and (first.expiration - as_of).days <= roll_days and len(eligible) > 1:
        return eligible[1]
    return first


def build_continuous_series(
    dated_prices: list[tuple[date, FuturesContract, Decimal]],
    *,
    adjustment: str = "none",
) -> list[tuple[date, Decimal, str]]:
    """Build a deterministic disclosed series without hiding contract rolls."""
    if adjustment not in {"none", "difference"}:
        raise ValueError("Unsupported continuous-series adjustment.")
    ordered = sorted(dated_prices, key=lambda value: value[0])
    result: list[tuple[date, Decimal, str]] = []
    cumulative = Decimal("0")
    previous_contract: FuturesContract | None = None
    previous_raw: Decimal | None = None
    for day, contract, raw in ordered:
        if previous_contract and contract.contract_symbol != previous_contract.contract_symbol and adjustment == "difference":
            if previous_raw is None:
                raise ValueError("Roll adjustment requires a previous contract price.")
            cumulative += previous_raw - raw
        adjusted = raw + cumulative if adjustment == "difference" else raw
        result.append((day, adjusted, contract.contract_symbol))
        previous_contract = contract
        previous_raw = raw
    return result


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    option_type: str
    strike: Decimal
    expiration: date
    multiplier: Decimal = Decimal("100")
    venue: str | None = None

    def __post_init__(self) -> None:
        if self.option_type not in {"call", "put"}:
            raise ValueError("Option type must be call or put.")
        object.__setattr__(self, "underlying", normalize_symbol(self.underlying))
