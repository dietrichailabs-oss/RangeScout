"""Local application settings with safe defaults."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from app.providers.public_policy import normalize_public_provider
from app.company_data.scheduler import CompanyUpdateSchedule, normalize_schedule


ALLOWED_LIVE_REFRESH_INTERVALS_MS = (500, 1000, 10000, 30000)
DEFAULT_LIVE_REFRESH_INTERVAL_MS = 10000
SMART_PROVIDER_MODE = "smart"
FORCED_PROVIDER_MODES = frozenset(
    {"yahoo", "finnhub", "twelve_data", "alpha_vantage", "coinbase_exchange", "kraken", "coinpaprika"}
)
FORBIDDEN_CREDENTIAL_SETTING_KEYS = frozenset(
    {
        "alpaca_key_id",
        "alpaca_secret_key",
        "api_key",
        "credential",
        "credentials",
        "finnhub_api_key",
        "key_id",
        "provider_credentials",
        "publishable_key",
        "secret_key",
        "token",
    }
)


@dataclass(frozen=True)
class AppSettings:
    window_width: int = 1280
    window_height: int = 820
    theme: str = "dark"
    default_provider: str = "yahoo"
    provider_mode: str = SMART_PROVIDER_MODE
    provider_policy_version: int = 6
    history_days_default: int = 365
    cache_days_max: int = 3650
    live_timeout_seconds: float = 12.0
    live_refresh_interval_ms: int = DEFAULT_LIVE_REFRESH_INTERVAL_MS
    ticker_position: str = "top"
    company_update_schedule: str = CompanyUpdateSchedule.WEEKLY.value
    logo_refresh_schedule: str = CompanyUpdateSchedule.MONTHLY.value
    last_page: int = 0
    research_period: str = "annual"
    selected_watchlist: str = ""
    window_x: int | None = None
    window_y: int | None = None
    splitter_positions: tuple[int, ...] = ()
    recent_symbols: tuple[str, ...] = ()


def load_default_settings() -> AppSettings:
    return AppSettings()


def load_user_settings(settings_dir: str | None = None) -> AppSettings:
    if settings_dir is None:
        return load_default_settings()

    path = Path(settings_dir) / "settings.json"
    if not path.exists():
        return load_default_settings()

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (OSError, json.JSONDecodeError):
        return load_default_settings()

    if not isinstance(payload, dict):
        return load_default_settings()

    sanitized_payload, removed_plaintext_credentials = _strip_credential_fields(payload)
    payload = sanitized_payload

    defaults = load_default_settings()
    provider_policy_version = _coerce_int(payload.get("provider_policy_version"), 1)
    default_provider = _coerce_str(payload.get("default_provider"), defaults.default_provider)
    refresh_value = payload.get("live_refresh_interval_ms")
    live_refresh_interval_ms = normalize_live_refresh_interval(refresh_value)
    normalized_provider = normalize_public_provider(default_provider)
    provider_selection_migrated = normalized_provider != default_provider.strip().lower()
    provider_mode = normalize_provider_mode(payload.get("provider_mode"))
    migrated = (
        provider_policy_version != 6
        or removed_plaintext_credentials
        or "live_refresh_interval_ms" not in payload
        or refresh_value != live_refresh_interval_ms
        or provider_selection_migrated
    )
    # RangeScout 1.2 limits public providers to Yahoo and Finnhub. Every
    # legacy or unknown provider selection migrates to Yahoo.
    default_provider = normalized_provider
    settings = AppSettings(
        window_width=_coerce_int(payload.get("window_width"), defaults.window_width),
        window_height=_coerce_int(payload.get("window_height"), defaults.window_height),
        theme=_coerce_str(payload.get("theme"), defaults.theme),
        default_provider=default_provider,
        provider_mode=provider_mode,
        provider_policy_version=6,
        history_days_default=_coerce_int(payload.get("history_days_default"), defaults.history_days_default),
        cache_days_max=_coerce_int(payload.get("cache_days_max"), defaults.cache_days_max),
        live_timeout_seconds=_coerce_float(payload.get("live_timeout_seconds"), defaults.live_timeout_seconds),
        live_refresh_interval_ms=live_refresh_interval_ms,
        ticker_position=_normalize_ticker_position(payload.get("ticker_position")),
        company_update_schedule=normalize_schedule(
            payload.get("company_update_schedule"), CompanyUpdateSchedule.WEEKLY
        ).value,
        logo_refresh_schedule=normalize_schedule(
            payload.get("logo_refresh_schedule"), CompanyUpdateSchedule.MONTHLY
        ).value,
        last_page=max(0, min(8, _coerce_int(payload.get("last_page"), defaults.last_page))),
        research_period=_normalize_research_period(payload.get("research_period")),
        selected_watchlist=_coerce_str(payload.get("selected_watchlist"), ""),
        window_x=_coerce_optional_int(payload.get("window_x")),
        window_y=_coerce_optional_int(payload.get("window_y")),
        splitter_positions=_coerce_int_tuple(payload.get("splitter_positions")),
        recent_symbols=_coerce_symbols(payload.get("recent_symbols")),
    )
    if migrated:
        _safe_dump(path, {**payload, **_settings_payload(settings)})
    return settings


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _strip_credential_fields(payload: dict[str, object]) -> tuple[dict[str, object], bool]:
    removed = False

    def sanitize(value: object) -> object:
        nonlocal removed
        if isinstance(value, dict):
            clean: dict[str, object] = {}
            for key, nested in value.items():
                if str(key).strip().lower() in FORBIDDEN_CREDENTIAL_SETTING_KEYS:
                    removed = True
                    continue
                clean[str(key)] = sanitize(nested)
            return clean
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(payload), removed  # type: ignore[return-value]


def _coerce_float(value: object, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value[:20]:
        try:
            result.append(max(0, int(item)))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _coerce_symbols(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        symbol = str(item).strip().upper()
        if symbol and symbol not in result and len(symbol) <= 32:
            result.append(symbol)
        if len(result) >= 12:
            break
    return tuple(result)


def _coerce_str(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value if value else fallback


def normalize_live_refresh_interval(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_LIVE_REFRESH_INTERVAL_MS
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIVE_REFRESH_INTERVAL_MS
    if interval not in ALLOWED_LIVE_REFRESH_INTERVALS_MS:
        return DEFAULT_LIVE_REFRESH_INTERVAL_MS
    return interval


def normalize_provider_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in FORCED_PROVIDER_MODES else SMART_PROVIDER_MODE


def _safe_dump(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _settings_payload(settings: AppSettings) -> dict[str, object]:
    return {
        "window_width": settings.window_width,
        "window_height": settings.window_height,
        "theme": settings.theme,
        "default_provider": normalize_public_provider(settings.default_provider),
        "provider_mode": normalize_provider_mode(settings.provider_mode),
        "provider_policy_version": 6,
        "history_days_default": settings.history_days_default,
        "cache_days_max": settings.cache_days_max,
        "live_timeout_seconds": settings.live_timeout_seconds,
        "live_refresh_interval_ms": settings.live_refresh_interval_ms,
        "ticker_position": settings.ticker_position,
        "company_update_schedule": settings.company_update_schedule,
        "logo_refresh_schedule": settings.logo_refresh_schedule,
        "last_page": settings.last_page,
        "research_period": settings.research_period,
        "selected_watchlist": settings.selected_watchlist,
        "window_x": settings.window_x,
        "window_y": settings.window_y,
        "splitter_positions": list(settings.splitter_positions),
        "recent_symbols": list(settings.recent_symbols),
    }


def _normalize_ticker_position(value: object) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in {"top", "bottom", "hidden"} else "top"


def _normalize_research_period(value: object) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in {"annual", "quarterly"} else "annual"


def save_user_settings(settings_dir: str, settings: AppSettings) -> None:
    path = Path(settings_dir) / "settings.json"
    _safe_dump(path, _settings_payload(settings))


def export_safe_settings(path: Path | str, settings: AppSettings) -> None:
    """Export only the same non-sensitive allowlist persisted in settings.json."""
    target = Path(path)
    payload = {"schema": "rangescout-safe-preferences-v1", "preferences": _settings_payload(settings)}
    sanitized, _removed = _strip_credential_fields(payload)
    _safe_dump(target, sanitized)


def import_safe_settings(path: Path | str, current: AppSettings) -> AppSettings:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("The preferences file is unavailable or invalid.") from None
    if not isinstance(payload, dict) or payload.get("schema") != "rangescout-safe-preferences-v1":
        raise ValueError("The preferences file has an unsupported schema.")
    preferences = payload.get("preferences")
    if not isinstance(preferences, dict):
        raise ValueError("The preferences file contains no preferences object.")
    clean, _removed = _strip_credential_fields(preferences)
    # Reuse the normal loader to keep all normalization/migration rules in one place.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rangescout-settings-import-") as folder:
        _safe_dump(Path(folder) / "settings.json", {**_settings_payload(current), **clean})
        return load_user_settings(folder)
