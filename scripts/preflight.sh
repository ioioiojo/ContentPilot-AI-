#!/usr/bin/env sh
set -eu
test -f .env || { echo "Create .env with prepare_server_env.sh first" >&2; exit 1; }
command -v docker >/dev/null
command -v curl >/dev/null
command -v flock >/dev/null
# Never print the expanded Compose configuration (it contains secrets).
docker compose config --quiet
docker compose run --rm --no-deps web python -m contentpilot_agent.manage check-config
echo "Configuration accepted. This does not validate provider credentials or server capacity."
