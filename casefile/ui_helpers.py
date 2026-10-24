"""Pure helpers for the Streamlit app (kept separate so they can be tested without Streamlit)."""

from __future__ import annotations

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
