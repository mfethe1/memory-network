from __future__ import annotations

from pathlib import Path

from bench.retrieval import harness
from code_index import db_router as db_mod


def _seed_db(root: Path):
    (root / "pkg").mkdir()
    (root / "pkg" / "memory.py").write_text(
        "def remember():\n    return 'memory broker needle'\n",
        encoding="utf-8",
    )
    (root / "pkg" / "diagnostics.py").write_text(
        "def diagnose():\n    return 'diagnostic graph context'\n",
        encoding="utf-8",
    )
    db_path = root / ".code_index" / "index.db"
    db_path.parent.mkdir()
    conn = db_mod.connect(db_path)
    db_mod.apply_schema(conn)
    file_pks: dict[str, int] = {}
    for file_path in ("pkg/memory.py", "pkg/diagnostics.py"):
        cursor = conn.execute(
            """
            INSERT INTO files(file_path, language, parse_status, indexed_at)
            VALUES (?, 'python', 'ok', '2099-01-01T00:00:00+00:00')
            """,
            (file_path,),
        )
        file_pks[file_path] = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO chunks(
            chunk_uid, file_pk, file_path, language, chunk_type,
            symbol_name, symbol_path, signature, start_line, end_line,
            content, raw_hash, normalized_hash
        )
        VALUES (
            'chunk-memory', ?, 'pkg/memory.py', 'python', 'function',
            'remember', 'pkg.memory.remember', 'def remember()', 1, 2,
            'def remember(): return memory broker needle',
            'raw-memory', 'norm-memory'
        )
        """,
        (file_pks["pkg/memory.py"],),
    )
    conn.execute(
        """
        INSERT INTO chunks(
            chunk_uid, file_pk, file_path, language, chunk_type,
            symbol_name, symbol_path, signature, start_line, end_line,
            content, raw_hash, normalized_hash
        )
        VALUES (
            'chunk-diagnostics', ?, 'pkg/diagnostics.py', 'python', 'function',
            'diagnose', 'pkg.diagnostics.diagnose', 'def diagnose()', 1, 2,
            'def diagnose(): return diagnostic graph context',
            'raw-diagnostics', 'norm-diagnostics'
        )
        """,
        (file_pks["pkg/diagnostics.py"],),
    )
    conn.commit()
    return conn


def test_harness_smoke_runs_broker_and_ripgrep(tmp_path: Path):
    conn = _seed_db(tmp_path)
    cases = [
        harness.RetrievalCase(
            id="memory",
            group="smoke",
            query="memory broker",
            expected=(("file", "pkg/memory.py"),),
        ),
        harness.RetrievalCase(
            id="diagnostics",
            group="smoke",
            query="diagnostic graph context",
            expected=(("file", "pkg/diagnostics.py"),),
        ),
    ]
    try:
        report = harness.run_benchmark(conn, tmp_path, cases, k=5)
    finally:
        db_mod.close(conn)

    assert report["kind"] == "retrieval_broker_vs_ripgrep_benchmark"
    assert report["case_count"] == 2
    assert report["aggregate"]["broker"]["micro"]["found_total"] >= 2
    if report["cases"][0]["ripgrep"].get("status") != "unavailable":
        assert report["aggregate"]["ripgrep"]["micro"]["found_total"] >= 2
    assert report["aggregate"]["broker"]["latency_ms"]["p95"] >= 0
    assert "diff" in report["cases"][0]
