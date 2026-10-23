"""Shared test helpers: principals, one in-memory Marqo, document seeding.

Fixtures built on these live in ``tests/conftest.py`` (``fake_marqo``,
``minio_uploads``).
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

from pipeline.auth.jwt import claims_to_user
from pipeline.vector_store import passage_schema_field_names
from tests.marqo_binding import MarqoBindingSentinel

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


class _Faults:
    """``fail()`` support shared by the fake client and its indexes."""

    def fail(self, method: str, error: BaseException, *, on_call: int | None = None):
        """Make ``method`` raise ``error`` on every call, or only on call ``on_call`` (1-based).

        Returns ``self`` so calls chain. For misbehaviour that is not a raise (a
        delete that silently does nothing), monkeypatch the method on the instance.
        """
        getattr(type(self), method)  # AttributeError for a method the fake does not have
        self._faults[method] = (error, on_call)
        return self

    def _call(self, method: str) -> None:
        count = self._calls[method] = self._calls.get(method, 0) + 1
        error, on_call = self._faults.get(method, (None, None))
        if error is not None and on_call in (None, count):
            raise error


class FakeIndex(_Faults):
    """In-memory structured Marqo index.

    It enforces the rules production has been bitten by: a filter may only name
    a field the index declares (HTTP 400 otherwise), and ``_id`` is not a
    retrievable attribute (prod hotfix #55). Search pages with
    ``limit``/``offset``; ``add_documents`` upserts by ``_id``.

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

    def _call(self, method):
        if self._missing:
            raise RuntimeError(f"index not found: {self.name}")
        super()._call(method)

    def get_settings(self):
        self._call("get_settings")
        return {
            "allFields": [{"name": n, "type": "text"} for n in sorted(self.fields)],
            "tensorFields": list(self.tensor_fields),
        }

    def get_stats(self):
        self._call("get_stats")
        return {"numberOfDocuments": len(self.records)}

    def get_document(self, _id):
        self._call("get_document")
        for record in self.records:
            if record.get("_id") == _id:
                return dict(record)
        raise RuntimeError(f"document not found: {_id}")

    def search(self, q=None, **params):
        params = {"q": q, **params}
        self.searches.append(params)
        if self.client is not None:
            self.client.searches.append((self.name, params))
        self._call("search")
        attributes = params.get("attributes_to_retrieve")
        if attributes is not None and "_id" in attributes:
            raise RuntimeError("400 Bad Request: `_id` is not a valid attribute")
        predicate, named = _compile_filter(params.get("filter_string"))
        unknown = sorted(named - self.fields - {"_id"})
        if unknown:
            raise RuntimeError(f"400 Bad Request: Index has no filterable field {unknown[0]}")
        offset, limit = params.get("offset") or 0, params.get("limit", 10)
        page = [r for r in self.records if predicate(r)][offset : offset + limit]
        if attributes is None:
            return {"hits": [dict(r) for r in page]}
        keep = [*attributes, "_id"]
        return {"hits": [{k: r[k] for k in keep if k in r} for r in page]}

    def add_documents(self, documents, **_kwargs):
        batch = [dict(d) for d in documents]
        self.added.append(batch)
        self._call("add_documents")
        items = []
        for doc in batch:
            if doc.get("_id") in self.rejected:
                items.append({"_id": doc["_id"], **self.rejected[doc["_id"]]})
                continue
            self.records[:] = [r for r in self.records if r.get("_id") != doc.get("_id")]
            self.records.append(doc)
            items.append({"_id": doc.get("_id"), "status": 200})
        return {"errors": any(i["status"] != 200 for i in items), "items": items}

    def delete_documents(self, ids):
        ids = list(ids)
        self.deleted.append(ids)
        self._call("delete_documents")
        self.records[:] = [r for r in self.records if r.get("_id") not in set(ids)]
        return {"errors": False}


class FakeMarqo(_Faults, MarqoBindingSentinel):
    """In-memory Marqo client, shared by every client production builds.

    Install it with the ``fake_marqo`` fixture (or ``FakeMarqo().install(monkeypatch)``).
    That replaces ``sys.modules["marqo"]``, which every production path reaches
    through ``MarqoStore.client()``'s lazy ``import marqo`` (including
    ``get_vector_store(url=...)``). Patching ``vector_store.get_vector_store``
    would miss modules that import it by name (``temporal.document_tasks``,
    ``services.search``).

    An index exists only once ``add_index`` or ``create_index`` registered it;
    any call on another name raises ``index not found`` like Marqo does.
    Recorders: ``searches`` (``(index name, kwargs)`` across every index),
    ``created_indexes``, ``deleted_indexes``, and ``constructions`` (one entry
    per ``marqo.Client(url=...)``, from ``MarqoBindingSentinel``).

    Before asserting a recorder is EMPTY, prove the fake was bound in the same
    test (``assert_constructed()``, or a positive on the same fake): an empty
    recorder also describes a fake production never reached.
    """

    def __init__(self):
        MarqoBindingSentinel.__init__(self)
        self.indexes: dict[str, FakeIndex] = {}
        self.searches: list[tuple[str, dict]] = []
        self.created_indexes: list[str] = []
        self.deleted_indexes: list[str] = []
        self._faults, self._calls = {}, {}

    def install(self, monkeypatch) -> FakeMarqo:
        monkeypatch.setitem(sys.modules, "marqo", SimpleNamespace(Client=self.connect))
        return self

    def connect(self, url=None, **_kwargs) -> FakeMarqo:
        """Stands in for ``marqo.Client``; also usable as a ``client_factory``."""
        self.record(url)
        self._call("connect")
        return self

    def add_index(self, name, records=(), **kwargs) -> FakeIndex:
        """Register (or replace) index ``name``; kwargs go to ``FakeIndex``."""
        index = FakeIndex(name, records, **kwargs)
        index.client = self
        self.indexes[name] = index
        return index

    def index(self, name):
        self._call("index")
        if name in self.indexes:
            return self.indexes[name]
        missing = FakeIndex(name)
        missing.client, missing._missing = self, True
        return missing

    def get_index(self, name):
        self._call("get_index")
        if name not in self.indexes:
            raise RuntimeError(f"index not found: {name}")
        return {"indexName": name}

    def get_indexes(self):
        self._call("get_indexes")
        return {"results": [{"indexName": n} for n in self.indexes]}

    def create_index(self, name, settings_dict=None):
        self.created_indexes.append(name)
        self._call("create_index")
        if name in self.indexes:
            raise RuntimeError(f"index already exists: {name}")
        settings = settings_dict or {}
        self.add_index(
            name,
            fields={f["name"] for f in settings.get("allFields") or []},
            tensor_fields=settings.get("tensorFields") or (),
        )
        return {"acknowledged": True}

    def delete_index(self, name):
        self.deleted_indexes.append(name)
        self._call("delete_index")
        if self.indexes.pop(name, None) is None:
            raise RuntimeError(f"index not found: {name}")
        return {"acknowledged": True}
