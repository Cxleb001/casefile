"""Score Casefile against the hidden labels.

Usage:
    python scripts/run_eval.py --rules                 # no model, federated view (fast baseline)
    python scripts/run_eval.py --rules --single-bank   # no model, Institution A's data only
    python scripts/run_eval.py                         # model-driven (needs a key)
    python scripts/run_eval.py --limit 10 --steps 5

Writes eval/results/<name>.json and prints a Markdown summary. The labels are
read only here, after each case is finished.

--single-bank shows what Institution A could conclude without federation: it
still sees its own ledger (including the first hop of payments to B accounts)
but not B's ledger or B's customer records.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import EVAL_DIR, RUNS_DIR, get_settings  # noqa: E402
from casefile.data import get_backend  # noqa: E402
from casefile.evidence import EvidenceStore  # noqa: E402
from casefile.agent.pipeline import load_alerts, run_case  # noqa: E402


class SingleBankBackend:
    """Hide Institution B: its ledger and customer view return no rows."""

    def __init__(self, inner, settings) -> None:
        self.inner = inner
        self.name = f"{inner.name}+single-bank"
        empty_ledger = f"(SELECT * FROM {settings.table('ledger_a')} WHERE 1 = 0)"
        empty_kyc = f"(SELECT * FROM {settings.table('kyc_a')} WHERE 1 = 0)"
        self.swaps = [(settings.table("ledger_b"), empty_ledger), (settings.table("kyc_b"), empty_kyc)]

    def query(self, sql, columns=None, limit=None):
        for name, replacement in self.swaps:
            sql = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w.])", replacement, sql)
        return self.inner.query(sql, columns=columns, limit=limit)

    def close(self) -> None:
        self.inner.close()


def load_labels() -> dict[str, dict]:
    path = EVAL_DIR / "labels.csv"
    if not path.exists():
        raise SystemExit("eval/labels.csv not found; run scripts/prepare_data.py first.")
    with open(path, encoding="utf-8") as handle:
        return {row["alert_id"]: row for row in csv.DictReader(handle)}


def score(rows: list[dict]) -> dict:
    tp = sum(r["truth"] and r["escalate"] for r in rows)
    fp = sum((not r["truth"]) and r["escalate"] for r in rows)
    fn = sum(r["truth"] and not r["escalate"] for r in rows)
    tn = sum((not r["truth"]) and not r["escalate"] for r in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    typed = [r for r in rows if r["truth"] and r["escalate"]]
    sanctions = [r for r in rows if r["sanctions_expected"] is not None]
    claims_total = sum(r["claims"] for r in rows)
    return {
        "alerts": len(rows),
        "accuracy": round((tp + tn) / len(rows), 3) if rows else 0.0,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "typology_hit_rate": round(sum(r["typology_hit"] for r in typed) / len(typed), 3) if typed else None,
        "sanctions_accuracy": round(sum(r["sanctions_ok"] for r in sanctions) / len(sanctions), 3) if sanctions else None,
        "claims_rejected_share": round(sum(r["claims_rejected"] for r in rows) / claims_total, 3) if claims_total else 0.0,
        "needs_review": sum(r["needs_review"] for r in rows),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1) if rows else 0.0,
        "mean_queries": round(sum(r["queries"] for r in rows) / len(rows), 1) if rows else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rules", action="store_true", help="no model: rules-only decisions")
    parser.add_argument("--single-bank", action="store_true", help="hide Institution B's data")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    settings = get_settings()
    labels = load_labels()
    backend = get_backend(settings)
    if args.single_bank:
        backend = SingleBankBackend(backend, settings)
    name = args.name or f"{'rules' if args.rules else 'model'}-{'single' if args.single_bank else 'federated'}" \
                        f"-{datetime.now():%Y%m%d-%H%M}"
    out_dir = RUNS_DIR / "eval" / name
    store = EvidenceStore(out_dir / "evidence.duckdb")
    llm = None
    if not args.rules:
        from casefile.llm import LLM

        llm = LLM(settings, on_call=store.record_llm_call)

    alerts = load_alerts()
    if args.limit:
        alerts = alerts[: args.limit]
    rows = []
    started = time.time()
    for alert in alerts:
        truth = labels[alert["alert_id"]]
        try:
            result = run_case(alert, backend=backend, store=store, settings=settings, llm=llm,
                              max_steps=args.steps, out_dir=out_dir / "cases")
            report = result["report"]
        except Exception as error:  # one broken case must not end the run
            print(f"{alert['alert_id']}: FAILED {error}")
            report = {"disposition": "CLOSE", "typology": "NONE", "confidence": 0, "claims": [],
                      "sanctions_referral": {"needed": False}, "verification": {"rejected": 0},
                      "timing": {"seconds": 0, "queries": 0}, "error": str(error)}
        is_suspicious = truth["is_suspicious"] == "1"
        patterns = set(filter(None, truth.get("pattern_types", "").split("|")))
        match = truth.get("watchlist_alert_match", "none")
        expected = None if match == "" else match in {"exact", "fuzzy"}
        row = {
            "alert_id": alert["alert_id"], "truth": is_suspicious, "patterns": sorted(patterns),
            "escalate": report["disposition"] == "ESCALATE", "typology": report.get("typology"),
            "typology_hit": report.get("typology") in patterns,
            "confidence": report.get("confidence"),
            "sanctions_expected": expected, "sanctions_flagged": bool(report["sanctions_referral"].get("needed")),
            "claims": len(report.get("claims", [])), "claims_rejected": report["verification"].get("rejected", 0),
            "needs_review": bool(report.get("needs_review")),
            "seconds": report["timing"]["seconds"], "queries": report["timing"]["queries"],
            "error": report.get("error"),
        }
        row["sanctions_ok"] = expected is None or row["sanctions_flagged"] == expected
        rows.append(row)
        ok = "ok " if row["escalate"] == row["truth"] else "MISS"
        print(f"{ok} {alert['alert_id']}  truth={'SUSP' if is_suspicious else 'clean'} {'|'.join(sorted(patterns)) or '-':<24} "
              f"-> {report['disposition']:<8} {report.get('typology', ''):<15} {row['seconds']:>5}s", flush=True)

    summary = score(rows)
    summary.update({"name": name, "mode": "rules" if args.rules else "model",
                    "view": "single-bank" if args.single_bank else "federated",
                    "backend": backend.name, "wall_seconds": round(time.time() - started, 1),
                    "models": None if llm is None else settings.models})
    (out_dir).mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\n## {name}\n")
    print("| alerts | accuracy | precision | recall | F1 | typology hit | sanctions acc. | claims rejected | s/alert |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    print(f"| {summary['alerts']} | {summary['accuracy']:.0%} | {summary['precision']:.0%} | {summary['recall']:.0%} | "
          f"{summary['f1']:.2f} | {summary['typology_hit_rate'] if summary['typology_hit_rate'] is not None else '-'} | "
          f"{summary['sanctions_accuracy'] if summary['sanctions_accuracy'] is not None else '-'} | "
          f"{summary['claims_rejected_share']:.0%} | {summary['mean_seconds']} |")
    print(f"\nconfusion: {summary['confusion']}; results: {out_dir / 'results.json'}")
    store.close()
    backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
