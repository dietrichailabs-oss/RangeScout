"""Pure scheduling rules for incremental company and logo maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from concurrent.futures import Future
from threading import Event, RLock, Thread
from typing import Any, Callable


class CompanyUpdateSchedule(str, Enum):
    OFF = "off"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


def normalize_schedule(value: object, default: CompanyUpdateSchedule) -> CompanyUpdateSchedule:
    if isinstance(value, CompanyUpdateSchedule):
        return value
    try:
        return CompanyUpdateSchedule(str(value).strip().lower())
    except ValueError:
        return default


def schedule_interval(schedule: CompanyUpdateSchedule) -> timedelta | None:
    if schedule is CompanyUpdateSchedule.WEEKLY:
        return timedelta(days=7)
    if schedule is CompanyUpdateSchedule.MONTHLY:
        return timedelta(days=30)
    return None


def next_update_at(
    last_success: datetime | None,
    schedule: CompanyUpdateSchedule,
    now: datetime | None = None,
) -> datetime | None:
    interval = schedule_interval(schedule)
    if interval is None:
        return None
    if last_success is None:
        current = now or datetime.now(timezone.utc)
        return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    current = last_success if last_success.tzinfo else last_success.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) + interval


def is_update_due(last_success: datetime | None, schedule: CompanyUpdateSchedule, now: datetime | None = None) -> bool:
    due = next_update_at(last_success, schedule, now)
    if due is None:
        return False
    current = now or datetime.now(timezone.utc)
    current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) >= due


class RecurringMaintenanceScheduler:
    """Hourly lifecycle-owned due checks with independent, deduplicated jobs."""

    def __init__(
        self,
        repository: Any,
        maintenance: Any,
        settings_fn: Callable[[], Any],
        *,
        now_fn: Callable[[], datetime] | None = None,
        check_interval_seconds: float = 3600.0,
        retry_cooldown: timedelta = timedelta(hours=1),
    ) -> None:
        self.repository = repository
        self.maintenance = maintenance
        self.settings_fn = settings_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.check_interval_seconds = max(300.0, float(check_interval_seconds))
        self.retry_cooldown = max(timedelta(minutes=5), retry_cooldown)
        self._stop = Event()
        self._lock = RLock()
        self._thread: Thread | None = None
        self._inflight: dict[str, Future[Any]] = {}
        self._last_attempt: dict[str, datetime] = {}

    def start(self) -> Future[Any] | None:
        started = self.check_due()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = Thread(
                    target=self._run,
                    name="rangescout-company-schedule",
                    daemon=True,
                )
                self._thread.start()
        return started.get("company_metadata")

    def check_due(self, now: datetime | None = None) -> dict[str, Future[Any]]:
        current = _as_utc(now or self._now())
        settings = self.settings_fn()
        schedules = {
            "company_metadata": normalize_schedule(
                getattr(settings, "company_update_schedule", None), CompanyUpdateSchedule.WEEKLY
            ),
            "logos": normalize_schedule(
                getattr(settings, "logo_refresh_schedule", None), CompanyUpdateSchedule.MONTHLY
            ),
        }
        starters = {
            "company_metadata": self.maintenance.refresh_companies,
            "logos": self.maintenance.refresh_logos,
        }
        started: dict[str, Future[Any]] = {}
        with self._lock:
            for kind, schedule in schedules.items():
                if schedule is CompanyUpdateSchedule.OFF:
                    continue
                existing = self._inflight.get(kind)
                if existing is not None and not existing.done():
                    continue
                if existing is not None:
                    self._inflight.pop(kind, None)
                last_attempt = self._last_attempt.get(kind)
                if last_attempt is not None and current < last_attempt + self.retry_cooldown:
                    continue
                if not is_update_due(self.repository.last_success(kind), schedule, current):
                    continue
                future = starters[kind]()
                self._last_attempt[kind] = current
                self._inflight[kind] = future
                future.add_done_callback(lambda completed, job=kind: self._job_finished(job, completed))
                started[kind] = future
        return started

    def status(self, now: datetime | None = None) -> dict[str, object]:
        current = _as_utc(now or self._now())
        settings = self.settings_fn()
        result: dict[str, object] = {}
        for kind, raw_schedule in (
            ("company_metadata", getattr(settings, "company_update_schedule", None)),
            ("logos", getattr(settings, "logo_refresh_schedule", None)),
        ):
            default = CompanyUpdateSchedule.WEEKLY if kind == "company_metadata" else CompanyUpdateSchedule.MONTHLY
            schedule = normalize_schedule(raw_schedule, default)
            last_success = self.repository.last_success(kind)
            due = next_update_at(last_success, schedule, current)
            last_attempt = self._last_attempt.get(kind)
            if due is not None and due <= current and last_attempt is not None:
                retry_at = last_attempt + self.retry_cooldown
                if retry_at > current:
                    due = retry_at
            with self._lock:
                running = kind in self._inflight and not self._inflight[kind].done()
            result[kind] = {
                "schedule": schedule.value,
                "last_success_utc": last_success.isoformat() if last_success else None,
                "next_due_utc": due.isoformat() if due else None,
                "running": running,
            }
        return result

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval_seconds):
            try:
                self.check_due()
            except Exception:
                # The next low-frequency tick retries; provider details remain in
                # the maintenance run record rather than leaking into logs.
                continue

    def _job_finished(self, kind: str, future: Future[Any]) -> None:  # noqa: ARG002
        with self._lock:
            if self._inflight.get(kind) is future:
                self._inflight.pop(kind, None)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
