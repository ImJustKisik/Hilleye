#!/usr/bin/env bash
set -e
BACKUP_DIR="/opt/Hilleye/backups"
DB_PATH="/opt/Hilleye/tracker.db"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/tracker_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"
gzip -f "${BACKUP_FILE}"
find "${BACKUP_DIR}" -name "tracker_*.db.gz" -mtime +7 -delete
