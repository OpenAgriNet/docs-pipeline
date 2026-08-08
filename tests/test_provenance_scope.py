"""Tenant scoping of the caller-supplied ``index_name`` on ``GET /provenance/chunk``.

``index_name`` is a plain query parameter, so the caller picks which *physical*
Marqo index the resolver reads. The tenant check on the route
(``_require_document_for_user``) only runs on the workflow resolved *from the
hit*, i.e. after ``get_vector_store().get_document(index_name, marqo_id)`` has
already returned another tenant's record. The trailing 404 hides the content but
not the existence, and the record is in this process's memory regardless.

The guard therefore has to fire BEFORE the store is touched, which is what these
tests assert: the spy store records the call and then raises, so a test cannot
pass merely because the response ends up a 404 anyway.

Semantics mirror the sibling route ``get_document_marqo_status``: the check runs
only for a non-default ``index_name``, and cross-tenant/unknown indexes surface
as **404** (never 403) so index existence is not leaked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.api as api
from pipeline.auth.jwt import claims_to_user


def _run(coro):
    return asyncio.run(coro)


def _status(exc_info) -> int:
    return exc_info.value.status_code


def _viewer_in(instance: str):
    """Restricted, search-capable principal scoped to one tenant."""
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


A = "tenant-a"
B = "tenant-b"
WF_A = "wf-prov-a"
WF_B = "wf-prov-b"
IDX_A = "t-tenant-a-vet"
IDX_B = "t-tenant-b-vet"
MARQO_ID = "chunk-marqo-id-1"


class _StoreProbeCalled(RuntimeError):
    """Raised by the spy so a leaked read can never be mistaken for a clean 404."""


class _SpyStore:
    """Vector store that RECORDS every read and then raises.

    Recording alone is not enough: if the guard regresses, the route would call
    this, get a hit, and still 404 later on the tenant check — a status-code-only
    assertion would pass. Raising makes the leaked read impossible to swallow.
    """

    def __init__(self, hit: dict | None = None):
        self.calls: list[tuple[str, str]] = []
        self._hit = hit

    def get_document(self, index_name, marqo_id):
        self.calls.append((index_name, marqo_id))
        if self._hit is None:
            raise _StoreProbeCalled(f"store read reached index {index_name!r}")
        return dict(self._hit)


@pytest.fixture
def seeded(db_connection):
    """Two tenants, each with its own registered non-default physical index."""
    db = db_connection
    for wf, doc_id, inst in ((WF_A, "d-prov-a", A), (WF_B, "d-prov-b", B)):
        db.upsert_document(
            workflow_id=wf,
            document_id=doc_id,
            filename=f"{inst}.pdf",
            filepath=f"/tmp/{wf}.pdf",
            stage="completed",
            instance=inst,
        )
        db.save_chunks(
            wf,
            [{"chunk_number": 1, "original_text": f"{inst} chunk body", "source_pages": [1]}],
        )
    db.create_index_row(A, "vet", IDX_A, is_default=True)
    db.create_index_row(B, "vet", IDX_B, is_default=True)
    return db


# =============================================================================
# (a) deny — and deny BEFORE the store read
# =============================================================================


def test_cross_tenant_index_denied_before_store_read(seeded, monkeypatch):
    """A tenant-a caller naming tenant-b's physical index never reaches Marqo."""
    spy = _SpyStore(hit={"workflow_id": WF_B, "chunk_num": 1})
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    with pytest.raises(HTTPException) as exc:
        _run(api.resolve_provenance_chunk(
            MagicMock(), _viewer_in(A), doc_id=None, chunk_num=None,
            marqo_id=MARQO_ID, index_name=IDX_B,
        ))

    assert _status(exc) == 404, "cross-tenant must 404 (existence-hiding), not 403"
    assert spy.calls == [], f"store was read before the tenant check: {spy.calls}"


def test_unregistered_index_denied_for_restricted_caller_before_read(seeded, monkeypatch):
    """A restricted caller may not address an unregistered physical index either.

    Same rule as ``assert_marqo_index_access``: only unrestricted callers may
    target the transitional legacy index by physical name.
    """
    spy = _SpyStore()
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    with pytest.raises(HTTPException) as exc:
        _run(api.resolve_provenance_chunk(
            MagicMock(), _viewer_in(A), doc_id=None, chunk_num=None,
            marqo_id=MARQO_ID, index_name="some-unregistered-index",
        ))

    assert _status(exc) == 404
    assert spy.calls == []


# =============================================================================
# (b) allow — the legitimate same-tenant, non-default-index caller
# =============================================================================


def test_same_tenant_own_non_default_index_still_resolves(seeded, monkeypatch):
    """Regression guard for the legit chat/retrieval caller.

    Clients enrich Marqo hits by passing the physical index they searched. A
    caller naming an index its own tenant owns MUST still get a full 200 payload.
    """
    spy = _SpyStore(hit={"workflow_id": WF_A, "chunk_num": 1})
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    payload = _run(api.resolve_provenance_chunk(
        MagicMock(), _viewer_in(A), doc_id=None, chunk_num=None,
        marqo_id=MARQO_ID, index_name=IDX_A,
    ))

    assert payload["workflow_id"] == WF_A
    assert payload["chunk_num"] == 1
    assert "pdf_url" in payload and "chunk_url" in payload
    # The read happened, against the caller's own index, unmodified.
    assert spy.calls == [(IDX_A, MARQO_ID)]


def test_same_tenant_non_default_index_without_marqo_id_unaffected(seeded, monkeypatch):
    """doc_id/chunk_num callers never read the store, so the guard is a no-op."""
    spy = _SpyStore()
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    payload = _run(api.resolve_provenance_chunk(
        MagicMock(), _viewer_in(A), doc_id=WF_A, chunk_num=1,
        marqo_id=None, index_name=IDX_A,
    ))

    assert payload["workflow_id"] == WF_A
    assert spy.calls == []


# =============================================================================
# (c) the default index path is unchanged
# =============================================================================


def test_default_index_path_unchanged(seeded, monkeypatch):
    """``documents-index`` (the default value) is passed through untouched.

    Matching the sibling route, the registry check is skipped for the default
    value; the tenant check on the resolved workflow still applies.
    """
    spy = _SpyStore(hit={"workflow_id": WF_A, "chunk_num": 1})
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    payload = _run(api.resolve_provenance_chunk(
        MagicMock(), _viewer_in(A), doc_id=None, chunk_num=None,
        marqo_id=MARQO_ID, index_name="documents-index",
    ))

    assert payload["workflow_id"] == WF_A
    assert spy.calls == [("documents-index", MARQO_ID)]


def test_default_index_still_tenant_checked_on_resolved_workflow(seeded, monkeypatch):
    """The default index is shared, so the workflow-level check must still bite."""
    spy = _SpyStore(hit={"workflow_id": WF_B, "chunk_num": 1})
    monkeypatch.setattr(api, "get_vector_store", lambda: spy)

    with pytest.raises(HTTPException) as exc:
        _run(api.resolve_provenance_chunk(
            MagicMock(), _viewer_in(A), doc_id=None, chunk_num=None,
            marqo_id=MARQO_ID, index_name="documents-index",
        ))

    assert _status(exc) == 404
    assert spy.calls == [("documents-index", MARQO_ID)]
