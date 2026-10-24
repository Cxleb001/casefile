"""Zetaris REST returns every value as text; ids must survive, numbers must be numbers."""

from __future__ import annotations

from dataclasses import replace

from casefile.config import get_settings
from casefile.data.zetaris import ZetarisBackend


class StubZetaris(ZetarisBackend):
    def __init__(self, payload):
        settings = replace(get_settings(), zetaris_route="proxy", zetaris_base_url="http://stub",
                           zetaris_org_id="1", zetaris_api_key="k")
        super().__init__(settings)
        self.payload = payload

    def raw(self, sql, limit=None):
        return self.payload


def test_hex_account_ids_are_not_turned_into_numbers():
    payload = {
        "headers": [{"name": "from_account"}, {"name": "from_bank"}, {"name": "amount_paid"}, {"name": "acct"},
                    {"name": "customer_id"}, {"name": "degree"}],
        "data": [["8729E230", "54557", "1.5E3", "000278764", "1E5", "12"],
                 ["415B39203", "1", "12.50", "00094D7C0", "967B449EF", "0"]],
    }
    result = StubZetaris(payload).query("SELECT ...")
    first, second = result.records()
    assert first["from_account"] == "8729E230" and first["acct"] == "000278764" and first["customer_id"] == "1E5"
    assert first["from_bank"] == 54557 and first["amount_paid"] == 1500.0 and first["degree"] == 12
    assert second["amount_paid"] == 12.5 and second["from_account"] == "415B39203"


def test_columns_passed_by_the_caller_name_bare_rows():
    payload = {"data": [["8729E230", "3"]]}
    result = StubZetaris(payload).query("SELECT ...", columns=["account", "n"])
    assert result.records() == [{"account": "8729E230", "n": 3}]
