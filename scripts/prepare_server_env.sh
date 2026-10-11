#!/usr/bin/env sh
set -eu
umask 077
if [ -e .env ]; then
  echo ".env already exists; refusing to overwrite it" >&2
  exit 1
fi
cp .env.server.example .env
secret="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
sed "s/REPLACE_WITH_RANDOM_SECRET/$secret/" .env > .env.tmp
mv .env.tmp .env
chmod 600 .env
echo "Created .env with a random database password. Review settings before starting services."
