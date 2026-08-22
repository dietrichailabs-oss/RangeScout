"""Bounded concurrent provider race with validation, health, and caching."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event, RLock, get_ident
from time import monotonic

from app.market_data.cache import ResultCache
from app.market_data.contracts import Capability, FabricRequest, FabricResult, RateLimited
from app.market_data.health import ProviderHealth
from app.market_data.execution import ProviderExecutionGate, RequestCancelled
from app.market_data.reconciliation import discrepancy_warning
from app.market_data.registry import FabricRegistry
from app.market_data.validation import ResultValidationError, validate_result


class NoEligibleProvider(RuntimeError):
    pass


class _CombinedCancellation:
    def __init__(self, internal: Event, external: Event | None) -> None:
        self.internal = internal
        self.external = external

    def is_set(self) -> bool:
        return self.internal.is_set() or bool(self.external and self.external.is_set())


class MarketDataRouter:
    def __init__(
        self,
        registry: FabricRegistry,
        *,
        max_workers: int = 8,
        max_fanout: int = 3,
        cache: ResultCache | None = None,
        health: ProviderHealth | None = None,
    ) -> None:
        self.registry = registry
        self.max_fanout = max(1, max_fanout)
        self.cache = cache if cache is not None else ResultCache()
        self.health = health if health is not None else ProviderHealth()
        self._background_executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="rangescout-provider-background"
        )
        self._quote_executor = ThreadPoolExecutor(
            max_workers=max(2, self.max_fanout), thread_name_prefix="rangescout-provider-quote"
        )
        self._closed = False
        self._lock = RLock()
        self._gates: dict[str, ProviderExecutionGate] = {}
        self._last_diagnostic: dict[str, object] = {}
        self._diagnostics_by_request: dict[str, dict[str, object]] = {}
        self._attempts_by_request: dict[str, list[dict[str, object]]] = {}

    def _ranked(self, request: FabricRequest, forced_provider_id: str | None = None):
        if forced_provider_id:
            try:
                forced = self.registry.get(forced_provider_id)
            except KeyError:
                return []
            descriptor = forced.descriptor
            candidates = [forced] if (
                descriptor.enabled
                and request.asset_class in descriptor.asset_classes
                and request.capability in descriptor.capabilities
            ) else []
        else:
            candidates = self.registry.eligible(request.asset_class, request.capability)
        eligible = []
        for adapter in candidates:
            if adapter.descriptor.requires_credentials and not adapter.health_check():
                continue
            window = self.health.window(
                adapter.descriptor.provider_id, request.asset_class.value, request.capability.value
            )
            try:
                rate_state = adapter.rate_limit_state()
            except Exception:
                rate_state = None
            if rate_state is not None and rate_state.limited:
                window.rate_limited(rate_state.retry_after_seconds)
                continue
            if window.allow():
                metrics = window.metrics()
                latency = metrics["p50_latency_ms"] if metrics["p50_latency_ms"] is not None else 10_000.0
                success = float(metrics["success_rate"])
                credential_bias = 100.0 if adapter.descriptor.requires_credentials else 0.0
                score = latency - (success * 500.0) + credential_bias
                eligible.append((score, adapter.descriptor.provider_id, adapter))
        eligible.sort(key=lambda item: (item[0], item[1]))
        limit = 1 if forced_provider_id else self.max_fanout
        return [item[2] for item in eligible[:limit]]

    def fetch(
        self,
        request: FabricRequest,
        *,
        cross_check: bool = False,
        budget_seconds: float | None = None,
        forced_provider_id: str | None = None,
        cancellation_event: Event | None = None,
    ) -> FabricResult:
        fetch_thread_id = get_ident()
        selection_started = monotonic()
        request_started_utc = datetime.now(timezone.utc).isoformat()
        if cancellation_event is not None and cancellation_event.is_set():
            raise RequestCancelled("Provider request was cancelled.")
        cached = self.cache.get(request)
        if cached is not None and (not forced_provider_id or cached.provider_id == forced_provider_id):
            metrics = self.health.window(
                cached.provider_id, request.asset_class.value, request.capability.value
            ).metrics()
            with self._lock:
                self._last_diagnostic = {
                    "request_id": request.request_id, "symbol": request.canonical_symbol,
                    "winning_provider": cached.provider_id, "cache": "hit", "latency_ms": 0.0,
                    "provider_timestamp": cached.provider_timestamp.isoformat() if cached.provider_timestamp else None,
                    "fallback_reason": None, "delay_class": cached.delay_class.value,
                    "capability": request.capability.value, "fetch_thread_id": fetch_thread_id,
                    "request_started_utc": request_started_utc,
                    "routing_mode": forced_provider_id or "smart",
                    "request_finished_utc": datetime.now(timezone.utc).isoformat(), "attempts": [],
                    "total_wall_clock_ms": round((monotonic() - selection_started) * 1000.0, 3),
                    "outcome": "fresh",
                    "circuit_state": metrics.get("circuit_state"),
                    "rate_limit_state": metrics.get("rate_limited_until") or "available",
                }
                self._remember_diagnostic(request.request_id)
            return replace(cached, request_id=request.request_id, warnings=cached.warnings + ("cache hit",))
        with self._lock:
            if self._closed:
                raise RuntimeError("Market-data router is shut down.")
        adapters = self._ranked(request, forced_provider_id)
        if not adapters:
            if forced_provider_id:
                name = forced_provider_id.replace("_", " ").title()
                raise NoEligibleProvider(
                    f"{name} is unavailable or unsupported for this request. Choose another provider or switch to Smart Search."
                )
            raise NoEligibleProvider("No authorized healthy provider can satisfy the request.")
        internal_cancelled = Event()
        cancelled = _CombinedCancellation(internal_cancelled, cancellation_event)
        with self._lock:
            self._attempts_by_request[request.request_id] = []
        executor = self._quote_executor if request.capability == Capability.QUOTE else self._background_executor
        submitted = monotonic()
        started: dict[Future[FabricResult], tuple[object, float]] = {
            executor.submit(self._execute, adapter, request, cancelled, submitted): (adapter, monotonic())
            for adapter in adapters
        }
        valid: list[FabricResult] = []
        failures: list[str] = []
        budget = max(0.05, float(budget_seconds)) if budget_seconds is not None else None
        try:
            pending = set(started)
            deadline = monotonic() + budget if budget is not None else None
            while pending:
                if cancelled.is_set():
                    raise RequestCancelled("Provider request was cancelled.")
                timeout = 0.025
                if deadline is not None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    timeout = min(timeout, remaining)
                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    adapter, began = started[future]
                    provider_id = adapter.descriptor.provider_id
                    window = self.health.window(provider_id, request.asset_class.value, request.capability.value)
                    latency_ms = (monotonic() - began) * 1000.0
                    try:
                        result = future.result()
                        current = self.registry.get(provider_id)
                        if current is not adapter:
                            raise ResultValidationError("Provider was disabled or replaced during request.")
                        validate_result(request, result)
                    except RateLimited as exc:
                        window.rate_limited(exc.retry_after_seconds)
                        failures.append(f"{provider_id}: rate limited")
                        continue
                    except ResultValidationError as exc:
                        window.failure(exc.kind)
                        failures.append(f"{provider_id}: {exc}")
                        continue
                    except TimeoutError:
                        window.failure("timeout")
                        failures.append(f"{provider_id}: timeout")
                        continue
                    except Exception as exc:
                        window.failure("failure")
                        failures.append(f"{provider_id}: {type(exc).__name__}")
                        continue
                    window.success(latency_ms)
                    valid.append(result)
                    if not cross_check or len(valid) >= 2:
                        break
                if valid and (not cross_check or len(valid) >= 2):
                    break
            if pending and not valid:
                for future, (adapter, _began) in started.items():
                    if not future.done():
                        provider_id = adapter.descriptor.provider_id
                        self.health.window(
                            provider_id, request.asset_class.value, request.capability.value
                        ).failure("budget_timeout")
                        failures.append(f"{provider_id}: request budget exceeded")
            if not valid:
                if forced_provider_id:
                    name = forced_provider_id.replace("_", " ").title()
                    raise NoEligibleProvider(
                        f"{name} failed this request. RangeScout did not fall back because forced-provider mode is active."
                    )
                raise NoEligibleProvider("All eligible providers failed validation: " + "; ".join(failures))
            winner = valid[0]
            warnings = list(winner.warnings)
            if len(valid) > 1:
                warning = discrepancy_warning(winner, valid[1])
                if warning:
                    warnings.append(warning)
            winner = replace(winner, warnings=tuple(warnings))
            self.cache.put(request, winner, datetime.now(timezone.utc))
            winner_metrics = self.health.window(
                winner.provider_id, request.asset_class.value, request.capability.value
            ).metrics()
            with self._lock:
                self._last_diagnostic = {
                    "request_id": request.request_id, "symbol": request.canonical_symbol,
                    "winning_provider": winner.provider_id, "cache": "miss",
                    "latency_ms": winner_metrics.get("p50_latency_ms"),
                    "provider_timestamp": winner.provider_timestamp.isoformat() if winner.provider_timestamp else None,
                    "fallback_reason": "; ".join(failures) if failures else None,
                    "circuit_state": winner_metrics.get("circuit_state"),
                    "rate_limit_state": winner_metrics.get("rate_limited_until") or "available",
                    "delay_class": winner.delay_class.value,
                    "capability": request.capability.value, "fetch_thread_id": fetch_thread_id,
                    "request_started_utc": request_started_utc,
                    "routing_mode": forced_provider_id or "smart",
                    "request_finished_utc": datetime.now(timezone.utc).isoformat(),
                    "total_wall_clock_ms": round((monotonic() - selection_started) * 1000.0, 3),
                    "outcome": "fresh",
                    "attempts": [dict(item) for item in self._attempts_by_request.get(request.request_id, ())],
                }
                self._remember_diagnostic(request.request_id)
            return winner
        except (RequestCancelled, NoEligibleProvider) as exc:
            outcome = "cancelled" if isinstance(exc, RequestCancelled) else "timeout"
            with self._lock:
                self._last_diagnostic = {
                    "request_id": request.request_id,
                    "symbol": request.canonical_symbol,
                    "winning_provider": None,
                    "cache": "miss",
                    "capability": request.capability.value,
                    "fetch_thread_id": fetch_thread_id,
                    "request_started_utc": request_started_utc,
                    "request_finished_utc": datetime.now(timezone.utc).isoformat(),
                    "routing_mode": forced_provider_id or "smart",
                    "total_wall_clock_ms": round((monotonic() - selection_started) * 1000.0, 3),
                    "outcome": outcome,
                    "attempts": [dict(item) for item in self._attempts_by_request.get(request.request_id, ())],
                }
                self._remember_diagnostic(request.request_id)
            raise
        finally:
            internal_cancelled.set()
            for future in started:
                if not future.done():
                    future.cancel()

    def _execute(self, adapter, request: FabricRequest, cancelled, submitted: float) -> FabricResult:
        provider_id = adapter.descriptor.provider_id
        with self._lock:
            gate = self._gates.get(provider_id)
            if gate is None or gate.adapter is not adapter:
                if gate is not None:
                    gate.close()
                gate = ProviderExecutionGate(adapter)
                self._gates[provider_id] = gate
        began = monotonic()
        attempt = {
            "provider_id": provider_id,
            "thread_id": get_ident(),
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "outcome": "running",
            "executor_queue_wait_ms": round((began - submitted) * 1000.0, 3),
        }
        with self._lock:
            self._attempts_by_request.setdefault(request.request_id, []).append(attempt)
        try:
            override = dict(request.provider_symbol_overrides).get(provider_id)
            adapter_request = replace(request, canonical_symbol=override) if override else request
            result = gate.execute(adapter_request, cancelled, attempt)
            if override:
                result = replace(
                    result, canonical_instrument_id=request.canonical_instrument_id,
                    canonical_symbol=request.canonical_symbol,
                )
        except RequestCancelled:
            with self._lock:
                attempt.update(
                    finished_utc=datetime.now(timezone.utc).isoformat(),
                    latency_ms=round((monotonic() - began) * 1000.0, 3),
                    outcome="cancelled",
                )
            raise
        except Exception as exc:
            with self._lock:
                attempt.update(
                    finished_utc=datetime.now(timezone.utc).isoformat(),
                    latency_ms=round((monotonic() - began) * 1000.0, 3),
                    outcome="failed",
                    error_type=type(exc).__name__,
                )
            raise
        with self._lock:
            attempt.update(
                finished_utc=datetime.now(timezone.utc).isoformat(),
                latency_ms=round((monotonic() - began) * 1000.0, 3),
                outcome="success",
                provider_timestamp=result.provider_timestamp.isoformat() if result.provider_timestamp else None,
                delay_class=result.delay_class.value,
            )
        return result

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            gates = tuple(self._gates.values())
        for gate in gates:
            gate.close()
        self._quote_executor.shutdown(wait=wait, cancel_futures=True)
        self._background_executor.shutdown(wait=wait, cancel_futures=True)

    def _remember_diagnostic(self, request_id: str) -> None:
        self._diagnostics_by_request[request_id] = dict(self._last_diagnostic)
        while len(self._diagnostics_by_request) > 64:
            oldest = next(iter(self._diagnostics_by_request))
            self._diagnostics_by_request.pop(oldest, None)
            self._attempts_by_request.pop(oldest, None)

    def diagnostics(self, request_id: str | None = None) -> dict[str, object]:
        with self._lock:
            diagnostic = self._diagnostics_by_request.get(request_id) if request_id else self._last_diagnostic
            result = dict(diagnostic or {})
            if request_id and request_id in self._attempts_by_request:
                result["attempts"] = [dict(item) for item in self._attempts_by_request[request_id]]
            return result

    def __enter__(self) -> "MarketDataRouter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
