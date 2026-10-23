"""Small helpers that keep the agent's SQL portable and safe.

Every query must run unchanged on DuckDB (the local stand-in) and on Zetaris
Lightning (Spark SQL), so tools stick to a common subset: CAST, CASE, COUNT
DISTINCT, UNION ALL, TIMESTAMP literals, no ``::`` casts, no ILIKE. Values
are inlined as literals because the Zetaris REST route has no bind
parameters, so everything inlined goes through these validators first.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

_ACCOUNT = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|CACHE|UNCACHE|"
    r"REGISTER|REFRESH|COPY|ATTACH|DETACH|INSTALL|LOAD|PRAGMA|SET|CALL|EXPORT|IMPORT|"
    r"COMPILE|ACTIVATE|REMOVE|ASSIGN|OFFSITE)\b",
    re.IGNORECASE,
)


class UnsafeSQL(ValueError):
    pass


def account_literal(account: Any) -> str:
    text = str(account).strip()
    if not _ACCOUNT.match(text):
        raise ValueError(f"Not a valid account id: {account!r}")
    return f"'{text}'"


def bank_literal(bank: Any) -> str:
    try:
        return str(int(bank))
    except (TypeError, ValueError) as error:
        raise ValueError(f"Not a valid bank id: {bank!r}") from error


def ts_literal(value: Any) -> str:
    """TIMESTAMP literal accepted by both DuckDB and Spark SQL."""
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).strip().replace("T", " ").replace("/", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                stamp = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Not a timestamp: {value!r}")
    return f"TIMESTAMP '{stamp:%Y-%m-%d %H:%M:%S}'"


def in_list(values: Iterable[Any], literal=account_literal) -> str:
    items = sorted({literal(v) for v in values})
    if not items:
        raise ValueError("Empty IN list")
    return "(" + ", ".join(items) + ")"


def string_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def check_readonly(sql: str, max_length: int = 6000) -> str:
    """Accept one read-only SELECT/WITH statement and return it without a trailing semicolon."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise UnsafeSQL("Empty SQL")
    if len(text) > max_length:
        raise UnsafeSQL(f"SQL longer than {max_length} characters")
    if ";" in text:
        raise UnsafeSQL("Only one statement is allowed")
    if "--" in text or "/*" in text:
        raise UnsafeSQL("Comments are not allowed")
    if not re.match(r"^(SELECT|WITH)\b", text, re.IGNORECASE):
        raise UnsafeSQL("Only SELECT or WITH queries are allowed")
    # Ignore quoted literals when looking for write keywords.
    unquoted = re.sub(r"'(?:[^']|'')*'", "''", text)
    match = _FORBIDDEN.search(unquoted)
    if match:
        raise UnsafeSQL(f"Keyword not allowed: {match.group(0).upper()}")
    return text


def to_iso(value: Any) -> str | None:
    """Normalise a timestamp from either backend (datetime or string) to 'YYYY-MM-DD HH:MM'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value).strip().replace("T", " ")
    return text[:16]


def to_datetime(value: Any) -> datetime | None:
    iso = to_iso(value)
    if not iso:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(iso[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None
