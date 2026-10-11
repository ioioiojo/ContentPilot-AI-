#!/usr/bin/env sh
set -eu
if [ "${1:-}" != "--confirm-replace-database" ] || [ -z "${2:-}" ] || [ ! -s "$2" ]; then
  echo "Usage: sh scripts/restore.sh --confirm-replace-database backups/contentpilot-TIMESTAMP.dump [--keep-stopped]" >&2
  echo "This replaces the current database; create a fresh backup first." >&2
  exit 2
fi
if [ "${3:-}" != "" ] && [ "$3" != "--keep-stopped" ]; then
  echo "Unknown option; use --keep-stopped for code rollback" >&2
  exit 2
fi
# Validate the archive before stopping services; refuse untrusted backup files.
command -v flock > /dev/null
exec 9>.contentpilot-deploy.lock
flock -n 9 || { echo "Another maintenance operation is running" >&2; exit 1; }
docker compose exec -T db pg_restore --list < "$2" > /dev/null
docker compose stop web worker
# Keep a recoverable snapshot of the database being replaced.
sh scripts/backup.sh
if docker compose exec -T db sh -c 'pg_restore --clean --if-exists --single-transaction --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$2"; then
  if [ -s "$2.key" ]; then
    docker compose run --rm --no-deps -T web python -m contentpilot_agent.config_backup restore < "$2.key"
  fi
  if [ "${3:-}" = "--keep-stopped" ]; then
    echo "Database restored. Services remain stopped; select matching code/image before starting."
    exit 0
  fi
  docker compose run --rm --no-deps -T web python -m contentpilot_agent.config_backup check
  docker compose up -d web worker
  sh scripts/smoke.sh
  echo "Restore complete. Verify HTTPS login and content."
else
  echo "Restore failed; transaction rolled back. Services remain stopped for inspection." >&2
  exit 1
fi
