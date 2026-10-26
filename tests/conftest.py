"""A tiny hand-built world with known answers, so agent tests don't depend on generated data.

Institution A = bank 1, Institution B = bank 2.

    S (A, subject "Ivan Petrov")  --1000 USD-->  X (A)
    X                              --980 USD-->   Y (B)       cross-institution
    Y                              --950 EUR-->   Z (B)
    Z                              --930 USD-->   S           cross-institution, closes a 4-hop ring
    S                              --500 USD-->   H (A)       H is a hub: 60 other senders
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import get_settings  # noqa: E402

# Tests never write model recordings into runs/; the replay tests point recording at a temp folder.
import os  # noqa: E402

os.environ["CASEFILE_RECORD_LLM"] = "false"

T0 = datetime(2022, 9, 3, 9, 0)


def _tx(i, ts, fb, fa, tb, ta, amount, cur="US Dollar", recv_amount=None, recv_cur=None, fmt="ACH"):
    return {
        "tx_id": f"T{i:04d}", "ts": ts, "from_bank": fb, "from_account": fa, "to_bank": tb, "to_account": ta,
        "amount_paid": float(amount), "payment_currency": cur,
        "amount_received": float(recv_amount if recv_amount is not None else amount),
        "receiving_currency": recv_cur or cur, "payment_format": fmt,
    }


def _kyc(bank, account, name, ctype="Individual", country="US", dob="1985-05-05"):
    return {
        "bank_id": bank, "account": account, "customer_id": f"C{account}", "customer_type": ctype,
        "full_name": name, "dob_or_incorporation": pd.Timestamp(dob), "national_id": f"ID-{account}",
        "country": country, "occupation_or_industry": "Trader", "account_open_date": pd.Timestamp("2015-01-01"),
        "declared_monthly_turnover_usd": 2000.0, "risk_rating": "Low", "pep_flag": False,
        "last_review_date": pd.Timestamp("2021-01-01"),
    }


@pytest.fixture()
def world(tmp_path):
    txs = [
        _tx(1, T0, 1, "S", 1, "X", 1000),
        _tx(2, T0 + timedelta(hours=5), 1, "X", 2, "Y", 980),
        _tx(3, T0 + timedelta(hours=10), 2, "Y", 2, "Z", 950, "Euro"),
        _tx(4, T0 + timedelta(hours=15), 2, "Z", 1, "S", 930),
        _tx(5, T0 + timedelta(hours=2), 1, "S", 1, "H", 500, fmt="Credit Card"),
    ]
    for i in range(60):
        txs.append(_tx(100 + i, T0 + timedelta(minutes=i), 1, f"R{i:02d}", 1, "H", 20 + i, fmt="Credit Card"))
    ledger = pd.DataFrame(txs)
    a_mask = (ledger.from_bank == 1) | (ledger.to_bank == 1)
    b_mask = (ledger.from_bank == 2) | (ledger.to_bank == 2)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    ledger[a_mask].to_parquet(prepared / "ledger_a.parquet", index=False)
    ledger[b_mask].to_parquet(prepared / "ledger_b.parquet", index=False)

    kyc_a = [_kyc(1, "S", "Ivan Petrov", country="RU", dob="1980-01-01"), _kyc(1, "X", "Mary Stone"),
             _kyc(1, "H", "MegaMart Ltd", ctype="Corporation")]
    kyc_a += [_kyc(1, f"R{i:02d}", f"Shopper {i}") for i in range(60)]
    pd.DataFrame(kyc_a).to_parquet(prepared / "kyc_a.parquet", index=False)
    pd.DataFrame([_kyc(2, "Y", "Hidden One", country="CY"), _kyc(2, "Z", "Hidden Two", country="CY")]) \
        .to_parquet(prepared / "kyc_b.parquet", index=False)
    pd.DataFrame([
        {"list_id": "SYN-00001", "entity_type": "Person", "name": "PETROV, Ivan", "aliases": "",
         "birth_or_incorporation": "1980-01-01", "country": "RU", "program": "SYN-FINANCIAL-SANCTIONS",
         "listed_on": "2015-01-01", "source": "test"},
        {"list_id": "SYN-00002", "entity_type": "Person", "name": "Mary Stoner", "aliases": "",
         "birth_or_incorporation": "1950-02-02", "country": "GB", "program": "SYN-ORGANISED-CRIME",
         "listed_on": "2015-01-01", "source": "test"},
    ]).to_csv(prepared / "watchlist.csv", index=False)

    settings = replace(get_settings(), data_backend="local")
    from casefile.data.local import LocalBackend
    from casefile.evidence import EvidenceStore

    backend = LocalBackend(settings, prepared_dir=prepared)
    store = EvidenceStore(tmp_path / "evidence.duckdb")
    alert = {"alert_id": "ALR-T01", "institution": "A", "bank_id": 1, "account": "S",
             "alert_ts": "2022-09-03 23:00", "rules": "PASS_THROUGH", "trigger": "test"}
    yield {"settings": settings, "backend": backend, "store": store, "alert": alert, "tmp": tmp_path}
    backend.close()
    store.close()
