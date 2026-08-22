from app.ui.runner import STARTUP_TAB_INDEXES


def test_startup_tab_mapping_matches_current_production_tab_order() -> None:
    assert STARTUP_TAB_INDEXES == {
        "market": 0,
        "live-trader": 1,
        "research": 2,
        "watchlists": 3,
        "scanner": 4,
        "alerts": 5,
        "notes": 6,
        "exports": 7,
        "settings": 8,
    }
