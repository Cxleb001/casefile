"""The one interface the agent uses to read data, whatever answers it."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass
class QueryResult:
    """Rows returned by a backend, plus what it took to get them."""

    sql: str
    columns: list[str]
    rows: list[list[Any]]
    backend: str
    duration_ms: int
    truncated: bool = False
    raw: Any = field(default=None, repr=False)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.columns)


class DataBackend(Protocol):
    name: str

    def query(
        self,
        sql: str,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        """Run one SQL statement.

        ``columns`` names the result columns when the backend doesn't return
        names (Zetaris REST returns bare rows). Tools always alias their
        SELECT columns and pass the same names here.
        """
        ...

    def close(self) -> None: ...


def coerce_value(value: Any) -> Any:
    """Turn the strings Zetaris REST returns into numbers where they look like numbers.

    REST returns every value as a string and may print decimals in exponent
    form ("1.2E3"), so numeric comparisons need this first.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "" or text.lower() in {"null", "none"}:
        return None
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return value
    try:
        number = float(text)
    except ValueError:
        return value
    return number if math.isfinite(number) else value


def get_backend(settings=None) -> DataBackend:
    """Build the backend named by CASEFILE_DATA_BACKEND."""
    from ..config import get_settings

    settings = settings or get_settings()
    if settings.data_backend == "zetaris":
        from .zetaris import ZetarisBackend

        return ZetarisBackend(settings)
    from .local import LocalBackend

    return LocalBackend(settings)
