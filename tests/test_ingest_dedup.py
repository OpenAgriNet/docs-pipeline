"""Ingest dedup: shared ``dedup_or_none`` for POST /documents and POST /upload."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from threading import Barrier, Thread
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline import db
from pipeline.auth.models import local_bypass_user
from pipeline.models import DocumentStage
from pipeline.services import documents as document_service
from pipeline.services import workflow_runtime
from pipeline.storage import minio as minio_storage
from tests.helpers import seed_document


@pytest.fixture(autouse=True)
def _disable_upload_limiter():
    """These route tests share the process-wide SlowAPI limiter with the suite."""
    from pipeline.rate_limit import limiter

    was = getattr(limiter, "enabled", True)
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


@pytest.fixture
def tracking_temporal(monkeypatch):
    """Temporal mock that only answers get_state for workflows in ``client._live`` (started ones included)."""
    import pipeline.temporal.client as temporal_client

    client, live = AsyncMock(), {}

    def get_workflow_handle(workflow_id: str):
        handle = AsyncMock()
        if workflow_id in live:
            handle.query = AsyncMock(return_value=live[workflow_id])
        else:
            handle.query = AsyncMock(side_effect=Exception("workflow not found"))
        return handle

    async def start_workflow(*args, **kwargs):
        workflow_id = kwargs.get("id")
        assert workflow_id, "start_workflow requires id="
        live[workflow_id] = {
            "stage": "registered", "document_id": "test-doc-id", "filename": "test.pdf",
            "page_count": 0, "chunk_count": 0, "error_message": None,
        }
        return get_workflow_handle(workflow_id)

    client.get_workflow_handle = MagicMock(side_effect=get_workflow_handle)
    client.start_workflow = AsyncMock(side_effect=start_workflow)
    client._live = live

    async def _get_client():
        return client

    monkeypatch.setattr(temporal_client, "get_client", _get_client)
    monkeypatch.setattr(temporal_client, "get_client_or_none", AsyncMock(return_value=client))
    temporal_client._client = client  # match how TestClient fixtures inject the client
    return client


def _seed(db_connection, workflow_id, fp, filename, *, stage="completed", instance="default", **fields):
    """A document row identified by content fingerprint ``fp``."""
    seed_document(
        db_connection, workflow_id, document_id=fp, canonical_document_id=fp, source_file_fingerprint=fp,
        filename=filename, stage=stage, instance=instance, **fields,
    )


def _dedup(workflow_id, fp="fp1", filename="a.pdf", **kwargs):
    return asyncio.run(document_service.dedup_or_none(
        local_bypass_user(), workflow_id, document_id=fp, canonical_document_id=fp,
        filename=filename, source_filename=filename, source_file_fingerprint=fp, **kwargs,
    ))


def _post_twice(test_client, temporal, path, first, second, *, minio=None):
    """Post ``first`` then ``second``; the second must be a duplicate of the first that starts and stores nothing."""
    one = test_client.post(path, **first)
    assert one.status_code == 200, one.text
    assert one.json().get("duplicate") is False
    assert temporal.start_workflow.call_count >= 1
    starts = temporal.start_workflow.call_count
    puts = minio.put_object.call_count if minio else None
    two = test_client.post(path, **second)
    assert two.status_code == 200, two.text
    assert two.json()["duplicate"] is True
    assert two.json()["workflow_id"] == one.json()["workflow_id"]
    assert temporal.start_workflow.call_count == starts
    if minio:
        assert minio.put_object.call_count == puts


def _pdf(filename, content):
    return {"files": {"file": (filename, content, "application/pdf")}}


def _upload_path_workflow_id(content: bytes, filename: str, instance: str = "default") -> str:
    object_name = f"{instance}/{hashlib.md5(content).hexdigest()}/{filename}"
    minio_path = f"minio://{minio_storage.bucket_name()}/{object_name}"
    return workflow_runtime.tenant_workflow_id(workflow_runtime.get_workflow_id(minio_path), instance)


def _race(db_connection, fp, workflow_ids):
    """Insert the same live fingerprint from two threads at once: one wins, the other is pointed at it."""
    barrier, outcomes = Barrier(2), []

    def _insert(workflow_id):
        barrier.wait()
        try:
            _seed(db_connection, workflow_id, fp, f"{workflow_id}.pdf", stage="registered")
            outcomes.append(("ok", workflow_id))
        except db.LiveFingerprintConflict as exc:
            outcomes.append(("conflict", exc.existing["workflow_id"]))

    threads = [Thread(target=_insert, args=(wf,)) for wf in workflow_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 2
    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1
    assert sum(1 for kind, _ in outcomes if kind == "conflict") == 1
    winner = next(wid for kind, wid in outcomes if kind == "ok")
    assert all(wid == winner for kind, wid in outcomes if kind == "conflict")


def _live_rows(db_connection, fp) -> int:
    with db_connection.get_connection() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM documents WHERE source_file_fingerprint = ?"
            " AND (is_disabled = 0 OR is_disabled IS NULL)",
            (fp,),
        ).fetchone()["c"])


def test_dedup_or_none_hit_sets_duplicate_true(db_connection, tracking_temporal):
    wf = "doc-dedup-hit"
    tracking_temporal._live[wf] = {"stage": "registered", "page_count": 0, "chunk_count": 0, "error_message": None}
    _seed(db_connection, wf, "fp1", "a.pdf", stage="registered", source_filename="a.pdf")
    summary, wid = _dedup(wf)
    assert wid == wf
    assert summary is not None
    assert summary.duplicate is True
    assert summary.workflow_id == wf
    assert summary.stage == DocumentStage.REGISTERED


def test_dedup_or_none_miss_without_sqlite_keeps_stable_id(db_connection, tracking_temporal):
    assert _dedup("doc-missing", fp="fp") == (None, "doc-missing")


def test_dedup_or_none_orphan_temporal_allocates_rerun(db_connection, tracking_temporal):
    """SQLite purged but Temporal still answers → new run id."""
    tracking_temporal._live["doc-orphan"] = {"stage": "registered", "page_count": 0, "chunk_count": 0}
    summary, wid = _dedup("doc-orphan")
    assert summary is None
    assert wid.startswith("doc-orphan-rerun-")


def test_dedup_or_none_force_skips_live_hit(db_connection, tracking_temporal):
    wf = "doc-force"
    tracking_temporal._live[wf] = {"stage": "registered", "page_count": 0, "chunk_count": 0}
    seed_document(db_connection, wf, document_id="fp1", filename="a.pdf", instance="default")
    summary, wid = _dedup(wf, force=True)
    assert summary is None
    assert wid.startswith("doc-force-rerun-")
    tracking_temporal.get_workflow_handle.assert_not_called()


def test_upload_second_identical_file_returns_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    upload = _pdf("sample.pdf", sample_pdf_content)
    _post_twice(test_client, tracking_temporal, "/upload", upload, upload)


def test_documents_register_second_identical_path_returns_duplicate(
    test_client, temp_pdf_file, monkeypatch, db_connection, tracking_temporal
):
    monkeypatch.setattr("pipeline.services.source_files.ALLOWED_FILE_PATHS", [str(temp_pdf_file.parent)])
    payload = {"json": {"filepath": str(temp_pdf_file)}}
    _post_twice(test_client, tracking_temporal, "/documents", payload, payload)


def test_upload_same_bytes_different_filename_returns_existing(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    _post_twice(
        test_client, tracking_temporal, "/upload",
        _pdf("report_final.pdf", sample_pdf_content), _pdf("Report Final.pdf", sample_pdf_content),
        minio=mock_minio_client,
    )


def test_documents_register_same_bytes_different_path_returns_existing(
    test_client, temp_pdf_file, monkeypatch, db_connection, tracking_temporal
):
    other = temp_pdf_file.parent / "other-name.pdf"
    other.write_bytes(temp_pdf_file.read_bytes())
    monkeypatch.setattr("pipeline.services.source_files.ALLOWED_FILE_PATHS", [str(temp_pdf_file.parent)])
    _post_twice(
        test_client, tracking_temporal, "/documents",
        {"json": {"filepath": str(temp_pdf_file)}}, {"json": {"filepath": str(other)}},
    )


def test_fingerprint_hit_without_temporal_reuses_sqlite_row(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    _seed(db_connection, "rebuild-doc-existing", fp, "report_final.pdf", source_filename="report_final.pdf")
    starts_before = tracking_temporal.start_workflow.call_count

    resp = test_client.post("/upload", **_pdf("Report Final.pdf", sample_pdf_content))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is True
    assert body["workflow_id"] == "rebuild-doc-existing"
    assert body["stage"] == "completed"
    assert tracking_temporal.start_workflow.call_count == starts_before
    assert mock_minio_client.put_object.call_count == 0


def test_second_live_fingerprint_insert_returns_winner(db_connection):
    fp = "shared-content-hash"
    _seed(db_connection, "rebuild-doc-completed", fp, "slug_name.pdf")
    with pytest.raises(db.LiveFingerprintConflict) as caught:
        _seed(db_connection, "doc-failed-sibling", fp, "Other Name.pdf", stage="failed")
    assert caught.value.existing["workflow_id"] == "rebuild-doc-completed"
    assert db_connection.find_live_document_by_fingerprint("default", fp)["workflow_id"] == "rebuild-doc-completed"


def test_fingerprint_hit_reuses_existing_live_row(db_connection, tracking_temporal):
    fp = "shared-content-hash"
    _seed(db_connection, "rebuild-doc-completed", fp, "slug_name.pdf")
    summary, wid = _dedup("doc-new-path-id", fp=fp, filename="Other Name.pdf", instance="default")
    assert summary is not None
    assert summary.duplicate is True
    assert wid == "rebuild-doc-completed"


def test_soft_deleted_fingerprint_is_not_a_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    _seed(db_connection, "doc-disabled", hashlib.md5(sample_pdf_content).hexdigest(), "gone.pdf")
    db_connection.set_document_disabled("doc-disabled", True)

    resp = test_client.post("/upload", **_pdf("gone.pdf", sample_pdf_content))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != "doc-disabled"
    assert tracking_temporal.start_workflow.call_count >= 1


@pytest.mark.parametrize("temporal_live", [True, False], ids=["temporal-live", "temporal-closed"])
@pytest.mark.parametrize("door", ["dedup_or_none", "upload"])
def test_disabled_same_path_starts_fresh(
    door, temporal_live, test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    """A soft-deleted row at the same path is never a hit, whether or not Temporal still answers."""
    filename = f"gone-same-path-{door}.pdf"
    wf = _upload_path_workflow_id(sample_pdf_content, filename)
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    if temporal_live:
        tracking_temporal._live[wf] = {"stage": "completed", "page_count": 1, "chunk_count": 1, "error_message": None}
    _seed(db_connection, wf, fp, filename, filepath=f"minio://documents/default/{fp}/{filename}")
    db_connection.set_document_disabled(wf, True)

    if door == "upload":
        resp = test_client.post("/upload", **_pdf(filename, sample_pdf_content))
        assert resp.status_code == 200, resp.text
        assert resp.json().get("duplicate") is False
        assert tracking_temporal.start_workflow.call_count >= 1
        wid = resp.json()["workflow_id"]
    else:
        summary, wid = _dedup(wf, fp=fp, filename=filename, instance="default")
        assert summary is None
    assert wid != wf
    assert wid.startswith(f"{wf}-rerun-")


def test_same_fingerprint_other_tenant_is_not_a_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    _seed(db_connection, "doc-tenant-a", hashlib.md5(sample_pdf_content).hexdigest(), "shared.pdf", instance="tenant-a")
    resp = test_client.post("/upload", params={"instance": "tenant-b"}, **_pdf("shared.pdf", sample_pdf_content))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != "doc-tenant-a"
    assert body.get("instance") == "tenant-b"


def test_concurrent_fingerprint_inserts_one_live_row(db_connection):
    _race(db_connection, "race-content-hash", ["wf-race-a", "wf-race-b"])
    assert _live_rows(db_connection, "race-content-hash") == 1


def test_insert_or_duplicate_lost_race_returns_winner(db_connection, tracking_temporal):
    fp = "insert-race-hash"

    def _claim(workflow_id):
        return asyncio.run(document_service.insert_or_duplicate(
            local_bypass_user(), workflow_id, document_id=fp, canonical_document_id=fp,
            filename=f"{workflow_id}.pdf", source_filename=f"{workflow_id}.pdf", source_file_fingerprint=fp,
            filepath=f"/tmp/{workflow_id}.pdf", stage="registered", instance="default",
        ))

    assert _claim("wf-first-claim") is None
    duplicate = _claim("wf-second-claim")
    assert duplicate is not None
    assert duplicate.duplicate is True
    assert duplicate.workflow_id == "wf-first-claim"


def test_soft_deleted_row_still_allows_fresh_fingerprint_insert(db_connection):
    fp = "disabled-then-fresh"
    _seed(db_connection, "wf-old-disabled", fp, "old.pdf")
    db_connection.set_document_disabled("wf-old-disabled", True)
    _seed(db_connection, "wf-old-disabled-rerun-1", fp, "old.pdf", stage="registered", filepath="/tmp/old-rerun.pdf")
    assert db_connection.find_live_document_by_fingerprint("default", fp)["workflow_id"] == "wf-old-disabled-rerun-1"


def _seed_legacy_duplicate_live_rows(db_mod, fingerprint: str) -> None:
    """Two live tenant+hash rows with the unique index and claims table gone.

    This is the #43 production state ``init_db`` used to skip uniqueness for.
    """
    now = datetime.utcnow().isoformat()
    with db_mod.get_connection() as conn:
        conn.execute("DROP INDEX IF EXISTS ux_documents_live_tenant_fingerprint")
        conn.execute("DROP TABLE IF EXISTS document_live_fingerprints")
        for wf, stage in (("wf-legacy-a", "completed"), ("wf-legacy-b", "failed")):
            conn.execute(
                "INSERT INTO documents (workflow_id, document_id, filename, filepath, source_file_fingerprint,"
                " canonical_document_id, stage, instance, created_at, updated_at, is_disabled)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (wf, fingerprint, f"{wf}.pdf", f"/tmp/{wf}.pdf", fingerprint, fingerprint, stage, "default", now, now),
            )
        conn.commit()
    db_mod.init_db()


def test_legacy_duplicate_db_blocks_concurrent_new_fingerprint(db_connection):
    """init_db on a dirty DB must still unique a brand-new hash."""
    _seed_legacy_duplicate_live_rows(db_connection, "legacy-dup-hash")
    with db_connection.get_connection() as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'ux_documents_live_tenant_fingerprint'"
        ).fetchone()
        claimed = conn.execute(
            "SELECT workflow_id FROM document_live_fingerprints WHERE fingerprint = ?", ("legacy-dup-hash",)
        ).fetchall()
    assert idx is None
    assert _live_rows(db_connection, "legacy-dup-hash") == 2
    assert [row["workflow_id"] for row in claimed] == ["wf-legacy-a"]

    _race(db_connection, "brand-new-hash", ["wf-new-a", "wf-new-b"])
    assert _live_rows(db_connection, "brand-new-hash") == 1
    assert _live_rows(db_connection, "legacy-dup-hash") == 2


def test_hard_delete_releases_fingerprint_claim(db_connection):
    fp = "hard-delete-then-reclaim"
    _seed(db_connection, "wf-hard-claimed", fp, "gone.pdf")
    db_connection.delete_document("wf-hard-claimed", cascade=True)
    _seed(db_connection, "wf-hard-reclaim", fp, "again.pdf", stage="registered")
    assert db_connection.find_live_document_by_fingerprint("default", fp)["workflow_id"] == "wf-hard-reclaim"
