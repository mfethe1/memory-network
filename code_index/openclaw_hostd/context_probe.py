"""Host-local passive context metrics probe.

The probe reports handles, hashes, counts, and health hints. It intentionally
does not emit raw transcript text or secret-bearing payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.models import mapping_tuple
from code_index.openclaw_context.models import string_tuple


class HostContextProbe:
    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        context_store: Any | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.context_store = context_store

    def collect_run_metrics(self, run: Mapping[str, Any]) -> HostContextMetrics:
        metadata = (
            run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
        )
        loaded_handles = _mapping_list(
            run.get("loaded_context_handles")
            or metadata.get("loaded_context_handles")
            or run.get("context_handles")
            or metadata.get("context_handles")
            or run.get("loaded_context_handles_json")
            or metadata.get("loaded_context_handles_json")
        )
        loaded_pointer_ids = tuple(
            pointer_id
            for pointer_id in (
                _text(
                    handle.get("pointer_id")
                    or handle.get("id")
                    or handle.get("handle")
                )
                for handle in loaded_handles
            )
            if pointer_id
        )
        loaded_files = tuple(
            _dedupe(
                string_tuple(
                    run.get("active_files")
                    or run.get("selected_paths")
                    or metadata.get("selected_paths")
                    or run.get("active_files_json")
                )
            )
        )
        degraded = []
        if self.context_store is not None:
            try:
                self.context_store.list_context_pointers()
            except Exception:
                degraded.append("fumemory_unavailable")
        return HostContextMetrics(
            host_id=_text(run.get("host_id")) or "",
            run_id=_text(run.get("run_id") or run.get("id")) or "",
            task_id=_text(run.get("task_id") or metadata.get("task_id")) or "",
            agent_id=_text(run.get("agent_id") or metadata.get("agent_id")) or "",
            estimated_tokens=_int(
                run.get("estimated_tokens") or metadata.get("estimated_tokens")
            ),
            loaded_files=loaded_files,
            loaded_pointer_ids=loaded_pointer_ids,
            file_hashes=self._file_hashes(loaded_files),
            active_claims=tuple(
                dict(item) for item in _mapping_list(run.get("active_claims"))
            ),
            recent_failures=string_tuple(
                run.get("recent_failures") or metadata.get("recent_failures")
            ),
            tool_output_volume=_int(
                run.get("tool_output_volume") or metadata.get("tool_output_volume")
            ),
            provider_compaction_signals=string_tuple(
                run.get("provider_compaction_signals")
                or metadata.get("provider_compaction_signals")
            ),
            approach_history=string_tuple(
                run.get("approach_history")
                or metadata.get("approach_history")
                or run.get("approach_history_json")
            ),
            degraded_reasons=tuple(degraded),
        )

    def collect_graph_payload_metrics(
        self,
        payload: Mapping[str, Any],
        *,
        host_id: str,
    ) -> tuple[HostContextMetrics, ...]:
        return tuple(
            self.collect_run_metrics({**dict(run), "host_id": host_id})
            for run in _graph_payload_runs(payload)
        )

    def local_context_source_handles(
        self,
        *,
        run_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Return handles to existing local context sources, not duplicated state."""

        safe_run_id = _text(run_id) or "run"
        return (
            {
                "source_kind": "context_packet",
                "source_uri": f"code_index://context-packet/{safe_run_id}",
            },
            {
                "source_kind": "graph_context",
                "source_uri": f"code_index://graph-context/{safe_run_id}",
            },
            {
                "source_kind": "collaboration_packet",
                "source_uri": f"code_index://collaboration/{safe_run_id}",
            },
            {
                "source_kind": "transcript",
                "source_uri": f"code_index://transcript/{safe_run_id}",
            },
            {
                "source_kind": "run_metadata",
                "source_uri": f"code_index://run-metadata/{safe_run_id}",
            },
            {
                "source_kind": "claim_data",
                "source_uri": f"code_index://claims/{safe_run_id}",
            },
        )

    def _file_hashes(self, files: tuple[str, ...]) -> dict[str, str]:
        if self.repo_root is None:
            return {}
        hashes: dict[str, str] = {}
        for rel in files:
            path = (self.repo_root / rel).resolve()
            try:
                path.relative_to(self.repo_root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 64), b""):
                    digest.update(chunk)
            hashes[rel] = f"sha256:{digest.hexdigest()}"
        return hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-index-openclaw-context-probe")
    parser.add_argument("--run-json", help="JSON object containing one run state")
    parser.add_argument("--repo-root", help="Repository root used for file hashes")
    args = parser.parse_args(argv)
    run = json.loads(args.run_json or "{}")
    if not isinstance(run, dict):
        raise SystemExit("--run-json must decode to a JSON object")
    metrics = HostContextProbe(repo_root=args.repo_root).collect_run_metrics(run)
    print(json.dumps(metrics.to_dict(), indent=2, sort_keys=True))
    return 0


def _graph_payload_runs(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    runs: list[Mapping[str, Any]] = []
    for key in ("active_runs", "runs"):
        runs.extend(_mapping_list(payload.get(key)))
    columns = payload.get("columns")
    if isinstance(columns, Mapping):
        for column in columns.values():
            if isinstance(column, Mapping):
                runs.extend(_mapping_list(column.get("runs")))
    return runs


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return list(mapping_tuple(value))
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
