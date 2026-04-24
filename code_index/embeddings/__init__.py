"""Semantic retrieval over chunks.

Closes the "find code like this" gap identified in the slice-7 critical
review. The embeddings table already exists in schema — this package
populates it and exposes a cosine-similarity query path.

Two entry points:
- `code_index embed` populates/refreshes embeddings.
- `code_index similar QUERY` returns ranked chunks.

Backend selection (lazy): tries `fastembed` first (small, CPU-only,
no PyTorch), falls back to `sentence-transformers`. Reports which via
`doctor --json`.
"""

from code_index.embeddings.models import (
    DEFAULT_MODEL,
    EmbeddingBackend,
    availability_report,
    get_backend,
)
from code_index.embeddings.store import (
    coverage,
    populate,
    search as semantic_search,
)

__all__ = [
    "DEFAULT_MODEL",
    "EmbeddingBackend",
    "availability_report",
    "coverage",
    "get_backend",
    "populate",
    "semantic_search",
]
