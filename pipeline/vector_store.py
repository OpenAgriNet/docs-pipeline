"""Vector-store adapter — the single place Marqo's quirks are encoded.

Before this module every route re-encoded Marqo's behaviour by hand: that the
client is built from ``MARQO_URL`` with a lazy ``import marqo``, that a delete
needs a search for ``_id`` first, that a missing index raises rather than
returning empty, that schema introspection means walking ``allFields``. Two
production incidents came out of that duplication — a filter naming an absent
``instance`` field, and a request for a non-existent ``_id`` attribute (#55) —
which is what this module exists to stop.

Nothing else in ``pipeline/`` imports ``marqo``, builds a client, reads a
``MARQO_*`` variable, or knows Marqo's filter grammar, index schema or response
shapes. Ops ``scripts/`` reach Marqo only through :func:`get_vector_store`.
``tests/test_vector_store.py`` scans the package (and scripts) and asserts it.
What lives here, therefore:

* the client and the endpoint — ONE definition, formerly five;
* the ``field:value`` filter grammar, including the domain-tag encoding;
* the canonical passage schema and what a live index looks like against it;
* physical index NAMING (``<ns><instance>-<name>``) — the registry TABLE stays
  in ``pipeline.db``;
* the read, write and purge calls themselves.

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

Ingest POLICY is NOT here, and that boundary is load-bearing. :meth:`describe_index`
reports; whether an absent index gets provisioned, whether schema drift is a
warning or a stop, and whether a tensor-less index may be written to stay in
``activities.ingest_to_marqo``. In particular a settings read that FAILS is never
flattened into an empty report — "I could not read the index" and "the index
declares no fields" lead to opposite decisions, and conflating them once made a
transient blip look like confirmed drift.

The semantics here are load-bearing: ``workflow_id`` purge scoping from #73,
the deliberately asymmetric capability probes, the page-and-re-search purge
loop with no chunk cap, the ``doc_id`` retrieval attribute from #55, and the
never-implicitly-recreate rule. HTTP and tenant policy remain in the services
that call this adapter.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

DEFAULT_MARQO_URL = "http://localhost:8882"

# Physical index of the legacy single-index deployment. The index service and
# registry use this as the transitional default.
DEFAULT_PHYSICAL_INDEX = "documents-index"

# Prefix for *new* per-tenant physical index names.
DEFAULT_INDEX_NAMESPACE = "t-"

# Logical index name charset — deliberately WITHOUT ``-`` so the single ``-``
# joining instance and name in a physical index name is an unambiguous separator.
# One physical name can therefore only ever map to one ``(instance, name)`` pair.
INDEX_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")

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


def default_physical_index() -> str:
    """Physical Marqo index of the legacy single-index deployment (transitional)."""
    return (
        os.environ.get("MARQO_INDEX_NAME") or DEFAULT_PHYSICAL_INDEX
    ).strip() or DEFAULT_PHYSICAL_INDEX


def index_namespace() -> str:
    """Prefix for *new* per-tenant physical index names (e.g. ``t-``)."""
    return os.environ.get("MARQO_INDEX_NAMESPACE", DEFAULT_INDEX_NAMESPACE)


def is_valid_logical_index_name(name: str | None) -> bool:
    """True when ``name`` is a logical index name safe to join into a physical one.

    Only what a *bad* name means is left to the caller, deliberately: the API
    turns it into a 400, while the registry falls back to the tenant's own
    ``default``. Those two reactions are policy, not naming, and they disagree.
    """
    return bool(INDEX_NAME_RE.fullmatch(name or ""))


def physical_index_name(instance: str, name: str) -> str:
    """Physical name for a per-tenant index: ``<ns><instance>-<name>``.

    Callers pass an already-normalised ``instance`` and a ``name`` they have
    checked with :func:`is_valid_logical_index_name`. Because the instance is
    regex-validated and the name can never contain ``-``, the single ``-``
    between them is an unambiguous separator: the result can never alias a
    different ``(instance, name)`` pair — which would otherwise let one tenant
    address (or destroy) another tenant's physical index.
    """
    return f"{index_namespace()}{instance}-{name}"


# =============================================================================
# Filter grammar
#
# Every ``field:value`` string the pipeline sends to Marqo is built here. It used
# to be spread across the routes, the purge path and the domain-tag helpers, and
# a filter naming a field the index does not declare is a hard 400 on the whole
# query — so the grammar gets exactly one home.
# =============================================================================


def escape_filter_term(value: str) -> str:
    """Escape a value for use inside a parenthesised Marqo filter term."""
    # Keep ":" intact — tags are dimension:value and Marqo filter syntax uses that form.
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def term_filter(field_name: str, value: Any) -> str:
    """Bare ``field:value`` clause, for values that need no escaping (ids, ints)."""
    return f"{field_name}:{value}"


def field_filter(field_name: str, value: str) -> str:
    """Parenthesised ``field:(value)`` clause with the value escaped."""
    return f"{field_name}:({escape_filter_term(value)})"


def any_of_filter(field_name: str, values: Sequence[str]) -> str:
    """``(field:(a) OR field:(b))`` — one clause per allowed value."""
    return "(" + " OR ".join(field_filter(field_name, value) for value in values) + ")"


def merge_filter_strings(*parts: str | None) -> str | None:
    """AND together the non-empty filter clauses, or ``None`` if there are none."""
    clauses = [part.strip() for part in parts if part and part.strip()]
    if not clauses:
        return None
    return " AND ".join(clauses)


# -- domain tags -------------------------------------------------------------
#
# Tags are stored in one flat, pipe-delimited ``domain_tags`` text field because a
# structured Marqo index has no list-membership filter. The leading/trailing pipes
# are what make a substring filter match whole tags, so writing the field and
# building the filter are two halves of one encoding and live together.


def tags_from_marqo_field(value: str | None) -> list[str]:
    """Split a stored ``domain_tags`` field back into tag keys."""
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def normalize_domain_tags_field(value: str | None) -> str:
    """Normalize a flat tag string into delimited Marqo filter form."""
    keys = tags_from_marqo_field(value)
    if not keys:
        return ""
    return "|" + "|".join(keys) + "|"


def tags_to_marqo_field(tags: list) -> str:
    """Pipe-delimited flat tag string for the Marqo ``domain_tags`` field.

    Wrapped with leading/trailing pipes so substring filters match whole tags
    (e.g. ``|region:north|`` does not match ``|region:northern|``).
    """
    keys = sorted({tag.key() for tag in tags})
    return normalize_domain_tags_field("|".join(keys))


def build_domain_tags_filter(tags: Iterable[str]) -> str | None:
    """Build a Marqo filter clause requiring all listed dimension:value tags."""
    # Function-local: ``domain_tags`` may import this module for the re-export
    # shim, and the adapter must never be the one to close that loop.
    from .domain_tags.base import normalize_tag_key

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        key = normalize_tag_key(raw if isinstance(raw, str) else str(raw))
        if not key or key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    if not normalized:
        return None
    # Match delimited whole tags stored by tags_to_marqo_field.
    return " AND ".join(
        field_filter("domain_tags", "|" + tag + "|") for tag in normalized
    )


# =============================================================================
# The canonical passage schema
#
# What a Marqo index this pipeline provisions looks like: which fields exist,
# which are filterable, and which one carries the embedding. Ingest, the admin
# create/reset routes and the tenant index routes all needed it, so it sat in
# ``activities`` and was imported backwards by the API.
# =============================================================================

_PASSAGE_TENSOR_FIELD = "text_for_embedding"

# Optional fields: an index that predates them is a schema *drift*, not a
# mismatch that should stop an ingest.
_OPTIONAL_PASSAGE_FIELDS = {"domain_tags", "instance"}


def passage_index_settings(
    model: str | None = None,
    overrides: Optional[dict] = None,
) -> dict:
    """Marqo settings for the canonical passage schema.

    ``model`` and ``overrides`` are arguments rather than post-hoc mutations of a
    returned dict: the create-index routes used to reach into the result and edit
    it, which made "what schema did we ask for" a two-place question.
    """
    all_fields = [
        {"name": "doc_id", "type": "text", "features": ["filter"]},
        {"name": "workflow_id", "type": "text", "features": ["filter"]},
        {"name": "instance", "type": "text", "features": ["filter"]},
        {"name": "type", "type": "text", "features": ["filter"]},
        {"name": "source", "type": "text", "features": ["filter"]},
        {"name": "filename", "type": "text", "features": ["filter"]},
        {"name": "name_gu", "type": "text", "features": ["filter"]},
        {"name": "name_en", "type": "text", "features": ["filter"]},
        {"name": "title_en", "type": "text", "features": ["filter"]},
        {"name": "title_gu", "type": "text", "features": ["filter"]},
        {"name": "doc_language", "type": "text", "features": ["filter"]},
        {"name": "category_tags", "type": "text", "features": ["filter"]},
        {"name": "doc_short_description", "type": "text", "features": ["filter"]},
        {"name": "doc_llm_description", "type": "text", "features": ["filter"]},
        {"name": "ingestion_status", "type": "text", "features": ["filter"]},
        {"name": "description", "type": "text", "features": ["lexical_search"]},
        {"name": "chunk_num", "type": "int", "features": ["filter"]},
        {"name": "section", "type": "text", "features": ["filter"]},
        {"name": "token_count", "type": "int", "features": ["filter"]},
        {"name": "page_start", "type": "int", "features": ["filter"]},
        {"name": "page_end", "type": "int", "features": ["filter"]},
        {"name": "is_reference", "type": "bool", "features": ["filter"]},
        {"name": "quality_score", "type": "float", "features": ["filter"]},
        {"name": "priority_rank", "type": "float", "features": ["filter"]},
        {"name": "domain_tags", "type": "text", "features": ["filter"]},
        {"name": "text", "type": "text", "features": ["lexical_search"]},
        {"name": "priority", "type": "float", "features": ["score_modifier", "filter"]},
        {"name": _PASSAGE_TENSOR_FIELD, "type": "text"},
    ]

    settings = {
        "type": "structured",
        "vectorNumericType": "float",
        "model": "hf/multilingual-e5-large",
        "normalizeEmbeddings": False,
        "textPreprocessing": {"splitLength": 3, "splitOverlap": 1, "splitMethod": "sentence"},
        "allFields": all_fields,
        "tensorFields": [_PASSAGE_TENSOR_FIELD],
    }
    if model:
        settings["model"] = model
    if isinstance(overrides, dict):
        settings.update(overrides)
    return settings


def passage_schema_field_names() -> set[str]:
    """Field names of the canonical passage schema."""
    return field_names_from_settings(passage_index_settings())


def core_passage_schema_field_names() -> set[str]:
    """Required passage fields.

    Optional fields like ``domain_tags`` and ``instance`` do not count as a
    missing core field, so an index predating them is not reported as broken.
    """
    return passage_schema_field_names() - _OPTIONAL_PASSAGE_FIELDS


def field_names_from_settings(settings: Any) -> set[str]:
    """Field names out of a Marqo index-settings payload.

    Marqo reports them under ``allFields``, each entry a dict with a ``name``.
    Unwrapping that is the quirk this hides; it used to be open-coded in four
    places.
    """
    if not isinstance(settings, dict):
        return set()
    return {
        f.get("name")
        for f in (settings.get("allFields") or [])
        if isinstance(f, dict) and f.get("name")
    }


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
# ``get_settings()`` — notably the tenant-scoping filter in
# ``pipeline.services.indexes``, where the authorization policy lives.
# =============================================================================


def index_field_names(index) -> set[str]:
    """Names of the fields a live index advertises.

    Raises whatever the backend raised — the callers below decide what a failure
    means, and they do NOT agree, on purpose.
    """
    return field_names_from_settings(index.get_settings())


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
    ``ingestion_records.prepare_records``) to keep the purge inside one document.

    ``workflow_id=None`` yields the UNSCOPED filter. It is used only where the
    index cannot answer a ``workflow_id`` filter at all, or as the stray probe in
    :func:`purge_ids` — never as an automatic retry that deletes whatever it
    finds.
    """
    scope = term_filter("doc_id", get_marqo_doc_id(document_id))
    if workflow_id:
        scope = merge_filter_strings(scope, term_filter("workflow_id", workflow_id))
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


def project_records(
    records: Sequence[dict],
    allowed: set[str],
    index_fields: set[str],
) -> list[dict]:
    """Drop fields the target index would reject, returning a NEW list.

    Marqo rejects a document carrying any field the structured index does not
    declare, so an older index would refuse every document rather than ingest the
    subset it understands. Projecting instead means an older index needs no
    migration — and, crucially, no destructive recreate — to keep accepting
    documents.

    ``allowed`` is the canonical passage schema; ``index_fields`` is what the live
    index actually advertises (empty means "unknown", so do not narrow on it).

    Returns a new list rather than editing in place: the caller's list is also the
    payload archived to object storage, and rewriting it under the archiver made
    the export and the ingest disagree about what was sent.
    """
    if not allowed:
        return list(records)
    projected: list[dict] = []
    for record in records:
        normalized = {"_id": record.get("_id")}
        for key, value in record.items():
            if key == "_id":
                continue
            if key not in allowed:
                continue
            if index_fields and key not in index_fields:
                continue
            normalized[key] = value
        projected.append(normalized)
    return projected


@dataclass(frozen=True)
class IndexSchemaReport:
    """What a live index looks like, measured against the passage schema.

    Reports, never decides. Whether an absent index should be provisioned,
    whether drift is a warning or a failure, and whether a tensor-less index may
    be written to are ingest POLICY and stay with the caller — the adapter
    deciding any of them would put an irreversible action behind a data type.
    """

    exists: bool
    field_names: set[str] = field(default_factory=set)
    tensor_fields: set[str] = field(default_factory=set)
    missing_core: list[str] = field(default_factory=list)

    @property
    def has_passage_tensor(self) -> bool:
        """True when the index embeds the canonical passage tensor field."""
        return _PASSAGE_TENSOR_FIELD in self.tensor_fields


@dataclass(frozen=True)
class AddResult:
    """Outcome of a batched write. ``errors`` is Marqo's per-item failures."""

    batches: int = 0
    errors: list[dict] = field(default_factory=list)


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

    def describe_index(self, index: str) -> IndexSchemaReport:
        """Measure a live index against the passage schema. Raises on a
        backend failure — an unreadable index is not an empty one."""
        ...

    def add_documents(
        self,
        index: str,
        records: Sequence[dict],
        batch_size: int = 10,
        on_batch: Optional[Callable[[list[dict], dict], None]] = None,
    ) -> AddResult:
        """Write ``records`` in batches."""
        ...

    def create_index(self, index: str, settings: dict) -> None:
        """Create ``index`` with the given backend settings."""
        ...

    def delete_index(self, index: str) -> None:
        """Drop ``index`` and everything in it."""
        ...

    def list_indexes(self) -> list[Any]:
        """Backend index listing (ops / debug scripts)."""
        ...

    def update_documents(self, index: str, records: Sequence[dict]) -> Any:
        """Partial update of existing records (ops backfill scripts)."""
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

    ``client_factory`` exists so a caller (in practice, a test) can supply its
    own client construction — it is the one seam the suite swaps. Default is a
    lazy ``import marqo`` against :func:`marqo_url`, resolved per call. The lazy
    import is load-bearing twice over: importing this module stays cheap, and the
    fakes bind by patching ``sys.modules["marqo"]``, which a module-level import
    would defeat.
    """

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
        *,
        url: Optional[str] = None,
    ) -> None:
        self._client_factory = client_factory
        self._url_override = (url or "").strip() or None

    @property
    def url(self) -> str:
        return self._url_override or marqo_url()

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
        try:
            return index_field_names(self._index(index))
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def index_exists(self, index: str) -> bool:
        """True when the index physically exists. Never raises — Marqo signals
        "no such index" by raising from ``get_index``."""
        try:
            self.client().get_index(index)
            return True
        except Exception:
            return False

    def describe_index(self, index: str) -> IndexSchemaReport:
        """What the live index declares, measured against the passage schema.

        A missing index yields ``exists=False`` and nothing else — there is no
        schema to report. A settings read that *fails* is NOT flattened into an
        empty report: it propagates untouched, because "I could not read the
        index" and "the index declares no fields" lead to opposite decisions and
        conflating them once meant a transient blip looked like confirmed drift.
        """
        if not self.index_exists(index):
            return IndexSchemaReport(exists=False)

        settings = self._index(index).get_settings()
        if not isinstance(settings, dict):
            settings = {}
        names = field_names_from_settings(settings)
        return IndexSchemaReport(
            exists=True,
            field_names=names,
            tensor_fields=set(settings.get("tensorFields") or []),
            missing_core=sorted(core_passage_schema_field_names() - names) if names else [],
        )

    # -- writes --------------------------------------------------------------

    def create_index(self, index: str, settings: dict) -> None:
        self.client().create_index(index, settings_dict=settings)

    def add_documents(
        self,
        index: str,
        records: Sequence[dict],
        batch_size: int = 10,
        on_batch: Optional[Callable[[list[dict], dict], None]] = None,
    ) -> AddResult:
        """Write ``records`` to ``index`` in batches of ``batch_size``.

        Marqo reports a partial failure inside a 200 response: a truthy ``errors``
        flag plus per-item ``status``/``error``/``message``/``code``. Unwrapping
        that shape is the quirk this hides; whether a failed item aborts the
        ingest is the caller's decision.

        ``on_batch(errors, raw_result)`` is a plain callable invoked after every
        batch — plain so no workflow-engine type reaches this module. Raising
        from it stops the write where it is.
        """
        handle = self._index(index)
        all_errors: list[dict] = []
        batches = 0
        for start in range(0, len(records), batch_size):
            result = handle.add_documents(list(records[start : start + batch_size]))
            batches += 1
            errors: list[dict] = []
            if result.get("errors"):
                for item in result.get("items") or []:
                    if item.get("status") != 200:
                        errors.append({
                            "_id": item.get("_id"),
                            "status": item.get("status"),
                            "error": item.get("error"),
                            "message": item.get("message"),
                            "code": item.get("code"),
                        })
            all_errors.extend(errors)
            if on_batch is not None:
                on_batch(errors, result)
        return AddResult(batches=batches, errors=all_errors)

    def delete_index(self, index: str) -> None:
        self.client().delete_index(index)

    def list_indexes(self) -> list[Any]:
        try:
            payload = self.client().get_indexes()
        except Exception as error:
            raise VectorStoreError(str(error)) from error
        if isinstance(payload, dict):
            return list(payload.get("results") or [])
        return list(payload or [])

    def update_documents(self, index: str, records: Sequence[dict]) -> Any:
        try:
            return self._index(index).update_documents(list(records))
        except Exception as error:
            raise VectorStoreError(str(error)) from error

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
                extra_filter=term_filter("chunk_num", chunk_num),
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


def get_vector_store(
    client_factory: Optional[Callable[[], Any]] = None,
    *,
    url: Optional[str] = None,
) -> VectorStore:
    """The store the application (and ops scripts) should use.

    A function rather than a module-level instance so it stays a single patch
    point for tests and a single place to swap backends later.

    ``url`` lets ops scripts target a non-default Marqo endpoint without building
    a client themselves — construction stays inside this module.
    """
    if client_factory is not None and url is not None:
        raise ValueError("pass client_factory or url, not both")
    if url is not None:
        target = url.strip() or marqo_url()

        def _factory(target_url: str = target):
            import marqo

            return marqo.Client(url=target_url)

        return MarqoStore(client_factory=_factory, url=target)
    return MarqoStore(client_factory=client_factory)
