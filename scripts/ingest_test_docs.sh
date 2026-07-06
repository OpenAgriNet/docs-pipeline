#!/usr/bin/env bash
# Ingest all PDFs from ~/test-docs via H100 pipeline API.
set -euo pipefail

DOCS_DIR="${1:-$HOME/test-docs}"
API="${PIPELINE_API:-http://127.0.0.1:8001}"

if [[ ! -d "$DOCS_DIR" ]]; then
  echo "Missing directory: $DOCS_DIR"
  exit 1
fi

shopt -s nullglob
pdfs=("$DOCS_DIR"/*.pdf)
if [[ ${#pdfs[@]} -eq 0 ]]; then
  echo "No PDFs in $DOCS_DIR"
  exit 1
fi

echo "API: $API"
echo "Files: ${#pdfs[@]}"
curl -sf "$API/health" | python3 -m json.tool || { echo "API not reachable"; exit 1; }
echo ""

for f in "${pdfs[@]}"; do
  name="$(basename "$f")"
  echo "========================================"
  echo "UPLOAD: $name"
  resp="$(curl -s -X POST "$API/upload?auto_approve=true" -F "file=@$f")"
  echo "$resp" | python3 -m json.tool || echo "$resp"
  wf="$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('workflow_id',''))" 2>/dev/null || true)"
  if [[ -n "$wf" ]]; then
    echo "workflow_id: $wf"
  fi
  echo ""
done

echo "Recent documents:"
curl -s "$API/documents?limit=10" | python3 -m json.tool
