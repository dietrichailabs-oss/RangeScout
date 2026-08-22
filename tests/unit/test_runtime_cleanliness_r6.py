from __future__ import annotations

from pathlib import Path

import app
from app.providers.public_policy import PUBLIC_CREDENTIAL_PROVIDER_IDS, PUBLIC_PROVIDER_IDS
from app.ui.main import RangeScoutWindow


FORBIDDEN_SHIPPING_MARKERS = (
    "RANGESCOUT_EVIDENCE_PROFILE",
    "apply_evidence_profile",
    "_evidence_ticker_values",
    "rangescout.r4-evidence-profile.v1",
    "disposable QA evidence profile",
)


def _shipping_application_source() -> str:
    app_root = Path(app.__file__).resolve().parent
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(app_root.rglob("*.py")))


def test_shipping_application_has_no_evidence_profile_hook_or_synthetic_state() -> None:
    source = _shipping_application_source()
    for marker in FORBIDDEN_SHIPPING_MARKERS:
        assert marker not in source
    assert not hasattr(RangeScoutWindow, "apply_evidence_profile")


def test_startup_automation_does_not_create_exports_or_load_json_profiles() -> None:
    runner_source = (Path(app.__file__).resolve().parent / "ui" / "runner.py").read_text(encoding="utf-8")
    assert "_on_export_csv" not in runner_source
    assert "json" not in runner_source
    assert "open(" not in runner_source
    assert "read_text(" not in runner_source


def test_public_provider_policy_remains_exactly_yahoo_and_finnhub() -> None:
    assert PUBLIC_PROVIDER_IDS == ("yahoo", "finnhub")
    assert PUBLIC_CREDENTIAL_PROVIDER_IDS == frozenset({"finnhub"})
    assert "alpaca" not in PUBLIC_PROVIDER_IDS
    assert "mock" not in PUBLIC_PROVIDER_IDS
