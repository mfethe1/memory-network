from __future__ import annotations

from pathlib import Path

from code_index.openclaw_context.policy import ContextRetrievalPolicy
from code_index.openclaw_context.policy import hold_assignment_for_avoid_pointers
from code_index.openclaw_context.store import SQLiteContextStore


def test_pointer_store_dedupes_by_source_uri_content_hash_and_locator(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        first = store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:file-v1",
            locator={"path": "pkg/service.py", "start_line": 10, "end_line": 20},
            summary="service implementation",
            tokens_estimate=120,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        replay = store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:file-v1",
            locator={"end_line": 20, "start_line": 10, "path": "pkg/service.py"},
            summary="updated wording should not create a duplicate",
            tokens_estimate=130,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        moved_locator = store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:file-v1",
            locator={"path": "pkg/service.py", "start_line": 21, "end_line": 30},
            summary="different selected region",
            tokens_estimate=80,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )

        assert replay.pointer_id == first.pointer_id
        assert moved_locator.pointer_id != first.pointer_id
        assert [pointer.pointer_id for pointer in store.list_context_pointers()] == [
            first.pointer_id,
            moved_locator.pointer_id,
        ]
    finally:
        store.close()


def test_sqlite_context_store_uses_durable_concurrency_pragmas(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        pragmas = store.sqlite_pragmas()

        assert pragmas["journal_mode"] == "wal"
        assert pragmas["busy_timeout"] >= 5000
        assert pragmas["foreign_keys"] == 1
        assert pragmas["synchronous"] in {1, 2}
    finally:
        store.close()


def test_sensitivity_filters_local_cross_provider_cross_host_and_external_routes(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        public = store.upsert_context_pointer(
            source_uri="memo://public",
            source_kind="decision",
            content_hash="h-public",
            locator={"id": "public"},
            summary="public decision",
            sensitivity="public",
            host_id="host-a",
            provider="codex",
        )
        host_private = store.upsert_context_pointer(
            source_uri="memo://host-private",
            source_kind="run_metadata",
            content_hash="h-host",
            locator={"id": "host"},
            summary="host-only run metadata",
            sensitivity="host_private",
            host_id="host-a",
            provider="codex",
        )
        provider_private = store.upsert_context_pointer(
            source_uri="memo://provider-private",
            source_kind="transcript",
            content_hash="h-provider",
            locator={"id": "provider"},
            summary="provider-private transcript handle",
            sensitivity="provider_private",
            host_id="host-a",
            provider="codex",
        )
        external_blocked = store.upsert_context_pointer(
            source_uri="memo://external-blocked",
            source_kind="claim",
            content_hash="h-external",
            locator={"id": "external"},
            summary="internal claim",
            sensitivity="external_blocked",
            host_id="host-a",
            provider="codex",
        )

        local = store.list_context_pointers(
            policy=ContextRetrievalPolicy(
                host_id="host-a",
                provider="codex",
                route_scope="local",
            )
        )
        cross_provider = store.list_context_pointers(
            policy=ContextRetrievalPolicy(
                host_id="host-a",
                provider="kimi",
                route_scope="cross_provider",
            )
        )
        cross_host = store.list_context_pointers(
            policy=ContextRetrievalPolicy(
                host_id="host-b",
                provider="codex",
                route_scope="cross_host",
            )
        )
        external = store.list_context_pointers(
            policy=ContextRetrievalPolicy(
                host_id="host-b",
                provider="codex",
                route_scope="external_message",
            )
        )

        assert {pointer.pointer_id for pointer in local} == {
            public.pointer_id,
            host_private.pointer_id,
            provider_private.pointer_id,
            external_blocked.pointer_id,
        }
        assert {pointer.pointer_id for pointer in cross_provider} == {
            public.pointer_id,
            host_private.pointer_id,
            external_blocked.pointer_id,
        }
        assert {pointer.pointer_id for pointer in cross_host} == {
            public.pointer_id,
        }
        assert {pointer.pointer_id for pointer in external} == {
            public.pointer_id,
        }
    finally:
        store.close()


def test_avoid_pointer_hold_decision_is_passive_and_reusable(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        avoid = store.upsert_context_pointer(
            source_uri="fumemory://avoid/retry-timeout-loop",
            source_kind="decision",
            pointer_kind="avoid",
            content_hash="h-avoid",
            locator={"target": "pkg.service.retry"},
            summary="Do not retry the timeout loop patch; it failed CI.",
            sensitivity="repo",
            target_symbols=["pkg.service.retry"],
        )

        held = hold_assignment_for_avoid_pointers(
            store,
            task_id="task-1",
            target_symbols=["pkg.service.retry"],
        )
        unrelated = hold_assignment_for_avoid_pointers(
            store,
            task_id="task-2",
            target_symbols=["pkg.service.parse"],
        )

        assert held.status == "held"
        assert held.reason == "avoid_pointer"
        assert held.pointer_ids == (avoid.pointer_id,)
        assert held.invoked_context_manager is False
        assert unrelated.status == "allow"
        assert unrelated.pointer_ids == ()
    finally:
        store.close()
