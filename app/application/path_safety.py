"""No-follow path safety checks for app-owned filesystem operations."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link_or_reparse_point(path: Path) -> bool:
    """Return True for links, junctions, or Windows reparse points without following them."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True

    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True

    if os.name != "nt":
        return False

    try:
        attributes = path.lstat().st_file_attributes
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
