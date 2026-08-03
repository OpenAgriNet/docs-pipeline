"""Vector-store adapter — the single place Marqo's quirks are encoded.

Before this module every route re-encoded Marqo's behaviour by hand: that a
delete needs a search for ``_id`` first, that a missing index raises rather than
returning empty, that schema introspection means walking ``allFields``. Two
production bugs in one day (a missing ``instance`` filterable field, a request
for a non-existent ``_id`` attribute) came from that duplication, which is what
this module exists to stop.

Layering rules, deliberately strict:

* **No FastAPI.** Failures raise :class:`VectorStoreError`; translating that
  into an HTTP status is the caller's policy decision, not the store's.
* **No auth.** The store never sees ``AuthUser``. Tenant scoping is built by the
  caller, which may ask :meth:`VectorStore.has_field` whether an index can
  support a scoping filter at all.
* **No registry / DB.** The store takes physical index names. Resolving
  ``(instance, logical name) -> physical index`` stays with the caller.

The delete methods are the exception to "raise on failure": they return a result
dict instead. A purge that found nothing, and a purge against an index that does
not exist, are both **benign** outcomes that callers must be able to tell apart
from a real backend failure, because the difference decides whether a
purge-before-flip sequence may proceed. See :meth:`VectorStore.delete_document`.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Optional, Protocol

DEFAULT_MARQO_URL = "http://localhost:8882"

# A purge searches for the chunk ids to remove; Marqo has no delete-by-filter.
_MAX_CHUNKS_PER_DOCUMENT = 1000

# A structured index rejects `_id` as a *retrievable* attribute and 400s the whole
# query, but returns `_id` on every hit anyway. So a purge asks for a real field
# and reads `_id` off the result. Named once here because both purge paths need
# it and asking for `_id` broke production against amul-veterinary-index (#55).
_PURGE_ATTRIBUTES = ["doc_id"]


class VectorStoreError(RuntimeError):
    """A vector-store operation failed.

    Callers translate this into whatever status their surface needs — 400 for a
    failed search, 404 for a missing index, 502 for a failed purge.
    """


def get_marqo_doc_id(document_id: str) -> str:
    """Document identifier stored in the Marqo ``doc_id`` field."""
    return document_id


def get_legacy_marqo_doc_id(document_id: str) -> str:
    """Legacy hashed ``doc_id`` used before provenance ingest alignment."""
    return hashlib.md5(document_id.encode()).hexdigest()


def index_missing_error(err: Exception | str) -> bool:
    """True when the backend is telling us the index does not exist.

    Marqo signals this as a generic error whose message we have to sniff, so the
    string match is a backend quirk and belongs in here rather than at a route.
    """
    text = str(err).lower()
    return "index not found" in text or "does not exist" in text


class VectorStore(Protocol):
    """Operations the pipeline needs from a vector backend.

    Implemented today only by :class:`MarqoStore`. This is the seam a second
    backend would plug into; nothing else in the codebase should import a
    backend client directly.
    """

    def search(self, index: str, **request: Any) -> dict:
        """Run a search. Raises :class:`VectorStoreError` on failure."""
        ...

    def get_document(self, index: str, doc_id: str) -> dict:
        """Fetch one indexed record by its backend id."""
        ...

    def delete_document(self, document_id: str, index: str) -> dict:
        """Purge every chunk of ``document_id``. Never raises."""
        ...

    def delete_chunk(self, document_id: str, chunk_num: int, index: str) -> dict:
        """Purge a single chunk. Never raises."""
        ...

    def get_settings(self, index: str) -> dict:
        """Live index settings."""
        ...

    def get_stats(self, index: str) -> dict:
        """Live index statistics."""
        ...

    def field_names(self, index: str) -> set[str]:
        """Field names the live index advertises."""
        ...

    def has_field(self, index: str, name: str) -> bool:
        """True when the index advertises ``name``. Never raises."""
        ...

    def index_exists(self, index: str) -> bool:
        """True when the index exists. Never raises."""
        ...

    def create_index(self, index: str, settings: dict) -> None:
        """Create ``index`` with the given backend settings."""
        ...

    def delete_index(self, index: str) -> None:
        """Drop ``index`` and everything in it."""
        ...


class MarqoStore:
    """:class:`VectorStore` backed by Marqo.

    The URL is read from ``MARQO_URL`` on every call rather than captured at
    construction, so tests and long-lived processes observe env changes the same
    way the previous inline call sites did.
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url

    @property
    def url(self) -> str:
        return self._url or os.environ.get("MARQO_URL", DEFAULT_MARQO_URL)

    def client(self):
        """Backend client. Imported lazily so importing this module is cheap."""
        import marqo

        return marqo.Client(url=self.url)

    def _index(self, index: str):
        return self.client().index(index)

    # -- reads ---------------------------------------------------------------

    def search(self, index: str, **request: Any) -> dict:
        try:
            return self._index(index).search(**request)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def get_document(self, index: str, doc_id: str) -> dict:
        try:
            return self._index(index).get_document(doc_id)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def get_settings(self, index: str) -> dict:
        try:
            return self._index(index).get_settings()
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def get_stats(self, index: str) -> dict:
        try:
            return self._index(index).get_stats()
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def field_names(self, index: str) -> set[str]:
        """Names of the fields the live index advertises.

        Marqo reports these under ``allFields`` in the index settings, each entry
        a dict with a ``name``. Unwrapping that is the quirk this hides.
        """
        settings = self.get_settings(index)
        return {
            field.get("name")
            for field in (settings.get("allFields") or [])
            if isinstance(field, dict) and field.get("name")
        }

    def has_field(self, index: str, name: str) -> bool:
        """True when the index advertises ``name``.

        Never raises: an unreachable or missing index cannot be shown to support
        the field, and every caller treats "cannot confirm" as "does not have it".
        """
        try:
            return name in self.field_names(index)
        except VectorStoreError:
            return False

    def index_exists(self, index: str) -> bool:
        try:
            self.client().get_index(index)
            return True
        except Exception:
            return False

    # -- writes --------------------------------------------------------------

    def create_index(self, index: str, settings: dict) -> None:
        try:
            self.client().create_index(index, settings_dict=settings)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def delete_index(self, index: str) -> None:
        try:
            self.client().delete_index(index)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    # -- purges --------------------------------------------------------------
    #
    # These return a result dict rather than raising, because callers must
    # distinguish three outcomes: purged, benignly-nothing-to-purge, and failed.
    # Only the third may abort a purge-before-flip sequence.

    def delete_chunk(self, document_id: str, chunk_num: int, index: str) -> dict:
        """Remove a single chunk from ``index``.

        Returns ``{"deleted": True, "chunk_id": ...}`` on success. On a benign
        miss returns ``deleted: False`` with a ``reason`` of ``not_found`` or
        ``index_missing``; on a real failure, ``deleted: False`` with ``error``.
        """
        marqo_doc_id = get_marqo_doc_id(document_id)
        try:
            index_handle = self.client().index(index)
            results = index_handle.search(
                q="",
                filter_string=f"doc_id:{marqo_doc_id} AND chunk_num:{chunk_num}",
                limit=1,
                attributes_to_retrieve=_PURGE_ATTRIBUTES,
            )
            if not results.get("hits"):
                return {"deleted": False, "reason": "not_found"}

            chunk_id = results["hits"][0]["_id"]
            index_handle.delete_documents(ids=[chunk_id])
            return {"deleted": True, "chunk_id": chunk_id}
        except Exception as error:
            # Missing index means nothing searchable — treat as already gone.
            if index_missing_error(error):
                return {"deleted": False, "reason": "index_missing"}
            return {"deleted": False, "error": str(error)}

    def delete_document(self, document_id: str, index: str) -> dict:
        """Remove every chunk of ``document_id`` from ``index``.

        Returns ``{"deleted": <count>, "doc_id": ...}``, plus ``reason:
        index_missing`` for a benign miss or ``error`` for a real failure.
        """
        try:
            index_handle = self.client().index(index)
            marqo_doc_id = get_marqo_doc_id(document_id)
            # Marqo has no delete-by-filter, so the ids have to be searched first.
            results = index_handle.search(
                q="",
                filter_string=f"doc_id:{marqo_doc_id}",
                limit=_MAX_CHUNKS_PER_DOCUMENT,
                attributes_to_retrieve=_PURGE_ATTRIBUTES,
            )
            if not results.get("hits"):
                return {"deleted": 0, "doc_id": marqo_doc_id}

            ids_to_delete = [hit["_id"] for hit in results["hits"]]
            if ids_to_delete:
                index_handle.delete_documents(ids=ids_to_delete)
            return {"deleted": len(ids_to_delete), "doc_id": marqo_doc_id}
        except Exception as error:
            # Missing index == nothing indexed for this tenant name yet.
            if index_missing_error(error):
                return {"deleted": 0, "doc_id": document_id, "reason": "index_missing"}
            return {"deleted": 0, "doc_id": document_id, "error": str(error)}


def get_vector_store() -> VectorStore:
    """The store the application should use.

    A function rather than a module-level instance so it stays a single patch
    point for tests and a single place to swap backends later.
    """
    return MarqoStore()
