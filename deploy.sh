#!/usr/bin/env bash
set -Eeuo pipefail

SSH_TARGET="${SSH_TARGET:-manabi}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/nhg-manabi}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%dT%H%M%S)"

echo "[1/5] Checking the nhg-manabi deployment"
ssh "$SSH_TARGET" "test -f '$REMOTE_ROOT/.env' && test -f '$REMOTE_ROOT/compose.yml'"

echo "[2/5] Backing up PostgreSQL"
ssh "$SSH_TARGET" "mkdir -p '$REMOTE_ROOT/backups' && cd '$REMOTE_ROOT' && docker compose exec -T db pg_dump -U nhg_manabi -d nhg_manabi -Fc > 'backups/database-$STAMP.dump'"

echo "[3/5] Uploading API source"
rsync -az --delete \
  --exclude '.DS_Store' --exclude '._*' --exclude '__pycache__' --exclude '*.pyc' \
  "$SCRIPT_DIR/app/" "$SSH_TARGET:$REMOTE_ROOT/api/app/"
rsync -az --delete \
  --exclude '.DS_Store' --exclude '._*' --exclude '__pycache__' --exclude '*.pyc' \
  "$SCRIPT_DIR/scripts/" "$SSH_TARGET:$REMOTE_ROOT/api/scripts/"
rsync -az --delete --exclude '.DS_Store' --exclude '._*' \
  "$SCRIPT_DIR/import_data/" "$SSH_TARGET:$REMOTE_ROOT/api/import_data/"
rsync -az "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR/requirements.txt" \
  "$SSH_TARGET:$REMOTE_ROOT/api/"
rsync -az "$SCRIPT_DIR/deploy/compose.yml" "$SSH_TARGET:$REMOTE_ROOT/compose.yml"

echo "[4/5] Rebuilding the API"
ssh "$SSH_TARGET" "cd '$REMOTE_ROOT' && docker compose up -d --build --remove-orphans api"

echo "[5/5] Verifying health"
ssh "$SSH_TARGET" "for i in \$(seq 1 30); do curl -fsS http://127.0.0.1:8002/api/health && exit 0; sleep 2; done; docker compose logs --tail=100 api; exit 1"
echo
echo "API deployed successfully. Backup: $REMOTE_ROOT/backups/database-$STAMP.dump"
