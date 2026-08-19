#!/usr/bin/env bash
# One-command docker compose deploy for registry / sandbox hosts.
#
# Usage (on the server, from the repo root):
#   ./scripts/deploy-compose.sh            # build + up
#   NO_BUILD=1 ./scripts/deploy-compose.sh # skip build (use preloaded images)
#
# Host port defaults (override in .env):
#   API_HOST_PORT=8011   UI_HOST_PORT=3011
#   KEYCLOAK_HOST_PORT=8081   TEMPORAL_UI_PORT=8090
#
# Required nginx snippet locations (see deploy/):
#   /docs-pipeline-api/ → 127.0.0.1:8011
#   /docs-pipeline/     → 127.0.0.1:3011
#   /auth/              → 127.0.0.1:8081   (Keycloak)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found — copy from .env.example and fill all secrets."
  exit 1
fi

# Load env so we can read port values for health checks below
set -a; source "$ENV_FILE"; set +a

API_PORT="${API_HOST_PORT:-8011}"
UI_PORT="${UI_HOST_PORT:-3011}"
KC_PORT="${KEYCLOAK_HOST_PORT:-8081}"

BUILD_FLAG=(--build)
if [[ "${NO_BUILD:-}" == "1" ]]; then
  BUILD_FLAG=()
fi

echo "===> compose file : $COMPOSE_FILE"
echo "===> env file     : $ENV_FILE"
echo "===> build flag   : ${BUILD_FLAG[*]:-<skipped>}"
echo ""

# ── 1. Pull images that are not built locally ─────────────────────────────────
echo "===> Pulling base images..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull \
  temporal temporal-db temporal-ui minio minio-init keycloak-db 2>/dev/null || true

# ── 2. Build application images ───────────────────────────────────────────────
if [[ ${#BUILD_FLAG[@]} -gt 0 ]]; then
  echo "===> Building application images..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    --no-cache keycloak lang-detect api worker ui
fi

# ── 3. Bring the stack up ─────────────────────────────────────────────────────
echo "===> Starting stack (detached)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

# ── 4. Status ─────────────────────────────────────────────────────────────────
echo ""
echo "===> Container status:"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# ── 5. Health checks ─────────────────────────────────────────────────────────
echo ""
echo "===> Waiting 10s for services to initialise..."
sleep 10

echo -n "  API     : "; curl -sf "http://127.0.0.1:${API_PORT}/health" && echo "OK" || echo "FAIL (may still be starting)"
echo -n "  UI      : "; curl -sf --max-time 3 "http://127.0.0.1:${UI_PORT}/" -o /dev/null && echo "OK" || echo "FAIL (may still be starting)"
echo -n "  Keycloak: "; curl -sf --max-time 5 "http://127.0.0.1:${KC_PORT}/health/ready" && echo "OK" || echo "FAIL (may still be starting — takes ~60s on first boot)"

# ── 6. Summary ────────────────────────────────────────────────────────────────
cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Deployed  (localhost binds — sit behind nginx)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 API       http://127.0.0.1:${API_PORT}/health
 UI        http://127.0.0.1:${UI_PORT}/
 Keycloak  http://127.0.0.1:${KC_PORT}/
           http://127.0.0.1:${KC_PORT}/admin/knowledge-engine/console/
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Public URLs (after nginx — see deploy/ snippets)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 https://<your-domain>/docs-pipeline-api/health
 https://<your-domain>/docs-pipeline/
 https://<your-domain>/auth/realms/knowledge-engine/.well-known/openid-configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Logs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs -f api worker keycloak
EOF
