#!/bin/sh
set -eu

BACKUP_DIR=${1:-./backups}
VOLUME=${MITEINANDER_VOLUME:-miteinander_data}
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
docker run --rm -v "$VOLUME:/data:ro" -v "$(cd "$BACKUP_DIR" && pwd):/backup" alpine \
  tar czf "/backup/miteinander-$STAMP.tgz" -C /data .
(cd "$BACKUP_DIR" && sha256sum "miteinander-$STAMP.tgz" > "miteinander-$STAMP.tgz.sha256")
echo "Backup erstellt: $BACKUP_DIR/miteinander-$STAMP.tgz"
