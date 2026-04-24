"""Pytest node-id formation for affected test rows."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
from typing import Any


def build_pytest_invocation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return pytest invocation metadata for affected test rows."""
    node_ids: list[str] = []
    skipped_tests: list[dict[str, str]] = []
    seen_skips: set[tuple[str, str]] = set()

    def skip(canonical_name: str, reason: str) -> None:
        key = (canonical_name, reason)
        if key not in seen_skips:
            skipped_tests.append({"canonical_name": canonical_name, "reason": reason})
            seen_skips.add(key)

    for row in rows:
        canonical_name = str(row.get("canonical_name") or "")
        file_path = row.get("def_file")
        if not canonical_name or not file_path:
            skip(canonical_name, "missing test file path")
            continue

        base_node_id = _base_node_id(str(file_path), canonical_name)
        if base_node_id is None:
            continue
        parametrize = row.get("parametrize")
        if not parametrize:
            node_ids.append(base_node_id)
            continue

        cases = parametrize.get("cases") or []
        explicit_ids = parametrize.get("ids")
        ids_callable = bool(parametrize.get("ids_callable"))
        if not cases:
            skip(canonical_name, "parametrize cases are unavailable")
            continue

        if ids_callable:
            skip(
                canonical_name,
                "parametrize ids= is a callable; pytest generates ids at collection time",
            )

        if parametrize.get("truncated"):
            case_count = parametrize.get("case_count")
            captured = len(cases)
            skip(
                canonical_name,
                f"parametrize captured {captured} of {case_count} cases; only captured cases emitted",
            )

        # When the user supplied explicit ids=[...] matching the case count,
        # use those literally. Pytest's selection semantics accept the user's
        # ids verbatim.
        use_explicit = (
            explicit_ids is not None
            and len(explicit_ids) == len(cases)
            and not ids_callable
        )

        for idx, case in enumerate(cases):
            if use_explicit:
                node_ids.append(f"{base_node_id}[{explicit_ids[idx]}]")
                continue
            try:
                case_id = pytest_case_id(str(case))
            except ValueError as exc:
                skip(canonical_name, str(exc))
                continue
            node_ids.append(f"{base_node_id}[{case_id}]")

    return {
        "runner": "pytest",
        "invocation": ["pytest", *node_ids],
        "node_ids": node_ids,
        "skipped_tests": skipped_tests,
    }


def pytest_case_id(case: str) -> str:
    """Convert a captured literal parametrize case into pytest's simple id."""
    parts = _split_case_components(_strip_outer_collection(case.strip()))
    values: list[str] = []
    for part in parts:
        literal = part.strip()
        if not literal:
            continue
        try:
            values.append(str(ast.literal_eval(literal)))
        except Exception as exc:  # noqa: BLE001 - keep formatter dependency-free.
            raise ValueError("parametrize arguments are not literal") from exc
    if not values:
        raise ValueError("parametrize arguments are not literal")
    return "-".join(values)


def _base_node_id(file_path: str, canonical_name: str) -> str | None:
    normalized = file_path.replace("\\", "/")
    rel = PurePosixPath(normalized)
    module_parts = list(rel.with_suffix("").parts)
    if module_parts and module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]
    module_name = ".".join(module_parts)
    if module_name and canonical_name == module_name:
        return None
    if module_name and canonical_name.startswith(f"{module_name}."):
        test_path = canonical_name[len(module_name) + 1 :]
    else:
        test_path = canonical_name.rsplit(".", 1)[-1]
    if not _is_pytest_test_path(test_path):
        return None
    return f"{normalized}::{'::'.join(test_path.split('.'))}"


def _is_pytest_test_path(test_path: str) -> bool:
    parts = test_path.split(".")
    if not parts or not parts[-1].startswith("test"):
        return False
    class_parts = parts[:-1]
    return all(part.startswith("Test") for part in class_parts)


def _strip_outer_collection(case: str) -> str:
    if len(case) < 2:
        return case
    pairs = {"(": ")", "[": "]"}
    closer = pairs.get(case[0])
    if closer is None or case[-1] != closer:
        return case
    depth = 0
    quote: str | None = None
    escaped = False
    for idx, char in enumerate(case):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0 and idx != len(case) - 1:
                return case
    return case[1:-1].strip()


def _split_case_components(case: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for idx, char in enumerate(case):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth -= 1
            continue
        if char == "," and depth == 0:
            parts.append(case[start:idx])
            start = idx + 1
    parts.append(case[start:])
    return parts
