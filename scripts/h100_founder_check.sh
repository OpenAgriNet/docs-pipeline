#!/usr/bin/env bash
# Run ON H100 after: cd ~/docs-pipeline && bash scripts/h100_founder_check.sh
set -euo pipefail

echo "==> 1) Marqo stats + unique doc count (may take 1-2 min)"
docker exec docs-pipeline-api-1 python3 /app/workspace/scripts/get_ingested_filenames.py 2>/dev/null | tee /tmp/marqo_all_filenames.txt | wc -l
echo "   (line count above = unique filenames in Marqo)"

echo ""
echo "==> 2) Fuzzy founder check (Marqo catalog scan)"
if [[ -f scripts/check_founder_list.py ]]; then
  docker cp scripts/check_founder_list.py docs-pipeline-api-1:/tmp/check_founder_list.py
  docker exec docs-pipeline-api-1 python3 /tmp/check_founder_list.py
else
  echo "   scripts/check_founder_list.py not found — scp from laptop first"
fi

echo ""
echo "==> 3) Find Kanav spreadsheet names on disk (partial)"
CORPUS="/home/aicloud/search/search/AMUL Docs"
for pat in "PARIPATRA NO.52" "PARIPATRA NO.69" "NO.73" "PARIPATRA NO. 5" "Paripatra No.66" "Milking Machine" "Paripatra No.29" "SILAGE" "Homeopathic" "cattle_feed"; do
  echo "--- find *${pat}* ---"
  find "$CORPUS" -iname "*${pat}*" 2>/dev/null | head -5
done

echo ""
echo "==> 4) Marqo keyword search (not exact filename)"
docker exec docs-pipeline-api-1 python3 -c "
import marqo
idx = marqo.Client(url='http://marqo:8882').index('amul-veterinary-index')
for q in ['SABAR PARIPATRA 52', 'PARIPATRA NO.52', 'TEAT DIP', 'PREGNANCY TEST', 'CATTLE INSURANCE', 'SILAGE', 'Milking Machine Subsidy', 'Homeopathic', 'cattle feed rate']:
    hits = idx.search(q=q, limit=5, attributes_to_retrieve=['filename','name_en'])
    fns = [h.get('filename') for h in hits.get('hits',[])]
    print(q, '->', fns)
"

echo ""
echo "==> 5) SQLite: any filename containing SABAR or Paripatra"
docker exec docs-pipeline-api-1 python3 -c "
import sqlite3
conn = sqlite3.connect('/data/documents.db')
rows = conn.execute(\"\"\"
  SELECT filename, stage, chunk_count FROM documents
  WHERE lower(filename) LIKE '%sabar%' OR lower(filename) LIKE '%paripatra%'
  ORDER BY filename LIMIT 40
\"\"\").fetchall()
print('rows:', len(rows))
for r in rows: print(r)
"
