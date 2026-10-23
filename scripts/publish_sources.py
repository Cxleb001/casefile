"""Put the prepared data where Zetaris federates over it, then print the registration SQL.

Nothing is created unless you add --yes. Without it each command only shows
what it would do.

    python scripts/publish_sources.py postgres          # Institution A -> Postgres (PG_URL)
    python scripts/publish_sources.py postgres --yes
    python scripts/publish_sources.py s3                # Institution B + watchlist -> S3 (S3_BUCKET)
    python scripts/publish_sources.py s3 --yes
    python scripts/publish_sources.py sql               # registration SQL with your names filled in
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import PREPARED_DIR, get_settings  # noqa: E402
from casefile.data.schema import KYC_COLUMNS, LEDGER_COLUMNS, POSTGRES_TYPES  # noqa: E402

POSTGRES_TABLES = {
    "ledger_a": ("ledger_a.parquet", LEDGER_COLUMNS, [
        "CREATE INDEX IF NOT EXISTS ledger_a_from ON ledger_a (from_bank, from_account)",
        "CREATE INDEX IF NOT EXISTS ledger_a_to ON ledger_a (to_bank, to_account)",
        "CREATE INDEX IF NOT EXISTS ledger_a_ts ON ledger_a (ts)",
    ]),
    "kyc_a": ("kyc_a.parquet", KYC_COLUMNS, [
        "CREATE UNIQUE INDEX IF NOT EXISTS kyc_a_account ON kyc_a (bank_id, account)",
    ]),
}

# S3 key (under S3_PREFIX) -> prepared file. Each table gets its own folder.
S3_OBJECTS = {
    "inst_b/ledger_b/ledger_b.parquet": "ledger_b.parquet",
    "inst_b/kyc_b/kyc_b.parquet": "kyc_b.parquet",
    "compliance/watchlist/watchlist.csv": "watchlist.csv",
}


def pg_parts(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise SystemExit("PG_URL must look like postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require")
    return {
        "PG_HOST": parsed.hostname,
        "PG_PORT": str(parsed.port or 5432),
        "PG_DB": parsed.path.lstrip("/") or "postgres",
        "PG_USER": parsed.username or "",
    }


def publish_postgres(settings, yes: bool, replace: bool) -> None:
    import pandas as pd

    if not settings.pg_url:
        raise SystemExit("Set PG_URL in .env first (Institution A's Postgres).")
    target = pg_parts(settings.pg_url)
    print(f"Postgres: {target['PG_HOST']}/{target['PG_DB']} as {target['PG_USER']}")
    frames = {}
    for table, (file, columns, _) in POSTGRES_TABLES.items():
        path = PREPARED_DIR / file
        if not path.exists():
            raise SystemExit(f"Missing {path}; run scripts/prepare_data.py first.")
        frames[table] = pd.read_parquet(path)[[c for c, _ in columns]]
        print(f"  {table}: {len(frames[table]):,} rows from {file}")
    if not yes:
        print("\nDry run. Add --yes to create the tables and load them" +
              (" (replacing existing ones)." if replace else "."))
        return

    import psycopg

    with psycopg.connect(settings.pg_url) as conn:
        for table, (_, columns, indexes) in POSTGRES_TABLES.items():
            with conn.cursor() as cur:
                if replace:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
                ddl = ", ".join(f"{name} {POSTGRES_TYPES[kind]}" for name, kind in columns)
                try:
                    cur.execute(f"CREATE TABLE {table} ({ddl})")
                except psycopg.errors.DuplicateTable:
                    raise SystemExit(f"Table {table} already exists. Add --replace to drop and reload it.")
                frame = frames[table].astype(object).where(frames[table].notna(), None)
                names = ", ".join(name for name, _ in columns)
                with cur.copy(f"COPY {table} ({names}) FROM STDIN") as copy:
                    for row in frame.itertuples(index=False, name=None):
                        copy.write_row(row)
                for statement in indexes:
                    cur.execute(statement)
                cur.execute(f"ANALYZE {table}")
            conn.commit()
            print(f"  loaded {table}")
    print("Done. Next: python scripts/publish_sources.py s3")


def publish_s3(settings, yes: bool) -> None:
    if not settings.s3_bucket:
        raise SystemExit("Set S3_BUCKET in .env first (Institution B's bucket).")
    plan = []
    for key, file in S3_OBJECTS.items():
        path = PREPARED_DIR / file
        if not path.exists():
            raise SystemExit(f"Missing {path}; run scripts/prepare_data.py first.")
        plan.append((path, f"{settings.s3_prefix}/{key}"))
        print(f"  {file:<18} -> s3://{settings.s3_bucket}/{settings.s3_prefix}/{key}  "
              f"({path.stat().st_size / 1e6:.1f} MB)")
    if not yes:
        print("\nDry run. Add --yes to upload.")
        return

    import boto3

    s3 = boto3.client("s3", region_name=settings.aws_region)
    for path, key in plan:
        s3.upload_file(str(path), settings.s3_bucket, key)
        print(f"  uploaded {key}")
    print("Done. Next: python scripts/publish_sources.py sql")


def print_sql(settings, agent_user: str) -> None:
    template = (ROOT / "scripts" / "zetaris" / "register_sources.sql").read_text(encoding="utf-8")
    values = {
        "S3_BUCKET": settings.s3_bucket or "<S3_BUCKET>",
        "S3_PREFIX": settings.s3_prefix,
        "AWS_REGION": settings.aws_region,
        "AGENT_USER": agent_user,
        "PG_HOST": "<PG_HOST>", "PG_PORT": "5432", "PG_DB": "<PG_DB>", "PG_USER": "<PG_USER>",
    }
    if settings.pg_url:
        values.update(pg_parts(settings.pg_url))
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    print(template)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=["postgres", "s3", "sql"])
    parser.add_argument("--yes", action="store_true", help="actually create / upload")
    parser.add_argument("--replace", action="store_true", help="postgres: drop and recreate existing tables")
    parser.add_argument("--agent-user", default="casefile-agent", help="sql: the restricted Zetaris user")
    args = parser.parse_args()

    settings = get_settings()
    if args.target == "postgres":
        publish_postgres(settings, args.yes, args.replace)
    elif args.target == "s3":
        publish_s3(settings, args.yes)
    else:
        print_sql(settings, args.agent_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
