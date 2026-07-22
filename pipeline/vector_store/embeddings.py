"""Embedding helpers for passage/query vectors (multilingual-e5-large compatible)."""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
DEFAULT_VECTOR_SIZE = 1024
_local_embedder = None


def get_embedding_model_name() -> str:
    return (os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL).strip()


def get_vector_size() -> int:
    return int(
        os.environ.get("EMBEDDING_VECTOR_SIZE")
        or os.environ.get("EMBEDDING_DIM")
        or DEFAULT_VECTOR_SIZE
    )


def get_embedding_provider() -> str:
    explicit = (os.environ.get("EMBEDDING_PROVIDER") or "").strip().lower()
    if explicit in {"local", "sentence_transformers", "openai_compatible"}:
        if explicit == "local":
            return "sentence_transformers"
        return explicit
    if (os.environ.get("EMBEDDING_BASE_URL") or "").strip():
        return "openai_compatible"
    return "sentence_transformers"


def _prefix_passage(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.lower().startswith("passage:"):
        return cleaned
    return f"passage: {cleaned}" if cleaned else "passage:"


def _prefix_query(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.lower().startswith("query:"):
        return cleaned
    return f"query: {cleaned}" if cleaned else "query:"


def _embed_openai_compatible(texts: list[str]) -> list[list[float]]:
    base_url = (os.environ.get("EMBEDDING_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("EMBEDDING_BASE_URL is required for openai_compatible embeddings")
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = get_embedding_model_name()
    url = base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "input": texts}
    timeout = float(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "120"))
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    items = data.get("data") or []
    items = sorted(items, key=lambda item: item.get("index", 0))
    vectors = [item.get("embedding") for item in items]
    if len(vectors) != len(texts) or any(v is None for v in vectors):
        raise RuntimeError(f"Embedding API returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


def _get_local_embedder():
    global _local_embedder
    if _local_embedder is None:
        from sentence_transformers import SentenceTransformer

        model_name = get_embedding_model_name()
        logger.info("Loading SentenceTransformer model %s", model_name)
        _local_embedder = SentenceTransformer(model_name)
    return _local_embedder


def _embed_local(texts: list[str]) -> list[list[float]]:
    model = _get_local_embedder()
    vectors = model.encode(texts, normalize_embeddings=False)
    return [vector.tolist() for vector in vectors]


def embed_texts(
    texts: Iterable[str],
    kind: str = "passage",
    use_e5_prefix: bool = True,
) -> list[list[float]]:
    """Embed texts as passage or query vectors."""
    prepared: list[str] = []
    for text in texts:
        raw = text or ""
        if not use_e5_prefix:
            prepared.append(raw)
        elif kind == "query":
            prepared.append(_prefix_query(raw))
        else:
            prepared.append(_prefix_passage(raw))

    if not prepared:
        return []

    provider = get_embedding_provider()
    if provider == "openai_compatible":
        return _embed_openai_compatible(prepared)
    return _embed_local(prepared)


def embed_query(query: str, use_e5_prefix: bool = True) -> list[float]:
    vectors = embed_texts([query], kind="query", use_e5_prefix=use_e5_prefix)
    return vectors[0]


def embed_passages(
    passages: list[str],
    use_e5_prefix: bool = True,
    batch_size: int = 32,
) -> list[list[float]]:
    if not passages:
        return []
    out: list[list[float]] = []
    for i in range(0, len(passages), batch_size):
        batch = passages[i : i + batch_size]
        out.extend(embed_texts(batch, kind="passage", use_e5_prefix=use_e5_prefix))
    return out
