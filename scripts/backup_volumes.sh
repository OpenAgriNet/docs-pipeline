#!/usr/bin/env bash
set -euo pipefail

# Backup all persistent data volumes used by the docs pipeline:
# - Temporal Postgres database
# - MinIO object storage
# - SQLite documents/chunks DB
# - Marqo Vespa backend data (/opt/vespa/var)
#
# Usage:
#   ./scripts/backup_volumes.sh                # writes to ./backups
#   ./scripts/backup_volumes.sh my-backups    # writes to ./my-backups
#
# NOTES:
# - For a fully consistent snapshot, stop the stack first:
#     cd deployment/docs-pipeline
#     docker compose stop
# - This script auto-detects the actual Docker volume names based on suffixes.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="${1:-backups}"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

backup_volume() {
  local logical_name="$1"
  local suffix="$2"

  # Find the actual docker volume whose name ends with the given suffix
  local vol_name
  vol_name="$(docker volume ls --format '{{.Name}}' | grep "${suffix}$" || true)"

  if [[ -z "$vol_name" ]]; then
    echo "WARNING: volume for ${logical_name} (*${suffix}) not found; skipping" >&2
    return 0
  fi

  echo "Backing up ${logical_name} volume: ${vol_name}"
  docker run --rm \
    -v "${vol_name}":/from \
    -v "${ROOT_DIR}/${BACKUP_DIR}":/backup \
    alpine sh -c "cd /from && tar czf /backup/${vol_name}-${TIMESTAMP}.tar.gz ."
}

echo "Backing up docs-pipeline volumes into ${BACKUP_DIR} (timestamp: ${TIMESTAMP})"

# 1) Temporal Postgres DB
backup_volume "temporal-db" "temporal-db-data"

# 2) MinIO object storage (all PDFs / objects)
backup_volume "minio" "minio-data"

# 3) SQLite documents/chunks DB
backup_volume "sqlite" "sqlite-data"

# 4) Marqo Vespa backend at /opt/vespa/var
echo "Detecting Marqo Vespa volume (/opt/vespa/var)…"

# Try to find the marqo container started by this docker-compose stack
MARQO_CONTAINER="$(docker ps -a --filter 'name=docs-pipeline-marqo' --format '{{.ID}}' | head -n 1 || true)"

if [[ -z "$MARQO_CONTAINER" ]]; then
  echo "WARNING: marqo container matching 'docs-pipeline-marqo' not found; skipping Vespa backup" >&2
else
  # Use docker inspect to find the volume mounted at /opt/vespa/var
  VESPA_VOL="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/vespa/var"}}{{.Name}}{{end}}{{end}}' "$MARQO_CONTAINER" || true)"

  if [[ -z "$VESPA_VOL" ]]; then
    echo "WARNING: no /opt/vespa/var volume found on container ${MARQO_CONTAINER}; skipping Vespa backup" >&2
  else
    echo "Backing up Marqo Vespa volume: ${VESPA_VOL}"
    docker run --rm \
      -v "${VESPA_VOL}":/from \
      -v "${ROOT_DIR}/${BACKUP_DIR}":/backup \
      alpine sh -c "cd /from && tar czf /backup/marqo-vespa-var-backup-${TIMESTAMP}.tar.gz ."
  fi
fi

echo
echo "Backup complete. Files in ${BACKUP_DIR}:"
ls -lh "${BACKUP_DIR}"

