"""Where every query, model call and claim is recorded.

This is what makes the report clickable: each claim lists the queries behind
it, and each query keeps its SQL and the rows it returned. The verifier writes
its verdict back onto the claim.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .config import RUNS_DIR

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS queries (
        query_id     VARCHAR PRIMARY KEY,
        case_id      VARCHAR,
        created_at   TIMESTAMP,
        backend      VARCHAR,
        purpose      VARCHAR,
        sql          VARCHAR,
        columns_json VARCHAR,
        rows_json    VARCHAR,
        row_count    INTEGER,
        truncated    BOOLEAN,
        duration_ms  INTEGER,
        error        VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claims (
        claim_id       VARCHAR PRIMARY KEY,
        case_id        VARCHAR,
        created_at     TIMESTAMP,
        text           VARCHAR,
        query_ids_json VARCHAR,
        verified       BOOLEAN,
        verifier_model VARCHAR,
        verifier_note  VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id           VARCHAR PRIMARY KEY,
        case_id           VARCHAR,
        created_at        TIMESTAMP,
        step              VARCHAR,
        provider          VARCHAR,
        model             VARCHAR,
        latency_ms        INTEGER,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER
    )
    """,
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


class EvidenceStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else RUNS_DIR / "casefile.duckdb"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))
        for statement in _SCHEMA:
            self.con.execute(statement)
        self._issued: dict[str, int] = {}

    def _query_id(self, case_id: str | None) -> str:
        """The n-th query of a case always gets the same id, so a recorded run can be replayed exactly
        (the model's answers cite these ids). Queries outside a case get a random id."""
        if not case_id:
            return "Q-" + uuid.uuid4().hex[:10]
        n = self._issued.get(case_id, 0)
        self._issued[case_id] = n + 1
        return "Q-" + hashlib.sha1(f"{case_id}#{n}".encode()).hexdigest()[:10]

    def forget_case(self, case_id: str) -> None:
        """Drop everything stored for a case, before running it again under the same id."""
        for table in ("queries", "claims", "llm_calls"):
            self.con.execute(f"DELETE FROM {table} WHERE case_id = ?", [case_id])
        self._issued.pop(case_id, None)

    # -- writes ---------------------------------------------------------------

    def record_query(self, case_id: str | None, result=None, *, sql: str = "", purpose: str = "",
                     backend: str = "", error: str | None = None) -> str:
        """Store a QueryResult (or a failed query) and return its id."""
        query_id = self._query_id(case_id)
        if result is not None:
            self.con.execute(
                "INSERT INTO queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [query_id, case_id, _now(), result.backend, purpose, result.sql, _json(result.columns),
                 _json(result.rows), result.row_count, result.truncated, result.duration_ms, None],
            )
        else:
            self.con.execute(
                "INSERT INTO queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [query_id, case_id, _now(), backend, purpose, sql, "[]", "[]", 0, False, 0, error],
            )
        return query_id

    def add_claim(self, case_id: str, text: str, query_ids: list[str]) -> str:
        claim_id = "C-" + uuid.uuid4().hex[:10]
        self.con.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)",
            [claim_id, case_id, _now(), text, _json(query_ids)],
        )
        return claim_id

    def set_verdict(self, claim_id: str, verified: bool, model: str, note: str) -> None:
        self.con.execute(
            "UPDATE claims SET verified = ?, verifier_model = ?, verifier_note = ? WHERE claim_id = ?",
            [verified, model, note, claim_id],
        )

    def record_llm_call(self, result, case_id: str | None = None) -> None:
        """Pass this as LLM(on_call=store.record_llm_call)."""
        self.con.execute(
            "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["L-" + uuid.uuid4().hex[:10], case_id, _now(), result.step, result.provider, result.model,
             result.latency_ms, result.prompt_tokens, result.completion_tokens],
        )

    # -- reads ----------------------------------------------------------------

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT query_id, case_id, backend, purpose, sql, columns_json, rows_json, row_count, "
            "truncated, duration_ms, error FROM queries WHERE query_id = ?",
            [query_id],
        ).fetchall()
        if not row:
            return None
        row = row[0]
        keys = ["query_id", "case_id", "backend", "purpose", "sql", "columns", "rows", "row_count",
                "truncated", "duration_ms", "error"]
        record = dict(zip(keys, row))
        record["columns"] = json.loads(record["columns"])
        record["rows"] = json.loads(record["rows"])
        return record

    def claims_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT claim_id, text, query_ids_json, verified, verifier_model, verifier_note "
            "FROM claims WHERE case_id = ? ORDER BY created_at",
            [case_id],
        ).fetchall()
        return [
            {"claim_id": r[0], "text": r[1], "query_ids": json.loads(r[2]), "verified": r[3],
             "verifier_model": r[4], "verifier_note": r[5]}
            for r in rows
        ]

    def model_usage(self) -> list[dict[str, Any]]:
        """Which model ran which step, for the README table."""
        rows = self.con.execute(
            "SELECT step, provider, model, count(*) AS calls, avg(latency_ms) AS avg_ms "
            "FROM llm_calls GROUP BY step, provider, model ORDER BY step"
        ).fetchall()
        return [{"step": r[0], "provider": r[1], "model": r[2], "calls": r[3], "avg_ms": round(r[4] or 0)}
                for r in rows]

    def close(self) -> None:
        self.con.close()
