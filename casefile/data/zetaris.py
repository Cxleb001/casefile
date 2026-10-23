"""Run SQL through a Zetaris cluster over HTTP.

Two routes, both taken from zetaris/getting-started:

* ``rest``  (default): the direct API, ``POST {ZETARIS_REST_URL}/sql-editor/sqls/run``
  with a bearer API key, as in scripts/zetaris_sql.py.
* ``proxy``: the web UI proxy, ``POST {ZETARIS_BASE_URL}/api/proxy/sql-editor/sqls/run-query``,
  as in scripts/query_zetaris.py.

Zetaris REST returns rows as lists of strings. ``_extract`` accepts the shapes
we know of; run ``python scripts/check_setup.py --raw`` to print the real
response from your cluster if parsing ever looks wrong.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import QueryResult, coerce_value


class ZetarisError(RuntimeError):
    pass


_ROW_KEYS = ("data", "rows", "records", "result", "results", "resultSet")
_COLUMN_KEYS = ("columns", "columnNames", "header", "headers", "fields", "schema", "metadata")


def _column_names(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    names = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("columnName") or item.get("label") or item.get("field")
            if not isinstance(name, str):
                return None
            names.append(name)
        else:
            return None
    return names


def _extract(payload: Any) -> tuple[list[str] | None, list[list[Any]]]:
    """Find (column names, rows) in a Zetaris JSON response."""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            columns = list(payload[0].keys())
            return columns, [[row.get(c) for c in columns] for row in payload]
        return None, [list(r) if isinstance(r, (list, tuple)) else [r] for r in payload]

    if not isinstance(payload, dict):
        return None, []

    columns = None
    for key in _COLUMN_KEYS:
        columns = _column_names(payload.get(key))
        if columns:
            break

    for key in _ROW_KEYS:
        if key not in payload:
            continue
        inner = payload[key]
        if isinstance(inner, dict):
            inner_columns, rows = _extract(inner)
            return columns or inner_columns, rows
        if isinstance(inner, list):
            inner_columns, rows = _extract(inner)
            return columns or inner_columns, rows
    return columns, []


def _error_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("error", "errorMessage", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                status = str(payload.get("status", "")).lower()
                if key != "message" or status in {"error", "failed", "failure"}:
                    return value.strip()
    return None


class ZetarisBackend:
    name = "zetaris"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.route = settings.zetaris_route
        if not settings.zetaris_org_id.isdigit():
            raise ZetarisError(
                "Set ZETARIS_ORG_ID in .env to the numeric ID under Management -> Organisation."
            )
        if self.route == "rest":
            if not settings.zetaris_rest_url:
                raise ZetarisError("Set ZETARIS_REST_URL in .env (the API base ending in /api/v1.0).")
            if not settings.zetaris_api_key:
                raise ZetarisError("Set ZETARIS_API_KEY in .env (Management -> API Tokens).")
        elif not settings.zetaris_base_url:
            raise ZetarisError("Set ZETARIS_BASE_URL in .env to the Zetaris web UI origin.")
        self._token: str | None = settings.zetaris_api_key or None

    # -- HTTP ---------------------------------------------------------------

    def _post(self, url: str, body: dict, timeout: int) -> Any:
        headers = {
            "Authorization": f"Bearer {self._auth_token()}",
            "X-Org-ID": self.settings.zetaris_org_id,
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text.strip() else None
        except HTTPError as error:
            detail = error.read(600).decode("utf-8", errors="replace")
            if "text/html" in error.headers.get("content-type", ""):
                detail = "the server returned an HTML page; check the URL in .env"
            hint = ""
            if error.code == 401:
                hint = " (API key rejected: create a fresh one under Management -> API Tokens)"
            elif error.code == 400 and "Not Allowed" in detail:
                hint = " (check ZETARIS_ORG_ID is your numeric organisation ID)"
            raise ZetarisError(f"HTTP {error.code} from Zetaris{hint}: {detail[:400]}") from error
        except (URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise ZetarisError(f"Cannot reach Zetaris at {url}: {reason}") from error

    def _auth_token(self) -> str:
        if self._token:
            return self._token
        # Proxy route without an API key: log in with username and password.
        s = self.settings
        if not (s.zetaris_username and s.zetaris_password):
            raise ZetarisError("Set ZETARIS_API_KEY, or ZETARIS_USERNAME and ZETARIS_PASSWORD, in .env.")
        body = json.dumps({"username": s.zetaris_username, "password": s.zetaris_password}).encode("utf-8")
        request = Request(
            f"{s.zetaris_base_url}/api/auth/login",
            data=body,
            headers={"Content-Type": "application/json", "X-Request-ID": str(uuid.uuid4())},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                login = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ZetarisError(f"Zetaris login failed: HTTP {error.code}") from error
        except (URLError, OSError, TimeoutError) as error:
            raise ZetarisError(f"Cannot reach Zetaris login: {getattr(error, 'reason', error)}") from error
        token = None
        if isinstance(login, dict):
            token = login.get("idToken") or login.get("token") or login.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ZetarisError("Zetaris login did not return an access token.")
        self._token = token
        return token

    # -- Public API ----------------------------------------------------------

    def raw(self, sql: str, limit: int | None = None) -> Any:
        """Return the unparsed JSON response, for debugging response shapes."""
        limit = limit or self.settings.zetaris_query_limit
        if self.route == "rest":
            body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
            return self._post(f"{self.settings.zetaris_rest_url}/sql-editor/sqls/run", body, timeout=600)
        body = {"sql": sql, "limit": limit}
        if self.settings.zetaris_engine_id:
            body["engineId"] = self.settings.zetaris_engine_id
        return self._post(
            f"{self.settings.zetaris_base_url}/api/proxy/sql-editor/sqls/run-query", body, timeout=600
        )

    def query(
        self,
        sql: str,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        limit = limit or self.settings.zetaris_query_limit
        started = time.perf_counter()
        payload = self.raw(sql, limit)
        duration_ms = int((time.perf_counter() - started) * 1000)

        found_columns, rows = _extract(payload)
        if not rows:
            error = _error_text(payload)
            if error:
                raise ZetarisError(f"Zetaris rejected the query: {error}")

        width = len(rows[0]) if rows else len(columns or found_columns or [])
        names = list(found_columns or columns or [f"c{i}" for i in range(width)])
        if rows and len(names) != width:
            raise ZetarisError(
                f"Expected {len(names)} columns ({', '.join(names)}) but rows have {width}; "
                "alias every SELECT column and pass the same names."
            )
        rows = [[coerce_value(v) for v in row] for row in rows]
        return QueryResult(
            sql=sql,
            columns=names,
            rows=rows,
            backend=self.name,
            duration_ms=duration_ms,
            truncated=len(rows) >= limit,
            raw=payload,
        )

    def close(self) -> None:
        pass
