"""Canonical, indexed natural-language normalization for instrument discovery."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterable


_APOSTROPHES = re.compile(r"['\u2018\u2019\u02bc\uff07]")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_LEGAL_SUFFIX = re.compile(
    r"\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group)\b\.?,?",
    re.IGNORECASE,
)
_PROVIDER_ALIAS_KINDS = frozenset(
    {"official_directory_symbol", "official_source_symbol_variant", "source_symbol", "provider_symbol"}
)


def normalize_search_text(value: object) -> str:
    """Return the sole stored/query/ranking representation for natural names."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _APOSTROPHES.sub("", text)
    text = text.replace("&", " and ")
    text = _LEGAL_SUFFIX.sub(" ", text)
    return _NON_ALPHANUMERIC.sub(" ", text).strip()


def normalize_search_compact(value: object) -> str:
    """Return the punctuation-deleted companion form without losing token boundaries."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _APOSTROPHES.sub("", text)
    text = text.replace("&", "and")
    return _NON_ALPHANUMERIC.sub("", text)


def optional_conjunction_variants(value: object) -> tuple[str, ...]:
    """Return safe name forms where a source ampersand may be omitted.

    The omission is generated only from an authoritative name/alias that
    actually contains ``&``. Query text is never globally stripped of the
    English word ``and``; this keeps unrelated natural-language searches from
    being conflated.
    """

    raw = unicodedata.normalize("NFKC", str(value or ""))
    if "&" not in raw:
        return ()
    omitted = raw.replace("&", " ")
    normalized = normalize_search_text(omitted)
    compact = normalize_search_compact(omitted)
    return tuple(dict.fromkeys(item for item in (normalized, compact) if item))


def indexed_search_variants(value: object) -> tuple[tuple[str, str], ...]:
    """Return canonical, compact and safe source-derived search documents."""

    normalized = normalize_search_text(value)
    compact = normalize_search_compact(value)
    variants: list[tuple[str, str]] = []
    if normalized:
        variants.append((normalized, "normalized"))
    if compact and compact != normalized:
        variants.append((compact, "compact"))
    for optional in optional_conjunction_variants(value):
        if optional not in {text for text, _kind in variants}:
            variants.append((optional, "optional_conjunction"))
    return tuple(variants)


def fts_prefix_query(value: object) -> str:
    """Build a safe token-prefix FTS query from canonical normalized text."""

    tokens = normalize_search_text(value).split()
    if not tokens:
        return ""
    escaped = [token.replace('"', '""') for token in tokens]
    return " AND ".join(
        f'"{token}"*' if index == len(escaped) - 1 else f'"{token}"'
        for index, token in enumerate(escaped)
    )


def rebuild_instrument_search_index(
    connection: sqlite3.Connection,
    instrument_ids: Iterable[int] | None = None,
) -> int:
    """Rebuild all or selected FTS documents inside the caller transaction."""

    instrument_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(rs_instruments)")
    }
    alias_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(rs_instrument_aliases)")
    }
    if not {"instrument_id", "security_name"}.issubset(instrument_columns):
        return 0
    selected = tuple(sorted({int(value) for value in instrument_ids or ()}))
    if selected:
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"DELETE FROM rs_instrument_search_fts WHERE instrument_id IN ({placeholders})", selected
        )
        instruments = connection.execute(
            f"SELECT instrument_id,security_name FROM rs_instruments WHERE instrument_id IN ({placeholders})",
            selected,
        ).fetchall()
        aliases = (
            connection.execute(
                f"SELECT instrument_id,alias_symbol,alias_kind FROM rs_instrument_aliases WHERE instrument_id IN ({placeholders})",
                selected,
            ).fetchall()
            if {"instrument_id", "alias_symbol", "alias_kind"}.issubset(alias_columns) else []
        )
    else:
        connection.execute("DELETE FROM rs_instrument_search_fts")
        instruments = connection.execute("SELECT instrument_id,security_name FROM rs_instruments").fetchall()
        aliases = (
            connection.execute("SELECT instrument_id,alias_symbol,alias_kind FROM rs_instrument_aliases").fetchall()
            if {"instrument_id", "alias_symbol", "alias_kind"}.issubset(alias_columns) else []
        )
    documents: set[tuple[int, str, str]] = set()
    for instrument_id, name in instruments:
        for text, variant_kind in indexed_search_variants(name):
            documents.add((int(instrument_id), text, f"security_name_{variant_kind}"))
    for instrument_id, alias, alias_kind in aliases:
        kind = str(alias_kind or "").strip().lower()
        if kind in _PROVIDER_ALIAS_KINDS:
            continue
        for text, variant_kind in indexed_search_variants(alias):
            documents.add((int(instrument_id), text, f"alias_{variant_kind}"))
    connection.executemany(
        "INSERT INTO rs_instrument_search_fts(instrument_id,normalized_text,source_kind) VALUES(?,?,?)",
        sorted(documents),
    )
    return len(documents)
