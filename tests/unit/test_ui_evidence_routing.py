from scripts.handoff.capture_ui_surfaces import _normalized_tab_name


def test_ui_evidence_tab_names_normalize_case_spaces_hyphens_and_underscores() -> None:
    assert _normalized_tab_name("Live Trader") == _normalized_tab_name("live-trader")
    assert _normalized_tab_name("Market") == _normalized_tab_name("market")
    assert _normalized_tab_name("official_feed") == "official feed"

