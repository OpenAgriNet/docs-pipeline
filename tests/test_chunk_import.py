"""Prepared imports: real SQLite transactions, in-memory object storage, no jobs."""

import csv
import hashlib
import io
import json
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter

from pipeline.chunking.base import count_tokens
from pipeline.services import chunk_import as imports
from pipeline.services.chunk_transfer import serialize_chunks
from tests.helpers import master_admin, member, seed_document


class Objects:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.removed = []

    def fput_object(self, bucket, key, path, **kwargs):
        self.uploads.append((bucket, key))
        self.objects[bucket, key] = Path(path).read_bytes()

    def remove_object(self, bucket, key):
        self.removed.append((bucket, key))
        self.objects.pop((bucket, key), None)


@pytest.fixture
def objects(db_connection, monkeypatch):
    store = Objects()
    monkeypatch.setattr(imports.storage, "get_client", lambda: store)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    return store


def transfer(rows=None, **kwargs):
    return imports.create_prepared_import(**{
        "user": member("a", "content_curator"), "instance": "a", "document_id": "new-document",
        "filename": "chunks.json", "file_format": "json",
        "content": json.dumps(rows if rows is not None else [{"chunk_number": 1, "text": "Milk"}],
                              ensure_ascii=False).encode(),
        **kwargs,
    })


def counts(db):
    with db.get_connection() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "chunks", "chunk_tags", "document_artifacts",
                              "document_live_fingerprints", "document_jobs", "document_index_status")}


def pdf(pages=3, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("file_format", ["json", "csv"])
def test_export_import_roundtrip_uses_effective_fields_only(db_connection, objects, file_format):
    db = db_connection
    seed_document(db, "old", instance="b", chunks=[{
        "chunk_number": 3, "original_text": "Old", "edited_text": "Stale edit",
        "page_start": 2, "page_end": 3, "is_reference": True, "is_reviewed": True,
        "source_page_numbers_json": "[2,3]", "reviewer_notes": "Approved elsewhere",
        "source_spans_json": '[{"page_number":2,"start_char":0,"end_char":3}]',
        "section_title": "દૂધ", "content_type": "table", "chunk_version": 7,
    }, {"chunk_number": 9, "original_text": "Hidden", "is_excluded": True}])
    db.replace_chunk_tags("old", 3, [{"dimension": "species", "value": "goat"}], source="auto")
    rows = list(db.iter_export_chunks(instances=["b"], include_excluded=True))
    # Stale originals/history must not overwrite effective edits or new tags.
    rows[0]["edited_text"] = '=1+1,"દૂધ"\r\nsecond line'
    rows[0]["domain_tags"] = [{"dimension": "species", "value": "cattle"}]
    content = "".join(serialize_chunks(rows, file_format)).encode()
    result = transfer(content=content, filename=f"chunks.{file_format}", file_format=file_format,
                      pdf_content=pdf(), pdf_filename="source.pdf")
    document = db.get_document(result["workflow_id"])
    chunks = db.get_chunks(result["workflow_id"], include_excluded=True)
    assert document["instance"] == "a" and document["page_count"] == 3
    assert document["filename"] == "source.pdf"
    assert document["stage"] == "ready_for_ingestion" and not document["query_enabled"]
    assert not document["ingested_at"] and document["latest_job_id"] is None
    assert result["workflow_id"] != "old" and result["document_id"] == "new-document"
    assert [c["chunk_number"] for c in chunks] == [3, 9]
    assert chunks[0]["original_text"] == rows[0]["edited_text"] and chunks[0]["edited_text"] is None
    assert chunks[0]["is_reference"] and chunks[1]["is_excluded"]
    assert not chunks[0]["is_reviewed"] and chunks[0]["reviewer_notes"] is None
    assert chunks[0]["chunk_version"] == 1 and chunks[0]["chunking_provider"] == "prepared_import"
    assert chunks[0]["section_title"] == "દૂધ" and chunks[0]["content_type"] == "table"
    assert json.loads(chunks[0]["source_page_numbers_json"]) == [2, 3]
    assert {(t["dimension"], t["value"], t["source"]) for t in db.get_chunk_tags(result["workflow_id"], 3)} == {
        ("species", "cattle", "manual")}
    artifacts = db.list_document_artifacts(result["workflow_id"])
    assert len(artifacts) == 2
    source = next(a for a in artifacts if a["artifact_type"] == "original_upload")
    assert objects.objects[imports.storage.parse_minio_uri(source["storage_uri"])] == content
    assert json.loads(source["metadata_json"])["imported_by"] == "member"
    from pipeline.services.documents import preview_pdf_source
    assert preview_pdf_source(document)[1] == "source.pdf"
    assert all(key.startswith(f"a/{result['workflow_id']}/") for _, key in objects.uploads)
    assert counts(db)["document_index_status"] == counts(db)["document_jobs"] == 0


def test_plain_csv_unicode_backslash_and_computed_tokens(db_connection, objects):
    text = "\\literal દૂધ"
    content = ('chunk_number,text,domain_tags,is_reference\n5,' + text + ',"[""species:cattle""]",true\n').encode()
    result = transfer(content=content, file_format="csv", filename="chunks.csv")
    chunk = db_connection.get_chunks(result["workflow_id"])[0]
    assert chunk["original_text"] == text
    assert chunk["token_count"] == count_tokens(text)
    assert chunk["page_start"] is None and chunk["page_end"] is None
    assert db_connection.get_document(result["workflow_id"])["page_count"] == 0


@pytest.mark.parametrize("row", [
    {"chunk_number": True, "text": "x"}, {"chunk_number": 0, "text": "x"},
    {"chunk_number": 1.5, "text": "x"}, {"chunk_number": 1, "text": " "},
    {"chunk_number": 1, "text": "x\x00y"}, {"chunk_number": 1, "original_text": "No active text"},
    {"chunk_number": 1, "text": "x", "is_excluded": "false"},
    {"chunk_number": 1, "text": "x", "token_count": -1},
    {"chunk_number": 1, "text": "x", "token_count": float("nan")},
    {"chunk_number": 1, "text": "x", "schema_version": 1},
    {"chunk_number": 1, "text": "x", "page_start": 2},
    {"chunk_number": 1, "text": "x", "page_start": 3, "page_end": 2},
    {"chunk_number": 1, "text": "x", "source_page_numbers_json": "[-1]"},
    {"chunk_number": 1, "text": "x", "source_spans_json": '[{"page_number":1,"start_char":9,"end_char":2}]'},
    {"chunk_number": 1, "text": "x", "source_spans_json": '[{"no_page":1}]'},
    {"chunk_number": 1, "text": "x", "domain_tags": False},
    {"chunk_number": 1, "text": "x", "domain_tags": ["not-a-tag"]},
    {"chunk_number": 1, "text": "x", "domain_tags": ["unknown:tag"]},
    {"chunk_number": 1, "text": "x", "domain_tag_metadata": {}},
    {"chunk_number": 1, "text": "x", "chunking_config_json": "[]"},
    {"chunk_number": 1, "text": "x", "section_title": "\ud800"},
    {"chunk_number": 1, "text": "x", "typo": True},
])
def test_invalid_row_after_good_row_leaves_no_side_effects(db_connection, objects, row):
    before = counts(db_connection)
    with pytest.raises(imports.ChunkImportError):
        transfer(content=json.dumps([{"chunk_number": 7, "text": "Valid first row"}, row]).encode())
    assert counts(db_connection) == before
    assert not objects.uploads


@pytest.mark.parametrize("rows", [[], {}, [None],
    [{"chunk_number": 1, "text": "a"}, {"chunk_number": 1, "text": "b"}],
    [{"chunk_number": 1, "text": "a", "source_workflow_id": "a"},
     {"chunk_number": 2, "text": "b", "source_workflow_id": "b"}],
])
def test_empty_bad_or_mixed_documents(db_connection, objects, rows):
    with pytest.raises(imports.ChunkImportError):
        transfer(content=json.dumps(rows).encode())
    assert not objects.uploads and not counts(db_connection)["documents"]


@pytest.mark.parametrize("content,file_format", [
    (b'[{"chunk_number":1,"text":"a","text":"b"}]', "json"),
    (b"\xff", "json"), (b"[", "json"),
    (b"chunk_number,text,text\n1,a,b", "csv"),
    (b"chunk_number,text\n1,a,extra", "csv"),
    (b"chunk_number,text\n1", "csv"),
    (b'chunk_number,text,csv_encoding\n1,x,unknown', "csv"),
    (b'chunk_number,text,is_reference\n1,x,"false"', "json"),
])
def test_bad_encoding_and_structure(db_connection, objects, content, file_format):
    with pytest.raises(imports.ChunkImportError):
        transfer(content=content, file_format=file_format, filename=f"chunks.{file_format}")
    assert not objects.uploads and not counts(db_connection)["documents"]


@pytest.mark.parametrize("limit,value,kwargs", [
    ("max_upload_bytes", 4, {}), ("max_rows", 1, {}), ("max_text_bytes", 1, {}),
    ("max_tags_per_chunk", 0, {}),
    ("max_pdf_bytes", 1, {"pdf_content": pdf(), "pdf_filename": "source.pdf"}),
    ("max_pdf_pages", 2, {"pdf_content": pdf(), "pdf_filename": "source.pdf"}),
])
def test_limits_before_upload(db_connection, objects, limit, value, kwargs):
    with pytest.raises(imports.ChunkImportError):
        transfer([{"chunk_number": n, "text": "Milk", "domain_tags": ["species:cattle"]} for n in (1, 2)],
                 limits=replace(imports.ImportLimits(), **{limit: value}), **kwargs)
    assert not objects.uploads and not counts(db_connection)["documents"]


@pytest.mark.parametrize("source,rows", [
    (b"not PDF", [{"chunk_number": 1, "text": "x"}]),
    (b"%PDF-broken", [{"chunk_number": 1, "text": "x"}]),
    (pdf(encrypted=True), [{"chunk_number": 1, "text": "x"}]),
    (pdf(0), [{"chunk_number": 1, "text": "x"}]),
    (pdf(), [{"chunk_number": 1, "text": "x", "page_start": 1, "page_end": 4}]),
    (pdf(), [{"chunk_number": 1, "text": "x", "source_page_numbers_json": "[4]"}]),
    (pdf(), [{"chunk_number": 1, "text": "x", "source_spans_json": '[{"page_number":4}]'}]),
])
def test_pdf_validation_and_page_bounds(db_connection, objects, source, rows):
    with pytest.raises(imports.ChunkImportError):
        transfer(rows, pdf_content=source, pdf_filename="source.pdf")
    assert not objects.uploads and not counts(db_connection)["documents"]


@pytest.mark.parametrize("user", [member("a", "viewer"), member("b", "content_curator"),
    member({"a": ["viewer"], "b": ["content_curator"]}), master_admin()])
def test_import_requires_upload_in_target_tenant(db_connection, objects, user):
    with pytest.raises(HTTPException):
        transfer(user=user)
    assert not objects.uploads and not counts(db_connection)["documents"]


def test_taxonomy_uses_tenant_policy_and_never_retags(db_connection, objects, monkeypatch):
    monkeypatch.setenv("DOMAIN_TAGGING_STRICT_TAXONOMY", "false")
    result = transfer([{"chunk_number": 1, "text": "x", "domain_tags": [" Custom:Tag ", "custom:tag"]}])
    tags = db_connection.get_chunk_tags(result["workflow_id"], 1)
    assert [(t["dimension"], t["value"]) for t in tags] == [("custom", "tag")]


def test_tenant_taxonomy_does_not_fall_back_to_other_tenant(db_connection, objects):
    db_connection.seed_taxonomy_for_instance("a", {"version": "a-only", "domains": {"local": {"species": ["camel"]}}})
    with pytest.raises(imports.ChunkImportError, match="tenant taxonomy"):
        transfer([{"chunk_number": 1, "text": "x", "domain_tags": ["species:cattle"]}])
    result = transfer([{"chunk_number": 1, "text": "x", "domain_tags": ["species:camel"]}])
    assert db_connection.get_chunk_tags(result["workflow_id"], 1)[0]["value"] == "camel"


@pytest.mark.parametrize("options", [
    {"document_id": " "}, {"filename": "../chunks.json"}, {"filename": "chunks.pdf"},
    {"document_id": "\ud800"}, {"filename": "\ud800.json"},
    {"pdf_filename": "source.pdf"}, {"pdf_content": pdf(), "pdf_filename": "../source.pdf"},
])
def test_invalid_identity_and_filenames_before_upload(db_connection, objects, options):
    with pytest.raises(imports.ChunkImportError):
        transfer(**options)
    assert not objects.uploads and not counts(db_connection)["documents"]


def test_large_plain_csv_field_and_literal_formula_text(db_connection, objects):
    text = "= " + "milk " * 30_000  # Larger than csv's default field limit.
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    writer.writerows([["chunk_number", "text", "token_count"], [3, text, 30_000]])
    result = transfer(content=content.getvalue().encode(), file_format="csv", filename="chunks.csv")
    assert db_connection.get_chunks(result["workflow_id"])[0]["original_text"] == text


def test_marked_csv_nonfinite_metadata_is_rejected(db_connection, objects):
    seed_document(db_connection, "old", instance="b", chunks=["x"])
    content = "".join(serialize_chunks(db_connection.iter_export_chunks(instances=["b"]), "csv"))
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    row = next(reader)
    row["domain_tag_metadata"] = "[NaN]"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerow(row)
    with pytest.raises(imports.ChunkImportError):
        transfer(content=output.getvalue().encode(), file_format="csv", filename="chunks.csv")
    assert not objects.uploads and counts(db_connection)["documents"] == 1


def test_same_identity_in_other_tenant_is_independent(db_connection, objects):
    seed_document(db_connection, "other", instance="b", document_id="new-document", chunks=["Untouched"])
    result = transfer()
    assert db_connection.get_document(result["workflow_id"])["instance"] == "a"
    assert db_connection.get_chunks("other")[0]["original_text"] == "Untouched"


@pytest.mark.parametrize("collision", ["document_id", "canonical_document_id", "fingerprint"])
def test_existing_identity_untouched(db_connection, objects, collision):
    db = db_connection
    fields = {"document_id": "new-document"} if collision == "document_id" else {
        "canonical_document_id": "new-document"} if collision == "canonical_document_id" else {
        "source_file_fingerprint": hashlib.sha256(json.dumps([{"chunk_number": 1, "text": "Milk"}]).encode()).hexdigest()}
    seed_document(db, "existing", instance="a", chunks=["Keep exactly"], **fields)
    before, original = counts(db), db.get_document("existing")
    with pytest.raises(db.PreparedImportConflict):
        transfer()
    assert counts(db) == before and db.get_document("existing") == original
    assert db.get_chunks("existing")[0]["original_text"] == "Keep exactly"
    assert not objects.uploads


def test_sql_failure_rolls_back_every_row_and_removes_new_objects(db_connection, objects):
    db = db_connection
    with db.get_connection() as conn:
        conn.execute("CREATE TRIGGER fail_artifact BEFORE INSERT ON document_artifacts BEGIN SELECT RAISE(ABORT, 'storage row failed'); END")
        conn.commit()
    before = counts(db)
    with pytest.raises(sqlite3.IntegrityError, match="storage row failed"):
        transfer(pdf_content=pdf(), pdf_filename="source.pdf")
    assert counts(db) == before
    assert len(objects.uploads) == len(objects.removed) == 2 and not objects.objects


def test_ambiguous_upload_failure_cleans_attempted_pdf_and_source(db_connection, objects, monkeypatch):
    upload = objects.fput_object

    def fail_after_write(*args, **kwargs):
        upload(*args, **kwargs)
        if len(objects.uploads) == 2:
            raise TimeoutError("Response lost after upload")

    monkeypatch.setattr(objects, "fput_object", fail_after_write)
    with pytest.raises(TimeoutError):
        transfer(pdf_content=pdf(), pdf_filename="source.pdf")
    assert not counts(db_connection)["documents"] and not objects.objects
    assert len(objects.removed) == 2


def test_failed_cleanup_reports_orphans(db_connection, objects, monkeypatch):
    monkeypatch.setattr(db_connection, "create_prepared_document", lambda *a: (_ for _ in ()).throw(RuntimeError("DB unavailable")))
    monkeypatch.setattr(objects, "remove_object", lambda *a: (_ for _ in ()).throw(ConnectionError("MinIO unavailable")))
    with pytest.raises(imports.ImportCleanupError) as error:
        transfer()
    assert error.value.orphaned_uris == [f"minio://{b}/{k}" for b, k in objects.uploads]
    assert str(error.value.__cause__) == "DB unavailable"
    assert not counts(db_connection)["documents"]


def test_concurrent_import_collision_compensates_only_losing_objects(db_connection, objects, monkeypatch):
    barrier = threading.Barrier(2)
    upload = objects.fput_object

    def concurrent_upload(*args, **kwargs):
        upload(*args, **kwargs)
        barrier.wait(timeout=10)

    monkeypatch.setattr(objects, "fput_object", concurrent_upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transfer, [{"chunk_number": 1, "text": text}]) for text in ("a", "b")]
    results, failures = [], []
    for future in futures:
        try:
            results.append(future.result())
        except db_connection.PreparedImportConflict as exc:
            failures.append(exc)
    assert len(results) == len(failures) == len(objects.objects) == len(objects.removed) == 1
    assert counts(db_connection)["documents"] == counts(db_connection)["chunks"] == 1
    assert all(results[0]["workflow_id"] in key for _, key in objects.objects)


def _process_insert(path, workflow, barrier, queue):
    from pipeline import db
    db.DB_PATH = path
    document = {"workflow_id": workflow, "instance": "a", "document_id": "same-id",
                "filename": "chunks.json", "source_file_fingerprint": workflow, "page_count": 0}
    with db.get_connection() as conn:
        db.assert_prepared_import_available(conn, "a", "same-id", workflow)
    barrier.wait(timeout=15)
    artifact = {"artifact_type": "original_upload", "storage_uri": f"minio://documents/a/{workflow}/chunks.json",
                "mime_type": "application/json", "filename": "chunks.json", "size_bytes": 1, "metadata": {}}
    chunks = imports._validate([{"chunk_number": 1, "text": workflow, "token_count": 1}],
                               "a", 0, imports.ImportLimits())
    try:
        db.create_prepared_document(document, chunks, [artifact])
    except Exception as exc:
        queue.put(type(exc).__name__)
    else:
        queue.put("created")


def test_identity_check_serializes_across_processes(db_connection):
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_process_insert, args=(db_connection.DB_PATH, str(n), barrier, queue)) for n in (1, 2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=25)
        if process.is_alive():
            process.terminate()
            process.join()
        assert process.exitcode == 0
    results = [queue.get(timeout=2), queue.get(timeout=2)]
    assert sorted(results) == ["PreparedImportConflict", "created"]
    assert counts(db_connection)["documents"] == counts(db_connection)["chunks"] == 1
