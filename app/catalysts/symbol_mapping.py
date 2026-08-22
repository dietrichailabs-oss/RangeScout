from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.catalysts.entities import CatalystEvent


@dataclass
class SymbolCatalog:
    aliases: dict[str, str] = field(default_factory=dict)
    companies: dict[str, str] = field(default_factory=dict)
    sectors: dict[str, str] = field(default_factory=dict)

    def register(self, symbol: str, company: str, sector: str, *aliases: str) -> None:
        normalized = symbol.strip().upper()
        self.companies[normalized] = company.strip()
        self.sectors[normalized] = sector.strip()
        for value in (normalized, company, *aliases):
            self.aliases[value.strip().lower()] = normalized

    def match(self, event: CatalystEvent) -> CatalystEvent:
        text = f"{event.title} {event.summary or ''}".lower()
        matched_symbols = {symbol for alias, symbol in self.aliases.items() if alias and alias in text}
        symbols = tuple(sorted({symbol.strip().upper() for symbol in event.symbols if symbol.strip()} | matched_symbols))
        companies = tuple(sorted({value.strip() for value in event.company_names if value.strip()} | {
            self.companies[symbol] for symbol in symbols if symbol in self.companies
        }))
        sectors = {
            value.strip() for value in event.sectors if value.strip()
        } | {
            self.sectors[symbol] for symbol in symbols if symbol in self.sectors
        } | {sector for sector in self.sectors.values() if sector.lower() in text}
        sectors = tuple(sorted(sectors))
        return replace(event, symbols=symbols, company_names=companies, sectors=sectors)
