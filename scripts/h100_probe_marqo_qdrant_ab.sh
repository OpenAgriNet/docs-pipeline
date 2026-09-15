#!/bin/bash
# Run Marqo vs Qdrant A/B probe on H100.
set -euo pipefail
cd /home/aicloud/docs-pipeline
IMAGE=docs-pipeline-main:95f2816
NET=docs-pipeline_default
VOL=docs-pipeline_sqlite-data
OUT=/tmp/probe_marqo_qdrant_ab.json
LOG=/tmp/probe_marqo_qdrant_ab.log

exec > >(tee "$LOG") 2>&1
echo "=== $(date -Is) probe start ==="

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
  -v "${VOL}:/data" \
  -v /home/aicloud/docs-pipeline/pipeline/embedding.py:/app/pipeline/embedding.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store_qdrant.py:/app/pipeline/vector_store_qdrant.py:ro \
  -v /home/aicloud/docs-pipeline/pipeline/vector_store.py:/app/pipeline/vector_store.py:ro \
  -v /home/aicloud/docs-pipeline/scripts/probe_marqo_qdrant_ab.py:/tmp/probe_marqo_qdrant_ab.py:ro \
  -v /tmp:/out \
  "$IMAGE" \
  bash -lc 'pip install -q "qdrant-client>=1.12.0" "fastembed>=0.4.0" && python /tmp/probe_marqo_qdrant_ab.py --json-out /out/probe_marqo_qdrant_ab.json'

echo "=== $(date -Is) probe done ==="
