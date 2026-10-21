"""Embedding helpers for the Qdrant vector backend.

Marqo embeds inside ``add_documents``. Qdrant does not, so dense (E5) and sparse
(BM25) vectors are produced here before upsert / search.

Prefixes match the existing Marqo passage schema:

* ingest / passage text → ``passage: …``
* search queries → ``query: …`` (usually already applied by ``run_search``)

Backends:

* ``fastembed`` (default) — local ONNX E5 + ``Qdrant/bm25`` sparse
* ``http`` — OpenAI-compatible ``/embeddings`` for dense; sparse still FastEmbed
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

import httpx

_PASSAGE_PREFIX = "passage: "
_QUERY_PREFIX = "query: "


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class EmbeddedBatch:
    dense: list[list[float]]
    sparse: list[SparseVector]


class Embedder(Protocol):
    @property
    def dense_dim(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> EmbeddedBatch: ...

    def embed_queries(self, texts: Sequence[str]) -> EmbeddedBatch: ...

    def embed_sparse_plain(self, texts: Sequence[str]) -> list[SparseVector]: ...


def ensure_passage_prefix(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.lower().startswith("passage:"):
        return cleaned
    return f"{_PASSAGE_PREFIX}{cleaned}"


def ensure_query_prefix(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.lower().startswith("query:"):
        return cleaned
    return f"{_QUERY_PREFIX}{cleaned}"


def l2_normalize(vector: Sequence[float]) -> list[float]:
    total = sum(float(v) * float(v) for v in vector) ** 0.5
    if total <= 0.0:
        return [float(v) for v in vector]
    return [float(v) / total for v in vector]


class FastEmbedEmbedder:
    """Local FastEmbed dense E5 + sparse BM25."""

    def __init__(
        self,
        *,
        dense_model: str | None = None,
        sparse_model: str | None = None,
        normalize: bool = True,
    ) -> None:
        from fastembed import SparseTextEmbedding, TextEmbedding

        self.dense_model_name = (
            dense_model
            or os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large").strip()
        )
        self.sparse_model_name = (
            sparse_model
            or os.environ.get("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25").strip()
        )
        self.normalize = normalize
        self._dense = TextEmbedding(model_name=self.dense_model_name)
        self._sparse = SparseTextEmbedding(model_name=self.sparse_model_name)
        # Probe dim once from a tiny encode.
        sample = list(self._dense.embed([ensure_passage_prefix("dim probe")], batch_size=1))[0]
        self._dense_dim = len(sample)

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    def embed_passages(self, texts: Sequence[str]) -> EmbeddedBatch:
        prefixed = [ensure_passage_prefix(text) for text in texts]
        return self._embed(prefixed)

    def embed_queries(self, texts: Sequence[str]) -> EmbeddedBatch:
        prefixed = [ensure_query_prefix(text) for text in texts]
        return self._embed(prefixed)

    def embed_sparse_plain(self, texts: Sequence[str]) -> list[SparseVector]:
        """Sparse BM25 over raw text (no E5 passage:/query: prefix)."""
        sparse_raw = list(self._sparse.embed([text or "" for text in texts]))
        return [
            SparseVector(
                indices=[int(i) for i in item.indices.tolist()],
                values=[float(v) for v in item.values.tolist()],
            )
            for item in sparse_raw
        ]

    def _embed(self, prefixed: Sequence[str]) -> EmbeddedBatch:
        dense_raw = list(self._dense.embed(list(prefixed)))
        dense = [l2_normalize(vec) if self.normalize else [float(v) for v in vec] for vec in dense_raw]
        sparse_raw = list(self._sparse.embed(list(prefixed)))
        sparse: list[SparseVector] = []
        for item in sparse_raw:
            indices = [int(i) for i in item.indices.tolist()]
            values = [float(v) for v in item.values.tolist()]
            sparse.append(SparseVector(indices=indices, values=values))
        return EmbeddedBatch(dense=dense, sparse=sparse)


class HttpDenseFastEmbedSparse:
    """Dense via OpenAI-compatible HTTP; sparse via FastEmbed BM25."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        sparse_model: str | None = None,
        normalize: bool = True,
        dense_dim: int | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        from fastembed import SparseTextEmbedding

        if not endpoint:
            raise ValueError("EMBEDDING_URL is required for EMBEDDING_BACKEND=http")
        self._url = endpoint.rstrip("/")
        if not self._url.endswith("/embeddings"):
            self._url = self._url + "/embeddings"
        self.model = model
        self.api_key = api_key
        self.normalize = normalize
        self.timeout_seconds = timeout_seconds
        self._dense_dim = dense_dim or int(os.environ.get("EMBEDDING_DIM", "1024"))
        self._sparse = SparseTextEmbedding(
            model_name=sparse_model
            or os.environ.get("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25").strip()
        )

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    def embed_passages(self, texts: Sequence[str]) -> EmbeddedBatch:
        return self._embed([ensure_passage_prefix(text) for text in texts])

    def embed_queries(self, texts: Sequence[str]) -> EmbeddedBatch:
        return self._embed([ensure_query_prefix(text) for text in texts])

    def embed_sparse_plain(self, texts: Sequence[str]) -> list[SparseVector]:
        sparse_raw = list(self._sparse.embed([text or "" for text in texts]))
        return [
            SparseVector(
                indices=[int(i) for i in item.indices.tolist()],
                values=[float(v) for v in item.values.tolist()],
            )
            for item in sparse_raw
        ]

    def _embed(self, prefixed: Sequence[str]) -> EmbeddedBatch:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": list(prefixed)}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(self._url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        items = sorted(data.get("data") or [], key=lambda row: int(row.get("index", 0)))
        dense: list[list[float]] = []
        for item in items:
            vector = item.get("embedding") or []
            dense.append(l2_normalize(vector) if self.normalize else [float(v) for v in vector])
        if len(dense) != len(prefixed):
            raise RuntimeError(
                f"Embedding endpoint returned {len(dense)} vectors for {len(prefixed)} inputs"
            )
        sparse_raw = list(self._sparse.embed(list(prefixed)))
        sparse = [
            SparseVector(
                indices=[int(i) for i in item.indices.tolist()],
                values=[float(v) for v in item.values.tolist()],
            )
            for item in sparse_raw
        ]
        return EmbeddedBatch(dense=dense, sparse=sparse)


_EMBEDDER: Embedder | None = None


def get_embedder() -> Embedder:
    """Process-wide embedder (lazy). Tests can call :func:`reset_embedder`."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    backend = (os.environ.get("EMBEDDING_BACKEND") or "fastembed").strip().lower()
    normalize = (os.environ.get("EMBEDDING_NORMALIZE") or "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if backend in {"http", "openai", "openai_compatible"}:
        _EMBEDDER = HttpDenseFastEmbedSparse(
            endpoint=os.environ.get("EMBEDDING_URL", "").strip(),
            model=os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large").strip(),
            api_key=os.environ.get("EMBEDDING_API_KEY", "").strip(),
            normalize=normalize,
        )
    elif backend in {"fastembed", "local", ""}:
        _EMBEDDER = FastEmbedEmbedder(normalize=normalize)
    else:
        raise ValueError(f"Unknown EMBEDDING_BACKEND={backend!r}")
    return _EMBEDDER


def reset_embedder(embedder: Embedder | None = None) -> None:
    """Replace or clear the cached embedder (tests)."""
    global _EMBEDDER
    _EMBEDDER = embedder


def strip_e5_prefix(text: str) -> str:
    """Return text without a leading passage:/query: prefix (for sparse lexical)."""
    return re.sub(r"^(passage|query)\s*:\s*", "", text or "", count=1, flags=re.IGNORECASE)
