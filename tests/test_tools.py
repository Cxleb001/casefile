from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from casefile.agent.analysis import passes_on, ring_is_plausible
from casefile.agent.case import CaseState, Edge
from casefile.agent.sqlutil import UnsafeSQL, check_readonly, ts_literal
from casefile.agent.tools import Toolbox
from casefile.agent.workup import standard_workup


def _toolbox(world):
    case = CaseState(world["alert"]["alert_id"], world["alert"])
    return Toolbox(world["backend"], world["store"], world["settings"], case), case


# -- SQL safety ------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE inst_a.ledger_a",
    "SELECT 1; DELETE FROM inst_a.ledger_a",
    "SELECT * FROM inst_a.ledger_a -- sneaky",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "CACHE TABLE inst_b.ledger_b",
])
def test_check_readonly_rejects_writes(sql):
    with pytest.raises(UnsafeSQL):
        check_readonly(sql)


def test_check_readonly_allows_select_and_keywords_inside_strings():
    assert check_readonly("SELECT 'drop table' AS note FROM inst_a.ledger_a;").startswith("SELECT")


def test_ts_literal_is_portable():
    assert ts_literal("2022/09/01 00:20") == "TIMESTAMP '2022-09-01 00:20:00'"


# -- amount consistency ------------------------------------------------------------

def _edge(i, src, dst, paid, received, hours):
    return Edge(f"T{i}", datetime(2022, 9, 1) + timedelta(hours=hours), src, dst, paid, "Rupee", received, "US Dollar", "ACH")


def test_passes_on_compares_in_the_middle_accounts_currency():
    # X receives 40 USD (worth 3,200 rupees to the sender) and pays on 38 USD: consistent.
    assert passes_on(_edge(1, "S", "X", 3200, 40, 0), _edge(2, "X", "Y", 38, 38, 2))
    assert not passes_on(_edge(1, "S", "X", 3200, 40, 0), _edge(2, "X", "Y", 400, 400, 2))


def test_ring_allows_one_time_drop_only():
    ring = [_edge(1, "S", "X", 100, 100, 10), _edge(2, "X", "Y", 98, 98, 11), _edge(3, "Y", "S", 95, 95, 1)]
    assert ring_is_plausible(ring)  # the ring started at Y; read from S it has one drop
    two_drops = [_edge(1, "S", "X", 100, 100, 10), _edge(2, "X", "Y", 98, 98, 1), _edge(3, "Y", "S", 95, 95, 5),
                 _edge(4, "S", "S", 95, 95, 2)]
    assert not ring_is_plausible(two_drops)  # 10 > 1 and 5 > 2: money can't go round in one pass
    too_slow = [_edge(1, "S", "X", 100, 100, 0), _edge(2, "X", "S", 98, 98, 24 * 12)]
    assert not ring_is_plausible(too_slow)


# -- tools on the hand-built world ---------------------------------------------------

def test_profile_shows_full_record_for_institution_a(world):
    tb, case = _toolbox(world)
    obs = tb.profile_account()
    assert obs.error is None
    assert obs.data["customer"]["institution"] == "A"
    assert obs.data["customer"]["full_name"] == "Ivan Petrov"
    assert obs.data["activity"]["out"]["n_counterparties"] == 2
    assert len(obs.query_ids) == 2 and all(q.startswith("Q-") for q in obs.query_ids)


def test_institution_b_names_are_never_returned(world):
    tb, case = _toolbox(world)
    obs = tb.profile_account(bank_id=2, account="Y")
    assert obs.data["customer"]["institution"] == "B"
    assert obs.data["customer"]["full_name"] is None
    assert any("withholds" in text for text in case.limitations)


def test_counterparties_join_both_institutions(world):
    tb, case = _toolbox(world)
    obs = tb.get_counterparties(direction="in")
    rows = obs.data["counterparties"]
    assert [r["account"] for r in rows] == ["2:Z"]
    assert rows[0]["institution"] == "B"


def test_workup_finds_the_ring_and_skips_the_hub(world):
    tb, case = _toolbox(world)
    observations = standard_workup(tb)
    assert all(o.error is None for o in observations), [o.error for o in observations if o.error]
    signals = {s["name"]: s for s in case.signals}
    assert signals["cycle"]["assessment"] == "suspicious"
    assert signals["cycle"]["value"]["hops"] == 4
    assert {"T0001", "T0002", "T0003", "T0004"} <= set(signals["cycle"]["tx_ids"])
    assert signals["hubs"]["value"]["hubs"] == 1
    # every signal that cites edges cites the queries those edges came from
    assert signals["cycle"]["query_ids"]
    # the cross-institution leg was seen in both ledgers
    assert case.edges["T0002"].seen_in == {"A", "B"}


def test_screening_finds_strong_subject_match_and_flags_b(world):
    tb, case = _toolbox(world)
    standard_workup(tb)
    obs = next(o for o in case.observations if o.tool == "screen_watchlist")
    subject_hits = [h for h in obs.data["hits"] if h["is_subject"]]
    assert subject_hits and subject_hits[0]["strength"] == "strong"
    assert obs.data["unscreenable_institution_b"] >= 2
    assert any("could not be screened" in text for text in case.limitations)


def test_run_sql_is_read_only_and_limited_to_registered_tables(world):
    tb, case = _toolbox(world)
    bad = tb.call("run_sql", {"sql": "DROP TABLE inst_a.ledger_a"})
    assert bad.error and "SELECT" in bad.error
    hidden = tb.call("run_sql", {"sql": "SELECT full_name FROM inst_b.kyc_b"})
    assert hidden.error
    ok = tb.call("run_sql", {"sql": "SELECT count(*) AS n FROM inst_a.ledger_a", "purpose": "count"})
    assert ok.error is None and ok.data["rows"][0]["n"] == 64  # S->X, X->Y, Z->S, S->H + 60 shoppers


def test_every_query_is_recorded_with_its_sql(world):
    tb, case = _toolbox(world)
    obs = tb.profile_account()
    record = world["store"].get_query(obs.query_ids[0])
    assert "kyc_a" in record["sql"] and record["row_count"] == 1


def test_unknown_tool_and_bad_args_become_observations(world):
    tb, case = _toolbox(world)
    assert tb.call("teleport", {}).error.startswith("Unknown tool")
    assert tb.call("follow_money", {"direction": "sideways"}).error
