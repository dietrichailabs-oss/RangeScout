"""Terms-aware company-logo pipeline with safe local-first resolution."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from threading import RLock

from app.application.path_safety import is_link_or_reparse_point
from app.company_logos.models import CompanyLogoAsset, CompanyLogoStatus
from app.company_logos.provider import (
    FINNHUB_LOGO_PROVIDER_ID,
    LOGO_DEV_PROVIDER_ID,
    MAX_LOGO_BYTES,
    TWELVE_DATA_LOGO_PROVIDER_ID,
    FinnhubProfileLogoClient,
    LogoDevClient,
    LogoProviderError,
    TwelveDataLogoClient,
)
from app.company_logos.repository import CompanyLogoStateRepository
from app.company_data.repository import CompanyDatabaseRepository, CompanyRecord
from app.security.credentials import CredentialStore


SESSION_CACHE_LIMIT = 128
SESSION_CACHE_TTL = timedelta(hours=24)
NEGATIVE_RETRY = timedelta(minutes=30)
RATE_LIMIT_RETRY = timedelta(minutes=10)
PERSISTENT_LOCAL_SOURCES = frozenset({"local_permitted", "wikimedia_commons", "simple_icons"})
SUPPORTED_LOCAL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg"})

_SOURCE_LABELS = {
    "local_permitted": "Local permitted logo",
    FINNHUB_LOGO_PROVIDER_ID: "Finnhub company profile",
    TWELVE_DATA_LOGO_PROVIDER_ID: "Twelve Data logo",
    LOGO_DEV_PROVIDER_ID: "Logo.dev ticker lookup",
    "wikimedia_commons": "Wikimedia Commons / Wikidata",
    "simple_icons": "Simple Icons",
    "ticker_monogram": "ticker monogram",
}


class CompanyLogoService:
    def __init__(
        self,
        database_path: Path | str,
        credential_store: CredentialStore,
        *,
        client: LogoDevClient | None = None,
        finnhub_client: FinnhubProfileLogoClient | None = None,
        twelve_data_client: TwelveDataLogoClient | None = None,
        logo_root: Path | str | None = None,
        now_fn=None,
    ) -> None:
        database = Path(database_path)
        self.repository = CompanyLogoStateRepository(database)
        self.company_repository = CompanyDatabaseRepository(database)
        self.credential_store = credential_store
        self.client = client or LogoDevClient()
        self.finnhub_client = finnhub_client or FinnhubProfileLogoClient()
        self.twelve_data_client = twelve_data_client or TwelveDataLogoClient()
        self.data_root = database.parent.resolve()
        self.logo_root = Path(logo_root).resolve() if logo_root is not None else (self.data_root / "logos").resolve()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._cache: OrderedDict[tuple[str, str, str], tuple[datetime, CompanyLogoAsset]] = OrderedDict()
        self._lock = RLock()

    def configured(self) -> bool:
        return any(self._credential_value(provider_id, field) for provider_id, field in (
            ("finnhub", "api_key"),
            ("twelve_data", "api_key"),
            (LOGO_DEV_PROVIDER_ID, "publishable_key"),
        ))

    def source_status(self) -> dict[str, object]:
        return {
            "configured": self.configured(),
            "source_order": (
                "local_permitted", FINNHUB_LOGO_PROVIDER_ID, TWELVE_DATA_LOGO_PROVIDER_ID,
                LOGO_DEV_PROVIDER_ID, "wikimedia_commons", "simple_icons", "ticker_monogram",
            ),
            "credential_sources": {
                FINNHUB_LOGO_PROVIDER_ID: bool(self._credential_value("finnhub", "api_key")),
                TWELVE_DATA_LOGO_PROVIDER_ID: bool(self._credential_value("twelve_data", "api_key")),
                LOGO_DEV_PROVIDER_ID: bool(self._credential_value(LOGO_DEV_PROVIDER_ID, "publishable_key")),
            },
            "persistent_sources": tuple(sorted(PERSISTENT_LOCAL_SOURCES)),
            "cache_policy": "licensed local files only; Finnhub/Twelve Data/Logo.dev image bytes remain session-only",
        }

    def cached(self, symbol: str, exchange: str | None = None, *, theme: str = "dark") -> CompanyLogoAsset | None:
        key = self._cache_key(symbol, exchange, theme)
        now = self._utc_now()
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None
            expires_at, asset = item
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return asset

    def resolve(
        self,
        symbol: str,
        exchange: str | None = None,
        *,
        theme: str = "dark",
        force: bool = False,
    ) -> CompanyLogoAsset:
        normalized_symbol = str(symbol).strip().upper()
        normalized_exchange = str(exchange or "").strip().upper() or None
        normalized_theme = "light" if theme == "light" else "dark"
        now = self._utc_now()
        key = self._cache_key(normalized_symbol, normalized_exchange, normalized_theme)
        if not force:
            cached = self.cached(normalized_symbol, normalized_exchange, theme=normalized_theme)
            if cached is not None:
                return cached

        local = self._resolve_local(normalized_symbol, normalized_exchange, now)
        if local is not None:
            self._remember(key, local, now)
            return local

        providers = (
            (FINNHUB_LOGO_PROVIDER_ID, "finnhub", "api_key", self.finnhub_client,
             "Finnhub company profile; image bytes remain session-only."),
            (TWELVE_DATA_LOGO_PROVIDER_ID, "twelve_data", "api_key", self.twelve_data_client,
             "Twelve Data logo endpoint; image bytes remain session-only."),
            (LOGO_DEV_PROVIDER_ID, LOGO_DEV_PROVIDER_ID, "publishable_key", self.client,
             "Logo.dev attribution required; image bytes remain session-only."),
        )
        attempted = False
        last_status = CompanyLogoStatus.UNCONFIGURED
        last_retry: datetime | None = None
        for source_id, credential_id, field, provider_client, license_text in providers:
            credential = self._credential_value(credential_id, field)
            if not credential:
                continue
            attempted = True
            if not force and self.repository.retry_blocked(normalized_symbol, normalized_exchange, source_id, now):
                state = self.repository.load(normalized_symbol, normalized_exchange, source_id)
                last_status = _state_status(state.status if state else "")
                last_retry = state.retry_after_utc if state else None
                continue
            try:
                fetched = provider_client.fetch(
                    normalized_symbol, normalized_exchange, credential, theme=normalized_theme
                )
            except LogoProviderError as exc:
                last_status = _status_for_error(exc.code)
                last_retry = now + (RATE_LIMIT_RETRY if last_status is CompanyLogoStatus.RATE_LIMITED else NEGATIVE_RETRY)
                self.repository.record(
                    symbol=normalized_symbol, exchange=normalized_exchange, provider_id=source_id,
                    status=last_status.value, attempted_at=now, retry_after=last_retry, error_code=exc.code,
                )
                self.company_repository.record_logo_result(
                    normalized_symbol,
                    source_id=source_id,
                    lookup_identifier=normalized_symbol,
                    source_url=_source_url(source_id),
                    content_sha256=None,
                    license_metadata=license_text,
                    success=False,
                    next_refresh_utc=last_retry,
                    error=exc.code,
                )
                continue

            digest = hashlib.sha256(fetched.content).hexdigest().upper()
            asset = CompanyLogoAsset(
                symbol=normalized_symbol,
                exchange=normalized_exchange,
                provider_id=source_id,
                status=CompanyLogoStatus.AVAILABLE,
                image_bytes=fetched.content,
                content_type=fetched.content_type,
                content_sha256=digest,
                fetched_at=now,
                message=f"Company logo loaded from {_SOURCE_LABELS[source_id]}.",
                source_url=fetched.source_url or _source_url(source_id),
                lookup_identifier=fetched.lookup_identifier or normalized_symbol,
                license_metadata=license_text,
                persistent_local_copy=False,
            )
            self.repository.record(
                symbol=normalized_symbol, exchange=normalized_exchange, provider_id=source_id,
                status=CompanyLogoStatus.AVAILABLE.value, attempted_at=now, success_at=now,
                content_type=fetched.content_type, content_sha256=digest,
            )
            self.company_repository.record_logo_result(
                normalized_symbol,
                source_id=source_id,
                lookup_identifier=asset.lookup_identifier,
                source_url=asset.source_url,
                content_sha256=digest,
                license_metadata=license_text,
                success=True,
                next_refresh_utc=now + timedelta(days=30),
            )
            self._remember(key, asset, now)
            return asset

        message = (
            "Configured company-logo sources are temporarily unavailable; showing ticker monogram."
            if attempted else
            "No optional company-logo credential or permitted local logo is available; showing ticker monogram."
        )
        return CompanyLogoAsset(
            symbol=normalized_symbol,
            exchange=normalized_exchange,
            provider_id="ticker_monogram",
            status=last_status,
            retry_after=last_retry,
            message=message,
            source_url="local://ticker-monogram",
            lookup_identifier=normalized_symbol,
            license_metadata="Project-rendered ticker text; no downloaded image bytes.",
        )

    def clear_session_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _resolve_local(self, symbol: str, exchange: str | None, now: datetime) -> CompanyLogoAsset | None:
        record = self.company_repository.resolve(symbol)
        if record is None or not record.local_logo_path:
            return None
        if record.logo_source_id not in PERSISTENT_LOCAL_SOURCES or not record.logo_license_metadata:
            return None
        loaded = self._load_safe_local_file(record)
        if loaded is None:
            return None
        content, content_type, digest = loaded
        return CompanyLogoAsset(
            symbol=symbol,
            exchange=exchange,
            provider_id=record.logo_source_id or "local_permitted",
            status=CompanyLogoStatus.AVAILABLE,
            image_bytes=content,
            content_type=content_type,
            content_sha256=digest,
            fetched_at=now,
            message=f"Company logo loaded from {_SOURCE_LABELS.get(record.logo_source_id or '', 'permitted local storage')}.",
            source_url=record.logo_source_url,
            lookup_identifier=record.logo_lookup_identifier or symbol,
            license_metadata=record.logo_license_metadata,
            persistent_local_copy=True,
        )

    def _load_safe_local_file(self, record: CompanyRecord) -> tuple[bytes, str, str] | None:
        relative = Path(str(record.local_logo_path))
        if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
            return None
        if relative.parts[0].lower() != "logos":
            return None
        candidate = self.data_root.joinpath(*relative.parts)
        try:
            root = self.logo_root.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != root and root not in resolved.parents:
            return None
        current = resolved
        while current != root:
            if is_link_or_reparse_point(current):
                return None
            current = current.parent
        if is_link_or_reparse_point(root) or not resolved.is_file():
            return None
        try:
            size = resolved.stat().st_size
            if size <= 0 or size > MAX_LOGO_BYTES or resolved.suffix.lower() not in SUPPORTED_LOCAL_SUFFIXES:
                return None
            content = resolved.read_bytes()
        except OSError:
            return None
        digest = hashlib.sha256(content).hexdigest().upper()
        if record.logo_content_sha256 and digest != record.logo_content_sha256.strip().upper():
            return None
        content_type = _validated_local_content_type(resolved.suffix.lower(), content)
        return (content, content_type, digest) if content_type else None

    def _credential_value(self, provider_id: str, field: str) -> str:
        try:
            credentials = self.credential_store.load(provider_id)
        except Exception:
            return ""
        return str(credentials.values.get(field, "")).strip() if credentials else ""

    def _remember(self, key: tuple[str, str, str], asset: CompanyLogoAsset, now: datetime) -> None:
        with self._lock:
            self._cache[key] = (now + SESSION_CACHE_TTL, asset)
            self._cache.move_to_end(key)
            while len(self._cache) > SESSION_CACHE_LIMIT:
                self._cache.popitem(last=False)

    @staticmethod
    def _cache_key(symbol: str, exchange: str | None, theme: str) -> tuple[str, str, str]:
        return (
            str(symbol).strip().upper(), str(exchange or "").strip().upper(),
            "light" if theme == "light" else "dark",
        )

    def _utc_now(self) -> datetime:
        now = self._now()
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _validated_local_content_type(suffix: str, content: bytes) -> str | None:
    if suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if suffix == ".webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if suffix == ".svg":
        try:
            text = content.decode("utf-8-sig").strip().lower()
        except UnicodeDecodeError:
            return None
        forbidden = ("<script", "<!entity", "<!doctype", "javascript:", "href=\"http", "href='http", "url(http")
        if "<svg" in text[:2048] and not any(token in text for token in forbidden):
            return "image/svg+xml"
    return None


def _source_url(source_id: str) -> str:
    return {
        FINNHUB_LOGO_PROVIDER_ID: FinnhubProfileLogoClient.profile_url,
        TWELVE_DATA_LOGO_PROVIDER_ID: TwelveDataLogoClient.logo_url,
        LOGO_DEV_PROVIDER_ID: "https://img.logo.dev/ticker/",
    }[source_id]


def _state_status(value: str) -> CompanyLogoStatus:
    try:
        return CompanyLogoStatus(value)
    except ValueError:
        return CompanyLogoStatus.UNAVAILABLE


def _status_for_error(code: str) -> CompanyLogoStatus:
    if code == "not_found":
        return CompanyLogoStatus.NOT_FOUND
    if code == "rate_limited":
        return CompanyLogoStatus.RATE_LIMITED
    return CompanyLogoStatus.UNAVAILABLE
