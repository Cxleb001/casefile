"""The transfer-pattern signals on small hand-built graphs with known answers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.agent.analysis import compute_signals, layered_transfer_signal, one_off_transfer_signal  # noqa: E402
from casefile.agent.case import CaseState  # noqa: E402
from casefile.agent.report import rules_report  # noqa: E402

T0 = datetime(2022, 9, 3, 9, 0)
SUBJECT = ("1", "S")


def tx(i, hours, src, dst, amount=1000.0, fmt="ACH"):
    (fb, fa), (tb, ta) = src, dst
    return {"tx_id": f"T{i:04d}", "ts": T0 + timedelta(hours=hours), "from_bank": fb, "from_account": fa,
            "to_bank": tb, "to_account": ta, "amount_paid": amount, "payment_currency": "US Dollar",
            "amount_received": amount, "receiving_currency": "US Dollar", "payment_format": fmt}


def make_case(rows) -> CaseState:
    case = CaseState("TEST", {"alert_id": "TEST", "bank_id": SUBJECT[0], "account": SUBJECT[1]})
    case.add_transactions(rows, "Q1")
    return case


def test_fan_out_of_one_off_transfers_is_strong():
    rows = [tx(i, 4 * i, SUBJECT, ("2", f"M{i}")) for i in range(9)]  # 9 accounts, one ACH each, 32 hours
    case = make_case(rows)
    sig = one_off_transfer_signal(case, case.subject)
    assert sig["strength"] == 3 and sig["value"]["shape"] == "fan-out" and sig["value"]["out"] == 9
    assert sig["query_ids"] == ["Q1"]
    case.signals = compute_signals(case)
    report = rules_report(case)
    assert report["disposition"] == "ESCALATE" and report["typology"] == "FAN-OUT"


def test_repeat_payees_and_card_purchases_are_not_one_off_transfers():
    rows = []
    for i in range(30):  # the same three payees again and again, plus one-off card purchases
        rows.append(tx(i, 12 * i, SUBJECT, ("2", f"P{i % 3}"), fmt="ACH"))
    rows += [tx(100 + i, 5 * i, SUBJECT, ("3", f"SHOP{i}"), amount=40.0, fmt="Credit Card") for i in range(6)]
    case = make_case(rows)
    sig = one_off_transfer_signal(case, case.subject)
    assert sig["strength"] == 0 and sig["assessment"] == "context"


def test_collect_then_scatter_is_gather_scatter():
    rows = [tx(i, i, ("2", f"IN{i}"), SUBJECT) for i in range(4)]
    rows += [tx(10 + i, 10 + i, SUBJECT, ("2", f"OUT{i}")) for i in range(4)]
    case = make_case(rows)
    sig = one_off_transfer_signal(case, case.subject)
    assert sig["value"]["shape"] == "gather-scatter" and sig["strength"] == 3


def test_money_passed_on_by_transfer_is_layering():
    rows = [tx(1, 0, SUBJECT, ("2", "M1")), tx(2, 1, SUBJECT, ("2", "M2")),
            tx(3, 20, ("2", "M1"), ("2", "N1"), 990.0), tx(4, 30, ("2", "M2"), ("2", "N2"), 980.0),
            tx(5, 200, ("2", "M2"), ("2", "N3"), 970.0)]  # outside 72 hours: not part of the chain
    case = make_case(rows)
    sig = layered_transfer_signal(case, case.subject)
    assert sig["strength"] == 2 and sig["value"] == {"chains": 2}
    assert "T0005" not in sig["tx_ids"]


def test_payment_that_stops_is_not_layering():
    rows = [tx(1, 0, SUBJECT, ("2", "M1")), tx(2, 20, ("2", "M1"), ("3", "SHOP"), 30.0, fmt="Credit Card")]
    case = make_case(rows)
    assert layered_transfer_signal(case, case.subject)["strength"] == 0
