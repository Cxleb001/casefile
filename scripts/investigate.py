"""Investigate one alert (or all of them) from the command line.

Usage:
    python scripts/investigate.py ALR-009              # model-driven (needs a key in .env)
    python scripts/investigate.py ALR-009 --rules      # no model: workup + rules-only decision
    python scripts/investigate.py --all --rules        # every alert in eval/alerts.csv
    python scripts/investigate.py ALR-009 --steps 5    # cap the planner's follow-up actions

Each run writes runs/cases/<case_id>.json (the full record), .report.json and .sar.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import get_settings  # noqa: E402
from casefile.data import get_backend  # noqa: E402
from casefile.evidence import EvidenceStore  # noqa: E402
from casefile.agent.pipeline import CASES_DIR, find_alert, load_alerts, run_case  # noqa: E402


def printer(event: dict) -> None:
    kind = event["type"]
    if kind == "stage":
        print(f"\n== {event['stage']} ==")
    elif kind == "observation":
        obs = event["observation"]
        first = (obs.error or obs.summary).split("\n")[0]
        print(f"  [{obs.tool}] {first[:150]}")
    elif kind == "action":
        print(f"  plan: {event.get('thought', '')[:160]}\n    -> {event['action']} {event.get('args')}")
    elif kind == "finish":
        print(f"  plan: done. {event.get('thought', '')[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("alert_id", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--rules", action="store_true", help="no model: rules-only decision")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if not args.alert_id and not args.all:
        parser.error("give an alert id or --all")

    settings = get_settings()
    backend = get_backend(settings)
    store = EvidenceStore()
    llm = None
    if not args.rules:
        from casefile.llm import make_llm

        llm = make_llm(settings, on_call=store.record_llm_call)  # sample mode: the recorded answers

    alerts = load_alerts() if args.all else [find_alert(args.alert_id)]
    for alert in alerts:
        print(f"\n######## {alert['alert_id']}  {alert['bank_id']}:{alert['account']}  ({alert['rules']})")
        result = run_case(alert, backend=backend, store=store, settings=settings, llm=llm, max_steps=args.steps,
                          on_event=None if args.quiet else printer)
        report = result["report"]
        v = report["verification"]
        print(f"\n=> {report['disposition']} ({report['typology']}, confidence {report['confidence']:.0%}) "
              f"in {report['timing']['seconds']}s, {report['timing']['queries']} queries; claims: "
              f"{v['verified']} verified, {v['checked']} checked, {v['rejected']} rejected"
              + ("  [NEEDS REVIEW]" if report.get("needs_review") else ""))
        print(f"   {report.get('headline', '')}")
        print(f"   SAR draft: {CASES_DIR / (result['paths']['case_id'] + '.sar.md')}")
    backend.close()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
