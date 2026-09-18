#!/bin/bash
# Seed qrels + run suites 4/5/6 on H100.
set -euo pipefail
cd /home/aicloud/docs-pipeline
IMAGE=docs-pipeline-main:95f2816
NET=docs-pipeline_default
VOL=docs-pipeline_sqlite-data
LOG=/tmp/probe_marqo_qdrant_456.log

exec > >(tee "$LOG") 2>&1
echo "=== $(date -Is) start 4/5/6 ==="

# Seed qrels on host (Marqo HTTP only)
python3 /home/aicloud/docs-pipeline/scripts/seed_qrels_marqo.py

docker run --rm --network "$NET" \
  -e PYTHONPATH=/app \
  -e VECTOR_STORE_BACKEND=qdrant \
  -e QDRANT_URL=http://qdrant:6333 \
  -e MARQO_URL=http://marqo:8882 \
  -e MARQO_AB_INDEX=amul-veterinary-rebuild-20260821 \
  -e QDRANT_AB_INDEX=amul-veterinary-rebuild-20260821-qdrant \
  -e EMBEDDING_BACKEND=fastembed \
  -e EMBEDDING_NORMALIZE=true \
  -e HF_HOME=/data/hf-cache \
  -e QRELS_PATH=/tmp/qrels_seed.jsonl \
  -v "${VOL}:/data" \
  -v /home/aicloud/docs-pipeline/pipeline/embedding.py:/app/pipeline/embedding.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store_qdrant.py:/app/pipeline/vector_store_qdrant.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store.py:/app/pipeline/vector_store.py:ro \
  -v /home/aicloud/docs-pipeline/scripts/probe_marqo_qdrant_456.py:/tmp/probe_marqo_qdrant_456.py:ro \
  -v /tmp/qrels_seed.jsonl:/tmp/qrels_seed.jsonl:ro \
  -v /tmp:/out \
  "$IMAGE" \
  bash -lc 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && python /tmp/probe_marqo_qdrant_456.py --json-out /out/probe_marqo_qdrant_456.json'

echo "=== $(date -Is) done ==="
