"""A stand-in for Zetaris while it isn't running: DuckDB over the prepared files.

It registers the same qualified names as Zetaris (``inst_a.ledger_a``,
``shared.kyc_b`` ...), and Institution B's KYC is exposed only as the governed
view without PII, exactly as on Zetaris. Use it to build and test; the demo
and the eval run on ``CASEFILE_DATA_BACKEND=zetaris``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

import duckdb

from ..config import PREPARED_DIR
from .base import QueryResult
from .schema import KYC_SHARED_COLUMNS, PREPARED_FILES


def _split(qualified: str) -> tuple[str, str]:
    if "." not in qualified:
        raise ValueError(f"Table name {qualified!r} must be qualified as <source>.<table>")
    schema, table = qualified.rsplit(".", 1)
    return schema, table


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class LocalBackend:
    name = "local"

    def __init__(self, settings, prepared_dir: Path | None = None) -> None:
        self.settings = settings
        self.prepared_dir = Path(prepared_dir or PREPARED_DIR)
        missing = [
            file for file, _ in PREPARED_FILES.values() if not (self.prepared_dir / file).exists()
        ]
        if missing:
            raise FileNotFoundError(
                f"Missing prepared files in {self.prepared_dir}: {', '.join(missing)}. "
                "Run scripts/prepare_data.py first."
            )
        self.con = duckdb.connect(database=":memory:")
        self._register()

    def _register(self) -> None:
        for key, (file, _) in PREPARED_FILES.items():
            schema, table = _split(self.settings.table(key))
            self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote(schema)}")
            path = (self.prepared_dir / file).as_posix().replace("'", "''")
            if file.endswith(".parquet"):
                source = f"read_parquet('{path}')"
            else:
                source = f"read_csv('{path}', header=true, all_varchar=true)"
            if key == "kyc_b":
                # Governed view: Institution B shares its KYC without PII.
                select = ", ".join(_quote(c) for c in KYC_SHARED_COLUMNS)
                sql = f"CREATE OR REPLACE VIEW {_quote(schema)}.{_quote(table)} AS SELECT {select} FROM {source}"
            else:
                sql = f"CREATE OR REPLACE VIEW {_quote(schema)}.{_quote(table)} AS SELECT * FROM {source}"
            self.con.execute(sql)

    def query(
        self,
        sql: str,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        limit = limit or self.settings.zetaris_query_limit
        started = time.perf_counter()
        cursor = self.con.execute(sql)
        names = [d[0] for d in cursor.description] if cursor.description else []
        rows = [list(r) for r in cursor.fetchmany(limit)] if names else []
        if hasattr(self.con, "abort"):  # DuckDB 2.x: release a partly read result
            try:
                self.con.abort()
            except Exception:
                pass
        duration_ms = int((time.perf_counter() - started) * 1000)
        if columns and len(columns) == len(names):
            names = list(columns)
        return QueryResult(
            sql=sql,
            columns=names,
            rows=rows,
            backend=self.name,
            duration_ms=duration_ms,
            truncated=len(rows) >= limit,
        )

    def close(self) -> None:
        self.con.close()
