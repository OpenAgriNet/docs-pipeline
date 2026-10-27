"""Shared test helpers: principals, document seeding, chunking events, one in-memory Marqo.

Fixtures built on these live in ``tests/conftest.py`` (``fake_marqo``,
``minio_uploads``).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from marqo import Client as _MarqoClient
from marqo.errors import MarqoWebError
from marqo.index import Index as _MarqoIndex

from pipeline.auth.jwt import claims_to_user
from pipeline.chunking.base import ChunkingResult
from pipeline.vector_store import passage_index_settings, passage_schema_field_names

#: Field names of the canonical passage schema, the default for a ``FakeIndex``.
PASSAGE_FIELDS = frozenset(passage_schema_field_names())


# =============================================================================
# Principals
# =============================================================================


def member(tenant, *roles: str, sub: str = "member"):
    """A caller holding ``roles`` inside ``tenant`` only.

    ``tenant`` may instead be a ``{tenant: [roles]}`` dict for a caller in
    several tenants. Pass ``sub`` when the caller's own user id matters (the
    tenant-membership self-guards compare it to the target user).
    """
    tenant_roles = dict(tenant) if isinstance(tenant, dict) else {tenant: list(roles)}
    return claims_to_user({"sub": sub, "tenant_roles": tenant_roles})


def master_admin(sub: str = "root"):
    """Realm ``master_admin``: control plane only, no tenant data."""
    return claims_to_user({"sub": sub, "realm_access": {"roles": ["master_admin"]}})


# =============================================================================
# Seeding
# =============================================================================


def seed_document(db, workflow_id, *, stage="registered", instance=None, pages=(), chunks=(), **fields):
    """Insert one document row, then its pages and chunks.

    ``pages`` / ``chunks`` items are full row dicts, or plain strings taken as
    the original text of page/chunk 1, 2, ... ``fields`` go to
    ``db.upsert_document``; ``document_id`` defaults to ``doc-<workflow_id>``,
    ``filename`` to ``doc.pdf`` and ``filepath`` to ``/tmp/<filename>``.
    """
    fields.setdefault("document_id", f"doc-{workflow_id}")
    fields.setdefault("filename", "doc.pdf")
    fields.setdefault("filepath", f"/tmp/{fields['filename']}")
    db.upsert_document(workflow_id=workflow_id, stage=stage, instance=instance, **fields)
    if pages:
        db.save_pages(workflow_id, [
            p if isinstance(p, dict) else {"page_number": n, "original_markdown": p}
            for n, p in enumerate(pages, start=1)
        ])
    if chunks:
        db.save_chunks(workflow_id, [
            c if isinstance(c, dict) else {"chunk_number": n, "original_text": c}
            for n, c in enumerate(chunks, start=1)
        ])


# =============================================================================
# Chunking
# =============================================================================


def window_chunk(text: str, page: int, token_count: int = 2) -> dict:
    """One chunk as a provider's checkpoint window emits it."""
    return {
        "text": text, "page_start": page, "page_end": page, "source_page_numbers": [page], "source_spans": [],
        "token_count": token_count, "section_title": "", "content_type": "body", "is_reference": False,
    }


def window_event(pages_processed: int, *chunks: dict) -> dict:
    """Provider progress event: one more window succeeded, emitting ``chunks``."""
    return {"pages_processed": pages_processed, "window_succeeded": True, "checkpoint_window_chunks": list(chunks)}


def chunking_result(config, *chunks) -> ChunkingResult:
    return ChunkingResult(
        chunks=list(chunks), provider=config.provider, model=config.model, config=config,
        stats={"chunk_count": len(chunks)},
    )


# =============================================================================
# Marqo
# =============================================================================

_FILTER_TOKEN = re.compile(
    r"\s*(?:(?P<term>[A-Za-z_]\w*:(?:\((?:\\.|[^\\)])*\)|[^\s()]+))|(?P<op>AND|OR|NOT)\b|(?P<paren>[()]))"
)


def _term_matches(record, field, wanted) -> bool:
    actual = record.get(field)
    values = actual if isinstance(actual, (list, tuple)) else [actual]
    return any(v is not None and (str(v).lower() if isinstance(v, bool) else str(v)) == wanted for v in values)


def _compile_filter(text: str | None):
    """``(predicate, fields named)`` for Marqo's filter grammar.

    ``field:value`` and ``field:(escaped value)`` terms joined by ``AND`` / ``OR``
    / ``NOT`` and parentheses. A term matches exactly (bools as ``true`` /
    ``false``; a list field when any element does); a record without the field
    never matches. The boolean structure is handed to Python's own parser: the
    compiled expression holds only ``and``/``or``/``not``/parentheses and term
    lookups, never filter text, and a malformed filter raises.
    """
    if not (text or "").strip():
        return (lambda _record: True), set()
    terms, parts, pos, text = [], [], 0, text.strip()
    while pos < len(text):
        match = _FILTER_TOKEN.match(text, pos)
        if not match:
            raise ValueError(f"fake Marqo cannot parse filter {text!r} at {pos}")
        token, pos = match.group(match.lastgroup), match.end()
        if match.lastgroup == "term":
            field, _, wanted = token.partition(":")
            if wanted.startswith("("):
                wanted = re.sub(r"\\(.)", r"\1", wanted[1:-1])
            terms.append((field, wanted))
            parts.append(f"t({len(terms) - 1})")
        else:
            parts.append(token.lower())
    code = compile(" ".join(parts), "<marqo filter>", "eval")

    def predicate(record):
        return eval(code, {"t": lambda i: _term_matches(record, *terms[i])})

    return predicate, {field for field, _ in terms}


def _real_signature(cls, method: str, *args, **kwargs) -> dict:
    """Bind the arguments as the installed Marqo client's ``method`` would; ``TypeError`` for any it does not take."""
    arguments = inspect.signature(getattr(cls, method)).bind(None, *args, **kwargs).arguments
    arguments.pop("self")
    return arguments


def _marqo_error(status: int, code: str, message: str) -> MarqoWebError:
    """What the real client raises for an HTTP error response: the JSON body as ``message``."""
    body = {"message": message, "code": code, "type": "invalid_request", "link": ""}
    return MarqoWebError(body, status_code=status, error_type="invalid_request", code=code)


def _not_found(name: str) -> MarqoWebError:
    return _marqo_error(404, "index_not_found", f"Index {name} not found")


#: Marqo's search caps: ``limit`` at most 1000, ``limit + offset`` at most 10000.
_MAX_LIMIT, _MAX_RETRIEVABLE = 1000, 10000

#: Passage-schema fields declared without the ``filter`` feature.
_UNFILTERABLE = frozenset(
    f["name"] for f in passage_index_settings()["allFields"] if "filter" not in f.get("features", ())
)


class FakeIndex:
    """In-memory structured Marqo index.

    It enforces the rules production has been bitten by: a filter may only name
    a filterable field the index declares (HTTP 400 otherwise), ``_id`` is not a
    retrievable attribute (prod hotfix #55), and search pages are capped like
    Marqo's (``limit`` <= 1000, ``limit + offset`` <= 10000). ``add_documents``
    upserts by ``_id``, generating one when a document has none.

    Filter values match exactly, so the substring match production assumes for
    a multi-tag ``domain_tags`` field is not modelled.

    Recorders (every attempt is logged, including one made to raise):
    ``searches`` (search kwargs; ``filters`` lists their filter strings),
    ``added`` (batches), ``deleted`` (id lists). ``rejected`` maps ``_id`` to the
    item error ``add_documents`` reports for it (``{"status": 400, "error": ...,
    "code": ...}``) instead of storing it.
    """

    def __init__(self, name="index", records=(), *, fields=None, tensor_fields=("text_for_embedding",)):
        self.name = name
        self.records = [dict(r) for r in records]
        self.fields = set(PASSAGE_FIELDS if fields is None else fields)
        self.tensor_fields = list(tensor_fields)
        self.searches: list[dict] = []
        self.added: list[list[dict]] = []
        self.deleted: list[list[str]] = []
        self.rejected: dict[str, dict] = {}
        self.client: FakeMarqo | None = None
        self._missing = False
        self._faults, self._calls = {}, {}

    @property
    def filters(self) -> list[str | None]:
        """The ``filter_string`` of every search, in order."""
        return [s.get("filter_string") for s in self.searches]

    def fail(self, method: str, error: BaseException, *, on_call: int | None = None):
        """Make ``method`` raise ``error`` on every call, or only on call ``on_call`` (1-based).

        Returns ``self`` so calls chain. For misbehaviour that is not a raise (a
        delete that silently does nothing), monkeypatch the method on the instance.
        """
        getattr(type(self), method)  # AttributeError for a method the fake does not have
        self._faults[method] = (error, on_call)
        return self

    def _call(self, method: str, *args, **kwargs) -> dict:
        arguments = _real_signature(_MarqoIndex, method, *args, **kwargs)
        if self._missing:
            raise _not_found(self.name)
        count = self._calls[method] = self._calls.get(method, 0) + 1
        error, on_call = self._faults.get(method, (None, None))
        if error is not None and on_call in (None, count):
            raise error
        return arguments

    def get_settings(self):
        self._call("get_settings")
        return {
            "allFields": [{"name": n, "type": "text"} for n in sorted(self.fields)],
            "tensorFields": list(self.tensor_fields),
        }

    def get_stats(self):
        self._call("get_stats")
        return {"numberOfDocuments": len(self.records)}

    def get_document(self, *args, **kwargs):
        _id = self._call("get_document", *args, **kwargs)["document_id"]
        for record in self.records:
            if record.get("_id") == _id:
                return dict(record)
        raise _marqo_error(404, "document_not_found", f"Document with _id {_id} not found")

    def search(self, *args, **kwargs):
        params = _real_signature(_MarqoIndex, "search", *args, **kwargs)
        self.searches.append(params)
        if self.client is not None:
            self.client.searches.append((self.name, params))
        self._call("search", *args, **kwargs)
        attributes = params.get("attributes_to_retrieve")
        if attributes is not None and "_id" in attributes:
            raise _marqo_error(400, "invalid_argument", "`_id` is not a valid attribute to retrieve")
        offset, limit = params.get("offset") or 0, params.get("limit", 10)
        if limit > _MAX_LIMIT or offset + limit > _MAX_RETRIEVABLE:
            raise _marqo_error(400, "illegal_request", f"limit {limit} / offset {offset} is over Marqo's cap")
        predicate, named = _compile_filter(params.get("filter_string"))
        unknown = sorted(named - (self.fields - _UNFILTERABLE) - {"_id"})
        if unknown:
            raise _marqo_error(400, "invalid_argument", f"Index has no filterable field {unknown[0]}")
        page = [r for r in self.records if predicate(r)][offset : offset + limit]
        if attributes is None:
            return {"hits": [dict(r) for r in page]}
        keep = [*attributes, "_id"]
        return {"hits": [{k: r[k] for k in keep if k in r} for r in page]}

    def add_documents(self, *args, **kwargs):
        batch = [dict(d) for d in _real_signature(_MarqoIndex, "add_documents", *args, **kwargs)["documents"]]
        self.added.append(batch)
        self._call("add_documents", *args, **kwargs)
        items = []
        for doc in batch:
            if doc.get("_id") in self.rejected:
                items.append({"_id": doc["_id"], **self.rejected[doc["_id"]]})
                continue
            doc.setdefault("_id", uuid.uuid4().hex)
            self.records[:] = [r for r in self.records if r.get("_id") != doc["_id"]]
            self.records.append(doc)
            items.append({"_id": doc["_id"], "status": 200})
        return {"errors": any(i["status"] != 200 for i in items), "items": items}

    def delete_documents(self, *args, **kwargs):
        ids = list(_real_signature(_MarqoIndex, "delete_documents", *args, **kwargs)["ids"])
        self.deleted.append(ids)
        self._call("delete_documents", *args, **kwargs)
        self.records[:] = [r for r in self.records if r.get("_id") not in set(ids)]
        return {"errors": False}


class FakeMarqo:
    """In-memory Marqo client, shared by every client production builds.

    Install it with the ``fake_marqo`` fixture (or ``FakeMarqo().install(monkeypatch)``).
    That replaces ``sys.modules["marqo"]``, which every production path reaches
    through ``MarqoStore.client()``'s lazy ``import marqo`` (including
    ``get_vector_store(url=...)``). Patching ``vector_store.get_vector_store``
    would miss modules that import it by name (``temporal.document_tasks``,
    ``services.search``). Every call is bound against the installed client's
    real signature.

    An index exists only once ``add_index`` or ``create_index`` registered it;
    any call on another name raises Marqo's ``index_not_found`` error, except
    ``delete_index``, which the real client lets pass.
    Recorders: ``constructions`` (one URL per ``marqo.Client(url=...)``),
    ``searches`` (``(index name, kwargs)`` across every index),
    ``created_indexes``, ``deleted_indexes``.

    Before asserting a recorder is EMPTY, prove the fake was bound in the same
    test (``assert_constructed()``, or a positive on the same fake): if that
    lazy import moves, the fake silently stops binding and an empty recorder
    passes vacuously.
    """

    def __init__(self):
        self.indexes: dict[str, FakeIndex] = {}
        self.constructions: list[str | None] = []
        self.searches: list[tuple[str, dict]] = []
        self.created_indexes: list[str] = []
        self.deleted_indexes: list[str] = []

    def install(self, monkeypatch) -> FakeMarqo:
        monkeypatch.setitem(sys.modules, "marqo", SimpleNamespace(Client=self.connect))
        return self

    def reset(self) -> None:
        """Clear every recorder; registered indexes stay."""
        for recorder in (self.constructions, self.searches, self.created_indexes, self.deleted_indexes):
            recorder.clear()

    def assert_constructed(self, why: str = "") -> None:
        assert self.constructions, (
            "the fake Marqo client was NEVER constructed: the fake no longer binds to the code "
            "under test, so any 'this never happened' assertion here is vacuously true. " + why
        )

    def connect(self, *args, **kwargs) -> FakeMarqo:
        """Stands in for ``marqo.Client``; also usable as a ``client_factory``."""
        self.constructions.append(_real_signature(_MarqoClient, "__init__", *args, **kwargs).get("url"))
        return self

    def add_index(self, name, records=(), **kwargs) -> FakeIndex:
        """Register (or replace) index ``name``; kwargs go to ``FakeIndex``."""
        index = FakeIndex(name, records, **kwargs)
        index.client = self
        self.indexes[name] = index
        return index

    def index(self, *args, **kwargs) -> FakeIndex:
        name = _real_signature(_MarqoClient, "index", *args, **kwargs)["index_name"]
        if name in self.indexes:
            return self.indexes[name]
        missing = FakeIndex(name)
        missing.client, missing._missing = self, True
        return missing

    def get_index(self, *args, **kwargs) -> FakeIndex:
        name = _real_signature(_MarqoClient, "get_index", *args, **kwargs)["index_name"]
        if name not in self.indexes:
            raise _not_found(name)
        return self.indexes[name]

    def get_indexes(self, *args, **kwargs):
        _real_signature(_MarqoClient, "get_indexes", *args, **kwargs)
        return {"results": [{"indexName": n} for n in self.indexes]}

    def create_index(self, *args, **kwargs):
        arguments = _real_signature(_MarqoClient, "create_index", *args, **kwargs)
        name, settings = arguments["index_name"], arguments.get("settings_dict")
        self.created_indexes.append(name)
        if not isinstance(settings, dict):
            raise TypeError(f"the fake builds indexes from settings_dict only, got {arguments}")
        if name in self.indexes:
            raise _marqo_error(409, "index_already_exists", f"Index {name} already exists")
        self.add_index(
            name,
            fields={f["name"] for f in settings.get("allFields") or []},
            tensor_fields=settings.get("tensorFields") or (),
        )
        return {"acknowledged": True}

    def delete_index(self, *args, **kwargs):
        name = _real_signature(_MarqoClient, "delete_index", *args, **kwargs)["index_name"]
        self.deleted_indexes.append(name)
        if self.indexes.pop(name, None) is None:
            return _not_found(name).message
        return {"acknowledged": True}


# =============================================================================
# Routes
# =============================================================================


def http_status(call) -> int:
    """Status code of the ``HTTPException`` that ``call`` raises.

    ``call`` is a coroutine (run to completion) or a zero-argument callable.
    """
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call) if inspect.iscoroutine(call) else call()
    return exc.value.status_code


def app_route(method: str, path: str):
    """The app route registered with exactly this path template and method."""
    from pipeline.app import app

    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or set()):
            return route
    raise AssertionError(f"route not registered: {method} {path}")


def dependency_calls(route) -> set:
    """Every dependency callable in ``route``'s dependant graph, nested ones included.

    For one route by template: ``dependency_calls(app_route("PUT", "/settings/search"))``.
    """
    calls, stack = set(), [route.dependant]
    while stack:
        dependant = stack.pop()
        calls.add(dependant.call)
        stack.extend(dependant.dependencies)
    return calls
