#!/usr/bin/env bash
# Paste this entire file into your H100 SSH session: bash h100_paste_find.sh
# Or: curl won't work — copy/paste from laptop.
set -euo pipefail
cd ~/docs-pipeline 2>/dev/null || true
CORPUS="/home/aicloud/search/search/AMUL Docs"

echo "========== DISK: find Kanav docs by number/topic =========="
for pat in \
  "PARIPATRA*52*" "PARIPATRA*69*" "TEAT*DIP*" "PREGNANCY*TEST*73*" \
  "INSURANCE*73*" "PARIPATRA*5*" "Paripatra*66*" "Milking*Machine*70*" \
  "Paripatra*29*" "SILAGE*6*" "Homeopathic" "cattle*feed*rate*"; do
  echo "--- *${pat}* ---"
  find "$CORPUS" -iname "*${pat}*" 2>/dev/null | head -5 || true
done

echo ""
echo "========== DISK: all Sabar PARIPATRA / CIRCULAR files =========="
find "$CORPUS" -iname "Sabar_*" 2>/dev/null | grep -iE "PARIPATRA|CIRCULAR" | sort

echo ""
echo "========== MARQO: keyword search (inside Docker) =========="
cat > /tmp/marqo_keyword_find.py << 'PYEOF'
import marqo

idx = marqo.Client(url="http://marqo:8882").index("amul-veterinary-index")
print("Marqo chunks:", idx.get_stats().get("numberOfDocuments"))

queries = [
    "PARIPATRA 52", "TEAT DIP 69", "PREGNANCY TEST 73", "CATTLE INSURANCE 73",
    "PARIPATRA 5", "Paripatra 66", "Milking Machine subsidy 70", "Paripatra 29",
    "SILAGE paripatra 6", "Homeopathic Gujarati", "cattle feed rate change",
]
for q in queries:
    hits = idx.search(q=q, limit=5, attributes_to_retrieve=["filename", "name_en", "doc_id"])
    fns = []
    for h in hits.get("hits", []):
        fn = h.get("filename") or ""
        if fn and fn not in fns:
            fns.append(fn)
    print(f"{q!r:40} -> {fns}")

print("\n--- Marqo filenames containing 'sabar' (semantic search, deduped) ---")
seen = set()
for q in ("SABAR paripatra", "SABAR circular"):
    for h in idx.search(q=q, limit=100, attributes_to_retrieve=["filename"]).get("hits", []):
        fn = (h.get("filename") or "").strip()
        if fn:
            seen.add(fn)
for fn in sorted(seen):
    print(" ", fn)
print(f"Total unique sabar-ish filenames in sample: {len(seen)}")
PYEOF
docker cp /tmp/marqo_keyword_find.py docs-pipeline-api-1:/tmp/marqo_keyword_find.py
docker exec docs-pipeline-api-1 python3 /tmp/marqo_keyword_find.py

echo ""
echo "========== FUZZY founder check (if script on disk) =========="
if [[ -f scripts/check_founder_list.py ]]; then
  docker cp scripts/check_founder_list.py docs-pipeline-api-1:/tmp/check_founder_list.py
  docker exec docs-pipeline-api-1 python3 /tmp/check_founder_list.py
else
  echo "scripts/check_founder_list.py not on server — scp from laptop:"
  echo "  scp scripts/check_founder_list.py amul-gpu-1:~/docs-pipeline/scripts/"
fi
