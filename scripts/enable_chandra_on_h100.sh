#!/usr/bin/env bash
# Run ON chnamulh100s01 (aicloud@10.185.25.197) after pulling latest docs-pipeline code.
set -euo pipefail

REPO="${REPO:-$HOME/docs-pipeline}"
OCR_BENCH="${OCR_BENCH:-$HOME/ocr-benchmark}"
CHANDRA_PORT="${CHANDRA_PORT:-8010}"
CHANDRA_GPU="${CHANDRA_GPU:-6}"
GEMMA_URL="${TRANSLATION_VLLM_BASE_URL:-http://10.185.25.198:8020/v1}"
export TRANSLATION_VLLM_BASE_URL="$GEMMA_URL"
export TRANSLATION_PROVIDER="${TRANSLATION_PROVIDER:-gemma_vllm}"
export TRANSLATION_MODEL="${TRANSLATION_MODEL:-gemma-4-31b-it}"

echo "==> Repo: $REPO"
echo "==> OCR bench: $OCR_BENCH"

if [[ -f "$OCR_BENCH/env.sh" ]]; then
  # shellcheck disable=SC1090
  source "$OCR_BENCH/env.sh"
fi

export HF_HOME="${HF_HOME:-$OCR_BENCH/hf-cache}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
export CHANDRA_HF_PORT="$CHANDRA_PORT"
export CUDA_VISIBLE_DEVICES="$CHANDRA_GPU"

echo "==> Stopping any previous Chandra HF server on :$CHANDRA_PORT"
pkill -f "chandra_hf_server.py" 2>/dev/null || true
sleep 1

PYTHON="$OCR_BENCH/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: $PYTHON not executable. Open an interactive SSH session on the H100 and run: source $OCR_BENCH/env.sh"
  exit 1
fi

echo "==> Starting Chandra HF API on GPU $CHANDRA_GPU port $CHANDRA_PORT"
nohup "$PYTHON" "$REPO/scripts/chandra_hf_server.py" > /tmp/chandra-hf.log 2>&1 &
sleep 10
curl -sf "http://127.0.0.1:${CHANDRA_PORT}/health" && echo

echo "==> Gemma translation endpoint: $GEMMA_URL"
curl -sf "${GEMMA_URL}/models" | python3 -c "import sys,json; d=json.load(sys.stdin); print('models:', [m.get('id') for m in d.get('data',[])])" && echo

echo "==> Rebuilding and restarting pipeline worker"
cd "$REPO"
docker compose build worker
docker compose up -d --no-deps --force-recreate worker

echo "==> Worker OCR env"
docker exec docs-pipeline-worker-1 env | grep -E '^(OCR_|CHANDRA_|TRANSLATION_)' || true

echo "==> Done. OCR-only test: python3 scripts/test_pipeline_e2e.py --mode ocr"
echo "==> Full pipeline test: python3 scripts/test_pipeline_e2e.py --mode full"
echo "==> Gemma probe: python3 scripts/gemma_probe.py"
