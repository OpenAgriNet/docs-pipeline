"""Vector-store adapter — the single place Marqo's quirks are encoded.

Before this module every route re-encoded Marqo's behaviour by hand: that the
client is built from ``MARQO_URL`` with a lazy ``import marqo``, that a delete
needs a search for ``_id`` first, that a missing index raises rather than
returning empty, that schema introspection means walking ``allFields``. Two
production incidents came out of that duplication — a filter naming an absent
``instance`` field, and a request for a non-existent ``_id`` attribute (#55) —
which is what this module exists to stop.

Layering rules, deliberately strict:

* **No FastAPI.** Failures raise :class:`VectorStoreError`; turning that into an
  HTTP status is the caller's policy decision, not the store's.
* **No auth.** The store never sees ``AuthUser``. Tenant scoping is built by the
  caller, which may ask the store (or :func:`index_has_instance_field`) whether
  an index can support a scoping filter at all.
* **No registry / DB.** The store takes *physical* index names. Resolving
  ``(instance, logical name) -> physical index`` stays with the caller.

The purge methods are the exception to "raise on failure": they return a result
dict instead. "Purged nothing", "the index does not exist" and "the backend
failed" are three different outcomes, and only the last may abort a
purge-before-flip sequence, so callers have to be able to tell them apart.

This is a behaviour-preserving extraction. Everything here was lifted verbatim
from ``pipeline.api``; the semantics that were paid for in production — the
``workflow_id`` purge scoping of #73, the deliberately asymmetric capability
probes, the page-and-re-search purge loop with no chunk cap, the ``doc_id``
retrieval attribute of #55 — are carried across unchanged.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Callable, Optional, Protocol

DEFAULT_MARQO_URL = "http://localhost:8882"

# A structured index rejects `_id` as a *retrievable* attribute and 400s the whole
# query, but returns `_id` on every hit anyway. So a purge asks for a real field
# and reads `_id` off the result. Named once here because every purge path needs
# it and asking for `_id` broke production against the live legacy index (#55).
_PURGE_ATTRIBUTES = ["doc_id"]

# Marqo has no delete-by-filter, so a purge is search-then-delete. This is the
# page size for the search half; the purge loops until the filter is empty, so it
# is NOT a ceiling on how many records a document may have (#73: the old fixed
# `limit=1000` silently truncated longer documents).
_MARQO_PURGE_PAGE = 1000


class VectorStoreError(RuntimeError):
    """A vector-store operation failed.

    Callers translate this into whatever status their surface needs — 400 for a
    failed search, 404 for a missing index, 502 for a failed purge.
    """


class MarqoPurgeScopeError(RuntimeError):
    """The index holds records under this ``doc_id`` that it cannot attribute.

    Deliberately loud. Raised when a scoped purge matched none of its own records
    while OTHER records share the ``doc_id``. Callers turn this into the normal
    fail-closed 502, so a disable/delete stops rather than guessing.
    """


class MarqoPurgeUnconfirmedError(RuntimeError):
    """The index still returns records that were just deleted.

    Raised when the purge loop issues a delete and the same ids come back on the
    next search. Returning them as deleted would report a removal that did not
    happen — the very failure (#73) this purge path exists to prevent.
    """


def get_marqo_doc_id(document_id: str) -> str:
    """Document identifier stored in the Marqo ``doc_id`` field."""
    return document_id


def get_legacy_marqo_doc_id(document_id: str) -> str:
    """Legacy hashed ``doc_id`` used before provenance ingest alignment."""
    return hashlib.md5(document_id.encode()).hexdigest()


def marqo_url() -> str:
    """The Marqo endpoint, read from the environment on every call.

    Read per call rather than captured at import so a long-lived process and a
    test observe env changes the same way the previous inline call sites did.
    """
    return os.environ.get("MARQO_URL", DEFAULT_MARQO_URL)


def index_missing_error(err: Exception | str) -> bool:
    """True when Marqo has no such index — equivalent to zero searchable chunks.

    Marqo signals this as a generic error whose message has to be sniffed, so the
    string match is a backend quirk and belongs here rather than at a route.
    """
    text = str(err).lower()
    return "index not found" in text or "does not exist" in text


# =============================================================================
# Handle-level helpers
#
# These take a live index handle rather than a name. They are used both by the
# store's own purge path and by callers that already hold something answering
# ``get_settings()`` — notably the tenant-scoping filter in ``pipeline.api``,
# which is auth policy and stays there.
# =============================================================================


def index_field_names(index) -> set[str]:
    """Names of the fields a live index advertises.

    Marqo reports these under ``allFields`` in the index settings, each entry a
    dict with a ``name``. Unwrapping that is the quirk this hides. Raises
    whatever the backend raised — the callers below decide what a failure means,
    and they do NOT agree, on purpose.
    """
    settings = index.get_settings()
    return {
        f.get("name")
        for f in (settings.get("allFields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def index_has_instance_field(index) -> bool:
    """True when the live Marqo index advertises a filterable `instance` field."""
    try:
        return "instance" in index_field_names(index)
    except Exception:
        return False


def index_has_workflow_id_field(index) -> bool:
    """True when the live Marqo index can answer a ``workflow_id:`` filter.

    Mirrors :func:`index_has_instance_field`. Marqo structured indexes accept a
    filter only on a field DECLARED at creation, and a filterable field cannot be
    added to an existing index in place — filtering on an undeclared one returns
    HTTP 400 ("index has no filterable field"). Indexes created before
    ``workflow_id`` joined the schema (see ``activities._index_settings``) would
    therefore reject the scoped purge filter outright, and the fail-closed purge
    path turns that into a 502 on every disable/delete.

    A probe FAILURE returns ``True`` (keep the narrow, scoped filter) — the
    OPPOSITE of :func:`index_has_instance_field`, deliberately. Both are
    fail-closed for their own direction: guessing "absent" here would silently
    widen every purge on a transient Marqo hiccup, whereas keeping the scoped
    filter means a genuinely missing field 400s and the purge stops, which is the
    right outcome for an unexplained error.
    """
    try:
        return "workflow_id" in index_field_names(index)
    except Exception:
        return True


def marqo_doc_scope_filter(document_id: str, workflow_id: Optional[str] = None) -> str:
    """Marqo filter selecting the records of ONE document.

    ``documents.workflow_id`` is the primary key and ``document_id`` carries no
    unique constraint, so two rows can share a ``document_id`` — the same bytes
    uploaded under two names is enough, since ``document_id`` is the content
    fingerprint while ``workflow_id`` derives from the path. A purge scoped only
    by ``doc_id`` therefore deleted the *other* document's records too (#73).
    Callers pass the ``workflow_id`` (stamped on every record by
    ``activities._prepare_records``) to keep the purge inside one document.

    ``workflow_id=None`` yields the UNSCOPED filter. It is used only where the
    index cannot answer a ``workflow_id`` filter at all, or as the stray probe in
    :func:`purge_ids` — never as an automatic retry that deletes whatever it
    finds.
    """
    scope = f"doc_id:{get_marqo_doc_id(document_id)}"
    if workflow_id:
        scope = f"{scope} AND workflow_id:{workflow_id}"
    return scope


def purge_ids(
    index,
    document_id: str,
    workflow_id: Optional[str],
    extra_filter: str = "",
    limit: int = _MARQO_PURGE_PAGE,
    check_ambiguity: bool = True,
) -> list[str]:
    """Record ids to purge for exactly one document. Never widens past it.

    * **Scoped** — the index declares ``workflow_id`` and the caller supplied
      one: ``doc_id AND workflow_id``. Whatever that matches is this document's.
      A scoped miss with no other records under the ``doc_id`` is simply "nothing
      to purge" (``[]``) — an already-purged document is not an error.
    * **Ambiguous** — a scoped miss while other records DO share the ``doc_id``.
      The index cannot tell "this document's records predate the ``workflow_id``
      stamp" from "this document was already purged and these belong to a
      co-resident document" — the two states are byte-identical — so we refuse
      and raise rather than widening the blast radius. Deleting another
      document's vectors is irreversible; failing loudly is not.
    * **Degraded** — the index cannot answer a ``workflow_id`` filter (or the
      caller passed none): ``doc_id`` is the only key such an index has, so use
      it. Per-document isolation is genuinely unavailable there — that is the
      pre-existing behaviour, not a regression, and it cannot be fixed by
      touching the index (see :func:`index_has_workflow_id_field`).
    """
    suffix = f" AND {extra_filter}" if extra_filter else ""

    def _search(scope: str) -> list[dict]:
        results = index.search(
            q="",
            filter_string=f"{scope}{suffix}",
            limit=limit,
            # `_id` is always in hits; a structured legacy index rejects `_id`
            # in attributes_to_retrieve (prod hotfix #55).
            attributes_to_retrieve=_PURGE_ATTRIBUTES,
        )
        return results.get("hits") or []

    if workflow_id and index_has_workflow_id_field(index):
        hits = _search(marqo_doc_scope_filter(document_id, workflow_id))
        if hits or not check_ambiguity:
            return [hit["_id"] for hit in hits]
        strays = _search(marqo_doc_scope_filter(document_id))
        if not strays:
            return []
        raise MarqoPurgeScopeError(
            f"{len(strays)} record(s) share doc_id "
            f"{get_marqo_doc_id(document_id)} but none belong to workflow "
            f"{workflow_id}; refusing to purge records this document may not own. "
            "This happens when the same file was ingested twice (a rename or a "
            "rerun), so the records cannot be attributed to one of them. To change "
            "this document's state without touching the index, retry with "
            "remove_from_search=false."
        )

    return [hit["_id"] for hit in _search(marqo_doc_scope_filter(document_id))]


def purge_document(
    index,
    document_id: str,
    workflow_id: Optional[str],
) -> list[str]:
    """Delete every record of one document, paging until the filter is empty.

    The search half is capped at ``_MARQO_PURGE_PAGE``, so a single pass left the
    tail of a longer document searchable behind a document the UI reports as
    removed (#73). Deleting each page shrinks the result set, so re-running the
    same search walks the whole document without needing offset paging.

    Only the FIRST page runs the ambiguity check: once a page has been deleted,
    the eventual empty result is this purge's own doing, not evidence of an
    unattributable record.
    """
    deleted: list[str] = []
    seen: set[str] = set()
    first = True
    while True:
        ids = purge_ids(index, document_id, workflow_id, check_ambiguity=first)
        first = False
        fresh = [i for i in ids if i not in seen]
        if not fresh:
            if ids:
                # The filter still matches records we already issued deletes for:
                # the delete did not take. Reporting success here would re-arm the
                # exact bug this function exists to fix (#73) — a document the UI
                # calls removed, still answering queries. Fail loudly instead.
                raise MarqoPurgeUnconfirmedError(
                    f"{len(ids)} record(s) for doc_id "
                    f"{get_marqo_doc_id(document_id)} are still searchable after "
                    "being deleted; the index did not accept the delete"
                )
            break
        index.delete_documents(ids=fresh)
        seen.update(fresh)
        deleted.extend(fresh)
    return deleted


# =============================================================================
# The seam
# =============================================================================


class VectorStore(Protocol):
    """Operations the pipeline needs from a vector backend.

    Implemented today only by :class:`MarqoStore`. This is where a second backend
    would plug in; outside this module nothing should import a backend client.
    """

    @property
    def url(self) -> str:
        """Endpoint the store talks to (reported by admin routes)."""
        ...

    def search(self, index: str, **request: Any) -> dict:
        """Run a search. Raises :class:`VectorStoreError` on failure."""
        ...

    def get_document(self, index: str, doc_id: str) -> dict:
        """Fetch one indexed record by its backend id."""
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

    def index_exists(self, index: str) -> bool:
        """True when the index exists. Never raises."""
        ...

    def create_index(self, index: str, settings: dict) -> None:
        """Create ``index`` with the given backend settings."""
        ...

    def delete_index(self, index: str) -> None:
        """Drop ``index`` and everything in it."""
        ...

    def delete_document(
        self, document_id: str, index: str, workflow_id: Optional[str] = None
    ) -> dict:
        """Purge every record of one document. Never raises."""
        ...

    def delete_chunk(
        self,
        document_id: str,
        chunk_num: int,
        index: str,
        workflow_id: Optional[str] = None,
    ) -> dict:
        """Purge a single chunk. Never raises."""
        ...


class MarqoStore:
    """:class:`VectorStore` backed by Marqo.

    ``client_factory`` exists so a caller can supply its own client construction
    (``pipeline.api`` passes its ``_marqo_client``, which is what the existing
    suite patches). Default is a lazy ``import marqo`` against
    :func:`marqo_url`, resolved per call.
    """

    def __init__(self, client_factory: Optional[Callable[[], Any]] = None) -> None:
        self._client_factory = client_factory

    @property
    def url(self) -> str:
        return marqo_url()

    def client(self):
        """Backend client. ``marqo`` is imported lazily so importing this module
        stays cheap and remains patchable via ``sys.modules``."""
        if self._client_factory is not None:
            return self._client_factory()
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
        settings = self.get_settings(index)
        return {
            f.get("name")
            for f in (settings.get("allFields") or [])
            if isinstance(f, dict) and f.get("name")
        }

    def index_exists(self, index: str) -> bool:
        """True when the index physically exists. Never raises — Marqo signals
        "no such index" by raising from ``get_index``."""
        try:
            self.client().get_index(index)
            return True
        except Exception:
            return False

    # -- writes --------------------------------------------------------------

    def create_index(self, index: str, settings: dict) -> None:
        self.client().create_index(index, settings_dict=settings)

    def delete_index(self, index: str) -> None:
        self.client().delete_index(index)

    # -- purges --------------------------------------------------------------
    #
    # These return a result dict rather than raising, because callers must
    # distinguish three outcomes: purged, benignly-nothing-to-purge, and failed.
    # Only the third may abort a purge-before-flip sequence.

    def delete_chunk(
        self,
        document_id: str,
        chunk_num: int,
        index: str,
        workflow_id: Optional[str] = None,
    ) -> dict:
        """Delete a single chunk from Marqo, scoped to one document's own run.

        ``workflow_id`` is the owning document row's primary key. Without it the
        purge can reach a co-resident document sharing this ``document_id``
        (#73).

        Returns ``{"deleted": True, "chunk_id": ...}`` on success. On a benign
        miss, ``deleted: False`` with a ``reason`` of ``not_found`` or
        ``index_missing``; on a real failure, ``deleted: False`` with ``error``.
        """
        try:
            index_handle = self._index(index)

            chunk_ids = purge_ids(
                index_handle,
                document_id,
                workflow_id,
                extra_filter=f"chunk_num:{chunk_num}",
                # Not 1: the ambiguity probe must be able to see a co-resident
                # document's record for the same chunk_num.
                limit=10,
            )
            if not chunk_ids:
                return {"deleted": False, "reason": "not_found"}

            # Delete the chunk
            chunk_id = chunk_ids[0]
            index_handle.delete_documents(ids=[chunk_id])

            return {"deleted": True, "chunk_id": chunk_id}

        except Exception as e:
            # Missing index means nothing searchable — treat as already gone.
            if index_missing_error(e):
                return {"deleted": False, "reason": "index_missing"}
            return {"deleted": False, "error": str(e)}

    def delete_document(
        self, document_id: str, index: str, workflow_id: Optional[str] = None
    ) -> dict:
        """Delete all records for a document — and only that document's.

        ``workflow_id`` is the owning document row's primary key. Required to
        keep the purge off a co-resident document sharing this ``document_id``
        (#73).

        Returns ``{"deleted": <count>, "doc_id": ...}``, plus ``reason:
        index_missing`` for a benign miss or ``error`` for a real failure.
        """
        try:
            index_handle = self._index(index)
            marqo_doc_id = get_marqo_doc_id(document_id)

            ids_deleted = purge_document(index_handle, document_id, workflow_id)

            return {"deleted": len(ids_deleted), "doc_id": marqo_doc_id}

        except Exception as e:
            # Missing index == nothing indexed for this tenant name yet.
            if index_missing_error(e):
                return {"deleted": 0, "doc_id": document_id, "reason": "index_missing"}
            return {"deleted": 0, "doc_id": document_id, "error": str(e)}


def get_vector_store(client_factory: Optional[Callable[[], Any]] = None) -> VectorStore:
    """The store the application should use.

    A function rather than a module-level instance so it stays a single patch
    point for tests and a single place to swap backends later.
    """
    return MarqoStore(client_factory=client_factory)
