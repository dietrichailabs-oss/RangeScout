"""Provider notation and source-placeholder hygiene for canonical instruments."""

from __future__ import annotations

from dataclasses import dataclass
import re
from string import ascii_uppercase, digits
from typing import Iterable


_YAHOO_ALLOWED = frozenset(ascii_uppercase + digits + ".^-=")
_PLACEHOLDER = re.compile(
    r"^(?:NONE|NULL|N/A|NOT[ _-]?(?:AVAILABLE|APPLICABLE)|NO[ _-]?TICKER|UNKNOWN|UNLISTED)[.\-]*$",
    re.IGNORECASE,
)
_DOLLAR_SERIES = re.compile(r"^([A-Z0-9.]+)\$([A-Z0-9]*)$")
_PROVIDER_ALIAS_KINDS = frozenset(
    {"official_directory_symbol", "official_source_symbol_variant", "source_symbol", "provider_symbol"}
)


class ProviderSymbolError(ValueError):
    """A symbol cannot be represented safely for a provider."""


@dataclass(frozen=True, slots=True)
class ProviderSymbolDecision:
    provider_id: str
    canonical_symbol: str
    provider_symbol: str | None
    status: str
    reason: str
    mapping_source: str
    evidence_aliases: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.status == "supported" and self.provider_symbol is not None


def is_placeholder_symbol(value: object) -> bool:
    normalized = str(value or "").strip().upper()
    return not normalized or bool(_PLACEHOLDER.fullmatch(normalized))


def is_provider_scoped_alias_kind(value: object) -> bool:
    return str(value or "").strip().lower() in _PROVIDER_ALIAS_KINDS


def normalize_yahoo_symbol(raw: str) -> str:
    if not isinstance(raw, str):
        raise ProviderSymbolError("Symbol must be a string.")
    stripped = raw.strip()
    if stripped != raw:
        raise ProviderSymbolError("Symbol format is invalid.")
    if stripped == stripped.lower() and not any(character in stripped for character in ".^-="):
        raise ProviderSymbolError("Symbol format is invalid.")
    normalized = stripped.upper()
    if is_placeholder_symbol(normalized):
        raise ProviderSymbolError("Symbol is a source placeholder, not a tradable identity.")
    if not normalized or len(normalized) > 64:
        raise ProviderSymbolError("Symbol length is invalid.")
    if any(character.isspace() or ord(character) < 0x20 for character in normalized):
        raise ProviderSymbolError("Symbol must not contain control or whitespace characters.")
    if any(character in {"&", "?", "#", "/", "%"} for character in normalized):
        raise ProviderSymbolError("Symbol contains unsupported URL control characters.")
    if not all(character in _YAHOO_ALLOWED for character in normalized):
        raise ProviderSymbolError("Symbol contains unsupported characters.")
    if normalized.startswith((".", "-")) or normalized.endswith((".", "-")):
        raise ProviderSymbolError("Symbol format is invalid.")
    if normalized.count("=") > 1 or normalized.count("^") > 1:
        raise ProviderSymbolError("Symbol format is invalid.")
    if normalized.startswith("^") and len(normalized) < 2:
        raise ProviderSymbolError("Symbol format is invalid.")
    return normalized


def derive_yahoo_provider_symbol(
    canonical_symbol: str,
    aliases: Iterable[tuple[str, str]],
) -> ProviderSymbolDecision:
    canonical = str(canonical_symbol or "").strip().upper()
    if is_placeholder_symbol(canonical):
        return ProviderSymbolDecision(
            "yahoo", canonical, None, "unsupported", "source_placeholder", "source_data_hygiene"
        )
    alias_rows = {
        (str(alias or "").strip().upper(), str(kind or "").strip().lower())
        for alias, kind in aliases
        if is_provider_scoped_alias_kind(kind)
    }
    observed = {alias for alias, _kind in alias_rows}
    if "." in canonical:
        explicit = sorted(
            alias for alias, kind in alias_rows
            if kind == "provider_symbol" and alias != canonical
        )
        for alias in explicit:
            try:
                provider_symbol = normalize_yahoo_symbol(alias)
            except ProviderSymbolError:
                continue
            return ProviderSymbolDecision(
                "yahoo", canonical, provider_symbol, "supported", "explicit_provider_symbol_mapping",
                "provider_specific_alias", (alias,),
            )
        official_dash = canonical.replace(".", "-")
        if official_dash in observed:
            provider_symbol = normalize_yahoo_symbol(official_dash)
            return ProviderSymbolDecision(
                "yahoo", canonical, provider_symbol, "supported", "official_source_dot_dash_crosswalk",
                "official_source_symbol_variant", (official_dash,),
            )
        return ProviderSymbolDecision(
            "yahoo", canonical, None, "unsupported", "unverified_dot_provider_identity",
            "provider_identity_not_established", tuple(sorted(observed)),
        )
    try:
        normalized = normalize_yahoo_symbol(canonical)
    except ProviderSymbolError as canonical_error:
        series = _DOLLAR_SERIES.fullmatch(canonical)
        if not series:
            return ProviderSymbolDecision(
                "yahoo", canonical, None, "unsupported", str(canonical_error), "provider_syntax_validation"
            )
        base, suffix = series.groups()
        directory_dash = f"{base}-{suffix}"
        directory_compact = f"{base}P{suffix}"
        if directory_dash not in observed or directory_compact not in observed:
            return ProviderSymbolDecision(
                "yahoo", canonical, None, "unsupported", "missing_cross_source_series_alias_evidence",
                "official_directory_crosswalk", tuple(sorted(observed)),
            )
        provider_symbol = normalize_yahoo_symbol(f"{base}-P{suffix}")
        return ProviderSymbolDecision(
            "yahoo", canonical, provider_symbol, "supported", "official_directory_series_crosswalk",
            "official_directory_alias_pair", (directory_dash, directory_compact),
        )
    return ProviderSymbolDecision(
        "yahoo", canonical, normalized, "supported", "canonical_symbol_provider_compatible", "canonical_identity"
    )
