"""Load the prepared data into the local sources container, then print the Zetaris registration SQL.

For the local route (Zetaris in Docker on your machine). Start the container first:

    docker compose -f infra/docker-compose.sources.yml up -d

Then:
    python scripts/publish_local.py check          # can Python reach the three databases?
    python scripts/publish_local.py load           # dry run: what would be loaded
    python scripts/publish_local.py load --yes     # create the tables and load them (use --replace to reload)
    python scripts/publish_local.py sql            # registration SQL for the Zetaris SQL Editor

Settings (optional, in .env): LOCAL_PG_HOST (localhost), LOCAL_PG_PORT (5433), LOCAL_PG_USER (casefile),
LOCAL_PG_PASSWORD (casefile-local), ZETARIS_PG_HOST (host.docker.internal: how Zetaris reaches it).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import PREPARED_DIR, get_settings  # noqa: E402,F401  (loads .env)
from casefile.data.schema import KYC_COLUMNS, LEDGER_COLUMNS, POSTGRES_TYPES, WATCHLIST_COLUMNS  # noqa: E402

# database -> table -> (prepared file, columns, indexes)
PLAN = {
    "bank_a": {
        "ledger_a": ("ledger_a.parquet", LEDGER_COLUMNS, ["(from_bank, from_account)", "(to_bank, to_account)",
                                                          "(from_account)", "(to_account)", "(ts)"]),
        "kyc_a": ("kyc_a.parquet", KYC_COLUMNS, ["(bank_id, account)", "(account)", "(customer_id)"]),
    },
    "bank_b": {
        "ledger_b": ("ledger_b.parquet", LEDGER_COLUMNS, ["(from_bank, from_account)", "(to_bank, to_account)",
                                                          "(from_account)", "(to_account)", "(ts)"]),
        "kyc_b": ("kyc_b.parquet", KYC_COLUMNS, ["(bank_id, account)", "(account)", "(customer_id)"]),
    },
    "compliance": {
        "watchlist": ("watchlist.csv", WATCHLIST_COLUMNS, ["(name)"]),
    },
}


def local_settings() -> dict[str, str]:
    return {
        "host": os.environ.get("LOCAL_PG_HOST", "localhost"),
        "port": os.environ.get("LOCAL_PG_PORT", "5433"),
        "user": os.environ.get("LOCAL_PG_USER", "casefile"),
        "password": os.environ.get("LOCAL_PG_PASSWORD", "casefile-local"),
        "zetaris_host": os.environ.get("ZETARIS_PG_HOST", "host.docker.internal"),
    }


def connect(cfg: dict[str, str], database: str):
    import psycopg

    return psycopg.connect(host=cfg["host"], port=int(cfg["port"]), user=cfg["user"], password=cfg["password"],
                           dbname=database, connect_timeout=10)


def read_prepared(file: str, columns):
    import pandas as pd

    path = PREPARED_DIR / file
    if not path.exists():
        raise SystemExit(f"Missing {path}; run scripts/prepare_data.py first.")
    if file.endswith(".csv"):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        frame = pd.read_parquet(path)
    return frame[[c for c, _ in columns]]


def check(cfg) -> int:
    ok = True
    for database in PLAN:
        try:
            with connect(cfg, database) as conn:
                conn.execute("SELECT 1")
            print(f"  [PASS] {cfg['host']}:{cfg['port']}/{database}")
        except Exception as error:
            ok = False
            print(f"  [FAIL] {cfg['host']}:{cfg['port']}/{database}: {str(error).splitlines()[0]}")
    if not ok:
        print("\nIs the container running?  docker compose -f infra/docker-compose.sources.yml up -d")
    return 0 if ok else 1


def load(cfg, yes: bool, replace: bool) -> int:
    frames = {}
    for database, tables in PLAN.items():
        for table, (file, columns, _) in tables.items():
            frames[(database, table)] = read_prepared(file, columns)
            print(f"  {database}.{table:<10} {len(frames[(database, table)]):>9,} rows from {file}")
    if not yes:
        print("\nDry run. Add --yes to create the tables and load them" + (" (replacing them)." if replace else "."))
        return 0

    import psycopg

    for database, tables in PLAN.items():
        with connect(cfg, database) as conn:
            for table, (_, columns, indexes) in tables.items():
                with conn.cursor() as cur:
                    if replace:
                        cur.execute(f"DROP TABLE IF EXISTS {table}")
                    ddl = ", ".join(f"{name} {POSTGRES_TYPES[kind]}" for name, kind in columns)
                    try:
                        cur.execute(f"CREATE TABLE {table} ({ddl})")
                    except psycopg.errors.DuplicateTable:
                        raise SystemExit(f"{database}.{table} already exists. Add --replace to drop and reload it.")
                    frame = frames[(database, table)]
                    frame = frame.astype(object).where(frame.notna(), None)
                    names = ", ".join(name for name, _ in columns)
                    with cur.copy(f"COPY {table} ({names}) FROM STDIN") as copy:
                        for row in frame.itertuples(index=False, name=None):
                            copy.write_row(row)
                    for i, cols in enumerate(indexes):
                        cur.execute(f"CREATE INDEX IF NOT EXISTS {table}_ix{i} ON {table} {cols}")
                    cur.execute(f"ANALYZE {table}")
                conn.commit()
                print(f"  loaded {database}.{table}")
    print("\nDone. Next: python scripts/publish_local.py sql   (then run it in the Zetaris SQL Editor)")
    return 0


def print_sql(cfg) -> int:
    template = (ROOT / "scripts" / "zetaris" / "register_local.sql").read_text(encoding="utf-8")
    values = {"ZETARIS_PG_HOST": cfg["zetaris_host"], "LOCAL_PG_PORT": "5432" if cfg["zetaris_host"] == "casefile-sources"
              else cfg["port"], "LOCAL_PG_USER": cfg["user"]}
    for name, value in values.items():
        template = template.replace("{{" + name + "}}", value)
    print(template)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["check", "load", "sql"])
    parser.add_argument("--yes", action="store_true", help="load: actually create and load the tables")
    parser.add_argument("--replace", action="store_true", help="load: drop and reload existing tables")
    args = parser.parse_args()
    get_settings()  # loads .env, so LOCAL_PG_* and ZETARIS_PG_HOST set there are used
    cfg = local_settings()
    if args.action == "check":
        return check(cfg)
    if args.action == "load":
        return load(cfg, args.yes, args.replace)
    return print_sql(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
