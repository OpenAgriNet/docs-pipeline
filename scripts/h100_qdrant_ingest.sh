#!/bin/bash
# H100-only: create Qdrant collection mapping + ingest rebuild SQLite chunks.
# Uses live image docs-pipeline-main:95f2816 (host checkout is incomplete).
set -euo pipefail

cd /home/aicloud/docs-pipeline
INDEX=amul-veterinary-rebuild-20260821-qdrant
IMAGE=docs-pipeline-main:95f2816
NET=docs-pipeline_default
VOL=docs-pipeline_sqlite-data
LOG=/tmp/qdrant_ingest.log

exec > >(tee -a "$LOG") 2>&1

echo "=== $(date -Is) start ==="
echo "=== qdrant status ==="
docker compose up -d qdrant
curl -sf http://127.0.0.1:6333/readyz
echo
curl -s http://127.0.0.1:6333/collections | python3 -m json.tool

run_api() {
  docker run --rm --network "$NET" \
    -e PYTHONPATH=/app \
    -e VECTOR_STORE_BACKEND=qdrant \
    -e QDRANT_URL=http://qdrant:6333 \
    -e QDRANT_INDEX_NAME="$INDEX" \
    -e DOCUMENT_DB_PATH=/data/rebuild-20260821/documents.db \
    -e EMBEDDING_BACKEND=fastembed \
    -e EMBEDDING_NORMALIZE=true \
    -e HF_HOME=/data/hf-cache \
    -v "${VOL}:/data" \
    -v /home/aicloud/docs-pipeline/pipeline/embedding.py:/app/pipeline/embedding.py:ro \
    -v /home/aicloud/docs-pipeline/pipeline/vector_store_qdrant.py:/app/pipeline/vector_store_qdrant.py:ro \
    -v /home/aicloud/docs-pipeline/pipeline/vector_store.py:/app/pipeline/vector_store.py:ro \
    -v /home/aicloud/docs-pipeline/scripts/reindex_sqlite_to_qdrant.py:/tmp/reindex_sqlite_to_qdrant.py:ro \
    -v /home/aicloud/docs-pipeline/scripts/h100_qdrant_create_collection.py:/tmp/h100_qdrant_create_collection.py:ro \
    "$IMAGE" \
    bash -lc "$*"
}

echo "=== dry-run ==="
run_api 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && python /tmp/reindex_sqlite_to_qdrant.py --index-name '"$INDEX"' --dry-run'

echo "=== create collection + print mapping ==="
run_api 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && python /tmp/h100_qdrant_create_collection.py'

echo "=== HTTP collection mapping ==="
curl -s "http://127.0.0.1:6333/collections/$INDEX" | python3 -m json.tool | head -200

echo "=== INGEST (long: first FastEmbed download + ~37k chunks) ==="
run_api 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && python /tmp/reindex_sqlite_to_qdrant.py --index-name '"$INDEX"' --batch-size 8'

echo "=== final collection ==="
curl -s "http://127.0.0.1:6333/collections/$INDEX" | python3 -m json.tool | head -100
echo "=== $(date -Is) done ==="
