from __future__ import annotations

from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import retrieval


def _seed_broker_db(root: Path):
    config = cfg_mod.load(root)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)

    file_pks: dict[str, int] = {}
    for path in (
        "pkg/memory.py",
        "pkg/budgeted_file.py",
        "pkg/dup.py",
        "pkg/hybridmemory.py",
    ):
        cursor = conn.execute(
            """
            INSERT INTO files(file_path, language, parse_status, indexed_at)
            VALUES (?, 'python', 'ok', '2099-01-01T00:00:00+00:00')
            """,
            (path,),
        )
        file_pks[path] = int(cursor.lastrowid)

    chunks = [
        (
            "chunk-memory",
            "pkg/memory.py",
            "function",
            "remember",
            "pkg.memory.remember",
            "def remember(): pass",
            "def remember():\n    return 'memory broker needle'\n",
        ),
        (
            "chunk-dup-file",
            "pkg/dup.py",
            "file",
            None,
            None,
            "",
            "# dup marker that should dedupe against the file path\n",
        ),
        (
            "chunk-hybrid",
            "pkg/hybridmemory.py",
            "function",
            "hybrid",
            "pkg.hybridmemory.hybrid",
            "def hybrid(): pass",
            "def hybrid():\n    return 'hybridmemory code chunk'\n",
        ),
    ]
    for chunk_uid, path, chunk_type, symbol_name, symbol_path, signature, content in chunks:
        conn.execute(
            """
            INSERT INTO chunks(
                chunk_uid, file_pk, file_path, language, chunk_type,
                symbol_name, symbol_path, signature, start_line, end_line,
                content, raw_hash, normalized_hash
            )
            VALUES (?, ?, ?, 'python', ?, ?, ?, ?, 1, 3, ?, ?, ?)
            """,
            (
                chunk_uid,
                file_pks[path],
                path,
                chunk_type,
                symbol_name,
                symbol_path,
                signature,
                content,
                f"raw-{chunk_uid}",
                f"norm-{chunk_uid}",
            ),
        )

    cursor = conn.execute(
        """
        INSERT INTO agent_runs(
            run_id, agent_name, status, prompt, selected_nodes_json,
            started_at, updated_at, metadata_json
        )
        VALUES (
            'run-hybrid', 'Worker', 'working', 'Investigate hybridmemory',
            '[]', '2099-01-01T00:00:00+00:00',
            '2099-01-01T00:01:00+00:00', '{}'
        )
        """
    )
    run_pk = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO agent_events(
            run_pk, timestamp, event_type, file_path, symbol_path, message,
            payload_json
        )
        VALUES (
            ?, '2099-01-01T00:02:00+00:00', 'edit',
            'pkg/hybridmemory.py', 'pkg.hybridmemory.hybrid',
            'hybridmemory transcript event', '{}'
        )
        """,
        (run_pk,),
    )
    return conn


def test_retrieval_result_contract_shape_and_enums(tmp_path: Path):
    conn = _seed_broker_db(tmp_path)
    try:
        response = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query="pkg/memory.py",
                limit=3,
                budget_bytes=1_000,
                sources=(retrieval.SourceKind.FILE_PATH,),
            ),
        )
    finally:
        db_mod.close(conn)

    payload = response.to_dict()
    assert payload["kind"] == "code_index_retrieval"
    assert payload["bytes_used"] == sum(
        item["byte_cost"] for item in payload["results"]
    )
    result = payload["results"][0]
    assert {
        "handle",
        "source_kind",
        "byte_cost",
        "provenance",
        "score",
        "why_included",
        "truncation_reason",
        "payload",
    } <= set(result)
    assert result["handle"] == "file:pkg/memory.py"
    assert result["source_kind"] == retrieval.SourceKind.FILE_PATH.value
    assert result["truncation_reason"] in {
        reason.value for reason in retrieval.TruncationReason
    }
    assert result["byte_cost"] == len(result["payload"]["text"].encode("utf-8"))


def test_retrieval_enforces_exact_utf8_byte_budget(tmp_path: Path):
    conn = _seed_broker_db(tmp_path)
    try:
        response = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query="budgeted",
                limit=2,
                budget_bytes=8,
                sources=(retrieval.SourceKind.FILE_PATH,),
            ),
        )
    finally:
        db_mod.close(conn)

    assert response.bytes_used == 8
    assert response.truncation_reason is retrieval.TruncationReason.BYTE_BUDGET
    assert len(response.results) == 1
    result = response.results[0]
    assert result.byte_cost == 8
    assert result.truncation_reason is retrieval.TruncationReason.BYTE_BUDGET
    assert len(result.payload["text"].encode("utf-8")) == 8


def test_retrieval_dedupes_whole_file_chunk_when_path_matches(tmp_path: Path):
    conn = _seed_broker_db(tmp_path)
    try:
        chunk_only = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query="dup",
                limit=5,
                budget_bytes=1_000,
                sources=(retrieval.SourceKind.CODE_CHUNK,),
            ),
        )
        mixed = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query="dup",
                limit=5,
                budget_bytes=1_000,
                sources=(
                    retrieval.SourceKind.FILE_PATH,
                    retrieval.SourceKind.CODE_CHUNK,
                ),
            ),
        )
    finally:
        db_mod.close(conn)

    assert any(result.handle == "chunk:chunk-dup-file" for result in chunk_only.results)
    handles = {result.handle for result in mixed.results}
    assert "file:pkg/dup.py" in handles
    assert "chunk:chunk-dup-file" not in handles


def test_retrieval_returns_file_and_transcript_matches(tmp_path: Path):
    conn = _seed_broker_db(tmp_path)
    try:
        response = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query="hybridmemory",
                limit=5,
                budget_bytes=2_000,
                sources=(
                    retrieval.SourceKind.FILE_PATH,
                    retrieval.SourceKind.TRANSCRIPT_EVENT,
                ),
            ),
        )
    finally:
        db_mod.close(conn)

    by_kind = {result.source_kind for result in response.results}
    assert retrieval.SourceKind.FILE_PATH in by_kind
    assert retrieval.SourceKind.TRANSCRIPT_EVENT in by_kind
    transcript = next(
        result
        for result in response.results
        if result.source_kind is retrieval.SourceKind.TRANSCRIPT_EVENT
    )
    assert transcript.file_path == "pkg/hybridmemory.py"
    assert "hybridmemory transcript event" in transcript.payload["text"]
