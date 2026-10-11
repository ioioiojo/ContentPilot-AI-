#!/usr/bin/env sh
set -eu
# Read-only probes. Use your monitoring system to alert on nonzero exit.
curl -fsS --max-time 5 http://127.0.0.1:18080/ready >/dev/null || {
  echo "ALERT: ContentPilot database or worker unavailable" >&2; exit 1;
}
test -d backups || { echo "ALERT: no backups directory" >&2; exit 1; }
recent=$(find backups -maxdepth 1 -type f -name '*.dump' -mmin -1560 -print -quit)
test -n "$recent" || { echo "ALERT: no backup in the last 26 hours" >&2; exit 1; }
echo "ContentPilot readiness and backup age OK"
