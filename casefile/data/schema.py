"""Column layouts shared by data prep, publishing, the local backend and the Zetaris SQL.

One definition here keeps the Postgres tables, the S3 files and the local
DuckDB views identical, so the agent's SQL runs unchanged on any backend.
"""

from __future__ import annotations

LEDGER_COLUMNS: list[tuple[str, str]] = [
    ("tx_id", "VARCHAR"),
    ("ts", "TIMESTAMP"),
    ("from_bank", "BIGINT"),
    ("from_account", "VARCHAR"),
    ("to_bank", "BIGINT"),
    ("to_account", "VARCHAR"),
    ("amount_paid", "DOUBLE"),
    ("payment_currency", "VARCHAR"),
    ("amount_received", "DOUBLE"),
    ("receiving_currency", "VARCHAR"),
    ("payment_format", "VARCHAR"),
]

KYC_COLUMNS: list[tuple[str, str]] = [
    ("bank_id", "BIGINT"),
    ("account", "VARCHAR"),
    ("customer_id", "VARCHAR"),
    ("customer_type", "VARCHAR"),
    ("full_name", "VARCHAR"),
    ("dob_or_incorporation", "DATE"),
    ("national_id", "VARCHAR"),
    ("country", "VARCHAR"),
    ("occupation_or_industry", "VARCHAR"),
    ("account_open_date", "DATE"),
    ("declared_monthly_turnover_usd", "DOUBLE"),
    ("risk_rating", "VARCHAR"),
    ("pep_flag", "BOOLEAN"),
    ("last_review_date", "DATE"),
]

# Columns Institution B withholds from partners. Its governed view drops them.
KYC_PII_COLUMNS = ("full_name", "dob_or_incorporation", "national_id")
KYC_SHARED_COLUMNS = [name for name, _ in KYC_COLUMNS if name not in KYC_PII_COLUMNS]

WATCHLIST_COLUMNS: list[tuple[str, str]] = [
    ("list_id", "VARCHAR"),
    ("entity_type", "VARCHAR"),
    ("name", "VARCHAR"),
    ("aliases", "VARCHAR"),
    ("birth_or_incorporation", "VARCHAR"),
    ("country", "VARCHAR"),
    ("program", "VARCHAR"),
    ("listed_on", "VARCHAR"),
    ("source", "VARCHAR"),
]

# Logical table -> (prepared file name, columns). kyc_b is published raw and
# exposed only through the governed view.
PREPARED_FILES = {
    "ledger_a": ("ledger_a.parquet", LEDGER_COLUMNS),
    "kyc_a": ("kyc_a.parquet", KYC_COLUMNS),
    "ledger_b": ("ledger_b.parquet", LEDGER_COLUMNS),
    "kyc_b": ("kyc_b.parquet", KYC_COLUMNS),
    "watchlist": ("watchlist.csv", WATCHLIST_COLUMNS),
}

POSTGRES_TYPES = {
    "VARCHAR": "TEXT",
    "TIMESTAMP": "TIMESTAMP",
    "BIGINT": "BIGINT",
    "DOUBLE": "DOUBLE PRECISION",
    "DATE": "DATE",
    "BOOLEAN": "BOOLEAN",
}
