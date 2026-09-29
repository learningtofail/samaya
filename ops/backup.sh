#!/usr/bin/env bash
# Nightly Postgres backup — spec §30. Runs the exact same `pg_dump` the
# deployment runbook has you run by hand before a schema migration
# (docker compose exec db pg_dump ...), on a schedule, with rotation —
# so a bad migration or a disk failure isn't a total-loss event just
# because nobody remembered to dump first that particular day.
#
# Runs on the HOST (the LXC), not inside the `app` container — `app`'s
# image (python:3.13-slim) has no pg_dump, and there's no reason to add
# one when the `db` container (postgres:16-alpine) already has it and is
# already reachable via `docker compose exec`.
#
# Install (one-time, on lxc-taraka):
#   chmod +x /opt/taraka/ops/backup.sh
#   crontab -e
#   # add a line (runs daily at 03:00 server time):
#   0 3 * * * /opt/taraka/ops/backup.sh >> /opt/taraka/backups/backup.log 2>&1
#
# Off-box copy: this script only writes to local disk (a backup sitting
# next to the database it backs up doesn't survive a disk failure). If
# you want an off-box copy, add an rclone/rsync/restic step after the
# dump completes — deliberately left out here since it depends on where
# you want backups to land (see spec §30.3, Out of Scope).

set -euo pipefail

COMPOSE_DIR="/opt/taraka"
BACKUP_DIR="${COMPOSE_DIR}/backups"
KEEP_DAYS=14
DB_NAME="kingshot_scheduler"
DB_USER="taraka"

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d_%H%M%S)"
outfile="${BACKUP_DIR}/kingshot_scheduler_${timestamp}.sql.gz"

cd "$COMPOSE_DIR"
echo "[$(date -Is)] Starting backup -> ${outfile}"
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$outfile"
echo "[$(date -Is)] Backup complete: $(du -h "$outfile" | cut -f1)"

# Rotation — delete dumps older than KEEP_DAYS. find's -mtime is in whole
# days, so this keeps roughly the last two weeks of nightly backups.
find "$BACKUP_DIR" -name 'kingshot_scheduler_*.sql.gz' -mtime "+${KEEP_DAYS}" -print -delete
echo "[$(date -Is)] Rotation complete — keeping backups from the last ${KEEP_DAYS} days"
