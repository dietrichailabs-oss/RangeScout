"""Terms-aware logo-source descriptors; network adapters remain opt-in/BYO-key."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LogoSourceDescriptor:
    source_id: str
    display_name: str
    official_url: str
    requires_user_credential: bool
    persistent_image_cache_permitted: bool
    enabled_by_default: bool
    note: str


LOGO_SOURCE_ORDER = (
    LogoSourceDescriptor("local_permitted", "Local permitted logo", "local://company-logo", False, True, True, "Only project/user-controlled or license-cleared files."),
    LogoSourceDescriptor("finnhub_profile", "Finnhub company profile", "https://finnhub.io/docs/api/company-profile2", True, False, True, "Session memory only unless current account terms explicitly permit persistence."),
    LogoSourceDescriptor("twelve_data_logo", "Twelve Data logo", "https://twelvedata.com/docs", True, False, True, "Session memory only unless current account terms explicitly permit persistence."),
    LogoSourceDescriptor("logo_dev", "Logo.dev ticker logo", "https://www.logo.dev/docs/logo-images/ticker", True, False, True, "Preserved 1.4.x BYO publishable-key path; session memory only."),
    LogoSourceDescriptor("wikimedia_commons", "Wikimedia Commons / Wikidata", "https://commons.wikimedia.org/", False, True, False, "Enable only when per-asset license and attribution metadata are retained."),
    LogoSourceDescriptor("simple_icons", "Simple Icons", "https://simpleicons.org/", False, True, False, "Only applicable brands; preserve CC0/source metadata and trademark caveats."),
    LogoSourceDescriptor("ticker_monogram", "Ticker monogram", "local://ticker-monogram", False, False, True, "Project-rendered fallback; no downloaded image bytes."),
)
