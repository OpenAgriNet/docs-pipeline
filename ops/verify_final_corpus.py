"""Read-only reconciliation of a backfill against its manifest and prior corpus."""
import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def normalized_hash(text):
    return hashlib.sha256(re.sub(r'\s+', ' ', text or '').strip().encode()).hexdigest()


def verify(root, target):
    from pipeline import db, vector_store
    from pipeline.ingestion_records import prepare_records
    if vector_store.vector_store_backend() != 'qdrant' or vector_store.default_physical_index() != target:
        raise RuntimeError('Explicit final target configuration required')
    client = vector_store.get_vector_store().client()
    by_doc = defaultdict(list)
    baseline_live = {}
    offset = None
    total = 0
    while True:
        points, offset = client.scroll(target, limit=512, offset=offset, with_payload=True, with_vectors=False)
        for point in points:
            p = point.payload
            text = re.sub(r'\s+', ' ', p.get('text','')).strip()
            rid = p.get('record_id',str(point.id))
            baseline_live[rid] = [rid,p.get('doc_id',''),p.get('filename',''),p.get('name_en',''),p.get('workflow_id',''),normalized_hash(text),len(text)]
            by_doc[p.get('doc_id','')].append(p)
            total += 1
        if offset is None:
            break
    old = json.loads((root/'baseline-rows.json').read_text())
    altered = [r[0] for r in old if baseline_live.get(r[0]) != r]
    documents = []
    for source in json.loads((root/'manifest.json').read_text()):
        did = source['source_id'].removeprefix('legacy:')
        with db.get_connection() as c:
            row = c.execute("SELECT * FROM documents WHERE document_id=? AND instance='amul' AND COALESCE(is_disabled,0)=0 ORDER BY created_at DESC LIMIT 1",(did,)).fetchone()
        points = by_doc.get(did,[])
        report = {'filename':source['filename'],'document_id':did,'indexed_chunks':len(points),'verified':False}
        if row:
            doc = dict(row)
            chunks = db.get_chunks(doc['workflow_id'],include_excluded=True)
            records = prepare_records(did,doc['filename'],chunks,workflow_id=doc['workflow_id'],instance=doc['instance'])
            expected = {str(r['_id']):normalized_hash(r['text']) for r in records}
            actual = {str(p['record_id']):normalized_hash(p['text']) for p in points}
            report.update(workflow_id=doc['workflow_id'],stage=doc['stage'],expected_chunks=len(records),
                          verified=bool(records) and actual==expected and doc['stage']=='completed')
        documents.append(report)
    result = {'verified_at':datetime.now(timezone.utc).isoformat(),'target':target,'points':total,
              'unique_indexed_files':len(by_doc),'baseline_records':len(old),'altered_or_missing_baseline':len(altered),
              'altered_or_missing_baseline_examples':altered[:10],'batch_documents':documents,
              'batch_verified':sum(d['verified'] for d in documents)}
    result['complete'] = not altered and result['batch_verified']==len(documents)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source-root',required=True)
    p.add_argument('--target',required=True)
    args = p.parse_args()
    root = Path(args.source_root)
    report = verify(root,args.target)
    (root/'final-verification.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps({k:v for k,v in report.items() if k!='batch_documents'},indent=2))
