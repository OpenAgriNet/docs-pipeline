import os, sys, threading, secrets
sys.path.insert(0, "/work/source")
sys.path.insert(0, "/cutover")
from pathlib import Path
import onnxruntime as ort
ort.preload_dlls(directory='')
from fastembed import TextEmbedding, SparseTextEmbedding
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, Field, ConfigDict
from pipeline.embedding import FastEmbedEmbedder
from pipeline.vector_store_qdrant import QdrantStore
from ops.qdrant_mirror_health import mirror_health
COLLECTION='amul-veterinary-final-20261023'
encoder=object.__new__(FastEmbedEmbedder)
encoder.dense_model_name='intfloat/multilingual-e5-large';encoder.sparse_model_name='Qdrant/bm25';encoder.normalize=False;encoder._dense_dim=1024
encoder._dense=TextEmbedding(model_name=encoder.dense_model_name,cache_dir='/job/cache',threads=4,local_files_only=True,providers=[('CUDAExecutionProvider',{'device_id':0,'gpu_mem_limit':6442450944,'arena_extend_strategy':'kSameAsRequested'}),'CPUExecutionProvider'])
assert 'CUDAExecutionProvider' in encoder._dense.model.model.get_providers()
encoder._sparse=SparseTextEmbedding(model_name=encoder.sparse_model_name,cache_dir='/job/cache',local_files_only=True)
store=QdrantStore(url='http://127.0.0.1:6336',embedder=encoder)
TOKEN=os.environ['QDRANT_SEARCH_API_KEY'].strip()
if len(TOKEN)<32: raise ValueError('QDRANT_SEARCH_API_KEY must contain at least 32 characters')
def require_auth(authorization: str | None = Header(default=None)):
 if not secrets.compare_digest(authorization or '', 'Bearer '+TOKEN):
  raise HTTPException(401,'authentication required')
lock=threading.Lock();app=FastAPI(dependencies=[Depends(require_auth)])
def require_current_source():
 try:
  return mirror_health(os.environ['QDRANT_SYNC_STATUS_PATH'], COLLECTION)
 except (KeyError, RuntimeError) as error:
  raise HTTPException(503, 'authoritative source synchronization is unavailable or stale') from error
class Search(BaseModel):
 model_config=ConfigDict(extra='forbid')
 q:str=Field(min_length=1,max_length=8000)
 limit:int=Field(default=120,ge=1,le=200)
 alpha:float=Field(default=.6,ge=0,le=1)
 rrf_k:int=Field(default=60,ge=1,le=1000)
 exclude_reference:bool=False
@app.get('/health')
def health():
 sync = require_current_source()
 if not Path('/work/collection-complete.json').exists():raise HTTPException(503,'collection not verified')
 report=store.describe_index(COLLECTION)
 if report.schema_errors:raise HTTPException(503,str(report.schema_errors))
 return {'status':'ok','collection':COLLECTION,'count':store.get_stats(COLLECTION)['numberOfDocuments'],'backend':'qdrant','model':encoder.dense_model_name,'device':'cuda','tenant':'amul','sync':sync}
@app.post('/search')
def search(body:Search):
 require_current_source()
 if not Path('/work/collection-complete.json').exists():raise HTTPException(503,'collection not verified')
 filters='instance:(amul) AND query_enabled:true'
 if body.exclude_reference:filters+=' AND is_reference:false'
 with lock:
  result=store.search(COLLECTION,q=body.q,limit=body.limit,search_method='hybrid',hybrid_parameters={'alpha':body.alpha,'rrfK':body.rrf_k,'rankingMethod':'rrf'},filter_string=filters)
 return {**result,'collection':COLLECTION,'backend':'qdrant'}
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host=os.environ.get('QDRANT_BIND_HOST','127.0.0.1'),port=int(os.environ.get('QDRANT_PORT','8016')))
