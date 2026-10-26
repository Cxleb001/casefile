"""Pure helpers for the Streamlit app (kept separate so they can be tested without Streamlit)."""

from __future__ import annotations

import html
import re
from typing import Any

CURRENCY = {
    "US Dollar": "USD", "Euro": "EUR", "UK Pound": "GBP", "Yuan": "CNY", "Yen": "JPY", "Rupee": "INR",
    "Ruble": "RUB", "Canadian Dollar": "CAD", "Australian Dollar": "AUD", "Swiss Franc": "CHF",
    "Mexican Peso": "MXN", "Brazil Real": "BRL", "Shekel": "ILS", "Saudi Riyal": "SAR", "Bitcoin": "BTC",
}
INSTITUTION_COLOUR = {"A": "#1d4ed8", "B": "#c2410c"}
FLAG_COLOUR = "#dc2626"


def short(account_key: str) -> str:
    bank, _, acct = account_key.partition(":")
    return f"{bank}:{acct[:6]}…" if len(acct) > 7 else account_key


def _q(text: Any) -> str:
    """Quote a DOT string. Backslashes are left alone on purpose: labels use \\n for line breaks."""
    return '"' + str(text).replace('"', '\\"') + '"'


def flagged_tx_ids(report_or_case: dict[str, Any]) -> set[str]:
    signals = report_or_case.get("signals", [])
    return {t for s in signals if s.get("assessment") == "suspicious" and s.get("strength", 0) >= 2
            for t in s.get("tx_ids", [])}


def flow_dot(case: dict[str, Any], max_direct: int = 10, include_flagged: bool = True) -> str:
    """Graphviz DOT of the money flow: every flagged edge and/or the subject's biggest direct flows."""
    subject = case["subject"]
    accounts = case.get("accounts", {})
    edges = case.get("edges", [])
    flagged = flagged_tx_ids(case)
    direct = [e for e in edges if subject in (e["from"], e["to"]) and e["from"] != e["to"]]
    direct.sort(key=lambda e: -float(e.get("usd") or 0))
    chosen = {e["tx_id"]: e for e in edges if e["tx_id"] in flagged} if include_flagged else {}
    if not include_flagged:
        flagged = set()
    for e in direct[:max_direct]:
        chosen.setdefault(e["tx_id"], e)

    nodes = {subject} | {e["from"] for e in chosen.values()} | {e["to"] for e in chosen.values()}
    flagged_nodes = {e["from"] for e in chosen.values() if e["tx_id"] in flagged} | \
                    {e["to"] for e in chosen.values() if e["tx_id"] in flagged}
    layout = 'layout=circo; mindist=1.4;' if (include_flagged and not max_direct and len(nodes) >= 3) else 'rankdir=LR;'
    lines = ["digraph flow {", f'{layout} bgcolor="transparent"; splines=true; nodesep=0.25; ranksep=0.6;',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=14, margin="0.15,0.06", color="#94a3b8", '
             'fillcolor="#f8fafc", fontcolor="#0f172a"];',
             'edge [fontname="Helvetica", fontsize=12, color="#94a3b8", fontcolor="#475569", arrowsize=0.7];']
    hub_cut = next((float(s["value"]["threshold"]) for s in case.get("signals", [])
                    if s.get("name") == "hubs" and isinstance(s.get("value"), dict)), 50.0)
    for node in sorted(nodes):
        info = accounts.get(node, {})
        inst = info.get("institution") or "?"
        colour = INSTITUTION_COLOUR.get(inst, "#64748b")
        degree = info.get("degree")
        label = f"{short(node)}\\n{inst} · {info.get('customer_type') or 'unknown'}"
        if degree is not None and degree >= hub_cut and node != subject:
            label += f"\\nhub: {degree} counterparties"
        attrs = [f"label={_q(label)}", f'color="{colour}"']
        if node == subject:
            attrs += ['penwidth=3', 'fillcolor="#fef3c7"']
        elif node in flagged_nodes:
            attrs += ['penwidth=2', f'fillcolor="#fee2e2"']
        lines.append(f"{_q(node)} [{', '.join(attrs)}];")
    for e in sorted(chosen.values(), key=lambda e: e["ts"]):
        cur = CURRENCY.get(e.get("payment_currency"), e.get("payment_currency", ""))
        label = f"{float(e['amount_paid']):,.0f} {cur}\\n{e['ts'][5:16]}"
        attrs = [f"label={_q(label)}"]
        if e["tx_id"] in flagged:
            attrs += [f'color="{FLAG_COLOUR}"', "penwidth=2", f'fontcolor="{FLAG_COLOUR}"']
        if len(e.get("seen_in", [])) == 2:
            attrs += ['style="dashed"']
        lines.append(f"{_q(e['from'])} -> {_q(e['to'])} [{', '.join(attrs)}];")
    lines.append("}")
    return "\n".join(lines)


def status_badge(status: str) -> str:
    return {"verified": "✅ verified", "checked": "☑️ checked", "rejected": "❌ rejected"}.get(status, "❔ unverified")


# ---------------------------------------------------------------- case view helpers

_ACCOUNT_KEY = re.compile(r"\b\d{1,6}:[0-9A-Za-z]{6,}\b")


def accounts_at(case: dict[str, Any], institution: str) -> set[str]:
    """Account keys ("bank:account") the case knows to be at an institution."""
    return {k for k, v in case.get("accounts", {}).items() if v.get("institution") == institution}


def bank_b_summary(case: dict[str, Any]) -> dict[str, int]:
    """What Institution B's data added to this case's money trail."""
    edges = case.get("edges", [])
    return {
        "b_only_tx": sum(1 for e in edges if (e.get("seen_in") or []) == ["B"]),
        "cross_tx": sum(1 for e in edges if set(e.get("seen_in") or []) == {"A", "B"}),
        "b_accounts": len(accounts_at(case, "B")),
        "tx": len(edges),
    }


def top_reasons(report: dict[str, Any], n: int = 3) -> list[str]:
    """The first claims that survived verification, in the order the report gives them."""
    kept = [c for c in report.get("claims", []) if c.get("verdict", {}).get("status") in {"verified", "checked"}]
    return [c["text"] for c in kept[:n]]


def mark_accounts(text: str, case: dict[str, Any]) -> str:
    """HTML-escape text and tag each account by institution, so Bank B's accounts stand out."""
    b, a = accounts_at(case, "B"), accounts_at(case, "A")
    out, last = [], 0
    for m in _ACCOUNT_KEY.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        key = m.group(0)
        cls = "cf-acct cf-acct-b" if key in b else "cf-acct cf-acct-a" if key in a else "cf-acct"
        title = " title=\"Institution B account\"" if key in b else ""
        out.append(f'<span class="{cls}"{title}>{html.escape(key)}</span>')
        last = m.end()
    out.append(html.escape(text[last:]))
    # "$" as an entity: Streamlit's markdown would otherwise read "$92k ... $280k" as a maths formula.
    return "".join(out).replace("$", "&#36;")


def query_source(sql: str, tables: dict[str, str]) -> str:
    """Which institution's data a query read, from the registered table names in its SQL."""
    def uses(*keys: str) -> bool:
        return any(re.search(rf"(?<![\w.]){re.escape(tables[k])}(?![\w.])", sql or "") for k in keys if k in tables)
    a, b = uses("ledger_a", "kyc_a"), uses("ledger_b", "kyc_b")
    if a and b:
        return "Banks A + B"
    if b:
        return "Bank B"
    if a:
        return "Bank A"
    return "Compliance" if uses("watchlist") else "Other"


def b_row_flags(rows: list[list[Any]], columns: list[str], b_accounts: set[str]) -> list[bool]:
    """Rows that show Institution B's side: seen only in B's ledger, or naming a B account."""
    b_ids = {k.split(":", 1)[1] for k in b_accounts}
    seen = columns.index("seen_in") if "seen_in" in columns else None
    flags = []
    for row in rows:
        only_b = seen is not None and str(row[seen]).strip("[]'\" ") == "B"
        flags.append(only_b or any(isinstance(v, str) and v in b_ids for v in row))
    return flags


def engine_outcome(engine: dict[str, Any]) -> tuple[str, str]:
    """A plain label for how a Markovian run ended, and a one-line reason."""
    status = engine.get("status")
    if status == "completed":
        return "Finished", "The planner decided it had tested enough."
    if status == "max-chunks":
        return "All steps used", "The planner used all the follow-ups it was allowed."
    error = str(engine.get("error") or "")
    reason = "the planner's reply was cut off or was not valid JSON" if "JSON" in error else (error[:120] or "an error")
    return "Stopped early", f"Stopped because {reason}; the decision rests on the evidence gathered so far."


def inbox_rows(alerts: list[dict[str, Any]], reports: dict[str, dict[str, Any]],
               analyst: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One row per alert for the start page, with the latest saved decision and the analyst's call, if any."""
    rows = []
    for alert in alerts:
        report = reports.get(alert["alert_id"])
        if report:
            v = report.get("verification", {})
            status = report["disposition"].capitalize() + (", needs review" if report.get("needs_review") else "")
            typology = (report.get("typology") or "NONE").replace("NONE", "–").capitalize()
            claims = f"{v.get('verified', 0) + v.get('checked', 0)} of {v.get('total', 0)}"
        else:
            status, typology, claims = "Not investigated", "", ""
        rows.append({"Alert": alert["alert_id"], "Account": f"{alert['bank_id']}:{alert['account']}",
                     "Raised": alert.get("alert_ts", ""), "Rule": alert["rules"].replace("|", ", ").replace("_", " ").lower(),
                     "Decision": status, "Typology": typology, "Claims verified": claims,
                     "Analyst": (analyst or {}).get(alert["alert_id"], "")})
    return rows
