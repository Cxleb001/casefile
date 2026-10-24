#!/usr/bin/env bash
# Start Zetaris inside a GitHub Codespace (or any Linux machine with Docker and 12+ GB of RAM).
#
# Before running:
#   1. Download zetaris-platform.zip from the Zetaris "Run locally" page and drag it into the
#      casefile folder in the Codespace file list.
#   2. Run the `docker login zetarishackathon.azurecr.io` command shown on that page.
#
# Usage:  bash scripts/codespace/start_zetaris.sh            (first time: unpack, configure, pull, start)
#         bash scripts/codespace/start_zetaris.sh --browser  (also allow the Codespace's forwarded URL
#                                                             to sign in to the Zetaris web UI)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ZIP="$ROOT/zetaris-platform.zip"
BROWSER=0
[ "${1:-}" = "--browser" ] && BROWSER=1

find_bundle() {
  find "$HOME" -maxdepth 3 -name docker-compose.yml -path '*zetaris*' 2>/dev/null | head -1
}

COMPOSE_FILE="$(find_bundle || true)"
if [ -z "$COMPOSE_FILE" ]; then
  if [ ! -f "$ZIP" ]; then
    echo "Step 1 missing: put zetaris-platform.zip in $ROOT (drag it into the file list on the left)."
    exit 1
  fi
  echo "Unpacking $ZIP into $HOME ..."
  unzip -q -o "$ZIP" -d "$HOME"
  COMPOSE_FILE="$(find_bundle)"
fi
DIR="$(dirname "$COMPOSE_FILE")"
cd "$DIR"
echo "Zetaris bundle: $DIR"

if [ ! -f .env ]; then
  echo "The bundle has no .env file. Ask Zetaris (HackOS Help desk) for the matching .env."
  exit 1
fi
[ -f .env.before-casefile ] || cp .env .env.before-casefile

set_env() {  # set_env KEY VALUE: replace the line if present, else append
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

# A 4-core / 16 GB Codespace has room for more than Zetaris's laptop defaults.
set_env ZSPARK_DRIVER_MEM 4g
set_env ZSPARK_DRIVER_CORES 2
set_env ZSPARK_MASTER_URL "local[3]"
set_env ZETARIS_API_MAX_HEAP 2g

if [ "$BROWSER" = "1" ] && [ -n "${CODESPACE_NAME:-}" ]; then
  FORWARDED="https://${CODESPACE_NAME}-3000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  set_env GUI_URL "$FORWARDED"
  set_env ALLOWED_ORIGINS "http://localhost:3000,${FORWARDED}"
  echo "Web UI will be at $FORWARDED"
fi
chmod 600 .env

echo "Checking Docker ..."
docker info >/dev/null 2>&1 || { echo "Docker is not running in this Codespace. Rebuild it (Codespaces menu > Rebuild Container)."; exit 1; }
[ -x ./preflight.sh ] && ./preflight.sh || echo "(preflight reported issues above; continuing)"

echo "Pulling images (about 10 GB, 5-15 minutes) ..."
docker compose pull
docker compose up -d

echo "Waiting for first-time setup to finish (up to 15 minutes) ..."
for i in $(seq 1 90); do
  state="$(docker compose ps --all --format '{{.Service}} {{.State}} {{.ExitCode}}' 2>/dev/null | awk '$1=="bootstrap"{print $2, $3}')"
  case "$state" in
    "exited 0") echo "Bootstrap finished."; break ;;
    exited*) echo "Bootstrap failed: $state"; docker compose logs --tail=60 bootstrap; exit 1 ;;
  esac
  sleep 10
done

docker compose ps --all
if [ -x ./verify.sh ]; then
  ./verify.sh || { echo "Some checks failed; wait a minute and run: (cd $DIR && ./verify.sh)"; exit 1; }
fi
echo
echo "Zetaris is running. Next: bash scripts/codespace/connect_sources.sh"
