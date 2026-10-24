#!/usr/bin/env bash
# Start Casefile's sources database and put it on Zetaris's Docker network,
# so Zetaris reaches it as host "casefile-sources" (port 5432).
#
# Usage:  bash scripts/codespace/connect_sources.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

docker compose -f infra/docker-compose.sources.yml up -d
echo "Waiting for the sources database ..."
for i in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' casefile-sources 2>/dev/null)" = "healthy" ] && break
  sleep 2
done

DRIVER="$(docker ps --format '{{.Names}}' | grep -m1 -i 'driver' || true)"
if [ -z "$DRIVER" ]; then
  echo "Zetaris doesn't seem to be running (no driver container). Run scripts/codespace/start_zetaris.sh first."
  exit 1
fi
NETWORK="$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$DRIVER" | awk '{print $1}')"
docker network connect "$NETWORK" casefile-sources 2>/dev/null || echo "(already on $NETWORK)"
echo "casefile-sources is on Zetaris's network: $NETWORK"

# Tell Casefile how Zetaris reaches the database.
touch .env
if grep -q '^ZETARIS_PG_HOST=' .env; then
  sed -i 's|^ZETARIS_PG_HOST=.*|ZETARIS_PG_HOST=casefile-sources|' .env
else
  echo 'ZETARIS_PG_HOST=casefile-sources' >> .env
fi
echo
echo "Next: python scripts/publish_local.py load --yes   then   python scripts/zetaris_apply.py"
