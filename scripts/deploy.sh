#!/usr/bin/env sh
set -eu
umask 077
case "${1:-}" in
  first|upgrade) ;;
  *) echo "Usage: sh scripts/deploy.sh first|upgrade (run from project directory)" >&2; exit 2 ;;
esac
command -v flock >/dev/null
exec 9>.contentpilot-deploy.lock
flock -n 9 || { echo "Another maintenance operation is running" >&2; exit 1; }
docker compose config --quiet
docker compose build --pull web
sh scripts/preflight.sh
docker compose up -d --wait db
exists=$(docker compose exec -T db sh -c 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_version' AND table_schema = 'public';
SQL
)
if [ "$1" = first ] && [ "$exists" != 0 ]; then
  echo "Database already initialized; use upgrade" >&2
  exit 1
fi
if [ "$1" = upgrade ]; then
  test "$exists" = 1 || { echo "No initialized database; use first" >&2; exit 1; }
  # Stop writers BEFORE backup. If backup/migration fails, leave them stopped.
  docker compose stop web worker
  sh scripts/backup.sh
fi
docker compose run --rm --no-deps web python -m contentpilot_agent.manage init-db
docker compose up -d web worker
sh scripts/smoke.sh
echo "Deployment candidate started. Complete deployment/LAUNCH.md acceptance."
