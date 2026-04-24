"""Embedding backend loader.

Lazy import chain: fastembed → sentence-transformers → None. Each backend
implements the same `EmbeddingBackend` protocol so the store layer doesn't
care which is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class EmbeddingBackend(Protocol):
    model_name: str
    dimension: int
    provider: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class _FastEmbedBackend:
    model_name: str
    dimension: int
    provider: str = "fastembed"
    _model: object = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return [list(map(float, v)) for v in self._model.embed(texts)]


@dataclass
class _SentenceTransformersBackend:
    model_name: str
    dimension: int
    provider: str = "sentence-transformers"
    _model: object = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        # Return plain Python floats, not numpy arrays.
        arr = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [list(map(float, row)) for row in arr]


def _fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
    except Exception:
        return False
    return True


def _sentence_transformers_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def availability_report() -> dict:
    """Describe which backend would be picked, without actually loading
    the model. Used by `doctor --json`."""
    fe = _fastembed_available()
    st = _sentence_transformers_available()
    if fe:
        provider = "fastembed"
    elif st:
        provider = "sentence-transformers"
    else:
        provider = None
    return {
        "available": fe or st,
        "provider": provider,
        "model_default": DEFAULT_MODEL,
        "backends": {"fastembed": fe, "sentence-transformers": st},
    }


# Known model dimensions. Keeps us from loading the model just to get
# `dimension` for a doctor report.
_MODEL_DIMENSIONS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
}


def get_backend(model_name: str | None = None) -> EmbeddingBackend:
    """Return a live backend for `model_name`.

    Raises `RuntimeError` when no backend can be loaded; callers are
    expected to check `availability_report()["available"]` first.
    """
    model = model_name or DEFAULT_MODEL
    dim = _MODEL_DIMENSIONS.get(model, 384)
    if _fastembed_available():
        return _FastEmbedBackend(model_name=model, dimension=dim)
    if _sentence_transformers_available():
        return _SentenceTransformersBackend(model_name=model, dimension=dim)
    raise RuntimeError(
        "No embedding backend available. Install one of: "
        "`pip install fastembed` (recommended, CPU-only) or "
        "`pip install sentence-transformers`."
    )
