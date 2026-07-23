#!/usr/bin/env bash
# One-command docker compose deploy for registry / sandbox hosts.
#
# Default localhost binds (override in .env):
#   API_HOST_PORT=8011   UI_HOST_PORT=3011   TEMPORAL_UI_PORT=8090
# Host nginx must proxy:
#   /docs-pipeline-api/ → 127.0.0.1:8011
#   /docs-pipeline/     → 127.0.0.1:3011
#
# Skip image build when using preloaded images:
#   NO_BUILD=1 ./scripts/deploy-compose.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.example and fill secrets."
  exit 1
fi

BUILD_FLAG=(--build)
if [[ "${NO_BUILD:-}" == "1" ]]; then
  BUILD_FLAG=(--no-build)
fi

echo "==> Using $COMPOSE_FILE + $ENV_FILE ${BUILD_FLAG[*]}"
echo "==> Starting stack..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d "${BUILD_FLAG[@]}"

echo "==> Status"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

echo "==> Health"
sleep 3
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
API_PORT="${API_HOST_PORT:-8011}"
UI_PORT="${UI_HOST_PORT:-3011}"

curl -sf "http://127.0.0.1:${API_PORT}/health" && echo || echo "API health check failed"
curl -sf "http://127.0.0.1:${UI_PORT}/health" && echo || echo "UI health check failed"

cat <<EOF

Deployed (localhost binds for host nginx):
  API:  http://127.0.0.1:${API_PORT}/health
  UI:   http://127.0.0.1:${UI_PORT}/

Public (after nginx snippet in deploy/nginx-docs-pipeline.snippet.conf):
  https://registry-sandbox-vistaar.da.gov.in/docs-pipeline/
  https://registry-sandbox-vistaar.da.gov.in/docs-pipeline-api/health

Logs:
  docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs -f api worker ui
EOF
