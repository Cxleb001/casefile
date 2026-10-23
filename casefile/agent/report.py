"""The decision and SAR draft: written by the model from the case record, or by rules when no model is set."""

from __future__ import annotations

import json
from typing import Any

from .analysis import suspicion_score
from .case import CaseState
from .prompts import TYPOLOGIES, report_system

REPORT_STEP = "plan"


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
        "LIMITATIONS\n" + ("\n".join(f"- {x}" for x in case.limitations) or "- none"),
        "EVIDENCE INDEX (cite these ids)\n" + evidence_index(case, store),
    ]
    if investigation and investigation.get("steps"):
        thoughts = [f"- step {s['step']}: {s.get('thought', '')} -> {s.get('action')}" for s in investigation["steps"]]
        parts.append("INVESTIGATOR NOTES\n" + "\n".join(thoughts))
    return "\n\n".join(parts)


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
        raw, result = llm.chat_json(REPORT_STEP, messages, max_tokens=6000, temperature=0.1, case_id=case.case_id)
    except LLMError as error:
        messages.append({"role": "user", "content": f"That was not one valid JSON object ({error}). Reply again "
                                                    "with exactly the JSON object described."})
        raw, result = llm.chat_json(REPORT_STEP, messages, max_tokens=6000, temperature=0.0, case_id=case.case_id)
    report = normalise(raw, case)
    report["author"] = {"model": result.model, "step": REPORT_STEP, "latency_ms": result.latency_ms}
    return report


def rules_report(case: CaseState) -> dict[str, Any]:
    """A decision from the signals alone: the no-model baseline, and the fallback when no key is set."""
    suspicious = [s for s in case.signals if s["assessment"] == "suspicious" and s["strength"] > 0]
    benign = [s for s in case.signals if s["assessment"] == "benign" and s["strength"] > 0]
    score = suspicion_score(case.signals)
    strong = any(s["strength"] >= 3 for s in suspicious) or sum(1 for s in suspicious if s["strength"] >= 2) >= 2
    escalate = strong and score >= 3
    typology = "NONE"
    for s in sorted(suspicious, key=lambda s: -s["strength"]):
        value = s.get("value") if isinstance(s.get("value"), dict) else {}
        typology = {
            "cycle": "CYCLE",
            "convergence": "SCATTER-GATHER" if value.get("shape") == "scatter-gather" else "GATHER-SCATTER",
            "uniform_amounts": "FAN-OUT" if value.get("direction") == "out" else "FAN-IN",
            "interconnection": "BIPARTITE",
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
    if report.get("needs_review"):
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
