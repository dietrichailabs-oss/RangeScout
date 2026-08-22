"""Offline-first ranked search over the provisioned company/instrument master."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
import sqlite3


_SUFFIXES = re.compile(
    r"\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group)\b\.?,?",
    re.IGNORECASE,
)
_SPACE = re.compile(r"[^a-z0-9]+")


def normalize_company_name(value: str) -> str:
    cleaned = _SUFFIXES.sub(" ", str(value or ""))
    return _SPACE.sub(" ", cleaned.lower()).strip()


@dataclass(frozen=True, slots=True)
class InstrumentSearchResult:
    symbol: str
    name: str
    exchange: str
    asset_type: str
    score: int
    match_kind: str

    @property
    def display_text(self) -> str:
        return f"{self.symbol}  ·  {self.name}  ·  {self.exchange}  ·  {self.asset_type}"


class LocalInstrumentSearch:
    """Rank local symbols/names without ever treating arbitrary prose as a ticker."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def search(self, query: str, limit: int = 12) -> list[InstrumentSearchResult]:
        raw = str(query or "").strip()
        if not raw or len(raw) > 160:
            return []
        upper = raw.upper()
        normalized = normalize_company_name(raw)
        like = f"%{raw}%"
        prefix = f"{raw}%"
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT DISTINCT i.instrument_id,i.canonical_symbol,i.security_name,
                          i.primary_venue,i.asset_class,i.is_active,
                          GROUP_CONCAT(a.alias_symbol, '|') AS aliases
                   FROM rs_instruments i
                   LEFT JOIN rs_instrument_aliases a ON a.instrument_id=i.instrument_id
                   WHERE UPPER(i.canonical_symbol)=? OR UPPER(COALESCE(a.alias_symbol,''))=?
                      OR i.security_name LIKE ? COLLATE NOCASE
                      OR i.canonical_symbol LIKE ? COLLATE NOCASE
                      OR COALESCE(a.alias_symbol,'') LIKE ? COLLATE NOCASE
                   GROUP BY i.instrument_id
                   ORDER BY i.is_active DESC, i.canonical_symbol
                   LIMIT 250""",
                (upper, upper, like, prefix, prefix),
            ).fetchall()
        ranked: list[InstrumentSearchResult] = []
        for row in rows:
            symbol = str(row["canonical_symbol"] or "").upper()
            name = str(row["security_name"] or symbol)
            aliases = {item.upper() for item in str(row["aliases"] or "").split("|") if item}
            name_norm = normalize_company_name(name)
            score, kind = self._score(raw, upper, normalized, symbol, aliases, name, name_norm)
            if score <= 0:
                continue
            ranked.append(InstrumentSearchResult(
                symbol=symbol,
                name=name,
                exchange=str(row["primary_venue"] or "N/A"),
                asset_type=str(row["asset_class"] or "unknown").replace("_", " ").title(),
                score=score + (5 if bool(row["is_active"]) else 0),
                match_kind=kind,
            ))
        ranked.sort(key=lambda item: (-item.score, item.symbol, item.exchange))
        return ranked[: max(1, min(50, int(limit)))]

    def resolve_unique(self, query: str) -> InstrumentSearchResult | None:
        results = self.search(query, 8)
        if not results:
            return None
        first = results[0]
        second = results[1] if len(results) > 1 else None
        if first.match_kind in {"exact_ticker", "exact_alias"}:
            return first
        if first.score >= 900 and (second is None or first.score - second.score >= 80):
            return first
        if first.score >= 760 and (second is None or first.score - second.score >= 150):
            return first
        return None

    @staticmethod
    def _score(raw: str, upper: str, normalized: str, symbol: str, aliases: set[str], name: str, name_norm: str) -> tuple[int, str]:
        if upper == symbol:
            return 1100, "exact_ticker"
        if upper in aliases:
            return 1050, "exact_alias"
        if raw.casefold() == name.casefold():
            return 1000, "exact_name"
        if normalized and normalized == name_norm:
            return 940, "normalized_name"
        if normalized and name_norm.startswith(normalized):
            return 820 - min(120, len(name_norm) - len(normalized)), "name_prefix"
        if symbol.startswith(upper) and len(upper) >= 2:
            return 740 - min(100, len(symbol) - len(upper)), "ticker_prefix"
        if normalized and len(normalized) >= 3 and normalized in name_norm:
            return 650 - min(180, name_norm.index(normalized)), "name_contains"
        if normalized and len(normalized) >= 5:
            ratio = SequenceMatcher(None, normalized, name_norm).ratio()
            if ratio >= 0.88:
                return int(500 * ratio), "high_confidence_fuzzy"
        return 0, "none"
