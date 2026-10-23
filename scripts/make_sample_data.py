"""Generate a small synthetic dataset in the IBM AMLworld file format.

Use it to test the pipeline before the real Kaggle files are on disk. It
writes three files that mimic AMLworld's layout (same headers, same Patterns
block format) but are NOT the real data:

    SAMPLE_Trans.csv      transactions with an Is Laundering flag
    SAMPLE_Patterns.txt   laundering attempts in BEGIN/END blocks
    SAMPLE_accounts.csv   bank, account and owning entity

Usage:
    python scripts/make_sample_data.py --out data/raw/sample
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PATTERNS = ["FAN-OUT", "FAN-IN", "GATHER-SCATTER", "SCATTER-GATHER", "CYCLE", "RANDOM", "BIPARTITE", "STACK"]
CURRENCIES = ["US Dollar", "Euro", "UK Pound", "Yuan", "Yen", "Rupee", "Canadian Dollar", "Swiss Franc", "Bitcoin"]
CURRENCY_WEIGHTS = [0.55, 0.15, 0.07, 0.06, 0.05, 0.05, 0.03, 0.03, 0.01]
FORMATS = ["Cheque", "Credit Card", "ACH", "Cash", "Wire", "Reinvestment", "Bitcoin"]
FORMAT_WEIGHTS = [0.38, 0.27, 0.12, 0.10, 0.04, 0.07, 0.02]
ENTITY_TYPES = ["Corporation", "Sole Proprietorship", "Partnership", "Individual"]
ENTITY_WEIGHTS = [0.45, 0.25, 0.10, 0.20]
START = datetime(2022, 9, 1)


def fmt_bank(bank_id: int) -> str:
    return f"{bank_id:03d}" if bank_id < 1000 else f"{bank_id:05d}"


def fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y/%m/%d %H:%M")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="data/raw/sample")
    parser.add_argument("--accounts", type=int, default=30_000)
    parser.add_argument("--banks", type=int, default=300)
    parser.add_argument("--transactions", type=int, default=400_000)
    parser.add_argument("--patterns", type=int, default=160)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Banks and accounts -----------------------------------------------------
    bank_ids = rng.choice(np.arange(1, 60_000), size=args.banks, replace=False)
    bank_sizes = rng.zipf(1.6, size=args.banks).astype(float)
    bank_of = rng.choice(bank_ids, size=args.accounts, p=bank_sizes / bank_sizes.sum())
    raw_ids = rng.choice(2**36, size=args.accounts * 2, replace=False)
    account_ids = np.array([f"{x:09X}" for x in raw_ids[: args.accounts]])
    entity_type = rng.choice(ENTITY_TYPES, size=args.accounts, p=ENTITY_WEIGHTS)
    entity_ids = np.array([f"{x:09X}" for x in raw_ids[args.accounts :]])
    home_currency = rng.choice(CURRENCIES, size=args.accounts, p=CURRENCY_WEIGHTS)

    accounts = pd.DataFrame(
        {
            "Bank Name": [f"Bank #{b}" for b in bank_of],
            "Bank ID": [fmt_bank(int(b)) for b in bank_of],
            "Account Number": account_ids,
            "Entity ID": entity_ids,
            "Entity Name": [f"{t} #{i}" for i, t in enumerate(entity_type)],
        }
    )

    # Background activity ----------------------------------------------------
    activity = rng.pareto(1.3, size=args.accounts) + 1.0
    activity /= activity.sum()
    n = args.transactions
    senders = rng.choice(args.accounts, size=n, p=activity)
    receivers = rng.choice(args.accounts, size=n, p=activity)
    self_mask = rng.random(n) < 0.07
    receivers[self_mask] = senders[self_mask]
    minutes = rng.integers(0, args.days * 24 * 60, size=n)
    amounts = np.round(np.exp(rng.normal(6.0, 1.8, size=n)), 2)
    formats = rng.choice(FORMATS, size=n, p=FORMAT_WEIGHTS)
    formats[self_mask] = "Reinvestment"
    pay_cur = home_currency[senders]
    recv_cur = home_currency[receivers]

    rows = []
    for i in range(n):
        s, r = senders[i], receivers[i]
        rate = 1.0 if pay_cur[i] == recv_cur[i] else float(np.round(rng.uniform(0.5, 1.5), 4))
        rows.append(
            (
                START + timedelta(minutes=int(minutes[i])),
                fmt_bank(int(bank_of[s])), account_ids[s],
                fmt_bank(int(bank_of[r])), account_ids[r],
                round(amounts[i] * rate, 2), recv_cur[i],
                amounts[i], pay_cur[i],
                formats[i], 0,
            )
        )

    # Laundering attempts ----------------------------------------------------
    criminal = rng.choice(args.accounts, size=max(200, args.accounts // 40), replace=False)
    pattern_blocks: list[tuple[str, str, list[tuple]]] = []

    def tx(src: int, dst: int, when: datetime, amount: float) -> tuple:
        fmt = rng.choice(["ACH", "Wire", "Cash", "Cheque", "Bitcoin"], p=[0.35, 0.25, 0.15, 0.15, 0.10])
        pc = "Bitcoin" if fmt == "Bitcoin" else home_currency[src]
        rc = "Bitcoin" if fmt == "Bitcoin" else home_currency[dst]
        rate = 1.0 if pc == rc else float(np.round(rng.uniform(0.5, 1.5), 4))
        return (when, fmt_bank(int(bank_of[src])), account_ids[src], fmt_bank(int(bank_of[dst])),
                account_ids[dst], round(amount * rate, 2), rc, round(amount, 2), pc, fmt, 1)

    for k in range(args.patterns):
        kind = PATTERNS[k % len(PATTERNS)]
        t0 = START + timedelta(minutes=int(rng.integers(0, (args.days - 3) * 24 * 60)))
        base = float(np.exp(rng.normal(8.5, 1.0)))
        step = lambda j: t0 + timedelta(hours=float(j) * rng.uniform(2, 14))  # noqa: E731
        pick = lambda m: list(rng.choice(criminal, size=m, replace=False))  # noqa: E731
        block: list[tuple] = []
        if kind == "FAN-OUT":
            hub, *outs = pick(int(rng.integers(5, 13)))
            block = [tx(hub, o, step(j), base * rng.uniform(0.6, 1.0)) for j, o in enumerate(outs)]
            detail = f"Max {len(outs)}-degree Fan-Out"
        elif kind == "FAN-IN":
            hub, *ins = pick(int(rng.integers(5, 13)))
            block = [tx(i, hub, step(j), base * rng.uniform(0.6, 1.0)) for j, i in enumerate(ins)]
            detail = f"Max {len(ins)}-degree Fan-In"
        elif kind == "GATHER-SCATTER":
            members = pick(int(rng.integers(7, 15)))
            hub, rest = members[0], members[1:]
            half = len(rest) // 2
            block = [tx(i, hub, step(j), base * rng.uniform(0.6, 1.0)) for j, i in enumerate(rest[:half])]
            block += [tx(hub, o, step(half + 1 + j), base * rng.uniform(0.5, 0.9)) for j, o in enumerate(rest[half:])]
            detail = f"Max {half}-degree Fan-In"
        elif kind == "SCATTER-GATHER":
            members = pick(int(rng.integers(5, 11)))
            src, dst, mids = members[0], members[1], members[2:]
            block = [tx(src, m, step(0), base * rng.uniform(0.7, 1.0)) for m in mids]
            block += [tx(m, dst, step(2), base * rng.uniform(0.6, 0.9)) for m in mids]
            detail = f"Max {len(mids)}-degree Fan-Out"
        elif kind == "CYCLE":
            ring = pick(int(rng.integers(3, 9)))
            block = [tx(ring[j], ring[(j + 1) % len(ring)], step(j), base * (0.97**j)) for j in range(len(ring))]
            detail = f"Max {len(ring)} hops"
        elif kind == "RANDOM":
            walk = pick(int(rng.integers(4, 9)))
            block = [tx(walk[j], walk[j + 1], step(j), base * (0.97**j)) for j in range(len(walk) - 1)]
            detail = f"Max {len(walk) - 1} hops"
        elif kind == "BIPARTITE":
            members = pick(int(rng.integers(4, 10)))
            left, right = members[: len(members) // 2], members[len(members) // 2 :]
            block = [tx(a, b, step(j), base * rng.uniform(0.5, 1.0))
                     for j, (a, b) in enumerate((a, b) for a in left for b in right) if rng.random() < 0.7]
            detail = f"Max {len(left)}-degree Bipartite"
        else:  # STACK
            members = pick(int(rng.integers(6, 13)))
            third = len(members) // 3
            a, b, c = members[:third], members[third : 2 * third], members[2 * third :]
            block = [tx(x, y, step(0), base * rng.uniform(0.6, 1.0)) for x in a for y in b if rng.random() < 0.7]
            block += [tx(y, z, step(2), base * rng.uniform(0.5, 0.9)) for y in b for z in c if rng.random() < 0.7]
            detail = f"Max {len(a)}-degree Stack"
        if block:
            pattern_blocks.append((kind, detail, block))
            rows.extend(block)

    # Integration: laundering entities spending into the ordinary economy.
    for _ in range(args.patterns * 3):
        src = int(rng.choice(criminal))
        dst = int(rng.choice(args.accounts, p=activity))
        when = START + timedelta(minutes=int(rng.integers(0, args.days * 24 * 60)))
        rows.append(tx(src, dst, when, float(np.exp(rng.normal(7.0, 1.0)))))

    # Write ----------------------------------------------------------------------
    rows.sort(key=lambda r: r[0])
    header = ("Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,"
              "Amount Paid,Payment Currency,Payment Format,Is Laundering")

    def line(r: tuple) -> str:
        return ",".join([fmt_ts(r[0]), r[1], r[2], r[3], r[4], f"{r[5]:.2f}", r[6], f"{r[7]:.2f}", r[8], r[9], str(r[10])])

    with open(out / "SAMPLE_Trans.csv", "w", encoding="utf-8", newline="\n") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(line(r) + "\n")

    with open(out / "SAMPLE_Patterns.txt", "w", encoding="utf-8", newline="\n") as f:
        for kind, detail, block in pattern_blocks:
            f.write(f"BEGIN LAUNDERING ATTEMPT - {kind}:  {detail}\n")
            for r in sorted(block, key=lambda r: r[0]):
                f.write(line(r) + "\n")
            f.write(f"END LAUNDERING ATTEMPT - {kind}\n\n")

    accounts.to_csv(out / "SAMPLE_accounts.csv", index=False)

    laundering = sum(1 for r in rows if r[10] == 1)
    print(f"Wrote {len(rows):,} transactions ({laundering:,} laundering, {len(pattern_blocks)} attempts) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
