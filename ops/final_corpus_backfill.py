"""Explicit, resumable operator backfill using the normal pipeline activities.

Run in a dedicated container with the tested chunk-window fix, existing storage
configuration, and QDRANT_INDEX_NAME set to the intended final collection. Never
changes the running API/worker or chat routing. State/logs belong on a host mount.
"""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path


def now():
    return datetime.utcnow().isoformat()


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    tmp.replace(path)


def validate_manifest(rows, root):
    seen = set()
    for row in rows:
        name = row['filename']
        if name != Path(name).name or name in seen:
            raise ValueError(f'Unsafe or repeated source name: {name}')
        seen.add(name)
        if int(row['original_final_points']) != 0:
            raise ValueError(f'Source is not a missing-target candidate: {name}')
        if row['download_status'] == 'original_verified':
            data = (root / 'originals' / name).read_bytes()
            if hashlib.md5(data).hexdigest() != row['downloaded_md5']:
                raise ValueError(f'Source fingerprint mismatch: {name}')
        elif row['download_status'] == 'indexed_text_only_original_unavailable':
            data = json.loads((root / 'indexed-only' / (name + '.indexed-records.json')).read_text())
            if not data['points'] or data['doc_id'] != row['source_id'].removeprefix('legacy:'):
                raise ValueError(f'Invalid recovered payload: {name}')
        else:
            raise ValueError(f'Unsupported source state: {name}')


async def main(args):
    from pipeline import db, vector_store
    from pipeline.services import indexes
    from pipeline.storage import minio as storage
    from pipeline.temporal import document_tasks as tasks
    from pipeline.chunking.openai_compatible import OpenAiCompatibleChunkingProvider

    root = Path(args.source_root)
    rows = json.loads((root / 'manifest.json').read_text())
    validate_manifest(rows, root)
    if args.only:
        rows = [r for r in rows if r['filename'] in args.only]
        if len(rows) != len(set(args.only)):
            raise ValueError('An explicitly selected file is absent from the manifest')
    if vector_store.vector_store_backend() != 'qdrant':
        raise RuntimeError('Backfill requires an explicit Qdrant backend')
    if indexes.resolve_index('amul') != args.target or vector_store.default_physical_index() != args.target:
        raise RuntimeError('Configured collection does not match the explicit target')
    if not getattr(OpenAiCompatibleChunkingProvider, 'handles_window_fallback', False):
        raise RuntimeError('Install the tested checkpoint-safe chunker before applying')
    store = vector_store.get_vector_store()
    client = store.client()
    client.get_collection(args.target)  # Never create a missing collection.
    if not args.apply:
        print(json.dumps({'target': args.target, 'sources': len(rows), 'apply': False}))
        return
    from qdrant_client import models as qm
    state_path = root / args.state_file
    state = {'target': args.target, 'started_at': now(), 'state': 'running', 'documents': {}}
    if state_path.exists():
        state['previous_run'] = json.loads(state_path.read_text()).get('started_at')
    lock = asyncio.Lock()

    def count(workflow):
        return client.count(args.target, count_filter=qm.Filter(must=[qm.FieldCondition(
            key='workflow_id', match=qm.MatchValue(value=workflow))]), exact=True).count

    async def report(name, **values):
        async with lock:
            state['documents'].setdefault(name, {}).update(values)
            state['updated_at'] = now()
            atomic_json(state_path, state)
            print(json.dumps({'file': name, **values}, ensure_ascii=False), flush=True)

    def register(row):
        name = row['filename']
        did = row['source_id'].removeprefix('legacy:')
        with db.get_connection() as c:
            found = c.execute('SELECT * FROM documents WHERE instance=? AND document_id=? AND COALESCE(is_disabled,0)=0 ORDER BY CASE WHEN workflow_id=? THEN 0 ELSE 1 END, created_at DESC LIMIT 1',
                              ('amul', did, row.get('selected_workflow', ''))).fetchone()
        if found:
            return dict(found)
        recovery = row['download_status'].startswith('indexed_text_only')
        wf = ('index-recovery-' if recovery else 'final-backfill-') + did[:24]
        source = root / ('indexed-only' if recovery else 'originals') / (name + '.indexed-records.json' if recovery else name)
        key = f'amul/final-backfill-20261028/{did}/{source.name}'
        storage.get_client().fput_object(storage.bucket_name(), key, str(source))
        uri = f'minio://{storage.bucket_name()}/{key}'
        db.upsert_document(wf, did, name, uri, instance='amul', canonical_document_id=did,
                           source_filename=name, source_file_fingerprint=None if recovery else row['downloaded_md5'],
                           source_type='index_recovery' if recovery else 'document',
                           canonical_input_type='index_payload' if recovery else source.suffix.lstrip('.'))
        db.add_document_artifact(workflow_id=wf, artifact_type='recovered_index_payload' if recovery else 'original_upload',
                                 storage_uri=uri, filename=source.name, size_bytes=source.stat().st_size,
                                 metadata={'source_url': row.get('source_url'), 'original_pdf_available': not recovery,
                                           'target': args.target, 'recovery_note': row.get('export_source')})
        if recovery:
            payload = json.loads(source.read_text())
            points = sorted(payload['points'], key=lambda p: p['payload'].get('chunk_num',0))
            chunks = []
            for number, point in enumerate(points, 1):
                p = point['payload']
                chunks.append({'chunk_number': number, 'original_text': p['text'], 'page_start': p.get('page_start',1),
                               'page_end': p.get('page_end',1), 'is_reference': bool(p.get('is_reference')),
                               'chunking_provider': 'index_recovery', 'reviewer_notes': 'Recovered indexed text; original PDF unavailable.'})
            db.save_chunks(wf, chunks)
            db.update_document_fields(wf, chunk_count=len(chunks), page_count=max(c['page_end'] or 1 for c in chunks))
        db.log_audit(workflow_id=wf, document_id=did, action_type='final_corpus_backfill_registered',
                     metadata={'target': args.target, 'source_uri': uri, 'original_pdf_available': not recovery})
        return db.get_document(wf)

    async def process(row):
        name = row['filename']
        doc = register(row)
        wf, did = doc['workflow_id'], doc['document_id']
        await report(name, workflow_id=wf, document_id=did, stage='queued')
        previous = db.get_latest_document_job(wf)
        config = json.loads(previous.get('config_json') or '{}') if previous else {}
        # Carry the existing large-document checkpoint into this explicitly bound job.
        config.update(source='final_corpus_backfill', target=args.target)
        job = db.create_document_job(workflow_id=wf, job_type='final_backfill', status='running',
                                     current_stage='registered', config=config)
        try:
            chunks = db.get_chunks(wf, include_excluded=True)
            checkpoint = config.get('chunk_checkpoint', {})
            incomplete_chunks = checkpoint.get('status') in ('running','interrupted')
            if not chunks or incomplete_chunks:
                pages = db.get_pages(wf)
                empty_ocr = bool(pages) and not any((p.get('edited_markdown') or p.get('original_markdown') or '').strip() for p in pages)
                if not pages or not doc.get('ocr_completed_at') or empty_ocr:
                    await tasks.update_document_state(wf, 'ocr_processing', doc.get('page_count',0))
                    await report(name, stage='ocr_processing')
                    result = await tasks.run_ocr_and_store(wf, doc['filepath'], force_redo=empty_ocr, retry_job_id=job)
                    if not any((p.get('original_markdown') or '').strip() for p in db.get_pages(wf)):
                        raise RuntimeError('OCR returned no text; refusing empty ingestion')
                    if empty_ocr:
                        db.update_document_fields(wf, translation_completed_at=None)
                    db.update_document_fields(wf, ocr_completed_at=now(), page_count=result['page_count'])
                doc = db.get_document(wf)
                if not doc.get('translation_completed_at'):
                    await tasks.update_document_state(wf, 'translation_processing', doc['page_count'])
                    await report(name, stage='translation_processing')
                    await tasks.detect_and_translate_pages_from_db(wf)
                    db.update_document_fields(wf, translation_completed_at=now())
                await tasks.update_document_state(wf, 'chunking', doc['page_count'])
                await report(name, stage='chunking', resume_page=checkpoint.get('next_page_offset',0))
                await tasks.create_chunks_from_db(wf, retry_job_id=job)
                # Apply the normal pipeline's domain tagging to newly generated chunks.
                await tasks.auto_tag_chunks_from_db(wf, name)
            doc = db.get_document(wf)
            await tasks.update_document_state(wf, 'ingesting', doc.get('page_count',0), doc.get('chunk_count',0))
            await report(name, stage='ingesting')
            result = await tasks.ingest_document_from_db(wf, did, name, index_name=args.target)
            actual = count(wf)
            expected = result.get('records_ingested',0)
            if not expected or actual != expected:
                raise RuntimeError(f'Index verification failed: expected {expected}, found {actual}')
            db.update_document_fields(wf, ingested_at=now(), reindex_required=0, reindex_reason=None)
            await tasks.update_document_state(wf, 'completed', doc.get('page_count',0), len(db.get_chunks(wf)))
            db.log_audit(workflow_id=wf, document_id=did, action_type='final_corpus_backfill_verified',
                         metadata={'target': args.target, 'indexed_chunks': actual})
            await report(name, stage='completed', indexed_chunks=actual)
        except Exception as exc:
            await tasks.update_document_state(wf, 'failed', error_message=f'{type(exc).__name__}: {exc}')
            await report(name, stage='failed', error=f'{type(exc).__name__}: {exc}')

    # Keep two book resumptions and one small-source lane active. All state is on H100.
    books = [r for r in rows if r['filename'] in ('Veterinary Medicine.pdf','The_Merck_Manual_of_Diagnosis_and_Therapy_2011_-_19th_Edn........pdf')]
    others = sorted([r for r in rows if r not in books], key=lambda r: (0 if r.get('selected_workflow') else 1, r.get('downloaded_bytes',0)))
    async def small_lane():
        for row in others:
            await process(row)
    await asyncio.gather(*(process(r) for r in books), small_lane())
    state['state'] = 'completed' if all(r.get('stage') == 'completed' for r in state['documents'].values()) else 'completed_with_failures'
    state['finished_at'] = now()
    atomic_json(state_path, state)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source-root', required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--only', action='append', default=[])
    p.add_argument('--state-file', default='run-status.json')
    asyncio.run(main(p.parse_args()))
