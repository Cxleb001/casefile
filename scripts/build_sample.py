"""Build sample/: a slice of the real data around a few showcase alerts, plus their recorded model answers.

    python scripts/build_sample.py --prepared data/prepared --eval eval \\
        --run runs/eval/final-ai-federated-v2 --alerts ALR-004,ALR-011,ALR-019

Every query the agent makes on those alerts (the standard workup, the rules decision, and the model's own
follow-ups replayed from the run's recordings) is run on the full data. The slice keeps every transaction
that touches an account those queries named or returned, the customer records of those accounts, and the
whole (synthetic) watchlist.

Then it checks itself on the slice, and refuses to write a sample that does not hold:
- every query must return the same rows it returned on the full data (matched by id; any order);
- every case must reach the same decision, typology, verdicts and screening result;
- with --run, the replay must also reach the decision recorded in that run.
If a query differs, the accounts it touched are added and the slice is cut again.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.agent.pipeline import run_case  # noqa: E402
from casefile.config import DEFAULT_TABLES, get_settings  # noqa: E402
from casefile.data.local import LocalBackend  # noqa: E402
from casefile.evidence import EvidenceStore  # noqa: E402
from casefile.replay import ReplayLLM, load_recordings  # noqa: E402

LITERAL = re.compile(r"'([0-9A-Za-z]{6,12})'")
MAX_STEPS = 8  # run_eval's default: a replay must allow the follow-ups the recorded run had


class Capture:
    """Pass queries through and remember every one, with the rows it returned."""

    def __init__(self, inner) -> None:
        self.inner, self.name, self.seen = inner, inner.name, []

    def query(self, sql, columns=None, limit=None):
        result = self.inner.query(sql, columns=columns, limit=limit)
        self.seen.append((sql, result.rows))
        return result

    def close(self) -> None:
        self.inner.close()


def known_accounts(prepared: Path) -> set[str]:
    con = duckdb.connect()
    accounts: set[str] = set()
    for name in ("ledger_a", "ledger_b"):
        path = (prepared / f"{name}.parquet").as_posix()
        for (account,) in con.execute(f"SELECT from_account FROM read_parquet('{path}') UNION "
                                      f"SELECT to_account FROM read_parquet('{path}')").fetchall():
            accounts.add(account)
    return accounts


def touched(seen, universe: set[str]) -> set[str]:
    found: set[str] = set()
    for sql, rows in seen:
        found.update(m for m in LITERAL.findall(sql) if m in universe)
        for row in rows:
            found.update(v for v in row if isinstance(v, str) and v in universe)
    return found


def outcome(report: dict) -> dict:
    return {
        "disposition": report["disposition"], "typology": report.get("typology"),
        "needs_review": bool(report.get("needs_review")),
        "sanctions": bool(report.get("sanctions_referral", {}).get("needed")),
        "claims": [(c["id"], c["verdict"]["status"]) for c in report.get("claims", [])],
        "steps": [(s.get("action"), json.dumps(s.get("args"), sort_keys=True))
                  for s in report.get("investigation", {}).get("steps", [])],
    }


def run_all(alerts, prepared: Path, recordings: Path | None, work: Path, label: str):
    """Run every alert rules-only and (with recordings) as a replay. Returns outcomes, query rows, capture."""
    settings = replace(get_settings(), data_backend="local", tables=dict(DEFAULT_TABLES))
    backend = Capture(LocalBackend(settings, prepared_dir=prepared))
    store = EvidenceStore(work / f"{label}.duckdb")
    outcomes, mismatches = {}, 0
    for alert in alerts:
        result = run_case(alert, backend=backend, store=store, settings=settings, llm=None,
                          case_id=f"{alert['alert_id']}-rules", out_dir=work / label)
        outcomes[(alert["alert_id"], "rules")] = outcome(result["report"])
        if recordings:
            llm = ReplayLLM(recordings, settings=settings)
            result = run_case(alert, backend=backend, store=store, settings=settings, llm=llm,
                              max_steps=MAX_STEPS, out_dir=work / label)
            outcomes[(alert["alert_id"], "replay")] = outcome(result["report"])
            mismatches += len(llm.mismatches)
    # Compared as sets: without an ORDER BY (or with ties) any engine may return rows in another order,
    # and Zetaris did too. The case outcomes below catch any effect order has on the decision.
    rows = {q: sorted(json.dumps(r, sort_keys=True) for r in json.loads(r_json))
            for q, r_json in store.con.execute("SELECT query_id, rows_json FROM queries").fetchall()}
    store.close()
    backend.close()
    return outcomes, rows, backend.seen, mismatches


def cut(prepared: Path, out: Path, accounts: set[str]) -> dict[str, int]:
    data = out / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE keep (account VARCHAR)")
    con.executemany("INSERT INTO keep VALUES (?)", [[a] for a in sorted(accounts)])
    counts = {}
    for name in ("ledger_a", "ledger_b"):
        src, dst = (prepared / f"{name}.parquet").as_posix(), (data / f"{name}.parquet").as_posix()
        con.execute(f"COPY (SELECT * FROM read_parquet('{src}') WHERE from_account IN (SELECT account FROM keep) "
                    f"OR to_account IN (SELECT account FROM keep) ORDER BY ts, tx_id) TO '{dst}' (FORMAT PARQUET)")
        counts[name] = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
    for name in ("kyc_a", "kyc_b"):
        src, dst = (prepared / f"{name}.parquet").as_posix(), (data / f"{name}.parquet").as_posix()
        con.execute(f"COPY (SELECT * FROM read_parquet('{src}') WHERE account IN (SELECT account FROM keep) "
                    f"ORDER BY bank_id, account) TO '{dst}' (FORMAT PARQUET)")
        counts[name] = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
    shutil.copy(prepared / "watchlist.csv", data / "watchlist.csv")
    with open(data / "watchlist.csv", encoding="utf-8") as handle:
        counts["watchlist"] = sum(1 for _ in handle) - 1
    return counts


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepared", type=Path, required=True, help="the full prepared data")
    parser.add_argument("--eval", type=Path, required=True, help="folder with alerts.csv and labels.csv")
    parser.add_argument("--run", type=Path, help="an eval run folder (runs/eval/<name>) with llm/ and cases/")
    parser.add_argument("--alerts", required=True, help="comma-separated alert ids")
    parser.add_argument("--out", type=Path, default=ROOT / "sample")
    args = parser.parse_args()

    wanted = [a.strip() for a in args.alerts.split(",") if a.strip()]
    alerts = [a for a in read_csv(args.eval / "alerts.csv") if a["alert_id"] in wanted]
    missing = set(wanted) - {a["alert_id"] for a in alerts}
    if missing:
        raise SystemExit(f"Not in {args.eval / 'alerts.csv'}: {', '.join(sorted(missing))}")
    labels = [r for r in read_csv(args.eval / "labels.csv") if r["alert_id"] in wanted]

    recordings, recorded_reports = None, {}
    work = Path(tempfile.mkdtemp(prefix="casefile-sample-"))
    if args.run:
        calls = load_recordings(args.run / "llm")
        recordings = work / "recordings"
        recordings.mkdir()
        for alert_id in wanted:
            case_ids = sorted(c for c in calls if c.rsplit("-", 2)[0] == alert_id)
            if not case_ids:
                raise SystemExit(f"No recording for {alert_id} in {args.run / 'llm'}")
            case_id = case_ids[-1]
            shutil.copy(args.run / "llm" / f"{case_id}.jsonl", recordings / f"{case_id}.jsonl")
            report_path = args.run / "cases" / f"{case_id}.report.json"
            if report_path.exists():
                recorded_reports[alert_id] = outcome(json.loads(report_path.read_text(encoding="utf-8")))

    print(f"Running {len(alerts)} alert(s) on the full data...")
    full, full_rows, seen, full_mismatch = run_all(alerts, args.prepared, recordings, work, "full")
    universe = known_accounts(args.prepared)
    accounts = touched(seen, universe)

    for attempt in range(1, 6):
        counts = cut(args.prepared, args.out, accounts)
        print(f"Slice {attempt}: {len(accounts)} accounts, " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        sliced, sliced_rows, _, slice_mismatch = run_all(alerts, args.out / "data", recordings, work, f"slice{attempt}")
        differing = [q for q, rows in full_rows.items() if sliced_rows.get(q) != rows]
        if not differing:
            break
        print(f"  {len(differing)} quer(ies) returned different rows on the slice: widening")
        for q in differing:
            accounts.update(v for row in full_rows[q] for v in re.findall(r'"([0-9A-Za-z]{6,12})"', row) if v in universe)
    else:
        raise SystemExit("The slice still differs from the full data after 5 attempts; not writing a sample.")

    problems = [f"{key}: slice {sliced.get(key)} != full {value}" for key, value in full.items() if sliced.get(key) != value]
    for alert_id, recorded in recorded_reports.items():
        mine = full.get((alert_id, "replay"))
        for field in ("disposition", "typology", "needs_review", "sanctions", "claims", "steps"):
            if mine and mine[field] != recorded[field]:
                problems.append(f"{alert_id}: replay {field} {mine[field]} != recorded run {recorded[field]}")
    if full_mismatch or slice_mismatch:
        print(f"note: {full_mismatch} (full) / {slice_mismatch} (slice) replayed answers matched by order only "
              "(the run read Zetaris; the sample reads DuckDB, so some prompts differ in formatting)")
    if problems:
        print("\n".join(problems))
        raise SystemExit("The sample does not reproduce the full data; not writing it.")

    write_csv(args.out / "alerts.csv", alerts)
    write_csv(args.out / "labels.csv", labels)
    if recordings:
        target = args.out / "recordings"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(recordings, target)
    manifest = {
        "alerts": wanted,
        "accounts_kept": len(accounts),
        "rows": counts,
        "source": "IBM AMLworld HI-Small transactions (CDLA-Sharing-1.0), sliced; KYC and watchlist are synthetic",
        "recorded_run": args.run.name if args.run else None,
        "max_steps": MAX_STEPS,
        "checks": {"queries_identical": len(full_rows), "cases_identical": len(full),
                   "replay_matches_recorded_run": len(recorded_reports)},
        "decisions": {f"{a}/{mode}": o["disposition"] + (" (needs review)" if o["needs_review"] else "")
                      for (a, mode), o in sorted(full.items())},
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"\nSample written to {args.out}: {len(full_rows)} queries and {len(full)} case runs reproduce exactly.")
    for key, value in manifest["decisions"].items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
