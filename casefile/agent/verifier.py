"""Check every claim in a report against the evidence it cites, before anyone reads it.

Two layers:
1. Deterministic: the cited evidence ids must exist in this case; every account
   id, date and number in a factual claim must appear in that evidence.
2. A model check (the ``verify`` step) of whether the cited evidence actually
   supports each claim, run on a different prompt from the writer, one call for
   all claims.

A claim that fails either layer is kept but marked rejected, so the reader sees
what the verifier caught. If the claims a decision rests on are rejected, the
case is flagged for human review instead of going out as written.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .case import CaseState
from .prompts import VERIFIER_SYSTEM

VERIFY_STEP = "verify"
VERIFY_MAX_TOKENS = 12000  # thinking tokens count too

_ACCOUNT = re.compile(r"\b\d{1,6}:[0-9A-Za-z]{4,}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.:\-])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(%|k\b|K\b|m\b|M\b|million\b|thousand\b)?")


def evidence_text(case: CaseState, store, qid: str, max_rows: int = 60) -> str:
    """Everything a claim citing ``qid`` may rely on: the query's rows, and the tool summaries and signals built on it."""
    parts = []
    record = store.get_query(qid)
    if record:
        rows = record.get("rows", [])[:max_rows]
        parts.append(f"{qid} {record.get('purpose', '')}: columns {record.get('columns')}; "
                     f"{record.get('row_count', 0)} rows; first rows: {json.dumps(rows, default=str)}")
    for obs in case.observations:
        if qid in obs.query_ids:
            parts.append(f"tool summary ({obs.tool}): {obs.summary}")
    for sig in case.signals:
        if qid in sig.get("query_ids", []):
            parts.append(f"signal {sig['name']}: {sig['finding']} value={json.dumps(sig.get('value'), default=str)}")
    return "\n".join(parts)


def _numbers(text: str) -> list[tuple[float, int, str]]:
    """(value, decimals, raw) for each number in text, with k/m suffixes expanded."""
    found = []
    for match in _NUMBER.finditer(text):
        whole, frac, suffix = match.group(1), match.group(2) or "", (match.group(3) or "").lower()
        try:
            value = float(whole.replace(",", "") + frac)
        except ValueError:
            continue
        scale = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(suffix, 1.0)
        decimals = len(frac) - 1 if frac else 0
        found.append((value * scale, decimals if scale == 1.0 else -1, match.group(0).strip()))
    return found


def _strip(text: str) -> str:
    return _TIME.sub(" ", _DATE.sub(" ", _ACCOUNT.sub(" ", text)))


def deterministic_check(claim: dict[str, Any], case: CaseState, store) -> tuple[str | None, str]:
    """Return (failure reason or None, the evidence text used)."""
    known = set(case.all_query_ids())
    cited = [q for q in claim.get("evidence", []) if q in known]
    if not claim.get("evidence"):
        return "cites no evidence", ""
    if not cited:
        return f"cites evidence that does not exist in this case: {', '.join(claim['evidence'])}", ""
    evidence = "\n".join(evidence_text(case, store, q) for q in cited)
    text = claim["text"]
    missing = [a for a in _ACCOUNT.findall(text) if a not in evidence and a.split(":")[1] not in evidence]
    missing += [d for d in _DATE.findall(text) if d not in evidence]
    if claim.get("kind") == "fact":
        pool = [v for v, _, _ in _numbers(_strip(evidence))]
        for value, decimals, raw in _numbers(_strip(text)):
            if value < 2 and decimals <= 0:
                continue  # "1 account", "0 matches": too common to check meaningfully
            if decimals == -1:  # "3.4k", "1.2 million": approximate
                ok = any(abs(value - p) <= 0.05 * max(value, 1) for p in pool)
            elif decimals == 0:
                ok = any(abs(value - p) < 1.0 or round(p) == value for p in pool)
            else:
                ok = any(abs(value - p) <= 0.6 * 10 ** (-decimals) for p in pool)
            if raw.endswith("%") and not ok:
                ok = any(abs(value - 100 * p) < 1.0 for p in pool if p <= 1.0)
            if not ok:
                missing.append(raw)
    if missing:
        return f"not found in the cited evidence: {', '.join(dict.fromkeys(missing))}", evidence
    return None, evidence


def verify_report(report: dict[str, Any], case: CaseState, store, llm=None) -> dict[str, Any]:
    from ..llm import LLMError

    to_model = []
    for claim in report["claims"] + report.get("counter_evidence", []):
        failure, evidence = deterministic_check(claim, case, store)
        if failure:
            claim["verdict"] = {"status": "rejected", "layer": "deterministic", "reason": failure}
        else:
            claim["verdict"] = {"status": "checked", "layer": "deterministic", "reason": "ids, accounts and numbers found"}
            to_model.append((claim, evidence))

    if llm is not None and to_model:
        payload = [{"id": c["id"], "kind": c["kind"], "claim": c["text"], "evidence": ev[:3500]} for c, ev in to_model]
        messages = [{"role": "system", "content": VERIFIER_SYSTEM},
                    {"role": "user", "content": json.dumps({"claims": payload}, ensure_ascii=False)}]
        try:
            raw, result = llm.chat_json(VERIFY_STEP, messages, max_tokens=VERIFY_MAX_TOKENS, temperature=0.0,
                                        case_id=case.case_id)
            verdicts = {str(v.get("id")): v for v in (raw.get("verdicts", []) if isinstance(raw, dict) else [])
                        if isinstance(v, dict)}
            for claim, _ in to_model:
                v = verdicts.get(claim["id"])
                if v is None:
                    claim["verdict"].update({"status": "checked", "layer": "deterministic",
                                             "reason": "model gave no verdict; deterministic checks passed"})
                elif v.get("supported") is True:
                    claim["verdict"] = {"status": "verified", "layer": "model", "model": result.model,
                                        "reason": str(v.get("reason", ""))}
                else:
                    claim["verdict"] = {"status": "rejected", "layer": "model", "model": result.model,
                                        "reason": str(v.get("reason", "not supported by the cited evidence"))}
        except LLMError as error:
            for claim, _ in to_model:
                claim["verdict"]["reason"] += f"; model check unavailable ({str(error)[:120]})"

    claims = report["claims"]
    passed = [c for c in claims if c["verdict"]["status"] in {"verified", "checked"}]
    rejected = [c for c in claims if c["verdict"]["status"] == "rejected"]
    report["verification"] = {
        "verified": sum(c["verdict"]["status"] == "verified" for c in claims),
        "checked": sum(c["verdict"]["status"] == "checked" for c in claims),
        "rejected": len(rejected),
        "total": len(claims),
    }
    if report["disposition"] == "ESCALATE" and not any(c["kind"] == "fact" for c in passed):
        report["needs_review"] = True
        report["confidence"] = min(report.get("confidence", 0.5), 0.5)
    for claim in claims:
        claim_id = store.add_claim(case.case_id, f"{claim['id']}: {claim['text']}", claim["evidence"])
        store.set_verdict(claim_id, claim["verdict"]["status"] in {"verified", "checked"},
                          claim["verdict"].get("model", claim["verdict"]["layer"]), claim["verdict"]["reason"])
    return report
