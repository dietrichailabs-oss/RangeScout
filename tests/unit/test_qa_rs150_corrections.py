from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timezone
from types import SimpleNamespace

from app.company_data.scheduler import RecurringMaintenanceScheduler


class _Repository:
    def __init__(self, company: datetime | None, logos: datetime | None) -> None:
        self.values = {"company_metadata": company, "logos": logos}

    def last_success(self, kind: str) -> datetime | None:
        return self.values[kind]


class _Maintenance:
    def __init__(self, repository: _Repository, now_fn) -> None:  # noqa: ANN001
        self.repository = repository
        self.now_fn = now_fn
        self.company_calls = 0
        self.logo_calls = 0

    def refresh_companies(self):
        self.company_calls += 1
        self.repository.values["company_metadata"] = self.now_fn()
        future = Future()
        future.set_result({"kind": "company_metadata"})
        return future

    def refresh_logos(self):
        self.logo_calls += 1
        self.repository.values["logos"] = self.now_fn()
        future = Future()
        future.set_result({"kind": "logos"})
        return future


def test_recurring_schedule_crosses_weekly_monthly_and_off_without_restart() -> None:
    clock = {"now": datetime(2026, 8, 7, 12, tzinfo=timezone.utc)}
    settings = SimpleNamespace(company_update_schedule="weekly", logo_refresh_schedule="monthly")
    repository = _Repository(
        datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    )
    maintenance = _Maintenance(repository, lambda: clock["now"])
    scheduler = RecurringMaintenanceScheduler(
        repository, maintenance, lambda: settings, now_fn=lambda: clock["now"]
    )

    assert scheduler.check_due() == {}
    clock["now"] = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    assert set(scheduler.check_due()) == {"company_metadata"}
    assert maintenance.company_calls == 1 and maintenance.logo_calls == 0
    assert scheduler.check_due() == {}

    settings.company_update_schedule = "off"
    clock["now"] = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    assert set(scheduler.check_due()) == {"logos"}
    assert maintenance.company_calls == 1 and maintenance.logo_calls == 1
    assert scheduler.check_due() == {}

    settings.logo_refresh_schedule = "off"
    clock["now"] = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    assert scheduler.check_due() == {}
    assert maintenance.company_calls == 1 and maintenance.logo_calls == 1


def test_schedule_change_rearms_due_behavior_without_restart_and_status_is_independent() -> None:
    clock = {"now": datetime(2026, 9, 1, 12, tzinfo=timezone.utc)}
    settings = SimpleNamespace(company_update_schedule="off", logo_refresh_schedule="off")
    repository = _Repository(
        datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    maintenance = _Maintenance(repository, lambda: clock["now"])
    scheduler = RecurringMaintenanceScheduler(
        repository, maintenance, lambda: settings, now_fn=lambda: clock["now"]
    )
    assert scheduler.check_due() == {}

    settings.company_update_schedule = "weekly"
    assert set(scheduler.check_due()) == {"company_metadata"}
    settings.logo_refresh_schedule = "monthly"
    status = scheduler.status()
    company = status["company_metadata"]
    logos = status["logos"]
    assert company["last_success_utc"].startswith("2026-09-01")
    assert company["next_due_utc"].startswith("2026-09-08")
    assert logos["last_success_utc"].startswith("2026-08-20")
    assert logos["next_due_utc"].startswith("2026-09-19")
    assert company["next_due_utc"] != logos["next_due_utc"]


def test_recurring_scheduler_deduplicates_inflight_and_stops_cleanly() -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    settings = SimpleNamespace(company_update_schedule="weekly", logo_refresh_schedule="off")
    repository = _Repository(datetime(2026, 1, 1, tzinfo=timezone.utc), None)

    class PendingMaintenance:
        def __init__(self) -> None:
            self.calls = 0
            self.future: Future[object] = Future()

        def refresh_companies(self):
            self.calls += 1
            return self.future

        def refresh_logos(self):
            raise AssertionError("logo refresh is Off")

    maintenance = PendingMaintenance()
    scheduler = RecurringMaintenanceScheduler(
        repository, maintenance, lambda: settings, now_fn=lambda: now
    )
    assert set(scheduler.check_due()) == {"company_metadata"}
    assert scheduler.check_due() == {}
    assert maintenance.calls == 1
    scheduler.start()
    assert maintenance.calls == 1
    scheduler.shutdown()
    maintenance.future.set_result({})
