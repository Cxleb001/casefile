"""Casefile: the analyst's view. Run with:  streamlit run app.py"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from casefile.agent.pipeline import CASES_DIR, load_alerts, run_case  # noqa: E402
from casefile.config import get_settings  # noqa: E402
from casefile.data import get_backend  # noqa: E402
from casefile.evidence import EvidenceStore  # noqa: E402
from casefile.ui_helpers import flow_dot, status_badge  # noqa: E402

st.set_page_config(page_title="Casefile", page_icon="🗂️", layout="wide")
st.markdown(
    """
<style>
  .block-container {padding-top: 1.6rem; max-width: 1400px;}
  .cf-badge {display:inline-block; padding:4px 12px; border-radius:999px; font-weight:700; font-size:0.95rem;}
  .cf-esc {background:#fee2e2; color:#991b1b;} .cf-close {background:#dcfce7; color:#166534;}
  .cf-review {background:#fef3c7; color:#92400e;}
  .cf-muted {color:#64748b; font-size:0.85rem;}
  .cf-claim {padding:6px 0 2px 0;} .cf-rejected {text-decoration:line-through; color:#94a3b8;}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def resources():
    settings = get_settings()
    backend = get_backend(settings)
    store = EvidenceStore()
    llm, llm_error = None, None
    try:
        from casefile.llm import LLM

        llm = LLM(settings, on_call=store.record_llm_call)
    except Exception as error:  # no key yet: rules-only mode still works
        llm_error = str(error)
    return settings, backend, store, llm, llm_error


settings, backend, store, llm, llm_error = resources()


def saved_cases() -> list[str]:
    if not CASES_DIR.exists():
        return []
    return sorted((p.name[: -len(".report.json")] for p in CASES_DIR.glob("*.report.json")), reverse=True)


def load_saved(case_id: str) -> dict:
    case = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    report = json.loads((CASES_DIR / f"{case_id}.report.json").read_text(encoding="utf-8"))
    sar = (CASES_DIR / f"{case_id}.sar.md").read_text(encoding="utf-8")
    return {"case": case, "report": report, "sar": sar, "case_id": case_id}


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("## 🗂️ Casefile")
    st.caption("Follows the money across institutions through Zetaris; every claim cites the query behind it.")
    st.markdown(f"**Data:** `{settings.data_backend}`" + (" (Zetaris federation)" if settings.data_backend == "zetaris"
                                                           else " (local stand-in)"))
    if llm is not None:
        st.markdown(f"**Models** ({settings.llm_provider})")
        for step, model in settings.models.items():
            st.caption(f"{step}: `{model}`")
    else:
        st.warning("No model key set: rules-only decisions. Add a key to `.env` for the full agent.")

    alerts = load_alerts()
    labels = [f"{a['alert_id']} · {a['bank_id']}:{a['account']} · {a['rules'].replace('|', ', ')}" for a in alerts]
    choice = st.selectbox("Alert queue", range(len(alerts)), format_func=lambda i: labels[i])
    use_model = st.toggle("Use the model", value=llm is not None, disabled=llm is None)
    steps = st.slider("Follow-up actions", 0, 10, 6, disabled=not use_model)
    run = st.button("Investigate", type="primary", width="stretch")

    st.divider()
    previous = saved_cases()
    picked = st.selectbox("Or open a saved case", [""] + previous)

# ---------------------------------------------------------------- run or load

if run:
    alert = alerts[choice]
    st.markdown(f"### Investigating {alert['alert_id']} · `{alert['bank_id']}:{alert['account']}`")
    with st.status("Running the standard workup…", expanded=True) as status:
        def on_event(event: dict) -> None:
            kind = event["type"]
            if kind == "stage":
                status.update(label={"workup": "Running the standard workup…", "investigation": "Testing explanations…",
                                     "report": "Writing the decision…", "verify": "Verifying every claim…"}
                              .get(event["stage"], event["stage"]))
            elif kind == "observation":
                obs = event["observation"]
                st.write(f"**{obs.tool}** · " + (obs.error or obs.summary.split("\n")[0])[:220])
            elif kind == "action":
                st.write(f"🧭 *{event.get('thought', '')}* → `{event['action']}`")
            elif kind == "finish":
                st.write(f"🧭 *{event.get('thought', '')}*")

        result = run_case(alert, backend=backend, store=store, settings=settings,
                          llm=llm if use_model else None, max_steps=steps, on_event=on_event)
        status.update(label=f"Done in {result['report']['timing']['seconds']}s", state="complete", expanded=False)
    st.session_state["open_case"] = result["paths"]["case_id"]
elif picked:
    st.session_state["open_case"] = picked

case_id = st.session_state.get("open_case")
if not case_id:
    st.markdown("# Casefile")
    st.markdown("Pick an alert and press **Investigate**. Casefile profiles the account, follows the money across "
                "Institution A and Institution B through Zetaris, tests innocent explanations, and writes a "
                "decision where every claim links to the exact query and rows behind it.")
    st.stop()

data = load_saved(case_id)
case, report = data["case"], data["report"]

# ---------------------------------------------------------------- header

disposition = report["disposition"]
badge = f'<span class="cf-badge {"cf-esc" if disposition == "ESCALATE" else "cf-close"}">{disposition}</span>'
if report.get("needs_review"):
    badge += ' <span class="cf-badge cf-review">NEEDS REVIEW</span>'
st.markdown(f"## {case['alert']['alert_id']} · `{case['subject']}` &nbsp; {badge}", unsafe_allow_html=True)
st.markdown(f"**{report.get('headline', '')}**")
if report.get("decision_basis"):
    measured = report.get("measured", {})
    signals = ", ".join(measured.get("signals", [])) or "none"
    (st.warning if report.get("needs_review") else st.info)(
        f"{report['decision_basis']}  \nMeasured structure: **{measured.get('disposition', '?')}** "
        f"(score {measured.get('score', '?')}; signals: {signals}) · model leaned **{report.get('model_disposition', '?')}**")
v = report.get("verification", {})
cols = st.columns(6)
cols[0].metric("Typology", report.get("typology", "NONE"))
cols[1].metric("Confidence", f"{report.get('confidence', 0):.0%}")
cols[2].metric("Claims verified", f"{v.get('verified', 0) + v.get('checked', 0)}/{v.get('total', 0)}")
cols[3].metric("Rejected by verifier", v.get("rejected", 0))
cols[4].metric("Queries", report.get("timing", {}).get("queries", 0))
cols[5].metric("Time", f"{report.get('timing', {}).get('seconds', 0)}s")
st.caption(f"Alert raised {case['alert'].get('alert_ts')} by {case['alert'].get('rules')}: {case['alert'].get('trigger')} · "
           f"written by `{report.get('author', {}).get('model', '?')}` · data via `{report.get('backend', '?')}`")


def claim_terms(text: str) -> set[str]:
    """Account ids and transaction ids a claim mentions, to pick out the rows that show it."""
    terms = {m.split(":")[1] for m in re.findall(r"\b\d{1,6}:[0-9A-Za-z]{4,}\b", text)}
    terms |= set(re.findall(r"\bT[0-9a-f]{16}\b", text))
    return terms


def show_evidence(qid: str, terms: set[str] | None = None) -> None:
    record = store.get_query(qid)
    if not record:
        st.caption(f"{qid}: not in this evidence store")
        return
    st.markdown(f"**{qid}** · {record['purpose']} · {record['row_count']} rows · {record['backend']} · "
                f"{record['duration_ms']} ms")
    st.code(record["sql"], language="sql")
    if not record["rows"]:
        return
    frame = pd.DataFrame(record["rows"], columns=record["columns"])
    if terms:
        mask = frame.astype(str).isin(terms).any(axis=1)
        if mask.any():
            st.caption(f"{int(mask.sum())} row(s) mention this claim's accounts:")
            st.dataframe(frame[mask].head(30), width="stretch", hide_index=True)
            with st.popover(f"All {len(frame)} rows"):
                st.dataframe(frame.head(200), width="stretch", hide_index=True)
            return
    st.dataframe(frame.head(50), width="stretch", hide_index=True)


tab_findings, tab_flow, tab_trace, tab_evidence, tab_sar = st.tabs(
    ["Findings", "Money flow", "Investigation trace", "Evidence", "SAR draft"])

with tab_findings:
    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Findings")
        for claim in report.get("claims", []):
            verdict = claim.get("verdict", {})
            rejected = verdict.get("status") == "rejected"
            css = "cf-claim cf-rejected" if rejected else "cf-claim"
            st.markdown(f'<div class="{css}"><b>{claim["id"]}</b> <span class="cf-muted">({claim["kind"]})</span> '
                        f'{claim["text"]}</div>', unsafe_allow_html=True)
            label = f"{status_badge(verdict.get('status', ''))} · evidence {', '.join(claim['evidence']) or 'none'}"
            with st.expander(label, expanded=False):
                st.caption(f"Verifier ({verdict.get('layer', '?')}): {verdict.get('reason', '')}")
                terms = claim_terms(claim["text"])
                for qid in claim["evidence"][:4]:
                    show_evidence(qid, terms)
        if report.get("counter_evidence"):
            st.markdown("#### Evidence against")
            for c in report["counter_evidence"]:
                st.markdown(f"- {c['text']}  \n  <span class='cf-muted'>{status_badge(c.get('verdict', {}).get('status', ''))}"
                            f" · {', '.join(c['evidence'])}</span>", unsafe_allow_html=True)
    with right:
        ref = report.get("sanctions_referral", {})
        st.markdown("#### Sanctions referral")
        (st.error if ref.get("needed") else st.success)(ref.get("reason") or ("Refer" if ref.get("needed") else "Not needed"))
        if report.get("counterparty_watchlist"):
            st.warning("Counterparty matches for the sanctions team:  \n" + "  \n".join(
                f"`{m['account']}` {m['match']} ({m['list_id']})" for m in report["counterparty_watchlist"]))
        if report.get("would_change_my_mind"):
            st.markdown("#### What would change this decision")
            for x in report["would_change_my_mind"]:
                st.markdown(f"- {x}")
        st.markdown("#### 🔒 What I could not check")
        for x in report.get("limitations", []) or ["Nothing withheld."]:
            st.markdown(f"- {x}")
        st.markdown("#### Signals")
        for s in case.get("signals", []):
            icon = {"suspicious": "🔴", "benign": "🟢"}.get(s["assessment"], "⚪") if s["strength"] else "⚪"
            st.markdown(f"{icon} **{s['name']}** · {s['finding']}")

with tab_flow:
    st.caption("Blue border = Institution A, orange = Institution B. Dashed edges cross between institutions "
               "(the transfer is in both ledgers).")
    has_pattern = any(s["assessment"] == "suspicious" and s["strength"] >= 2 and s.get("tx_ids")
                      for s in case.get("signals", []))
    if has_pattern:
        st.markdown("#### The pattern behind the decision")
        st.graphviz_chart(flow_dot(case, max_direct=0), width="stretch")
        with st.expander("The subject's largest direct flows"):
            st.graphviz_chart(flow_dot(case, max_direct=12, include_flagged=False), width="stretch")
    else:
        st.markdown("#### The subject's largest direct flows")
        st.graphviz_chart(flow_dot(case, max_direct=12, include_flagged=False), width="stretch")

with tab_trace:
    st.markdown("#### Standard workup and follow-ups")
    for obs in case.get("observations", []):
        title = f"#{obs['step']} {obs['tool']} " + (json.dumps({k: v for k, v in obs["args"].items() if v is not None})
                                                   if obs["args"] else "")
        with st.expander(title, expanded=False):
            st.text(obs.get("error") or obs["summary"])
            st.caption("evidence: " + ", ".join(obs["query_ids"]))
    plan = report.get("investigation", {}).get("steps", [])
    if plan:
        st.markdown("#### Planner reasoning")
        for step in plan:
            st.markdown(f"**{step['step']}.** *{step.get('thought', '')}* → `{step.get('action')}` "
                        f"{json.dumps(step.get('args', {}))}" + (f"  \n⚠️ {step['skipped']}" if step.get("skipped") else ""))

with tab_evidence:
    qids = []
    for obs in case.get("observations", []):
        for q in obs["query_ids"]:
            if q not in qids:
                qids.append(q)
    rows = []
    for q in qids:
        r = store.get_query(q) or {}
        rows.append({"id": q, "purpose": r.get("purpose"), "rows": r.get("row_count"), "ms": r.get("duration_ms"),
                     "backend": r.get("backend")})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    pick = st.selectbox("Show query", qids)
    if pick:
        show_evidence(pick)

with tab_sar:
    st.download_button("Download SAR draft (.md)", data["sar"], file_name=f"{case_id}.sar.md")
    st.markdown(data["sar"].split("## Evidence appendix")[0])
