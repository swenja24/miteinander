#!/bin/sh
set -eu
umask 077

BACKUP_DIR=${1:-./backups}
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="miteinander-$STAMP.tgz"
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

mkdir -p "$BACKUP_DIR"
docker compose exec -T database pg_dump -U miteinander -d miteinander -Fc > "$TMP_DIR/database.dump"
docker compose exec -T miteinander tar czf - -C /app/data . > "$TMP_DIR/files.tgz"
tar czf "$BACKUP_DIR/$ARCHIVE" -C "$TMP_DIR" database.dump files.tgz
(cd "$BACKUP_DIR" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")
echo "Backup erstellt: $BACKUP_DIR/$ARCHIVE"
