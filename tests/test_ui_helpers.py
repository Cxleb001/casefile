"""The analyst view's helpers: Bank B marking, safe text, plain labels."""

from __future__ import annotations

from casefile.ui_helpers import (b_row_flags, bank_b_summary, engine_outcome, inbox_rows, mark_accounts, query_source,
                                 top_reasons)

CASE = {
    "accounts": {"1:AAAA1111": {"institution": "A"}, "2:BBBB2222": {"institution": "B"}},
    "edges": [{"seen_in": ["A"]}, {"seen_in": ["A", "B"]}, {"seen_in": ["B"]}, {"seen_in": ["B"]}],
}
TABLES = {"ledger_a": "inst_a.ledger_a", "kyc_a": "inst_a.kyc_a", "ledger_b": "inst_b.ledger_b",
          "kyc_b": "inst_b.kyc_b_shared", "watchlist": "compliance.watchlist"}


def test_bank_b_accounts_are_tagged_and_text_is_escaped():
    out = mark_accounts("1:AAAA1111 paid 2:BBBB2222 <b>$92k</b> then $280k", CASE)
    assert 'class="cf-acct cf-acct-b"' in out and 'class="cf-acct cf-acct-a"' in out
    assert "<b>" not in out and "&lt;b&gt;" in out          # model text can't inject markup
    assert "$" not in out and out.count("&#36;") == 2       # two amounts are not read as a maths formula


def test_what_bank_b_added_is_counted_from_the_traced_edges():
    assert bank_b_summary(CASE) == {"b_only_tx": 2, "cross_tx": 1, "b_accounts": 1, "tx": 4}


def test_evidence_rows_from_bank_b_are_flagged():
    rows = [["T1", "AAAA1111", "['A']"], ["T2", "BBBB2222", "['A', 'B']"], ["T3", "XXXX", "['B']"]]
    assert b_row_flags(rows, ["tx_id", "account", "seen_in"], {"2:BBBB2222"}) == [False, True, True]


def test_query_source_names_the_institution_from_the_sql():
    assert query_source("SELECT * FROM inst_a.ledger_a UNION SELECT * FROM inst_b.ledger_b", TABLES) == "Banks A + B"
    assert query_source("SELECT * FROM inst_b.kyc_b_shared", TABLES) == "Bank B"
    assert query_source("SELECT * FROM compliance.watchlist", TABLES) == "Compliance"


def test_engine_outcomes_read_as_plain_words():
    assert engine_outcome({"status": "completed"})[0] == "Finished"
    assert engine_outcome({"status": "max-chunks"})[0] == "All steps used"
    label, why = engine_outcome({"status": "errored", "error": "Model did not return valid JSON: {"})
    assert label == "Stopped early" and "not valid JSON" in why


def test_inbox_rows_carry_the_decision_and_the_analysts_call():
    alerts = [{"alert_id": "ALR-1", "bank_id": "1", "account": "AAAA1111", "alert_ts": "2022-09-01 10:00",
               "rules": "FAN_IN|PASS_THROUGH"},
              {"alert_id": "ALR-2", "bank_id": "1", "account": "CCCC", "alert_ts": "", "rules": "FAN_OUT"}]
    report = {"disposition": "ESCALATE", "typology": "CYCLE", "needs_review": True,
              "verification": {"verified": 5, "checked": 1, "total": 7}}
    rows = inbox_rows(alerts, {"ALR-1": report}, {"ALR-1": "SAR filed"})
    assert rows[0]["Decision"] == "Escalate, needs review" and rows[0]["Claims verified"] == "6 of 7"
    assert rows[0]["Rule"] == "fan in, pass through" and rows[0]["Analyst"] == "SAR filed"
    assert rows[1]["Decision"] == "Not investigated" and rows[1]["Analyst"] == ""


def test_top_reasons_skip_rejected_claims():
    report = {"claims": [{"text": "a", "verdict": {"status": "rejected"}}, {"text": "b", "verdict": {"status": "verified"}},
                         {"text": "c", "verdict": {"status": "checked"}}]}
    assert top_reasons(report) == ["b", "c"]
