"""Check that data and models are reachable. Today's gate: one federated query and one Nemotron reply.

Usage:
    python scripts/check_setup.py            # data checks, then a model reply
    python scripts/check_setup.py --data     # data checks only
    python scripts/check_setup.py --model    # model reply only
    python scripts/check_setup.py --models   # list the Nemotron models your key can call
    python scripts/check_setup.py --raw      # print Zetaris's raw JSON for SELECT 1 (debugging)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import EVAL_DIR, get_settings  # noqa: E402
from casefile.data import get_backend  # noqa: E402

FEDERATED_COLUMNS = [
    "alert_account", "partner_bank", "partner_account", "partner_customer_type",
    "partner_country", "onward_transfers", "onward_recipients",
]


def federated_sql(settings, bank: int, account: str) -> str:
    """Money that left an Institution A account for Institution B, and where it went next.

    Joins three systems in one query: A's Postgres ledger, B's S3 ledger and
    B's governed KYC view.
    """
    account = account.replace("'", "''")
    return f"""
SELECT a.from_account AS alert_account,
       a.to_bank AS partner_bank,
       a.to_account AS partner_account,
       k.customer_type AS partner_customer_type,
       k.country AS partner_country,
       count(DISTINCT b.tx_id) AS onward_transfers,
       count(DISTINCT b.to_account) AS onward_recipients
FROM {settings.table('ledger_a')} a
JOIN {settings.table('ledger_b')} b
  ON b.from_bank = a.to_bank AND b.from_account = a.to_account AND b.ts >= a.ts
JOIN {settings.table('kyc_b')} k
  ON k.bank_id = a.to_bank AND k.account = a.to_account
WHERE a.from_bank = {int(bank)} AND a.from_account = '{account}'
GROUP BY a.from_account, a.to_bank, a.to_account, k.customer_type, k.country
ORDER BY onward_transfers DESC
LIMIT 5
""".strip()


def report(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))
    return ok


def print_table(columns, rows) -> None:
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(columns)]
    print("    " + "  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print("    " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def check_data(settings) -> bool:
    print(f"\nData backend: {settings.data_backend}")
    try:
        backend = get_backend(settings)
    except Exception as error:
        return report(False, "connect", str(error))

    ok = True
    try:
        result = backend.query("SELECT 1 AS one", columns=["one"])
        ok &= report(result.rows == [[1]], "SELECT 1", f"{result.rows} in {result.duration_ms} ms")
    except Exception as error:
        return report(False, "SELECT 1", str(error))

    for key in ("ledger_a", "kyc_a", "ledger_b", "kyc_b", "watchlist"):
        table = settings.table(key)
        try:
            result = backend.query(f"SELECT count(*) AS n FROM {table}", columns=["n"])
            ok &= report(True, f"{table}", f"{result.rows[0][0]:,} rows")
        except Exception as error:
            ok &= report(False, f"{table}", str(error)[:300])

    alerts_path = EVAL_DIR / "alerts.csv"
    if not alerts_path.exists():
        return report(False, "federated query", "eval/alerts.csv missing; run scripts/prepare_data.py") and ok
    with open(alerts_path, encoding="utf-8") as handle:
        alerts = list(csv.DictReader(handle))

    for alert in alerts[:10]:
        sql = federated_sql(settings, int(alert["bank_id"]), alert["account"])
        try:
            result = backend.query(sql, columns=FEDERATED_COLUMNS)
        except Exception as error:
            return report(False, "federated query (A ledger x B ledger x B KYC view)", str(error)[:400]) and False
        if result.rows:
            report(True, "federated query (A ledger x B ledger x B KYC view)",
                   f"{alert['alert_id']}, {result.row_count} partner accounts, {result.duration_ms} ms")
            print_table(result.columns, result.rows)
            break
    else:
        ok &= report(False, "federated query", "no alert in the first 10 sent money to Institution B")
    backend.close()
    return ok


def check_model(settings, list_only: bool = False) -> bool:
    from casefile.llm import LLM

    print(f"\nModel provider: {settings.llm_provider} ({settings.llm_base_url})")
    try:
        llm = LLM(settings)
    except Exception as error:
        return report(False, "client", str(error))

    try:
        models = llm.list_models()
        wanted = [m for m in models if "nemotron" in m.lower() or m.startswith("meterless/")]
        report(True, "list models", f"{len(models)} available, {len(wanted)} Nemotron/Meterless")
        if list_only:
            for model in wanted:
                print(f"    {model}")
            return True
        for step, model in settings.models.items():
            if models and model not in models:
                print(f"    note: {step} model {model!r} is not in the list; set CASEFILE_MODEL_{step.upper()} in .env")
    except Exception as error:
        report(False, "list models", str(error)[:300])
        if list_only:
            return False

    try:
        result = llm.chat(
            "plan",
            [{"role": "user", "content": "Reply with exactly one word: ready"}],
            max_tokens=200,
            temperature=0,
        )
        return report(bool(result.content), "chat", f"{result.model} said {result.content[:60]!r} in {result.latency_ms} ms")
    except Exception as error:
        return report(False, "chat", str(error)[:400])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", action="store_true")
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--models", action="store_true")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if args.raw:
        from casefile.data.zetaris import ZetarisBackend

        print(json.dumps(ZetarisBackend(settings).raw("SELECT 1"), indent=2)[:4000])
        return 0
    if args.models:
        return 0 if check_model(settings, list_only=True) else 1

    run_data = args.data or not args.model
    run_model = args.model or not args.data
    ok = True
    if run_data:
        ok &= check_data(settings)
    if run_model:
        ok &= check_model(settings)
    print("\nAll checks passed." if ok else "\nSome checks failed; see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
