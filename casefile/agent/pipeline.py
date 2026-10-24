"""One alert in, one verified case file out.

    workup (deterministic, every case)  ->  investigation (planner model)
    ->  report (model, or rules without one)  ->  verifier  ->  saved case + SAR draft
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import EVAL_DIR, RUNS_DIR
from .case import CaseState
from .investigator import investigate
from .analysis import compute_signals
from .report import apply_evidence_standard, draft_report, report_json, report_markdown, rules_report
from .tools import Toolbox
from .verifier import verify_report
from .workup import standard_workup

CASES_DIR = RUNS_DIR / "cases"


def load_alerts(path: Path | None = None) -> list[dict[str, Any]]:
    with open(path or EVAL_DIR / "alerts.csv", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def find_alert(alert_id: str, path: Path | None = None) -> dict[str, Any]:
    for alert in load_alerts(path):
        if alert["alert_id"] == alert_id:
            return alert
    raise KeyError(f"No alert {alert_id} in {path or EVAL_DIR / 'alerts.csv'}")


def run_case(alert: dict[str, Any], *, backend, store, settings, llm=None, max_steps: int = 8,
             on_event: Callable[[dict[str, Any]], None] | None = None, out_dir: Path | None = None,
             case_id: str | None = None) -> dict[str, Any]:
    """Investigate one alert end to end and save the case file. ``llm=None`` runs rules-only."""
    started = time.perf_counter()
    case_id = case_id or f"{alert['alert_id']}-{datetime.now():%Y%m%d-%H%M%S}"
    case = CaseState(case_id, alert)
    toolbox = Toolbox(backend, store, settings, case)

    def emit(event: dict[str, Any]) -> None:
        if on_event:
            on_event(event)

    emit({"type": "stage", "stage": "workup"})
    standard_workup(toolbox, on_step=lambda obs: emit({"type": "observation", "observation": obs}))

    investigation: dict[str, Any] = {"steps": [], "leaning": None, "open_questions": []}
    if llm is not None:
        emit({"type": "stage", "stage": "investigation"})
        investigation = investigate(toolbox, llm, max_steps=max_steps, on_event=emit)
        # The investigation added its own queries to the graph: measure the structure again on all of it.
        case.signals = compute_signals(case)
        emit({"type": "stage", "stage": "report"})
        report = draft_report(case, store, llm, investigation, settings)
    else:
        report = rules_report(case)

    emit({"type": "stage", "stage": "verify"})
    report = verify_report(report, case, store, llm)
    if llm is not None:
        report = apply_evidence_standard(report, case)

    # A decision built on failed data checks must never go out looking confident.
    failed = [o for o in case.observations if o.error]
    if failed:
        tools = ", ".join(sorted({o.tool for o in failed}))
        report["needs_review"] = True
        report["confidence"] = min(report.get("confidence", 0.5), 0.3)
        report["limitations"].insert(0, f"{len(failed)} of {len(case.observations)} checks failed ({tools}); "
                                        f"this decision rests on incomplete data.")
        report["data_errors"] = [{"tool": o.tool, "error": (o.error or "")[:300]} for o in failed]
    report["investigation"] = investigation
    report["timing"] = {"seconds": round(time.perf_counter() - started, 1),
                        "queries": len(case.all_query_ids()), "tool_calls": len(case.observations)}
    report["backend"] = backend.name

    out = out_dir or CASES_DIR
    case.save(out)
    (out / f"{case_id}.report.json").write_text(report_json(report), encoding="utf-8")
    (out / f"{case_id}.sar.md").write_text(report_markdown(report, case, store), encoding="utf-8")
    emit({"type": "done", "case_id": case_id, "report": report})
    return {"case": case, "report": report, "paths": {"dir": str(out), "case_id": case_id}}


def load_case(case_id: str, directory: Path | None = None) -> dict[str, Any]:
    directory = directory or CASES_DIR
    case = json.loads((directory / f"{case_id}.json").read_text(encoding="utf-8"))
    report = json.loads((directory / f"{case_id}.report.json").read_text(encoding="utf-8"))
    return {"case": case, "report": report}
