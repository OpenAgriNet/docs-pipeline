#!/usr/bin/env bash
# Build prod images on a machine with good network (e.g. laptop),
# export a gzipped tarball for air-gapped / slow-apk servers.
#
# Usage (from repo root, on Apple Silicon use amd64 for Linux servers):
#   ./scripts/export-prod-images.sh
#   scp dist/docs-pipeline-images.tar.gz user@server:~/docs-pipeline/
#
# On server:
#   gunzip -c docs-pipeline-images.tar.gz | docker load
#   NO_BUILD=1 ./scripts/deploy-compose.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
OUT_DIR="${OUT_DIR:-dist}"
OUT_FILE="${OUT_FILE:-$OUT_DIR/docs-pipeline-images.tar.gz}"
PLATFORM="${PLATFORM:-linux/amd64}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.example and fill VITE_* / secrets."
  exit 1
fi

mkdir -p "$OUT_DIR"

echo "==> Building custom images for $PLATFORM"
export DOCKER_DEFAULT_PLATFORM="$PLATFORM"

# Prefer buildx when available (needed for cross-arch from Apple Silicon)
if docker buildx version >/dev/null 2>&1; then
  echo "==> Using docker buildx"
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a

  docker buildx build --platform "$PLATFORM" \
    -t docs-pipeline-lang-detect:latest \
    -f lang-detect/Dockerfile ./lang-detect --load

  docker buildx build --platform "$PLATFORM" \
    -t docs-pipeline-api:latest \
    -t docs-pipeline-worker:latest \
    -f Dockerfile . --load

  docker buildx build --platform "$PLATFORM" \
    -t docs-pipeline-ui:latest \
    -f ui/Dockerfile.prod \
    --build-arg "VITE_API_BASE=${VITE_API_BASE:-/docs-pipeline-api}" \
    --build-arg "VITE_BASE=${VITE_BASE:-/docs-pipeline/}" \
    --build-arg "VITE_KEYCLOAK_URL=${VITE_KEYCLOAK_URL:-https://auth-vistaar.da.gov.in/auth}" \
    --build-arg "VITE_KEYCLOAK_REALM=${VITE_KEYCLOAK_REALM:-bharat-vistaar}" \
    --build-arg "VITE_KEYCLOAK_CLIENT_ID=${VITE_KEYCLOAK_CLIENT_ID:-bharat-vistaar}" \
    --build-arg "VITE_AUTH_ENABLED=${VITE_AUTH_ENABLED:-true}" \
    ./ui --load
else
  echo "==> Using docker compose build (DOCKER_DEFAULT_PLATFORM=$PLATFORM)"
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    lang-detect api worker ui
  # Ensure worker tag exists (same image as api)
  docker tag docs-pipeline-api:latest docs-pipeline-worker:latest 2>/dev/null || true
fi

echo "==> Saving → $OUT_FILE"
docker save \
  docs-pipeline-lang-detect:latest \
  docs-pipeline-api:latest \
  docs-pipeline-worker:latest \
  docs-pipeline-ui:latest \
  | gzip -1 > "$OUT_FILE"

ls -lh "$OUT_FILE"
echo
echo "Copy to server, then:"
echo "  gunzip -c docs-pipeline-images.tar.gz | docker load"
echo "  NO_BUILD=1 ./scripts/deploy-compose.sh"
