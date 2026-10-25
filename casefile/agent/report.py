"""The decision and SAR draft: written by the model from the case record, or by rules when no model is set."""

from __future__ import annotations

import json
from typing import Any

from .analysis import suspicion_score
from .case import CaseState
from .prompts import TYPOLOGIES, report_system

REPORT_STEP = "report"
REPORT_MAX_TOKENS = 12000  # thinking tokens count too


def evidence_index(case: CaseState, store) -> str:
    lines = []
    for qid in case.all_query_ids():
        record = store.get_query(qid) or {}
        lines.append(f"{qid}: {record.get('purpose', '?')} ({record.get('row_count', 0)} rows)")
    return "\n".join(lines)


def _record_for_model(case: CaseState, store, investigation: dict[str, Any] | None) -> str:
    alert = case.alert
    parts = [
        f"ALERT {alert.get('alert_id')}: account {case.subject}, raised {alert.get('alert_ts')} "
        f"by rules {alert.get('rules')}: {alert.get('trigger')}",
        "INVESTIGATION RECORD\n" + "\n\n".join(f"#{o.step} " + o.for_model(4000) for o in case.observations),
        "SIGNALS\n" + "\n".join(f"- {s['name']} [{s['assessment']}, {s['strength']}/3]: {s['finding']} "
                                f"(evidence: {', '.join(s['query_ids'][:6]) or 'derived'})" for s in case.signals),
        "MEASURED STRUCTURE (deterministic, from the signals above)\n" + _measured_line(case),
        "LIMITATIONS\n" + ("\n".join(f"- {x}" for x in case.limitations) or "- none"),
        "EVIDENCE INDEX (cite these ids)\n" + evidence_index(case, store),
    ]
    if investigation and investigation.get("steps"):
        thoughts = [f"- step {s['step']}: {s.get('thought', '')} -> {s.get('action')}" for s in investigation["steps"]]
        parts.append("INVESTIGATOR NOTES\n" + "\n".join(thoughts))
    return "\n\n".join(parts)


def _measured_line(case: CaseState) -> str:
    measured = rules_report(case)
    return (f"{measured['disposition']} (typology {measured['typology']}, suspicion score "
            f"{suspicion_score(case.signals)}). Escalation needs one strong structural signal, two medium ones, "
            f"or a score of 6 or more.")


def normalise(raw: Any, case: CaseState) -> dict[str, Any]:
    """Coerce a model's report into the shape the verifier and UI expect."""
    raw = raw if isinstance(raw, dict) else {}
    disposition = str(raw.get("disposition", "")).upper()
    disposition = disposition if disposition in {"ESCALATE", "CLOSE"} else "ESCALATE"
    typology = str(raw.get("typology", "NONE")).upper().replace("_", "-")
    typology = typology if typology in TYPOLOGIES else "NONE"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    def items(value, prefix):
        out = []
        for i, item in enumerate(value if isinstance(value, list) else []):
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            evidence = item.get("evidence") or []
            evidence = [evidence] if isinstance(evidence, str) else evidence
            out.append({
                "id": str(item.get("id") or f"{prefix}{i + 1}"),
                "kind": "inference" if str(item.get("kind", "fact")).lower().startswith("inf") else "fact",
                "text": str(item["text"]).strip(),
                "evidence": [str(e).strip() for e in evidence if str(e).strip()],
            })
        return out

    referral = raw.get("sanctions_referral") if isinstance(raw.get("sanctions_referral"), dict) else {}
    limitations = [str(x) for x in raw.get("limitations", []) if str(x).strip()] if isinstance(raw.get("limitations"), list) else []
    for text in case.limitations:
        if text not in limitations:
            limitations.append(text)
    return {
        "disposition": disposition,
        "typology": typology,
        "confidence": round(confidence, 2),
        "headline": str(raw.get("headline", "")).strip(),
        "claims": items(raw.get("claims"), "C"),
        "counter_evidence": items(raw.get("counter_evidence"), "X"),
        "would_change_my_mind": [str(x) for x in raw.get("would_change_my_mind", []) if str(x).strip()]
        if isinstance(raw.get("would_change_my_mind"), list) else [],
        "sanctions_referral": {
            "needed": bool(referral.get("needed", False)),
            "reason": str(referral.get("reason", "")),
            "evidence": [str(e) for e in referral.get("evidence", []) if str(e).strip()]
            if isinstance(referral.get("evidence"), list) else [],
        },
        "limitations": limitations,
        "narrative": str(raw.get("narrative", "")).strip(),
    }


def draft_report(case: CaseState, store, llm, investigation: dict[str, Any] | None = None,
                 settings=None) -> dict[str, Any]:
    from ..llm import LLMError

    settings = settings or llm.settings
    messages = [{"role": "system", "content": report_system(settings)},
                {"role": "user", "content": _record_for_model(case, store, investigation)}]
    try:
        raw, result = llm.chat_json(REPORT_STEP, messages, max_tokens=REPORT_MAX_TOKENS, temperature=0.1,
                                    case_id=case.case_id)
    except LLMError as error:
        messages.append({"role": "user", "content": f"That was not one valid JSON object ({str(error)[:200]}). "
                                                    "Reply again with exactly the JSON object described, shorter."})
        try:
            raw, result = llm.chat_json(REPORT_STEP, messages, max_tokens=REPORT_MAX_TOKENS, temperature=0.0,
                                        case_id=case.case_id)
        except LLMError as again:
            # A case must never fail because a reply was malformed: fall back to the measured decision.
            report = rules_report(case)
            report["needs_review"] = True
            report["limitations"].insert(0, f"The model's report could not be used ({str(again)[:160]}); "
                                            "this is the rules-only decision from the measured signals.")
            report["author"] = {"model": "rules (model report unusable)", "step": REPORT_STEP, "latency_ms": 0}
            return report
    report = normalise(raw, case)
    report["author"] = {"model": result.model, "step": REPORT_STEP, "latency_ms": result.latency_ms}
    return report


def apply_evidence_standard(report: dict[str, Any], case: CaseState) -> dict[str, Any]:
    """The decision must rest on a laundering structure measured in the money-flow graph.

    The model investigates, argues and writes, but models find a story in any
    graph. So the disposition follows the measured structure (the deterministic
    signals, recomputed after the investigation added its own queries). When the
    model disagrees, its reasons stay in the report and the case goes to a human.
    """
    measured = rules_report(case)
    model_view = report["disposition"]
    report["model_disposition"] = model_view
    report["measured"] = {"disposition": measured["disposition"], "typology": measured["typology"],
                          "score": suspicion_score(case.signals),
                          "signals": [f"{s['name']} {s['strength']}/3" for s in case.signals
                                      if s["assessment"] == "suspicious" and s["strength"] > 0]}
    if model_view == measured["disposition"]:
        report["decision_basis"] = "The model's conclusion and the measured structure agree."
        if report["disposition"] == "ESCALATE" and report.get("typology", "NONE") == "NONE":
            report["typology"] = measured["typology"]
        return report
    if measured["disposition"] == "ESCALATE":
        note = (f"Needs review: the model leaned CLOSE, but the measured structure ({', '.join(report['measured']['signals'])}) "
                f"meets the escalation standard, so the case is escalated for a human to weigh the model's reasons.")
        report["typology"] = measured["typology"]
    else:
        note = ("Needs review: the model leaned ESCALATE, but no laundering structure in the subject's own funds "
                f"meets the escalation standard (score {report['measured']['score']}), so the case is closed and the "
                "model's concerns are listed for a human reviewer.")
        report["typology"] = "NONE"
    report["disposition"] = measured["disposition"]
    report["needs_review"] = True
    report["confidence"] = min(report.get("confidence", 0.5), 0.5)
    report["decision_basis"] = note
    report["limitations"].insert(0, note)
    return report


def apply_screening_standard(report: dict[str, Any], case: CaseState) -> dict[str, Any]:
    """A sanctions referral concerns the subject and follows the screening result.

    Only a strong match on the subject (name, date of birth and country) refers the
    case; strong matches on counterparties are listed separately for the sanctions
    team, so they are reported without being mistaken for the subject's own hit.
    """
    screen = next((o for o in case.observations if o.tool == "screen_watchlist" and not o.error), None)
    hits = screen.data.get("hits", []) if screen else []
    evidence = screen.query_ids[:2] if screen else []
    subject = [h for h in hits if h.get("is_subject") and h.get("strength") == "strong"]
    model_view = report.get("sanctions_referral") or {}
    report["model_sanctions_referral"] = bool(model_view.get("needed"))
    if subject:
        h = subject[0]
        reason = model_view.get("reason") if model_view.get("needed") else ""
        report["sanctions_referral"] = {
            "needed": True, "evidence": model_view.get("evidence") or evidence,
            "reason": reason or f"Strong watchlist match on the subject: '{h['customer_name']}' ~ '{h['listed_name']}' "
                                f"({h['list_id']}, {h['program']}), same date of birth and country."}
    else:
        report["sanctions_referral"] = {"needed": False, "evidence": [],
                                        "reason": "No strong watchlist match on the subject."}
    report["counterparty_watchlist"] = [
        {"account": h["account"], "match": f"'{h['customer_name']}' ~ '{h['listed_name']}'", "list_id": h["list_id"],
         "program": h["program"], "evidence": evidence}
        for h in hits if not h.get("is_subject") and h.get("strength") == "strong"]
    return report


def rules_report(case: CaseState) -> dict[str, Any]:
    """A decision from the signals alone: the no-model baseline, and the fallback when no key is set."""
    suspicious = [s for s in case.signals if s["assessment"] == "suspicious" and s["strength"] > 0]
    benign = [s for s in case.signals if s["assessment"] == "benign" and s["strength"] > 0]
    score = suspicion_score(case.signals)
    strong = any(s["strength"] >= 3 for s in suspicious) or sum(1 for s in suspicious if s["strength"] >= 2) >= 2
    # One strong signal or two medium ones, or many weaker ones that add up.
    escalate = (strong and score >= 3) or score >= 6
    typology = "NONE"
    # Most specific shape first when strengths tie: a ring or a collecting account says more than a fan.
    specificity = {"cycle": 0, "convergence": 1, "one_off_transfers": 2, "interconnection": 3, "uniform_amounts": 4}
    for s in sorted(suspicious, key=lambda s: (-s["strength"], specificity.get(s["name"], 9))):
        value = s.get("value") if isinstance(s.get("value"), dict) else {}
        typology = {
            "cycle": "CYCLE",
            "convergence": "SCATTER-GATHER" if value.get("shape") == "scatter-gather" else "GATHER-SCATTER",
            "uniform_amounts": "FAN-OUT" if value.get("direction") == "out" else "FAN-IN",
            "interconnection": "BIPARTITE",
            "one_off_transfers": {"fan-out": "FAN-OUT", "fan-in": "FAN-IN"}.get(value.get("shape"), "GATHER-SCATTER"),
        }.get(s["name"], "NONE")
        if typology != "NONE":
            break
    claims = []
    profile = next((o for o in case.observations if o.tool == "profile_account" and not o.error), None)
    if profile:
        claims.append({"id": "C1", "kind": "fact", "text": profile.summary.split("\n")[0], "evidence": profile.query_ids[:1]})
    for s in suspicious + benign:
        claims.append({"id": f"C{len(claims) + 1}", "kind": "fact", "text": s["finding"], "evidence": s["query_ids"][:6]})
    screen = next((o for o in case.observations if o.tool == "screen_watchlist" and not o.error), None)
    hits = [h for h in (screen.data.get("hits", []) if screen else []) if h["is_subject"] and h["strength"] == "strong"]
    report = {
        "disposition": "ESCALATE" if escalate else "CLOSE",
        "typology": typology if escalate else "NONE",
        "confidence": round(min(0.9, 0.5 + 0.08 * abs(score - 3)), 2),
        "headline": (f"Escalate {case.subject}: {len(suspicious)} laundering signal(s), score {score}."
                     if escalate else f"Close {case.subject}: signals too weak (score {score})."),
        "claims": claims,
        "counter_evidence": [{"id": f"X{i + 1}", "kind": "fact", "text": s["finding"], "evidence": s["query_ids"][:6]}
                             for i, s in enumerate(benign)],
        "would_change_my_mind": [],
        "sanctions_referral": {"needed": bool(hits), "reason": "strong watchlist match on the subject" if hits else "",
                               "evidence": screen.query_ids[:2] if (hits and screen) else []},
        "limitations": list(case.limitations),
        "narrative": "Rules-only decision (no model configured): " + "; ".join(s["finding"] for s in suspicious[:3]),
        "author": {"model": "rules", "step": "rules", "latency_ms": 0},
    }
    return report


def report_markdown(report: dict[str, Any], case: CaseState, store=None) -> str:
    """The SAR draft as Markdown, with the evidence appendix."""
    mark = {"verified": "✓", "checked": "✓", "rejected": "✗", "unverified": "?"}
    lines = [f"# Case {case.case_id}: {report['disposition']}", "",
             f"**{report.get('headline', '')}**", "",
             f"- Subject: `{case.subject}`  ",
             f"- Alert: {case.alert.get('alert_id')} ({case.alert.get('rules')}), {case.alert.get('alert_ts')}",
             f"- Typology: {report['typology']}; confidence {report['confidence']:.0%}",
             f"- Written by: {report.get('author', {}).get('model', '?')}", ""]
    if report.get("decision_basis") and report.get("needs_review"):
        lines += [f"> {report['decision_basis']}", ""]
    elif report.get("needs_review"):
        lines += ["> Needs human review: the verifier rejected the claims this decision rests on.", ""]
    lines += ["## Findings", ""]
    for c in report["claims"]:
        verdict = c.get("verdict", {})
        tick = mark.get(verdict.get("status", "unverified"), "?")
        text = f"~~{c['text']}~~" if verdict.get("status") == "rejected" else c["text"]
        lines.append(f"- {tick} **{c['id']}** ({c['kind']}) {text} — evidence: {', '.join(c['evidence']) or 'none'}")
        if verdict.get("status") == "rejected":
            lines.append(f"    - Rejected by verifier: {verdict.get('reason', '')}")
    if report.get("counter_evidence"):
        lines += ["", "## Evidence against", ""]
        lines += [f"- {c['text']} — evidence: {', '.join(c['evidence']) or 'none'}" for c in report["counter_evidence"]]
    if report.get("would_change_my_mind"):
        lines += ["", "## What would change this decision", ""] + [f"- {x}" for x in report["would_change_my_mind"]]
    ref = report.get("sanctions_referral", {})
    lines += ["", "## Sanctions referral", "", ("Yes: " + ref.get("reason", "")) if ref.get("needed") else "Not needed."]
    if report.get("counterparty_watchlist"):
        lines += ["", "## Counterparty watchlist matches (for the sanctions team)", ""]
        lines += [f"- `{m['account']}` {m['match']} ({m['list_id']}, {m['program']}) — evidence: {', '.join(m['evidence'])}"
                  for m in report["counterparty_watchlist"]]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in report.get("limitations", [])]
    lines += ["", "## Narrative", "", report.get("narrative", "")]
    if store is not None:
        lines += ["", "## Evidence appendix", ""]
        for qid in case.all_query_ids():
            record = store.get_query(qid) or {}
            lines += [f"### {qid}: {record.get('purpose', '')} ({record.get('row_count', 0)} rows, "
                      f"{record.get('backend', '')}, {record.get('duration_ms', 0)} ms)", "", "```sql",
                      str(record.get("sql", "")).strip(), "```", ""]
    return "\n".join(lines)


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)
