#!/usr/bin/env bash
# Daily backup of SQLite DB + customer uploads.
# Installed to /etc/cron.daily/partsmall-backup by install.sh.
#
# Keeps last 14 days locally. For off-site, sync /opt/partsmall/backups
# to S3/R2/Backblaze separately (TODO).

set -euo pipefail

APP_DIR="/opt/partsmall"
BACKUP_DIR="${APP_DIR}/backups"
RETAIN_DAYS=14

mkdir -p "${BACKUP_DIR}"

DATE=$(date +%Y%m%d-%H%M%S)
ARCHIVE="${BACKUP_DIR}/partsmall-${DATE}.tar.gz"

# Use SQLite .backup so we get a consistent snapshot even if app is writing
SNAPSHOT="${BACKUP_DIR}/partsmall-${DATE}.db"
sqlite3 "${APP_DIR}/data/partsmall.db" ".backup '${SNAPSHOT}'"

tar -czf "${ARCHIVE}" \
    -C "${APP_DIR}" \
    --transform "s,^backups/partsmall-${DATE}.db,db/partsmall.db," \
    "backups/partsmall-${DATE}.db" \
    uploads \
    generated 2>/dev/null || true

rm -f "${SNAPSHOT}"

# Prune old backups
find "${BACKUP_DIR}" -name 'partsmall-*.tar.gz' -mtime +${RETAIN_DAYS} -delete

echo "Backup OK: ${ARCHIVE}"
