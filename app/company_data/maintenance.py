"""Bounded nonblocking company database and logo maintenance orchestration."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from app.company_data.repository import CompanyDatabaseRepository


class CompanyMaintenanceService:
    def __init__(self, repository: CompanyDatabaseRepository, discovery: Any, logo_service: Any) -> None:
        self.repository = repository
        self.discovery = discovery
        self.logo_service = logo_service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rangescout-company-maintenance")
        self._company_future: Future[Any] | None = None
        self._logo_future: Future[dict[str, int]] | None = None
        self._lock = RLock()

    def refresh_companies(self):
        with self._lock:
            if self._company_future is not None and not self._company_future.done():
                return self._company_future
            self._company_future = self.discovery.refresh_manual()
            self._company_future.add_done_callback(self._record_company_result)
            return self._company_future

    def _record_company_result(self, future) -> None:
        try:
            report = future.result()
        except Exception as exc:
            self.repository.record_update_run("company_metadata", status="failed", source_failures=1, error=str(exc))
            return
        self.repository.record_update_run(
            "company_metadata", status="complete", before=report.before_count, after=report.after_count,
            added=report.added, changed=report.changed, inactive=report.removed_inactive,
        )

    def refresh_logos(self, *, limit: int = 25) -> Future[dict[str, int]]:
        with self._lock:
            if self._logo_future is not None and not self._logo_future.done():
                return self._logo_future
            symbols = self.repository.due_logo_symbols(limit=limit)
            self._logo_future = self._executor.submit(self._refresh_logo_batch, symbols)
            return self._logo_future

    def _refresh_logo_batch(self, symbols: tuple[tuple[str, str], ...]) -> dict[str, int]:
        successes = failures = 0
        for symbol, venue in symbols:
            asset = self.logo_service.resolve(symbol, venue, force=True)
            if asset.has_image:
                successes += 1
            else:
                failures += 1
        self.repository.record_update_run(
            "logos", status="complete", logo_successes=successes, logo_failures=failures
        )
        return {"attempted": len(symbols), "successes": successes, "failures": failures}

    def status(self) -> dict[str, object]:
        status = asdict(self.repository.status())
        with self._lock:
            status["company_update_running"] = self._company_future is not None and not self._company_future.done()
            status["logo_update_running"] = self._logo_future is not None and not self._logo_future.done()
        return status

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
