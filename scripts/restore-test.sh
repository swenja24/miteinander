#!/bin/sh
set -eu

ARCHIVE=${1:?Bitte den Pfad zu einem Backup angeben.}
TMP_DIR=$(mktemp -d)
CONTAINER="miteinander-restore-test-$$"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ -f "$ARCHIVE.sha256" ]; then
  (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256")
fi
tar xzf "$ARCHIVE" -C "$TMP_DIR"
test -s "$TMP_DIR/database.dump"
test -s "$TMP_DIR/files.tgz"
tar tzf "$TMP_DIR/files.tgz" >/dev/null

docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=restore-test -v "$TMP_DIR:/backup:ro" postgres:17-alpine >/dev/null
attempt=0
until docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "Der temporäre PostgreSQL-Server wurde nicht rechtzeitig bereit." >&2
    exit 1
  fi
  sleep 1
done
docker exec "$CONTAINER" createdb -U postgres restore_test
docker exec "$CONTAINER" pg_restore -U postgres -d restore_test --no-owner --exit-on-error /backup/database.dump
docker exec "$CONTAINER" psql -U postgres -d restore_test -Atc "SELECT COUNT(*) FROM core_workspace" >/dev/null
echo "Wiederherstellungstest erfolgreich: Datenbank und Dateiarchiv sind lesbar."
