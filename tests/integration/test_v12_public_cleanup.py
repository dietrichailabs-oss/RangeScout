from __future__ import annotations

from pathlib import Path

from app.application.bootstrap import RangeScoutApplication
from app.configuration.settings import load_user_settings
from app.providers.registry import default_provider_registry


PUBLIC_TEXT_TARGETS = (
    "README.md",
    "scripts/package_release.py",
    "docs/MARKET_DATA_NOTICE.md",
    "docs/PRIVACY_AND_DATA_USE.md",
    "docs/PROVIDER_CREDENTIAL_SECURITY.md",
    "docs/architecture.md",
    "docs/provider-review.md",
    "docs/PROVIDER_STREAMING_REVIEW.md",
    "packaging/windows/RangeScout.iss",
)


def test_public_provider_registry_and_runtime_have_only_approved_providers(tmp_path: Path) -> None:
    registry = default_provider_registry()
    assert registry.list_available() == ["yahoo", "finnhub"]
    app = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app.registry.list_available() == ["yahoo", "finnhub"]
    finally:
        app.store.close()


def test_legacy_offline_provider_selection_migrates_safely(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        '{"default_provider":"mock","provider_policy_version":4,"theme":"dark"}', encoding="utf-8"
    )
    settings = load_user_settings(str(tmp_path))
    assert settings.default_provider == "yahoo"
    assert settings.provider_policy_version == 6


def test_public_runtime_and_current_release_text_have_no_removed_provider_terms() -> None:
    root = Path(__file__).resolve().parents[2]
    targets = [path for path in (root / "app").rglob("*.py")]
    targets.extend(root / relative for relative in PUBLIC_TEXT_TARGETS)
    forbidden = ("mock", "simulated provider", "demo provider", "offline deterministic")
    violations = []
    for path in targets:
        text = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            if term in text:
                violations.append(f"{path.relative_to(root).as_posix()}: {term}")
    assert violations == []
