from __future__ import annotations

from app.application.active_symbol import ActiveSymbolController, normalize_symbol


def test_active_symbol_normalization_and_generation() -> None:
    controller = ActiveSymbolController("aapl")
    initial = controller.request(source="market")
    assert initial.symbol == "AAPL"
    assert initial.generation == 0

    changed = controller.set(" msft ", source="watchlist")
    assert changed.symbol == "MSFT"
    assert changed.source == "watchlist"
    assert changed.generation == 1
    assert not controller.accepts(initial)


def test_same_symbol_does_not_invalidate_in_flight_work() -> None:
    controller = ActiveSymbolController("BRK.B")
    request = controller.request(source="research")
    state = controller.set("brk.b", source="ticker")
    assert state.generation == 0
    assert controller.accepts(request)


def test_request_identity_is_unique_and_bound_to_source_and_timestamp() -> None:
    controller = ActiveSymbolController("NVDA")
    first = controller.request(source="sec-companyfacts")
    second = controller.request(source="yahoo-quote")
    assert second.request_id > first.request_id
    assert first.source == "sec-companyfacts"
    assert first.requested_at.tzinfo is not None


def test_symbol_validation_rejects_empty_and_unsupported_characters() -> None:
    for value in ("", "AAPL\\USD", "AAPL;DROP"):
        try:
            normalize_symbol(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected {value!r} to be rejected")



def test_normalize_symbol_accepts_exchange_preferred_separator():
    assert normalize_symbol("mtb$j") == "MTB$J"
