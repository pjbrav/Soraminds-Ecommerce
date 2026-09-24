"""Embedding service: sentence-transformers if available, deterministic fallback if not.

Why local embeddings over API embeddings: zero marginal cost, deterministic,
and exactly matches the spec's ">= 0.85 cosine similarity" requirement for
column/category fuzzy matching (see ARCHITECTURE.md).

The hashed-trigram fallback keeps the prototype fully runnable (and testable)
in environments where torch/sentence-transformers is not installed. Its
similarity scores are conservative, so borderline matches surface to the
owner rather than silently auto-mapping.
"""
from __future__ import annotations

import hashlib
import math
import threading

from .. import settings

FALLBACK_MODEL = "local-hashed-trigram-v1"
_LOCK = threading.Lock()
_MODEL = None
_PROVIDER: str | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _hash_trigram_vector(text: str, dim: int = 384) -> list[float]:
    low = " " + text.lower().strip() + " "
    vec = [0.0] * dim
    for i in range(len(low) - 2):
        gram = low[i : i + 3]
        h = int.from_bytes(hashlib.md5(gram.encode()).digest()[:4], "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm:
        vec = [x / norm for x in vec]
    return vec


class _FallbackEmbedder:
    """Deterministic, dependency-free stand-in for MiniLM cosine similarity.

    Blends char-trigram cosine with token-set overlap so plural/typo variants
    ("Veg Curry" ~ "Veg Curries") still reach the 0.85 auto-map threshold,
    while genuinely different strings stay far below it.
    """

    model_name = FALLBACK_MODEL

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def similarity(self, a: str, b: str) -> float:
        from rapidfuzz import fuzz

        trigram = _cosine(self._hash(a), self._hash(b))
        token = fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0
        return round(max(trigram, 0.65 * token + 0.35 * trigram), 4)

    def _hash(self, text: str) -> list[float]:
        vec = self._cache.get(text)
        if vec is None:
            vec = _hash_trigram_vector(text)
            self._cache[text] = vec
        return vec


class _MiniLMEmbedder:
    model_name = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # lazy, heavy import

        self._model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME)
        self.model_name = f"sentence-transformers/{settings.EMBEDDING_MODEL_NAME}"

    def similarity(self, a: str, b: str) -> float:
        vecs = self._model.encode([a, b], normalize_embeddings=True)
        return round(float(vecs[0] @ vecs[1]), 4)


def _get_embedder():
    global _MODEL, _PROVIDER
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        mode = settings.EMBEDDING_PROVIDER
        if mode == "fallback":
            _MODEL, _PROVIDER = _FallbackEmbedder(), "fallback"
            return _MODEL
        try:
            _MODEL, _PROVIDER = _MiniLMEmbedder(), "sentence-transformers"
        except Exception:  # noqa: BLE001 - graceful degradation is the design
            _MODEL, _PROVIDER = _FallbackEmbedder(), "fallback"
        return _MODEL


def similarity(a: str, b: str) -> float:
    """Cosine similarity between two short texts."""
    return _get_embedder().similarity(a, b)


def embed(text: str) -> tuple[str, list[float]]:
    """Embed one text. Returns (model_name, vector)."""
    embedder = _get_embedder()
    if isinstance(embedder, _MiniLMEmbedder):
        vec = embedder._model.encode([text], normalize_embeddings=True)[0]
        return embedder.model_name, [round(float(x), 6) for x in vec]
    return embedder.model_name, _hash_trigram_vector(text)


def provider() -> str:
    if _PROVIDER is None:
        _get_embedder()
    return _PROVIDER or "unknown"


def vector_id(text: str) -> str:
    return "vec_" + hashlib.sha1(text.encode()).hexdigest()[:6]


def embedding_source_text(item: dict) -> str:
    """RAG-ready source text: item_name + description + tags + category."""
    parts = [
        item.get("item_name") or "",
        item.get("category") or "",
        item.get("description") or "",
        ", ".join(item.get("tags") or []),
    ]
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + "."
