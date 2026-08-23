"""Application composition root for Stage 0."""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from app.configuration.settings import AppSettings, load_default_settings, load_user_settings, save_user_settings
from app.application.path_safety import is_link_or_reparse_point
from app.historical_store.repository import HistoricalStore
from app.providers.registry import ProviderRegistry, default_provider_registry
from app.providers.public_policy import PUBLIC_PROVIDER_IDS, require_public_provider
from app.providers.base import MarketDataProvider
from app.providers.configuration import ProviderConfigurationService
from app.platform import platform_adapter
from app.domain.errors import DataRootError
from app.security.credentials import CredentialStore, default_credential_store
from app.market_data.providers.catalog import default_fabric_registry
from app.market_data.contracts import Capability
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter
from app.market_data.service import FabricMarketDataService
from app.market_data.discovery import DiscoveryCoordinator
from app.company_logos.service import CompanyLogoService
from app.company_data.repository import CompanyDatabaseRepository
from app.company_data.maintenance import CompanyMaintenanceService
from app.company_data.scheduler import RecurringMaintenanceScheduler
from app.company_data.master import provision_company_master
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver, InstrumentMatch
from app.application.local_snapshot import LocalSnapshotRepository


@dataclass
class RangeScoutApplication:
    data_dir: Path | str | None = None
    provider_id: str | None = None
    settings: AppSettings | None = None
    registry: ProviderRegistry | None = None
    credential_store: CredentialStore | None = None
    fabric_registry: FabricRegistry | None = None
    discovery_coordinator: DiscoveryCoordinator | None = None

    def __post_init__(self) -> None:
        if self.provider_id is not None:
            self.provider_id = require_public_provider(self.provider_id)
        requested_data_dir = Path(self.data_dir) if self.data_dir else Path(platform_adapter().app_data_dir)
        try:
            resolved_data_dir = self._provision_data_dir(requested_data_dir)
        except OSError as exc:
            raise DataRootError(f"Unable to prepare application data root: {requested_data_dir}") from exc
        except DataRootError:
            raise
        self.data_dir = resolved_data_dir
        self.store = HistoricalStore(self.data_dir / "history.sqlite")
        self.company_master_report = provision_company_master(self.store.path)
        self.instrument_reference_changes = InstrumentReferenceSeeder(self.store.path).apply()
        self.local_snapshots = LocalSnapshotRepository(self.store.path)
        self.settings = self.settings if self.settings is not None else load_user_settings(str(self.data_dir))
        self.credential_store = self.credential_store or default_credential_store()
        if self.registry is None:
            self.registry = default_provider_registry(
                timeout_seconds=self.settings.live_timeout_seconds,
                credential_store=self.credential_store,
            )
        else:
            public_registry = ProviderRegistry(PUBLIC_PROVIDER_IDS)
            for public_provider_id in PUBLIC_PROVIDER_IDS:
                try:
                    public_registry.register(self.registry.get(public_provider_id))
                except KeyError:
                    continue
            self.registry = public_registry
        self.fabric_registry = self.fabric_registry or default_fabric_registry(self.credential_store, self.registry)
        self.market_data_router = MarketDataRouter(self.fabric_registry)
        self.market_data_service = FabricMarketDataService(self.market_data_router, self.settings.provider_mode)
        self.discovery_coordinator = self.discovery_coordinator or DiscoveryCoordinator(self.store.path)
        self.company_logo_service = CompanyLogoService(self.store.path, self.credential_store)
        self.company_database = CompanyDatabaseRepository(self.store.path)
        self.company_maintenance = CompanyMaintenanceService(
            self.company_database, self.discovery_coordinator, self.company_logo_service
        )
        self.company_maintenance_scheduler = RecurringMaintenanceScheduler(
            self.company_database,
            self.company_maintenance,
            lambda: self.settings,
        )
        self.provider_configuration = ProviderConfigurationService(self.registry, self.credential_store)
        if self.settings.default_provider not in self.registry.list_available():
            fallback_provider = "yahoo"
            if fallback_provider not in self.registry.list_available():
                raise ValueError("The required Yahoo provider is not registered.")
            self.settings = replace(self.settings, default_provider=fallback_provider)
            self.persist_settings()
        self.provider_id = self.provider_id or self.settings.default_provider
        self.provider: MarketDataProvider = self.registry.get(self.provider_id)

    def persist_settings(self) -> None:
        save_user_settings(str(self.data_dir), self.settings)

    def get_provider(self, provider_id: str | None = None) -> MarketDataProvider:
        if provider_id is None:
            return self.provider
        return self.registry.get(provider_id)

    def fabric_provider_statuses(self) -> list[dict[str, object]]:
        statuses: list[dict[str, object]] = []
        for adapter in self.fabric_registry.snapshot():
            descriptor = adapter.descriptor
            configured = True
            if descriptor.requires_credentials:
                try:
                    configured = self.credential_store.load(descriptor.provider_id) is not None
                except Exception:
                    configured = False
            statuses.append(
                {
                    "provider_id": descriptor.provider_id,
                    "display_name": descriptor.display_name,
                    "enabled": descriptor.enabled,
                    "configured": configured,
                    "requires_credentials": descriptor.requires_credentials,
                    "delay_class": descriptor.delay_class.value,
                    "decision": descriptor.terms.decision,
                    "reason": descriptor.terms.reason,
                    "capabilities": sorted(value.value for value in descriptor.capabilities),
                }
            )
        return statuses

    def set_provider_mode(self, provider_mode: str) -> str:
        normalized = self.market_data_service.set_provider_mode(provider_mode)
        if self.settings.provider_mode != normalized:
            self.settings = replace(self.settings, provider_mode=normalized)
            self.persist_settings()
        return normalized

    def start_background_services(self):
        return self.company_maintenance_scheduler.start()

    def refresh_instrument_discovery(self):
        return self.discovery_coordinator.refresh_manual()

    def instrument_discovery_status(self) -> dict[str, object]:
        return self.discovery_coordinator.status()

    def company_logo_status(self) -> dict[str, object]:
        return self.company_logo_service.source_status()

    def company_database_status(self) -> dict[str, object]:
        return {
            **self.company_maintenance.status(),
            "schedule": self.company_maintenance_scheduler.status(),
        }

    def reevaluate_company_maintenance(self):
        return self.company_maintenance_scheduler.check_due()

    def refresh_company_database(self):
        return self.company_maintenance.refresh_companies()

    def refresh_company_logos(self):
        return self.company_maintenance.refresh_logos()

    def check_local_database(self) -> dict[str, object]:
        return self.company_database.health()

    def search_instruments(self, query: str, limit: int = 25) -> list[dict[str, object]]:
        return self.discovery_coordinator.search(query, limit)

    def discover_instruments(self, query: str, limit: int = 12) -> list[InstrumentMatch]:
        """Credentialed provider discovery fallback; callers run this off the UI thread."""

        resolver = InstrumentResolver(self.store.path)
        for adapter in self.fabric_registry.snapshot():
            descriptor = adapter.descriptor
            discover = getattr(adapter, "search_instruments", None)
            if (
                not descriptor.enabled
                or Capability.SYMBOL_SEARCH not in descriptor.capabilities
                or not callable(discover)
            ):
                continue
            try:
                if descriptor.requires_credentials and not adapter.health_check():
                    continue
                rows = discover(query, limit)
                resolver.enrich_provider_results(descriptor.provider_id, rows)
            except Exception:
                continue
        return resolver.search(query, limit)

    def shutdown(self) -> None:
        self.company_maintenance_scheduler.shutdown()
        self.company_maintenance.shutdown()
        self.discovery_coordinator.shutdown(wait=True)
        self.market_data_router.shutdown(wait=True)
        self.store.close()

    def _provision_data_dir(self, candidate: Path) -> Path:
        candidate = candidate.expanduser()
        if is_link_or_reparse_point(candidate):
            raise DataRootError(f"Application data root is unsafe: {candidate}")
        if candidate.exists():
            if not candidate.is_dir():
                raise DataRootError(f"Application data root is not a directory: {candidate}")

        candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.exists():
            raise DataRootError(f"Application data root is unavailable: {candidate}")
        if is_link_or_reparse_point(candidate):
            raise DataRootError(f"Application data root is unsafe: {candidate}")
        self._assert_path_writable(candidate)
        return candidate

    def _assert_path_writable(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            raise DataRootError(f"Data root must be an existing directory: {path}")
        if not os.access(path, os.W_OK):
            raise OSError(f"{path} is not writable")
        if os.name == "nt":
            file_attributes = getattr(path.stat(), "st_file_attributes", 0)
            if file_attributes and file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                raise OSError(f"{path} is marked read-only")
        probe = path / ".rangescout_write_check"
        try:
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
        finally:
            with contextlib.suppress(OSError):
                probe.unlink(missing_ok=True)
