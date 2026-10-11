#!/usr/bin/env sh
set -eu
umask 077
mkdir -p backups
file="backups/contentpilot-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
set -C
trap 'rm -f "$file.tmp" "$file.key.tmp"' EXIT HUP INT TERM
docker compose exec -T db sh -c 'pg_dump --format=custom -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$file.tmp"
test -s "$file.tmp"
# Keep the key with the encrypted configuration backup; do not print it.
# Export verifies that the key decrypts all persisted service configurations.
docker compose run --rm --no-deps -T web python -m contentpilot_agent.config_backup export > "$file.key.tmp"
if [ -s "$file.key.tmp" ]; then
  mv "$file.key.tmp" "$file.key"
fi
mv "$file.tmp" "$file"
echo "Backup created: $file (custom pg_dump format)"
# No automatic deletion: review and rotate backups with your own retention policy.
