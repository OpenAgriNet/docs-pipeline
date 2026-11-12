"""Chunk downloads exercise the real database, routing and permission graph."""

import csv
import io
import json
from datetime import datetime, timezone

import pytest

from pipeline.auth import deps as auth_deps
from pipeline.auth.models import local_bypass_user
from pipeline.services.chunk_transfer import CSV_FIELDS, EXPORT_FIELDS, decode_csv_record
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
    assert record["schema_version"] == 2
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
    assert reader.fieldnames == list(CSV_FIELDS)
    assert len(rows) == 1
    assert rows[0]["text"] == "\\" + text
    assert rows[0]["csv_encoding"] == "backslash-v1"
    assert decode_csv_record(rows[0])["text"] == text
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
    assert len({row["exported_at"] for row in rows}) == 1
    assert datetime.fromisoformat(rows[0]["exported_at"]).tzinfo == timezone.utc
    expected, total = db.search_chunks(query="milk", tags=["topic:milk"], stage="completed", instances=["a"], limit=1000)
    assert len(rows) == total
    assert [(r["source_workflow_id"], r["chunk_number"]) for r in rows] == [
        (r["workflow_id"], r["chunk_number"]) for r in expected
    ]
    inline = exports.get("/chunks/export?q=milk%20topic:milk&stage=completed").json()
    assert [{k: v for k, v in r.items() if k != "exported_at"} for r in inline] == [
        {k: v for k, v in r.items() if k != "exported_at"} for r in rows
    ]


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
    seed_document(db, "wf-a", instance="a", stage="chunk_review", chunks=["one", "two", "three"])
    db.set_document_display_name("wf-a", "Before")
    for n in (1, 2, 3):
        tag(db, "wf-a", n, "before")
    iterator = db.iter_export_chunks(instances=["a"])
    first = next(iterator)
    db.update_chunk("wf-a", 3, edited_text="changed")
    tag(db, "wf-a", 3, "after")
    db.set_document_display_name("wf-a", "After")
    db.set_document_query_enabled("wf-a", False)
    db.set_document_disabled("wf-a", True)
    db.update_document_stage("wf-a", "completed")
    rows = [first, *iterator]
    assert [r["original_text"] for r in rows] == ["one", "two", "three"]
    assert rows[2]["edited_text"] is None
    assert rows[2]["domain_tags"][0]["value"] == "before"
    assert {r["source_display_name"] for r in rows} == {"Before"}
    assert {r["source_stage"] for r in rows} == {"chunk_review"}
    assert all(r["source_query_enabled"] is True and r["source_is_disabled"] is False for r in rows)
    assert db.get_chunk("wf-a", 3)["edited_text"] == "changed"
    assert db.get_document("wf-a")["display_name"] == "After"


def test_json_and_csv_preserve_document_context_and_all_chunk_values(exports, db_connection):
    db = db_connection
    edited = '=SUM(1,2)\nદૂધ "table"\r\n\\literal'
    seed_document(db, "wf-a", instance="a", stage="chunking", chunks=[{
        "chunk_number": 7, "original_text": "'original", "edited_text": edited,
        "page_start": 2, "page_end": 5, "token_count": 45,
        "section_title": None, "reviewer_notes": "", "content_type": "table",
        "is_reference": True, "is_reviewed": True, "is_excluded": True,
        "source_page_numbers_json": "[2,3,5]", "source_spans_json": '[{"page":2}]',
    }])
    db.set_document_display_name("wf-a", "=Veterinary, manual")
    db.set_document_query_enabled("wf-a", False)
    db.set_document_disabled("wf-a", True)
    tag(db, "wf-a", 7)
    path = "/documents/wf-a/chunks/export?include_excluded=true"
    canonical = exports.get(path).json()[0]
    raw_csv = exports.get(path + "&format=csv").content.decode("utf-8-sig")
    cells = next(csv.DictReader(io.StringIO(raw_csv, newline="")))
    restored = decode_csv_record(cells)

    assert canonical["text"] == canonical["edited_text"] == edited
    assert canonical["original_text"] == "'original"
    assert canonical["source_display_name"] == "=Veterinary, manual"
    assert canonical["source_stage"] == "chunking"
    assert canonical["source_query_enabled"] is False
    assert canonical["source_is_disabled"] is True
    assert canonical["is_excluded"] is True
    assert cells["source_display_name"] == "\\=Veterinary, manual"
    assert cells["reviewer_notes"] == "\\" and cells["section_title"] == ""
    assert restored["reviewer_notes"] == "" and restored["section_title"] is None
    assert {k: v for k, v in restored.items() if k != "exported_at"} == {
        k: v for k, v in canonical.items() if k != "exported_at"
    }
    assert datetime.fromisoformat(restored["exported_at"]).tzinfo == timezone.utc

    # Editing the transfer text/tags must not be undone by stale history fields.
    cells["text"] = "નવું લખાણ"
    cells["domain_tags"] = '["topic:new"]'
    changed = decode_csv_record(cells)
    assert changed["text"] == "નવું લખાણ" and changed["domain_tags"] == ["topic:new"]
    assert changed["edited_text"] == edited and changed["original_text"] == "'original"
    assert changed["domain_tag_metadata"][0]["value"] == "milk"


@pytest.mark.parametrize("text", [
    "=1+1", "+1+1", "-1+1", "@SUM(1,2)", "＝1+1", "＋1+1", "－1+1", "＠SUM(1,2)",
    "\t=1+1", "\r=1+1", "\n=1+1", "  =1+1", "\u200b=1+1", "\x00=1+1",
    "\\=literal", "\\ordinary", "'literal", '"=literal',
    '=1+2";=1+2', '=1+2\'" ;,=1+2', "",
])
def test_csv_formula_prefixes_are_visible_text_and_reversible(exports, db_connection, text):
    seed_document(db_connection, "wf-a", instance="a", chunks=[text])
    path = "/documents/wf-a/chunks/export"
    cells = next(csv.DictReader(io.StringIO(exports.get(path + "?format=csv").content.decode("utf-8-sig"))))
    assert cells["text"] == "\\" + text
    assert decode_csv_record(cells)["text"] == text
    assert exports.get(path).json()[0]["text"] == text


def test_csv_decoder_refuses_unknown_encoding_or_version(exports, db_connection):
    seed_document(db_connection, "wf-a", instance="a", chunks=["ordinary"])
    response = exports.get("/documents/wf-a/chunks/export?format=csv")
    cells = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert cells["text"] == "ordinary"
    with pytest.raises(ValueError, match="encoding"):
        decode_csv_record({**cells, "csv_encoding": "unknown"})
    with pytest.raises(ValueError, match="version"):
        decode_csv_record({**cells, "schema_version": "1"})
    with pytest.raises(ValueError, match="token_count"):
        decode_csv_record({**cells, "token_count": "not JSON"})
    with pytest.raises(ValueError, match="columns"):
        decode_csv_record({k: v for k, v in cells.items() if k != "text"})


@pytest.mark.parametrize("path", ["/chunks/export", "/documents/wf-a/chunks/export"])
def test_export_validates_format(exports, db_connection, path):
    seed_document(db_connection, "wf-a", instance="a", chunks=["a"])
    assert exports.get(path + "?format=xml").status_code == 422


def test_legacy_export_shape_is_unchanged(exports, db_connection):
    seed_document(db_connection, "wf-a", instance="a", chunks=["a"])
    response = exports.get("/documents/wf-a/export/chunks")
    assert response.status_code == 200
    assert set(response.json()[0]) == {"_id", "doc_id", "name", "text", "chunk_num", "token_count", "source"}
