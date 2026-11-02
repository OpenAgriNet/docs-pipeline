"""Chunk downloads exercise the real database, routing and permission graph."""

import csv
import io
import json

import pytest

from pipeline.auth import deps as auth_deps
from pipeline.auth.models import local_bypass_user
from pipeline.services.chunk_transfer import EXPORT_FIELDS
from tests.helpers import master_admin, member, seed_document


@pytest.fixture
def exports(test_client, monkeypatch):
    # Exercise auth-on requests, stubbing only JWT validation as in the route
    # contract suite. A bypass-mode request would make tenant checks vacuous.
    monkeypatch.setenv("AUTH_DISABLED", "false")
    users = {"viewer": member("a", "viewer"), "root": master_admin(), "bypass": local_bypass_user()}
    monkeypatch.setattr(auth_deps, "decode_and_validate_token", lambda token, config: users[token])
    test_client.headers["Authorization"] = "Bearer viewer"
    return test_client


def tag(db, workflow, number, value="milk", source="manual"):
    db.replace_chunk_tags(workflow, number, [
        {"dimension": "topic", "value": value},
    ], source=source)


def test_document_export_preserves_saved_metadata_and_gaps(exports, db_connection):
    db = db_connection
    seed_document(db, "wf-a", instance="a", chunks=[{
        "chunk_number": 3, "original_text": "Original", "edited_text": "દૂધ, \"milk\"\nSecond line",
        "token_count": 12, "page_start": 2, "page_end": 4,
        "section_title": "Treatment", "content_type": "table", "is_reference": True,
        "source_page_numbers_json": "[2,4]", "source_spans_json": '[{"page":2}]',
        "chunking_provider": "manual", "chunking_config_json": '{"size":400}',
        "is_reviewed": True, "reviewer_notes": "checked",
    }, {"chunk_number": 8, "original_text": "Hidden", "is_excluded": True}])
    tag(db, "wf-a", 3)
    response = exports.get("/documents/wf-a/chunks/export")
    assert response.status_code == 200  # Must not route to /chunks/{chunk_num}.
    assert response.headers["content-disposition"] == 'attachment; filename="chunks.json"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    rows = response.json()
    assert len(rows) == 1
    record = rows[0]
    assert set(record) == set(EXPORT_FIELDS)
    assert record["schema_version"] == 1
    assert record["chunk_number"] == 3
    assert record["text"] == 'દૂધ, "milk"\nSecond line'
    assert record["original_text"] == "Original"
    assert record["edited_text"] == record["text"]
    assert record["is_reference"] is True
    assert record["is_reviewed"] is True
    assert record["is_excluded"] is False
    assert (record["page_start"], record["page_end"]) == (2, 4)
    assert record["section_title"] == "Treatment"
    assert record["source_spans_json"] == '[{"page":2}]'
    assert record["chunking_config_json"] == '{"size":400}'
    assert record["domain_tags"] == ["topic:milk"]
    assert record["domain_tag_metadata"][0]["source"] == "manual"
    assert record["source_workflow_id"] == "wf-a"
    assert record["source_document_id"] == "doc-wf-a"
    assert record["source_instance"] == "a"
    assert "id" not in record and "workflow_id" not in record
    included = exports.get("/documents/wf-a/chunks/export?include_excluded=true").json()
    assert [r["chunk_number"] for r in included] == [3, 8]
    assert included[1]["is_excluded"] is True


def test_csv_preserves_unicode_multiline_and_nested_values(exports, db_connection):
    text = '=text,"દૂધ"\r\nmore\n\tdata'
    seed_document(db_connection, "wf-a", instance="a", chunks=[text])
    tag(db_connection, "wf-a", 1)
    response = exports.get("/documents/wf-a/chunks/export?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline=""))
    rows = list(reader)
    assert reader.fieldnames == list(EXPORT_FIELDS)
    assert len(rows) == 1
    assert rows[0]["text"] == text
    assert json.loads(rows[0]["domain_tags"]) == ["topic:milk"]
    assert json.loads(rows[0]["domain_tag_metadata"])[0]["source"] == "manual"
    assert json.loads(rows[0]["is_reference"]) is False


def test_export_matches_all_filtered_search_results_beyond_page_cap(exports, db_connection):
    db = db_connection
    seed_document(db, "wf-a", instance="a", stage="completed", chunks=[
        {"chunk_number": n, "original_text": f"milk {n}"} for n in range(1, 604)
    ])
    for n in range(1, 604):
        tag(db, "wf-a", n)
    seed_document(db, "wf-other-stage", instance="a", stage="failed", chunks=["milk"])
    seed_document(db, "wf-other-tenant", instance="b", stage="completed", chunks=["milk"])
    seed_document(db, "wf-other-text", instance="a", stage="completed", chunks=["water"])
    seed_document(db, "wf-other-tag", instance="a", stage="completed", chunks=["milk"])
    for wf in ("wf-other-stage", "wf-other-tenant", "wf-other-text"):
        tag(db, wf, 1)
    url = "/chunks/export?q=milk&tags=topic:milk&stage=completed"
    response = exports.get(url)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 603
    assert {row["source_workflow_id"] for row in rows} == {"wf-a"}
    assert [row["chunk_number"] for row in rows] == list(range(1, 604))
    expected, total = db.search_chunks(query="milk", tags=["topic:milk"], stage="completed", instances=["a"], limit=1000)
    assert len(rows) == total
    assert [(r["source_workflow_id"], r["chunk_number"]) for r in rows] == [
        (r["workflow_id"], r["chunk_number"]) for r in expected
    ]
    assert exports.get("/chunks/export?q=milk%20topic:milk&stage=completed").json() == rows


def test_export_tenant_boundaries_and_empty_results(exports, db_connection):
    seed_document(db_connection, "wf-a", instance="a", chunks=["A only"])
    seed_document(db_connection, "wf-b", instance="b", chunks=["B secret"])
    assert exports.get("/documents/wf-b/chunks/export").status_code == 404
    assert exports.get("/documents/missing/chunks/export").status_code == 404
    assert [r["text"] for r in exports.get("/chunks/export").json()] == ["A only"]
    assert exports.get("/chunks/export?q=absent").json() == []
    exports.headers["Authorization"] = "Bearer root"
    assert exports.get("/chunks/export").status_code == 403
    assert exports.get("/documents/wf-a/chunks/export").status_code == 403
    exports.headers["Authorization"] = "Bearer bypass"
    assert len(exports.get("/chunks/export").json()) == 2


def test_snapshot_keeps_text_and_tags_consistent_during_edits(db_connection):
    db = db_connection
    seed_document(db, "wf-a", instance="a", chunks=["one", "two", "three"])
    for n in (1, 2, 3):
        tag(db, "wf-a", n, "before")
    iterator = db.iter_export_chunks(instances=["a"])
    first = next(iterator)
    db.update_chunk("wf-a", 3, edited_text="changed")
    tag(db, "wf-a", 3, "after")
    rows = [first, *iterator]
    assert [r["original_text"] for r in rows] == ["one", "two", "three"]
    assert rows[2]["edited_text"] is None
    assert rows[2]["domain_tags"][0]["value"] == "before"
    assert db.get_chunk("wf-a", 3)["edited_text"] == "changed"


@pytest.mark.parametrize("path", ["/chunks/export", "/documents/wf-a/chunks/export"])
def test_export_validates_format(exports, db_connection, path):
    seed_document(db_connection, "wf-a", instance="a", chunks=["a"])
    assert exports.get(path + "?format=xml").status_code == 422


def test_legacy_export_shape_is_unchanged(exports, db_connection):
    seed_document(db_connection, "wf-a", instance="a", chunks=["a"])
    response = exports.get("/documents/wf-a/export/chunks")
    assert response.status_code == 200
    assert set(response.json()[0]) == {"_id", "doc_id", "name", "text", "chunk_num", "token_count", "source"}
