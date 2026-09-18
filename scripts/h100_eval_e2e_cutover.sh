#!/bin/bash
# Full E2E eval + read-only cutover audit on H100. Does NOT flip backends.
set -euo pipefail
cd /home/aicloud/docs-pipeline
IMAGE=docs-pipeline-main:95f2816
NET=docs-pipeline_default
VOL=docs-pipeline_sqlite-data
LOG=/tmp/eval_e2e_cutover.log

exec > >(tee "$LOG") 2>&1
echo "=== $(date -Is) E2E eval + cutover audit (no prod changes) ==="

docker run --rm --network "$NET" \
  -e PYTHONPATH=/app \
  -e MARQO_URL=http://marqo:8882 \
  -e QDRANT_URL=http://qdrant:6333 \
  -e MARQO_AB_INDEX=amul-veterinary-rebuild-20260821 \
  -e QDRANT_AB_INDEX=amul-veterinary-rebuild-20260821-qdrant \
  -e EMBEDDING_BACKEND=fastembed \
  -e EMBEDDING_NORMALIZE=true \
  -e HF_HOME=/data/hf-cache \
  -e DOCUMENT_DB_PATH=/data/rebuild-20260821/documents.db \
  -e VECTOR_STORE_BACKEND=marqo \
  -e API_DOCS_ENABLED=false \
  -v "${VOL}:/data" \
  -v /home/aicloud/docs-pipeline/pipeline/embedding.py:/app/pipeline/embedding.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store_qdrant.py:/app/pipeline/vector_store_qdrant.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store.py:/app/pipeline/vector_store.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/api_docs.py:/app/pipeline/api_docs.py:ro \
  -v /home/aicloud/docs-pipeline/scripts/eval_marqo_qdrant_e2e.py:/tmp/eval_marqo_qdrant_e2e.py:ro \
  -v /home/aicloud/docs-pipeline/scripts/audit_qdrant_cutover_readiness.py:/tmp/audit_qdrant_cutover_readiness.py:ro \
  -v /tmp:/out \
  "$IMAGE" \
  bash -lc 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && \
    python /tmp/eval_marqo_qdrant_e2e.py --json-out /out/eval_marqo_qdrant_e2e.json --qrels-out /out/qrels_consensus.jsonl; \
    echo; \
    python /tmp/audit_qdrant_cutover_readiness.py'

echo "=== $(date -Is) done (CHANGED_NOTHING) ==="
