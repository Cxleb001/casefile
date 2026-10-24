#!/usr/bin/env bash
# Download IBM AMLworld HI-Small from Kaggle straight into data/raw (no laptop download needed).
#
# Needs a Kaggle API token: kaggle.com > your profile > Settings (Account) > API > Generate New Token.
# Run this and paste the token when asked (it is stored in ~/.kaggle/access_token, never printed):
#   bash scripts/codespace/get_data.sh
# An old-style kaggle.json also works: drag it into the casefile folder first.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

mkdir -p "$HOME/.kaggle"
if [ -f kaggle.json ]; then
  mv kaggle.json "$HOME/.kaggle/kaggle.json" && chmod 600 "$HOME/.kaggle/kaggle.json"
fi
if [ ! -f "$HOME/.kaggle/kaggle.json" ] && [ ! -f "$HOME/.kaggle/access_token" ] && [ -z "${KAGGLE_API_TOKEN:-}" ]; then
  echo "Kaggle: open kaggle.com > your profile > Settings > API > Generate New Token, and copy the token."
  read -r -s -p "Paste the Kaggle token (hidden), then Enter: " TOKEN; echo
  [ -n "$TOKEN" ] || { echo "No token given."; exit 1; }
  printf '%s' "$TOKEN" > "$HOME/.kaggle/access_token" && chmod 600 "$HOME/.kaggle/access_token"
  echo "Saved to ~/.kaggle/access_token"
fi

KAGGLE=".venv/bin/kaggle"; [ -x "$KAGGLE" ] || KAGGLE="kaggle"
DATASET="ealtman2019/ibm-transactions-for-anti-money-laundering-aml"
mkdir -p data/raw
for f in HI-Small_Trans.csv HI-Small_Patterns.txt HI-Small_accounts.csv; do
  if [ -f "data/raw/$f" ]; then echo "have $f"; continue; fi
  echo "downloading $f ..."
  "$KAGGLE" datasets download -d "$DATASET" -f "$f" -p data/raw -q || echo "  could not download $f (fine if it is the accounts file)"
  if [ -f "data/raw/$f.zip" ]; then unzip -o -q "data/raw/$f.zip" -d data/raw && rm "data/raw/$f.zip"; fi
done
ls -lh data/raw

ACCOUNTS=""
[ -f data/raw/HI-Small_accounts.csv ] && ACCOUNTS="--accounts data/raw/HI-Small_accounts.csv"
echo
echo "Next:"
echo "  .venv/bin/python scripts/prepare_data.py --trans data/raw/HI-Small_Trans.csv --patterns data/raw/HI-Small_Patterns.txt $ACCOUNTS --memory-limit 6GB"
