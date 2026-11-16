#!/usr/bin/env bash
set -e
BACKUP_DIR="${HILLEYE_BACKUP_DIR:-/var/lib/hilleye/backups}"
DB_PATH="${HILLEYE_DB_PATH:-/var/lib/hilleye/tracker.db}"
LOGS_DB_PATH="${HILLEYE_LOGS_DB_PATH:-$(dirname "${DB_PATH}")/logs.db}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

mkdir -p "${BACKUP_DIR}"
if [ -f "${DB_PATH}" ]; then
  BACKUP_FILE="${BACKUP_DIR}/tracker_${TIMESTAMP}.db"
  sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"
  gzip -f "${BACKUP_FILE}"
fi
if [ -f "${LOGS_DB_PATH}" ]; then
  LOGS_BACKUP_FILE="${BACKUP_DIR}/logs_${TIMESTAMP}.db"
  sqlite3 "${LOGS_DB_PATH}" ".backup '${LOGS_BACKUP_FILE}'"
  gzip -f "${LOGS_BACKUP_FILE}"
fi
find "${BACKUP_DIR}" -name "*.db.gz" -mtime +7 -delete
