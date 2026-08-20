#!/usr/bin/env bash
# Build prod artifacts (optional) and SCP them to the dev server.
#
# Usage (from repo root):
#   ./scripts/transfer-prod-to-dev.sh                 # rebuild images + transfer
#   SKIP_BUILD=1 ./scripts/transfer-prod-to-dev.sh    # transfer existing dist/ only
#   BUILD_CONFIG_ONLY=1 ./scripts/transfer-prod-to-dev.sh  # refresh config zip only
#
# Credentials (priority order):
#   1) env vars: DEV_HOST DEV_PORT DEV_USER DEV_SSH_PASSWORD REMOTE_DIR
#   2) repo-root .deploy-credentials  (gitignored — never commit)
#
# Requires: docker (if building), sshpass
#   macOS:  brew install hudochenkov/sshpass/sshpass
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CRED_FILE="${CRED_FILE:-$ROOT/.deploy-credentials}"
if [[ -f "$CRED_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  source "$CRED_FILE"
  set +a
fi

DEV_HOST="${DEV_HOST:-10.128.188.3}"
DEV_PORT="${DEV_PORT:-9822}"
DEV_USER="${DEV_USER:-akshat}"
# Path under the remote user's home (no leading ~ — avoids Mac-side expansion).
REMOTE_DIR="${REMOTE_DIR:-docs-pipeline}"
REMOTE_DIR="${REMOTE_DIR#~/}"          # normalize ~/foo → foo
REMOTE_DIR="${REMOTE_DIR#/home/*/}"    # tolerate accidental absolute home paths poorly; prefer relative
# If someone passed a full absolute path, keep it.
if [[ "$REMOTE_DIR" == /* ]]; then
  :
else
  # relative to remote $HOME
  :
fi
DEV_SSH_PASSWORD="${DEV_SSH_PASSWORD:-}"

IMAGES_OUT="${IMAGES_OUT:-$ROOT/dist/docs-pipeline-images.tar.gz}"
CONFIG_ZIP="${CONFIG_ZIP:-$ROOT/dist/docs-pipeline-deploy-config.zip}"

if [[ -z "$DEV_SSH_PASSWORD" ]]; then
  echo "ERROR: DEV_SSH_PASSWORD is not set."
  echo "Create $CRED_FILE (gitignored) or export DEV_SSH_PASSWORD."
  exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "ERROR: sshpass is required for password-based SCP."
  echo "  macOS:  brew install hudochenkov/sshpass/sshpass"
  echo "  Ubuntu: sudo apt-get install -y sshpass"
  exit 1
fi

build_config_zip() {
  echo "==> Packaging deploy config → $CONFIG_ZIP"
  local stage="$ROOT/dist/deploy-bundle"
  rm -rf "$stage"
  mkdir -p "$stage/scripts" "$stage/deploy"
  cp "$ROOT/docker-compose.prod.yml" "$stage/"
  cp "$ROOT/.env.example" "$stage/"
  cp "$ROOT/scripts/deploy-compose.sh" "$stage/scripts/"
  cp "$ROOT/scripts/export-prod-images.sh" "$stage/scripts/"
  cp "$ROOT/deploy/nginx-docs-pipeline.snippet.conf" "$stage/deploy/"
  chmod +x "$stage/scripts/"*.sh
  mkdir -p "$ROOT/dist"
  (
    cd "$ROOT/dist"
    rm -f docs-pipeline-deploy-config.zip
    zip -qr docs-pipeline-deploy-config.zip deploy-bundle
  )
  ls -lh "$CONFIG_ZIP"
}

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  if [[ "${BUILD_CONFIG_ONLY:-0}" == "1" ]]; then
    build_config_zip
  else
    echo "==> Building prod images (linux/amd64)…"
    if [[ ! -f "$ROOT/.env" ]]; then
      echo "ERROR: missing $ROOT/.env (needed for UI build args). Copy from .env.example."
      exit 1
    fi
    "$ROOT/scripts/export-prod-images.sh"
    build_config_zip
  fi
else
  echo "==> SKIP_BUILD=1 — using existing artifacts"
  if [[ ! -f "$CONFIG_ZIP" ]]; then
    build_config_zip
  fi
fi

if [[ ! -f "$IMAGES_OUT" ]]; then
  echo "ERROR: missing image tarball: $IMAGES_OUT"
  echo "Run without SKIP_BUILD, or: ./scripts/export-prod-images.sh"
  exit 1
fi
if [[ ! -f "$CONFIG_ZIP" ]]; then
  echo "ERROR: missing config archive: $CONFIG_ZIP"
  exit 1
fi

echo "==> Artifacts"
ls -lh "$IMAGES_OUT" "$CONFIG_ZIP"

export SSHPASS="$DEV_SSH_PASSWORD"
# This host uses keyboard-interactive for SSH password auth.
SSH_OPTS=(
  -o StrictHostKeyChecking=accept-new
  -o PreferredAuthentications=keyboard-interactive,password
  -o PubkeyAuthentication=no
  -o NumberOfPasswordPrompts=1
  -o ConnectTimeout=30
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=120
)
SSH_BASE=(sshpass -e ssh -p "$DEV_PORT" "${SSH_OPTS[@]}")
SCP_BASE=(sshpass -e scp -P "$DEV_PORT" "${SSH_OPTS[@]}")

REMOTE="${DEV_USER}@${DEV_HOST}"

echo "==> Resolving remote path under home for: $REMOTE_DIR"
if [[ "$REMOTE_DIR" == /* ]]; then
  REMOTE_ABS="$REMOTE_DIR"
else
  REMOTE_HOME="$("${SSH_BASE[@]}" "$REMOTE" 'printf %s "$HOME"')"
  REMOTE_ABS="${REMOTE_HOME%/}/${REMOTE_DIR}"
fi
echo "==> Remote dir: $REMOTE_ABS"

"${SSH_BASE[@]}" "$REMOTE" "mkdir -p '$REMOTE_ABS'"

# Always remove incomplete image tarball before upload (avoids "unexpected EOF" on docker load)
if [[ "${REMOTE_CLEAN:-1}" == "1" ]]; then
  echo "==> Removing any partial remote image tarball"
  "${SSH_BASE[@]}" "$REMOTE" "rm -f '$REMOTE_ABS/docs-pipeline-images.tar.gz'"
fi

# Config first (tiny) so deploy scripts exist even if images take long
echo "==> Uploading config zip → ${REMOTE}:${REMOTE_ABS}/"
"${SCP_BASE[@]}" "$CONFIG_ZIP" "${REMOTE}:${REMOTE_ABS}/docs-pipeline-deploy-config.zip"

echo "==> Uploading images (~3GB) → ${REMOTE}:${REMOTE_ABS}/  (do not interrupt)"
"${SCP_BASE[@]}" "$IMAGES_OUT" "${REMOTE}:${REMOTE_ABS}/docs-pipeline-images.tar.gz"

echo "==> Verifying remote sizes match local"
LOCAL_IMG_SIZE=$(stat -f%z "$IMAGES_OUT" 2>/dev/null || stat -c%s "$IMAGES_OUT")
LOCAL_CFG_SIZE=$(stat -f%z "$CONFIG_ZIP" 2>/dev/null || stat -c%s "$CONFIG_ZIP")
REMOTE_SIZES="$("${SSH_BASE[@]}" "$REMOTE" "stat -c%s '$REMOTE_ABS/docs-pipeline-images.tar.gz'; stat -c%s '$REMOTE_ABS/docs-pipeline-deploy-config.zip'")"
REMOTE_IMG_SIZE=$(echo "$REMOTE_SIZES" | sed -n '1p')
REMOTE_CFG_SIZE=$(echo "$REMOTE_SIZES" | sed -n '2p')
echo "  images: local=$LOCAL_IMG_SIZE remote=$REMOTE_IMG_SIZE"
echo "  config: local=$LOCAL_CFG_SIZE remote=$REMOTE_CFG_SIZE"
if [[ "$LOCAL_IMG_SIZE" != "$REMOTE_IMG_SIZE" || "$LOCAL_CFG_SIZE" != "$REMOTE_CFG_SIZE" ]]; then
  echo "ERROR: size mismatch — transfer incomplete. Re-run: SKIP_BUILD=1 ./scripts/transfer-prod-to-dev.sh"
  exit 1
fi
echo "==> Size check OK"

echo
echo "Done. Files on server:"
echo "  ${REMOTE_ABS}/docs-pipeline-images.tar.gz"
echo "  ${REMOTE_ABS}/docs-pipeline-deploy-config.zip"
echo
echo "On the server:"
echo "  ssh -p $DEV_PORT $DEV_USER@$DEV_HOST"
echo "  cd $REMOTE_ABS"
echo "  unzip -o docs-pipeline-deploy-config.zip && cp -rn deploy-bundle/* . 2>/dev/null || true"
echo "  # optional integrity: gzip -t docs-pipeline-images.tar.gz"
echo "  gunzip -c docs-pipeline-images.tar.gz | docker load"
echo "  # create/update .env from .env.example if needed"
echo "  NO_BUILD=1 ./scripts/deploy-compose.sh"
