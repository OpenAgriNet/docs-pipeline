#!/usr/bin/env bash
# Start Chandra OCR 2 vLLM on the H100 using the existing /amulpfsdata install.
# GPUs 0-5 are occupied by TranslateGemma + Qwen; this targets GPU 6 by default.
set -euo pipefail

OCR_BENCH_ROOT=/amulpfsdata/models/ocr-benchmark
CHANDRA_PORT="${CHANDRA_PORT:-8010}"
CHANDRA_GPU="${CHANDRA_GPU:-6}"
MODEL_SNAPSHOT="${OCR_BENCH_ROOT}/hf-cache/hub/models--datalab-to--chandra-ocr-2/snapshots/808e4613421aad847f44b9383e49201fb8dd1175"

export HF_HOME="${OCR_BENCH_ROOT}/hf-cache"
export HF_HUB_CACHE="${OCR_BENCH_ROOT}/hf-cache/hub"
export VLLM_GPUS="${CHANDRA_GPU}"
export VLLM_MODEL_NAME=chandra
export MODEL_CHECKPOINT="${MODEL_SNAPSHOT}"

echo "Chandra model: ${MODEL_CHECKPOINT}"
echo "GPU: ${CHANDRA_GPU}, port: ${CHANDRA_PORT}"

docker run -d --name chandra-ocr-vllm --restart unless-stopped \
  --runtime nvidia \
  --gpus "device=${CHANDRA_GPU}" \
  -v "${MODEL_SNAPSHOT}:/model:ro" \
  -p "${CHANDRA_PORT}:8000" \
  --ipc=host \
  vllm/vllm-openai:v0.17.0 \
  --model /model \
  --no-enforce-eager \
  --max-num-seqs 64 \
  --dtype bfloat16 \
  --max-model-len 18000 \
  --max_num_batched_tokens 8192 \
  --gpu-memory-utilization 0.85 \
  --enable-prefix-caching \
  --mm-processor-kwargs '{"min_pixels": 3136, "max_pixels": 6291456}' \
  --served-model-name chandra
