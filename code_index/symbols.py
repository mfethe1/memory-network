"""Stable symbol and chunk identity.

`symbol_uid` is a deterministic declaration key, not a refactor-durable
identifier. It is computed as:

    sha1("{language}\x1f{kind}\x1f{canonical_name}\x1f{signature_norm}\x1f{container_uid}")[:20]

It is stable across re-parses of the same declaration and independent of
file path and line numbers (so moving a function to a sibling file keeps
its UID). It DOES change whenever canonical_name, signature, container,
kind, or language change. Explicit refactors can migrate identity in
place via `code_index update --rename-map PATH` — that path preserves
`symbol_pk` so downstream FK references in relations, occurrences,
chunks.primary_symbol_pk, and test_edges survive.

chunk_uid is secondary and scoped per file + chunk type + primary symbol, so
the same symbol rendered twice (e.g. a stub plus an implementation) gets
distinct chunk_uids.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from code_index.hashing import short_uid

_SEP = "\x1f"


@dataclass(frozen=True)
class SymbolIdentity:
    language: str
    kind: str
    canonical_name: str
    signature_norm: str = ""
    container_uid: str = ""

    @property
    def symbol_uid(self) -> str:
        payload = _SEP.join(
            (
                self.language,
                self.kind,
                self.canonical_name,
                self.signature_norm,
                self.container_uid,
            )
        )
        return short_uid(payload)


def make_chunk_uid(
    *,
    file_path: str,
    chunk_type: str,
    symbol_uid: str,
    symbol_path: str,
    occurrence_index: int = 0,
) -> str:
    payload = _SEP.join(
        (
            file_path,
            chunk_type,
            symbol_uid,
            symbol_path,
            str(occurrence_index),
        )
    )
    return short_uid(payload)


def normalize_signature(raw: str | None) -> str:
    if not raw:
        return ""
    return " ".join(raw.split())


def rename_symbol(
    conn: sqlite3.Connection,
    *,
    old_canonical: str,
    new_canonical: str,
) -> bool:
    """Opt-in identity migration across an explicit refactor.

    Finds the live symbol row matching `old_canonical`, preserves its
    `symbol_pk` (so every FK edge in relations, occurrences,
    chunks.primary_symbol_pk, and test_edges continues to resolve),
    rewrites `canonical_name`, and recomputes `symbol_uid` using the
    canonical `SymbolIdentity` recipe. Container UID is derived from the
    current `container_symbol_pk` (if any) by reading that row's
    `symbol_uid` — keeping the new UID consistent with what the reindex
    pipeline would produce for the renamed declaration.

    Returns True on success, False if no live symbol matches `old_canonical`.
    Raises `sqlite3.IntegrityError` if the new UID collides with a different
    existing symbol row (caller must resolve the merge explicitly).

    Known limitations:
    - matches by canonical_name only; if two live rows share the same
      canonical_name across different kind/language/signature/container,
      the first match wins. Call twice with more-qualified names if needed.
    - parent/child renames are order-sensitive — rename the parent
      (class/module) BEFORE the children so the children recompute against
      the new container UID.
    - does not rebuild derived UID strings copied into
      `test_edges.path_json`, `chunk_edits.symbol_uid`, or
      `unresolved_calls.src_symbol_uid`. Run `code_index update --all`
      (or targeted `--files` for affected paths) after the migration to
      refresh those projections.
    - does not handle container moves (`pkg.a.foo` → `pkg.b.foo` under a
      different parent). Express such refactors as a rename-map entry
      plus a reindex of the moved file; the reindex will re-home the
      symbol via its new file's imports.
    """
    row = conn.execute(
        "SELECT symbol_pk, language, kind, signature_norm, container_symbol_pk "
        "FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL",
        (old_canonical,),
    ).fetchone()
    if row is None:
        return False

    container_uid = ""
    container_pk = row["container_symbol_pk"]
    if container_pk is not None:
        crow = conn.execute(
            "SELECT symbol_uid FROM symbols WHERE symbol_pk = ?",
            (container_pk,),
        ).fetchone()
        if crow is not None:
            container_uid = crow["symbol_uid"] or ""

    new_uid = SymbolIdentity(
        language=row["language"] or "",
        kind=row["kind"],
        canonical_name=new_canonical,
        signature_norm=row["signature_norm"] or "",
        container_uid=container_uid,
    ).symbol_uid

    conn.execute(
        "UPDATE symbols SET canonical_name = ?, symbol_uid = ? WHERE symbol_pk = ?",
        (new_canonical, new_uid, row["symbol_pk"]),
    )
    return True
