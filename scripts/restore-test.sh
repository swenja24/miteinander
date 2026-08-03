#!/bin/sh
set -eu

ARCHIVE=${1:?Bitte den Pfad zu einem Backup angeben.}
if [ -f "$ARCHIVE.sha256" ]; then (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256"); fi
TEST_VOLUME="miteinander_restore_test_$$"
cleanup() { docker volume rm "$TEST_VOLUME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker volume create "$TEST_VOLUME" >/dev/null
docker run --rm -v "$TEST_VOLUME:/data" -v "$(cd "$(dirname "$ARCHIVE")" && pwd):/backup:ro" alpine \
  tar xzf "/backup/$(basename "$ARCHIVE")" -C /data
docker run --rm -v "$TEST_VOLUME:/data:ro" alpine test -s /data/familie.json
echo "Wiederherstellungstest erfolgreich; familie.json ist vorhanden."
