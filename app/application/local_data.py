"""Local app-owned data deletion service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.application.path_safety import is_link_or_reparse_point
from app.domain.errors import LocalDataDeletionError


from app.historical_store.repository import HistoricalStore


ALLOWLISTED_LOCAL_FILES = (
    "history.sqlite",
    "history.sqlite-wal",
    "history.sqlite-shm",
    "settings.json",
    "watchlists.json",
    "notes.json",
    "research_cache",
    "temp",
)


@dataclass(frozen=True)
class LocalDataDeletionReport:
    data_root: str
    requested_targets: list[str]
    deleted_paths: list[str]
    missing_paths: list[str]
    failed_paths: dict[str, str]
    refused_unsafe_paths: dict[str, str]
    complete: bool


def _validate_root(root: Path) -> None:
    if is_link_or_reparse_point(root):
        raise LocalDataDeletionError(f"Unsafe data root (symlink/reparse): {root}")
    if root.is_file():
        raise LocalDataDeletionError(f"Data root is not a directory: {root}")


def _validate_target_name(name: str) -> bool:
    if not name or name in {"", "..", ".", "/", "\\"}:
        return False
    if any(ch in name for ch in ("/", "\\", "\x00")):
        return False
    if any(ch.isspace() for ch in name):
        return False
    if ".." in name:
        return False
    return name in ALLOWLISTED_LOCAL_FILES


def _safe_remove_child(path: Path, root: Path, refused: dict[str, str]) -> bool:
    if is_link_or_reparse_point(path):
        relative = str(path.relative_to(root)).replace("\\", "/")
        refused[relative] = "target is symlink/reparse"
        return False
    if path.is_dir():
        complete = True
        for child in sorted(path.iterdir(), reverse=True):
            complete = _safe_remove_child(child, root, refused) and complete
        if not complete:
            return False
        path.rmdir()
    else:
        path.unlink()
    return True


def delete_local_data(
    app_data_root: Path | str,
    *,
    targets: list[str] | None = None,
    store: HistoricalStore | None = None,
) -> LocalDataDeletionReport:
    root = Path(app_data_root)
    requested_targets = list(targets or ALLOWLISTED_LOCAL_FILES)
    _validate_root(root)

    if store is not None and "history.sqlite" in requested_targets:
        store.close()

    deleted: list[str] = []
    missing: list[str] = []
    failed: dict[str, str] = {}
    refused: dict[str, str] = {}

    for name in requested_targets:
        if not _validate_target_name(name):
            refused[name] = "target not in allowlist"
            continue

        path = root / name
        if is_link_or_reparse_point(path):
            refused[name] = "target is symlink/reparse"
            continue
        if not path.exists():
            missing.append(name)
            continue
        try:
            removed = _safe_remove_child(path, root, refused)
            if not removed:
                failed[name] = "unsafe descendant remains"
                continue
            if path.exists():
                failed[name] = "path was not deleted"
                continue
            deleted.append(name)
        except Exception as exc:  # pragma: no cover - exercised via focused unit regression
            failed[name] = str(exc)

    complete = not failed and not refused
    return LocalDataDeletionReport(
        data_root=str(root),
        requested_targets=requested_targets,
        deleted_paths=deleted,
        missing_paths=missing,
        failed_paths=failed,
        refused_unsafe_paths=refused,
        complete=complete,
    )
