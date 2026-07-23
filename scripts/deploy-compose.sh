#!/usr/bin/env bash
# One-command docker compose deploy for registry / sandbox hosts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.example and fill secrets."
  exit 1
fi

echo "==> Using $COMPOSE_FILE + $ENV_FILE"
echo "==> Building & starting stack..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build

echo "==> Status"
docker compose -f "$COMPOSE_FILE" ps

echo "==> Health"
sleep 3
curl -sf http://127.0.0.1:8001/health && echo || echo "API health check failed"
curl -sf http://127.0.0.1:3011/health && echo || echo "UI health check failed"

cat <<EOF

Deployed (localhost binds for host nginx):
  API:  http://127.0.0.1:8001/health
  UI:   http://127.0.0.1:3011/

Public (after nginx snippet):
  https://registry-sandbox-vistaar.da.gov.in/docs-pipeline/
  https://registry-sandbox-vistaar.da.gov.in/docs-pipeline-api/health

Logs:
  docker compose -f $COMPOSE_FILE logs -f api worker ui
EOF
