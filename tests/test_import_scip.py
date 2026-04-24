from __future__ import annotations

import json
import textwrap
from pathlib import Path

from code_index import db as db_mod
from code_index.cli import main
from code_index.config import load as load_config
from code_index.scip_import import canonical_from_scip_symbol


FOO_SYMBOL = "scip-python python sample 0.1 pkg/mod/foo()."
IMPL_SYMBOL = "scip-python python sample 0.1 pkg/mod/Impl#"
PROTO_SYMBOL = "scip-python python sample 0.1 pkg/proto/Proto#"


def _write_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        textwrap.dedent(
            """
            def foo() -> int:
                return 1


            class Impl:
                pass


            value = foo()
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _write_scip_json(root: Path) -> Path:
    payload = {
        "metadata": {
            "toolInfo": {"name": "test-scip", "version": "0.0"},
            "projectRoot": str(root),
        },
        "externalSymbols": [
            {
                "symbol": PROTO_SYMBOL,
                "kind": "Class",
                "displayName": "Proto",
            }
        ],
        "documents": [
            {
                "language": "python",
                "relativePath": "pkg/mod.py",
                "symbols": [
                    {
                        "symbol": FOO_SYMBOL,
                        "kind": "Function",
                        "displayName": "foo",
                        "signatureDocumentation": {
                            "language": "python",
                            "text": "def foo() -> int",
                        },
                    },
                    {
                        "symbol": IMPL_SYMBOL,
                        "kind": "Class",
                        "displayName": "Impl",
                        "relationships": [
                            {
                                "symbol": PROTO_SYMBOL,
                                "isImplementation": True,
                            }
                        ],
                    },
                ],
                "occurrences": [
                    {
                        "range": [0, 4, 7],
                        "symbol": FOO_SYMBOL,
                        "symbolRoles": 1,
                        "syntaxKind": "IdentifierFunctionDefinition",
                    },
                    {
                        "range": [8, 8, 11],
                        "symbol": FOO_SYMBOL,
                        "symbolRoles": 8,
                        "syntaxKind": "IdentifierFunction",
                    },
                    {
                        "range": [4, 6, 10],
                        "symbol": IMPL_SYMBOL,
                        "symbolRoles": 1,
                        "syntaxKind": "IdentifierType",
                    },
                ],
            }
        ],
    }
    path = root / "index.scip.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_canonical_from_scip_symbol_parses_descriptor_path() -> None:
    assert canonical_from_scip_symbol(FOO_SYMBOL) == "pkg.mod.foo"
    assert canonical_from_scip_symbol(IMPL_SYMBOL) == "pkg.mod.Impl"


def test_import_scip_json_populates_semantic_spine(tmp_path: Path, capsys) -> None:
    _write_repo(tmp_path)
    scip_json = _write_scip_json(tmp_path)

    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(
        [
            "import-scip",
            "--root",
            str(tmp_path),
            "--json-index",
            str(scip_json),
            "--json",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["stats"]["documents_seen"] == 1
    assert payload["stats"]["symbols_upserted"] == 2
    assert payload["stats"]["external_symbols_upserted"] == 1
    assert payload["stats"]["occurrences_inserted"] == 3
    assert payload["stats"]["relations_inserted"] == 1

    config = load_config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT canonical_name, kind, semantic_source
              FROM symbols
             WHERE semantic_source = 'scip:test-scip'
             ORDER BY canonical_name
            """
        ).fetchall()
        names = {row["canonical_name"]: row["kind"] for row in rows}
        assert names["pkg.mod.foo"] == "function"
        assert names["pkg.mod.Impl"] == "class"
        assert names["pkg.proto.Proto"] == "class"

        occ_roles = conn.execute(
            """
            SELECT o.role
             FROM occurrences o
             JOIN symbols s ON s.symbol_pk = o.symbol_pk
             WHERE s.canonical_name = 'pkg.mod.foo'
               AND s.semantic_source = 'scip:test-scip'
             ORDER BY o.role
            """
        ).fetchall()
        assert [row["role"] for row in occ_roles] == ["definition", "reference"]

        rel = conn.execute(
            """
            SELECT r.relation_kind, src.canonical_name AS src, dst.canonical_name AS dst
              FROM relations r
              JOIN symbols src ON src.symbol_pk = r.src_symbol_pk
              JOIN symbols dst ON dst.symbol_pk = r.dst_symbol_pk
             WHERE r.provenance = 'scip:test-scip'
            """
        ).fetchone()
        assert dict(rel) == {
            "relation_kind": "implements",
            "src": "pkg.mod.Impl",
            "dst": "pkg.proto.Proto",
        }
    finally:
        db_mod.close(conn)
