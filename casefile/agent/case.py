"""Everything the agent has learned about one alert, kept in one place.

The case holds the money-flow graph built up by the tools (every edge knows
which query found it and which institution's ledger it was seen in), the
customer records it has looked up, every observation in order, and the
signals the analysis derived. The report, the verifier and the UI all read
from here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .fx import to_usd
from .sqlutil import to_datetime, to_iso


def key(bank: Any, account: Any) -> str:
    return f"{int(bank)}:{str(account).strip()}"


def split_key(account_key: str) -> tuple[int, str]:
    bank, account = account_key.split(":", 1)
    return int(bank), account


@dataclass
class Edge:
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount_paid: float
    payment_currency: str
    amount_received: float
    receiving_currency: str
    payment_format: str
    seen_in: set[str] = field(default_factory=set)
    query_ids: set[str] = field(default_factory=set)

    @property
    def usd(self) -> float:
        return to_usd(self.amount_paid, self.payment_currency)

    @property
    def is_self(self) -> bool:
        return self.src == self.dst

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "ts": self.ts.strftime("%Y-%m-%d %H:%M"),
            "from": self.src,
            "to": self.dst,
            "amount_paid": round(self.amount_paid, 2),
            "payment_currency": self.payment_currency,
            "amount_received": round(self.amount_received, 2),
            "receiving_currency": self.receiving_currency,
            "payment_format": self.payment_format,
            "usd": round(self.usd, 2),
            "seen_in": sorted(self.seen_in),
            "query_ids": sorted(self.query_ids),
        }


@dataclass
class Observation:
    step: int
    tool: str
    args: dict[str, Any]
    summary: str
    data: dict[str, Any]
    query_ids: list[str]
    error: str | None = None

    def for_model(self, max_chars: int = 3500) -> str:
        """Compact text the model sees: summary, evidence ids, then the data."""
        head = f"[{self.tool}] evidence: {', '.join(self.query_ids) or 'none'}\n{self.summary}"
        if self.error:
            return f"[{self.tool}] ERROR: {self.error}"
        body = json.dumps(self.data, default=str, ensure_ascii=False)
        if len(head) + len(body) > max_chars:
            body = body[: max(0, max_chars - len(head) - 20)] + " ...(truncated)"
        return f"{head}\n{body}"


class CaseState:
    def __init__(self, case_id: str, alert: dict[str, Any]) -> None:
        self.case_id = case_id
        self.alert = dict(alert)
        self.subject = key(alert["bank_id"], alert["account"])
        self.edges: dict[str, Edge] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.observations: list[Observation] = []
        self.signals: list[dict[str, Any]] = []
        self.limitations: list[str] = []
        self.started_at = datetime.now()

    # -- building up ---------------------------------------------------------

    def add_transactions(self, records: list[dict[str, Any]], query_id: str) -> int:
        """Add ledger rows (from either institution) to the graph; returns how many were new."""
        added = 0
        for row in records:
            tx_id = str(row["tx_id"])
            seen = str(row.get("seen_in") or "")
            edge = self.edges.get(tx_id)
            if edge is None:
                ts = to_datetime(row["ts"])
                if ts is None:
                    continue
                edge = Edge(
                    tx_id=tx_id,
                    ts=ts,
                    src=key(row["from_bank"], row["from_account"]),
                    dst=key(row["to_bank"], row["to_account"]),
                    amount_paid=float(row["amount_paid"] or 0.0),
                    payment_currency=str(row["payment_currency"]),
                    amount_received=float(row["amount_received"] or 0.0),
                    receiving_currency=str(row["receiving_currency"]),
                    payment_format=str(row["payment_format"]),
                )
                self.edges[tx_id] = edge
                added += 1
            if seen:
                edge.seen_in.add(seen)
            edge.query_ids.add(query_id)
        return added

    def add_account(self, account_key: str, info: dict[str, Any]) -> None:
        current = self.accounts.setdefault(account_key, {})
        current.update({k: v for k, v in info.items() if v is not None})

    def institution_of(self, account_key: str) -> str | None:
        return self.accounts.get(account_key, {}).get("institution")

    def add_observation(self, tool: str, args: dict[str, Any], summary: str, data: dict[str, Any],
                        query_ids: list[str], error: str | None = None) -> Observation:
        obs = Observation(len(self.observations) + 1, tool, args, summary, data, query_ids, error)
        self.observations.append(obs)
        return obs

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    # -- reading -------------------------------------------------------------

    def subject_edges(self, direction: str) -> list[Edge]:
        if direction == "out":
            return [e for e in self.edges.values() if e.src == self.subject and not e.is_self]
        return [e for e in self.edges.values() if e.dst == self.subject and not e.is_self]

    def all_query_ids(self) -> list[str]:
        seen: list[str] = []
        for obs in self.observations:
            for qid in obs.query_ids:
                if qid not in seen:
                    seen.append(qid)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "alert": self.alert,
            "subject": self.subject,
            "accounts": self.accounts,
            "edges": [e.to_dict() for e in sorted(self.edges.values(), key=lambda e: (e.ts, e.tx_id))],
            "observations": [
                {"step": o.step, "tool": o.tool, "args": o.args, "summary": o.summary, "data": o.data,
                 "query_ids": o.query_ids, "error": o.error}
                for o in self.observations
            ],
            "signals": self.signals,
            "limitations": self.limitations,
            "started_at": self.started_at.isoformat(timespec="seconds"),
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.case_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        return path


def fmt_usd(value: float) -> str:
    return f"${value:,.0f}"


def fmt_ts(value: Any) -> str:
    return to_iso(value) or ""
