#!/usr/bin/env bash
# Create a passage-style Marqo index with E5 embedding text and metadata.
# Usage: MARQO_URL=http://localhost:8882 ./scripts/create_marqo_passage_index.sh
#        Or from host: ./scripts/create_marqo_passage_index.sh

set -e
MARQO_URL="${MARQO_URL:-http://localhost:8882}"
INDEX_NAME="${INDEX_NAME:-documents-index}"

SETTINGS='{
  "type": "structured",
  "vectorNumericType": "float",
  "model": "hf/multilingual-e5-large",
  "normalizeEmbeddings": false,
  "textPreprocessing": {"splitLength": 3, "splitOverlap": 1, "splitMethod": "sentence"},
  "allFields": [
    {"name": "doc_id", "type": "text", "features": ["filter"]},
    {"name": "type", "type": "text", "features": ["filter"]},
    {"name": "source", "type": "text", "features": ["filter"]},
    {"name": "filename", "type": "text", "features": ["filter"]},
    {"name": "name_gu", "type": "text", "features": ["filter"]},
    {"name": "name_en", "type": "text", "features": ["filter"]},
    {"name": "title_en", "type": "text", "features": ["filter"]},
    {"name": "title_gu", "type": "text", "features": ["filter"]},
    {"name": "doc_language", "type": "text", "features": ["filter"]},
    {"name": "category_tags", "type": "text", "features": ["filter"]},
    {"name": "doc_short_description", "type": "text", "features": ["filter"]},
    {"name": "doc_llm_description", "type": "text", "features": ["filter"]},
    {"name": "ingestion_status", "type": "text", "features": ["filter"]},
    {"name": "description", "type": "text", "features": ["lexical_search"]},
    {"name": "chunk_num", "type": "int", "features": ["filter"]},
    {"name": "token_count", "type": "int", "features": ["filter"]},
    {"name": "page_start", "type": "int", "features": ["filter"]},
    {"name": "page_end", "type": "int", "features": ["filter"]},
    {"name": "is_reference", "type": "bool", "features": ["filter"]},
    {"name": "quality_score", "type": "float", "features": ["filter"]},
    {"name": "priority_rank", "type": "float", "features": ["filter"]},
    {"name": "text", "type": "text", "features": ["lexical_search"]},
    {"name": "priority", "type": "float", "features": ["score_modifier", "filter"]},
    {"name": "text_for_embedding", "type": "text"}
  ],
  "tensorFields": ["text_for_embedding"]
}'

echo "Creating index $INDEX_NAME at $MARQO_URL ..."
RESP=$(curl -s -X POST "${MARQO_URL}/indexes/${INDEX_NAME}" \
  -H "Content-Type: application/json" \
  -d "$SETTINGS")
echo "$RESP" | jq .
if echo "$RESP" | jq -e '.acknowledged == true' >/dev/null 2>&1; then
  echo "Index created. Run bulk reingest, then verify: curl -s -X POST \"${MARQO_URL}/indexes/${INDEX_NAME}/search\" -H 'Content-Type: application/json' -d '{\"q\":\"veterinary\",\"limit\":3}' | jq"
else
  echo "Create may have failed. Check response above."
  exit 1
fi
