#!/usr/bin/env bash
set -eu
PDF="/home/aicloud/docs-pipeline/incoming/sabar_test.pdf"
API="http://127.0.0.1:8001"

echo "==> Chandra health"
curl -sf "http://127.0.0.1:8010/health" && echo || echo "CHANDRA_DOWN"

echo "==> Uploading $PDF"
RESP=$(curl -sf -X POST "${API}/upload?auto_approve=true&stop_after_ocr=true" \
  -F "file=@${PDF}")
echo "$RESP"
WID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['workflow_id'])")
echo "workflow_id=$WID"

echo "==> Polling status"
for i in $(seq 1 60); do
  STATUS=$(curl -sf "${API}/documents/${WID}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stage'), d.get('page_count'), d.get('error_message'))")
  echo "[$i] $STATUS"
  echo "$STATUS" | grep -qE "ocr_review|failed|completed" && break
  sleep 5
done
