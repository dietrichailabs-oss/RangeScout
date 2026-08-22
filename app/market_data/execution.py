"""Cross-request provider execution gates for concurrency and start pacing."""

from __future__ import annotations

from threading import Condition
from time import monotonic
from typing import Callable

from app.market_data.contracts import Capability, FabricRequest, FabricResult, MarketDataAdapter, RateLimited


class RequestCancelled(RuntimeError):
    """A caller superseded a request before it could produce a visible result."""


class ProviderExecutionGate:
    def __init__(
        self,
        adapter: MarketDataAdapter,
        *,
        max_queue: int | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        descriptor = adapter.descriptor
        self.adapter = adapter
        self._capacity = max(1, descriptor.max_concurrency)
        # Background work may not consume every provider slot when the provider
        # can run concurrent requests. A quote always has one reserved lane.
        self._background_capacity = max(1, self._capacity - 1)
        self._minimum_interval = max(0.0, descriptor.minimum_request_interval_seconds)
        self._max_queue = max_queue or max(8, descriptor.max_concurrency * 8)
        self._clock = clock
        self._condition = Condition()
        self._waiting = 0
        self._waiting_interactive = 0
        self._active = 0
        self._active_background = 0
        self._last_start: float | None = None
        self._closed = False

    def execute(self, request: FabricRequest, cancelled, timings: dict[str, object] | None = None) -> FabricResult:
        interactive = request.capability == Capability.QUOTE
        queue_began = self._clock()
        self._enter_queue(cancelled, interactive)
        queued = True
        acquired = False
        try:
            with self._condition:
                while not self._can_start(interactive):
                    self._assert_active(cancelled)
                    self._condition.wait(timeout=0.025)
                self._assert_active(cancelled)
                self._active += 1
                if not interactive:
                    self._active_background += 1
                acquired = True
            self._leave_queue(interactive)
            queued = False
            if timings is not None:
                timings["gate_wait_ms"] = round((self._clock() - queue_began) * 1000.0, 3)
            self._pace_start(cancelled)
            state = self.adapter.rate_limit_state()
            if state.limited:
                raise RateLimited(state.retry_after_seconds)
            self._assert_active(cancelled)
            adapter_began = self._clock()
            if timings is not None:
                timings["provider_started_monotonic"] = adapter_began
            try:
                return self.adapter.request(request)
            finally:
                if timings is not None:
                    timings["adapter_network_ms"] = round((self._clock() - adapter_began) * 1000.0, 3)
        finally:
            if queued:
                self._leave_queue(interactive)
            if acquired:
                with self._condition:
                    self._active -= 1
                    if not interactive:
                        self._active_background -= 1
                    self._condition.notify_all()

    def _can_start(self, interactive: bool) -> bool:
        if self._active >= self._capacity:
            return False
        if interactive:
            return True
        # With a single provider slot, give a waiting quote strict priority.
        if self._capacity == 1:
            return self._waiting_interactive == 0
        return self._active_background < self._background_capacity

    def _enter_queue(self, cancelled, interactive: bool) -> None:
        with self._condition:
            self._assert_active(cancelled)
            if self._waiting >= self._max_queue:
                raise RateLimited(1.0)
            self._waiting += 1
            if interactive:
                self._waiting_interactive += 1

    def _leave_queue(self, interactive: bool) -> None:
        with self._condition:
            self._waiting -= 1
            if interactive:
                self._waiting_interactive -= 1
            self._condition.notify_all()

    def _pace_start(self, cancelled) -> None:
        with self._condition:
            while self._last_start is not None:
                self._assert_active(cancelled)
                remaining = self._minimum_interval - (self._clock() - self._last_start)
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(0.05, remaining))
            self._assert_active(cancelled)
            self._last_start = self._clock()
            self._condition.notify_all()

    def _assert_active(self, cancelled) -> None:
        if self._closed:
            raise RuntimeError("Provider execution gate is shut down.")
        if cancelled.is_set():
            raise RequestCancelled("Provider request was cancelled.")

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
