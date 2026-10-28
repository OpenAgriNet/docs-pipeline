"""Read-only H100 completion monitor; never retries or changes services/indexes."""
import argparse
import json
import subprocess
import time
from pathlib import Path

READ_ONLY_AUDIT = r'''
import sys,json,sqlite3,urllib.request,re,hashlib
from collections import defaultdict
from datetime import datetime,timezone
from pipeline.ingestion_records import prepare_records
inputs=json.load(sys.stdin)
target=inputs['target']
assert target=='amul-veterinary-final-20261023'
c=sqlite3.connect('file:/data/rebuild-20260821/documents.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
def norm(t):return re.sub(r'\s+',' ',t or '').strip()
def digest(t):return hashlib.sha256(norm(t).encode()).hexdigest()
def api(path,body):
 req=urllib.request.Request('http://qdrant-dev-native:6333/collections/'+target+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
 return json.load(urllib.request.urlopen(req,timeout=60))['result']
by_doc=defaultdict(list);actual={};offset=None
while True:
 body={'limit':512,'with_payload':True,'with_vector':False}
 if offset is not None:body['offset']=offset
 page=api('/points/scroll',body)
 for point in page['points']:
  p=point['payload'];rid=p.get('record_id',str(point['id']));text=norm(p.get('text',''))
  actual[rid]=[rid,p.get('doc_id',''),p.get('filename',''),p.get('name_en',''),p.get('workflow_id',''),digest(text),len(text)]
  by_doc[p.get('doc_id','')].append(p)
 offset=page.get('next_page_offset')
 if offset is None:break
changed=[row[0] for row in inputs['baseline'] if actual.get(row[0])!=row]
reports=[]
for source in inputs['manifest']:
 did=source['source_id'].removeprefix('legacy:')
 row=c.execute("SELECT * FROM documents WHERE document_id=? AND instance='amul' AND COALESCE(is_disabled,0)=0 ORDER BY created_at DESC LIMIT 1",(did,)).fetchone()
 report={'filename':source['filename'],'document_id':did,'verified':False,'indexed_chunks':len(by_doc[did])}
 if row:
  doc=dict(row);chunks=[dict(r) for r in c.execute('SELECT * FROM chunks WHERE workflow_id=? ORDER BY chunk_number',(doc['workflow_id'],))]
  records=prepare_records(did,doc['filename'],chunks,workflow_id=doc['workflow_id'],instance=doc['instance'])
  expected={str(r['_id']):digest(r['text']) for r in records}
  found={str(p['record_id']):digest(p['text']) for p in by_doc[did]}
  report.update(workflow_id=doc['workflow_id'],stage=doc['stage'],expected_chunks=len(records),verified=bool(records) and found==expected and doc['stage']=='completed')
 reports.append(report)
result={'verified_at':datetime.now(timezone.utc).isoformat(),'target':target,'points':len(actual),'unique_indexed_files':sum(bool(p) for p in by_doc.values()),'baseline_records':len(inputs['baseline']),'altered_or_missing_baseline':len(changed),'batch_documents':reports,'batch_verified':sum(r['verified'] for r in reports)}
result['complete']=not changed and result['batch_verified']==len(reports)
print(json.dumps(result))
'''


def main(args):
    root=Path(args.root)
    inputs={'target':args.target,'baseline':json.loads((root/'baseline-rows.json').read_text()),
            'manifest':json.loads((root/'manifest.json').read_text())}
    while True:
        result=subprocess.run(['docker','exec','-i','-e','PYTHONDONTWRITEBYTECODE=1','docs-pipeline-api-1',
                               'python','-c',READ_ONLY_AUDIT],input=json.dumps(inputs),capture_output=True,text=True,timeout=180)
        if result.returncode:
            (root/'monitor-last-error.log').write_text(result.stderr)
            time.sleep(60)
            continue
        report=json.loads(result.stdout)
        (root/'monitored-verification.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
        print(json.dumps({k:report[k] for k in ['verified_at','batch_verified','points','unique_indexed_files','altered_or_missing_baseline']}),flush=True)
        if report['complete'] or report['altered_or_missing_baseline']:
            break
        info=json.loads(subprocess.check_output(['docker','inspect',args.container]))[0]
        if not info['State']['Running']:
            report['attention_required']=[r['filename'] for r in report['batch_documents'] if not r['verified']]
            break
        time.sleep(60)
    (root/'COMPLETION.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--root',required=True)
    p.add_argument('--container',required=True)
    p.add_argument('--target',required=True)
    main(p.parse_args())
