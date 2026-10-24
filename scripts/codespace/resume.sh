#!/usr/bin/env bash
# After reopening a stopped Codespace: start Zetaris and the sources database again, and reconnect them.
#   bash scripts/codespace/resume.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$(find "$HOME" -maxdepth 3 -name docker-compose.yml -path '*zetaris*' 2>/dev/null | head -1)"
if [ -z "$COMPOSE_FILE" ]; then
  echo "Zetaris isn't installed in this Codespace yet: run bash scripts/codespace/start_zetaris.sh"
  exit 1
fi
(cd "$(dirname "$COMPOSE_FILE")" && docker compose up -d)
echo "Waiting for Zetaris to answer on port 3000 ..."
for i in $(seq 1 60); do
  curl -s -o /dev/null http://localhost:3000 && break
  sleep 5
done
bash "$ROOT/scripts/codespace/connect_sources.sh"
echo
echo "Back up. Check with: python scripts/check_setup.py"
