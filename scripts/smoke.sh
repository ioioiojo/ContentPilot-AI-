#!/usr/bin/env sh
set -eu
attempt=0
while [ "$attempt" -lt 60 ]; do
  if curl -fsS --max-time 3 http://127.0.0.1:18080/health >/dev/null 2>&1 &&
     curl -fsS --max-time 3 http://127.0.0.1:18080/ready >/dev/null 2>&1; then
    echo "Database and worker ready. Verify HTTPS login and a real task before launch."
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 2
done
echo "Readiness failed. Inspect: docker compose ps; docker compose logs --tail=100 web worker" >&2
exit 1
