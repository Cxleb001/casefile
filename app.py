"""Casefile: the analyst's view. Run with:  streamlit run app.py"""

from __future__ import annotations

import html
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
from casefile.replay import alert_of  # noqa: E402
from casefile.ui_helpers import (accounts_at, b_row_flags, bank_b_summary, engine_outcome, flow_dot,  # noqa: E402
                                 inbox_rows, mark_accounts, query_source, status_badge, top_reasons)

st.set_page_config(page_title="Casefile", page_icon="🗂️", layout="wide")

# Colours: ink and paper for reading; Institution A in ledger blue and Institution B in burnt orange everywhere
# (cards, account tags, evidence rows, the money-flow graph); the verdict colours appear only on the stamp.
st.markdown(
    """
<style>
:root { --ink:#18263A; --muted:#5B6B7F; --rule:#D5DCE5; --surface:#FFFFFF;
        --bank-a:#1D4ED8; --bank-b:#C2410C; --bank-b-tint:#FCEDE2; --esc:#B42318; --close:#17663F; --review:#8A5A00; }
.block-container { padding-top: 4rem; max-width: 1320px; }  /* clear of Streamlit's top bar, so nothing sits under it */
[data-testid="stSidebar"] .cf-wordmark { font-family: 'Source Serif 4', Georgia, serif; font-size: 2rem !important;
               font-weight: 700; line-height: 1.1; margin: 0 0 4px; color: var(--ink); }
.cf-lede { color: var(--muted); font-size: .92rem; line-height: 1.45; margin: 2px 0 10px; }
.cf-card { background: var(--surface); border: 1px solid var(--rule); border-left: 6px solid var(--accent, var(--ink));
           border-radius: 6px; padding: 20px 26px 16px; margin: 6px 0 18px; }
.cf-card.esc { --accent: var(--esc); } .cf-card.close { --accent: var(--close); }
.cf-top { display: flex; gap: 28px; align-items: flex-start; justify-content: space-between; }
.cf-caseline { color: var(--muted); font-size: .92rem; margin: 0 0 6px; }
.cf-headline { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.5rem; line-height: 1.32; font-weight: 600;
               color: var(--ink); margin: 0; max-width: 64ch; }
.cf-stamp { flex: none; transform: rotate(-5deg); border: 3px double currentColor; border-radius: 4px;
            padding: 7px 16px 5px; font-family: 'Source Serif 4', Georgia, serif; font-weight: 700; font-size: 1.4rem;
            letter-spacing: .14em; text-transform: uppercase; text-align: center; margin: 6px 6px 0 0; opacity: .9; }
.cf-stamp small { display: block; font-size: .6rem; letter-spacing: .1em; margin-top: 3px; }
.cf-stamp.esc { color: var(--esc); } .cf-stamp.close { color: var(--close); }
.cf-cols { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 28px; margin-top: 18px; }
.cf-cols h4 { font-size: 1.02rem; margin: 0 0 8px; }
.cf-cols ul { margin: 0; padding-left: 1.1em; } .cf-cols li { margin: 0 0 7px; line-height: 1.45; }
.cf-bpanel { background: var(--bank-b-tint); border-radius: 6px; padding: 12px 16px; }
.cf-bpanel p { margin: 0 0 7px; line-height: 1.45; } .cf-bpanel p:last-child { margin: 0; }
.cf-facts { display: flex; flex-wrap: wrap; gap: 10px 36px; margin: 18px 0 0; padding-top: 14px; border-top: 1px solid var(--rule); }
.cf-facts dt { color: var(--muted); font-size: .8rem; } .cf-facts dd { margin: 0; font-weight: 600; font-size: 1.04rem; }
.cf-basis { margin: 12px 0 0; font-size: .9rem; color: var(--muted); line-height: 1.45; }
.cf-basis.review { color: var(--review); }
.cf-acct { font-family: 'IBM Plex Mono', ui-monospace, monospace; font-size: .86em; padding: 0 4px; border-radius: 3px;
           background: #EEF2F7; white-space: nowrap; }
.cf-acct-a { box-shadow: inset 0 -2px 0 var(--bank-a); }
.cf-acct-b { background: var(--bank-b-tint); color: #8A2E07; box-shadow: inset 0 -2px 0 var(--bank-b); }
.cf-claim { padding: 12px 0 4px; line-height: 1.5; }
.cf-claim .cf-id { font-weight: 700; margin-right: 6px; } .cf-claim .cf-kind { color: var(--muted); font-size: .8rem; margin-right: 6px; }
.cf-rejected { color: #7C8899; } .cf-rejected .cf-text { text-decoration: line-through; }
.cf-struck { color: var(--esc); font-size: .88rem; margin-top: 4px; text-decoration: none; }
.cf-note { color: var(--muted); font-size: .86rem; }
.cf-redact { display: inline-block; width: 4.2em; height: .78em; background: var(--ink); border-radius: 2px;
             vertical-align: middle; margin: 0 3px; }
.cf-tag-b { color: #8A2E07; background: var(--bank-b-tint); border-radius: 3px; padding: 1px 6px; font-size: .8rem; }
@media (max-width: 900px) { .cf-top { flex-direction: column; } .cf-cols { grid-template-columns: 1fr; } }
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
        from casefile.llm import make_llm

        llm = make_llm(settings, on_call=store.record_llm_call)  # sample mode: the recorded answers
    except Exception as error:  # no key yet: rules-only mode still works
        llm_error = str(error)
    return settings, backend, store, llm, llm_error


settings, backend, store, llm, llm_error = resources()
state = st.session_state


def saved_cases() -> list[str]:
    if not CASES_DIR.exists():
        return []
    return sorted((p.name[: -len(".report.json")] for p in CASES_DIR.glob("*.report.json")), reverse=True)


def load_saved(case_id: str) -> dict:
    case = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    report = json.loads((CASES_DIR / f"{case_id}.report.json").read_text(encoding="utf-8"))
    sar = (CASES_DIR / f"{case_id}.sar.md").read_text(encoding="utf-8")
    return {"case": case, "report": report, "sar": sar, "case_id": case_id}


def latest_by_alert(case_ids: list[str]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for case_id in case_ids:  # newest first
        latest.setdefault(alert_of(case_id), case_id)
    return latest


def open_case(case_id: str | None) -> None:
    state["open_case"] = case_id


# The analyst's own call on a case, kept next to the case file. Casefile drafts; a person decides.
ANALYST_ACTIONS = {
    "file_sar": "SAR filed", "close": "Closed", "override_close": "Closed, overriding Casefile",
    "override_escalate": "Escalated, overriding Casefile", "more_info": "More information requested",
}


def analyst_path(case_id: str) -> Path:
    return CASES_DIR / f"{case_id}.analyst.json"


def load_analyst(case_id: str) -> dict | None:
    try:
        return json.loads(analyst_path(case_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_analyst(case_id: str, action: str) -> None:
    from datetime import datetime

    record = {"action": action, "label": ANALYST_ACTIONS[action], "note": state.get(f"note-{case_id}", "").strip(),
              "at": datetime.now().isoformat(timespec="minutes")}
    analyst_path(case_id).write_text(json.dumps(record, indent=2), encoding="utf-8")
    st.toast(record["label"])


def clear_analyst(case_id: str) -> None:
    analyst_path(case_id).unlink(missing_ok=True)


def back_to_inbox() -> None:
    state["open_case"] = None
    state["picked"] = ""
    state["inbox_key"] = state.get("inbox_key", 0) + 1  # clears the table's selection


# ---------------------------------------------------------------- sidebar

alerts = load_alerts()
with st.sidebar:
    st.markdown('<p class="cf-wordmark">Casefile</p>', unsafe_allow_html=True)
    st.markdown('<p class="cf-lede">Follows the money from Institution A into Institution B through Zetaris, and '
                'cites the query behind every claim.</p>', unsafe_allow_html=True)
    if settings.sample_mode:
        st.info("**Sample mode.** A slice of the real data, with the model answers recorded from a run through "
                "Meterless Nightshift, replayed in order. No Zetaris, Nightshift or keys needed.")
    data_label = {"zetaris": "Zetaris, federating both banks"}.get(settings.data_backend,
                                                                    "bundled sample (DuckDB)" if settings.sample_mode
                                                                    else "local stand-in (DuckDB)")
    st.markdown(f"**Data:** {data_label}")
    if llm is not None:
        st.markdown("**Models** (recorded through Nightshift)" if settings.sample_mode
                    else "**Models** (through Meterless Nightshift)" if settings.llm_provider == "meterless"
                    else f"**Models** ({settings.llm_provider})")
        models = llm.models if settings.sample_mode else settings.models
        for step in ("plan", "report", "verify"):
            if step in models:
                st.caption(f"{step}: `{models[step]}`")
    else:
        st.warning("No model connected, so decisions are rules-only. Set `METERLESS_TOKEN` in `.env` "
                   "(or run with `SAMPLE_MODE=true`).")

    labels = [f"{a['alert_id']}  {a['bank_id']}:{a['account']}" for a in alerts]
    choice = st.selectbox("Alert", range(len(alerts)), format_func=lambda i: labels[i])
    use_model = st.toggle("Use the model", value=llm is not None, disabled=llm is None)
    # A replay must allow the same number of follow-ups as the recorded run (run_eval's default, 8).
    steps = st.slider("Follow-up actions", 0, 10, 8 if settings.sample_mode else 6,
                      disabled=not use_model or settings.sample_mode)
    run = st.button("Investigate", type="primary", width="stretch")

    st.divider()
    previous = saved_cases()
    st.selectbox("Open a saved case", [""] + previous, key="picked",
                 on_change=lambda: open_case(state.get("picked") or None))

# ---------------------------------------------------------------- run, inbox or case

queued = state.pop("run_alert", None)  # an alert the inbox asked to investigate
run_alert = alerts[choice] if run else next((a for a in alerts if a["alert_id"] == queued), None)
if run_alert:
    st.markdown(f"### Investigating {run_alert['alert_id']}")
    with st.status("Running the standard workup…", expanded=True) as status:
        def on_event(event: dict) -> None:
            kind = event["type"]
            if kind == "stage":
                status.update(label={"workup": "Running the standard workup…", "investigation": "Testing explanations…",
                                     "report": "Writing the decision…", "verify": "Verifying every claim…"}
                              .get(event["stage"], event["stage"]))
            elif kind == "observation":
                obs = event["observation"]
                st.write(f"**{obs.tool}**: " + (obs.error or obs.summary.split("\n")[0])[:220])
            elif kind == "action":
                st.write(f"🧭 *{event.get('thought', '')}* asks `{event['action']}`")
            elif kind == "finish":
                st.write(f"🧭 *{event.get('thought', '')}*")

        result = run_case(run_alert, backend=backend, store=store, settings=settings,
                          llm=llm if use_model else None, max_steps=steps, on_event=on_event)
        status.update(label=f"Done in {result['report']['timing']['seconds']} s", state="complete", expanded=False)
    open_case(result["paths"]["case_id"])
    previous = saved_cases()

case_id = state.get("open_case")
if not case_id:
    latest = latest_by_alert(previous)
    reports = {}
    for alert_id, cid in latest.items():
        try:
            reports[alert_id] = json.loads((CASES_DIR / f"{cid}.report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    done = sum(1 for a in alerts if a["alert_id"] in reports)
    st.title("Alert inbox")
    st.markdown(f"{len(alerts)} alerts from Institution A's transaction monitoring, {done} investigated. Click a row "
                "to open its case file, or to investigate it: Casefile profiles the account, follows the money into "
                "Institution B, tests innocent explanations, and writes a decision in which every claim cites the "
                "query and rows behind it.")
    calls = {alert_id: (load_analyst(cid) or {}).get("label", "") for alert_id, cid in latest.items()}
    frame = pd.DataFrame(inbox_rows(alerts, reports, calls))
    event = st.dataframe(
        frame, hide_index=True, width="stretch", height=min(40 + 35 * len(frame), 1400),  # the whole queue, no inner scroll
        on_select="rerun", selection_mode=["single-row", "single-cell"], key=f"inbox-{state.get('inbox_key', 0)}",
        column_config={"Alert": st.column_config.TextColumn(width=80),
                       "Account": st.column_config.TextColumn(width=150),
                       "Raised": st.column_config.TextColumn(width=130),
                       "Rule": st.column_config.TextColumn(width=110),
                       "Decision": st.column_config.TextColumn(width=150),
                       "Typology": st.column_config.TextColumn(width=110),
                       "Claims verified": st.column_config.TextColumn("Verified", width=75),
                       "Analyst": st.column_config.TextColumn("Analyst's call", width=170)})
    picked = event.selection if event else None  # a row's box, or any cell in the row
    rows = (list(picked.rows) or [cell[0] for cell in getattr(picked, "cells", [])]) if picked else []
    if rows:
        alert = alerts[rows[0]]
        if alert["alert_id"] in latest:
            open_case(latest[alert["alert_id"]])
            st.rerun()
        st.markdown(f"**{alert['alert_id']}** has not been investigated yet. {alert['trigger'].capitalize()}.")
        if st.button(f"Investigate {alert['alert_id']}", type="primary"):
            state["run_alert"] = alert["alert_id"]
            st.rerun()
    st.stop()

data = load_saved(case_id)
case, report = data["case"], data["report"]
b_accounts = accounts_at(case, "B")

# ---------------------------------------------------------------- decision card

st.button("Back to the inbox", on_click=back_to_inbox)
disposition = report["disposition"]
tone = "esc" if disposition == "ESCALATE" else "close"
alert = case["alert"]
rules = alert.get("rules", "").replace("|", ", ").replace("_", " ").lower()
subject_inst = (case.get("accounts", {}).get(case["subject"], {}).get("institution") or "A")
reasons = "".join(f"<li>{mark_accounts(r, case)}</li>" for r in top_reasons(report)) or \
    "<li>No claim survived verification; see the findings below.</li>"
b = bank_b_summary(case)
if b["b_accounts"] or b["b_only_tx"]:
    b_html = (f"<p><b>{b['b_only_tx']}</b> of the {b['tx']} transactions traced are visible only in Institution B's "
              f"ledger, across <b>{b['b_accounts']}</b> Institution B accounts; <b>{b['cross_tx']}</b> transfers cross "
              f"between the banks.</p>")
else:
    b_html = "<p>No Institution B data was used in this case.</p>"
b_html += ('<p>Institution B shares its customers without names <span class="cf-redact"></span> birth dates '
           '<span class="cf-redact"></span> or ID numbers <span class="cf-redact"></span>. Casefile works from its '
           'governed view and says what that view kept it from checking.</p>')
v = report.get("verification", {})
seconds = report.get("timing", {}).get("seconds", 0)
facts = [("Typology", (report.get("typology") or "NONE").replace("NONE", "None found").capitalize()),
         ("Confidence", f"{report.get('confidence', 0):.0%}"),
         ("Claims verified", f"{v.get('verified', 0) + v.get('checked', 0)} of {v.get('total', 0)}"),
         ("Struck out by the verifier", str(v.get("rejected", 0))),
         ("Queries", str(report.get("timing", {}).get("queries", 0))),
         ("Time", f"{int(seconds // 60)} min {int(seconds % 60)} s" if seconds >= 60 else f"{seconds:.0f} s")]
facts_html = "".join(f"<div><dt>{k}</dt><dd>{html.escape(val)}</dd></div>" for k, val in facts)
basis = report.get("decision_basis") or ""
stamp_note = "<small>needs review</small>" if report.get("needs_review") else ""
st.markdown(f"""
<div class="cf-card {tone}">
  <div class="cf-top">
    <div>
      <p class="cf-caseline">{html.escape(alert['alert_id'])}: account {mark_accounts(case['subject'], case)} at
        Institution {subject_inst}, flagged {html.escape(str(alert.get('alert_ts', '')))} by the {html.escape(rules)} rule</p>
      <p class="cf-headline">{html.escape(report.get('headline', ''))}</p>
    </div>
    <div class="cf-stamp {tone}">{html.escape(disposition.lower())}{stamp_note}</div>
  </div>
  <div class="cf-cols">
    <div><h4>Why</h4><ul>{reasons}</ul></div>
    <div class="cf-bpanel"><h4>What Institution B added</h4>{b_html}</div>
  </div>
  <dl class="cf-facts">{facts_html}</dl>
  {f'<p class="cf-basis {"review" if report.get("needs_review") else ""}">{html.escape(basis)}</p>' if basis else ''}
</div>
""", unsafe_allow_html=True)
st.caption(f"Written by `{report.get('author', {}).get('model', '?')}`, data read through `{report.get('backend', '?')}`. "
           "Accounts at Institution B are marked in orange.")

with st.container(border=True):
    decided = load_analyst(case_id)
    if decided:
        note = f" Note: {decided['note']}" if decided.get("note") else ""
        left, right = st.columns([5, 1], vertical_alignment="center")
        left.markdown(f"**Your decision: {decided['label']}** on {decided['at'].replace('T', ' at ')}.{note}")
        right.button("Change it", key=f"undo-{case_id}", on_click=clear_analyst, args=(case_id,), width="stretch")
    else:
        st.markdown("**Your decision.** Casefile drafts the decision; you make it. "
                    + ("Casefile recommends filing a SAR." if disposition == "ESCALATE" else "Casefile recommends closing."))
        st.text_input("Note for the file (optional)", key=f"note-{case_id}",
                      placeholder="e.g. Confirmed with relationship manager; no trade documents on file")
        if disposition == "ESCALATE":
            options = [("File the SAR", "file_sar", "primary"), ("Close instead", "override_close", "secondary")]
        else:
            options = [("Close the case", "close", "primary"), ("Escalate instead", "override_escalate", "secondary")]
        options.append(("Ask for more information", "more_info", "secondary"))
        cols = st.columns(len(options))
        for col, (label, action, kind) in zip(cols, options):
            col.button(label, key=f"{action}-{case_id}", type=kind, on_click=save_analyst, args=(case_id, action),
                       width="stretch")


def md(text: str) -> str:
    """Plain text for st.markdown: escape "$" so amounts are not read as a maths formula."""
    return str(text).replace("$", "\\$")


def claim_terms(text: str) -> set[str]:
    """Account ids and transaction ids a claim mentions, to pick out the rows that show it."""
    terms = {m.split(":")[1] for m in re.findall(r"\b\d{1,6}:[0-9A-Za-z]{4,}\b", text)}
    terms |= set(re.findall(r"\bT[0-9a-f]{16}\b", text))
    return terms


def styled(frame: pd.DataFrame, flags: list[bool]):
    """Tint the rows that show Institution B's side."""
    marks = pd.Series(flags, index=frame.index)
    return frame.style.apply(lambda row: ["background-color: #FCEDE2" if marks[row.name] else "" for _ in row], axis=1)


def show_evidence(qid: str, terms: set[str] | None = None) -> None:
    record = store.get_query(qid)
    if not record:
        st.caption(f"{qid}: not in this evidence store")
        return
    source = query_source(record["sql"], settings.tables)
    st.markdown(f"**{qid}**: {record['purpose']}. {record['row_count']} rows from {source}, through "
                f"{record['backend']} in {record['duration_ms']} ms")
    st.code(record["sql"], language="sql")
    if not record["rows"]:
        return
    frame = pd.DataFrame(record["rows"], columns=record["columns"])
    flags = b_row_flags(record["rows"], record["columns"], b_accounts)
    if any(flags):
        st.markdown('<span class="cf-tag-b">Rows tinted orange show Institution B\'s side.</span>', unsafe_allow_html=True)
    if terms:
        mask = frame.astype(str).isin(terms).any(axis=1)
        if mask.any():
            st.caption(f"{int(mask.sum())} of {len(frame)} rows mention this claim's accounts:")
            part = frame[mask].head(30)
            st.dataframe(styled(part, [flags[i] for i in part.index]), width="stretch", hide_index=True)
            with st.popover(f"All {len(frame)} rows"):
                whole = frame.head(200)
                st.dataframe(styled(whole, flags[: len(whole)]), width="stretch", hide_index=True)
            return
    part = frame.head(50)
    st.dataframe(styled(part, flags[: len(part)]), width="stretch", hide_index=True)


tab_findings, tab_flow, tab_trace, tab_evidence, tab_sar = st.tabs(
    ["Findings", "Money flow", "Investigation trace", "Evidence", "SAR draft"])

with tab_findings:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("#### Findings")
        st.markdown('<p class="cf-note">Each claim cites the queries behind it. Open one to see the SQL, the rows, '
                    'and what the verifier decided.</p>', unsafe_allow_html=True)
        for claim in report.get("claims", []):
            verdict = claim.get("verdict", {})
            rejected = verdict.get("status") == "rejected"
            why_struck = ""
            if rejected:
                who = "a deterministic check" if verdict.get("layer") == "deterministic" else \
                    f"the verifier model ({verdict.get('model', 'model')})"
                why_struck = (f'<div class="cf-struck">Struck out by {html.escape(who)}: '
                              f'{mark_accounts(verdict.get("reason", ""), case)}</div>')
            st.markdown(f'<div class="cf-claim{" cf-rejected" if rejected else ""}"><span class="cf-id">'
                        f'{html.escape(claim["id"])}</span><span class="cf-kind">{html.escape(claim["kind"])}</span>'
                        f'<span class="cf-text">{mark_accounts(claim["text"], case)}</span>{why_struck}</div>',
                        unsafe_allow_html=True)
            label = f"{status_badge(verdict.get('status', ''))}, evidence {', '.join(claim['evidence']) or 'none'}"
            with st.expander(label, expanded=False):
                checker = verdict.get("model") or verdict.get("layer", "?")
                st.caption(f"Verifier ({verdict.get('layer', '?')}, {checker}): {verdict.get('reason', '')}")
                terms = claim_terms(claim["text"])
                for qid in claim["evidence"][:4]:
                    show_evidence(qid, terms)
        if report.get("counter_evidence"):
            st.markdown("#### Evidence against")
            for c in report["counter_evidence"]:
                st.markdown(f"- {mark_accounts(c['text'], case)}  \n  <span class='cf-note'>"
                            f"{status_badge(c.get('verdict', {}).get('status', ''))}, {', '.join(c['evidence'])}</span>",
                            unsafe_allow_html=True)
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
                st.markdown(f"- {md(x)}")
        st.markdown("#### What I could not check")
        for x in report.get("limitations", []) or ["Nothing was withheld."]:
            st.markdown(f"- {md(x)}")
        st.markdown("#### Measured signals")
        for s in case.get("signals", []):
            icon = {"suspicious": "🔴", "benign": "🟢"}.get(s["assessment"], "⚪") if s["strength"] else "⚪"
            st.markdown(f"{icon} **{s['name'].replace('_', ' ')}**: {md(s['finding'])}")

with tab_flow:
    st.markdown('<p class="cf-note">Blue outline: Institution A. Orange outline: Institution B. A dashed arrow '
                'crosses between the banks, so the transfer appears in both ledgers. Red marks the pattern '
                'behind the decision.</p>', unsafe_allow_html=True)
    has_pattern = any(s["assessment"] == "suspicious" and s["strength"] >= 2 and s.get("tx_ids")
                      for s in case.get("signals", []))
    if has_pattern:
        st.markdown("#### The pattern behind the decision")
        graph, why = st.columns([3, 2], gap="large")  # the graph at a size that fits one screen
        graph.graphviz_chart(flow_dot(case, max_direct=0), width="stretch")
        with why:
            st.markdown("**Signals behind it**")
            for sig in case.get("signals", []):
                if sig["assessment"] == "suspicious" and sig["strength"] >= 2:
                    st.markdown(f"- **{sig['name'].replace('_', ' ')}** ({sig['strength']}/3): {md(sig['finding'])}")
        with st.expander("The subject's largest direct flows"):
            st.graphviz_chart(flow_dot(case, max_direct=12, include_flagged=False), width="stretch")
    else:
        st.markdown("#### The subject's largest direct flows")
        st.graphviz_chart(flow_dot(case, max_direct=12, include_flagged=False), width="stretch")

with tab_trace:
    investigation = report.get("investigation", {})
    engine = investigation.get("engine")
    if engine:
        st.markdown("#### Markovian engine (Meterless)")
        outcome, why = engine_outcome(engine)
        sizes = [n for n in engine.get("measured_prompt_tokens", []) if n]
        cols = st.columns(4)
        cols[0].metric("Steps run", engine.get("chunks", 0))
        cols[1].metric("Ended", outcome)
        cols[2].metric("State carried, at most", f"{engine.get('chunk_config', {}).get('carryoverTokens', '?')} tokens")
        cols[3].metric("Largest prompt after step 1", f"{max(sizes[1:]):,} tokens" if len(sizes) > 1 else "n/a")
        st.caption(why)
        if sizes:
            st.caption("Prompt size per step, as reported by the model provider. Step 1 reads the whole case record; "
                       "each later step reads only the case brief, the carried state and the latest result, so the "
                       "prompt stays bounded however long the investigation runs.")
            st.bar_chart(pd.DataFrame({"prompt tokens": sizes}, index=[f"step {i + 1}" for i in range(len(sizes))]),
                         color="#23395B", height=240)
    plan = investigation.get("steps", [])
    if plan:
        st.markdown("#### What the planner asked, step by step")
        for step in plan:
            n = step["step"]
            if step.get("error"):
                st.warning(f"Step {n}: the planner's reply could not be used (it was cut off or was not valid JSON), "
                           "so the investigation stopped here.")
                continue
            thought = (step.get("thought") or "").strip()
            action = step.get("action")
            if action == "finish":
                ask = f"finished, leaning **{(step.get('args') or {}).get('leaning', '?')}**"
            elif action:
                ask = f"asked `{action}` {json.dumps(step.get('args', {}))}"
            else:
                ask = ""
            line = f"**{n}.** " + (f"*{thought}* " if thought else "") + ask
            if step.get("skipped"):
                line += "  \nRefused: that exact call had already run."
            st.markdown(line)
            if step.get("carryover_in"):
                with st.expander(f"State carried into step {n}"):
                    st.text(step["carryover_in"])
    if engine and engine.get("reflection"):
        st.caption(f"Reflection after the run: {engine['reflection'][:400]}")
    st.markdown("#### Standard workup and follow-up results")
    for obs in case.get("observations", []):
        title = f"#{obs['step']} {obs['tool']} " + (json.dumps({k: v for k, v in obs["args"].items() if v is not None})
                                                   if obs["args"] else "")
        with st.expander(title, expanded=False):
            st.text(obs.get("error") or obs["summary"])
            st.caption("evidence: " + ", ".join(obs["query_ids"]))

with tab_evidence:
    qids = []
    for obs in case.get("observations", []):
        for q in obs["query_ids"]:
            if q not in qids:
                qids.append(q)
    rows = []
    for q in qids:
        r = store.get_query(q) or {}
        rows.append({"Evidence": q, "What it asked": r.get("purpose"), "Data from": query_source(r.get("sql", ""), settings.tables),
                     "Rows": r.get("row_count"), "ms": r.get("duration_ms"), "Through": r.get("backend")})
    st.markdown(f"{len(qids)} queries, each kept with its SQL and the rows it returned.")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    pick = st.selectbox("Show query", qids)
    if pick:
        show_evidence(pick)

with tab_sar:
    decided = load_analyst(case_id)
    if decided:
        st.markdown(f"**Analyst decision:** {decided['label']} on {decided['at'].replace('T', ' at ')}"
                    + (f". Note: {decided['note']}" if decided.get("note") else "."))
    st.download_button("Download the SAR draft (.md)", data["sar"], file_name=f"{case_id}.sar.md")
    st.markdown(data["sar"].split("## Evidence appendix")[0])
